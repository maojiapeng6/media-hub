"""榜单：影片榜 / 用户榜，日・周・月；**任何群友都可自助查询**

数据来源：Emby 的 Playback Reporting 插件「自定义 SQL 查询」接口
    POST /emby/user_usage_stats/submit_custom_query
         {"CustomQueryString": "...", "ReplaceUserId": false}
    返回 {"colums": [...], "results": [[...], ...]}   ← 注意官方拼写就是 colums

底层表 PlaybackActivity 字段（实测）：
    DateCreated / UserId / ItemId / ItemType / ItemName /
    PlaybackMethod / ClientName / DeviceName /
    PlayDuration / PauseDuration / RemoteAddress / TranscodeReasons
真实观看时长 = PlayDuration - PauseDuration（单位：秒）

插件未安装时返回 None，并给出明确提示而不是抛异常。

交互（v2）：所有榜单消息都带按钮，点一下就能换周期 / 换类型 / 看自己排名，
不需要记命令、也不需要重发消息。
"""
from __future__ import annotations

import asyncio
import io
import os
import time

import requests
from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import InputMediaPhoto
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import Message

from .. import bot
from .. import db
from .. import poster
from .. import settings as S
from .. import ui

# ★ 本模块原先多处用 parse_mode=PM 但**从没定义过 PM**，一旦走到就 NameError
#   （认领引导那段就中招）。这里补齐，与其余模块口径一致。
PM = ParseMode.HTML
from ..helper import send_group, send_group_photo
from . import perm

CMD = dict(prefixes=S.PREFIX)

_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 180
_UMAP: dict = {"ts": 0.0, "m": {}}

# 最近一次渲染出来的「用户榜」快照，供「认领名字」按钮复用
_PANEL: dict = {"ts": 0.0, "days": 1, "names": [], "taken": {},
                "chat": None, "mid": None}


def _key(s) -> str:
    """归一化比对键：小写 + 只保留字母数字。

    Emby 用户名和 TG 用户名经常只差大小写、空格、下划线，
    直接 == 比对会漏掉一大半，这里统一压成纯字母数字再比。
    """
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


