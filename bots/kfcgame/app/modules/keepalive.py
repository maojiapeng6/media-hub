# -*- coding: utf-8 -*-
"""PT 站点保号系统 —— bot 端引擎 + 命令

设计（2026-10-12 用户需求）：
  1. 覆盖 6 站：ZMTP·织梦 / NicePT / PTSKIT·拾刻 / cyanbug·大青虫 / HDArea / M-Team·馒头
  2. 每站独立定制任务，严格按官方规则（见 keepalive_sites.py）
  3. 无斜杠命令：保号 / 保号规则（+ /ka 斜杠别名），群聊私聊均响应
     ★ 本模块属「PT」范畴，仅群主/被单独授权的管理员可用（perm: ka=ADMIN）
     ★ PT 保号命令**不叫「发电」**；「发电」是 Emby 范畴的公开功能（modules/power.py）
  4. 每站可独立启停；规则 / 进度 / 状态 Bot 与 Web 实时一致
  5. 绑定信息（cookie/passkey/rss）由 Windows 侧抓取脚本写入 secrets.json
  6. 状态文件 state.json 与 Web（media-hub:5678）共享：
     bot 容器 /app/keepalive/state.json ←→ web 容器 /dockroot/kfcgame/keepalive/state.json
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as H
import hmac
import json
import os
import re
import shutil
import threading
import time

import requests
from loguru import logger
from pyrogram import ContinuePropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import settings as S
from .. import ui
from . import keepalive_sites as KS
from . import perm
from . import svc

PM = ParseMode.HTML
CMD = dict(prefixes=S.PREFIX)


# ════════════════════════════════════════════════════════════════
#  链接排版（★ 用户 2026-10-13 要求）
# ────────────────────────────────────────────────────────────────
#  坑：Telegram 会把「URL 后面紧跟的中文」一并吞进 URL —— 写成
#      `https://zmpt.cc（私聊检查账号状态）`
#   整段都会被识别成一个**无效链接**，用户点了没反应（看着像没有链接）。
#  规矩（全模块统一，新增文案一律照此办理）：
#    ① 链接**单独成行**；
#    ② 链接一律用 <a href> 标准格式（不用裸 URL，避免被前后字符污染）；
#    ③ 括号说明 / 账号状态之类纯文本**另起一行**，绝不能贴在同一行。
# ════════════════════════════════════════════════════════════════
def _url_line(url: str, label: str = "", icon: str = "🔗") -> str:
    """一条**独占一行**的可点链接（HTML <a> 标准格式）"""
    u = str(url or "").strip()
    if not u:
        return ""
    txt = str(label or "").strip() or u
    return f'{icon} <a href="{ui.esc(u)}">{ui.esc(txt)}</a>'


def _url_block(url: str, note: str = "", label: str = "") -> list[str]:
    """链接行 + 说明行（说明绝不挂在链接后面）—— 返回 list[str] 直接拼进 lines"""
    out = [_url_line(url, label)]
    if note:
        out.append(f"　　{ui.esc(note)}")
    return [x for x in out if x]

# ════════════════════════════════════════════════════════════════
#  状态 / 机密文件（state.json 与 Web 共享；secrets.json 仅 bot 用）
# ════════════════════════════════════════════════════════════════
KA_DIR = os.environ.get("KA_DIR", "/app/keepalive")
STATE_PATH = os.path.join(KA_DIR, "state.json")
SECRET_PATH = os.path.join(KA_DIR, "secrets.json")
_lock = threading.Lock()


def _blank_site() -> dict:
    return {
        "enabled": True, "bound": False,
        "last_login": 0.0, "last_probe": 0.0, "last_error": "",
        "traffic_ts": 0.0, "seed_ids": [], "notified": {},
        "stats": {"upload": 0.0, "download": 0.0, "ratio": 0.0, "seeding": 0,
                  "bonus": 0.0, "hr": 0, "level": "", "joined": "", "uid": 0},
        "tasks": {},
    }


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {"sites": {}, "updated": 0}
    for s in KS.SITES:
        st["sites"].setdefault(s["id"], _blank_site())
    return st


def save_state(st: dict) -> None:
    st["updated"] = time.time()
    st["_cfg"] = KS.SITES          # 完整站点配置随状态落盘，Web 端直接读，无需各自维护两份
    try:
        os.makedirs(KA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)          # 原子替换，Web 读到的一定是完整 JSON
    except Exception as e:
        logger.warning(f"保号 state 写入失败: {e}")


def load_secrets() -> dict:
    try:
        with open(SECRET_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_secrets(d: dict) -> bool:
    """secrets.json 原子写（M-Team JWT 轮换回写用；rw 挂载，免部署）"""
    try:
        os.makedirs(KA_DIR, exist_ok=True)
        tmp = SECRET_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, SECRET_PATH)
        return True
    except Exception as e:
        logger.warning(f"保号 secrets 写入失败: {e}")
        return False


def _proxies() -> dict | None:
    """保号访问固定走直连（不使用 S.PROXY）。

    实测（NAS 容器 → 各 PT 站，带浏览器 UA）：
      · 走代理时部分站点出口被 Cloudflare 挑战：zmpt 403、hdarea 403；
      · 直连：hdarea 200、nicept/cyanbug/mteam 均 200；仅 zmpt 仍 403
        （CF JS 挑战，cf_clearance 与 IP 绑定，NAS 换 IP 即失效，无解）。
    ⇒ 直连对全部站点 ≥ 代理，故保号固定直连。
      Telegram 客户端仍用 S.PROXY（app/__init__.py），不受本函数影响。
    """
    return None


# ════════════════════════════════════════════════════════════════
#  HTTP 访问（保活登录 + 状态探测）
# ════════════════════════════════════════════════════════════════
def _site_cookie(site: dict) -> dict | None:
    """secrets.json 里存的该站 cookie 名称→值"""
    sec = load_secrets().get("sites", {}).get(site["id"]) or {}
    ck = sec.get("cookies") or {}
    return ck or None


def _http_get(site: dict, path: str) -> tuple[int, str]:
    ck = _site_cookie(site)
    if not ck:
        return 0, ""
    # ★ 2026-10-17：保活 GET 也放宽到 (10s/50s) —— 高负载下 25s 会把
    #   「登录成功但统计页慢」误判成失败。
    for _attempt in (1, 2):
        try:
            r = requests.get(site["home"].rstrip("/") + path, cookies=ck,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"},
                             proxies=_proxies(), timeout=(10, 50), allow_redirects=True)
            return r.status_code, r.text
        except Exception as e:
            logger.debug(f"KA GET {site['id']} {path}(第{_attempt}次): {e}")
            if _attempt == 2:
                return 0, ""
            time.sleep(3)
    return 0, ""


_UNIT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4,
         "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _size_to_bytes(s: str) -> float:
    m = re.match(r"([\d.,]+)\s*([KMGT]?I?B)", s, re.I)
    if not m:
        return float(m and 0 or 0)
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    unit = m.group(2).upper()
    return num * _UNIT.get(unit, 1)


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _find_stat(text: str, labels: list[str], cast=float) -> float:
    """在纯文本里按『标签: 数值』抓数，兼容大小写与全角冒号。
    ★ 2026-10-14：标签与冒号之间容忍 `[ 按钮 ]` 噪声（NexusPHP topbar 常见
      「魔力值 [ 使用 ]: 81.4」），否则这类站魔力恒 0。"""
    gap = r"\s*(?:\[[^\]]{0,8}\]\s*)*[:：]?\s*"
    for lb in labels:
        m = re.search(lb + gap + r"([0-9][0-9,.]*\s*(?:[KMGT]?I?B|%|×|x|次|条|小时|h)?)",
                      text, re.I)
        if m:
            raw = m.group(1).strip()
            if cast is float:
                if re.search(r"[KMGT]", raw, re.I) and not re.search(r"(小时|h|次|条|%)", raw, re.I):
                    return _size_to_bytes(raw)
                try:
                    return float(raw.replace(",", "").rstrip("%×x次条"))
                except ValueError:
                    continue
            else:
                try:
                    return float(re.sub(r"[^\d.]", "", raw) or 0)
                except ValueError:
                    continue
    return cast(0)


def parse_stats(site: dict, html: str) -> dict:
    """NexusPHP / M-Team 首页文本 → 统计字典（宽松解析，抓不到就 0）"""
    txt = _strip_tags(html)
    txt = re.sub(r"[ \t\r\xa0]+", " ", txt)
    st = {"upload": 0.0, "download": 0.0, "ratio": 0.0, "seeding": 0,
          "bonus": 0.0, "hr": 0, "level": "", "joined": "", "uid": 0}
    st["upload"] = _find_stat(txt, ["上传量", "上传", "上載", "上傳"], float)
    st["download"] = _find_stat(txt, ["下载量", "下载", "下載"], float)
    st["ratio"] = _find_stat(txt, ["分享率", "比率", "ratio"], float)
    # ★ 2026-10-14：这些 NexusPHP 首页没有「做种数」标签，topbar 是
    #   「当前活动: <做种> <下载>」——第一个数即活跃做种数（实测 hdarea/zmpt）。
    #   「当前活动」必须排最前：英文 seeding / 做种中 会先命中全站统计大数。
    _sd = int(_find_stat(txt, ["当前活动", "做种数", "做种中", "做種"], int))
    # 个人做种数不可能上千，抓到全站级大数（cyanbug 顶栏顺序差异命中总数）→ 判无效归 0
    st["seeding"] = _sd if _sd <= 5000 else 0
    st["bonus"] = _find_stat(txt, ["魔力值", "魔力", "积分", "bonus", "bonus point"], float)
    st["hr"] = int(_find_stat(txt, ["H&R", "HR", "HnR"], int))
    m = re.search(r"(?:等级|Level|用戶等級|用户等级)\s*[:：]?\s*([^\s,，。|]{1,12})", txt, re.I)
    if m:
        # ★ 防误抓：页面正文/news 里常出现「等级」字样后跟普通句或 JS 代码
        #   （曾抓出 smenu.setup(）。合法等级只含中英文数字·空格，否则弃用。
        v = m.group(1)
        st["level"] = v if re.fullmatch(r"[0-9A-Za-z一-鿿·\s]{1,12}", v) and \
            not re.search(r"[(){}\[\];.=<>/#_-]", v) else ""
    m = re.search(r"(?:注册时间|加入时间|註冊時間|注册日期|joined)\s*[:：]?\s*"
                  r"(\d{4}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2})", txt, re.I)
    if m:
        st["joined"] = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
    # ★ 2026-10-17 官方规则接入 + 语义纠正（用户指出织梦"根本没有邀请"）：
    #   侧栏「邀请 [发送]: X(Y)」经与用户实机核对，语义 = **剩余可用(总配额)**，
    #   不是"已发送(可用)"。织梦 0(2) = 剩余 0 个可用、总配额 2 个（有配额但当前
    #   没有可发名额）；大青虫 1(2) = 剩 1 可用、总 2。
    #   邀请资格 = 等级达标/过新手考核 → 站点发放名额；invite.php 无权限 =
    #   当前等级不够。官方规则页没有"积分兑换邀请"条款，不要按魔力估算。
    st["invite_avail"] = 0
    st["invite_quota"] = 0
    m = re.search(r"(?:邀請|邀请)\s*(?:\[\s*(?:發)?送?\s*\])?\s*[:：]?\s*"
                  r"(\d+)\s*[（(]\s*(\d+)\s*[）)]", txt)
    if m:
        st["invite_avail"] = int(m.group(1))
        st["invite_quota"] = int(m.group(2))
    else:
        # hdarea 等站只有单数字「邀请: 0」→ 视为剩余可用
        m2 = re.search(r"(?:邀請|邀请)\s*(?:\[\s*(?:發)?送?\s*\])?\s*[:：]\s*(\d+)\s*$",
                       txt, re.M)
        if m2:
            st["invite_avail"] = int(m2.group(1))
            st["invite_quota"] = int(m2.group(1))
    return st


def is_logged_in(site: dict, html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    if "logout.php" in low or "usercp.php" in low or "/logout" in low:
        return True
    if "login.php" in low and "form" in low and "usercp" not in low:
        return False
    return bool(re.search(r"(做种|分享率|魔力|上传量|用戶|用户中心|我的)", _strip_tags(html)))


async def do_login(site: dict, st: dict) -> bool:
    """访问首页 + 控制面板 = 保活登录（M-Team 需真实浏览器，另行走 web 模式）"""
    code, html = await asyncio.to_thread(_http_get, site, "/")
    if not code:
        await asyncio.to_thread(_http_get, site, "/index.php")
        code, html = await asyncio.to_thread(_http_get, site, "/")
    if code and is_logged_in(site, html):
        st["last_login"] = time.time()
        st["last_error"] = ""
        st["bound"] = True
        log = f"✅ 已访问 {site['name']}（HTTP 保活）"
        logger.info(log)
        return True
    if code == 0:
        st["last_error"] = "访问失败（网络/代理？）"
    elif not is_logged_in(site, html):
        st["last_error"] = "未登录（cookie 失效，需重新抓取绑定信息）"
    return False


async def probe(site: dict, st: dict, login_ok: bool = False) -> None:
    """抓 usercp 统计。★ 2026-10-15 修「误报未登录」：
    访问（do_login）刚成功就说明账号登录正常，此时探测失败只是
    stats 页面抓不到（CF 拦截 / 页面改版），不能再写「未登录或访问异常」——
    那会让用户收到「站点未登录」的假告警。改为清 error + 软提示 note。"""
    code, html = await asyncio.to_thread(_http_get, site, "/usercp.php")
    if not code:
        code, html = await asyncio.to_thread(_http_get, site, "/")
    if code and is_logged_in(site, html):
        stats = await asyncio.to_thread(parse_stats, site, html)
        old = st["stats"]
        if stats["upload"] > old.get("upload", 0) or stats["download"] > old.get("download", 0):
            st["traffic_ts"] = time.time()
        st["stats"] = stats
        st["last_probe"] = time.time()
        st["last_login"] = max(st["last_login"], time.time())  # 探测同时也算访问
        st["last_error"] = ""
        st["note"] = ""
        st["bound"] = True
    elif login_ok:
        # 登录成功但 stats 抓不到：账号没危险，只是统计暂缺 → 不清 last_login
        st["last_error"] = ""
        st["note"] = "统计抓取暂缺（页面改版/CF），账号登录正常"
    else:
        st["last_error"] = "探测失败：未登录或访问异常"


# ════════════════════════════════════════════════════════════════
#  M-Team 新版（SPA + API）保号 —— 2026-10-14
# ────────────────────────────────────────────────────────────────
#  M-Team 已从 NexusPHP 迁到 SPA（正常入口 kp.m-team.cc / 江浙沪
#  zp.m-team.io，门户 www.m-team.io；旧 m-team.cc 只是 SPA 壳）。
#  旧的 cookie 流程（_http_get + is_logged_in）对新站不适用：
#  登录态是浏览器 localStorage['auth'] 的 JWT（请求头 Authorization），
#  由 media-hub ccpull 从 CookieCloud 同步进 secrets.json 的
#  sites.mteam.jwt；apikey（x-api-key）走 /member/profile 只读统计。
#  保号 = POST /api/member/updateLastBrowse（SPA 每次页面加载都调它），
#  签名算法与 MP autosignin 插件（app/plugins/autosignin/sites/mteam.py，
#  2026-10 维护版）逐字段一致；响应头 authorization 若是新 JWT 则回写。
# ════════════════════════════════════════════════════════════════
_MT_API = "https://api.m-team.cc/api"
_MT_SIGN_SECRET = "HLkPcWmycL57mfJt"
_MT_WEBVERSION = "2000"


def _mt_sign(ts_ms: int) -> str:
    """前端同款签名：base64(HMAC-SHA1(密钥, 'POST&/api/member/updateLastBrowse&毫秒时间戳'))"""
    msg = f"POST&/api/member/updateLastBrowse&{ts_ms}".encode()
    return base64.b64encode(hmac.new(_MT_SIGN_SECRET.encode(), msg, hashlib.sha1).digest()).decode()


def _mt_jwt_exp(jwt: str) -> float:
    """解 JWT payload 的 exp（epoch 秒），解不开返回 0"""
    try:
        import base64 as _b64, json as _j
        pl = jwt.split(".")[1]
        pl += "=" * (-len(pl) % 4)
        return float((_j.loads(_b64.urlsafe_b64decode(pl)) or {}).get("exp") or 0)
    except Exception:
        return 0.0


def _mt_headers(jwt: str, ts_ms: int) -> dict:
    """复刻 SPA 请求头。did/visitorid：2026-10-14 起优先用浏览器真实指纹
    （CookieCloud localStorage 里的 did/visitorId，media-hub 同步进 secrets），
    缺失才退回 JWT 派生值。注意：真实 did 也救不了已撤销的会话（实测 401），
    所以 JWT 失效只能浏览器重登——这里只是让头更像浏览器。"""
    sec_mt = (load_secrets().get("sites", {}) or {}).get("mteam") or {}
    did = str(sec_mt.get("did") or "").strip()
    vid = str(sec_mt.get("visitorid") or "").strip()
    if not did or not vid:
        fp = hashlib.sha256((jwt or "").encode()).hexdigest()
        did, vid = did or fp[:32], vid or fp[32:]
    return {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Authorization": jwt,
        "Origin": "https://kp.m-team.cc",
        "Referer": "https://kp.m-team.cc/index",
        "Ts": str(ts_ms // 1000),
        "webversion": _MT_WEBVERSION,
        "Did": did,
        "visitorid": vid,
        "User-Agent": _KA_UA,
    }


def _mt_secret_update(**kw) -> None:
    """读-改-写 secrets.json 的 sites.mteam（JWT 轮换回写）"""
    try:
        sec = load_secrets()
        st = sec.setdefault("sites", {}).setdefault("mteam", {})
        st.update(kw)
        save_secrets(sec)
    except Exception as e:
        logger.debug(f"mteam secrets 回写失败: {e}")


def _mt_parse_browse(j: dict, jwt: str = "") -> tuple[bool, str]:
    """updateLastBrowse 业务判定（与 MP autosignin 插件同口径：
    站点 2026-10 起成功时也返回 code=1/FAIL，仅此组合可确认）

    401 细分（2026-10-14 替罪馒头误报复盘）：
      · JWT 时间真过期（exp<now）      → 「JWT 已到期」——正常到期，重登即可
      · JWT 时间没到但服务端拒（401） → 「会话被服务端撤销/顶号」——多半是
        浏览器又登录了新会话把旧的踢了，或站方轮换；重登刷新 JWT 同样解决
