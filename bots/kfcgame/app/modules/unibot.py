"""unibot —— 三机器人统一层（2026-10-15 用户拍板）

职责：
  1. Emby 建号/续期/解禁：走 Emby API（与 EmbyBoss 同策略同字段），
     结果落 `embyboss.emby` 共享表 —— boss / game / RT 三边看到的都是同一条。
  2. 签到统一：共享「当日签到标记」（embyboss.emby.ch）。boss 签到本来就
     读写 ch（公版代码，零补丁）；game 打坐开始前查 ch —— 今天已在 boss
     签过就拒；game 签完把 ch 写上当日时间 —— 真·互斥，防刷奖。
  3. V积分派送：群主无限额度，任意金额转给任意 TG 用户（embyboss.emby.iv 列，
     即 boss 的「V积分」钱包），流水落 kfc_point_transfer 供 Web/对账。
  4. 入群自动建档：任何人进群 → embyboss.emby 建行 + kfc_members 记录
     UID/实时用户名（改名后续期/求片场景会顺手刷新）。

⚠️ 红线：
  · 绝不改 RT 的 sqlite（闭源 Go + WAL，写坏 = 85 个号全炸）；RT 只读。
  · boss 镜像只叠**可整体移除的补丁层**（uniboss.py 等），公版文件不动。
  · emby.iv（V积分钱包）/ ch（签到标记）是 boss 在用的列，写入幂等、
    值格式与 boss 一致；emby.us 是 boss 的【注册天数池】，**严禁当积分用**。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import string

import requests
from sqlalchemy import text

from .. import db
from .. import settings as S
from . import rtapi

logger = logging.getLogger("unibot")

# Emby API（game 自己的 config 里就有，与 boss 指同一台 KFC 8096）
_EMBY = S.EMBY or {}
SHARED = S.SHARED_DB          # "embyboss"


# ───────────────────────── Emby 建号 ─────────────────────────
def gen_password(n: int = 8) -> str:
    """随机密码（大小写+数字，与 boss pwd_create 同风格）"""
    alpha = string.ascii_letters + string.digits
    return "".join(secrets.choice(alpha) for _ in range(n))


def _emby_api(path: str, method: str = "GET", **kw):
    url = (_EMBY.get("url") or "").rstrip("/") + path
    p = dict(kw.pop("params", {}) or {})
    p["api_key"] = _EMBY.get("api_key") or ""
    kw.setdefault("timeout", 15)
    if method == "POST":
        return requests.post(url, params=p, **kw)
    if method == "DELETE":
        return requests.delete(url, params=p, **kw)
    return requests.get(url, params=p, **kw)


# 与 boss create_policy 逐字段对齐（公版默认 + 用户实测值）：
# 隐藏、非管理员、2 路同播、屏蔽「播放列表」等额外库
_POLICY = {
    "IsAdministrator": False,
    "IsHidden": True,
    "IsHiddenRemotely": True,
    "IsDisabled": False,
    "EnableRemoteControlOfOtherUsers": False,
    "EnableSharedDeviceControl": False,
    "EnableRemoteAccess": True,
    "EnableLiveTvManagement": False,
    "EnableLiveTvAccess": True,
    "EnableMediaPlayback": True,
    "EnableAudioPlaybackTranscoding": False,
    "EnableVideoPlaybackTranscoding": False,
    "EnablePlaybackRemuxing": False,
    "EnableContentDeletion": False,
    "EnableContentDownloading": False,
    "EnableSubtitleDownloading": False,
    "EnableSubtitleManagement": False,
    "EnableSyncTranscoding": False,
    "EnableMediaConversion": False,
    "EnableAllDevices": True,
    "SimultaneousStreamLimit": 2,
    "AllowCameraUpload": False,
}
# boss 默认屏蔽库（emby_block/extra）里最关键的一个：R级电影由 lib_block 控制，
# game 侧建号统一屏蔽成人库；用户在 Web 可调。
_BLOCK_FOLDERS = ["播放列表", "R级电影"]


def create_emby_user(name: str, days: int = 30,
                     password: str | None = None,
                     tg: int | None = None, pwd2: str = "") -> dict:
    """在 KFC Emby 上开号并写好 embyboss.emby —— boss/RT 立即可见可管。

    返回 {"ok":bool, "embyid":str, "name":str, "pwd":str, "ex":datetime,
          "msg":str}；失败不抛异常。tg 传入时把行绑到该用户（boss 台账）；
    pwd2=安全码（敏感操作验证，Boss 同语义）。
    """
    if not (_EMBY.get("url") and _EMBY.get("api_key")):
        return {"ok": False, "msg": "bot 未配置 Emby 连接"}
    password = password or gen_password(8)
    try:
        r = _emby_api("/emby/Users/New", "POST", json={"Name": name})
        if r.status_code not in (200, 201):
            # 同名已存在 → Emby 返回 400，给出人话
            return {"ok": False, "msg": f"Emby 创建失败 HTTP {r.status_code}（重名？）"}
        uid = (r.json() or {}).get("Id")
        if not uid:
            return {"ok": False, "msg": "Emby 未返回用户ID"}
        r2 = _emby_api(f"/emby/Users/{uid}/Password", "POST", json={"Id": str(uid), "NewPw": password})
        if r2.status_code not in (200, 204):
            return {"ok": False, "msg": f"设密码失败 HTTP {r2.status_code}"}
        pol = dict(_POLICY)
        pol["BlockedMediaFolders"] = _BLOCK_FOLDERS
        r3 = _emby_api(f"/emby/Users/{uid}/Policy", "POST", json=pol)
        if r3.status_code not in (200, 204):
            logger.warning(f"策略下发失败 HTTP {r3.status_code}（号已建，走默认策略）")
        now = dt.datetime.now()
        ex = now + dt.timedelta(days=int(days))
        _record_in_boss(name, password, uid, now, ex, tg=tg, pwd2=pwd2)
        return {"ok": True, "embyid": str(uid), "name": name,
                "pwd": password, "ex": ex, "msg": "开号成功"}
    except Exception as e:
        logger.warning(f"create_emby_user 异常: {e}")
        return {"ok": False, "msg": f"开号异常: {str(e)[:80]}"}


def bind_after_open(tg: int, embyid: str, name: str, password: str,
                    tg_name: str = "", tg_username: str = "") -> None:
    """开号成功后的三处绑定（与 bind.py 的验证通过同语义）：
    ① boss 台账行已由 create_emby_user 写好；② kfc_bind 供榜单；
    ③ kfc_emby_bind 存 AES 密文密码（Web 数据库页可查）。"""
    ensure_member(tg, tg_name, tg_username)
    k = db._ka_key()
    ts = int(__import__("time").time())
    with db._engine.begin() as c:
        c.execute(text(
            "INSERT INTO kfc_bind (tg, emby_name) VALUES (:tg, :n) "
            "ON DUPLICATE KEY UPDATE emby_name = VALUES(emby_name), ts = NOW()"),
            {"tg": int(tg), "n": name[:64]})
        c.execute(text(
            "INSERT INTO kfc_emby_bind (tg, tg_name, tg_username, status, "
            "emby_name, emby_uid, pw_hex, bound_ts, updated_ts) "
            "VALUES (:tg, :tn, :tu, 'bound', :n, :uid, "
            "HEX(AES_ENCRYPT(:pw, :k)), :ts, :ts) "
            "ON DUPLICATE KEY UPDATE status='bound', emby_name=VALUES(emby_name), "
            "emby_uid=VALUES(emby_uid), pw_hex=VALUES(pw_hex), "
            "fail_count=0, bound_ts=VALUES(bound_ts), updated_ts=VALUES(updated_ts)"),
            {"tg": int(tg), "tn": (tg_name or "")[:64], "tu": (tg_username or "")[:64],
             "n": name[:64], "uid": str(embyid)[:32], "pw": password, "k": k, "ts": ts})


def _record_in_boss(name, pwd, embyid, cr, ex, tg=None, pwd2: str = ""):
    """把新号写进 embyboss.emby（boss 的账本）。有 tg 就 UPDATE 那行，
    没有就 INSERT（name 唯一索引，重名加后缀重试）。
    ★ 2026-11-04 修复：UPDATE 影响 0 行（该 tg 尚无行）时补 INSERT，
      否则会出现「Emby 已建号、boss 账本查无此人」的孤儿账号。
    pwd2=安全码（Boss 同语义，敏感操作验证用）。"""
    import pymysql
    with db._engine.begin() as c:
        if tg:
            u = c.execute(text(
                f"UPDATE `{SHARED}`.emby SET embyid=:eid, name=:n, pwd=:p, "
                f"pwd2=:p2, lv='b', cr=:cr, ex=:ex WHERE tg=:tg"),
                {"eid": embyid, "n": name, "p": pwd, "p2": (pwd2 or ""),
                 "cr": cr, "ex": ex, "tg": int(tg)})
            if u.rowcount:
                return
            # 无行 → 补 INSERT；name 唯一冲突时加后缀重试
            base = name
            for i in range(6):
                nm = base if i == 0 else f"{base}{i}"
                try:
                    c.execute(text(
                        f"INSERT INTO `{SHARED}`.emby "
                        f"(tg, embyid, name, pwd, pwd2, lv, cr, ex, us, iv) "
                        f"VALUES (:tg, :eid, :n, :p, :p2, 'b', :cr, :ex, 0, 0)"),
                        {"tg": int(tg), "eid": embyid, "n": nm, "p": pwd,
                         "p2": (pwd2 or ""), "cr": cr, "ex": ex})
                    return
                except pymysql.err.IntegrityError:
                    continue
            return
        c.execute(text(
            f"INSERT INTO `{SHARED}`.emby (tg, embyid, name, pwd, pwd2, lv, cr, ex, us, iv) "
            f"VALUES (0, :eid, :n, :p, :p2, 'b', :cr, :ex, 0, 0)"),
            {"eid": embyid, "n": name, "p": pwd, "p2": (pwd2 or ""),
             "cr": cr, "ex": ex})


def renew_emby(tg: int, days: int = 30) -> dict:
    """续期：只动 ex（过期时间），与 boss 续期同语义。lv 顺带回 'b'（解禁外）。"""
    with db._engine.begin() as c:
        r = c.execute(text(
            f"SELECT embyid, ex, lv FROM `{SHARED}`.emby WHERE tg=:tg"),
            {"tg": int(tg)}).first()
        if not r or not r[0]:
            return {"ok": False, "msg": "此人还没有 Emby 账号"}
        base = max(dt.datetime.now(), r[1] or dt.datetime.now())
        ex_new = base + dt.timedelta(days=int(days))
        c.execute(text(
            f"UPDATE `{SHARED}`.emby SET ex=:ex, lv=IF(lv='c', lv, 'b') WHERE tg=:tg"),
            {"ex": ex_new, "tg": int(tg)})
    # ★ 2026-11-03 续期同步 RT（best-effort）：让 RT 面板/RT bot 同一套到期
    try:
        if rtapi.enabled():
            rr = rtapi.renew_user(tg, days)
            logger.info(f"续期→RT 同步 tg={tg} {days}天: {rr.get('msg')}")
    except Exception as e:
        logger.warning(f"续期→RT 同步异常 tg={tg}: {e}")
    return {"ok": True, "ex": ex_new, "embyid": str(r[0])}


def emby_account_of(tg: int) -> dict | None:
    """查某 TG 用户的 embyboss 账本行（Web/播报/改名刷新都用它）"""
    with db._engine.connect() as c:
        r = c.execute(text(
            f"SELECT tg, embyid, name, pwd, pwd2, lv, cr, ex, us, iv, ch "
            f"FROM `{SHARED}`.emby WHERE tg=:tg"), {"tg": int(tg)}).first()
    return dict(r._mapping) if r else None


# ───────────────────────── 签到互斥（共享 ch） ─────────────────────────
def checkin_state(tg: int) -> dict:
    """今天是否已在别处（EmbyBoss）签到 —— 读共享 ch 列。

    返回 {"today": bool, "at": datetime|None}；无行/查询失败保守视为未签。
    """
    try:
        acc = emby_account_of(tg)
    except Exception:
        return {"today": False, "at": None}
    if not acc:
        return {"today": False, "at": None}
    ch = acc.get("ch")
    if isinstance(ch, str):
        try:
            ch = dt.datetime.strptime(ch[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            ch = None
    if ch and ch.date() == dt.date.today():
        return {"today": True, "at": ch}
    return {"today": False, "at": ch}


def checkin_mark(tg: int) -> None:
    """game 打坐成功后把 ch 写成现在 —— boss 那边今天再点签到就会自己提示
    「已经签到过」，天然互斥，boss 零补丁。行不存在则先补建行。"""
    now = dt.datetime.now()
    with db._engine.begin() as c:
        n = c.execute(text(
            f"UPDATE `{SHARED}`.emby SET ch=:ts WHERE tg=:tg"),
            {"ts": now, "tg": int(tg)}).rowcount
        if not n:
            c.execute(text(
                f"INSERT INTO `{SHARED}`.emby (tg, name, lv, us, iv, ch) "
                f"VALUES (:tg, NULL, 'd', 0, 0, :ts)"),
                {"tg": int(tg), "ts": now})


# ───────────────────────── 入群自动建档 ─────────────────────────
def ensure_member(tg: int, name: str = "", username: str = "") -> None:
    """任何人进群/私聊过 bot：embyboss 账本建行 + kfc_members 建档。
    已存在就顺手刷新实时用户名（改名自动跟进）。幂等、静默失败。"""
    name = db._clean_name(name)[:64]
    username = db._clean_name(username)[:64]
    try:
        db.ensure_shared_user(tg)
        # ★ 入群捕获：tg_members 落库（embyboss.embyboss.tg_members）。
        #   建表单独执行，避免 DDL 嵌在事务里出问题；已存在则忽略。
        try:
            with db._engine.begin() as c:
                c.execute(text(
                    f"CREATE TABLE IF NOT EXISTS `{SHARED}`.tg_members ("
                    f"tg BIGINT PRIMARY KEY, username VARCHAR(64), "
                    f"joined_at DATETIME)"))
        except Exception as e:
            logger.debug(f"tg_members 建表失败（已存在可忽略） {tg}: {e}")
        with db._engine.begin() as c:
            # 首见记录 joined_at，之后只刷新 username
            c.execute(text(
                f"INSERT INTO `{SHARED}`.tg_members (tg, username, joined_at) "
                f"VALUES (:tg, :u, NOW()) "
                f"ON DUPLICATE KEY UPDATE username=VALUES(username)"),
                {"tg": int(tg), "u": username})
            # ★ 2026-11-04 修复（用户投诉）：**已开号用户绝不能用 TG 用户名覆盖
            #   Emby 用户名**（曾把自设的账号名刷成 TG 名，登录名被改）。
            #   只对「还没开号」的行补显示名。
            if username:
                c.execute(text(
                    f"UPDATE `{SHARED}`.emby SET name=:u "
                    f"WHERE tg=:tg AND (name IS NULL OR embyid IS NULL OR embyid='')"),
                    {"tg": int(tg), "u": username})
            c.execute(text(
                "INSERT INTO kfc_members (tg, tg_name, tg_username, first_ts, last_seen) "
                "VALUES (:tg, :n, :u, NOW(), NOW()) "
                "ON DUPLICATE KEY UPDATE tg_name=VALUES(tg_name), "
                "tg_username=VALUES(tg_username), last_seen=NOW()"),
                {"tg": int(tg), "n": name, "u": username})
    except Exception as e:
        logger.debug(f"ensure_member 失败 {tg}: {e}")


def refresh_name(tg: int, name: str = "", username: str = "") -> None:
    """播报前刷新实时用户名（签到/续期/求片等一切个人场景调用）。"""
    ensure_member(tg, name, username)


def member_of(tg: int) -> dict | None:
    """读 kfc_members 建档（tg/tg_name/tg_username/last_seen）。"""
    try:
        with db._engine.connect() as c:
            r = c.execute(text(
                "SELECT tg, tg_name, tg_username, first_ts, last_seen "
                "FROM kfc_members WHERE tg = :tg"), {"tg": int(tg)}).first()
        return dict(r._mapping) if r else None
    except Exception as e:
        logger.debug(f"member_of 失败 {tg}: {e}")
        return None


def find_by_username(username: str) -> int | None:
    """按 TG 用户名查 kfc_members 里的 UID（/kk @某人 的解析兜底）"""
    try:
        with db._engine.connect() as c:
            r = c.execute(text(
                "SELECT tg FROM kfc_members WHERE tg_username=:u "
                "ORDER BY last_seen DESC LIMIT 1"),
                {"u": (username or "").lstrip("@")[:64]}).first()
        return int(r[0]) if r else None
    except Exception:
        return None


# ───────────────────────── V积分派送（群主特权） ─────────────────────────
def transfer_points(from_uid: int, to_uid: int, amount: int,
                    note: str = "") -> dict:
    """群主向任意人派送任意金额 V积分。

    群主（S.OWNER 或 perm 'admin'）视作**无限额度**：不做余额校验、不扣减；
    每次派送写流水表 kfc_point_transfer，Web 可查、防内部纠纷。
    """
    amount = int(amount)
    if amount <= 0:
        return {"ok": False, "msg": "金额必须为正整数"}
    if int(to_uid) == int(from_uid):
        return {"ok": False, "msg": "不能给自己派送"}
    newbal = db.add_points(to_uid, amount)
    try:
        with db._engine.begin() as c:
            c.execute(text(
                "INSERT INTO kfc_point_transfer (from_tg, to_tg, amount, to_balance, note) "
                "VALUES (:f, :t, :a, :b, :n)"),
                {"f": int(from_uid), "t": int(to_uid), "a": amount,
                 "b": newbal, "n": (note or "")[:120]})
    except Exception as e:
        logger.warning(f"派送流水写入失败: {e}")
    return {"ok": True, "to_balance": newbal, "amount": amount}


def transfer_log(limit: int = 30) -> list[dict]:
    with db._engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, from_tg, to_tg, amount, to_balance, note, ts "
            "FROM kfc_point_transfer ORDER BY id DESC LIMIT :l"),
            {"l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════
#  统一码仓铸造（2026-10-15 用户拍板：两边同价同货、汇率统一）
# ════════════════════════════════════════════════════════════
# game 兑换阁卖出码 → 真正往 boss 码仓（embyboss.Rcode）铸一枚可用码。
# boss 消费语义（源码实锤）：
#   注册码  前缀 '{ranks.logo}-{times}-Register_{p}'，logo=SAKURA（config
#           ranks.logo），code_prefix ∈ logo 或 ==tg 才放行 → 铸给买家时用
#           f'SAKURA-{tg}-Register_{rand}' 让「前缀==买家UID」校验通过。
#   续期码  含 'Renew' 即走续期分支，us=天数 → 续 30/90/365。
#   白名单  含 'Whitelist'。
# Rcode 列：code(PK) tg us used usedtime —— tg=签发人（记群主），
# used/usedtime 由 boss 兑换时回写。
import random as _rand

_CODE_ALPHA = string.ascii_uppercase + string.digits


def _rnd(n: int = 10) -> str:
    return "".join(_rand.choice(_CODE_ALPHA) for _ in range(n))


def mint_boss_code(kind: str, days: int, buyer_tg: int,
                   issuer_tg: int) -> dict:
    """往 embyboss.Rcode 铸一枚真码。kind: invite/renew/whitelist。
    返回 {"ok":bool,"code":str,"msg":str}。铸出的码在【两个机器人都能用】：
    boss 直接兑换；game 走「boss_verify」通道代兑。"""
    kind = (kind or "").strip().lower()
    buyer_tg, issuer_tg = int(buyer_tg), int(issuer_tg)
    logo = _boss_logo()
    if kind == "invite":
        # ★ 2026-11-04 用户拍板：KFC-天数-Register-随机码（照搬 Boss 的天数段，
        #   且全用短横线、绝不出现下划线）。Boss 只校验第一段(KFC)，天数段安全。
        code = f"{logo}-{int(days)}-Register-{_rnd(10)}"
    elif kind == "renew":
        code = f"{logo}-Renew-{days}-{_rnd(10)}"
    elif kind == "whitelist":
        code = f"{logo}-Whitelist-{_rnd(10)}"
    else:
        return {"ok": False, "msg": "未知类型 %s" % kind}
    try:
        with db._engine.begin() as c:
            c.execute(text(
                "INSERT INTO `embyboss`.Rcode (code, tg, us, used, usedtime) "
                "VALUES (:c, :t, :u, NULL, NULL)"),
                {"c": code[:50], "t": issuer_tg, "u": int(days)})
        # 同步在 game 码仓登记一条（保持 /mycodes、库存口径一致）
        try:
            db.add_codes("boss:" + kind[:3], [code])
        except Exception:
            pass
        return {"ok": True, "code": code, "kind": kind, "days": days}
    except Exception as e:
        logger.warning(f"铸 boss 码失败 {kind}: {e}")
        return {"ok": False, "msg": str(e)[:80]}


def boss_stock() -> dict:
    """boss 码仓存量（未用），供 Web/对账。"""
    try:
        with db._engine.connect() as c:
            rows = c.execute(text(
                "SELECT CASE WHEN code LIKE '%Register%' THEN 'invite' "
                "WHEN code LIKE '%Renew%' THEN 'renew' "
                "WHEN code LIKE '%Whitelist%' THEN 'whitelist' END AS k, "
                "COUNT(*) FROM embyboss.Rcode WHERE used IS NULL GROUP BY k")).all()
        return {str(r[0] or "other"): int(r[1]) for r in rows}
    except Exception:
        return {}


def _boss_logo() -> str:
    """签发前缀：优先 game config 的 boss.logo（与 boss ranks.logo 保持一致，
    现值 KFC）。boss 校验 `code前缀 ∈ logo 或 == 买家UID`，两边必须同步。"""
    try:
        return str((S.CFG.get("boss") or {}).get("logo") or "KFC")
    except Exception:
        return "KFC"


def peek_boss_code(code: str) -> dict:
    """预检 boss 码（不占码）：{"ok","kind","days","msg"}。
    供注册流程先验码、再收集用户名/安全码（Boss 同语义），防占码后失败。"""
    norm = (code or "").strip().replace("-", "").replace("_", "")
    if len(norm) < 6:
        return {"ok": False, "msg": "码格式不对"}
    try:
        with db._engine.connect() as c:
            r = c.execute(text(
                "SELECT code, us, used FROM embyboss.Rcode "
                "WHERE REPLACE(REPLACE(code,'-',''),'_','')=:c LIMIT 1"),
                {"c": norm[:64]}).first()
        if not r:
            return {"ok": False, "msg": "这枚码不在 boss 码仓（或已删除）"}
        if r[2]:
            return {"ok": False, "msg": "此码已被使用"}
        code_s = str(r[0])
        days = int(r[1] or 30)
        kind = ("invite" if "Register" in code_s else
                "renew" if "Renew" in code_s else
                "whitelist" if "Whitelist" in code_s else "invite")
        return {"ok": True, "kind": kind, "days": days}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:80]}


def redeem_boss_code_in_game(code: str, buyer_tg: int,
                             username: str | None = None,
                             pwd2: str | None = None) -> dict:
    """game 端代兑 boss 码（用户拍板：在哪个 bot 买都能全局用）。

    直接按 boss 的兑换语义操作 embyboss.Rcode + emby 行：
      · 注册码 → us 天数池 +code.us（boss 侧「创建账户」消耗 us 开号）
      · 续期码 → ex 直接 +code.us 天（与 boss _redeem_renew 同式）
      · 白名单 → lv='a'
    幂等防双花：UPDATE ... WHERE used IS NULL 条件更新，rowcount==0 即已被用。
    """
    code = (code or "").strip()
    if len(code) < 6:
        return {"ok": False, "msg": "码格式不对"}
    # ★ 2026-10-23 用户反馈：`KFC-1081104971-Register_JRTCSJ2N6B` 常被输成
    #   `KFC1081104971RegisterJRTCSJ2N6B`（丢了 - 和 _）→ 老代码精确匹配直接报
    #   「这枚码不在 boss 码仓」。现在统一按「只保留字母数字」比对，两种写法都能兑。
    norm = "".join(ch for ch in code if ch.isalnum())
    if len(norm) < 6:
        return {"ok": False, "msg": "码格式不对"}
    buyer_tg = int(buyer_tg)
    try:
        with db._engine.connect() as c:
            r = c.execute(text(
                "SELECT code, us, used FROM embyboss.Rcode "
                "WHERE REPLACE(REPLACE(code,'-',''),'_','')=:c LIMIT 1"),
                {"c": norm[:64]}).first()
        if not r:
            return {"ok": False, "msg": "这枚码不在 boss 码仓（或已删除）"}
        if r[2]:
            return {"ok": False, "msg": "此码已被使用（used=%s）" % r[2]}
        code = str(r[0])          # ★ 后续一律用码仓里的真实码（带分隔符）
        days = int(r[1] or 30)
        kind = ("invite" if "Register" in code else
                "renew" if "Renew" in code else
                "whitelist" if "Whitelist" in code else "invite")
        db.ensure_shared_user(buyer_tg)
        # 与 boss 同规的前置校验（防白扣）：
        #   注册码 → 已有账户/已有资格都拒；续期/白名单 → 必须先有账户
        acc = emby_account_of(buyer_tg) or {}
        if kind == "invite" and (acc.get("embyid") or int(acc.get("us") or 0) > 0):
            return {"ok": False,
                    "msg": ("你已有账户" if acc.get("embyid")
                            else "已有注册资格（天数池 %s 天），请勿重复存码" % acc.get("us"))}
        if kind != "invite" and not acc.get("embyid"):
            return {"ok": False, "msg": "续期/白名单需要先拥有 Emby 账户"}
        with db._engine.begin() as c:
            # ① 先原子占码（防双花：boss 那边同一瞬间用也只会成功一边）
            u = c.execute(text(
                "UPDATE embyboss.Rcode SET used=:tg, usedtime=NOW() "
                "WHERE code=:c AND used IS NULL"),
                {"tg": buyer_tg, "c": code[:50]})
            if u.rowcount != 1:
                return {"ok": False, "msg": "此码刚刚被别处兑换了（防双花拦截）"}
            # ② 按类型发放权益
            if kind == "invite":
                c.execute(text(
                    "UPDATE embyboss.emby SET us = us + :d WHERE tg=:tg"),
                    {"d": days, "tg": buyer_tg})
            elif kind == "renew":
                c.execute(text(
                    "UPDATE embyboss.emby SET ex = GREATEST(IFNULL(ex, NOW()), NOW()) "
                    "+ INTERVAL :d DAY, lv=IF(lv='c','b',lv) WHERE tg=:tg"),
                    {"d": days, "tg": buyer_tg})
            else:
                c.execute(text(
                    "UPDATE embyboss.emby SET lv='a' WHERE tg=:tg"), {"tg": buyer_tg})
        # ★ 2026-10-23 用户要求：注册码兑换后**直接开号**（不再只加天数池）。
        #   ★ 2026-11-04 改走 open_account 双通道（RT 优先，未配置/失败回退
        #     Emby 直连）——原先 RT 未配置时只能入池开不了号，新人被卡死。
        #   放在事务外：外部 HTTP 调用不能长时间持库锁。
        opened = None
        rt_msg = ""
        if kind == "invite" and not acc.get("embyid"):
            r = open_account(buyer_tg, days, username=username, pwd2=pwd2)
            if r.get("ok"):
                opened = {"username": r["username"], "password": r["password"],
                          "embyid": r["embyid"]}
            else:
                rt_msg = ("（自动开号未成功：%s，天数已入池，"
                          "可点「👑 创建账户」或去 Boss 手动开通）" % r.get("msg"))
        # ④ game 码仓若有登记则同步标记（对账）
        try:
            with db._engine.begin() as c:
                c.execute(text(
                    "UPDATE kfc_redeem_code SET used_by=:tg, used_at=NOW() "
                    "WHERE code=:c AND used_by IS NULL"),
                    {"tg": buyer_tg, "c": code[:64]})
        except Exception:
            pass
        # ★ 2026-11-03 续期同步 RT + 溯源记录（best-effort）
        try:
            if rtapi.enabled():
                if kind == "renew":
                    rr = rtapi.renew_user(buyer_tg, days)
                    logger.info(f"续期→RT 同步 tg={buyer_tg} {days}天: {rr.get('msg')}")
                # 溯源：记本次核销的码（注册码→open_code，续期码→last_renew_code）
                col = ("open_code" if kind == "invite" else
                       "last_renew_code" if kind == "renew" else None)
                if col:
                    with db._engine.begin() as c2:
                        c2.execute(text(
                            f"UPDATE `{S.SHARED_DB}`.emby SET {col}=:c WHERE tg=:tg"),
                            {"c": code[:64], "tg": buyer_tg})
        except Exception as e:
            logger.warning(f"RT 同步/溯源异常 tg={buyer_tg}: {e}")
        if opened:
            return {"ok": True, "kind": kind, "days": days, "opened": opened,
                    "msg": ("✅ 账号已开通\n"
                            "· 用户名 | %s\n· 密码 | %s\n· 有效期 | %d 天\n"
                            "· Emby 与 RT 面板同一套账号密码" %
                            (opened["username"], opened["password"], days))}
        return {"ok": True, "kind": kind, "days": days,
                "msg": {"invite": "已入账 %d 天注册资格（boss 侧「创建账户」开通）%s" % (days, rt_msg),
                        "renew": "已续 %d 天" % days,
                        "whitelist": "已升级为白名单"}[kind]}
    except Exception as e:
        logger.warning(f"代兑 boss 码异常: {e}")
        return {"ok": False, "msg": str(e)[:80]}


def void_boss_code(code: str) -> None:
    """作废一枚 boss 码（发码失败回滚时用）：从 Rcode 删除未使用的该码。"""
    try:
        with db._engine.begin() as c:
            c.execute(text(
                "DELETE FROM embyboss.Rcode WHERE code=:c AND used IS NULL"),
                {"c": (code or "")[:50]})
    except Exception as e:
        logger.warning(f"作废 boss 码失败 {code}: {e}")


# ───────────────────── 创建账户（Boss「👑 创建账户」同语义） ─────────────────────
def open_account(tg: int, days: int, username: str | None = None,
                 pwd2: str | None = None) -> dict:
    """开号（双通道）：优先 RT 建号（三端同步）；RT 未启用/失败 →
    回退 Emby 直连开号（/kk 同款，unibot.create_emby_user）。

    ★ 2026-11-04 实测：kfcgame config.json 的 rt 段为空 → rtapi.enabled()=False，
      原先兑换码/创建账户都只能入天数池开不了号。双通道后任意情况都能真开号。
    ★ 2026-11-04 照搬 Boss：username=自定义用户名（默认 kfc<tg>），
      pwd2=安全码（敏感操作验证）。开号成功后 us 清零。
    返回 {"ok","username","password","embyid","days"} 或 {"ok":False,"msg"}。
    """
    tg = int(tg)
    days = max(1, int(days))
    uname = (username or "kfc%d" % tg).strip()[:32] or ("kfc%d" % tg)
    sec = (pwd2 or "").strip()
    # 通道一：RT（三端账号密码一致）
    if rtapi.enabled():
        try:
            pwd = rtapi.gen_password(8)
            r = rtapi.create_user(tg, uname, pwd, days)
            if r.get("ok"):
                with db._engine.begin() as c:
                    c.execute(text(
                        f"UPDATE `{SHARED}`.emby SET embyid=:eid, name=:nm, pwd=:pw, "
                        f"pwd2=:p2, cr=NOW(), ex=NOW() + INTERVAL :d DAY, "
                        f"lv=IF(lv='d','b',lv), us=0 WHERE tg=:tg"),
                        {"eid": r["emby_user_id"], "nm": uname, "pw": pwd,
                         "p2": sec, "d": days, "tg": tg})
                return {"ok": True, "username": uname, "password": pwd,
                        "embyid": r["emby_user_id"], "days": days}
            logger.warning(f"RT 开号未成功（{r.get('msg')}），回退 Emby 直连")
        except Exception as e:
            logger.warning(f"RT 开号异常，回退 Emby 直连: {e}")
    # 通道二：Emby 直连兜底（create_emby_user 内部已写 boss 账本，含 pwd2）
    res = create_emby_user(uname, days, None, tg=tg, pwd2=sec)
    if res.get("ok"):
        try:
            with db._engine.begin() as c:
                c.execute(text(f"UPDATE `{SHARED}`.emby SET us=0 WHERE tg=:tg"),
                          {"tg": tg})
        except Exception:
            pass
        return {"ok": True, "username": res["name"], "password": res["pwd"],
                "embyid": res["embyid"], "days": days}
    return {"ok": False, "msg": res.get("msg") or "开号失败"}


# ───────────────────────── 删除账号（404 容错） ─────────────────────────
def delete_emby_account(tg: int) -> dict:
    """删除某 TG 用户的 Emby 账号。

    ① 调 Emby 删服务端用户；② 无论成功还是「404 资源不存在」（该用户已在
       服务端消失，常见于 Emby 重装/迁移 GUID 改变），都清理本地 boss 账本与
       密码表，避免悬空记录永远删不掉（Boss 旧逻辑对 404 无容错，正是
       「删除账号一直报错」的根因）。
    返回 {"ok":bool,"msg":str}。
    """
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    try:
        if eid:
            r = _emby_api(f"/emby/Users/{eid}", "DELETE")
            if r.status_code not in (200, 204):
                # 404 = 服务端已无此用户 → 视为已删；其余状态码才真失败
                if r.status_code != 404:
                    return {"ok": False,
                            "msg": f"Emby 删除失败 HTTP {r.status_code}"}
                logger.warning(f"delete_emby_account: Emby 端已不存在 {eid}，"
                               f"按已删处理（清理本地记录）")
        with db._engine.begin() as c:
            c.execute(text(
                f"UPDATE `{SHARED}`.emby SET embyid=NULL, name=NULL, pwd=NULL, "
                f"pwd2=NULL, lv='d', cr=NULL, ex=NULL WHERE tg=:tg"),
                {"tg": int(tg)})
            c.execute(text("DELETE FROM kfc_emby_bind WHERE tg=:tg"),
                      {"tg": int(tg)})
            # ★ 2026-11-04 修复 1048：kfc_bind.emby_name 是 NOT NULL 列，
            #   SET NULL 会 IntegrityError；删号就该整行删（榜单也不再显示）
            c.execute(text("DELETE FROM kfc_bind WHERE tg=:tg"), {"tg": int(tg)})
        return {"ok": True, "msg": "账号已删除（含本地记录清理）"}
    except Exception as e:
        logger.warning(f"delete_emby_account 异常 tg={tg}: {e}")
        return {"ok": False, "msg": f"删除异常: {str(e)[:80]}"}


def reset_emby_password(tg: int) -> dict:
    """随机重置密码（兜底用；不动安全码 pwd2）。

    ★ 2026-11-04 修复：原实现把 pwd2 也写成新密码，导致安全码失效
      （用户第二次验安全码失败）。pwd2 是安全码，绝不能动。
    返回 {"ok":bool,"pwd":str,"msg":str}。
    """
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "该用户还没有 Emby 账号"}
    new_pw = gen_password(10)
    try:
        r = _emby_api(f"/emby/Users/{eid}/Password", "POST",
                      json={"Id": str(eid), "NewPw": new_pw})
        if r.status_code not in (200, 204):
            return {"ok": False, "msg": f"Emby 重置失败 HTTP {r.status_code}"}
        with db._engine.begin() as c:
            c.execute(text(
                f"UPDATE `{SHARED}`.emby SET pwd=:pw WHERE tg=:tg"),
                {"pw": new_pw, "tg": int(tg)})
            c.execute(text(
                "UPDATE kfc_emby_bind SET pw_hex=HEX(AES_ENCRYPT(:pw, :k)) "
                "WHERE tg=:tg"),
                {"pw": new_pw, "k": db._ka_key(), "tg": int(tg)})
        return {"ok": True, "pwd": new_pw, "msg": "密码已重置"}
    except Exception as e:
        logger.warning(f"reset_emby_password 异常 tg={tg}: {e}")
        return {"ok": False, "msg": f"重置异常: {str(e)[:80]}"}


# ───────────────────── 用户自设密码 / 改用户名（Boss 重置密码语义） ─────────────────────
def set_emby_password(tg: int, new_pw: str) -> dict:
    """把密码改成用户自己指定的新密码（安全码验证后才会走到这里）。

    ★ 与 reset_emby_password 的区别：这里是用户自设新密码，且**只改 pwd，
      绝不动 pwd2（安全码）**。返回 {"ok","pwd","msg"}。
    """
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "该用户还没有 Emby 账号"}
    new_pw = (new_pw or "").strip()
    if not (6 <= len(new_pw) <= 32):
        return {"ok": False, "msg": "密码需 6~32 位"}
    try:
        r = _emby_api(f"/emby/Users/{eid}/Password", "POST",
                      json={"Id": str(eid), "NewPw": new_pw})
        if r.status_code not in (200, 204):
            return {"ok": False, "msg": f"Emby 改密失败 HTTP {r.status_code}"}
        with db._engine.begin() as c:
            c.execute(text(f"UPDATE `{SHARED}`.emby SET pwd=:pw WHERE tg=:tg"),
                      {"pw": new_pw, "tg": int(tg)})
            c.execute(text(
                "UPDATE kfc_emby_bind SET pw_hex=HEX(AES_ENCRYPT(:pw, :k)) "
                "WHERE tg=:tg"), {"pw": new_pw, "k": db._ka_key(), "tg": int(tg)})
        return {"ok": True, "pwd": new_pw, "msg": "密码已更新"}
    except Exception as e:
        logger.warning(f"set_emby_password 异常 tg={tg}: {e}")
        return {"ok": False, "msg": f"改密异常: {str(e)[:80]}"}


def rename_emby_user(tg: int, new_name: str) -> dict:
    """改 Emby 账号用户名（安全码验证后）。Emby: GET 用户 → 改 Name → POST 回写。

    同步更新 boss 账本 name 与 kfc_bind（榜单）。返回 {"ok","name","msg"}。
    """
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "该用户还没有 Emby 账号"}
    new_name = (new_name or "").strip()[:32]
    if not (2 <= len(new_name) <= 32) or any(c in "<>@#&/\\`'\"" for c in new_name):
        return {"ok": False, "msg": "用户名需 2~32 字，禁特殊字符（< > @ # & / \\）"}
    try:
        r = _emby_api(f"/emby/Users/{eid}")
        if r.status_code != 200:
            return {"ok": False, "msg": f"读取用户失败 HTTP {r.status_code}"}
        dto = r.json() or {}
        dto["Name"] = new_name
        r2 = _emby_api(f"/emby/Users/{eid}", "POST", json=dto)
        if r2.status_code not in (200, 204):
            return {"ok": False, "msg": f"Emby 改名失败 HTTP {r2.status_code}"}
        with db._engine.begin() as c:
            c.execute(text(f"UPDATE `{SHARED}`.emby SET name=:n WHERE tg=:tg"),
                      {"n": new_name, "tg": int(tg)})
            try:
                c.execute(text("UPDATE kfc_bind SET emby_name=:n WHERE tg=:tg"),
                          {"n": new_name[:64], "tg": int(tg)})
            except Exception:
                pass
        return {"ok": True, "name": new_name, "msg": "用户名已更新"}
    except Exception as e:
        logger.warning(f"rename_emby_user 异常 tg={tg}: {e}")
        return {"ok": False, "msg": f"改名异常: {str(e)[:80]}"}


# ───────────────────── 媒体库显隐 / 收藏 / 设备（对标 Boss） ─────────────────────
# 用户可在面板里隐藏/显示的「杂项/成人」库（与建号 _BLOCK_FOLDERS 一致）
HIDE_LIBS = ["播放列表", "R级电影"]


def _media_folders() -> dict:
    """Emby 全部实体媒体库 {name: id}；失败返回 {}"""
    try:
        r = _emby_api("/emby/Library/MediaFolders")
        if r.status_code != 200:
            return {}
        return {it.get("Name"): it.get("Id")
                for it in ((r.json() or {}).get("Items") or []) if it.get("Name")}
    except Exception:
        return {}


def _user_policy(eid: str):
    try:
        r = _emby_api(f"/emby/Users/{eid}")
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("Policy") or {}
    except Exception:
        return None


def _user_views(eid: str) -> dict:
    """用户实际可见的库 {name: id}。★ id 命名空间与 EnabledFolders 一致
      （Boss 建的号用 GUID、我们写的用数字 id——只有 Views 能拿到同一套）。失败 {}。"""
    try:
        r = _emby_api(f"/emby/Users/{eid}/Views?UserId={eid}", timeout=8)
        if r.status_code != 200:
            return {}
        return {it.get("Name"): it.get("Id")
                for it in ((r.json() or {}).get("Items") or []) if it.get("Name")}
    except Exception:
        return {}


def libs_hidden(tg: int) -> dict:
    """读当前用户隐藏了哪些媒体库。
    ★ 2026-11-05 修正：改用 Views 名字差集判定（全库名 - 可见库名）。
      旧实现拿 EnabledFolders(GUID) 比 MediaFolders(数字id)，命名空间不同、
      会把全部库误判成隐藏；Views 是用户实见口径，谁写的策略都准。"""
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "还没有 Emby 账号"}
    views = _user_views(eid)
    if not views:
        pol = _user_policy(eid)
        if pol is None:
            return {"ok": False, "msg": "读取用户策略失败（Emby 端无此号？）"}
        return {"ok": True, "hidden": [], "enable_all": bool(pol.get("EnableAllFolders", True))}
    folders = _media_folders()
    hidden = [n for n in folders if n not in views]
    return {"ok": True, "hidden": hidden, "enable_all": False}


def set_libs_hidden(tg: int, hide: bool) -> dict:
    """隐藏 / 显示 HIDE_LIBS 库。
    ★ 2026-11-05 重写：以 Views 为基准（id 命名空间与策略一致），彻底避开
      GUID/数字id 混用问题；保留原策略其它字段整份 POST（对齐 Boss 做法）。
      hide → EnabledFolders = 当前可见库去掉目标；show → EnableAllFolders=True 全开
      （面板按钮语义就是「全部显示」）。"""
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "还没有 Emby 账号"}
    pol = _user_policy(eid)
    if pol is None:
        return {"ok": False, "msg": "读取用户策略失败（Emby 端无此号？）"}
    views = _user_views(eid)          # {name: id} 同命名空间
    if not views:
        return {"ok": False, "msg": "读取可见媒体库失败"}
    policy = dict(pol)
    if hide:
        tgt = [views[n] for n in HIDE_LIBS if n in views]
        if not tgt:
            return {"ok": True, "msg": "已隐藏（目标库本就不可见）"}
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = [i for i in views.values() if i not in tgt]
        policy["BlockedMediaFolders"] = list({*(policy.get("BlockedMediaFolders") or []), *tgt})
    else:
        policy["EnableAllFolders"] = True
        policy["EnabledFolders"] = []
        policy["BlockedMediaFolders"] = []
    r = _emby_api(f"/emby/Users/{eid}/Policy", "POST", json=policy)
    if r.status_code not in (200, 204):
        return {"ok": False, "msg": f"策略下发失败 HTTP {r.status_code}"}
    return {"ok": True, "msg": "已隐藏" if hide else "已全部显示"}


def get_favorites(tg: int, start: int = 0, limit: int = 10) -> dict:
    """用户收藏（电影/剧集/演员），对齐 Boss my_favorites。"""
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "还没有 Emby 账号"}
    r = _emby_api(f"/emby/Users/{eid}/Items?Filters=IsFavorite&Recursive=true"
                  f"&IncludeItemTypes=Movie,Series,Episode,Person"
                  f"&StartIndex={int(start)}&Limit={int(limit)}")
    if r.status_code != 200:
        return {"ok": False, "msg": f"读取收藏失败 HTTP {r.status_code}"}
    return {"ok": True, "data": r.json() or {}}


def get_devices(tg: int) -> dict:
    """用户播放过的设备 & IP（PlaybackActivity 自定义查询，对齐 Boss my_devices；
    需要 Emby 的 user_usage_stats 统计插件）。"""
    acc = emby_account_of(tg) or {}
    eid = acc.get("embyid")
    if not eid:
        return {"ok": False, "msg": "还没有 Emby 账号"}
    if not str(eid).replace("-", "").replace("_", "").isalnum():
        return {"ok": False, "msg": "用户ID格式异常"}
    sql = ("SELECT DeviceName,ClientName,RemoteAddress FROM PlaybackActivity "
           f"WHERE UserId = '{eid}'")
    r = _emby_api("/emby/user_usage_stats/submit_custom_query", "POST",
                  json={"CustomQueryString": sql, "ReplaceUserId": True})
    if r.status_code != 200:
        return {"ok": False, "msg": f"查询失败 HTTP {r.status_code}"}
    devs, ips = [], []
    for row in ((r.json() or {}).get("results") or []):
        try:
            dev, cli, ip = row[0], row[1], row[2]
        except Exception:
            continue
        key = f"{dev}|{cli}"
        if key not in devs:
            devs.append(key)
        if ip and ip not in ips:
            ips.append(ip)
    return {"ok": True, "devices": devs, "ips": ips}


# ───────────── 多端到期对账（Boss/Game/RT 任意一端续期自动对齐） ─────────────
def _parse_dt(v):
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, str):
        try:
            return dt.datetime.strptime(v[:19].replace("T", " "),
                                        "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


def sync_ex_to_rt() -> dict:
    """到期对账：以 boss 账本（embyboss.emby.ex）为准，把落后的 RT 端续齐。

    覆盖场景：Boss /renew、Game 续期码等只写了 boss/Emby 未同步 RT 的情况
    （用户要求：RT 开的号在 Boss/Game 上续费后，RT 面板也要同步）。
    RT 领先 boss 的方向由 media-hub rt_pull_sync 回填，这里不处理。
    """
    if not rtapi.enabled():
        return {"ok": False, "msg": "RT 未启用"}
    checked = fixed = failed = 0
    try:
        with db._engine.connect() as c:
            rows = c.execute(text(
                f"SELECT tg, ex FROM `{SHARED}`.emby "
                "WHERE tg IS NOT NULL AND tg != 0 AND embyid IS NOT NULL "
                "AND embyid != '' AND ex IS NOT NULL AND lv != 'd'")).mappings().all()
    except Exception as e:
        return {"ok": False, "msg": str(e)[:80]}
    for r in rows:
        tg = int(r["tg"] or 0)
        boss_ex = _parse_dt(r["ex"])
        if not tg or not boss_ex:
            continue
        try:
            u = rtapi.find_user(tg)
            if not u:
                continue          # RT 没这个号（如 Emby 直连开的），跳过
            checked += 1
            rt_ex = _parse_dt(str(u.get("expiry_date") or ""))
            if not rt_ex:
                continue
            gap = (boss_ex - rt_ex).total_seconds()
            if gap > 3600:        # boss 领先 >1h → 给 RT 补齐
                days = max(1, round(gap / 86400))
                rr = rtapi.renew_user(tg, days)
                if rr.get("ok"):
                    fixed += 1
                    logger.info(f"到期对账：tg={tg} RT 落后 {days} 天已补齐")
                else:
                    failed += 1
                    logger.warning(f"到期对账 tg={tg} 补齐失败: {rr.get('msg')}")
        except Exception as e:
            failed += 1
            logger.debug(f"到期对账 tg={tg} 异常: {e}")
    return {"ok": True, "checked": checked, "fixed": fixed, "failed": failed}


async def sync_ex_to_rt_job():
    """调度入口（AsyncIOScheduler 需协程；同步 requests 放线程池）。"""
    await asyncio.to_thread(sync_ex_to_rt)


# ───────────── 服务器线路 / 媒体库 / 统计 / 换绑TG（面板扩充后端） ─────────────
def server_lines() -> list[dict]:
    """播放线路列表（config.emby.lines；空则回落 public_url）。"""
    ls = [l for l in (S.EMBY.get("lines") or []) if l.get("url")]
    if ls:
        return ls
    pub = S.EMBY.get("public_url")
    return [{"name": "默认线路", "url": pub, "enabled": True}] if pub else []


def libs_list() -> dict:
    """全服媒体库清单（名称列表）。"""
    try:
        r = _emby_api("/emby/Library/MediaFolders")
        if r.status_code != 200:
            return {"ok": False, "msg": f"HTTP {r.status_code}"}
        items = (r.json() or {}).get("Items") or []
        return {"ok": True, "libs": [i.get("Name") for i in items if i.get("Name")]}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:80]}


def media_stats() -> dict:
    """服务器统计：影/剧/集数量、媒体库数、账号数、正在播放数、版本。

    ★ 2026-11-05 提速：5 个 Emby 请求改**线程池并发**（总耗时=最慢一个，
      原来串行要等 5 个之和）；Users/Sessions 大列表加 Fields 瘦身；
      timeout 8s。NAS 负载高时也不再卡半分钟。"""
    from concurrent.futures import ThreadPoolExecutor
    out = {"ok": False}
    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_cnt = ex.submit(lambda: _emby_api("/emby/Items/Counts", timeout=8))
            f_lib = ex.submit(lambda: _emby_api(
                "/emby/Library/MediaFolders?Fields=Name", timeout=8))
            f_usr = ex.submit(lambda: _emby_api(
                "/emby/Users?Fields=Name", timeout=8))
            f_ses = ex.submit(lambda: _emby_api(
                "/emby/Sessions?Fields=NowPlayingItem", timeout=8))
            f_pub = ex.submit(lambda: _emby_api("/emby/System/Info/Public", timeout=8))
            r, lf, us, ss, pub = (f_cnt.result(), f_lib.result(), f_usr.result(),
                                  f_ses.result(), f_pub.result())
        j = r.json() or {} if r.status_code == 200 else {}
        out["movies"] = int(j.get("MovieCount") or 0)
        out["series"] = int(j.get("SeriesCount") or 0)
        out["episodes"] = int(j.get("EpisodeCount") or 0)
        out["libs"] = len(((lf.json() or {}).get("Items") or [])) if lf.status_code == 200 else 0
        out["users"] = len(us.json() or []) if us.status_code == 200 else 0
        sess = ss.json() or [] if ss.status_code == 200 else []
        out["playing"] = sum(1 for x in sess if x.get("NowPlayingItem"))
        pj = pub.json() or {} if pub.status_code == 200 else {}
        out["server"] = pj.get("ServerName") or "KFC"
        out["version"] = pj.get("Version") or "?"
        out["ok"] = True
    except Exception as e:
        out["msg"] = str(e)[:80]
    return out


def rebind_tg_by_name(new_tg: int, name: str, sec: str) -> dict:
    """换绑/认领 TG（Boss changetg 同语义）：用「Emby用户名+安全码」把账号
    迁到当前 TG 名下。安全码不符/名下已有号/查无号都拒绝。

    ★ 2026-11-04：账号字段（embyid/name/pwd/pwd2/lv/cr/ex）整体搬到新 TG 行，
      新行自身的 iv（V积分钱包）保留不动、us 清零；附属表同步迁移。
    """
    new_tg = int(new_tg)
    try:
        with db._engine.begin() as c:
            row = c.execute(text(
                f"SELECT tg, embyid, name, pwd, pwd2, lv, cr, ex FROM `{SHARED}`.emby "
                "WHERE name=:n AND embyid IS NOT NULL AND embyid != '' LIMIT 1"),
                {"n": name[:64]}).first()
            if not row:
                return {"ok": False, "msg": "查无此 Emby 账号（注意区分大小写）"}
            if (row[4] or "") != (sec or "").strip():
                return {"ok": False, "msg": "安全码不正确"}
            old_tg = int(row[0] or 0)
            if old_tg == new_tg:
                return {"ok": False, "msg": "这个账号已经绑在你名下了"}
            cur = c.execute(text(
                f"SELECT embyid, us FROM `{SHARED}`.emby WHERE tg=:t"),
                {"t": new_tg}).first()
            if cur and cur[0]:
                return {"ok": False, "msg": "你名下已有 Emby 账号，不能重复绑定"}
            if cur:                        # 有行但无账号：写入账号字段，保 iv、清 us
                c.execute(text(
                    f"UPDATE `{SHARED}`.emby SET embyid=:eid, name=:n, pwd=:p, "
                    "pwd2=:p2, lv=:lv, cr=:cr, ex=:ex, us=0 WHERE tg=:t"),
                    {"eid": row[1], "n": row[2], "p": row[3], "p2": row[4],
                     "lv": row[5] or "b", "cr": row[6], "ex": row[7], "t": new_tg})
            else:                          # 无行：整行重建（iv 从 0 起）
                c.execute(text(
                    f"INSERT INTO `{SHARED}`.emby "
                    "(tg, embyid, name, pwd, pwd2, lv, cr, ex, us, iv) VALUES "
                    "(:t, :eid, :n, :p, :p2, :lv, :cr, :ex, 0, 0)"),
                    {"t": new_tg, "eid": row[1], "n": row[2], "p": row[3],
                     "p2": row[4], "lv": row[5] or "b", "cr": row[6], "ex": row[7]})
            if old_tg:                     # 清掉源 TG 行（tg=0 的公共行不动）
                c.execute(text(f"DELETE FROM `{SHARED}`.emby WHERE tg=:t"),
                          {"t": old_tg})
            for tbl in ("kfc_bind", "kfc_emby_bind"):
                try:
                    c.execute(text(f"DELETE FROM {tbl} WHERE tg=:t"), {"t": new_tg})
                    if old_tg:
                        c.execute(text(f"UPDATE {tbl} SET tg=:nw WHERE tg=:old"),
                                  {"nw": new_tg, "old": old_tg})
                except Exception:
                    pass
        return {"ok": True, "msg": "账号已绑定到你的 TG"}
    except Exception as e:
        logger.warning(f"rebind_tg_by_name 异常 name={name}: {e}")
        return {"ok": False, "msg": str(e)[:80]}