# ────────────────────────────────────────────── 底层
def _custom(sql: str) -> list[dict] | None:
    """执行插件自定义 SQL；返回 None 表示插件不可用/出错。

    ★ 2026-10-24 修日报失败：Emby 的 PlaybackActivity 是 SQLite，播放写入并发时
      偶发锁库导致单次请求 30s 读超时（查询本身 0.0s 就返回）。改为「重试 + 退避」，
      最多 3 次、间隔递增，超时逐次放宽；只在连续失败后才判定不可用。
    """
    last_err = ""
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(
                S.EMBY["url"].rstrip("/") + "/emby/user_usage_stats/submit_custom_query",
                params={"api_key": S.EMBY["api_key"]},
                json={"CustomQueryString": sql, "ReplaceUserId": False},
                timeout=30 + attempt * 20,   # 30 → 50 → 70
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                logger.warning(f"自定义查询 HTTP {r.status_code}（第{attempt+1}次）")
            else:
                j = r.json()
                logger.debug(f"播放库自定义查询 {time.time() - t0:.2f}s（第{attempt+1}次）")
                if isinstance(j, dict) and "colums" in j:
                    cols = j.get("colums") or []
                    rows = j.get("results") or []
                    return [dict(zip(cols, row)) for row in rows]
                return None   # 结构异常（非超时）→ 不重试
        except Exception as e:
            last_err = str(e)[:120]
            logger.warning(f"自定义查询失败（第{attempt+1}次）: {last_err}")
        time.sleep(1.5 * (attempt + 1))   # 退避 1.5s / 3s
    logger.warning(f"自定义查询重试 3 次仍失败：{last_err}")
    return None


def _api(path: str, **params):
    try:
        params["api_key"] = S.EMBY["api_key"]
        r = requests.get(S.EMBY["url"].rstrip("/") + path, params=params, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.warning(f"Emby 接口失败 {path}: {e}")
        return None


def user_map() -> dict[str, str]:
    """Emby UserId -> 用户名"""
    if _UMAP["m"] and time.time() - _UMAP["ts"] < 600:
        return _UMAP["m"]
    data = _api("/emby/Users")
    if isinstance(data, list):
        _UMAP["m"] = {str(u["Id"]): str(u.get("Name") or "?") for u in data if u.get("Id")}
        _UMAP["ts"] = time.time()
    return _UMAP["m"]


def _norm(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


# ────────────────────────────────────────────── Emby 用户名 -> TG ID
_TGMAP: dict = {"ts": 0.0, "m": {}}


def tg_map() -> dict[str, int]:
    """Emby 用户名 -> TG ID。
    优先用群友 /bind 自己绑定的（最可靠），其次才是 EmbyBoss 的 emby 表。
    """
    if _TGMAP["m"] and time.time() - _TGMAP["ts"] < 900:
        return _TGMAP["m"]
    m: dict[str, int] = {}
    try:
        m.update(db.bind_map())
    except Exception as e:
        logger.debug(f"读取自愿绑定失败: {e}")
    from sqlalchemy import text
    try:
        with db._engine.connect() as c:
            rows = c.execute(text(
                f"SELECT tg, name FROM `{S.SHARED_DB}`.emby "
                "WHERE name IS NOT NULL AND name <> '' AND name <> '?'")).all()
        for tg, name in rows:
            m.setdefault(str(name), int(tg))
    except Exception as e:
        logger.warning(f"读取 Emby->TG 映射失败: {e}")
    _TGMAP["m"] = m
    _TGMAP["ts"] = time.time()
    return m


# ────────────────────────────────────────────── 两种榜单
def item_stats(days: int = 1) -> list[dict] | None:
    """影片榜：[{name, plays, minutes}]"""
    key = f"item{days}"
    if key in _CACHE and time.time() - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]
    sql = (
        "SELECT ItemId AS iid, ItemName AS n, COUNT(1) AS p, "
        "SUM(PlayDuration - PauseDuration) AS d "
        "FROM PlaybackActivity "
        f"WHERE DateCreated >= datetime('now', '-{int(days)} day') "
        "AND UserId NOT IN (SELECT UserId FROM UserList) "
        "GROUP BY ItemName ORDER BY d DESC LIMIT 80"
    )
    rows = _custom(sql)
    if rows is None:
        return None
    agg: dict[str, dict] = {}
    for r in rows:
        raw = str(r.get("n") or "").strip()
        if not raw:
            continue
        # 剧集名形如 "剧名 - S01E01"，归并到剧名
        name = raw.split(" - ")[0].strip() if " - " in raw else raw
        rec = agg.setdefault(name, {"name": name, "plays": 0, "minutes": 0.0,
                                    "iid": r.get("iid")})
        rec["plays"] += int(_norm(r.get("p")))
        rec["minutes"] += _norm(r.get("d")) / 60.0
    out = [v for v in agg.values() if v["plays"] > 0 or v["minutes"] > 0]
    out.sort(key=lambda x: (x["minutes"], x["plays"]), reverse=True)
    _CACHE[key] = (time.time(), out)
    return out


def user_stats(days: int = 1) -> list[dict] | None:
    """用户榜（谁看得最多）：[{name, plays, minutes}]"""
    key = f"user{days}"
    if key in _CACHE and time.time() - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]
    sql = (
        "SELECT UserId AS u, COUNT(1) AS p, "
        "SUM(PlayDuration - PauseDuration) AS d "
        "FROM PlaybackActivity "
        f"WHERE DateCreated >= datetime('now', '-{int(days)} day') "
        "AND UserId NOT IN (SELECT UserId FROM UserList) "
        "GROUP BY UserId ORDER BY d DESC LIMIT 80"
    )
    rows = _custom(sql)
    if rows is None:
        return None
    umap = user_map()
    tmap = tg_map()
    out = []
    for r in rows:
        uid = str(r.get("u") or "")
        name = umap.get(uid)
        if not name:
            continue
        plays, mins = int(_norm(r.get("p"))), _norm(r.get("d")) / 60.0
        if plays <= 0 and mins <= 0:
            continue
        # ★ 2026-10-17 修复：TG ID 反查加「群成员自动比对」兜底。
        #   原来只查 tg_map()（自愿绑定表），没绑过的用户 tg=None →
        #   _user_caption 拿不到链接，榜单用户名全是纯文本。
        #   用户明确要求「有 TG 用户名就该能反推 UID」——群成员表
        #   （load_members 缓存，1914 个 TG 用户名 / 2077 人）就是干这个的。
        _tg = tmap.get(str(name))
        if not _tg:
            try:
                _tg = match_tg(str(name))
            except Exception:
                _tg = None
        # 顺手把 TG 用户名/显示名带上，caption 直接用，省一次反查
        _un = ""
        _disp = ""
        if _tg:
            try:
                _tu = tg_user_of(_tg) or {}
                _un = str(_tu.get("username") or "").lstrip("@")
                _fn = "".join(ch for ch in str(_tu.get("first") or "")
                              if ch.isprintable()).strip()
                _disp = _fn
            except Exception:
                pass
        out.append({"name": name, "plays": plays, "minutes": mins,
                    "tg": _tg, "uid": uid, "username": _un, "display": _disp})
    out.sort(key=lambda x: (x["minutes"], x["plays"]), reverse=True)
    _CACHE[key] = (time.time(), out)
    return out


# ────────────────────────────────────────────── 渲染
def _render(items: list[dict], title: str, top: int = 10) -> list[str]:
    lines = [f"🏆 **{title}**", ui.divider()]
    for i, v in enumerate(items[:top], 1):
        hr = v["minutes"] / 60
        lines.append(f"{ui.rank_tag(i)}｜**{ui.safe(v['name'])}**｜"
                     f"{v['plays']} 次｜{hr:.1f} 小时")
    if not items:
        lines.append("_暂无播放数据（插件已装好，看片后就会有记录）_")
    return lines


PLUGIN_HINT = ("请在 Emby 后台 → 插件 → 目录 搜索 **Playback Reporting** 安装后重启 Emby。")

_LABEL = {1: "今日", 7: "本周", 30: "本月"}


def _board_kb(days: int = 1, kind: str = "i") -> KM:
    """所有榜单共用的导航键盘"""
    def mk(k: str, d: int, text: str) -> B:
        return B(("✅ " if (k == kind and d == days) else "") + text, f"rk:{k}:{d}")
    rows = [
        [mk("i", 1, "🎬 今日"), mk("i", 7, "🎬 本周"), mk("i", 30, "🎬 本月")],
        [mk("u", 1, "👥 今日"), mk("u", 7, "👥 本周"), mk("u", 30, "👥 本月")],
    ]
    if kind == "u":
        # 用户榜多一个「认领」入口：Emby 用户名和 TG 账号没有现成的对应关系，
        # 让本人点一下认领，名字才会变成能点开个人主页的 TG 链接。
        rows.append([B("🏅 我的观看排名", "rk:mine"), B("🙋 认领我的名字", "rk:claim")])
    else:
        rows.append([B("🏅 我的观看排名", "rk:mine")])
    return KM(rows)


def _stats(kind: str, days: int) -> list[dict] | None:
    return item_stats(days) if kind == "i" else user_stats(days)


def _title(kind: str, days: int) -> str:
    return f"{_LABEL.get(days, str(days) + '天')}{'影片榜' if kind == 'i' else '用户榜'}"


# Telegram 媒体图注硬上限 1024 字符；配图时必须压在下面，否则整张图发不出。
MEDIA_CAP_MAX = 1020


def _media_cap(kind: str, title: str, data: list[dict], cap_full: str) -> str:
    """配图时使用的图注：用户榜用精简版（每行一个用户），影片榜超长则截断，
    确保不触发 MEDIA_CAPTION_TOO_LONG。纯文字消息（无图）没有此限制，可用完整版。"""
    if kind == "u":
        return _user_caption(title, data, compact=True)
    if len(cap_full) <= MEDIA_CAP_MAX:
        return cap_full
    return cap_full[:MEDIA_CAP_MAX - 1].rstrip() + "…"


def _user_id_card(title: str, items: list[dict], top: int = 10) -> str:
    """★ 2026-10-17 新增：用户榜「可点击身份卡」。

    图片 caption 有两个硬伤：① 手机端默认折叠，用户根本看不到；② 1024 字上限。
    而用户明确要求「用户名/UID/Emby用户名 各占一行、名字本身是超链接」。
    所以在图片发成功后，单独跟一条纯文本消息（4096 上限）放完整身份卡。

    链接统一用 `tg://user?id=<TG数字ID>`（mention 实体）：点开是**个人主页
    卡片**（资料页弹窗），不会跳进聊天框。链接文字不套粗体 —— mention+bold
    叠在同一段时 TG 客户端会吞链接（实测）。
    """
    shown = [v for v in items[:top]]
    if not shown:
        return ""
    lines = [f"👥 **{ui.safe(title)} · 可点击身份卡**", _rule()]
    miss = 0
    for i, v in enumerate(shown, 1):
        nm = ui.safe(v.get("name") or "未知")
        tg = v.get("tg")
        try:
            tg = int(tg) if tg else 0
        except Exception:
            tg = 0
        hr = (v.get("minutes") or 0) / 60.0
        plays = v.get("plays") or 0
        lines.append(f"{_head(i)}｜{plays} 次｜{hr:.1f} 小时")
        if tg > 0:
            url = "tg://user?id=%d" % tg
            # 有 TG 用户名就显示它，否则退回 Emby 名（都能点）
            disp = ui.safe((v.get("username") or "").lstrip("@")) or \
                   ui.safe(v.get("display") or "") or nm
            lines.append(f"**👤 用户名：** [{disp}]({url})")
            lines.append(f"**│　UID：** [{tg}]({url})")
            lines.append(f"**│　Emby用户名：** [{nm}]({url})")
        else:
            # 没反查到 TG ID：用户名/Emby名仍给出来，UID 明说未关联
            lines.append(f"**👤 用户名：** {nm}")
            lines.append("**│　UID：** _未关联 TG_")
            lines.append(f"**│　Emby用户名：** {nm}")
            miss += 1
        lines.append(_rule())
    if miss:
        lines.append(f"🙋 有 **{miss}** 位还没关联 TG，点头像下方「🙋 认领我的名字」即可点亮链接")
    else:
        lines.append("✅ 本榜全员已点亮主页链接，点用户名/UID 直达个人主页卡片")
    return "\n".join(lines)


async def _board_photo(title: str, data: list[dict], kind: str = "i") -> bytes | None:
    """渲染海报失败会返回 None，调用方负责降级成文字
    kind: i=影片榜（用封面海报） / u=用户榜（用彩色头像卡片）
    """
    note = f"{S.SERVER_NAME} · {time.strftime('%Y-%m-%d %H:%M')}"
    fn = poster.draw_user_board if kind == "u" else poster.draw_board
    return await asyncio.to_thread(fn, title, data, note)


async def _render_board(client, kind: str, days: int):
    """取数 → 补资料 → 出图，一次做完。

    返回 (data, caption, png)：
      · data is None  => 播放统计插件不可用
      · png is None   => 渲染失败（极少见），调用方需降级成文字
    """
    data = await asyncio.to_thread(_stats, kind, days)
    if data is None:
        return None, "", None
    title = _title(kind, days)
    _t0 = time.time()
    if kind == "u":
        data = await _prepare_users(client, data)
        cap = _user_caption(title, data)
        _PANEL.update(ts=time.time(), days=days,
                      names=[str(v["name"]) for v in data[:10]],
                      taken={str(v["name"]): bool(v.get("tg")) for v in data[:10]})
    else:
        data = await _prepare_items(data)
        cap = _item_caption(title, data)
    png = await _board_photo(title, data, kind)
    # ★ 2026-10-16：出图耗时日志（用户反馈榜单慢，加日志便于验证提速效果）
    logger.info(f"⏱ 榜单渲染 {title}：{len(data)} 条 / 图 "
                f"{'None' if not png else str(len(png) // 1024) + 'KB'} / "
                f"耗时 {time.time() - _t0:.1f}s")
    return data, cap, png


async def _put_board(client, message, png: bytes, cap: str, kb):
    """把榜单贴到目标消息上，成功返回最终那条消息，失败返回 None。

    ★ 这里是「用户榜没有图片」的根因所在：
      旧代码一律走 edit_media，但 /top 发出来的是一条**纯文字**消息，
      对文字消息调用 edit_media 必然失败，又被 except 静默吞掉退回文字，
      于是用户看到的永远是「没有图片的榜单」。
      现在分两种情况：
        · 目标本来就是图片 → 原地换图（edit_media），不产生新消息
        · 目标是一条文字   → 删掉它，重新发一张带图的消息
    """
    if getattr(message, "photo", None) or getattr(message, "document", None):
        try:
            await message.edit_media(
                InputMediaPhoto(io.BytesIO(png), caption=cap), reply_markup=kb)
            return message
        except MessageNotModified:       # 连点同一个按钮：图没变不算失败
            return message
        except Exception as e:
            logger.warning(f"榜单换图失败: {e}")
            return None
    try:                                  # 删掉旧的文字消息，避免群里堆积
        await message.delete()
    except Exception:
        pass
    try:
        return await client.send_photo(message.chat.id, io.BytesIO(png),
                                       caption=cap, reply_markup=kb)
    except Exception as e:
        logger.warning(f"发送榜单图片失败: {e}")
        return None


# ══════════════════════════════════════════════ 资料补全（TMDB / TG 头像）
_META: dict[str, tuple[float, dict]] = {}
_META_TTL = 3600


def _parse_meta(j: dict) -> dict:
    """从单个 Emby Item 解析出 TMDB 链接 / 年份 / 评分"""
    meta: dict = {}
    prov = j.get("ProviderIds") or {}
    tid = prov.get("Tmdb") or prov.get("tmdb") or ""
    typ = j.get("Type") or ""

    if not tid:                      # MT 生成 strm 时把 tmdbid 写进了目录名
        import re
        m = re.search(r"tmdbid-(\d+)", j.get("Path") or "")
        if m:
            tid = m.group(1)
            if not typ or typ == "Episode":
                typ = "Series"

    if not tid and j.get("SeriesId"):   # 单集没元数据，向上找剧集
        sj = _api("/emby/Items", Ids=str(j["SeriesId"]),
                  Fields="ProviderIds,ProductionYear")
        sarr = (sj or {}).get("Items") if isinstance(sj, dict) else None
        sj = sarr[0] if sarr else None
        if isinstance(sj, dict):
            tid = (sj.get("ProviderIds") or {}).get("Tmdb") or ""
            if not j.get("ProductionYear") and sj.get("ProductionYear"):
                j["ProductionYear"] = sj["ProductionYear"]

    if tid:
        base = ("https://www.themoviedb.org/tv/"
                if typ in ("Series", "Episode", "Season")
                else "https://www.themoviedb.org/movie/")
        meta["url"] = base + str(tid)
    if j.get("ProductionYear"):
        meta["year"] = int(j["ProductionYear"])
    if j.get("CommunityRating"):
        meta["rating"] = round(float(j["CommunityRating"]), 1)
    meta["type"] = typ
    return meta


def _batch_meta(ids: list[str]) -> dict[str, dict]:
    """批量取元数据。

    ⚠️ 两个关键点：
      1) 播放记录里的 ItemId 是「数字内部 ID」，`/emby/Items/{id}` 会 404，
         必须走 `/emby/Items?Ids=xxx`
      2) Ids 支持逗号分隔，10 个一次请求 —— 逐个查要 10 次 HTTP，慢 10 倍
    """
    now = time.time()
    out: dict[str, dict] = {}
    todo: list[str] = []
    for i in ids:
        c = _META.get(i)
        if c and now - c[0] < _META_TTL:
            out[i] = c[1]
        else:
            todo.append(i)

    for k in range(0, len(todo), 10):
        chunk = todo[k:k + 10]
        j = _api("/emby/Items", Ids=",".join(chunk), Limit=10,
                 Fields="ProviderIds,ProductionYear,Type,CommunityRating,Path,SeriesId")
        arr = (j or {}).get("Items") if isinstance(j, dict) else None
        got = set()
        for it in (arr or []):
            iid = str(it.get("Id"))
            m = _parse_meta(it)
            _META[iid] = (time.time(), m)
            out[iid] = m
            got.add(iid)
        for i in chunk:               # 查不到的也缓存空值，避免每次都重试
            if i not in got:
                _META[i] = (time.time(), {})
                out[i] = {}
    return out


async def _prepare_items(data: list[dict]) -> list[dict]:
    """给影片榜补 TMDB 链接 / 年份 / 评分（批量，一次请求）"""
    def work():
        ids = [str(i["iid"]) for i in data[:10] if i.get("iid")]
        if not ids:
            return data
        metas = _batch_meta(ids)
        for it in data[:10]:
            if it.get("iid"):
                it.update(metas.get(str(it["iid"]), {}))
        return data
    return await asyncio.to_thread(work)


_AVATAR: dict[str, tuple[float, bytes | None]] = {}


def _emby_avatar(uid: str) -> bytes | None:
    """Emby 用户头像兜底：EmbyBoss 的 name 字段大面积为空，反查不到 TG 时用这个"""
    now = time.time()
    c = _AVATAR.get(uid)
    if c and now - c[0] < 3600:
        return c[1]
    data = None
    try:
        r = requests.get(
            f"{S.EMBY['url'].rstrip('/')}/emby/Users/{uid}/Images/Primary",
            params={"api_key": S.EMBY["api_key"], "maxWidth": 300}, timeout=12)
        if r.status_code == 200 and len(r.content) > 500 and r.content[:4] != b"<htm":
            data = r.content
    except Exception:
        pass
    _AVATAR[uid] = (now, data)
    return data


_MEMBERS: dict = {"ts": 0.0, "m": {}, "u": {}, "n": {}, "info": {}}


async def load_members(client) -> dict[str, int]:
    """拉主群成员，建「用户名/昵称 -> TG ID」映射，并顺手把资料缓存下来。

    EmbyBoss 的 emby.name 基本是空的，没法直接反查；但 Emby 用户名往往
    跟这个人的 TG 用户名或昵称一致，所以拿群成员表来比对是最靠谱的办法。

    关键性能点：get_chat_members 返回的 User 对象里**已经带着** username
    和头像 file_id。这里一次性缓存下来，后面补资料时就能直接复用，
    不必再为每个上榜用户单独发一次 get_users —— 10 个人就是 10 ~ 20 次
    多余的 Telegram 往返，那正是「点一下等半天」的来源。
    """
    if _MEMBERS["m"] and time.time() - _MEMBERS["ts"] < 1800:
        return _MEMBERS["m"]
    m: dict[str, int] = {}
    mu: dict[str, int] = {}          # 只收 TG 用户名（最可靠，重名概率极低）
    mn: dict[str, int] = {}          # 收昵称 / 姓名（次可靠）
    info: dict[int, dict] = {}
    for gid in S.GROUP:
        try:
            async for mem in client.get_chat_members(gid):
                u = mem.user
                if not u or getattr(u, "is_bot", False):
                    continue
                ph = getattr(u, "photo", None)
                rec = {
                    "username": getattr(u, "username", None),
                    "first": getattr(u, "first_name", None),
                    "photo": getattr(ph, "big_file_id", None),
                }
                info[int(u.id)] = rec
                if u.username:
                    mu[str(u.username).strip().lower()] = u.id
                    mu[_key(u.username)] = u.id
                for key in (u.username, u.first_name,
                            f"{u.first_name or ''}{u.last_name or ''}"):
                    if key and str(key).strip():
                        m[str(key).strip().lower()] = u.id
                        m[_key(key)] = u.id
                        if key is not u.username:
                            mn[str(key).strip().lower()] = u.id
                            mn[_key(key)] = u.id
        except Exception as e:
            logger.warning(f"拉取群成员失败 {gid}: {e}")
    if m:
        _MEMBERS.update(m=m, u=mu, n=mn, info=info, ts=time.time())
        logger.info(f"群成员表已缓存：{len(m)} 个键 / {len(mu)} 个 TG 用户名 / "
                    f"{len(info)} 人")
    return m


def match_tg(name: str) -> int | None:
    """拿一个 Emby 用户名去群成员表里找 TG ID。

    优先级：TG 用户名 > 群昵称/姓名 > 手机号/数字号段。
    昵称容易撞（群里叫「www」的可能不止一个意思），所以昵称匹配要求更长。

    ★ 「数字用户名」是重灾区：Emby 名常见 `18091416699` / `2463146414` /
      `007` 这种，用手机号或QQ 号当名字。群里这类人的 TG 用户名往往就是
      同一串数字，所以按**纯数字**单独建一张表去撞，命中率比昵称高得多。
    """
    k = _key(name)
    if len(k) < 3:
        return None
    tg = _MEMBERS["u"].get(k)
    if tg:
        return int(tg)
    # 昵称重名概率高，要求更长；中文名信息量大，2 个字就够判断了
    cjk = any("一" <= ch <= "鿿" for ch in k)
    if len(k) >= (2 if cjk else 4):
        tg = _MEMBERS["n"].get(k)
        if tg:
            return int(tg)
    # 纯数字用户名（手机号/QQ号）：门槛降到 5 位，降低撞号风险
    if k.isdigit() and len(k) >= 5:
        tg = _MEMBERS["u"].get(k) or _MEMBERS["n"].get(k)
        if tg:
            return int(tg)
    return None


def auto_bind(min_report: bool = False) -> dict:
    """批量自动关联：把所有 Emby 用户名拿去群里比对，能唯一命中的直接落库。

    ★ 为什么不能「直接调用他们的 TG」：
      Emby 那边只给得出一个**用户名**（18091416699 / aliang / www …），
      它既不存 TG ID 也不存手机号，EmbyBoss 的映射表又是空的 —— 没有任何字段
      能把两条数据连起来。能做的只有「名字比对」：Emby 名 == 群里某人的 TG 用户名
      （或昵称）时就自动关联；对不上的（比如用手机号当 Emby 名的）才需要本人认领。
    """
    if not _MEMBERS["u"] and not _MEMBERS["m"]:
        return {"ok": 0, "skip": 0, "bound": [], "conflict": []}
    umap = user_map()
    if not umap:
        return {"ok": 0, "skip": 0, "bound": [], "conflict": []}

    names = sorted({str(v).strip() for v in umap.values()
                    if v and str(v).strip()})
    hits: dict[str, int] = {}
    for name in names:
        tg = match_tg(name)
        if tg:
            hits[name] = tg
    logger.info(f"Emby→TG 自动比对：Emby 名 {len(names)} 个 / 命中 {len(hits)} 个"
                f" / 样例 {names[:6]}")

    # 一个 TG 账号被多个 Emby 名命中 = 有歧义，全部放弃，宁可让人自己认领
    cnt: dict[int, list[str]] = {}
    for n, t in hits.items():
        cnt.setdefault(t, []).append(n)
    conflict = sorted(n for ns in cnt.values() if len(ns) > 1 for n in ns)

    bound = []
    for n, t in sorted(hits.items()):
        if n in conflict:
            continue
        if db.tg_by_emby_name(n):        # 这个 Emby 名已经有人认领/关联过
            continue
        if db.my_bind(t):                # 这个 TG 已经绑了别的 Emby 名
            continue
        db.set_bind(t, n)
        bound.append((n, t))

    # 无论有没有新增都清一次：user_stats 的缓存里也存了 tg，不清就会继续显示旧的
    _TGMAP["ts"] = 0.0
    _CACHE.clear()
    logger.info(f"自动关联结果：新增 {len(bound)} / 命中 {len(hits)} "
                f"/ 歧义 {len(conflict)}")
    return {"ok": len(bound), "skip": len(conflict),
            "bound": bound, "conflict": conflict}


def unbound_names(days: int = 30) -> list[str]:
    """还没关联 TG 的 Emby 用户名（按观看量从高到低，优先催活跃的）"""
    names = [str(v["name"]) for v in (user_stats(days) or [])
             if not v.get("tg")]
    if not names:
        names = [str(v) for v in user_map().values()
                 if v and not db.tg_by_emby_name(str(v))]
    return names


class _AvatarCache:
    """TG 头像磁盘缓存（用户 2026-10-16 要求榜单提速）。

    榜单每次都要画头像，原来每次都 get_chat_photos + download_media 走网络，
    10 个用户 ≈ 10+ 次往返 → 出图要 40~60s。这里按 tg 落盘到
    /app/keepalive/avatar_cache/<tg>.jpg，TTL 12 小时，首次之后秒出。
    写在 keepalive（rw 挂载）里，**重建容器不丢**。
    """

    DIR = "/app/keepalive/avatar_cache"
    TTL = 12 * 3600

    def ensure_dir(self) -> None:
        try:
            os.makedirs(self.DIR, exist_ok=True)
        except Exception as e:
            logger.debug(f"头像缓存目录创建失败: {e}")

    def _path(self, tg: int) -> str:
        return os.path.join(self.DIR, "%s.jpg" % int(tg))

    def get(self, tg: int) -> bytes | None:
        """命中且未过期则返回图片字节；miss / 过期 / 读失败一律返回 None。"""
        p = self._path(tg)
        try:
            st = os.stat(p)
            if time.time() - st.st_mtime > self.TTL:
                return None
            with open(p, "rb") as f:
                return f.read()
        except Exception:
            return None

    def put(self, tg: int, blob: bytes) -> None:
        try:
            self.ensure_dir()
            p = self._path(tg)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, p)          # 原子替换，避免并发写出半个文件
        except Exception as e:
            logger.debug(f"头像缓存写入失败 {tg}: {e}")