"""
    code = str(j.get("code"))
    msg = str(j.get("message") or "").strip()
    if j.get("success") is True or code == "0" or msg.upper() == "SUCCESS":
        return True, "保号成功（已更新访问时间）"
    if code == "1" and msg.upper() == "FAIL":
        return True, "保号成功（站点返回 FAIL，实为成功）"
    if code == "401" or "full authentication" in msg.lower():
        exp = _mt_jwt_exp(jwt) if jwt else 0
        if exp and exp < time.time():
            return False, ("JWT 已到期（%s），请在浏览器重新登录 kp.m-team.cc "
                           "后用 CookieCloud 同步" % time.strftime('%m-%d', time.localtime(exp)))
        return False, ("JWT 会话被站点撤销（未到期但拒认，多为浏览器新登录顶掉旧会话）。"
                       "处理：浏览器登录一次 kp.m-team.cc → CookieCloud 点上传 → "
                       "Web「保号」页会自动同步（≤1 小时，也可手动立即同步）")
    return False, f"接口未确认（可能需要重新同步 JWT）：{msg or code}"


def _mt_stats(d: dict) -> dict:
    """profile.data → 统计字典（兼容其他站 stats 结构，额外带 seedtime 秒）"""
    mc = d.get("memberCount") or {}
    ms = d.get("memberStatus") or {}

    def _f(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    st = {"upload": _f(mc.get("uploaded")), "download": _f(mc.get("downloaded")),
          "ratio": _f(mc.get("shareRate")), "seeding": 0,
          "bonus": _f(mc.get("bonus")), "hr": 0,
          "level": "VIP" if ms.get("vip") else ("捐赠者" if ms.get("donor") else ""),
          "joined": str(d.get("createdDate") or "")[:10],
          "uid": int(_f(d.get("id"))), "seedtime": _f(d.get("seedtime"))}
    # ★ 邀请位：profile.invites = 当前可用邀请名额（官方 API 字段）
    try:
        st["invite_avail"] = int(_f(d.get("invites")))
        st["invite_quota"] = int(_f(d.get("limitInvites")))
    except Exception:
        st["invite_avail"] = 0
        st["invite_quota"] = 0
    return st


async def mteam_tick(site: dict, ss: dict) -> None:
    """馒头新版保号：updateLastBrowse（JWT 保号）+ profile（apikey 统计）。

    ★ 2026-10-15 顺序重排（IP 绑定误报复盘）：
      实测 api.m-team.cc 的 browse 端点对"非浏览器来源"会 401
      （JWT 没过期也拒——CF/设备校验），但 apikey 的 profile 一直 200。
      账号是否真危险以 **apikey 存活** 为准：profile 通 → /browse 失败只是
      黄字提示（信标没刷成，40 天删除线还远）；profile 也挂 → 才是真红告警。
    """
    sec = load_secrets().get("sites", {}).get("mteam") or {}
    jwt = (sec.get("jwt") or "").strip()
    apikey = (sec.get("apikey") or "").strip()
    if not jwt:
        ss["last_error"] = ("未配置 JWT：浏览器登录 kp.m-team.cc 后，"
                            "在 CookieCloud 扩展里开 localStorage 同步")
        return
    # ① 存活探测（apikey profile —— 权威信号，先跑）
    alive = False
    try:
        r2 = await asyncio.to_thread(
            requests.post, _MT_API + "/member/profile",
            headers={"x-api-key": apikey, "Accept": "application/json",
                     "User-Agent": _KA_UA},
            proxies=_proxies(), timeout=30)
        d = (r2.json() or {}).get("data") or {}
        if d:
            alive = True
            stats = _mt_stats(d)
            old = ss["stats"]
            if (stats["upload"] > old.get("upload", 0)
                    or stats["download"] > old.get("download", 0)):
                ss["traffic_ts"] = time.time()
            ss["stats"] = stats
            ss["last_probe"] = time.time()
            ss["bound"] = True
    except Exception as e:
        logger.debug(f"mteam profile: {e}")
    # ② 保号信标（JWT browse）：成功就刷新登录态；失败按存活情况分级
    ts_ms = int(time.time() * 1000)
    browse_ok = False
    browse_msg = ""
    try:
        r = await asyncio.to_thread(
            requests.post, _MT_API + "/member/updateLastBrowse",
            headers=_mt_headers(jwt, ts_ms),
            data={"_timestamp": str(ts_ms), "_sgin": _mt_sign(ts_ms)},
            proxies=_proxies(), timeout=30)
        try:
            j = r.json()
        except Exception:
            j = {}
        browse_ok, browse_msg = _mt_parse_browse(j, jwt)
        # JWT 轮换：响应头 authorization 是新登录态就回写 secrets
        new_jwt = (r.headers.get("authorization") or "").strip()
        if new_jwt and new_jwt != jwt:
            _mt_secret_update(jwt=new_jwt, jwt_ts=time.time())
            logger.info("馒头 JWT 已轮换并回写 secrets.json")
    except Exception as e:
        browse_msg = f"保号请求异常: {e}"
    if browse_ok:
        ss["last_login"] = time.time()
        ss["last_error"] = ""
        ss["bound"] = True
    elif alive:
        # 账号活着（apikey 通）→ browse 失败降级为 info，不触发红告警刷屏
        ss["last_error"] = ""
        ss["note"] = "browse信标暂被站点拒绝（apikey 存活，账号无危险）"
        logger.info(f"馒头 browse 未确认但账号存活: {browse_msg}")
    else:
        ss["last_error"] = browse_msg
        logger.warning(f"馒头保号失败且 profile 不可用: {browse_msg}")


# ════════════════════════════════════════════════════════════════
#  任务计算（严格按站点要求）
# ════════════════════════════════════════════════════════════════
def _ka_newbie(stats: dict) -> bool:
    """是否「新账号」：注册未满 30 天，或上传/下载都为 0（辅种还没起量）。
    新号的流量/分享率/新手考核类任务标 'new' 不计未达标。"""
    j = str(stats.get("joined") or "").strip()
    if j:
        try:
            import datetime as _dt
            txt = j.replace("/", "-")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                try:
                    d = _dt.datetime.strptime(txt[:19], fmt)
                    return (_dt.datetime.now() - d).days < 30
                except Exception:
                    continue
        except Exception:
            pass
    up = float(stats.get("upload") or 0)
    dl = float(stats.get("download") or 0)
    return up <= 0 and dl <= 0


def _invite_progress(site: dict, stats: dict) -> dict:
    """各站「可邀请他人」的真实进度 —— 严格按 2026-10-17 实抓的官方规则页。

    ★ 核心纠正：邀请**不是**「攒够魔力数字就能兑换」，而是
      「账号等级达标 / 通过新手考核 → 站点发放邀请名额」。
      实测 7 站 invite.php 全部返回「没有该权限」，即当前无一解锁邀请权。
      之前把 hdarea=700 / nicept=300 当魔力门槛是错的（那是新手考核线，
      且 nicept 真实考核是四项，非 300）。

    返回 {need, cur, status}；status 仅 ok/warn（warn 不算保号故障）。
    """
    sid = site["id"]
    req = site.get("req") or {}
    up = float(stats.get("upload") or 0) / 1024**3
    dl = float(stats.get("download") or 0) / 1024**3
    bns = float(stats.get("bonus") or 0)
    ratio = float(stats.get("ratio") or 0)
    avail = int(stats.get("invite_avail") or 0)
    lvl = str(stats.get("level") or "")

    # ── 馒头 M-Team：官方 wiki 等级权限表 ──
    if sid == "mteam":
        need = "官方：知縣/Elite User 起可发邀请（升 Elite 需 8周+上传>400GB+分享率>3）；" \
               "另需先过新手考核三项（上传>30G、下载>15G、做种魔力>6000）"
        nb_ok = up > 30 and dl > 15 and bns > 6000
        parts = []
        if up <= 30: parts.append("上传还差 %.0fG" % max(0.0, 30 - up))
        if dl <= 15: parts.append("下载还差 %.0fG" % max(0.0, 15 - dl))
        if bns <= 6000: parts.append("做种魔力还差 %.0f" % max(0.0, 6000 - bns))
        if not nb_ok:
            return {"need": need, "cur": "距邀请：先过新手考核 —— " + " · ".join(parts),
                    "status": "warn"}
        # 新手过了，看等级/邀请位
        if avail > 0:
            return {"need": need, "cur": "✅ 新手已过，现有 %d 个邀请位" % avail, "status": "ok"}
        return {"need": need, "cur": "新手已过，待升 Elite User 解锁邀请位（上传 %.0fG/率 %.1f）"
                % (up, ratio), "status": "warn"}

    # ── 老师 NicePT：官方新手考核2025 四项 ──
    if sid == "nicept":
        it = req.get("newbie_items") or {}
        t_up = it.get("upload_gb", 200); t_dl = it.get("download_gb", 200)
        t_hr = it.get("seed_hours", 200); t_bn = it.get("bonus", 30000)
        need = "官方：通过新手考核2025 四项后站点发放邀请位 —— " \
               "上传>200GB、下载>200GB、平均做种>200h、魔力>30000"
        parts = []
        if up < t_up: parts.append("上传 %.1f/%dG" % (up, t_up))
        if dl < t_dl: parts.append("下载 %.1f/%dG" % (dl, t_dl))
        # 做种时长 stats 里没稳定字段，用魔力做代理提示
        if bns < t_bn: parts.append("魔力 %.0f/%d" % (bns, t_bn))
        if avail > 0:
            return {"need": need, "cur": "✅ 现有 %d 个邀请位" % avail, "status": "ok"}
        if not parts:
            return {"need": need, "cur": "数据接近达标，待站点核验后发放邀请位", "status": "warn"}
        return {"need": need, "cur": "距邀请（新手考核）：" + " · ".join(parts), "status": "warn"}

    # ── 高清视界 HDArea：官方 Crazy User 起可邀请 ──
    if sid == "hdarea":
        need = "官方：达到 Crazy User 及以上即可发送邀请（升级靠做种时间/上传/分享率）"
        if avail > 0:
            return {"need": need, "cur": "✅ 现有 %d 个邀请位" % avail, "status": "ok"}
        return {"need": need, "cur": "当前等级 %s · 邀请位 0 —— 刷做种/上传升 Crazy User 解锁"
                % (lvl or "未抓取"), "status": "warn"}

    # ── 织梦 / 拾刻 / 大青虫 / CarPT：站点按等级发放名额 ──
    #   侧栏「邀请[发送]: 剩余可用(总配额)」。织梦 0(2) = 剩 0 可用（总配额 2）；
    #   大青虫 1(2) = 剩 1 可用。官方规则页无"积分兑换邀请"条款，不按魔力估算。
    quota = int(stats.get("invite_quota") or 0)
    need = req.get("invite_rule") or "邀请名额由站点按等级/贡献发放（侧栏「邀请[发送]:剩余(总配额)」）"
    if avail > 0:
        return {"need": need,
                "cur": "✅ 剩余 %d 个邀请位可用（总配额 %d）" % (avail, quota),
                "status": "ok"}
    extra = ""
    if sid == "zmpt":
        extra = "（官方规则：需查验被邀请者三站低隐私；禁止公开场合邀请；禁止买卖邀请）"
    elif sid == "carpt":
        extra = "（零流量新号，先辅种产生上传/下载，14 天内不产流量会被封号）"
    elif sid == "cyanbug":
        extra = "（已发过邀请；Veteran User 及以上封存免删）"
    cur = "邀请位 0"
    if quota > 0:
        cur += "（总配额 %d，需等级/贡献解锁）" % quota
    cur += " —— 靠做种/上传/分享率升等级解锁" + extra
    return {"need": need, "cur": cur, "status": "warn"}


def calc_tasks(site: dict, st: dict) -> list[dict]:
    now = time.time()
    req = site["req"]
    stats = st.get("stats") or {}
    tasks: list[dict] = []

    # ★ 2026-10-15 新账号保护：注册未满 30 天（或上传/下载都为 0）的号，
    #   「流量/分享率/新手考核」不算未达标，标 'new'（养成中）——
    #   否则新号一注册就被判三条 bad，用户看到「全站未达标」以为保号没执行。
    newbie = _ka_newbie(stats)

    # ① 登录保活
    interval = req["login_interval"] * 86400
    since = (now - st["last_login"]) / 86400 if st["last_login"] else 999
    left = req["login_interval"] - since
    s = "ok" if since <= req["login_interval"] * 0.7 else ("warn" if since < req["login_interval"] else "bad")
    tasks.append({"key": "login", "name": "定期登录保活",
                  "need": f"每 {req['login_interval']} 天访问一次（站点红线 {req['login_days']} 天）",
                  "cur": f"距上次 {'%.1f' % since} 天，还剩 {('%.1f' % left) if left > 0 else '0'} 天",
                  "status": s, "mode": req["login_mode"]})

    # ② 防「无流量删号」
    ntd = req["no_traffic_days"]
    since_tr = (now - st.get("traffic_ts", 0)) / 86400 if st.get("traffic_ts") else 999
    s = "ok" if since_tr <= ntd * 0.6 else ("warn" if since_tr < ntd else "bad")
    if newbie and s != "ok":
        s = "new"
    tasks.append({"key": "traffic", "name": "保持流量增长",
                  "need": f"{ntd} 天内上传/下载必须有增量（否则触发无流量删号）",
                  "cur": ("新账号养成中（辅种起量后自动转正）" if s == "new"
                          else f"距上次流量变化 {'%.1f' % since_tr} 天"),
                  "status": s})

    # ③ 分享率（NexusPHP/M-Team 同款规则：下载为 0 时站点显示「无限」= 完美，
    #    各站 profile 里 ratio 原始值是 0，直接判会把所有 0 下载新号一直误报红）
    ratio = float(stats.get("ratio") or 0)
    mr = req["min_ratio"]
    dl_b = float(stats.get("download") or 0)
    up_b = float(stats.get("upload") or 0)
    if dl_b == 0 and up_b > 0:
        s = "ok"
        cur = "下载为 0 → 分享率无限（完美）"
    else:
        s = "ok" if ratio >= mr else ("warn" if ratio >= mr * 0.8 else "bad")
        if newbie and s != "ok":
            s = "new"
        cur = "新账号养成中（辅种起量后自动转正）" if s == "new" else f"当前 {ratio:.2f}"
    tasks.append({"key": "ratio", "name": "分享率维持",
                  "need": f"分享率 ≥ {mr}（下载为 0 时按无限处理）",
                  "cur": cur, "status": s})

    # ④ H&R / 做种
    if site["id"] == "mteam":
        # M-Team 无 H&R 制度；做种时长来自 profile.seedtime（秒）
        stime = float(stats.get("seedtime") or 0)
        tasks.append({"key": "hr", "name": "H&R / 做种",
                      "need": req["hr_desc"],
                      "cur": f"累计做种 {stime / 3600:.1f} 小时",
                      "status": "ok"})
    else:
        hr = int(stats.get("hr") or 0)
        seeding = int(stats.get("seeding") or 0)
        # ★ 2026-10-15：H&R 只按「未达标 H&R 条数」判；做种数为 0 不拉低达标
        #   （辅种由 seed 任务单独管）——否则 6 站全显示「未达标」，用户以为
        #   保号没执行，实际登录保活全都正常。
        s = "bad" if hr >= 8 else ("warn" if hr > 0 else "ok")
        tasks.append({"key": "hr", "name": "H&R / 做种",
                      "need": req["hr_desc"],
                      "cur": f"未达标 H&R {hr} 条 · 做种中 {seeding} 个", "status": s})

    # ⑤ 保号做种（长期）
    if site["id"] == "mteam":
        # 新版 M-Team 无 cookie 辅种链路，保号已改走 API（updateLastBrowse）
        tasks.append({"key": "seed", "name": "自动辅种（免费种子）",
                      "need": "新版 M-Team 无 cookie 辅种链路，保号走 API",
                      "cur": "不适用（API 保号）", "status": "off"})
    else:
        seed_n = len(st.get("seed_ids") or [])
        s = "ok" if seed_n >= 1 else "warn"
        tasks.append({"key": "seed", "name": "自动辅种（免费种子）",
                      "need": "保持 ≥1 个本站免费种子做种中（辅种即可，无需下载到本地）",
                      "cur": f"在管 {seed_n} 个", "status": s})

    # ⑥ 新手考核（各站口径不同，严禁互相套）
    if req.get("newbie") and site["id"] in ("mteam", "nicept", "hdarea"):
        up, dl = stats.get("upload", 0) / 1024**3, stats.get("download", 0) / 1024**3
        bns = stats.get("bonus", 0)
        if site["id"] == "mteam":
            # M-Team 官方：上传>30G、下载>15G、做种获得魔力>6000
            s = "ok" if (up > 30 and dl > 15 and bns > 6000) else "warn"
            cur = f"上传 {up:.1f}G / 下载 {dl:.1f}G / 做种魔力 {bns:.0f}"
        elif site["id"] == "nicept":
            # ★ 官方新手考核2025 四项（2026-10-17 实抓侧栏，非旧「300 时魔」）
            it = req.get("newbie_items") or {}
            t_up, t_dl, t_hr, t_bn = (it.get("upload_gb", 200), it.get("download_gb", 200),
                                      it.get("seed_hours", 200), it.get("bonus", 30000))
            s = "ok" if (up >= t_up and dl >= t_dl and bns >= t_bn) else "warn"
            cur = (f"上传 {up:.1f}/{t_up}G · 下载 {dl:.1f}/{t_dl}G · "
                   f"魔力 {bns:.0f}/{t_bn}（另需平均做种>{t_hr}h）")
        else:  # hdarea：4 种考核方式选 1，B 档时魔 1000 / L 档 700
            s = "ok" if bns >= 700 else "warn"
            cur = f"魔力 {bns:.0f}（4 选 1：B=1000 / L=700，Web 控制台确认所选方式）"
        # 新账号养成中不计未达标（与 traffic/ratio 同口径）
        if newbie and s != "ok":
            s = "new"
            cur = "新账号养成中（辅种起量后自动转正）"
        tasks.append({"key": "newbie", "name": "新手考核",
                      "need": req["newbie"], "cur": cur, "status": s})
    # ⑦ 邀请资格进度（用户要到各站邀请别人 → 清楚还差多少）
    #   ★ 全 7 站都列（含新加的 CarPT），口径按官方规则页实抓。
    inv = _invite_progress(site, stats)
    tasks.append({"key": "invite", "name": "邀请资格进度",
                  "need": inv["need"], "cur": inv["cur"], "status": inv["status"]})

    return tasks


def site_worst(tasks: list[dict]) -> str:
    for lv in ("bad", "warn"):
        if any(t["status"] == lv for t in tasks):
            return lv
    return "ok"


# ════════════════════════════════════════════════════════════════
#  定时任务：保活 tick / 辅种 / 群说明同步
# ════════════════════════════════════════════════════════════════
_notify_cd: dict[str, float] = {}


async def _notify_owner(site: dict, text: str, key: str) -> None:
    now = time.time()
    if now - _notify_cd.get(f"{site['id']}:{key}", 0) < 12 * 3600:
        return
    _notify_cd[f"{site['id']}:{key}"] = now
    try:
        await bot.send_message(int(S.OWNER), ui.clip_html(text), parse_mode=PM,
                               disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"保号告警发送失败: {e}")


async def _tick_async(site: dict, ss: dict) -> None:
    if site["id"] == "mteam":
        await mteam_tick(site, ss)      # 新版 M-Team：API 保号（JWT + apikey）
    else:
        login_ok = await do_login(site, ss)
        await probe(site, ss, login_ok=login_ok)
    tasks = calc_tasks(site, ss)
    ss["tasks"] = {t["key"]: {"status": t["status"]} for t in tasks}
    worst = site_worst(tasks)
    if worst == "bad":
        bad = [t for t in tasks if t["status"] == "bad"]
        # ★ 链接必须单独成行 + <a href> 标准格式：写成
        #   `https://xxx（私聊检查账号状态）` 会被 Telegram 整段识别成无效链接
        tail = ["", ui.divider()] + _url_block(
            site["home"], "私聊发「保号」可查看账号状态与进度")
        await _notify_owner(
            site,
            f"⚠️ <b>保号告警 · {site['name']}</b>\n"
            + "\n".join(f"· <b>{t['name']}</b>：{t['cur']}" for t in bad)
            + "\n".join(tail), worst)
    elif ss.get("last_error"):
        # ★ 2026-10-14：同一错误只在「首次出现」或「距上次告警>24h」时提醒，
        #   避免馒头 JWT 撤销这类「已知且需人工浏览器处理」的问题每 30 分钟刷屏。
        import hashlib as _hl
        err_fp = _hl.md5(ss["last_error"].encode()).hexdigest()[:12]
        last_fp, last_at = ss.get("err_fp"), float(ss.get("err_at") or 0)
        if err_fp != last_fp or (time.time() - last_at) > 86400:
            ss["err_fp"], ss["err_at"] = err_fp, time.time()
            await _notify_owner(site, f"⚠️ <b>保号异常 · {site['name']}</b>\n{ss['last_error']}", "err")


async def refresh_now() -> None:
    """★ 2026-10-16：Web 点「刷新魔力值」时立刻重抓一轮。

    实现：给每个启用站点打 kick 标记并立刻跑一次 tick()。
    tick() 里 `ss.pop("kick")` 会强制该站执行访问登录+探测（抓魔力值/上传下载/分享率），
    抓完写回 state.json，Web 侧读同一份文件即可立刻看到新值。
    """
    st = load_state()
    n = 0
    for site in KS.SITES:
        ss = st["sites"].setdefault(site["id"], _blank_site())
        if not ss["enabled"]:
            continue
        ss["kick"] = 1
        n += 1
    save_state(st)
    logger.info(f"已标记 {n} 个站点立即重抓保号数据")
    await tick()


async def tick() -> None:
    """每 30 分钟：遍历启用中的站点 —— 到期访问登录 + 探测 + 任务重算 + 告警"""
    st = load_state()
    now = time.time()
    dirty = False
    for site in KS.SITES:
        ss = st["sites"].setdefault(site["id"], _blank_site())
        if not ss["enabled"]:
            continue
        req = site["req"]
        need_login = now - ss["last_login"] > req["login_interval"] * 86400 * 0.8
        need_probe = now - ss["last_probe"] > 6 * 3600
        kick = ss.pop("kick", 0)          # Web 端「立即执行」请求
        if need_login or need_probe or kick:
            logger.info(f"保号[{site['name']}] 访问登录+探测 "
                        f"(login_due={need_login}, kick={bool(kick)})")
            try:
                await _tick_async(site, ss)
                dirty = True
            except Exception as e:
                logger.warning(f"保号[{site['name']}] 访问异常: {e}")
        # 轻量任务状态刷新（不发网络请求也保持状态新鲜）
        ss["tasks"] = {t["key"]: {"status": t["status"]} for t in calc_tasks(site, ss)}
    if dirty or True:
        save_state(st)


# ── 辅种：抓免费种子 → 推 qB（分类 keepalive） ──────────────────
_WISH_KW = {'ts': 0.0, 'kw': set()}


def _wish_keywords() -> set[str]:
    """求片/订阅片名关键词黑名单（10 分钟缓存）。

    ★ 2026-10-20 用户要求：求片下载完+上传完即结束，**不许二次下载**。
      根因是保号辅种会把「已经求过的片」当免费种重新挑走（嫁金钗案例）。
      这里从 kfc_wishlist / kfc_request 取片名，归一成小写关键词，
      辅种挑种时命中就跳过。
    """
    now = time.time()
    if _WISH_KW['kw'] and now - _WISH_KW['ts'] < 600:
        return _WISH_KW['kw']
    kw: set[str] = set()
    try:
        import pymysql
        c = pymysql.connect(host=S.DB['host'], port=int(S.DB['port']),
                            user=S.DB['user'], password=S.DB['password'],
                            database=S.GAME_DB, charset='utf8mb4',
                            cursorclass=pymysql.cursors.DictCursor,
                            connect_timeout=6)
        try:
            with c.cursor() as cur:
                for t in ('kfc_wishlist', 'kfc_request'):
                    try:
                        cur.execute("SELECT title, keyword FROM %s" % t)
                        for r in cur.fetchall():
                            for k in (r.get('title'), r.get('keyword')):
                                k = (k or '').strip().lower()
                                if len(k) >= 2:
                                    kw.add(k)
                    except Exception:
                        continue
        finally:
            c.close()
    except Exception as e:
        logger.debug(f"求片关键词黑名单读取失败: {e}")
    _WISH_KW.update(ts=now, kw=kw)
    return kw


def _pick_free_torrent(site: dict) -> dict | None:
    """NexusPHP 免费种列表挑一个种子（优先 5~120GB，避免过小被判刷流）。

    兼容两种列表结构：
      · 经典表格（class="row..."，大小在 <td> 里）
      · 新版 flex 布局（hdarea 等，大小在行尾且被标签分隔）
    做法：按 details.php 链接切行 → 剥标签取行内文本 → 第一个 大小 模式即本行大小。
    """
    sec = load_secrets().get("sites", {}).get(site["id"]) or {}
    pk, rss = sec.get("passkey", ""), sec.get("rss", "")
    # 黑名单（2026-10-14 加）：dead_ids=挂超12h仍0%的死种；seed_ids=已在管的
    # （防删除前重复选同一个）。案例：34917 Lost in Space 挂 24h 0%，免费列表
    # 顺序固定导致每次都重选它 → 永久跳过。
    _st0 = load_state()
    _ss0 = _st0["sites"].get(site["id"]) or {}
    _skip: set[int] = set()
    for i in (_ss0.get("dead_ids") or []) + (_ss0.get("seed_ids") or []):
        try:
            _skip.add(int(i))
        except Exception:
            pass
    # qB 现存种子名（防重复 add：qB 对已存在的 hash 会静默拒绝，选中也白选）
    _qb_names: set[str] = set()
    try:
        for x in (svc.qb_get("/api/v2/torrents/info") or []):
            _qb_names.add(str(x.get("name") or ""))
    except Exception:
        pass
    # ★ 2026-10-20 用户要求：求片完成即止，禁止保号辅种再把已求片片名挑一遍
    #   （案例：嫁金钗求片完成后，辅种又把它当免费种挑走 → 二次下载占盘）。
    #   做法：把 kfc_wishlist / kfc_request 里的片名做成关键词黑名单（10 分钟缓存），
    #   免费种名命中任一关键词就跳过。
    _wish_kw = _wish_keywords()
    if not pk and not _site_cookie(site):
        return None   # 既无 passkey 又无 cookie 才放弃；cookie 鉴权站（如 ptskit）无需 passkey
    url = f"{site['home'].rstrip('/')}/torrents.php?spstate=free"
    # ★ 2026-10-17：NAS 高负载（loadavg 22 / swap 满）时 25s 单次抓取频繁超时，
    #   导致辅种周期白跑、做种数上不去。改为 (连接10s/读取50s) × 2 次重试。
    html = None
    # ★ 2026-10-25：直连优先，代理只作兜底。
    #   实测 ptskit：走代理 _pick_free_torrent 返回 None，同一 cookie 同一 URL
    #   **直连**却拿到 314 个免费种 —— 站点对代理 IP 有限制/代理不稳。
    #   老代码无条件 proxies=_proxies()，导致「有 cookie 也抓不到免费种」。
    _px = _proxies()
    for _px_mode in ('direct', 'proxy'):
        if _px_mode == 'proxy' and not _px:
            continue
        for _attempt in (1, 2):
            try:
                ck = _site_cookie(site) or {}
                r = requests.get(url, cookies=ck,
                                 proxies=(_px if _px_mode == 'proxy' else None),
                                 timeout=(10, 50))
                if r.status_code != 200:
                    break
                html = r.text
                break
            except Exception as e:
                logger.debug(f"KA 免费种子抓取失败 {site['id']}({_px_mode}/第{_attempt}次): {e}")
                time.sleep(2)
        if html:
            break
    try:
        # 按种子链接切行（引号前缀确保不误匹配 userdetails.php；
        # id 后允许 &amp;hit=1 / &amp;dllist=1 等后缀）
        parts = re.split(r'href="details\.php\?id=(\d+)[^"]*"', html)
        # parts = [前缀, id1, 行1, id2, 行2, ...]
        for idx in range(1, len(parts) - 1, 2):
            tid, chunk = parts[idx], parts[idx + 1]
            try:
                if int(tid) in _skip:
                    continue
            except Exception:
                pass
            # ★ 2026-10-25：优先取 <a title="真实发布名">。PTS 实测 312 个候选行里
            #   195 行用老正则只抓到 `&nbsp;`（名称在 title 属性里，不在标签文本中）。
            mt = (re.search(r'<a[^>]+\btitle="([^"]{4,160})"', chunk)
                  or re.search(r'[^>]*>(?:<b>)?\s*([^<>]{4,120})', chunk))
            name = (H.unescape(mt.group(1)).strip() if mt else "").strip()
            if not name:
                continue
            txt = re.sub(r"<[^>]+>", " ", chunk[:3000])
            ms = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)\b", txt)
            if not ms:
                continue
            b = _size_to_bytes(ms.group(1) + " " + ms.group(2))
            # ★ 2026-10-25 放宽：原来只收 5G~120G，导致免费区大量小种被跳过、
            #   候选枯竭（PTS 实测连跑 4 次有 2 次「未抓到合适免费种」）。
            #   做种积分按「在管做种数/时间」算，不只看体积 → 小种一样攒积分，
            #   而且下载快、不占盘（磁盘只剩 176G 时尤其重要）。
            if KA_SEED_MIN_GB * 1024**3 <= b <= KA_SEED_MAX_GB * 1024**3:
                # ★ 2026-10-20：已求片片名一律跳过（禁止二次下载）
                nm_low = name.lower()
                if _wish_kw and any(k in nm_low for k in _wish_kw):
                    logger.debug(f"辅种跳过已求片片名: {name[:40]}")
                    continue
                return {"id": tid, "name": name, "size": b}
    except Exception as e:
        logger.debug(f"KA 免费种子抓取失败 {site['id']}: {e}")
    return None


_KA_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# 保号辅种种子的 qB 身份标签（hash 级，不会被下载盘归类改掉）。
# pt2cloud 磁盘回收、MT 整理都按它跳过 —— 站方规则：删/改做种中的文件
# = 该种子判定未完成，直接掉上传与魔力。保号种子必须永不被动。
KA_TAG = "保号"
# 每站保底做种数（2026-10-17 用户拍板 1→3：保号前提下多挂种涨魔力/上传）。
# seed_tick 每 20min 给不足目标的站补 1 个免费种（5~120GB，见 _pick_free_torrent）。
KA_SEED_TARGET = 3
# ★ 2026-10-25 辅种体积窗口（原硬编码 5G~120G，候选容易枯竭）
KA_SEED_MIN_GB = 0.5         # 最小 0.5G：小种下载快、攒做种积分一样有效
KA_SEED_MAX_GB = 120         # 最大 120G：PTS 免费区大量「短剧大包」>40G，
# ★ 2026-10-24 磁盘爆盘事故（3.7T 塞了 9.2T 任务、566 个 keepalive 种子）：
#   根因是 seed_once 加种后没记进 seed_ids，每 20 分钟以为不够就再加 → 无限膨胀。
#   这里加两道硬闸：① keepalive 总量上限 ② 磁盘可用空间下限。
# ★ 2026-10-25 修正：10-24 我把上限设成 40，结果 keepalive 已堆到 157 个 →
#   _ka_guard_ok() 永远返回 False，**辅种被彻底卡死**（PTS 做种积分、carpt/hdfans
#   补种全加不进）。数量上限本意是防爆盘，但真正的瓶颈是磁盘空间不是个数。
#   现在：数量上限放到 200（够 8 站各 20+），磁盘下限仍是硬闸。
KA_MAX_TORRENTS = 300         # ★ 2026-10-27 再放宽：200 又撞满了（182 stalledUP 僵尸种+
#   累积种），磁盘仍有 166G（硬闸 60G 才是真保护）。保号要持续加种，
#   数量上限只是防无节制，真正的瓶颈是磁盘空间。
KA_MIN_FREE_GB = 60           # 可用空间低于 60G 一律不再加种（硬闸）


def _ka_guard_ok() -> bool:
    """加种前的硬闸：磁盘够 + 总量没超。返回 False 表示不许再加。"""
    try:
        du = shutil.disk_usage("/downloads")
        free_gb = du.free / 1024 ** 3
        if free_gb < KA_MIN_FREE_GB:
            logger.warning(f"保号暂停加种：可用空间仅 {free_gb}G < {KA_MIN_FREE_GB}G")
            return False
    except Exception:
        pass
    try:
        arr = svc.qb_get("/api/v2/torrents/info?category=keepalive") or []
        if len(arr) >= KA_MAX_TORRENTS:
            logger.warning(f"保号暂停加种：keepalive 已有 {len(arr)} 个 ≥ 上限 {KA_MAX_TORRENTS}")
            return False
    except Exception:
        pass
    return True


def _looks_torrent(resp) -> bool:
    """判断响应是否为 .torrent（bencode：以 'd' 开头且含 announce）。"""
    try:
        ct = (resp.headers.get("Content-Type") or "").lower()
        body = resp.content or b""
        if "bittorrent" in ct:
            return True
        return (resp.status_code == 200 and len(body) > 100
                and body[:1] == b"d" and b"announce" in body[:8192])
    except Exception:
        return False


def _info_hash(blob: bytes) -> str:
    """从 .torrent 字节算 info_hash（sha1 of bencoded info dict）。
    ★ 用于精确匹配 qB 里刚加入的种子 —— 不靠名字（downhash 站 HTML 标题
      与 .torrent 内部 name 常不一致，如 [中文]英文 vs 纯英文）。"""
    try:
        i = blob.index(b"4:info")
        # 从 info 值起做 bencode 平衡扫描
        start = i + len(b"4:info")
        depth = 0
        j = start
        n = len(blob)
        if blob[start:start + 1] != b"d":
            return ""
        j = start + 1
        while j < n:
            c = blob[j:j + 1]
            if c in (b"d", b"l"):
                depth += 1; j += 1
            elif c == b"e":
                depth -= 1; j += 1
                if depth == 0:
                    break
            elif c == b"i":
                k = blob.index(b"e", j); j = k + 1
            elif c.isdigit():
                k = blob.index(b":", j)
                ln = int(blob[j:k])
                j = k + 1 + ln
            else:
                j += 1
        info = blob[start:j]
        return hashlib.sha1(info).hexdigest()
    except Exception:
        return ""


def _download_torrent(site: dict, tid: str) -> bytes | None:
    """用站点 cookie 下载 .torrent（cookie 鉴权站，如 ptskit：download.php?id=X 无 passkey）。
    先 GET；若返回"首次下载提示"HTML，则按 NexusPHP 表单 POST 确认（type=firsttime）后再取文件。
    ★ CarPT 这类 downhash 站：匿名 GET 只回提示页，带 cookie+passkey 才给文件 ——
      所以先试 cookie+passkey，再退 cookie-only，再退 firsttime POST。
    返回 .torrent 字节或 None。"""
    ck = _site_cookie(site)
    if not ck:
        return None
    pk = (load_secrets().get("sites", {}).get(site["id"]) or {}).get("passkey", "")
    base = f"{site['home'].rstrip('/')}/download.php?id={tid}"
    urls = []
    if pk:
        urls.append(base + "&passkey=%s" % pk)
    urls.append(base)
    last = None
    for u in urls:
        try:
            r = requests.get(u, cookies=ck, headers={"User-Agent": _KA_UA, "Referer": u},
                             proxies=_proxies(), timeout=40, allow_redirects=True)
            if _looks_torrent(r):
                return r.content
            last = r
        except Exception as e:
            logger.debug(f"KA 下载种子失败 {site['id']} #{tid} ({u}): {e}")
    # 命中首次下载提示页 → POST 确认表单拿文件
    try:
        r2 = requests.post(base, cookies=ck,
                           data={"id": tid, "type": "firsttime",
                                 "hidenotice": "1", "submit": "下载种子文件"},
                           headers={"User-Agent": _KA_UA, "Referer": base},
                           proxies=_proxies(), timeout=40, allow_redirects=True)
        if _looks_torrent(r2):
            return r2.content
    except Exception as e:
        logger.debug(f"KA 下载种子(firsttime)失败 {site['id']} #{tid}: {e}")
    return None


def _blacklist_dead(ss: dict, tid) -> None:
    """把 qB 拒绝/确认拉不动的种子 id 记入 dead_ids（选种时永久跳过）。
    容量封顶 30 条，超出丢最旧的（免费列表本就有限，防 state 膨胀）。"""
    dead = ss.setdefault("dead_ids", [])
    try:
        tid = int(tid)
    except Exception:
        return
    if tid not in dead:
        dead.append(tid)
    if len(dead) > 30:
        del dead[:-30]


async def seed_once(site: dict) -> dict:
    if site["id"] == "mteam":
        # 新版 M-Team 无 cookie 辅种链路（SPA + API），保号走 mteam_tick
        return {"ok": False, "msg": "新版 M-Team 无 cookie 辅种（保号已走 API）"}
    _st = load_state()
    ss = _st["sites"].setdefault(site["id"], _blank_site())
    pk = (load_secrets().get("sites", {}).get(site["id"]) or {}).get("passkey", "")
    # ★ 2026-10-14：最多换 3 个候选种。qB 拒绝（重复 hash/站点拒下载）或
    #   .torrent 拉取失败 → 该 id 进 dead_ids 黑名单，下一轮不再选它。
    #   修掉旧行为：选中死种 34917 后每次都重选同一个、永远补不上。
    t = None
    last_err = ""
    want_hash = ""
    for _attempt in range(3):
        t = await asyncio.to_thread(_pick_free_torrent, site)
        if not t:
            last_err = "未抓到合适的免费种子（或未绑定 cookie/passkey）"
            break
        if pk and not (site.get("req") or {}).get("seed_via_cookie"):
            # 有 passkey：给 qB 一个带 passkey 的下载 URL（NexusPHP 传统站，qB 匿名可取）
            want_hash = ""   # URL 添加：拿不到 blob，只能按名匹配
            dl = f"{site['home'].rstrip('/')}/download.php?id={t['id']}&passkey={pk}"
            ok = await asyncio.to_thread(
                svc.qb_post, "/api/v2/torrents/add", {"urls": dl, "category": "keepalive"})
        else:
            # 无 passkey（cookie 鉴权站，如 ptskit）或有 passkey 但需 cookie 会话
            # （如 CarPT downhash）：bot 用 cookie 自己下 .torrent，再把文件体推给 qB
            # （.torrent 内 announce 已含 passkey，qB 无需 cookie 即可做种）
            blob = await asyncio.to_thread(_download_torrent, site, t["id"])
            if not blob:
                last_err = "下载 .torrent 失败（cookie 失效或站点改版）"
                _blacklist_dead(ss, t["id"])
                save_state(_st)
                continue
            want_hash = _info_hash(blob)   # ★ downhash 站按 hash 精确匹配
            ok = await asyncio.to_thread(
                svc.qb_post, "/api/v2/torrents/add", {"category": "keepalive"},
                files={"torrents": ("%s.torrent" % t["id"], blob, "application/x-bittorrent")})
        if ok:
            break
        # qB 静默拒绝（重复/死链）→ 拉黑换下一个
        last_err = "qB 未接受该种子（重复已存在或站点拒绝）"
        _blacklist_dead(ss, t["id"])
        save_state(_st)
        t = None
    if not t:
        return {"ok": False, "msg": last_err or "无可选种子"}
    if ok:
        # 确保归类到 keepalive（部分 qB 版本 add 的 category 参数不生效，按名补设一次，
        # 避免辅种内容混进「订阅」被整理流水线当影视扫走）
        # ★ 同时打「保号」标签：这是 hash 级身份标，category 会被下载盘归类改成
        #   「订阅」，但标签没人动 —— pt2cloud 磁盘回收 / sortpath 都按它免疫，
        #   防止保号做种文件被删（站方判定：动过文件/删过种 = 未完成种子，掉魔力）。
        # ★ 同时如实探测：qB 对重复种子返回 202 但实际拒绝（日志「检测到尝试添加重复的
        # 种子文件」），必须确认种子真的在 qB 里才算成功；URL 添加的元数据拉取有延迟，
        # 最多重试 3 次。
        try:
            found = False
            for _ in range(4):
                for x in (svc.qb_get("/api/v2/torrents/info") or []):
                    # ★ downhash 站（CarPT）HTML 标题 ≠ .torrent 内部名 →
                    #   有 want_hash 时按 info_hash 精确匹配，否则退回按名匹配。
                    hit = ((x.get("hash") or "").lower() == want_hash.lower()) \
                        if want_hash else ((x.get("name") or "") == t["name"])
                    if hit:
                        found = True
                        if x.get("category") != "keepalive":
                            svc.qb_post("/api/v2/torrents/setCategory",
                                        {"hashes": x["hash"], "category": "keepalive"})
                        if KA_TAG not in (x.get("tags") or ""):
                            svc.qb_post("/api/v2/torrents/addTags",
                                        {"hashes": x["hash"], "tags": KA_TAG})
                        break
                if found:
                    break
                time.sleep(2)
            if not found:
                return {"ok": False, "msg": "qB 未接受该种子（重复已存在或站点拒绝）"}
        except Exception:
            pass
        ss.setdefault("seed_ids", []).append(t["id"])
        save_state(_st)
        return {"ok": True, "msg": f"已推 qB 做种：{t['name']}（{t['size']/1024**3:.1f}GB）"}
    return {"ok": False, "msg": "qB 推送失败检查 qB 连接"}


async def seed_tick() -> None:
    """每 20 分钟：给启用中的站补免费种子做种（保持 ≥1 个）。
    ★ 2026-10-13：支持 Web「指定辅种」——ss['seed_kick'] 置位即强制该站跑一次辅种
      （先消费掉标记再跑，避免一次触发被重复执行）。"""
    st = load_state()
    kicked, changed = [], False
    for site in KS.SITES:
        ss = st["sites"].get(site["id"]) or {}
        if not ss.get("enabled"):
            continue
        if ss.pop("seed_kick", 0):
            kicked.append(site)
            changed = True
    if changed:
        save_state(st)      # 先落盘消费掉 seed_kick
    for site in kicked:
        try:
            logger.info(f"保号[{site['name']}] 指定辅种（Web 触发）")
            await seed_once(site)
        except Exception as e:
            logger.debug(f"指定辅种 {site['id']}: {e}")
    # 常规：保持每站 ≥1 个做种
    st2 = load_state()
    for site in KS.SITES:
        if site["id"] == "mteam":
            continue        # 新版 M-Team 无 cookie 辅种链路（保号走 API）
        ss = st2["sites"].get(site["id"]) or {}
        if not ss.get("enabled"):
            continue
        if site not in kicked and len(ss.get("seed_ids") or []) < KA_SEED_TARGET:
            if not _ka_guard_ok():
                break        # 磁盘满/总量超限 → 本轮不再给任何站加种
            try:
                await seed_once(site)
            except Exception as e:
                logger.debug(f"辅种 {site['id']}: {e}")


# ════════════════════════════════════════════════════════════════
#  群说明同步（括号标注 = 需自备 115 会员）
# ════════════════════════════════════════════════════════════════
_DESC_BLOCK = ("\n⚡ 发电(Emby)：群内发「发电」看规则价格\n"
               "（括号）标注=需自备115会员｜自带注册码+续期码 ¥5｜无需自备 ¥10/月\n"
               "📺 Emby服保号：群内发「保号」看账号活跃/防回收规则\n"
               "群游戏赚V积分可兑换｜PT站点保号为群主专属")


async def group_sync() -> None:
    """群说明同步：把旧的保号块整段替换成新版（发电=Emby公开 / 保号=Emby服规则 / PT保号=群主专属）"""
    for gid in list(getattr(S, "GROUP", []) or []):
        try:
            chat = await bot.get_chat(int(gid))
            desc = chat.description or ""
            base = desc
            # 切掉旧块（从任意一个旧标记开始的全是过期内容）
            for mk in ("\n⚡ 发电(Emby)", "\n⚡ PT保号"):
                i = base.find(mk)
                if i >= 0:
                    base = base[:i]
                    break
            new = (base + _DESC_BLOCK)[:255]
            if "发电(Emby)" in desc and new == desc:
                continue                        # 已是最新，别重复 set 触发 flood
            await bot.set_chat_description(int(gid), new)
            logger.info(f"已更新群说明（{gid}）")
        except Exception as e:
            logger.debug(f"群说明更新失败 {gid}: {e}")


# ════════════════════════════════════════════════════════════════
#  文案渲染
# ════════════════════════════════════════════════════════════════
def _name_show(site: dict) -> str:
    """（括号）标注 = 需自备 115 会员"""
    return f"（{site['name']}）" if site["need_own_115"] else site["name"]


_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "bad": "❌", "pending": "⏳", "off": "⛔"}


def panel_text(st: dict) -> str:
    lines = ["⚡ <b>PT 保号中心</b>（管理员）", ui.divider(),
             "🔧 自动登录保活 · 免费种子辅种 · H&R/分享率监控",
             "💡 「（括号）」= 需自备 115 会员（面向群友的「发电」服务）", ui.divider()]
    for site in KS.SITES:
        ss = st["sites"].get(site["id"]) or _blank_site()
        if not ss["enabled"]:
            lines.append(f"⛔ {_name_show(site)}　<b>已停用</b>")
            continue
        req = site["req"]
        since = (time.time() - ss["last_login"]) / 86400 if ss["last_login"] else 999
        left = max(0.0, req["login_interval"] - since)
        _st2 = ss.get("stats") or {}
        ratio = float(_st2.get("ratio") or 0)
        # 下载=0 且上传>0 → 站点分享率实为无限（与 calc_tasks/Web 同口径）
        if float(_st2.get("download") or 0) == 0 and float(_st2.get("upload") or 0) > 0:
            ratio_disp = "∞"
        else:
            ratio_disp = f"{ratio:.2f}"
        seeding = int(_st2.get("seeding") or 0)
        hr = int(_st2.get("hr") or 0)
        go = f"剩 {left:.0f} 天访问" if ss["last_login"] else "待首次访问"
        lines.append(f"{'✅' if left > 3 else '⚠️'} {_name_show(site)}　{go}")
        lines.append(f"　　📊 分享率 {ratio_disp} · 做种 {seeding} · H&R {hr}"
                     f"　| 每 {req['login_interval']} 天访问"
                     + (f"　| {ss['last_error']}" if ss.get("last_error") else ""))
    lines.append(ui.divider())
    lines.append("👇 点站点看规则/进度/启停；「保号规则」看价格与说明")
    return ui.clip_html("\n".join(lines))


def panel_kb(st: dict) -> KM:
    rows = []
    for site in KS.SITES:
        ss = st["sites"].get(site["id"]) or _blank_site()
        mark = ui.mark(ss["enabled"])
        rows.append([B(f"{mark} {site['name']}", callback_data=f"ka:site:{site['id']}")])
    rows.append([B("📖 保号规则·价格", callback_data="ka:price"),
                 B("🔄 刷新", callback_data="ka:home")])
    return KM(rows)


def site_text(site: dict, ss: dict) -> str:
    req = site["req"]
    stats = ss.get("stats") or {}
    tasks = calc_tasks(site, ss)
    lines = [f"{site['sym']} <b>{_name_show(site)}</b>", ui.divider(),
             f"{'🟢 运行中' if ss['enabled'] else '⛔ 已停用'}　类型 {site['kind']}"]
    # ★ 站点链接独占一行（标准 <a href>），绝不在这行后面追加任何说明文字
    lines += _url_block(site["home"], "站点首页（保活访问的就是它）")
    lines += [ui.divider(), "<b>📋 保号任务与状态</b>"]
    for t in tasks:
        lines.append(f"{_STATUS_ICON.get(t['status'], '❔')} <b>{t['name']}</b>")
        lines.append(f"　　要求：{t['need']}")
        lines.append(f"　　现状：{t['cur']}")
    lines.append(ui.divider())
    lines.append(f"📊 上传 {stats.get('upload', 0)/1024**3:.1f}G · "
                 f"下载 {stats.get('download', 0)/1024**3:.1f}G · "
                 f"分享率 {float(stats.get('ratio') or 0):.2f}")
    if stats.get("level"):
        lines.append(f"🎖 等级 {stats['level']}" + (f"（{stats['joined']} 注册）" if stats.get("joined") else ""))
    lines.append(f"🕒 上次访问：{'%.1f 天前' % ((time.time()-ss['last_login'])/86400) if ss['last_login'] else '从未'}"
                 f"　绑定：{'✅ 已抓取' if ss['bound'] else '❌ 未抓取（跑绑定脚本）'}")
    if ss.get("last_error"):
        lines.append(f"⚠️ {ss['last_error']}")
    return ui.clip_html("\n".join(lines))


def site_kb(site: dict, ss: dict) -> KM:
    return KM([
        [B(("⛔ 停用本站保号" if ss["enabled"] else "▶️ 开启本站保号"),
           callback_data=f"ka:tgl:{site['id']}"),
         B("📖 完整规则", callback_data=f"ka:rules:{site['id']}")],
        [B("▶️ 立即访问+探测", callback_data=f"ka:run:{site['id']}"),
         B("🌱 补一个免费种子", callback_data=f"ka:seed:{site['id']}")],
        [B("⬅️ 返回总览", callback_data="ka:home")],
    ])


def site_rules_text(site: dict) -> str:
    lines = [f"{site['sym']} <b>{_name_show(site)}</b> · 官方规则要点", ui.divider(),
             f"🔗 <a href=\"{site['rules_url']}\">规则原文</a>", ui.divider()]
    lines += [f"· {r}" for r in site["rules"]]
    lines.append(ui.divider())
    req = site["req"]
    lines.append(f"⏱ 删号红线：未封存 <b>{req['login_days']} 天</b>不登录 / "
                 f"无流量 <b>{req['no_traffic_days']} 天</b>")
    lines.append(f"🔁 本系统访问间隔：每 <b>{req['login_interval']} 天</b>（{req['login_mode']} 模式）")
    return ui.clip_html("\n".join(lines))


def rules_price_text(public: bool = False) -> str:
    """保号规则 + 价格 + V 积分提示（用户指定口径）

    ★ 2026-10-13：这是**面向所有人**的完整规则说明。咨询「保号规则」时直接
      整段推送，**不要求绑定、不要求管理员权限**（降低咨询门槛）。
      public=True 时补一段「想办理/查看进度」的引导，并把键盘换成公开版。
    """
    lines = ["📖 <b>PT 保号规则 · 价格 · 完整说明</b>", ui.divider(),
             "保号 = 帮你的 PT 站点账号做日常养护，系统会：",
             "　① 按各站红线定期登录保活（不登录会被删号）",
             "　② 自动挑免费种子辅种，把分享率做上去",
             "　③ 监控分享率 / H&R / 新手考核，快踩线时告警",
             "　④ 同步到 MoviePilot，求片时可直接搜到", ui.divider(),
             "<b>💰 价格</b>"]
    lines += KS.PRICE_LINES
    lines.append(ui.divider())
    lines.append(KS.VPOINT_HINT)
    lines.append(KS.RULE_UNIFIED)
    lines.append(ui.divider())
    lines.append(f"<b>📌 {KS.BRACKET_NOTE}</b>")
    lines.append("　" + "　".join(_name_show(s) for s in KS.SITES))
    lines.append(ui.divider())
    lines.append("<b>⏱ 各站红线与访问频率</b>")
    for s in KS.SITES:
        r = s["req"]
        lines.append(f"{s['sym']} <b>{_name_show(s)}</b>")
        lines.append(f"　· 未登录 <b>{r['login_days']} 天</b> 即删/封号"
                     f"　|　无流量 <b>{r['no_traffic_days']} 天</b> 即删号")
        lines.append(f"　· 本系统每 <b>{r['login_interval']} 天</b> 访问一次"
                     f"　|　分享率安全线 {r['min_ratio']}")
    lines.append(ui.divider())
    lines.append("<b>🌐 站点直达</b>（点击打开官网）")
    for s in KS.SITES:
        # ★ 链接一律独占一行，行尾不挂任何说明文字
        lines.append(_url_line(s["home"], f"{s['name']}", icon="　·"))
    lines.append(ui.divider())
    if public:
        lines.append("💡 这是<b>公开规则说明</b>，查询无需绑定账号、无需权限。")
        lines.append("　想办理保号 / 查看自己账号状态 → 私聊群主或管理员。")
        lines.append("　配套服务说明：发「<b>发电</b>」看 Emby 增值服务规则。")
    else:
        lines.append("🛠 管理员：发「<b>保号</b>」打开保号总览，可逐站启停、立即访问。")
    return ui.clip_html("\n".join(lines), limit=3800)


def price_kb(public: bool = False) -> KM:
    """规则页键盘。public=True ⇒ 不出现「返回总览」（非管理员进不去）"""
    if public:
        return KM([
            [B("⚡ 看发电规则", callback_data="pwr:show")],
            [B("📋 我的求片", callback_data="qpmine"),
             B("❌ 关闭", callback_data="ka:close")],
        ])
    return KM([[B("⬅️ 返回总览", callback_data="ka:home")],
               [B("❌ 关闭", callback_data="ka:close")]])


async def send_rules(msg_or_cb, public: bool = True, edit: bool = False) -> None:
    """把完整规则说明推给提问者（私聊/群聊/回调编辑都走这里）"""
    text = rules_price_text(public=public)
    kb = price_kb(public=public)
    try:
        if edit:
            await msg_or_cb.message.edit_text(
                text, parse_mode=PM, reply_markup=kb,
                disable_web_page_preview=True)
        else:
            await msg_or_cb.reply(text, parse_mode=PM, reply_markup=kb,
                                  disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"推送保号规则失败: {e}")


# ════════════════════════════════════════════════════════════════
#  命令入口
#   ★ 2026-10-13 分层（用户要求「降低咨询门槛、取消强制绑定」）：
#     · **规则咨询**（保号规则 / 保号帮助 / 保号价格）→ 公开，任何人可查，
#       直接整段推送完整规则说明，不需要绑定、不需要管理员权限。
#     · **操作面板**（保号 / 保号状态 / 站点直达）→ 仍限群主与授权管理员，
#       无权限时不再只回一句「⛔ 不可用」，而是把完整规则推给他 + 引导找管理员。
# ════════════════════════════════════════════════════════════════
_EXACT = {"保号", "保号规则", "保号状态", "保号帮助",
          "保号中心", "pt保号", "ka"}

# 规则咨询：公开（免绑定 / 免权限）
_RULE_WORDS = {"保号规则", "保号帮助", "保号价格", "pt保号规则", "ka规则",
               "保号规则说明", "保号说明", "kahelp"}


@bot.on_message(filters.text, group=0)
async def ka_text(client, msg: Message):
    """无斜杠触发：保号（总览）/ 保号规则。

    ★ 2026-10-14（用户拍板）分层：
      · 群友发「保号 / 保号规则 / 保号状态」→ 想看的是 **Emby 服保号规则**
        （纯 Emby，无任何 PT 内容）。
      · PT 站点保号（总览 / PT 规则 / 站点直达）→ 仅群主 / 授权管理员。
    """
    try:
        if not msg.text:
            raise ContinuePropagation
        u = msg.from_user
        if not u or u.is_bot:
            raise ContinuePropagation
        t = re.sub(r"\s+", "", msg.text.strip()).lower().replace("！", "!").replace("？", "?").rstrip("!.。~～,，?？")
        is_ka = perm.can(u.id, "ka")

        async def _emby():
            from . import power
            await msg.reply(power.emby_keepalive_text(), parse_mode=PM,
                            reply_markup=power.emby_ka_kb(),
                            disable_web_page_preview=True)

        # ① 规则咨询：管理员看 PT 规则；群友看 Emby 服保号规则
        if t in _RULE_WORDS:
            if is_ka:
                await send_rules(msg, public=False)
            else:
                await _emby()
            return

        # ② 操作总览：管理员专属；群友给 Emby 服保号规则
        if t in ("保号", "保号状态", "保号中心", "ka", "pt保号"):
            if not is_ka:
                await _emby()
                return
            st = load_state()
            await msg.reply(panel_text(st), parse_mode=PM,
                            reply_markup=panel_kb(st), disable_web_page_preview=True)
            return

        # ③ 站点直达：保号 zmpt / ka 馒头 …（管理员专属；群友给 Emby 规则）
        m = re.match(r"^(?:保号|pt保号|ka)[,，:：\s]?(\w{2,10})$", t)
        if m:
            site = _match_site(m.group(1))
            if site:
                if not is_ka:
                    await _emby()
                    return
                ss = load_state()["sites"].get(site["id"]) or _blank_site()
                await msg.reply(site_text(site, ss), parse_mode=PM,
                                reply_markup=site_kb(site, ss),
                                disable_web_page_preview=True)
                return

        raise ContinuePropagation
    except ContinuePropagation:
        raise
    except Exception as e:
        logger.debug(f"ka_text: {e}")
        raise ContinuePropagation


def _match_site(kw: str) -> dict | None:
    kw = kw.lower()
    for s in KS.SITES:
        if kw == s["id"].lower() or kw in s["name"].lower() or kw in s["sym"]:
            return s
    alias = {"织梦": "zmpt", "青虫": "cyanbug", "大青虫": "cyanbug",
             "拾刻": "ptskit", "馒头": "mteam", "mt": "mteam"}
    return KS.SITES_BY_ID.get(alias.get(kw, "")) if kw in alias else None


@bot.on_message(filters.command(["ka", "保号", "保号规则", "karule"], **CMD))
async def ka_cmd(client, msg: Message):
    """/ka 系列：
      · 群友 / 规则咨询 → Emby 服保号规则（公开，免绑定）
      · PT 保号总览 / PT 规则 → 仅群主 / 授权管理员
    """
    u = msg.from_user
    if not u:
        return
    arg = (msg.text or "").split(maxsplit=1)
    kw = re.sub(r"\s+", "", arg[1]).lower() if len(arg) > 1 else ""
    cmd = (msg.text or "").split()[0].lstrip("/!").lower()
    is_ka = perm.can(u.id, "ka")

    async def _emby():
        from . import power
        await msg.reply(power.emby_keepalive_text(), parse_mode=PM,
                        reply_markup=power.emby_ka_kb(),
                        disable_web_page_preview=True)

    # 「/保号规则」「/karule」「/ka 规则」→ 管理员看 PT 规则；群友看 Emby
    if cmd in ("保号规则", "karule") or kw in ("规则", "rule", "价格", "说明", "帮助"):
        if is_ka:
            await send_rules(msg, public=False)
        else:
            await _emby()
        return
    if not is_ka:
        await _emby()
        return
    st = load_state()
    await msg.reply(panel_text(st), parse_mode=PM,
                    reply_markup=panel_kb(st), disable_web_page_preview=True)


# ── 回调 ────────────────────────────────────────────────────────
@bot.on_callback_query(filters.regex(r"^ka:(home|price|close)$"))
async def cb_ka_nav(client, cb: CallbackQuery):
    act = cb.data.split(":")[1]
    uid = cb.from_user.id if cb.from_user else 0

    if act == "close":
        try:
            await cb.message.delete()
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await cb.answer("已关闭")
        return

    if act == "price":
        # ★ 规则页公开：非管理员也能翻（面板里的「返回总览」会自己校验权限）
        await send_rules(cb, public=not perm.can(uid, "ka"), edit=True)
        await cb.answer()
        return

    if not perm.can(uid, "ka"):
        await cb.answer("⛔ 保号总览仅群主 / 授权管理员可用", show_alert=True)
        return
    st = load_state()
    await cb.message.edit_text(panel_text(st), parse_mode=PM,
                               reply_markup=panel_kb(st),
                               disable_web_page_preview=True)
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^ka:site:(\w+)$"))
@perm.gate_cb("ka")
async def cb_ka_site(client, cb: CallbackQuery):
    sid = cb.data.split(":")[2]
    site = KS.SITES_BY_ID.get(sid)
    if not site:
        await cb.answer("站点不存在", show_alert=True)
        return
    ss = load_state()["sites"].get(sid) or _blank_site()
    await cb.message.edit_text(site_text(site, ss), parse_mode=PM,
                               reply_markup=site_kb(site, ss),
                               disable_web_page_preview=True)
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^ka:tgl:(\w+)$"))
@perm.gate_cb("ka")
async def cb_ka_tgl(client, cb: CallbackQuery):
    sid = cb.data.split(":")[2]
    site = KS.SITES_BY_ID.get(sid)
    if not site:
        await cb.answer("站点不存在", show_alert=True)
        return
    st = load_state()
    ss = st["sites"].setdefault(sid, _blank_site())
    ss["enabled"] = not ss["enabled"]
    save_state(st)
    await cb.message.edit_text(site_text(site, ss), parse_mode=PM,
                               reply_markup=site_kb(site, ss),
                               disable_web_page_preview=True)
    await cb.answer(("▶️ 已开启" if ss["enabled"] else "⛔ 已停用") + f" {site['name']} 保号")


@bot.on_callback_query(filters.regex(r"^ka:rules:(\w+)$"))
@perm.gate_cb("ka")
async def cb_ka_rules(client, cb: CallbackQuery):
    sid = cb.data.split(":")[2]
    site = KS.SITES_BY_ID.get(sid)
    if not site:
        await cb.answer("站点不存在", show_alert=True)
        return
    ss = load_state()["sites"].get(sid) or _blank_site()
    await cb.message.edit_text(site_rules_text(site), parse_mode=PM,
                               reply_markup=site_kb(site, ss),
                               disable_web_page_preview=True)
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^ka:run:(\w+)$"))
@perm.gate_cb("ka")
async def cb_ka_run(client, cb: CallbackQuery):
    sid = cb.data.split(":")[2]
    site = KS.SITES_BY_ID.get(sid)
    if not site:
        await cb.answer("站点不存在", show_alert=True)
        return
    await cb.answer("⏳ 正在访问站点…")
    st = load_state()
    ss = st["sites"].setdefault(sid, _blank_site())
    if site["req"]["login_mode"] == "web":
        ss["last_error"] = "M-Team 必须真实浏览器访问（第三方工具不算登录），请用 Edge 抓取脚本访问"
    await _tick_async(site, ss)
    save_state(st)
    await cb.message.edit_text(site_text(site, ss), parse_mode=PM,
                               reply_markup=site_kb(site, ss),
                               disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^ka:seed:(\w+)$"))
@perm.gate_cb("ka")
async def cb_ka_seed(client, cb: CallbackQuery):
    sid = cb.data.split(":")[2]
    site = KS.SITES_BY_ID.get(sid)
    if not site:
        await cb.answer("站点不存在", show_alert=True)
        return
    await cb.answer("⏳ 正在找免费种子…")
    r = await seed_once(site)
    await cb.answer(r["msg"][:180], show_alert=True)


def init() -> None:
    """启动时建目录 + 落默认状态（幂等）"""
    try:
        os.makedirs(KA_DIR, exist_ok=True)
        st = load_state()
        save_state(st)
        logger.info(f"保号系统就绪：{len(KS.SITES)} 站，state={STATE_PATH}")
    except Exception as e:
        logger.warning(f"保号系统初始化失败: {e}")