_AV_CACHE = _AvatarCache()


async def _prepare_users(client, data: list[dict]) -> list[dict]:
    """给用户榜补头像与身份：优先 TG 头像，取不到就退回 Emby 用户头像

    ★ 2026-10-16 性能优化（用户反馈「不就是生成个图片吗为什么这么慢」）：
      慢在头像 —— 原代码每次都 get_chat_photos + download_media 重新下载，
      10 个用户 = 10+ 次网络往返，叠加群成员加载能到 40~60s。
      现在加**磁盘缓存**（/app/keepalive/avatar_cache，TTL 12h）+ 并发上限，
      首次之后一律命中本地文件，榜单出图降到 1~2s。
    """
    members: dict[int, dict] = {}
    try:
        await load_members(client)
        members = _MEMBERS["info"]
        for it in data[:10]:
            if not it.get("tg") and it.get("name"):
                tg = match_tg(it["name"])
                if not tg:                       # 兜底：查用户自愿 /bind 的绑定表
                    tg = db.tg_by_emby_name(str(it["name"]))
                if tg:
                    it["tg"] = tg
    except Exception as e:
        logger.debug(f"群成员匹配失败: {e}")

    tgs = [i["tg"] for i in data[:10] if i.get("tg")]

    if tgs:
        _AV_CACHE.ensure_dir()

        async def one(tg: int):
            info: dict = {}
            known = members.get(tg) or {}
            if known.get("username") or known.get("first"):
                # 缓存里已有资料，直接复用，省掉一次 get_users
                info["username"] = known.get("username")
                info["first"] = known.get("first")
            else:
                try:
                    u = await client.get_users(tg)
                    info["username"] = getattr(u, "username", None)
                    info["first"] = getattr(u, "first_name", None)
                except Exception as e:
                    logger.debug(f"取 TG 资料失败 {tg}: {e}")
            # 头像：先查本地磁盘缓存，命中就不再走网络
            cached = _AV_CACHE.get(tg)
            if cached:
                info["avatar"] = cached
                return tg, info
            try:
                fid = known.get("photo")
                if not fid:                     # 缓存里没头像才去现查
                    photos = [p async for p in client.get_chat_photos(tg, limit=1)]
                    fid = photos[0].file_id if photos else None
                if fid:
                    buf = await client.download_media(fid, in_memory=True)
                    b = buf.getvalue()
                    info["avatar"] = b
                    _AV_CACHE.put(tg, b)          # 写盘，下次直接命中
            except Exception as e:
                logger.debug(f"取 TG 头像失败 {tg}: {e}")
            return tg, info

        # 限并发：一次性 10+ 个 download_media 容易撞 Telegram 限流反而更慢
        _sem = asyncio.Semaphore(4)

        async def guarded(tg):
            async with _sem:
                return await one(tg)

        for r in await asyncio.gather(*(guarded(t) for t in tgs),
                                      return_exceptions=True):
            if isinstance(r, tuple):
                for it in data:
                    if it.get("tg") == r[0]:
                        it.update(r[1])
                        it["tg_username"] = r[1].get("username")

    def fill():                       # 还没头像的，用 Emby 头像补上
        for it in data[:10]:
            if not it.get("avatar") and it.get("uid"):
                b = _emby_avatar(str(it["uid"]))
                if b:
                    it["avatar"] = b
        return data
    return await asyncio.to_thread(fill)


# ══════════════════════════════════════════════ 文字说明（图文并重）
def _fhm(minutes: float) -> str:
    m = int(max(minutes, 0))
    return f"{m} 分钟" if m < 60 else f"{m // 60} 时 {m % 60:02d} 分"


# 名次徽章统一表：前三金银铜，其余统一用同款徽章
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _head(i: int) -> str:
    """名次抬头。

    ★ 第 1~10 名一律是「奖牌 + 第X名」，X 都是单个汉字，
      所以抬头宽度完全一致，后面的正文自动排成一列，不会参差。
    """
    return f"{_MEDALS.get(i, '🏅')} 第{ui.cn_no(i)}名"


_IND = "　　"          # 第二行的缩进（两个全角空格），让数据行挂在名次下方


def _metrics(v: dict) -> str:
    """数据行：次数与时长统一带图标，任何名次都不缺符号"""
    return f"{_IND}▶️ {v['plays']} 次　⏱ {_fhm(v['minutes'])}"


def _tail(items: list[dict]) -> str:
    total = sum(i.get("minutes", 0) for i in items)
    return (f"📊 共 **{len(items)}** 条 · 累计 **{int(total // 60)} 时 "
            f"{int(total % 60):02d} 分**　🕒 `{time.strftime('%H:%M')}`")


def _item_caption(title: str, items: list[dict]) -> str:
    """影片榜：抬头「第X名」宽度一致，每部片两行：

    　🥇 第一名｜🎬 片名（年份）　⭐8.1
    　　　▶️ 12 次　⏱ 3 时 20 分
    """
    lines = [f"🏆 **{ui.safe(title)}**", ui.divider()]
    for i, v in enumerate(items[:10], 1):
        nm = ui.safe(v["name"])
        nm = nm if len(nm) <= 20 else nm[:19] + "…"
        link = f"[{nm}]({v['url']})" if v.get("url") else f"**{nm}**"
        yr = f"（{v['year']}）" if v.get("year") else ""
        lines.append(f"{_head(i)}｜🎬 {link}{yr}")
        lines.append(_metrics(v))
    lines += [ui.divider(), _tail(items)]
    return "\n".join(lines)


def _rule() -> str:
    """加粗分隔线（榜单专用，markdown 模式）"""
    return "**" + ui.divider() + "**"


def _user_caption(title: str, items: list[dict], compact: bool = False) -> str:
    """用户榜说明文字。

    ★ 严格按用户 2026-10-12 定下的格式：三个维度（用户名 / UID / Emby用户名）
      **各占一行，名字本身就是超链接**（tg://user?id=<TG数字ID>，mention 实体，
      点开是个人主页卡片而非聊天框）。不加 @，不自创「打开主页」等文案。
    ★ **链接文字一律不套粗体**：mention + bold 叠在同一段时，TG 客户端会把链接
      吞掉（实测只有部分行能点开），故这里只给链接；维度标签保持加粗。
    ★ compact=True 用于「配图」场景：Telegram 媒体图注硬上限 1024 字符，
      超出会 400 MEDIA_CAPTION_TOO_LONG 导致整张图发不出，故只取前若干名；
      完整名次以文字版（无图时）为准。
    """
    lines = [f"🏆 **{ui.safe(title)}**", _rule()]
    miss = 0
    shown = items[:5] if compact else items[:10]
    for i, v in enumerate(shown, 1):
        tg = v.get("tg")
        nm = ui.safe(v["name"])
        un = ui.safe((v.get("username") or "").lstrip("@"))
        lines.append(_head(i))
        if tg:
            url = "tg://user?id=" + str(tg)
            disp = un or nm           # 有 TG 用户名就用它，没有就退回 Emby 名
            lines.append(f"**👤 用户名：** [{disp}]({url})")
            lines.append(f"**│　UID：** [{tg}]({url})")
            lines.append(f"**│　Emby用户名：** [{nm}]({url})")
        else:
            lines.append(f"**👤 用户名：** {nm}")
            lines.append("**│　UID：** 未关联 TG")
            lines.append(f"**│　Emby用户名：** {nm}")
            miss += 1
        lines.append(_metrics(v))
        if not compact:
            lines.append(_rule())
    lines.append(_tail(items))
    if miss:
        lines.append(f"🙋 有 **{miss}** 位还没关联 TG，点下方「认领我的名字」即可点亮主页链接")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════ 定时推送
async def push_rank(client, days: int, title: str):
    # 同步 requests 会阻塞事件循环，放线程池里跑
    items = await asyncio.to_thread(item_stats, days)
    if items is None:
        await send_group(client, f"⚠️ **{title}** 暂时无法生成。\n{ui.divider()}\n{PLUGIN_HINT}")
        return
    users = await asyncio.to_thread(user_stats, days)
    kb = _board_kb(days, "i")

    items = await _prepare_items(items)
    ftitle = f"{title.split('·')[0].strip()} · 影片榜"
    png = await _board_photo(ftitle, items)
    if png and await send_group_photo(
            client, png,
            caption=_media_cap("i", ftitle, items, _item_caption(ftitle, items)),
            reply_markup=kb):
        # ★ 2026-10-17：定时推送也跟上「可点击身份卡」—— 用户要求所有涉及
        #   用户信息的播报，用户名/UID 都必须是能点开的超链接。
        if users:
            try:
                _u = await _prepare_users(client, users[:10])
                card = _user_id_card(f"{title.split('·')[0].strip()} · 用户榜", _u)
                if card:
                    await send_group(client, card, disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"定时推送身份卡失败: {e}")
        return
    # 降级：图片没发出去就退回文字，避免定时推送静默失败
    lines = _render(items, f"{title} · 影片榜", 10)
    if users:
        lines += ["", _user_caption(f"{title} · 用户榜", users[:5])]
    lines += ["", "💡 点按钮即可换周期 / 看用户榜 / 查自己的排名"]
    await send_group(client, "\n".join(lines), reply_markup=kb)


# ═══════════════════════════════════════════════════ 群内自助查询
@bot.on_message(filters.command(["dayrank", "day", "日榜"], **CMD))
async def cmd_dayrank(client, msg: Message):
    await _reply(client, msg, 1)


@bot.on_message(filters.command(["weekrank", "周榜"], **CMD))
async def cmd_weekrank(client, msg: Message):
    await _reply(client, msg, 7)


@bot.on_message(filters.command(["monthrank", "month", "月榜"], **CMD))
async def cmd_monthrank(client, msg: Message):
    await _reply(client, msg, 30)


_MENU = ("🔮 **选择要看的榜单**\n" + ui.divider() + "\n"
         "🎬 影片榜 = 哪部片最火\n"
         "👥 用户榜 = 谁看得最多\n\n"
         "_下面每个按钮都是直接可点的，选一次即可自动刷新_")


async def _reply(client, msg: Message, days: int, kind: str = "i"):
    data, cap, png = await _render_board(client, kind, days)
    if data is None:
        await msg.reply(f"⚠️ 榜单暂时无法生成。\n{ui.divider()}\n{PLUGIN_HINT}")
        return
    kb = _board_kb(days, kind)
    title = _title(kind, days)
    if png:
        try:
            sent = await msg.reply_photo(io.BytesIO(png),
                                         caption=_media_cap(kind, title, data, cap),
                                         reply_markup=kb)
            _PANEL.update(chat=sent.chat.id, mid=sent.id)
            return
        except Exception as e:
            logger.warning(f"发送榜单海报失败，降级为文字: {e}")
    await msg.reply(cap + "\n\n⚠️ _图片渲染失败，先用文字版_", reply_markup=kb)


@bot.on_message(filters.command(["top", "rankboard", "榜单"], **CMD))
async def cmd_top(client, msg: Message):
    """直接把「今日影片榜」画成图发出来。

    ★ 关键：第一条消息就必须是**图片**，后面点按钮换榜才能原地 edit_media。
      以前这里发的是纯文字菜单，导致之后每次换榜都只能退化成文字 ——
      也就是「榜单没有图片」的直接原因。
    """
    data, cap, png = await _render_board(client, "i", 1)
    kb = _board_kb(1, "i")
    if data is None:
        await msg.reply(f"⚠️ 榜单暂时无法生成。\n{ui.divider()}\n{PLUGIN_HINT}")
        return
    if png:
        try:
            sent = await msg.reply_photo(io.BytesIO(png), caption=cap,
                                         reply_markup=kb)
            _PANEL.update(chat=sent.chat.id, mid=sent.id)
            return
        except Exception as e:
            logger.warning(f"发送榜单海报失败，降级为文字: {e}")
    await msg.reply(_MENU, reply_markup=kb)


@bot.on_callback_query(filters.regex(r"^rk:(i|u):(\d+)$"))
async def cb_rk(client, cb):
    kind = cb.data.split(":")[1]
    days = int(cb.data.split(":")[2])
    try:
        # 先消耗回调查询：_prepare_users 含多次 TG 网络请求（get_users / 头像下载），
        # 整段可能 >30s，不提前 answer 会在结尾触发 QueryIdInvalid 且图发不出。
        await cb.answer("生成中…")
    except Exception:
        pass
    data, cap, png = await _render_board(client, kind, days)
    if data is None:
        try:
            await cb.message.edit_text("需要 Emby 安装 Playback Reporting 插件",
                                     reply_markup=_board_kb(days, kind))
        except Exception:
            pass
        return
    kb = _board_kb(days, kind)
    title = _title(kind, days)
    cap_media = _media_cap(kind, title, data, cap)
    m = await _put_board(client, cb.message, png, cap_media, kb) if png else None
    if m is not None:
        _PANEL.update(chat=m.chat.id, mid=m.id)
        # ★ 2026-10-17 用户要求：榜单用户名必须可点。
        #   图片 caption 在手机端会被折叠、且有 1024 字硬限，链接经常看不到 →
        #   图片发成功后，紧跟一条**纯文本身份卡**，列出该榜全部上榜用户的
        #   三维度（用户名/UID/Emby用户名）超链接，点开是个人主页卡片。
        if kind == "u":
            try:
                card = _user_id_card(title, data)
                if card:
                    await client.send_message(m.chat.id, card, disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"用户榜身份卡发送失败: {e}")
        return
    try:
        await cb.message.edit_text(cap + "\n\n⚠️ _图片发送失败，先用文字版_",
                                   reply_markup=kb)
    except Exception as e:
        logger.warning(f"榜单文字兜底失败: {e}")


# ═══════════════════════════════════════════════════ 认领自己的 Emby 名字
# Emby 用户名和 TG 账号之间**没有任何现成数据**能对上（EmbyBoss 的 emby 表
# 几乎全空），所以与其猜，不如让本人点一下认领 —— 认领后名字立刻变成
# tg://user?id=<TG ID> 的可点链接，头像也会换成 TA 的 TG 头像。
_CLAIM_TTL = 1800


def _free_names() -> list[str]:
    if time.time() - _PANEL["ts"] > _CLAIM_TTL or not _PANEL["names"]:
        return []
    return [n for n in _PANEL["names"] if not _PANEL["taken"].get(n)]


@bot.on_callback_query(filters.regex(r"^rk:claim$"))
async def cb_claim(client, cb):
    await cb.answer()
    free = _free_names()
    if not free:
        await cb.answer("榜单里的名字都已关联 TG 啦", show_alert=True)
        return
    cur = await asyncio.to_thread(db.my_bind, cb.from_user.id)
    rows, line = [], []
    for i, n in enumerate(free):
        line.append(B(("✅ " if n == cur else "") + n[:24], f"rk:claimok:{i}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([B("❌ 取消", "rk:claimno")])
    try:
        await cb.message.reply(
            "🙋 **认领你的 Emby 名字**\n" + ui.divider() + "\n"
            "⚠️ 为防止冒领，认领必须**私聊我**完成身份核验（要输一次该 Emby 账号的密码）。\n"
            "👉 请私聊我发 `/claim`，从列表里点你自己的名字。\n\n"
            "关联之后，榜单上你的名字就能**点开你的 TG 主页**，"
            "并且头像会换成你的 TG 头像。",
            reply_markup=KM(rows))
    except Exception as e:
        logger.warning(f"发送认领菜单失败: {e}")


@bot.on_callback_query(filters.regex(r"^rk:claimok:(\d+)$"))
async def cb_claimok(client, cb):
    free = _free_names()
    idx = int(cb.data.split(":")[2])
    if idx >= len(free):
        await cb.answer("这条已失效，请重新打开榜单再认领", show_alert=True)
        return
    name = free[idx]
    await cb.answer()
    try:
        # 身份核验涉及密码，严禁在群里进行 —— 引导去私聊
        await client.send_message(
            cb.from_user.id,
            f"🙋 **认领「{ui.safe(name)}」**\n{ui.divider()}\n"
            "为防止认领他人身份，请在**这里（私聊）**完成核验：\n"
            "直接发送该 Emby 账号的**密码**，验证通过立即关联。\n"
            f"{ui.divider()}\n发「取消」可放弃。",
            parse_mode=PM)
        from . import bind as _bd
        _bd._WAIT[cb.from_user.id] = {
            "step": "claim_pw", "claim_name": name, "ts": __import__("time").time()}
    except Exception as e:
        logger.warning(f"私聊认领引导失败: {e}")
        await cb.answer("请先私聊我发一次消息，再回来点认领", show_alert=True)


@bot.on_callback_query(filters.regex(r"^rk:claimno$"))
async def cb_claimno(client, cb):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^rk:mine$"))
async def cb_rk_mine(client, cb):
    await cb.answer()
    text, kb = await asyncio.to_thread(_myview, cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        logger.debug(f"编辑我的排名失败: {e}")


# ═══════════════════════════════════════════════════ 我的观看统计
def _myview(uid: int) -> tuple[str, KM]:
    cv = db.get_cultivator(uid)
    back = KM([[B("↩️ 回到榜单", "rk:i:1")]])
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start` 后再来查看。", back
    users = user_stats(30)
    if users is None:
        return f"⚠️ 无法查询观看统计。\n{ui.divider()}\n{PLUGIN_HINT}", back

    # Emby 名字有两条来源：自愿关联表 kfc_bind（主要），
    # 以及 EmbyBoss 的 emby.name（几乎全空，仅作兜底）
    emby_name = db.my_bind(uid)
    if not emby_name:
        from sqlalchemy import text
        try:
            with db._engine.connect() as c:
                r = c.execute(text(
                    f"SELECT name FROM `{S.SHARED_DB}`.emby WHERE tg = :tg"),
                    {"tg": uid}).first()
            emby_name = r[0] if r and r[0] and r[0] != "?" else None
        except Exception as e:
            logger.debug(f"读取 EmbyBoss 绑定失败: {e}")
    if not emby_name:
        return ("⚠️ 还没查到你的 Emby 名字，无法定位观看统计。\n"
                f"{ui.divider()}\n"
                "发 `/bind 你的Emby用户名`，或到用户榜点"
                "「🙋 认领我的名字」认领一下即可。"), back

    pos = next((i for i, v in enumerate(users, 1) if v["name"] == emby_name), None)
    me = next((v for v in users if v["name"] == emby_name),
              {"plays": 0, "minutes": 0.0})
    text = (
        f"📺 **{ui.safe(emby_name)} 的观看统计（近 30 天）**\n{ui.divider()}\n"
        f"🎬 播放次数｜{me['plays']} 次\n"
        f"⏱ 观看时长｜{me['minutes'] / 60:.1f} 小时\n"
        f"🏅 群内排名｜"
        + (f"第 **{ui.cn_no(pos)}** 名 / 共 {len(users)} 位" if pos else "尚未上榜")
        + f"\n{ui.divider()}\n"
        "💡 点按钮可随时切换看别的榜"
    )
    return text, back


@bot.on_message(filters.command(["myview", "myviewrank", "我的观看"], **CMD))
async def cmd_myview(client, msg: Message):
    text, kb = await asyncio.to_thread(_myview, msg.from_user.id)
    await msg.reply(text, reply_markup=kb)


# ═══════════════════════════════════════════════════ 自愿绑定 Emby 账号
def bind_text(uid: int) -> str:
    """绑定说明（同步：命令与按钮共用）"""
    cur = db.my_bind(uid)
    return ("🔗 **关联你的 Emby 名字**\n" + ui.divider() + "\n"
            "关联后用户榜会显示你的 **TG 头像**，名字也能点进你的个人主页。\n"
            "（`EmbyBoss` 里几乎没人登记 Emby 用户名，所以只能靠自愿关联）\n\n"
            "用法：`/bind 你的Emby用户名`\n"
            "更简单：打开用户榜，点下方 **🙋 认领我的名字**，点一下即可。\n"
            + (f"当前：**{ui.safe(cur)}**" if cur else "当前：**未关联**"))


def _resolve_tg(client_uid: int, token: str):
    """把 "@用户名" / 纯数字 TG ID 解析成 TG ID；解析不了返回 None"""
    t = str(token).strip().lstrip("@")
    if t.isdigit():
        return int(t)
    return _MEMBERS["m"].get(_key(t)) or _MEMBERS["m"].get(t.lower())


# ★ 这里原本挂的是 ["bind", "绑定"]，有两个致命问题：
#   1) 与 modules/bind.py 的「/bind 两步密码验证」**同名**，Pyrogram 下两个
#      handler 都会执行，用户发 /bind 会同时收到「请输入 Emby 用户名」和
#      「已关联 XXX」两条互相矛盾的回复；
#   2) 更严重：它**不做任何身份核验**，`/bind 别人的名字` 就能把别人的 Emby 名
#      挂到自己头上 —— 正是需求里明令禁止的「认领他人身份」。
#
#   现改为管理员专属的 /setbind（代绑）：走 perm.gate("bindctl") 鉴权，
#   且**不再提供免验证的自助绑定**——普通用户一律走 /claim，必须输该 Emby
#   账号的密码完成核验才能关联。
@bot.on_message(filters.command(["setbind", "代绑"], **CMD))
@perm.gate("bindctl")
async def cmd_setbind(client, msg: Message):
    uid = msg.from_user.id
    arg = (msg.text or "").split(maxsplit=1)
    parts = arg[1].split() if len(arg) > 1 else []

    if len(parts) < 2:
        await msg.reply(
            "🛠 <b>管理员代绑</b>\n" + ui.divider() + "\n"
            "用法：`/setbind &lt;Emby名&gt; &lt;TG ID 或 @用户名&gt;`\n"
            f"{ui.divider()}\n"
            "代绑会跳过密码核验，请只对**本人确认过身份**的群友使用。\n"
            "群友自助关联请让他们发 `/claim`（需输密码核验）。",
            parse_mode=ParseMode.HTML)
        return

    name = parts[0].strip()[:64]
    target = _resolve_tg(uid, parts[1])
    if not target:
        await msg.reply(
            f"⚠️ 认不出 **{ui.safe(parts[1])}** 是谁。\n{ui.divider()}\n"
            "请改成数字 TG ID，或确认 TA 在主群里有用户名。",
            reply_markup=ui.back_menu())
        return
    await asyncio.to_thread(db.set_bind, int(target), name)
    _TGMAP["ts"] = 0.0
    _CACHE.clear()
    await msg.reply(
        f"✅ 已把 **{ui.safe(name)}** 关联到 "
        f"[{target}](tg://user?id={target})\n{ui.divider()}\n"
        "刷新榜单后 TA 的名字就是可点的个人主页链接了。",
        reply_markup=ui.back_menu())


# ═══════════════════════════════════════════════════ 批量关联管理（管理员）
@bot.on_message(filters.command(["syncbind", "sync", "同步绑定"], **CMD))
@perm.gate("bindctl")
async def cmd_syncbind(client, msg: Message):
    """把所有 Emby 用户名拿去和群成员比对，能对上的自动关联，不用人操作"""
    tip = await msg.reply("🔄 正在比对群成员与 Emby 用户名……")
    await load_members(client)                 # 保证成员表是热的
    r = await asyncio.to_thread(auto_bind)
    lines = [f"✅ **自动关联完成**：新增 **{r['ok']}** 条"]
    if r["bound"]:
        lines.append(ui.divider())
        for n, t in r["bound"][:20]:
            lines.append(f"· **{ui.safe(n)}** → [{t}](tg://user?id={t})")
        if len(r["bound"]) > 20:
            lines.append(f"_……另有 {len(r['bound']) - 20} 条_")
    if r["conflict"]:
        lines += [ui.divider(),
                  f"⚠️ 有 {len(r['conflict'])} 个名字匹配到同一个 TG，已跳过："]
        lines.append("　" + "、".join(ui.safe(x) for x in r["conflict"][:10]))
    left = await asyncio.to_thread(unbound_names, 30)
    lines += [ui.divider(),
              f"仍未关联 **{len(left)}** 位，发 `/callbind` 可以在群里喊他们认领"]
    try:
        await tip.edit_text("\n".join(lines))
    except Exception:
        await msg.reply("\n".join(lines))


@bot.on_message(filters.command(["callbind", "call", "喊绑定"], **CMD))
@perm.gate("bindctl")
async def cmd_callbind(client, msg: Message):
    """在群里发一条公告，列出还没关联的 Emby 名字，让大家点一下认领"""
    await load_members(client)
    await asyncio.to_thread(auto_bind)          # 先自动补一遍，少喊几个人
    left = await asyncio.to_thread(unbound_names, 30)
    if not left:
        await msg.reply("🎉 所有上榜的 Emby 名字都已经关联 TG 了。")
        return
    # 让「认领」按钮有数据可用
    data = await asyncio.to_thread(user_stats, 1)
    if data:
        _PANEL.update(ts=time.time(), days=1,
                      names=[str(v["name"]) for v in data[:10]],
                      taken={str(v["name"]): bool(v.get("tg")) for v in data[:10]})
    show = left[:20]
    lines = ["📢 **还差一步：认领你的 Emby 名字**", ui.divider(),
             "榜单上这些名字还没和 TG 账号关联，所以点不开个人主页、也显示不了头像。",
             "**点下面的「🙋 认领我的名字」，再点你自己的那个名字，一秒搞定。**",
             ui.divider()]
    for i, n in enumerate(show, 1):
        lines.append(f"{i}. **{ui.safe(n)}**")
    if len(left) > len(show):
        lines.append(f"_……还有 {len(left) - len(show)} 位_")
    await msg.reply("\n".join(lines),
                    reply_markup=KM([[B("🙋 认领我的名字", "rk:claim")]]))


@bot.on_message(filters.command(["unbound", "未绑定", "没绑定"], **CMD))
@perm.gate("bindctl")
async def cmd_unbound(client, msg: Message):
    """列出还没关联 TG 的 Emby 用户名"""
    await load_members(client)
    left = await asyncio.to_thread(unbound_names, 30)
    lines = [f"📋 **未关联 TG 的 Emby 名字：{len(left)} 个**", ui.divider()]
    for i, n in enumerate(left[:40], 1):
        lines.append(f"{i}. {ui.safe(n)}")
    if len(left) > 40:
        lines.append(f"_……还有 {len(left) - 40} 个_")
    lines += [ui.divider(),
              "发 `/syncbind` 自动补关联，或 `/callbind` 在群里喊大家认领。"]
    await msg.reply("\n".join(lines))


# ═══════════════════════════════════════════════════ 实时在线播报
def _ticks_to_clock(ticks) -> str:
    """Emby 的时间单位是 100 纳秒"""
    try:
        sec = int(ticks or 0) / 10_000_000
    except Exception:
        return "00:00"
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _still(pool: list[dict]) -> bytes | None:
    """取当前播放内容的剧照，作为播报配图。

    优先 Background 大图（视觉更冲击），逐级回退：
      自己的 Backdrop → 所属剧集 Backdrop → 自己的 Poster
    """
    iid = pool[0].get("Id")
    sid = pool[0].get("SeriesId") or pool[0].get("ParentBackdropItemId")
    cands = []
    if iid:
        cands.append(("Backdrop", str(iid), 1280))
    if sid:
        cands.append(("Backdrop", str(sid), 1280))
    if iid:
        cands.append(("Primary", str(iid), 900))
    base = S.EMBY["url"].rstrip("/")
    for kind, rid, w in cands:
        try:
            r = requests.get(
                f"{base}/emby/Items/{rid}/Images/{kind}",
                params={"api_key": S.EMBY["api_key"], "maxWidth": w, "quality": 90},
                timeout=15)
            # 缺图时 Emby 会返回 404，或是极小占位图，都要过滤掉
            if r.status_code == 200 and len(r.content) > 3000 \
                    and r.content[:4] != b"<htm":
                return r.content
        except Exception as e:
            logger.debug(f"取剧照失败 {kind}/{rid}: {e}")
    return None


def _bar(p: float, n: int = 12) -> str:
    f = max(0, min(int(round(p * n)), n))
    return "█" * f + "░" * (n - f)


def _quality(item: dict) -> tuple[str, str]:
    """★ 2026-10-15 用户定稿：画质与体积**分开两行**。

    返回 (画质行, 体积行)。画质行顺序严格按 MT 命名规则：
      分辨率 → 压制/来源 → 动态范围 → 色深 → 编码 → 音频 → 码率
    Emby 的 MediaStreams 不带「压制/来源」（REMUX/WEB-DL 之类只在文件名里），
    该项按实际取不到就跳过，不编造。
    """
    vs = [m for m in (item.get("MediaStreams") or []) if m.get("Type") == "Video"]
    as_ = [m for m in (item.get("MediaStreams") or []) if m.get("Type") == "Audio"]
    v = vs[0] if vs else {}
    a = as_[0] if as_ else {}
    h = int(v.get("Height") or 0); w = int(v.get("Width") or 0)
    if h >= 2160 or w >= 3840:
        res = "4K"
    elif h >= 1440:
        res = "2K"
    elif h >= 1080:
        res = "1080p"
    elif h >= 720:
        res = "720p"
    else:
        res = f"{h}p" if h else ""
    parts = [res] if res else []

    # 动态范围：DV(带 P5/P7/P8) / HDR10+ / HDR10 / HLG / SDR——**按实际情况写清**
    vr = str(v.get("VideoRange") or v.get("VideoRangeType") or "").upper()
    drs = []
    if "DOVI" in vr or "DOLBY" in vr:
        _p = v.get("DvProfile")
        drs.append(f"杜比视界 P{int(_p)}" if _p else "杜比视界")
    if "HDR10PLUS" in vr or "HDR10+" in vr:
        drs.append("HDR10+")
    elif "HDR10" in vr:
        drs.append("HDR10")
    if "HLG" in vr:
        drs.append("HLG")
    if not drs:
        # 没有 HDR/DV → 明确写 SDR（用户：动态范围要写具体，不能空着）
        drs.append("SDR" if ("SDR" in vr or not vr) else vr)
    parts += drs

    bd = int(v.get("BitDepth") or 0)
    if bd:
        parts.append(f"{bd}bit")

    vc = str(v.get("Codec") or "").upper()
    if vc:
        parts.append(vc)

    disp = str(a.get("DisplayTitle") or "").upper()
    ac = str(a.get("Codec") or "").upper()
    ch = a.get("Channels") or 0
    if "ATMOS" in disp:
        parts.append("杜比全景声")
    elif "DTS" in disp:
        parts.append("DTS" + (f" {ch}.1" if ch and ch != 2 else ("" if ch == 2 else f" {ch}ch")))
    elif ac:
        parts.append(f"{ac} {ch}.1" if ch and ch != 2 else (f"{ac} 2.0" if ch == 2 else ac))

    src = (item.get("MediaSources") or [{}])[0]
    br = int(src.get("BitRate") or v.get("BitRate") or 0)
    if br:
        parts.append(f"{br / 1e6:.1f} Mbps")

    qual = " · ".join(p for p in parts if p)
    # ★ 体积独立一行：拿不到真实字节数也**必须显示这一行**（用户 2026-10-15
    #   两次强调「文件大小下一行分行显示」），strm 库 Emby 不回写字节数，
    #   就按文件名里的体积标识推，再不行明说未知，绝不悄悄省略整行。
    sz = int(src.get("SizeInMb") or 0)
    if sz:
        gb = sz / 1024
        size_line = (f"💾 文件大小｜**{gb:.1f} GB**" if gb >= 1
                     else f"💾 文件大小｜**{sz:.0f} MB**")
    else:
        # strm 库拿不到 SizeInMb → 用「码率 × 时长」估算
        est = 0.0
        try:
            br = int(src.get("BitRate") or v.get("BitRate")
                     or item.get("Bitrate") or 0)
            rt = float(item.get("RunTimeTicks") or 0) / 10_000_000.0
            if br > 0 and rt > 0:
                est = br / 8.0 * rt
        except Exception:
            est = 0.0
        if est > 0:
            size_line = f"\U0001F4BE 文件大小｜约 **{est / 1024 ** 3:.1f} GB**"
        else:
            size_line = "💾 文件大小｜_未知（strm 库未回写字节数）_"
    return qual, size_line


def _qual_from_path(item: dict) -> tuple[str, str]:
    """MediaStreams 拿不到画质时的兜底：从文件名解析（strm 库常见）。

    ★ 2026-10-15 实测：strm 库的 `MediaSources` 是**空列表**（拿不到 Path 也
      拿不到 SizeInMb），但 `item.Path` 有完整文件名（含 2160p/WEB-DL/HDR10/
      10-bit/H265/码率），所以兜底必须读 item.Path。
    返回 (画质行, 体积行)；解析不到返回 ("", "")。
    """
    path = str(item.get("Path") or "")
    if not path:
        return "", ""
    try:
        from .request import _ver_profile
        v = _ver_profile([path])
        if not v.get("res") and not v.get("quals"):
            return "", ""
        from .request import _qual_line as _ql
        q = _ql(v)
        # 文件名里带体积标识（如 12.5GB / 4.7G）也一并提出来
        import re as _re
        m = (_re.search(r"(\d+(?:\.\d+)?)\s*GB", path, _re.I)
             or _re.search(r"(\d+(?:\.\d+)?)\s*G\b", path))
        sz = ""
        if m:
            sz = "💾 文件大小｜**%.1f GB**（文件名标识）" % float(m.group(1))
        return q, sz
    except Exception as e:
        logger.debug(f"文件名解析画质失败 {path[:40]}: {e}")
        return "", ""


_TGUSER_CACHE: dict[int, tuple[float, dict]] = {}
_MAP_SYNC_TS = 0.0


def tg_user_of(uid) -> dict:
    # TG UID -> 用户名/昵称（Bot API getChat，走代理，缓存10分钟）
    try:
        uid = int(uid or 0)
    except Exception:
        return {}
    if uid <= 0:
        return {}
    now = time.time()
    hit = _TGUSER_CACHE.get(uid)
    if hit and now - hit[0] < 600:
        return hit[1]
    out = {"username": "", "first_name": "", "ok": False}
    try:
        from ..settings import BOT_TOKEN, PROXY
        import requests
        px = None
        if PROXY:
            h = "%s://%s:%s" % (PROXY.get("scheme") or "http",
                                PROXY.get("hostname"), PROXY.get("port"))
            px = {"http": h, "https": h}
        r = requests.get("https://api.telegram.org/bot%s/getChat" % BOT_TOKEN,
                         params={"chat_id": uid}, proxies=px, timeout=20)
        d = r.json()
        res = d.get("result") or {}
        if d.get("ok") and res:
            out = {"username": str(res.get("username") or ""),
                   "first_name": str(res.get("first_name") or ""),
                   "ok": True}
    except Exception as e:
        logger.debug(f"TG 用户反查失败 {uid}: {e}")
    _TGUSER_CACHE[uid] = (now, out)
    return out

def sync_emby_tg_map(force: bool = False) -> int:
    """把 Emby 用户名 -> TG(UID/昵称/username/可点链接) 的映射写库
    （kfcgame.kfc_emby_tg_map），供 Web 仪表盘直接读（media-hub 无 TG token）。

    数据源：tg_map()（Emby 名 -> TG UID，来自群友 /bind + boss.emby）
            + tg_user_of()（UID -> TG 用户名，Bot API getChat）
    10 分钟同步一次（幂等，UPSERT）。返回写入条数。
    """
    import time as _t
    global _MAP_SYNC_TS
    now = _t.time()
    if not force and now - _MAP_SYNC_TS < 600:
        return 0
    _MAP_SYNC_TS = now
    try:
        tmap = tg_map()
    except Exception as e:
        logger.debug(f"sync_emby_tg_map 取映射失败: {e}")
        return 0
    n = 0
    try:
        from sqlalchemy import text as _text
        with db._engine.begin() as c:
            for nm, tg in (tmap or {}).items():
                try:
                    tg = int(tg or 0)
                except Exception:
                    continue
                if not nm or tg <= 0:
                    continue
                tu = tg_user_of(tg)
                un = (tu.get("username") or "").lstrip("@")
                _fn = "".join(ch for ch in str(tu.get("first_name") or "")
                              if ch.isprintable()).strip()
                url = ("https://t.me/" + un) if un else ("tg://user?id=%d" % tg)
                c.execute(_text(
                    "INSERT INTO kfc_emby_tg_map "
                    "(emby_name, tg, tg_name, tg_username, tg_url) "
                    "VALUES (:n,:t,:nm,:un,:u) "
                    "ON DUPLICATE KEY UPDATE tg=VALUES(tg), tg_name=VALUES(tg_name), "
                    "tg_username=VALUES(tg_username), tg_url=VALUES(tg_url)"),
                    {"n": str(nm)[:64], "t": tg, "nm": _fn[:64],
                     "un": un[:64], "u": url[:128]})
                n += 1
    except Exception as e:
        logger.debug(f"sync_emby_tg_map 写库失败: {e}")
        return 0
    if n:
        logger.info(f"Emby->TG 映射已同步 {n} 条到 kfc_emby_tg_map")
    return n


def _who_lines(raw_name: str, tg) -> list[str]:
    # ★ 2026-10-16b 修正：链接改用 tg://user?id=<TG数字ID>（mention 实体），
    #   点开是<b>个人主页卡片</b>（资料页弹窗，不进聊天框）；
    #   https://t.me/username（text_url）在 TG 客户端会直接进聊天界面，故弃用。
    #   没有 TG 数字 ID 的用户不给链接。
    nm = ui.safe(raw_name)
    try:
        tg = int(tg) if tg else 0
    except Exception:
        tg = 0
    if tg <= 0:
        return ["**\U0001F464 \u7528\u6237\u540D\uFF1A** **" + nm + "**",
                "**\u2502\u3000UID\uFF1A** **\u672A\u5173\u8054 TG\uFF08\u8BE5 Emby \u53F7\u672A\u7ED1\u5B9A/\u672A\u5F00\u53F7\uFF09**",
                "**\u2502\u3000Emby\u7528\u6237\u540D\uFF1A** **" + nm + "**"]
    tu = tg_user_of(tg)
    un = (tu.get("username") or "").lstrip("@")
    _fn = "".join(ch for ch in str(tu.get("first_name") or "")
                      if ch.isprintable()).strip()
    disp = ui.safe(_fn) or nm
    url = "tg://user?id=" + str(tg)
    if not un:
        return ["**\U0001F464 \u7528\u6237\u540D\uFF1a** [" + disp + "](" + url + ")",
                "**\u2502\u3000UID\uFF1a** [" + str(tg) + "](" + url + ")",
                "**\u2502\u3000Emby\u7528\u6237\u540D\uFF1a** [" + nm + "](" + url + ")"]
    # ★ 2026-10-16e：链接文字不再套粗体。mention + bold 叠在同一段时，
    #   TG 客户端会把链接吞掉（实测只有部分行点得开），故只发 mention。
    return ["**\U0001F464 \u7528\u6237\u540D\uFF1a** [" + disp + "](" + url + ")",
            "**\u2502\u3000UID\uFF1a** [" + str(tg) + "](" + url + ")",
            "**\u2502\u3000Emby\u7528\u6237\u540D\uFF1a** [" + nm + "](" + url + ")"]


def _episode_title(item: dict) -> tuple[str, str | None]:
    """返回 (展示标题, 上级剧集名)"""
    ser = item.get("SeriesName")
    if item.get("Type") == "Episode" and ser:
        se, ep = item.get("ParentIndexNumber"), item.get("IndexNumber")
        tag = f" S{se:02d}E{ep:02d}" if se and ep else ""
        return (f"{ser}{tag} · {item.get('Name') or ''}", ser)
    return (item.get("Name") or "未知", ser)


def _today_played(limit: int = 8) -> list[str]:
    """今日播放记录（没人观看时 /now 也给出）。数据源：Emby 各用户最近播放。
    返回渲染好的行列表。"""
    out = []
    try:
        users = _api("/emby/Users") or []
        tmap = tg_map()
        rows = []
        # 只查最近活跃的前 15 个用户（78 个全查会超时）
        for u in (users if isinstance(users, list) else [])[:15]:
            uid = u.get("Id")
            if not uid:
                continue
            d = _api("/emby/Items?UserId=%s&Filters=IsPlayed&SortBy=DatePlayed"
                    "&SortOrder=Descending&Recursive=true&Limit=3"
                    "&Fields=UserData,DatePlayed" % uid)
            items = (d or {}).get("Items") or []
            for it in items[:2]:
                ud = it.get("UserData") or {}
                lp = str(ud.get("LastPlayedDate") or "")
                if lp[:10] != time.strftime("%Y-%m-%d"):
                    continue
                rows.append((lp, str(u.get("Name") or "?"),
                             str(it.get("SeriesName") or it.get("Name") or ""),
                             int(ud.get("PlayCount") or 0)))
        rows.sort(key=lambda x: x[0], reverse=True)
        for lp, un, tt, pc in rows[:limit]:
            out.append("**%s** %s（%s 次）" % (ui.safe(un), ui.safe(tt)[:26], pc))
    except Exception as e:
        logger.debug(f"_today_played 失败: {e}")
    return out


def _now_data(page: int = 0, per: int = 2) -> tuple[tuple[str, str], bytes | None]:
    """返回 ((首条文字, 其余文字), 配图)。page/per 支持翻页。"""
    sessions = _api("/emby/Sessions") or []
    if not isinstance(sessions, list):
        sessions = []
    umap = user_map()
    tmap = tg_map()
    online, pool = set(), []
    for s in sessions:
        uid = s.get("UserId")
        if uid:
            online.add(str(uid))
        if s.get("NowPlayingItem"):
            pool.append(s)

    page = max(0, int(page or 0))
    per = max(1, int(per or 2))
    pages = max(1, (len(pool) + per - 1) // per)
    page = min(page, pages - 1)
    pg_txt = f"│　📄 第 **{page + 1}/{pages}** 页（共 {len(pool)} 个播放会话）"

    lines = [
        "🎬 **Emby 在线播报**",
        ui.divider(),
        f"👥 在线人数｜**{len(online)}** 人　　▶️ 播放会话｜**{len(pool)}** 个",
        pg_txt,
        f"🕒 播报时间｜`{time.strftime('%Y-%m-%d %H:%M:%S')}`",
    ]
    if not pool:
        lines += [ui.divider(), "_当前没有人在观看_ 🌙", ""]
        # 没人看也给今日播放记录（用户 2026-10-16 要求）
        try:
            rec = _today_played()
            if rec:
                lines.append("📺 **今日播放记录**（最近 %d 条）" % len(rec))
                for r in rec:
                    lines.append("│　" + r)
        except Exception:
            pass
        return ("\n".join(lines), "", 1), None

    # ★ 2026-10-15 用户明确要求：**不管几个会话都用完整竖线排版**（三行身份卡
    #   + 完整画质 + 体积独立行），不接受紧凑版。受 Telegram 图片说明 1024 字
    #   限制：带图时 caption 放「头部 + 第 1 个会话」，其余会话走紧随其后的
    #   纯文字消息（4096 上限，可容纳 6+ 个会话）。
    page = max(0, int(page or 0))
    per = max(1, int(per or 2))
    pages = max(1, (len(pool) + per - 1) // per)
    page = min(page, pages - 1)
    show = pool[page * per:(page + 1) * per]
    pg_txt = f"│　📄 第 **{page + 1}/{pages}** 页（共 {len(pool)} 个播放会话）"
    lines.append(ui.divider())
    for s in show:
        item = s.get("NowPlayingItem") or {}
        st = s.get("PlayState") or {}
        raw_name = umap.get(str(s.get("UserId")), "未知")
        # ★ 先查自愿绑定表，再退回群成员自动比对 —— 后者能救回一部分没绑过的人
        tg = tmap.get(str(raw_name))
        if not tg:
            tg = match_tg(str(raw_name))
        who_lines = _who_lines(raw_name, tg)

        title, _ser = _episode_title(item)
        if item.get("ProductionYear"):
            title = f"{title}（{item['ProductionYear']}）"
        iid = str(item.get("Id") or "")
        meta = _batch_meta([iid]).get(iid, {}) if iid else {}
        title = (f"[{ui.safe(title)}]({meta['url']})" if meta.get("url")
                 else ui.safe(title))

        pos = _ticks_to_clock(st.get("PositionTicks"))
        dur = _ticks_to_clock(item.get("RunTimeTicks"))
        try:
            p = int(st.get("PositionTicks") or 0) / max(
                int(item.get("RunTimeTicks") or 1), 1)
            p = max(min(p, 1.0), 0.0)
        except Exception:
            p = 0.0
        rem_ticks = max(int(item.get("RunTimeTicks") or 0)
                        - int(st.get("PositionTicks") or 0), 0)
        rem_clk = _ticks_to_clock(rem_ticks)
        icon, state = ("⏸", "暂停中") if st.get("IsPaused") else ("▶️", "播放中")
        app = s.get("Client") or "未知客户端"
        dev = s.get("DeviceName") or "未知设备"
        qual, size_line = _quality(item)
        # ★ 2026-10-15：strm 库 MediaSources 为空 → MediaStreams 往往也缺
        #   Width/Codec，画质会只剩「SDR」、体积「未知」。用 item.Path 的
        #   文件名兜底补全（实测能补出 4K · WEB-DL · H265 · 12.5Mbps）。
        need_fb = (not qual or qual == "SDR" or qual == "未知"
                   or "未知" in size_line)
        if need_fb:
            q2, s2 = _qual_from_path(item)
            if q2:
                qual = q2 if (not qual or qual in ("SDR", "未知")) else (qual + " · " + q2)
            if s2 and "未知" in size_line:
                size_line = s2
        bar = _bar(p, 14)
        pct = f"{p * 100:.0f}%"

        # 完整竖线排版（每个字段独占一行）
        lines += who_lines
        lines.append(f"│　📶 {icon} {state}")
        lines.append(f"│　🎞 {qual}" if qual else "│　🎞 _画质未知_")
        if size_line:
            lines.append(f"│　{size_line}")     # ★ 体积独立一行
        lines.append(f"│　🎬 {title}")
        lines.append(f"│　⏳ {pos} / {dur}（剩 {rem_clk}）　{bar} {pct}")
        lines.append(f"│　📱 {ui.safe(app)} · {ui.safe(dev)}")
    if pages > 1:
        lines.append("_用下方按钮翻页查看其余页_")
    # 带图时 caption 只能放 ~1000 字：头部 + 第 1 个会话；其余走第二条文字消息
    first_user = next((i for i, ln in enumerate(lines)
                       if ln.startswith("👤 用户名")), -1)
    second_user = next((i for i, ln in enumerate(lines)
                        if i > first_user >= 0 and ln.startswith("👤 用户名")), -1)
    still = _still([show[0].get("NowPlayingItem") or {}]) if show else None
    if second_user > 0:
        cap = "\n".join(lines[:second_user])
        rest = "\n".join(lines[second_user:])
        return (cap, rest, pages), still
    return ("\n".join(lines), "", pages), still


def _now_text() -> str:
    r = _now_data()[0]
    return r[0] if isinstance(r, tuple) else r


_NOW_PG: dict[int, int] = {}


def _now_kb(page: int = 0, pages: int = 1) -> KM:
    nav = []
    if page > 0:
        nav.append(B("\u25C0\uFE0F 上一页", f"now:pg:{page - 1}"))
    nav.append(B("\U0001F504 刷新", "now:refresh"))
    if page < pages - 1:
        nav.append(B("下一页 \u25B6\uFE0F", f"now:pg:{page + 1}"))
    return KM([nav] if nav else [])



@bot.on_message(filters.command(["now", "online", "在线", "zb"], **CMD))
async def cmd_now(client, msg: Message):
    await asyncio.to_thread(sync_emby_tg_map)
    packed, png = await asyncio.to_thread(_now_data)
    text, rest, pages = (packed if isinstance(packed, tuple) and len(packed) == 3
                        else (packed[0], packed[1] if isinstance(packed, tuple) else "", 1))
    kb = _now_kb(0, pages)
    if png:                                    # 带剧照发出去，取不到就退回文字
        try:
            await msg.reply_photo(io.BytesIO(png), caption=text[:1000],
                                  reply_markup=kb)
            if rest:                           # 其余会话走紧随的纯文字消息
                await msg.reply(rest, reply_markup=kb,
                                disable_web_page_preview=True)
            return
        except Exception as e:
            logger.warning(f"发送在线播报剧照失败，退回文字: {e}")
    await msg.reply(text + (("\n" + rest) if rest else ""), reply_markup=kb)


@bot.on_callback_query(filters.regex(r"^now:refresh$"))
@bot.on_callback_query(filters.regex(r"^now:pg:(\d+)$"))
async def cb_now_pg(client, cb):
    try:
        pg = int(cb.matches[0].group(1))
    except Exception:
        pg = 0
    _NOW_PG[cb.message.chat.id] = pg
    packed, png = await asyncio.to_thread(_now_data, pg)
    text, rest, pages = (packed if isinstance(packed, tuple) and len(packed) == 3
                        else (packed[0], "", 1))
    pg = min(pg, pages - 1)
    kb = _now_kb(pg, pages)
    full = text + (("\n" + rest) if rest else "")
    try:
        if png:
            await cb.message.edit_media(
                InputMediaPhoto(io.BytesIO(png), caption=text[:1000]),
                reply_markup=kb)
        else:
            await cb.message.edit_text(full, reply_markup=kb,
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"翻页刷新失败: {e}")
    try:
        await cb.answer()
    except Exception:
        pass

async def cb_now(client, cb):
    pg = _NOW_PG.get(cb.message.chat.id, 0)
    packed, png = await asyncio.to_thread(_now_data, pg)
    text, rest, pages = (packed if isinstance(packed, tuple) and len(packed) == 3
                        else (packed[0], packed[1] if isinstance(packed, tuple) else "", 1))
    pg = min(pg, pages - 1)
    _NOW_PG[cb.message.chat.id] = pg
    kb = _now_kb(pg, pages)
    if png:
        try:
            await cb.message.edit_media(
                InputMediaPhoto(io.BytesIO(png), caption=text[:1000]),
                reply_markup=kb)
            await cb.answer("已刷新")
            return
        except Exception as e:
            logger.debug(f"刷新播报剧照失败，退回文字: {e}")
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        logger.debug(f"刷新在线播报失败: {e}")
    await cb.answer("已刷新")
