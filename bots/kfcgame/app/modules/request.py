"""求片一条龙 v4

流程：
    触发（/qp 片名 · /qp 后直接打字 · 按钮）
      → 一张「总览拼图」把所有候选海报合成一张，下面列出全部候选按钮
      → 点某个候选 → 原地换成该片的大图 + 详细介绍
      → 电视剧先选季（全季全集 / 第 N 季），再选集（全集 / 第 N 集 / 直接输集数）
      → 确认页（显示将要产生多少下载任务）→ ✅确认 / ↩️返回 / ❌取消
      → **提交给管理员审批**，通过后 MoviePilot 才真正动手

设计要点：
  · 全程只占用「一条消息」，用 edit_media 原地换图，不刷屏
  · 所有播报里的用户名都是 tg://user?id= 的真实超链接，点一下跳主页
  · 文案用中文标签 + 全角排版，对齐整齐
"""
from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from pyrogram import ContinuePropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import InputMediaPhoto
from pyrogram.types import Message

# ⚠️ 全局客户端是 ParseMode.MARKDOWN，但本模块文案一律用 HTML 标签（<b>/<a href>），
#    所以每次发送都必须显式带上 parse_mode=PM，否则标签会原样裸露给用户。
#    HTML 相对 Markdown 更稳：只需转义 & < > 三个字符，
#    不像 Markdown 会被用户昵称里的 * _ [ ] 打断导致整条消息报 Can't parse entities。
PM = ParseMode.HTML

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from . import perm

CMD = dict(prefixes=S.PREFIX)

_tok: dict = {"v": None, "ts": 0.0}

# uid -> 会话：{"ts","keyword","results","sea","chat","mid"}
_QP_CACHE: dict[int, dict] = {}
_QP_TTL = 1800                       # 候选 30 分钟内有效

# uid -> 等待直接输入的文本状态
#   "kw"                     等待片名
#   ("epnum", idx, sea)      等待集数
#   ("seapage", idx)         等待季页码
#   ("eppage", idx, sea)     等待集页码
_WAIT: dict[int, Any] = {}
_WAIT_TTL = 900

_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_TTL = 300

_IMG_CACHE: dict[str, tuple[float, bytes]] = {}
_IMG_TTL = 1800
_IMG_MAX = 160

_FONT = "/app/fonts/PingFangBold.ttf"

# MySQL 里存的是 UTC，容器本地是北京时间 —— 展示前统一换算，否则两处时间对不上
_CN = timezone(timedelta(hours=8))


def _cn_now() -> str:
    return datetime.now(_CN).strftime("%Y-%m-%d %H:%M:%S")


def _cn_dt(dt) -> str:
    if dt is None:
        return ""
    try:
        if getattr(dt, "tzinfo", None):
            return dt.astimezone(_CN).strftime("%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)[:19]


def is_admin(uid) -> bool:
    """管理员判定（走 perm：owner + kfc_admin 表里的授权管理员）"""
    try:
        from . import perm
        return perm.level_of(int(uid)) >= perm.ADMIN
    except Exception:
        try:
            return int(uid) == int(S.OWNER or 0)
        except Exception:
            return False


def admin_ids() -> list[int]:
    out = []
    try:
        from .. import db
        extra = [int(a["tg"]) for a in db.admins()]
    except Exception:
        extra = []
    for i in [S.OWNER] + list(S.ADMINS or []) + extra:
        try:
            i = int(i)
        except Exception:
            continue
        if i and i not in out:
            out.append(i)
    return out

# TMDB 类型标签（电影 + 电视剧合并表）
_GENRES = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪", 99: "纪录",
    18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史", 27: "恐怖",
    10402: "音乐", 9648: "悬疑", 10749: "爱情", 878: "科幻",
    10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部",
    10759: "动作冒险", 10762: "儿童", 10763: "新闻", 10764: "真人秀",
    10765: "科幻奇幻", 10766: "肥皂剧", 10767: "脱口秀", 10768: "战争政治",
}


# ═══════════════════════════════════════════════ 文本工具（HTML）
def _esc(s) -> str:
    """HTML 实体转义：用户名/片名是用户可控内容，不转义会破坏整条消息"""
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _t(s, n: int = 60) -> str:
    """取一段安全文本（转义 + 截断）"""
    return _esc(str(s or "").strip())[:n]


def _ulink(uid, name: str = "", username: str = "") -> str:
    """可点击的 Telegram 用户主页链接

    ★ TG 用户名前面**不带 @**（用户 2026-10-11 要求）——直接是超链接文本。
    """
    try:
        uid = int(uid)
    except Exception:
        return _esc(name or "某人")
    label = (username or name or f"用户{uid}")
    return f'<a href="tg://user?id={uid}">{_esc(label)}</a>'


def _ucard_one(uid, name: str = "", username: str = "", label: str = "") -> str:
    """身份展示的**单行**版本（列表/行内拼接用）：只出「👤 [名字](主页)」，
    三行标准卡（用户名/UID/Emby）用 _ucard。改名实时性由调用方传最新 name。"""
    u = str(username or "").lstrip("@").strip()
    nm = _t(name or u or "未知用户", 14).strip()
    try:
        import re as _re
        nm = _re.sub(r"[\x00-\x1f\x7f]", "", nm)
    except Exception:
        pass
    lbl = (label + "｜") if label else ""
    if uid:
        return f'👤 {lbl}<a href="tg://user?id={int(uid)}">{nm}</a>'
    return f"👤 {nm or '未知'}"


def _ucard(uid, name: str = "", username: str = "", label: str = "",
           icon: str = "👤", indent: str = "　　") -> list[str]:
    """统一身份展示：**三行标准卡**（用户名/UID/Emby名，全粗体、全超链到
    tg://user?id=UID）——求片人/审核人/开号等所有个人相关场景都走这里。

    ★ 2026-10-15 用户要求：求片人「只有 UID 没有用户名」必须解决。
      名字为空时按 kfc_members → boss.emby.name（RT 开号带过来的）兜底解析，
      仍解析不到才显示「TG 用户+UID 后4位」，绝不裸奔一个未知用户。
    """
    try:
        emby = ui.emby_name_of(uid)
    except Exception:
        emby = None
    nm = str(name or "").strip() or _resolve_name(int(uid or 0))
    un = str(username or "").strip() or _resolve_username(int(uid or 0))
    return ui.user_card(uid, nm, un, emby or "", label=label,
                        icon=icon, mode="html", indent=indent)


_NAME_CACHE: dict[int, tuple[float, str, str]] = {}


def _resolve_name(uid: int) -> str:
    """UID → 实时用户名（带 60s 缓存）。多级兜底：
    kfc_members.tg_name → kfc_members.tg_username → boss.emby.name →
    「TG 用户+后4位」。永远不返回空。"""
    if uid <= 0:
        return ""
    c = _NAME_CACHE.get(uid)
    if c and time.time() - c[0] < 60:
        return c[1]
    nm = ""
    try:
        from . import unibot
        row = unibot.member_of(uid)
        if row:
            nm = str(row.get("tg_name") or "").strip()
            if not nm:
                nm = str(row.get("tg_username") or "").strip()
        if not nm:
            acc = unibot.emby_account_of(uid) or {}
            nm = str(acc.get("name") or "").strip()
    except Exception as e:
        logger.debug(f"名字解析失败 {uid}: {e}")
    if not nm:
        nm = f"TG 用户{str(uid)[-4:]}"
    _NAME_CACHE[uid] = (time.time(), nm, "")
    return nm


def _resolve_username(uid: int) -> str:
    if uid <= 0:
        return ""
    c = _NAME_CACHE.get(uid)
    if c and time.time() - c[0] < 60:
        return c[2]
    un = ""
    try:
        from . import unibot
        row = unibot.member_of(uid)
        if row:
            un = str(row.get("tg_username") or "").strip()
    except Exception:
        pass
    _NAME_CACHE.setdefault(uid, (time.time(), _resolve_name(uid), un))
    return un


def _mention(uid, name: str = "", username: str = "") -> str:
    """@提及某人（2026-10-13 用户要求：通知里显示 TG 用户名并超链到主页）。

    · 有 TG 用户名 → <b>@用户名</b>，链到 https://t.me/用户名（群里会真 @ 到本人）
    · 没用户名      → 名字链到 tg://user?id=UID（点一下打开他的主页）
    """
    u = str(username or "").lstrip("@").strip()
    if u:
        return f'<a href="https://t.me/{_esc(u)}">@{_esc(u)}</a>'
    nm = _t(name or "", 16).strip() or (f"UID{uid}" if uid else "—")
    if uid:
        return f'<a href="tg://user?id={int(uid)}">{nm}</a>'
    return nm


def _tlink(title, tid="", mtype: str = "", n: int = 0) -> str:
    """片名 → 可点 TMDB 超链接（本模块走 HTML）"""
    return ui.title_link(title, tid, mtype, n, mode="html")


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


# ═══════════════════════════════════════════════ MoviePilot
def _base() -> str:
    return S.MP["url"].rstrip("/")


def mp_login(force: bool = False) -> str | None:
    if not S.MP.get("enable"):
        return None
    if not force and _tok["v"] and time.time() - _tok["ts"] < 1500:
        return _tok["v"]
    try:
        r = requests.post(
            f"{_base()}/api/v1/login/access-token",
            data={"username": S.MP["username"], "password": S.MP["password"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=12,
        )
        token = r.json().get("access_token")
        if token:
            _tok.update(v=token, ts=time.time())
            return token
        logger.warning(f"MoviePilot 登录失败: {r.text[:120]}")
    except Exception as e:
        logger.warning(f"MoviePilot 连接异常: {e}")
    return None


def _req(method: str, path: str, **kw) -> Any:
    tok = mp_login()
    if not tok:
        return None
    kw.setdefault("timeout", 15)
    headers = {"Authorization": f"Bearer {tok}"}
    try:
        r = requests.request(method, f"{_base()}{path}", headers=headers, **kw)
        if r.status_code == 401:
            tok = mp_login(force=True)
            if not tok:
                return None
            headers["Authorization"] = f"Bearer {tok}"
            r = requests.request(method, f"{_base()}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            logger.warning(f"MP {method} {path} -> {r.status_code} {r.text[:160]}")
            return {"_err": f"{r.status_code} {r.text[:160]}"}
        return r.json()
    except Exception as e:
        logger.warning(f"MP {method} {path} 异常: {e}")
        return None


def mp_search(keyword: str, limit: int = 9, use_cache: bool = True) -> list[dict]:
    now = time.time()
    if use_cache:
        c = _SEARCH_CACHE.get(keyword)
        if c and now - c[0] < _SEARCH_TTL:
            return c[1]
    j = _req("GET", "/api/v1/media/search", params={"title": keyword})
    if not isinstance(j, dict) or not j.get("success"):
        return []
    out = []
    for it in (j.get("data") or [])[:limit]:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        gids = it.get("genre_ids") or []
        genres = " / ".join(_GENRES.get(int(g), "") for g in gids
                            if int(g or 0) in _GENRES)[:200]
        out.append({
            "title": title,
            "year": str(it.get("year") or ""),
            "media_id": str(it.get("tmdb_id") or it.get("media_id") or ""),
            "media_source": it.get("media_source") or "themoviedb",
            "type": it.get("type") or "电影",
            "poster": _poster(it.get("poster_path")),
            "backdrop": _poster(it.get("backdrop_path"), "w780"),
            "overview": (it.get("overview") or "").strip().replace("\n", " "),
            "vote": it.get("vote_average") or 0,
            "release": it.get("release_date") or "",
            "original": (it.get("original_title") or "").strip(),
            "link": it.get("detail_link") or "",
            "genres": genres,
            "genre_ids": [int(g or 0) for g in gids],
        })
    _SEARCH_CACHE[keyword] = (now, out)
    return out


def mp_seasons(media_id: str, media_source: str = "themoviedb") -> list[dict]:
    if not media_id:
        return []
    j = _req("GET", "/api/v1/media/seasons",
             params={"media_source": media_source, "media_id": media_id})
    if not isinstance(j, dict) or not j.get("success"):
        return []
    out = []
    for s in (j.get("data") or []):
        if not isinstance(s, dict):
            continue
        out.append({
            "season_number": int(s.get("season_number") or 0),
            "name": (s.get("name") or "").strip(),
            "episode_count": int(s.get("episode_count") or 0),
            "air_date": s.get("air_date") or "",
        })
    out.sort(key=lambda x: (x["season_number"] == 0, x["season_number"]))
    return out


_RECOG_CACHE: dict[str, tuple[float, dict]] = {}
_RECOG_TTL = 1800
_MP_DOWN_UNTIL = 0.0   # ★ 熔断：MP 连接失败后 180s 内 mp_recognize 直接秒回 {}，不再打 MP


def mp_recognize(name: str) -> dict:
    """种子名 / 文件名 -> {title, year, tmdb_id, type, lang, country, poster}；识别失败返回 {}

    ⚠️ requests 会自己做 URL 编码，这里千万别再 quote 一次，
       否则种子名里的空格/点被二次转义，MP 什么都识别不出来
    ★ lang / country 是 Emby 地区细分的权威判据：英文种子名（Dune/Avengers）
      本身不含地区词，只看种子名永远判不出「欧美电影 / 华语电影」这类细分。
      MP 的 media_info 里本来就有 original_language(如 en/ja) 与 origin_country(['US'])。
    """
    global _MP_DOWN_UNTIL
    key = str(name or "").strip()[:160]
    if not key:
        return {}
    hit = _RECOG_CACHE.get(key)
    if hit and time.time() - hit[0] < _RECOG_TTL:
        return hit[1]
    if time.time() < _MP_DOWN_UNTIL:      # ★ 熔断中：MP 正不可用，秒回空，绝不卡线程/事件循环
        return {}
    out: dict = {}
    try:
        j = _req("GET", "/api/v1/media/recognize", params={"title": key})
        if j is None:                     # ★ _req 吞异常返回 None = MP 连不上 → 立刻熔断
            _MP_DOWN_UNTIL = time.time() + 180
            logger.warning("MP 识别不可用(超时/登录失败)：熔断 180s，期间识别秒回空")
        else:
            _MP_DOWN_UNTIL = 0.0          # MP 正常应答（含 4xx），解除熔断
        data = j.get("data") if isinstance(j, dict) else None
        mi = (data or {}).get("media_info") if isinstance(data, dict) else None
        if isinstance(mi, dict) and mi:
            lang = str(mi.get("original_language") or "").strip().lower()
            oc = mi.get("origin_country") or []
            country = str(oc[0]).strip().upper() if isinstance(oc, list) and oc else ""
            out = {
                "title": str(mi.get("title") or "").strip(),
                "year": str(mi.get("year") or ""),
                "tmdb_id": str(mi.get("tmdb_id") or ""),
                "type": mi.get("type") or "",
                "lang": lang,
                "country": country,
                "poster": _poster(mi.get("poster_path"), "w342"),
            }
    except Exception as e:
        _MP_DOWN_UNTIL = time.time() + 180   # ★ MP 不可用：熔断 3 分钟，期间所有识别秒回 {}
        logger.warning(f"MP 识别失败(触发熔断180s) {key[:40]}: {e}")
    _RECOG_CACHE[key] = (time.time(), out)
    return out


def mp_sub_url(u, size: str = "w342") -> str:
    """把 MP / TMDB 给的图片字段兜底成可直接下载的地址"""
    s = str(u or "").strip()
    if not s:
        return ""
    if s.startswith("http"):
        return re.sub(r"/t/p/[^/]+/", f"/t/p/{size}/", s)
    return f"https://image.tmdb.org/t/p/{size}/{s.lstrip('/')}"


def mp_kick_search(sub_id: int) -> bool:
    if not sub_id:
        return False
    j = _req("POST", f"/api/v1/subscribe/search/{int(sub_id)}")
    ok = isinstance(j, dict) and j.get("success")
    logger.info(f"订阅 {sub_id} 立即搜索：{'已触发' if ok else j}")
    return bool(ok)


def mp_subscribe(item: dict, season: int | None = None,
                 start_ep: int | None = None,
                 total_ep: int | None = None) -> tuple[bool, str, int]:
    """提交订阅。

    ★ 关键修复：以前「整季」只传 season 不传集数范围，MoviePilot 会自动按
      TMDB 的总集数把整季每一集都建下载任务，点一下冒出几十个任务。
      现在集数范围一律显式给出：
        单集   -> start_episode = total_episode = N       （只下这一集）
        整季   -> start_episode = 1, total_episode = 本季集数
        全季全集 -> 不传（MP 按整部剧追）但会先给用户看预计集数
    """
    payload = {
        "name": item["title"],
        "year": item["year"],
        "type": item["type"],
        "media_source": item["media_source"],
        "media_id": item["media_id"],
        # ★ 2026-10-13 用户要求：MP 聚合所有站点搜到的资源里，
        #   **优先挑体积最大 / 质量最好的版本**（best_version=洗版模式）。
        "best_version": True,
    }
    if season:
        payload["season"] = int(season)
    if start_ep:
        payload["start_episode"] = int(start_ep)
        payload["total_episode"] = int(total_ep or start_ep)
    elif season and total_ep:
        payload["start_episode"] = 1
        payload["total_episode"] = int(total_ep)

    j = _req("POST", "/api/v1/subscribe/", json=payload)
    if isinstance(j, dict) and j.get("success"):
        return True, "已加入订阅", _sub_id(j)
    if j is None:
        return False, "MoviePilot 未响应", 0
    msg = ""
    if isinstance(j, dict):
        msg = j.get("message") or str(j.get("_err") or "")
    if "已存在" in msg or "exist" in msg.lower():
        return True, "该片已在订阅中", 0
    return False, (msg or "提交失败")[:80], 0


def _sub_id(j: dict) -> int:
    d = j.get("data")
    if isinstance(d, dict):
        try:
            return int(d.get("id") or 0)
        except Exception:
            return 0
    if isinstance(d, int):
        return d
    return 0


def mp_subscribe_list(limit: int = 30) -> list[dict]:
    """订阅列表。

    ⚠️ 坑：/api/v1/subscribe/list 只认 API_TOKEN（?token=），用 Bearer 会 401
    「token 校验不通过」；要用 Bearer 就打 /api/v1/subscribe/ （带 page/count）。
    """
    j = _req("GET", "/api/v1/subscribe/",
             params={"page": 1, "count": int(limit)})
    if not isinstance(j, dict):
        return []
    return [x for x in (j.get("data") or []) if isinstance(x, dict)]


def mp_subscribe_del(sub_id: int) -> bool:
    j = _req("DELETE", f"/api/v1/subscribe/{int(sub_id)}")
    return bool(isinstance(j, dict) and j.get("success"))


def mp_sub_state(sub_id: int, state: str) -> tuple[bool, str]:
    """改 MoviePilot 订阅状态。

    ★ 实测（2026-10-12）：MP 没有 pause/resume 路由，
      正确写法是 <b>PUT /api/v1/subscribe/status/{id}?state=</b>：
        state='P' → 暂停订阅（不再自动搜索下载）
        state='R' → 恢复订阅（重新开始追更）
      用错方法会得到 405 Method Not Allowed。
    """
    st = str(state or "").upper()
    if st not in ("P", "R"):
        return False, "状态只能是 P(暂停) / R(恢复)"
    j = _req("PUT", f"/api/v1/subscribe/status/{int(sub_id)}", params={"state": st})
    if isinstance(j, dict) and j.get("success"):
        return True, "已暂停" if st == "P" else "已恢复"
    msg = ""
    if isinstance(j, dict):
        msg = j.get("message") or str(j.get("_err") or "")
    return False, (msg or "操作失败")[:60]


def mp_sub_pause(sub_id: int) -> tuple[bool, str]:
    return mp_sub_state(sub_id, "P")


def mp_sub_resume(sub_id: int) -> tuple[bool, str]:
    return mp_sub_state(sub_id, "R")


def find_sub(name: str, season: int = 0) -> dict | None:
    """按片名 + 季号在现存 MP 订阅里找一条（查漏补缺的暂停/恢复要用）"""
    nm = str(name or "").strip()
    if not nm:
        return None
    try:
        subs = mp_subscribe_list(200)
    except Exception:
        return None
    for s in subs:
        sn = str(s.get("name") or "").strip()
        if not sn:
            continue
        if not (nm[:6] in sn or sn[:6] in nm):
            continue
        if season and int(s.get("season") or 0) not in (0, int(season)):
            continue
        return s
    return None


# ═══════════════════════════════════════════════ MP 订阅存活性
# ★ 用户 2026-10-11 反馈的「求片通过审核后从任务列表消失」的根因：
#   MoviePilot 在**订阅完成**时会**直接删掉 subscribe 行**
#   （源码 app/application/subscription/complete.py → stage_delete_sync），
#   只往 subscribehistory 留一份历史。
#   而 bot 原来把「MP 订阅列表」当唯一数据源 ⇒ 订阅一完成，记录就"消失"了。
#   ⇒ 必须能区分：**订阅还在（在途）** vs **订阅已被移除（已完成归档）**。
_SUBIDS: dict = {"ts": 0.0, "ids": set()}
_SUBIDS_TTL = 60


def mp_sub_ids(ttl: int = _SUBIDS_TTL) -> set[int]:
    """MoviePilot 里**当前仍然存在**的订阅 ID 集合（默认 60 秒缓存）"""
    if _SUBIDS["ts"] and time.time() - _SUBIDS["ts"] < ttl:
        return _SUBIDS["ids"]
    ids: set[int] = set()
    try:
        for s in mp_subscribe_list(200):
            try:
                i = int(s.get("id") or 0)
            except Exception:
                i = 0
            if i:
                ids.add(i)
        _SUBIDS.update(ts=time.time(), ids=ids)      # 只有成功才刷新时间戳
    except Exception as e:
        logger.debug(f"取 MP 订阅 ID 失败：{e}")
    return _SUBIDS["ids"]


def mp_sub_ids_fresh() -> bool:
    """订阅 ID 列表是否**成功取到过**。

    MP 不可达时列表会是空的，这时绝不能把在途订阅判成「已完成」——
    那比不显示更糟（管理员会以为片子已经下好了）。
    """
    return bool(_SUBIDS["ts"])


def is_archived(a: dict) -> bool:
    """这条求片的 MP 订阅是否已被移除（= 已完成归档）

    ★ 只有「曾经拿到过 sub_id」且「MP 列表取到过」时才敢判定归档，
      否则 MP 临时不可达会把在途订阅误判成"已完成"，那比不显示更糟。
    """
    try:
        sid = int(a.get("sub_id") or 0)
    except Exception:
        sid = 0
    if not sid:
        return False
    ids = mp_sub_ids()
    if not _SUBIDS["ts"]:
        return False                    # 一次都没成功取到 ⇒ 不下结论
    return sid not in ids


def sub_state(a: dict) -> str:
    """求片当前真实状态：'archived'（已完成归档）/ 'live'（在途）/ 'none'（无订阅号）"""
    try:
        sid = int(a.get("sub_id") or 0)
    except Exception:
        sid = 0
    if not sid:
        return "none"
    return "archived" if is_archived(a) else "live"


async def push_audit_to_mp(a: dict, charge: bool = True) -> tuple[bool, str, int, int, bool]:
    """把一条已审批的求片推给 MoviePilot —— **全项目唯一实现**。

    ★ 为什么必须唯一：`reqmgr._approve` 与 `request.cb_audit` 原来是两份
      复制粘贴的代码，加了「勾选式选集」后只有一份更新，
      另一份就悄悄退化成"只下第一段" —— 这就是管理台审批丢集的原因。
      现在两边都调这一个函数，不可能再各跑各的。

    charge=False 用于「重新下载已归档的求片」：同一条申请已经扣过灵石，
    不能因为重下一次就再扣一遍。

    返回 (是否成功, 备注, 主订阅号, 订阅条数, 是否已触发立即搜索)
    """
    item = {
        "title": a.get("title") or "",
        "year": a.get("year") or "",
        "type": a.get("mtype") or "电影",
        "media_source": a.get("media_source") or "themoviedb",
        "media_id": a.get("tmdb_id") or "",
    }
    sea = int(a.get("season") or 0)
    ep = int(a.get("start_ep") or 0)
    tot = int(a.get("total_ep") or 0)

    # 勾选式选集：MP 订阅只认连续区间 ⇒ 按连续段拆成多条订阅
    eps = sorted(int(x) for x in str(a.get("ep_list") or "").split(",")
                 if x.strip().isdigit())
    runs = _runs(eps) if len(eps) > 1 else []
    subs: list[int] = []
    if runs:
        ok, notes = True, []
        for (s0, e0) in runs:
            o, nt, sid = await asyncio.to_thread(
                mp_subscribe, item, sea or None, s0, e0)
            ok = ok and o
            if nt:
                notes.append(nt)
            if sid:
                subs.append(sid)
        note = "；".join(dict.fromkeys(notes)) or "已加入订阅"
    else:
        ok, note, sid = await asyncio.to_thread(
            mp_subscribe, item, sea or None, ep or None, tot or None)
        if sid:
            subs.append(sid)
    sub_id = subs[0] if subs else 0

    if not ok:
        return False, note, sub_id, len(subs), False

    # 扣灵石（失败不影响订阅；重新下载时不重复扣）
    if charge:
        try:
            cv = db.get_cultivator(int(a["tg"]))
            price = int(S.MP.get("request_price", 1))
            if cv and price > 0:
                db.try_spend_ls(int(a["tg"]), price)
        except Exception as e:
            logger.debug(f"求片扣费失败（不影响订阅）: {e}")

    # 登记求片清单：中文名 + 原名都登记（PT 种子名多为英文，只存中文匹配不上）
    try:
        db.add_wishlist(int(a["tg"]), a.get("tg_name") or "",
                        a.get("title") or "", a.get("title") or "",
                        a.get("year") or "")
        if a.get("original"):
            db.add_wishlist(int(a["tg"]), a.get("tg_name") or "",
                            a.get("original"), a.get("title") or "",
                            a.get("year") or "")
    except Exception as e:
        logger.debug(f"求片清单登记失败（不影响订阅）: {e}")

    # 立即触发搜索（每条订阅都要踢，否则拆出来的第 2、3 段要等下一轮）
    kicked = False
    for sid in subs:
        try:
            if await asyncio.to_thread(mp_kick_search, sid):
                kicked = True
        except Exception as e:
            logger.debug(f"触发搜索失败 #{sid}: {e}")

    _SUBIDS["ts"] = 0.0          # 订阅刚建好，让"已归档"判定立刻反映最新状态
    return True, note, sub_id, len(subs), kicked


# ═══════════════════════════════════════════════ 图片
def _poster(url: str | None, size: str = "w500") -> str:
    if not url or not str(url).startswith("http"):
        return ""
    return re.sub(r"/t/p/[^/]+/", f"/t/p/{size}/", str(url))


def _fetch_img(url: str) -> bytes | None:
    if not url:
        return None
    hit = _IMG_CACHE.get(url)
    if hit and time.time() - hit[0] < _IMG_TTL:
        return hit[1]
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and r.content and len(r.content) < 5 * 1024 * 1024:
            if len(_IMG_CACHE) >= _IMG_MAX:
                _IMG_CACHE.pop(next(iter(_IMG_CACHE)), None)
            _IMG_CACHE[url] = (time.time(), r.content)
            return r.content
    except Exception as e:
        logger.debug(f"海报下载失败 {url}: {e}")
    return None


def _font(size: int):
    try:
        return ImageFont.truetype(_FONT, size)
    except Exception:
        return ImageFont.load_default()


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    """等比裁剪填满 w×h"""
    sw, sh = im.size
    if sw == 0 or sh == 0:
        return im.resize((w, h))
    scale = max(w / sw, h / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    return im.crop(((nw - w) // 2, (nh - h) // 2,
                    (nw - w) // 2 + w, (nh - h) // 2 + h))


def _collage(results: list[dict]) -> bytes | None:
    """把所有候选海报拼成一张总览图（带序号角标和片名）"""
    n = len(results)
    if n == 0:
        return None
    pics = []
    for r in results:
        u = _poster(r.get("poster"), "w342") or _poster(r.get("backdrop"), "w500")
        pics.append(_fetch_img(u) if u else None)
    if not any(pics):
        return None

    cols = 1 if n == 1 else (2 if n <= 4 else 3)
    cw, ch, lab, pad = 340, 500, 52, 10
    rows = (n + cols - 1) // cols
    W = cols * cw + pad * (cols + 1)
    H = rows * (ch + lab) + pad * (rows + 1)
    canvas = Image.new("RGB", (W, H), (16, 17, 22))
    dr = ImageDraw.Draw(canvas)
    ft_txt = _font(21)
    ft_idx = _font(22)

    for i, data in enumerate(pics):
        col, row = i % cols, i // cols
        x = pad + col * (cw + pad)
        y = pad + row * (ch + lab + pad)
        try:
            if data:
                im = Image.open(io.BytesIO(data)).convert("RGB")
                canvas.paste(_cover(im, cw, ch), (x, y))
            else:
                raise ValueError("no poster")
        except Exception:
            dr.rectangle([x, y, x + cw, y + ch], fill=(38, 40, 48))
            dr.text((x + 16, y + 20), "无海报", font=ft_txt, fill=(150, 152, 160))

        # 序号角标
        bx, by, br = x + 24, y + 24, 21
        dr.ellipse([bx - br, by - br, bx + br, by + br], fill=(232, 74, 95))
        tw = dr.textlength(str(i + 1), font=ft_idx)
        dr.text((bx - tw / 2, by - 15), str(i + 1), font=ft_idx, fill=(255, 255, 255))

        # 底部标题条
        ty = y + ch
        dr.rectangle([x, ty, x + cw, ty + lab], fill=(26, 27, 33))
        r = results[i]
        label = f"{r.get('title') or ''}"
        if r.get("year"):
            label += f"（{r['year']}）"
        while dr.textlength(label, font=ft_txt) > cw - 20 and len(label) > 4:
            label = label[:-2]
        dr.text((x + 10, ty + 14), label, font=ft_txt, fill=(238, 240, 246))

    bio = io.BytesIO()
    canvas.save(bio, "JPEG", quality=88, optimize=True)
    return bio.getvalue()


# ═══════════════════════════════════════════════ 消息原地刷新
async def _show(msg: Message, pic, caption: str, kb: KM | None) -> Message | None:
    """把一条消息原地换成「图 + 说明 + 按钮」。

    全程只占用一条消息：总览拼图 → 详情大图 → 选季 → 选集 → 确认，
    都是 edit_media 换图，群内不会刷屏。
    """
    cap = ui.clip_html(caption)
    try:
        if pic:
            buf = io.BytesIO(pic)
            buf.name = "p.jpg"
            await msg.edit_media(
                InputMediaPhoto(buf, caption=cap, parse_mode=PM),
                reply_markup=kb)
        else:
            await msg.edit_text(cap, reply_markup=kb, parse_mode=PM,
                                disable_web_page_preview=True)
        return msg
    except Exception as e:
        logger.debug(f"原地换图失败，降级重发: {e}")
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        if pic:
            buf = io.BytesIO(pic)
            buf.name = "p.jpg"
            return await bot.send_photo(msg.chat.id, buf, caption=cap,
                                        reply_markup=kb, parse_mode=PM)
        return await bot.send_message(msg.chat.id, cap, reply_markup=kb,
                                      parse_mode=PM,
                                      disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"降级重发也失败: {e}")
        return None


async def _edit_txt(msg: Message, caption: str, kb: KM | None) -> None:
    """只改文字，保留当前图片"""
    cap = ui.clip_html(caption)
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(cap, reply_markup=kb, parse_mode=PM)
        else:
            await msg.edit_text(cap, reply_markup=kb, parse_mode=PM,
                                disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"改写文案失败: {e}")


# ═══════════════════════════════════════════════ 会话
def _cache_of(uid: int) -> dict | None:
    c = _QP_CACHE.get(uid)
    if not c or time.time() - c["ts"] > _QP_TTL:
        return None
    return c


def _sea_list(c: dict, idx: int) -> list[dict]:
    return c.get("sea", {}).get(idx) or []


def _sea_total(c: dict, idx: int, season: int) -> int:
    for s in _sea_list(c, idx):
        if s["season_number"] == season:
            return int(s.get("episode_count") or 0)
    return 0


def _all_total(c: dict, idx: int) -> int:
    return sum(int(s.get("episode_count") or 0) for s in _sea_list(c, idx))


def _single_season_of(c: dict, idx: int) -> int:
    """全季求片时，若 TMDB 只有一个有效季（忽略 season 0 花絮季），
    返回该季号用于逐集查库；多季则返回 0（多季各自查太贵，退回整剧判定）。"""
    lst = [s for s in _sea_list(c, idx)
           if int(s.get("season_number") or 0) > 0
           and int(s.get("episode_count") or 0) > 0]
    if len(lst) == 1:
        return int(lst[0]["season_number"])
    return 0


# ───────────────────────────────────── 选集勾选（用户 2026-10-11 要求）
# 原来一集一个按钮、点一下就直接进确认页 ⇒ 想挑「第 3、7、12 集」得走三遍流程，
# 中间缺集时特别难用。现在改成**勾选式多选**：勾中的集一起提交。
# ⚠️ MoviePilot 的订阅只支持「连续区间」(start_episode~total_episode)，
#    所以下发时按连续段拆成多条订阅（1,2,3,7,9 → 1-3 + 7 + 9 共 3 条）。
def _epsel(c: dict, sea: int, reset: bool = False) -> set[int]:
    """当前会话里「第 sea 季」已勾选的集号集合；换季自动清空"""
    st = c.get("epsel")
    if reset or not isinstance(st, dict) or st.get("sea") != sea:
        st = {"sea": sea, "eps": set()}
        c["epsel"] = st
    return st["eps"]


def _eps_of(c: dict, sea: int) -> list[int]:
    """已勾选集号（升序）；没勾过返回空列表"""
    st = c.get("epsel")
    if not isinstance(st, dict) or st.get("sea") != sea:
        return []
    return sorted(int(x) for x in (st.get("eps") or set()))


# ───────────── 库里已有·按集（2026-10-13 用户要求）─────────────
# 选集完成后，先去 Emby 把「这一季我已经有哪些集、各是什么画质/码率/体积」
# 摊开给用户看，再让他选：换版本 / 跳过已有只求缺失 / 仍然全求。
def _eps_index_of(season_id: str, limit: int = 2000) -> dict:
    """某季 → {集号: 版本画像}

    ★ strm 库里 MediaSources 恒空，画质/码率/体积只能从 Path / 文件名解析，
      再配合 RunTimeTicks（时长）用「码率 × 时长」反推体积（见 _ver_profile）。
    ★ 2026-10-14：同一集多版本（1080p/4K 各一条 strm）时**保留体积/质量更优
      的那份**（此前是字典直接覆盖，可能把 4K 展示成后处理的 1080p）；
      limit 提到 2000 以覆盖分页后的重复版本。
    """
    out: dict[int, dict] = {}
    start = 0
    while start < limit:
        ej = _emby_api("/emby/Items", ParentId=str(season_id),
                       IncludeItemTypes="Episode", Recursive="true",
                       StartIndex=start, Limit=min(500, limit - start),
                       Fields="IndexNumber,Path,FileName,RunTimeTicks")
        if ej is None:
            break
        items = [(e if isinstance(e, dict) else {})
                 for e in ((ej or {}).get("Items") or [])]
        total = int(ej.get("TotalRecordCount") or 0)
        for e in items:
            try:
                n = int(e.get("IndexNumber") or 0)
            except Exception:
                continue
            if n <= 0:
                continue
            nm = str(e.get("Path") or e.get("FileName") or "")
            try:
                rt = float(int(e.get("RunTimeTicks") or 0)) / 6e8
            except Exception:
                rt = 0.0
            v = _ver_profile([nm], rt)
            old = out.get(n)
            if old is None or (float(v.get("size_gb") or 0), float(v.get("score") or 0)) \
                    > (float(old.get("size_gb") or 0), float(old.get("score") or 0)):
                out[n] = v
        if len(items) == 0 or start + len(items) >= total:
            break
        start += len(items)
    return out


def _lib_eps(title: str, tmdb_id: str, season: int) -> dict:
    """Emby 库里「该剧第 N 季」→ {集号: 版本画像}；查不到返回 {}"""
    try:
        hit = _emby_hit(title, tmdb_id, "电视剧", season)
        sid = str(hit.get("id") or "")
        if not sid:
            return {}
        raw = _seasons_of(sid)
        tgt = next((s.get("Id") for s in raw
                    if int(s.get("IndexNumber") or 0) == int(season)), None)
        if not tgt:
            return {}
        return _eps_index_of(str(tgt))
    except Exception as e:
        logger.debug(f"库内按集查询失败: {e}")
        return {}


def _qual_line(v: dict) -> str:
    """★ 2026-10-15 用户定稿顺序（所有画质展示统一走这里）：
    分辨率 → 压制/来源(REMUX/x265…) → 动态范围 → 色深 → 编码 → 音频 → 码率
    （同一行）。**体积不在这行**，由调用方单独起一行写（_ep_brief/_qual_block）。
    """
    seg: list[str] = []
    # ① 分辨率
    if v.get("res"):
        seg.append(str(v["res"]))
    # ② 压制 / 来源（REMUX / BluRay / WEB-DL / HDTV）
    if v.get("src"):
        seg.append(str(v["src"]))
    # ③ 动态范围（杜比视界 / HDR10+ / HDR10 / HLG / SDR）
    dr: list[str] = []
    if v.get("dv"):
        dr.append("杜比视界")
    if v.get("hdr"):
        dr.append(str(v["hdr"]))
    if dr:
        seg.append("+".join(dr))
    # ④ 色深（10bit / 8bit）
    bd = [str(x) for x in (v.get("bitdepth") or []) if str(x).strip()]
    if bd:
        seg.append("/".join(bd))
    # ⑤ 编码（H265/H264/AV1）
    cod = [str(x) for x in (v.get("cod") or []) if str(x).strip()]
    if cod:
        seg.append("/".join(cod))
    # ⑥ 音频（Atmos/TrueHD/DDP5.1/DTS…）
    aud = [str(x) for x in (v.get("aud") or []) if str(x).strip()]
    if aud:
        seg.append("/".join(aud[:3]))
    # ⑦ 码率
    try:
        br = float(v.get("bitrate") or 0)
        if br:
            seg.append(f"{br:.1f}Mbps")
    except Exception:
        pass
    if not seg:
        seg = [str(v.get("quals") or "—")]
    return " · ".join(seg)


def _size_line(v: dict, unit: str = "") -> str:
    """体积独立行（用户：文件大小不要和画质挤同一行，单独一行方便看）"""
    try:
        sz = float(v.get("size_gb") or 0)
    except Exception:
        sz = 0.0
    if sz > 0:
        return f"💾 体积：约 <b>{sz:.1f} GB</b>{unit}"
    return "💾 体积：未知（strm 库无真实字节数，文件名里也推算不出）"


def _ep_brief(n: int, v: dict) -> str:
    """单集画质：第一行画质（_qual_line 顺序）+ 第二行体积（独立行）"""
    return (f"　🎞 E{n}：" + _qual_line(v) + "\n"
            + f"　　" + _size_line(v).replace("💾 体积：", "💾 "))


def _runs(eps: list[int]) -> list[tuple[int, int]]:
    """[1,2,3,7,9] → [(1,3),(7,7),(9,9)] —— 连续段合并（MP 只认连续区间）"""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(eps):
        j = i
        while j + 1 < len(eps) and eps[j + 1] == eps[j] + 1:
            j += 1
        out.append((eps[i], eps[j]))
        i = j + 1
    return out


def _runs_txt(eps: list[int]) -> str:
    """[1,2,3,7,9] → "1-3、7、9"（读起来比罗列 1、2、3、7、9 省一半字）"""
    return "、".join(str(a) if a == b else f"{a}-{b}" for a, b in _runs(eps))


def _scope_txt(item: dict, season: int, ep: int, c: dict, idx: int) -> tuple[str, int]:
    """(范围说明, 预计下载任务数)

    ★ 勾选式选集优先：勾了多集就按勾选清单走（连续段合并显示）。
    """
    if (item.get("type") or "") != "电视剧":
        return "全片（单部）", 1
    if not season:
        n = _all_total(c, idx)
        return (f"全季全集（共 {n} 集）" if n else "全季全集"), (n or 0)
    eps = _eps_of(c, season)
    if len(eps) == 1:
        return f"第 {season} 季 · 第 {eps[0]} 集", 1
    if len(eps) > 1:
        return (f"第 {season} 季 · 第 {_runs_txt(eps)} 集"
                f"（共 {len(eps)} 集）"), len(eps)
    if not ep:
        n = _sea_total(c, idx, season)
        return (f"第 {season} 季 · 全集（{n} 集）" if n
                else f"第 {season} 季 · 全集"), (n or 0)
    return f"第 {season} 季 · 第 {ep} 集", 1


# ═══════════════════════════════════════════════ 文案
def _mlink(text: str, url: str) -> str:
    """把一段已经转义过的文本做成 <a> 超链接；没有链接就原样返回"""
    text = str(text or "")
    if not url:
        return text
    return f'<a href="{_esc(url)}">{text}</a>'


def _tmdb_url(r: dict) -> str:
    """TMDB 详情页链接；电影是 /movie/，剧集是 /tv/"""
    tid = str(r.get("tmdb_id") or r.get("media_id") or "").strip()
    if not tid or not tid.isdigit():
        return ""
    kind = "tv" if (r.get("type") or "") == "电视剧" else "movie"
    return f"https://www.themoviedb.org/{kind}/{tid}"


def _ov_caption(keyword: str, results: list[dict]) -> str:
    lines = [
        f"🔍 <b>搜索结果 · {_t(keyword, 30)}</b>",
        _div(),
        f"共找到 <b>{len(results)}</b> 个候选，已合成上方总览图",
    ]
    for i, r in enumerate(results, 1):
        y = f"（{r['year']}）" if r.get("year") else ""
        star = f"　⭐{r['vote']}" if r.get("vote") else ""
        # 片名一律做成 TMDB 超链接（指向 TMDB 详情页），不再只是纯文本
        link = _tmdb_url(r) or str(r.get("link") or "")
        title = _mlink(_t(r["title"], 26), link)
        lines.append(f"<b>{i}.</b> 🎬 {title}{y}　{r['type']}{star}")
    lines.append(_div())
    lines.append("👆 片名可直接点开 TMDB 详情；点下面的按钮查看每部片的大图与选集")
    return ui.clip_html("\n".join(lines))


def _pick_link(r: dict) -> str:
    """优先自己拼的规范 TMDB 链接，其次用 MP 给的 detail_link"""
    return _tmdb_url(r) or str(r.get("link") or "")


def _detail_caption(r: dict) -> str:
    y = f"（{r['year']}）" if r.get("year") else ""
    lines = [f"🎬 <b>{_mlink(_t(r['title'], 40), _pick_link(r))}</b>{y}", _div()]
    meta = [f"📁 类型：{_t(r['type'], 8)}"]
    if r.get("release"):
        meta.append(f"📅 {_t(r['release'], 12)}")
    if r.get("vote"):
        meta.append(f"⭐ {r['vote']}")
    lines.append("　·　".join(meta))
    if r.get("genres"):
        lines.append(f"🏷 分类：{_t(r['genres'], 60)}")
    if r.get("original") and r["original"] != r["title"]:
        lines.append(f"🌐 原名：{_t(r['original'], 40)}")
    ov = (r.get("overview") or "").strip()
    if ov:
        lines.append(_div())
        lines.append(_t(ov, 200) + ("…" if len(ov) > 200 else ""))
    lines.append(_div())
    link = _pick_link(r)
    if link:
        lines.append(f'🔗 <a href="{_esc(link)}">查看 TMDB 详情</a>')
    lines.append("👆 点「求这部」开始选择范围")
    return ui.clip_html("\n".join(lines))


def _confirm_caption(r: dict, season: int, ep: int, c: dict, idx: int,
                     u) -> str:
    scope, n = _scope_txt(r, season, ep, c, idx)
    y = f"（{_t(r['year'], 8)}）" if r.get("year") else ""
    lines = [
        "🔔 <b>确认求片</b>", _div(),
    ]
    lines += _ucard(u.id, u.first_name or '', u.username or '', "求片人")
    lines += [
        # 片名做成 TMDB 超链接，点一下直达该片详情页
        f"🎬 影片：{_tlink(r['title'], r.get('media_id'), r.get('type'), 34)}{y}",
        f"📁 类型：{_t(r['type'], 8)}",
    ]
    if r.get("genres"):
        lines.append(f"🏷 分类：{_t(r['genres'], 60)}")
    lines.append(f"📄 范围：{scope}")
    if n:
        lines.append(f"📦 预计：{n} 个下载任务")
    else:
        lines.append("📦 预计：集数未知，按整部追新")
    if r.get("tmdb_id") or r.get("media_id"):
        lines.append(f"🆔 TMDB：{_t(r.get('tmdb_id') or r.get('media_id'), 16)}")
    lines.append(_div())
    if n and n > 12:
        lines.append(f"⚠️ 该范围共 <b>{n}</b> 集，会一次性产生大量下载任务，请确认无误")
    # ★ 库里已有·按集（2026-10-13 / 2026-10-15 修「已有部分集数要明确提示」）：
    #   摊开已有集的画质/来源/码率/体积，并明确「只求缺集」的选项。
    #   _have/_miss 由 _go_confirm 逐集算好（含全季场景），这里直接用，
    #   不再从 _eps_of 重算（重算在全季 season=0 时会算空 → 漏报）。
    have = c.get("_have") or []
    lib = c.get("_lib") or {}
    miss = [int(x) for x in (c.get("_miss") or [])]
    total = int(c.get("_total") or 0)
    if have:
        lines.append(_div())
        if miss:
            lines.append(f"⚠️ <b>库里已有 {len(have)} 集、还缺 {len(miss)} 集</b>"
                         + (f"（应求共 {total} 集）" if total else "")
                         + "。已有集画质如下：")
        else:
            lines.append(f"📦 <b>库里已有全部 {len(have)} 集</b>（画质/码率/体积如下）：")
        for e in have[:12]:
            lines.append(_ep_brief(e, lib.get(str(e)) or {}))
        if len(have) > 12:
            lines.append(f"　…等共 {len(have)} 集")
        if miss:
            lines.append(f"➡️ 缺失 <b>{len(miss)}</b> 集：{_t(_runs_txt(miss), 60)}")
        lines.append("👉 可点「⏭ 只求缺集」自动过滤掉已有集，或「换版本」连已有集一起洗。")
    lines.append("⏳ 提交后需管理员确认，通过后才会开始下载")
    return ui.clip_html("\n".join(lines))


def _submit_caption(r: dict, season: int, ep: int, c: dict, idx: int,
                    aid: int) -> str:
    scope, _n = _scope_txt(r, season, ep, c, idx)
    y = f"（{_t(r['year'], 8)}）" if r.get("year") else ""
    lines = [
        "📨 <b>求片申请已提交</b>", _div(),
        f"🎬 影片：{_tlink(r['title'], r.get('media_id'), r.get('type'), 34)}{y}",
        f"📄 范围：{scope}",
        f"🆔 编号：#{aid}",
        f"⏰ 时间：{_cn_now()}",
        _div(),
        "⏳ 已发送给管理员审核，通过后会自动开始下载",
        "💡 用 /myreqs 查看我的求片申请状态",
    ]
    return ui.clip_html("\n".join(lines))


def _audit_caption(a: dict) -> str:
    """管理员收到的待审核卡片（中文排版，带用户超链接）"""
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    ts = _cn_dt(a.get("created_at"))
    lines = [
        "📋 <b>新求片待审核</b>", _div(),
    ]
    lines += _ucard(a.get('tg'), a.get('tg_name') or '',
                    a.get('tg_username') or '', "求片人")
    lines += [
        f"🎬 影片：{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}{y}",
        f"📁 类型：{_t(a.get('mtype') or '未知', 8)}",
    ]
    if a.get("genres"):
        lines.append(f"🏷 分类：{_t(a['genres'], 60)}")
    lines.append(f"📄 范围：{_t(a.get('scope') or '全片', 40)}")
    if a.get("ep_count"):
        lines.append(f"📦 任务：约 {int(a['ep_count'])} 个下载任务")
    if a.get("tmdb_id"):
        lines.append(f"🆔 TMDB：{_t(a['tmdb_id'], 16)}")
    # ★ 版本备注：求片人标注的「要 4K / REMUX / 内封简中」等，管理员照着找
    rm = str(a.get("remark") or "").strip()
    if rm:
        lines.append(_div())
        lines.append(f"📝 <b>指定版本</b>：{_t(rm, 120)}")
    if int(a.get("prio") or 0):
        lines.append("⭐ <b>已标为优先处理</b>")
    lines.append(_div())
    if a.get("link"):
        lines.append(f'🔗 <a href="{_esc(a["link"])}">查看 TMDB 详情</a>')
    lines.append(f"⏰ 求片时间：{ts}")
    lines.append(f"🆔 申请号：#{a.get('id')}")
    return ui.clip_html("\n".join(lines), limit=3500)


# ═══════════════════════════════════════════════ 键盘
def _ov_kb(results: list[dict]) -> KM:
    rows = []
    row = []
    for i, r in enumerate(results):
        y = f"（{r['year']}）" if r.get("year") else ""
        lab = f"{i + 1}. {r['title'][:10]}{y}"
        row.append(B(lab, f"qpd:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("❌ 取消", "qpc")])
    return KM(rows)


def _detail_kb(idx: int, r: dict) -> KM:
    rows = [[B("📥 求这部", f"qpr:{idx}")]]
    link = _tmdb_url(r)
    if link:
        rows.append([B("🔗 查看 TMDB 详情", url=link)])
    rows.append([B("↩️ 返回列表", "qplist"), B("❌ 取消", "qpc")])
    return KM(rows)


def _nav(first: str, prev: str, nxt: str, last: str, jump: str,
         page: int, pages: int) -> list[B]:
    row = []
    if page > 0:
        row.append(B("⏮ 首页", first))
        row.append(B("⬅️ 上一页", prev))
    if page < pages - 1:
        row.append(B("➡️ 下一页", nxt))
        row.append(B("⏭ 末页", last))
    return row


def _season_kb(idx: int, seasons: list[dict], page: int = 0,
               per: int = 8) -> KM:
    pages = max(1, (len(seasons) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = [[B("🎞 全季全集（整部剧一起追）", f"qps:{idx}:0")]]
    chunk = seasons[page * per:(page + 1) * per]
    row = []
    for s in chunk:
        n = s["season_number"]
        ep = s.get("episode_count") or 0
        lab = f"第 {n} 季" + (f" · {ep}集" if ep else "")
        row.append(B(lab, f"qps:{idx}:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        nav = _nav(f"qpns:{idx}:0", f"qpns:{idx}:{page - 1}",
                   f"qpns:{idx}:{page + 1}", f"qpns:{idx}:{pages - 1}",
                   "", page, pages)
        if nav:
            rows.append(nav)
        rows.append([B("🔢 跳到第几页", f"qpj:s:{idx}:0")])
        rows.append([B(f"📄 第 {page + 1} / {pages} 页", "qnoop")])
    rows.append([B("↩️ 返回详情", f"qpd:{idx}"), B("❌ 取消", "qpc")])
    return KM(rows)


def _ep_kb(idx: int, season: int, total: int, page: int = 0,
           per: int = 20, sel: set[int] | None = None) -> KM:
    """选集键盘 —— **勾选式多选**：点一集切换 ✅/⬜，最后「提交勾选」一起下单。

    ★ 为什么改成勾选（用户 2026-10-11 要求）：中间缺集时只想补第 3、7、12 集，
      原来一集一个按钮、点一下就直接进确认页 ⇒ 得把整个流程走三遍。
      现在勾几集就下几集，一次提交。
    """
    sel = set(sel or ())
    top = total if total > 0 else 1
    pages = max(1, (top + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = [[B("📦 本季全集（不挑集）", f"qpe:{idx}:{season}:0")]]
    start, end = page * per, min((page + 1) * per, top)
    row = []
    for e in range(start + 1, end + 1):
        row.append(B(f"{ui.ON}E{e}" if e in sel else f"E{e}",
                     f"qpse:{idx}:{season}:{e}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    n_sel = len(sel)
    # ★ 2026-10-13：用户反馈「选集后没有确认求片的按钮」——单独给一个最显眼的
    #   「✅ 确认求片」按钮（整行独占），没勾集时直接按本季全集走，不再卡住。
    rows.append([B("✅ 确认求片" + (("（%d 集）" % n_sel) if n_sel else "（本季全集）"),
                   f"qpeok:{idx}:{season}")])
    rows.append([B(f"{ui.ON} 提交勾选（{n_sel} 集）", f"qpeok:{idx}:{season}"),
                 B(f"{ui.OFF} 清空勾选", f"qpen:{idx}:{season}")])
    rows.append([B(f"{ui.ON} 本页全勾", f"qpea:{idx}:{season}:{page}")])
    if pages > 1:
        nav = _nav(f"qpne:{idx}:{season}:0", f"qpne:{idx}:{season}:{page - 1}",
                   f"qpne:{idx}:{season}:{page + 1}",
                   f"qpne:{idx}:{season}:{pages - 1}", "", page, pages)
        if nav:
            rows.append(nav)
        rows.append([B("🔢 输入集数（加进勾选）", f"qpj:e:{idx}:{season}"),
                     B("🔢 跳页", f"qpj:p:{idx}:{season}")])
        rows.append([B(f"📄 第 {page + 1} / {pages} 页", "qnoop")])
    else:
        rows.append([B("🔢 输入集数（加进勾选）", f"qpj:e:{idx}:{season}")])
    rows.append([B("↩️ 返回选季", f"qpback:{idx}"), B("❌ 取消", "qpc")])
    return KM(rows)


def _confirm_kb(idx: int, season: int, ep: int,
                have: list | None = None, miss: list | None = None) -> KM:
    """确认页键盘。

    ★ 2026-10-13：库里已有集时，除了「仍然求」，再给一个
      「⏭ 跳过已有，只求缺失」——用户点一下就自动过滤掉库里已有的集。
    ★ 2026-10-15：have 与 miss 都由 _go_confirm 预计算（含全季），
      「只求缺集」按钮在 <b>既有已收、又有缺失</b> 时才出现，语义更准。
    """
    have = [int(x) for x in (have or [])]
    miss = [int(x) for x in (miss or [])]
    rows = []
    if have and miss:
        rows.append([B(f"⏭ 只求缺集（{len(miss)} 集）", f"qpkeep:{idx}:{season}")])
        rows.append([B("🔄 换版本（已有集也重下洗版）", f"qpok:{idx}:{season}:{ep}")])
    elif have and not miss:
        # 全都有了 → 只给换版本（继续求就是洗更高版本），并说明
        rows.append([B("🔄 仍要换版本重下", f"qpok:{idx}:{season}:{ep}")])
    else:
        rows.append([B("✅ 确认提交", f"qpok:{idx}:{season}:{ep}")])
    rows.append([B("↩️ 返回上一步", f"qpback2:{idx}:{season}:{ep}"),
                 B("❌ 取消", "qpc")])
    return KM(rows)


def _ctb_btn(aid: int = 0):
    """「📮 投稿资源」按钮（我手里已有资源，直接投链接/磁力）

    ★ 懒加载 contrib：contrib 反过来要用本模块的 is_admin / _ucard，
      模块级互相 import 会形成环，所以只在拼键盘时才导入。
    """
    try:
        from .contrib import contrib_button
        return contrib_button(aid)
    except Exception as e:
        logger.debug(f"投稿按钮不可用: {e}")
        return None


def _after_kb(aid: int = 0) -> KM:
    """提交回执的键盘：常驻「📝 备注」+「📮 投稿」（用户 2026-10-13 要求）"""
    rows = []
    if aid:
        b = _ctb_btn(aid)
        rows.append([x for x in (B("📝 备注指定版本", f"qn:ask:{aid}"), b) if x])
    rows.append([B("📋 我的求片申请", "qpmine"), B("🔍 继续求片", "qpnew")])
    return KM(rows)


def _audit_kb(aid: int, prio: int = 0) -> KM:
    """管理员收到的待审核卡片键盘（通过 / 驳回 / ⭐优先 / 📝备注）"""
    return KM([
        [B("✅ 通过", f"qa:ok:{aid}"), B("❌ 驳回", f"qa:no:{aid}")],
        [B("⭐ 优先处理" if not prio else "☆ 取消优先", f"qa:prio:{aid}"),
         B("📝 备注", f"qn:ask:{aid}")],
        [B("📋 待审核列表", "qa:list")],
    ])


def _season_text(r: dict, seasons: list[dict], page: int = 0,
                 per: int = 8) -> str:
    pages = max(1, (len(seasons) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    y = f"（{r['year']}）" if r.get("year") else ""
    lines = [f"📺 <b>{_t(r['title'], 30)}</b>{y}　请选择季", _div()]
    for s in seasons[page * per:(page + 1) * per]:
        n = s["season_number"]
        nm = s.get("name") or ""
        ep = s.get("episode_count") or 0
        lines.append(f"<code>S{n}</code>　{_t(nm, 14)}　共 {ep} 集")
    lines.append(_div())
    lines.append("👆 第一个按钮是<b>全季全集</b>；只想看某一季就点对应季号")
    if pages > 1:
        lines.append(f"📄 第 {page + 1} / {pages} 页，可用 ⏮ ⬅️ ➡️ ⏭ 翻页")
    return ui.clip_html("\n".join(lines))


def _ep_text(r: dict, season: int, total: int, page: int = 0,
             per: int = 20, sel: list[int] | None = None) -> str:
    top = total if total > 0 else 1
    pages = max(1, (top + per - 1) // per)
    page = max(0, min(page, pages - 1))
    sel = sorted(int(x) for x in (sel or []))
    lines = [f"📺 <b>{_t(r['title'], 30)}</b>　第 {season} 季", _div(),
             f"本季共 <b>{total if total else '未知'}</b> 集"]
    if sel:
        lines.append(f"{ui.ON} 已勾选 <b>{len(sel)}</b> 集："
                     f"<b>{_t(_runs_txt(sel), 42)}</b>")
    lines.append(_div())
    lines.append(f"👆 点集号 = <b>勾选/取消</b>（{ui.ON} 就是勾上了），"
                 "勾完点「提交勾选」一起下")
    lines.append("💡 <b>中间缺集也没关系</b>，只勾你要的那几集就行")
    lines.append("💡 集数多时用「🔢 输入集数（加进勾选）」直接加")
    if pages > 1:
        lines.append(f"📄 第 {page + 1} / {pages} 页，可用 ⏮ ⬅️ ➡️ ⏭ 翻页")
    return ui.clip_html("\n".join(lines))


# ═══════════════════════════════════════════════ 搜索入口
async def _run_search(msg: Message, keyword: str, u) -> None:
    if not await asyncio.to_thread(mp_login):
        await msg.reply("⚠️ MoviePilot 未连接，请稍后再试或联系管理员。",
                   parse_mode=PM)
        return
    hint = await msg.reply(f"🔍 正在搜索「{_t(keyword, 30)}」…",
                            parse_mode=PM)
    results = await asyncio.to_thread(mp_search, keyword)
    if not results:
        try:
            await hint.delete()
        except Exception:
            pass
        await msg.reply(
            f"😕 没搜到「{_t(keyword, 30)}」。\n{_div()}\n换个别名试试，"
            f"或者确认片名有没有写错。", parse_mode=PM)
        return

    _QP_CACHE[u.id] = {"ts": time.time(), "keyword": keyword,
                       "results": results, "sea": {}}
    pic = await asyncio.to_thread(_collage, results)
    cap = _ov_caption(keyword, results)
    kb = _ov_kb(results)
    try:
        await hint.delete()
    except Exception:
        pass
    if pic:
        buf = io.BytesIO(pic)
        buf.name = "overview.jpg"
        sent = await msg.reply_photo(buf, caption=cap, reply_markup=kb,
                                     parse_mode=PM)
    else:
        sent = await msg.reply(cap, reply_markup=kb, parse_mode=PM,
                               disable_web_page_preview=True)
    _QP_CACHE[u.id]["chat"] = sent.chat.id
    _QP_CACHE[u.id]["mid"] = sent.id


@bot.on_message(filters.command(["qp", "search", "找片", "求片", "mp", "request"],
                                **CMD))
async def cmd_qp(client, msg: Message):
    """求片入口：带片名就直接搜，不带就等用户直接打字"""
    u = msg.from_user
    if not u:
        return
    arg = (msg.text or "").split(maxsplit=1)
    keyword = arg[1].strip()[:60] if len(arg) > 1 else ""

    if not keyword:
        _WAIT[u.id] = "kw"
        await msg.reply(
            "🔍 <b>求片</b>\n" + _div() + "\n"
            "请<b>直接发送片名</b>，不用再加命令。\n"
            "例如直接输入：<code>流浪地球</code>\n" + _div() + "\n"
            "也可以 <code>/qp 流浪地球</code> 一步到位\n"
            "⌛ 等待输入（10 分钟内有效）",
            reply_markup=KM([[B("❌ 取消", "qpcwait")]]),
            parse_mode=PM)
        return
    await _run_search(msg, keyword, u)


# ⚠️ Pyrogram 同一 group 内，第一个 check 通过的 handler 执行完就 break，
#    后面的 handler 根本不会被执行。mptask 也有一个 filters.text，
#    而 main.py 里 mptask 比 request 先导入 —— 结果所有文本消息都被它吃掉，
#    这里永远收不到「用户直接打片名」。
#    双重保险：① 独占 group=2（mptask 用 3，互不干扰，外层 group 循环不带 break）
#              ② 不是自己的等待态就 ContinuePropagation，让下一个 handler 接手
@bot.on_message(filters.text, group=2)
async def on_plain_text(client, msg: Message):
    """等待输入态：用户直接打片名 / 集数 / 页码"""
    u = msg.from_user
    if not u or u.is_bot:
        raise ContinuePropagation
    txt = (msg.text or "").strip()
    if not txt or txt[0] in "/!！":
        raise ContinuePropagation
    st = _WAIT.get(u.id)
    if not st:
        raise ContinuePropagation

    if st == "kw":
        _WAIT.pop(u.id, None)
        await _run_search(msg, txt[:60], u)
        return

    c = _cache_of(u.id)
    if not c:
        _WAIT.pop(u.id, None)
        await msg.reply("⌛ 会话已过期，请重新 /qp 搜索", parse_mode=PM)
        return

    try:
        num = int(re.sub(r"\D", "", txt) or "0")
    except Exception:
        num = 0

    if st[0] == "epnum":
        _, idx, sea = st
        total = _sea_total(c, idx, sea) or 0
        # ★ 支持一次输入多集（"3,7,12" / "3 7 12" / "3、7"），直接**加进勾选**
        nums = sorted({int(x) for x in re.findall(r"\d+", txt) if int(x) > 0})
        if not nums:
            await msg.reply("⚠️ 请输入集数（纯数字；多集可用逗号或空格隔开，"
                            "如 <code>3,7,12</code>）", parse_mode=PM)
            return
        if total:
            bad = [n for n in nums if n > total]
            if bad:
                await msg.reply(
                    f"⚠️ 第 {sea} 季只有 {total} 集，这些超出范围："
                    f"{_t('、'.join(str(b) for b in bad), 30)}", parse_mode=PM)
                return
        _WAIT.pop(u.id, None)
        sel = _epsel(c, sea)
        sel |= set(nums)
        await msg.reply(
            f"{ui.ON} 已勾选 <b>{len(sel)}</b> 集："
            f"<b>{_t(_runs_txt(sorted(sel)), 60)}</b>\n"
            f"💡 还能继续点集号或再输入，勾完点「提交勾选」一起下",
            parse_mode=PM)
        await _show_ep(await _sess_msg(c, msg), c, idx, sea, 0, u)
        return

    if st[0] == "seapage":
        _WAIT.pop(u.id, None)
        idx = st[1]
        seasons = _sea_list(c, idx)
        pages = max(1, (len(seasons) + 7) // 8)
        page = max(0, min(num - 1, pages - 1))
        await _show_season(msg, c, idx, page, u)
        return

    if st[0] == "eppage":
        _WAIT.pop(u.id, None)
        _, idx, sea = st
        total = _sea_total(c, idx, sea) or 1
        pages = max(1, (total + 19) // 20)
        page = max(0, min(num - 1, pages - 1))
        await _show_ep(msg, c, idx, sea, page, u)


async def _sess_msg(c: dict, msg: Message):
    """尽量复用会话里那条消息"""
    chat, mid = c.get("chat"), c.get("mid")
    if chat and mid:
        try:
            return await bot.get_messages(chat, mid)
        except Exception:
            pass
    return msg


# ═══════════════════════════════════════════════ 各页面
async def _show_detail(msg: Message, c: dict, idx: int, u) -> None:
    r = c["results"][idx]
    pic = await asyncio.to_thread(
        _fetch_img, r.get("poster") or r.get("backdrop"))
    await _show(msg, pic, _detail_caption(r), _detail_kb(idx, r))


async def _show_season(msg: Message, c: dict, idx: int, page: int, u) -> None:
    r = c["results"][idx]
    seasons = _sea_list(c, idx)
    if not seasons:
        await _go_confirm(msg, c, idx, 0, 0, u)
        return
    pic = await asyncio.to_thread(
        _fetch_img, r.get("poster") or r.get("backdrop"))
    await _show(msg, pic, _season_text(r, seasons, page),
                _season_kb(idx, seasons, page))


async def _show_ep(msg: Message, c: dict, idx: int, sea: int, page: int,
                   u) -> None:
    r = c["results"][idx]
    total = _sea_total(c, idx, sea)
    sel = _eps_of(c, sea)
    pic = await asyncio.to_thread(
        _fetch_img, r.get("poster") or r.get("backdrop"))
    await _show(msg, pic, _ep_text(r, sea, total, page, sel=sel),
                _ep_kb(idx, sea, total, page, sel=set(sel)))


async def _go_confirm(msg: Message, c: dict, idx: int, sea: int, ep: int,
                      u) -> None:
    r = c["results"][idx]
    # ★ 库里已有·按集（2026-10-13 建 / 2026-10-15 修「全季漏判」）：
    #   进确认页前查 Emby，摊开「已有哪些集 + 画质/码率/体积」，让用户能选
    #   「跳过已有只求缺失」。
    #   ★ 旧 bug：只在 sea>0 时查库；用户求「全季」（sea=0）时 want 为空 →
    #     根本不比对 → 提交后 _emby_hit 笼统回「库里已有」，把「其实还缺
    #     E59/E60/E72~E82」完全说没了。现在全季（单一有效季时）也逐集查库、
    #     按 TMDB 声明的总集数算缺失，给「只求缺集」选项。
    try:
        want_sea = sea or _single_season_of(c, idx)   # 全季且只有一季→按那季查
        total = _sea_total(c, idx, want_sea) if want_sea else _all_total(c, idx)
        eps = _eps_of(c, sea) if sea else []
        want = eps or list(range(1, (total or 0) + 1))
        lib = {}
        if want_sea and want:
            lib = await asyncio.to_thread(
                _lib_eps, r.get("title") or "", r.get("media_id") or "", want_sea)
        have = sorted(e for e in want if e in lib)
        miss = [e for e in want if e not in lib]
        c["_sea"] = want_sea
        c["_total"] = total
        c["_lib"] = {str(k): v for k, v in lib.items()}
        c["_have"] = have
        c["_miss"] = miss
    except Exception as e:
        logger.debug(f"确认前查库失败: {e}")
        c["_lib"], c["_have"], c["_miss"] = {}, [], []
    pic = await asyncio.to_thread(
        _fetch_img, r.get("poster") or r.get("backdrop"))
    await _show(msg, pic, _confirm_caption(r, sea, ep, c, idx, u),
                _confirm_kb(idx, sea, ep, c.get("_have") or [],
                            c.get("_miss") or []))


# ═══════════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^qnoop$"))
async def cb_noop(client, cb):
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^qpcwait$"))
async def cb_wait_cancel(client, cb):
    _WAIT.pop(cb.from_user.id, None)
    await cb.answer("已取消")
    try:
        await cb.message.delete()
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^qpc$"))
async def cb_cancel(client, cb):
    uid = cb.from_user.id
    _QP_CACHE.pop(uid, None)
    _WAIT.pop(uid, None)
    await cb.answer("已取消")
    try:
        await cb.message.edit_text("🔍 已取消本次求片。", parse_mode=PM)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass


@bot.on_callback_query(filters.regex(r"^qpnew$"))
async def cb_new(client, cb):
    _WAIT[cb.from_user.id] = "kw"
    await cb.answer()
    await cb.message.reply(
        "🔍 请<b>直接发送片名</b>（不用再加命令）\n"
        "⌛ 等待输入（10 分钟内有效）",
        reply_markup=KM([[B("❌ 取消", "qpcwait")]]),
        parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^qpd:(\d+)$"))
async def cb_detail(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    idx = int(cb.data.split(":", 1)[1])
    if not (0 <= idx < len(c["results"])):
        await cb.answer("序号无效", show_alert=True)
        return
    await cb.answer()
    await _show_detail(cb.message, c, idx, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qplist$"))
async def cb_list(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    await cb.answer()
    pic = await asyncio.to_thread(_collage, c["results"])
    await _show(cb.message, pic, _ov_caption(c["keyword"], c["results"]),
                _ov_kb(c["results"]))


@bot.on_callback_query(filters.regex(r"^qpr:(\d+)$"))
async def cb_pick(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    idx = int(cb.data.split(":", 1)[1])
    item = c["results"][idx]
    if (item.get("type") or "") != "电视剧":
        await cb.answer()
        await _go_confirm(cb.message, c, idx, 0, 0, cb.from_user)
        return
    await cb.answer("📺 正在获取季信息…")
    seasons = _sea_list(c, idx)
    if not seasons:
        seasons = await asyncio.to_thread(mp_seasons, item["media_id"],
                                         item["media_source"])
        if seasons:
            c.setdefault("sea", {})[idx] = seasons
    if not seasons:
        await _go_confirm(cb.message, c, idx, 0, 0, cb.from_user)
        return
    await _show_season(cb.message, c, idx, 0, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpns:(\d+):(-?\d+)$"))
async def cb_season_page(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, idx, page = cb.data.split(":")
    await cb.answer()
    await _show_season(cb.message, c, int(idx), max(0, int(page)), cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpne:(\d+):(\d+):(-?\d+)$"))
async def cb_ep_page(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, idx, sea, page = cb.data.split(":")
    await cb.answer()
    await _show_ep(cb.message, c, int(idx), int(sea), max(0, int(page)),
                   cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpj:([sep]):(\d+):(-?\d+)$"))
async def cb_jump(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, kind, idx, sea = cb.data.split(":")
    idx, sea = int(idx), int(sea)
    uid = cb.from_user.id
    if kind == "e":
        _WAIT[uid] = ("epnum", idx, sea)
        tip = "请输入<b>集数</b>（纯数字）"
    elif kind == "s":
        _WAIT[uid] = ("seapage", idx)
        tip = "请输入要跳转的<b>页码</b>"
    else:
        _WAIT[uid] = ("eppage", idx, sea)
        tip = "请输入要跳转的<b>页码</b>"
    await cb.answer()
    await cb.message.reply(
        f"🔢 <b>跳转</b>\n{_div()}\n{tip}\n"
        "⌛ 直接发送数字即可（10 分钟内有效）",
        reply_markup=KM([[B("❌ 取消", "qpcwait")]]),
        parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^qps:(\d+):(\d+)$"))
async def cb_season(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea = cb.data.split(":")
    idx, sea = int(idx), int(sea)
    if sea == 0:                                   # 全季全集
        await cb.answer()
        await _go_confirm(cb.message, c, idx, 0, 0, cb.from_user)
        return
    total = _sea_total(c, idx, sea)
    if not total:
        await cb.answer("⚠️ 该季集数未知，按整季订阅")
        await _go_confirm(cb.message, c, idx, sea, 0, cb.from_user)
        return
    await cb.answer(f"第 {sea} 季 共 {total} 集")
    await _show_ep(cb.message, c, idx, sea, 0, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpe:(\d+):(\d+):(\d+)$"))
async def cb_episode(client, cb):
    """「本季全集」(ep=0) 直接进确认页；ep>0 是旧版单集按钮（老消息里可能还有），
    等价于「只勾这一集」后再提交。"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea, ep = cb.data.split(":")
    idx, sea, ep = int(idx), int(sea), int(ep)
    if ep:
        _epsel(c, sea, reset=True).add(ep)
    await cb.answer()
    await _go_confirm(cb.message, c, idx, sea, ep, cb.from_user)


# ───────────────────────── 勾选式选集（用户 2026-10-11 要求）─────────────
@bot.on_callback_query(filters.regex(r"^qpse:(\d+):(\d+):(\d+)$"))
async def cb_ep_toggle(client, cb):
    """点某一集 → **切换勾选**（✅ ⇄ 空框），原地刷新，不跳页、不重新下海报"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea, ep = cb.data.split(":")
    idx, sea, ep = int(idx), int(sea), int(ep)
    sel = _epsel(c, sea)
    if ep in sel:
        sel.discard(ep)
        tip = f"{ui.OFF} 已取消第 {ep} 集"
    else:
        sel.add(ep)
        tip = f"{ui.ON} 已勾选第 {ep} 集（共 {len(sel)} 集）"
    await cb.answer(tip)
    total = _sea_total(c, idx, sea)
    pages = max(1, (max(total, 1) + 19) // 20)
    page = max(0, min((ep - 1) // 20, pages - 1))
    await _show_ep(cb.message, c, idx, sea, page, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpea:(\d+):(\d+):(\d+)$"))
async def cb_ep_all(client, cb):
    """把当前这一页的集全部勾上（跨页保留已勾选的）"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, idx, sea, page = cb.data.split(":")
    idx, sea, page = int(idx), int(sea), int(page)
    total = _sea_total(c, idx, sea)
    start, end = page * 20, min((page + 1) * 20, total if total > 0 else 1)
    sel = _epsel(c, sea)
    sel |= set(range(start + 1, end + 1))
    await cb.answer(f"{ui.ON} 本页已全勾（共 {len(sel)} 集）")
    await _show_ep(cb.message, c, idx, sea, page, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpen:(\d+):(\d+)$"))
async def cb_ep_none(client, cb):
    """清空这一季的勾选"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, idx, sea = cb.data.split(":")
    idx, sea = int(idx), int(sea)
    _epsel(c, sea, reset=True)
    await cb.answer(f"{ui.OFF} 已清空勾选")
    await _show_ep(cb.message, c, idx, sea, 0, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpeok:(\d+):(\d+)$"))
async def cb_ep_ok(client, cb):
    """提交勾选：把勾中的几集**一起**带进确认页。
    ★ 2026-10-13 修复「选季选集后没有确定按钮」：没勾任何集时不再 dead-end
      弹提示，而是直接按「本季全集」进确认页——保证「确定」永远能往前走。"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea = cb.data.split(":")
    idx, sea = int(idx), int(sea)
    eps = _eps_of(c, sea)
    if not eps:
        await cb.answer("未勾选，按本季全集提交")
        await _go_confirm(cb.message, c, idx, sea, 0, cb.from_user)
        return
    await cb.answer(f"已选 {len(eps)} 集：{_runs_txt(eps)[:40]}")
    await _go_confirm(cb.message, c, idx, sea, eps[0], cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpkeep:(\d+):(\d+)$"))
async def cb_keep_missing(client, cb):
    """「⏭ 跳过已有，只求缺失」：把勾选集合过滤成 Emby 里没有的集，重开确认页。

    ★ 2026-10-13 用户要求：可选跳过库里已有集数、只求剩下没有的。
    """
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea = cb.data.split(":")
    idx, sea = int(idx), int(sea)
    # ★ 2026-10-15：直接用 _go_confirm 预计算好的缺失清单（全季场景 _eps_of
    #   返回空 → 旧逻辑会把「只求缺集」误判成「全都齐了没的可求」）。
    miss = [int(x) for x in (c.get("_miss") or [])]
    if not miss:
        # 兜底：万一 _miss 没算，按已勾选集与 _have 求差
        have = set(int(x) for x in (c.get("_have") or []))
        miss = [e for e in _eps_of(c, sea) if e not in have]
    if not miss:
        await cb.answer("库里这些集都有了，没有缺失的集可求", show_alert=True)
        return
    # 勾选集合改成「只留缺失的」，再回确认页（此时确认页不再有“已有”提示）
    real_sea = int(c.get("_sea") or sea or 0)
    _epsel(c, real_sea, reset=True).update(miss)
    c["_have"] = []
    c["_miss"] = []
    await cb.answer(f"已过滤：只求缺失的 {len(miss)} 集")
    # 用真实季号重进确认页（全季过滤后就是求这一季缺的集）
    await _go_confirm(cb.message, c, idx, real_sea, miss[0], cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpback:(\d+)$"))
async def cb_back_season(client, cb):
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    idx = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await _show_season(cb.message, c, idx, 0, cb.from_user)


@bot.on_callback_query(filters.regex(r"^qpback2:(\d+):(\d+):(\d+)$"))
async def cb_back2(client, cb):
    """确认页的「返回上一步」：回到选季 / 选集"""
    c = _cache_of(cb.from_user.id)
    if not c:
        await cb.answer("⌛ 会话已过期", show_alert=True)
        return
    _, idx, sea, ep = cb.data.split(":")
    idx, sea, ep = int(idx), int(sea), int(ep)
    await cb.answer()
    if (c["results"][idx].get("type") or "") != "电视剧":
        await _show_detail(cb.message, c, idx, cb.from_user)
        return
    if not sea:
        await _show_season(cb.message, c, idx, 0, cb.from_user)
        return
    await _show_ep(cb.message, c, idx, sea, 0, cb.from_user)


# ═══════════════════════════════════════════════ 提交 → 审批
# ═══════════════════════════════════════════════════════════════════
# 求片前查 Emby：库里已有 → 群里立即通知 + 给「取消 / 继续 / 求其他质量版本」
# ───────────────────────────────────────────────────────────────────
# 需求（2026-10-12）：
#   ① 求片命中 Emby 已有资源 → **立即在群内通知群友该影片已有**
#   ② 通知下方按钮：取消求片 / 继续求片 / 求此影片其他质量版本
#   ③ 点「求此影片其他质量版本」→ 进入**备注环节**，可写分辨率/编码/字幕/音轨等
#      提交后把备注带进 MP 订阅与播报，继续求片流程
#
# 匹配策略（按准确度从高到低）：
#   TMDB id 精确匹配（ProviderIds.Tmdb） > 片名归一化相等 > 片名互含
# ═══════════════════════════════════════════════════════════════════
_EMBY_HIT_TTL = 300.0                 # 同名查库结果缓存 5 分钟
_emby_hit_cache: dict[str, tuple[float, dict]] = {}

# 「求其他质量版本」等待备注的状态：uid -> aid
_ALT_WAIT: dict[int, int] = {}
_ALT_TTL = 900.0                      # 15 分钟不填就作废
_ALT_TS: dict[int, float] = {}


def _emby_api(path: str, **params):
    """Emby REST 只读封装（失败返回 None，绝不抛异常打断求片主流程）"""
    try:
        params["api_key"] = S.EMBY["api_key"]
        r = requests.get(S.EMBY["url"].rstrip("/") + path,
                         params=params, timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"Emby 接口失败 {path}: {e}")
        return None


def _norm_name(s: str) -> str:
    """片名归一化：只留汉字/字母/数字，去空格标点，用于弱匹配"""
    return re.sub(r"[^0-9a-z一-鿿]+", "", str(s or "").lower())


def _qual_of(it: dict) -> str:
    """抽出画质要点。

    ⚠️ 本库是 **strm 库**（MediaSources / MediaStreams 永远为空，ChildCount 也是 null），
       所以画质只能从 **strm 文件名** 里解析，例如：
       怪奇物语：1985故事集.2026.S01E01.2160p.WEB-DL.HDR10.20.9Mbps.24fps.10-bit.H265.DDP 5.1.strm
    """
    return _qual_from_names([str(it.get("Path") or it.get("FileName") or "")])


def _qual_from_names(names: list[str]) -> str:
    """从一批文件名里汇总画质要点（分辨率 / HDR / 杜比视界 / 编码 / 音轨 / 来源）"""
    res, hdr, dv, cod, aud, src = set(), set(), set(), set(), set(), set()
    for nm in names:
        s = str(nm or "")
        if not s:
            continue
        u = s.upper()
        # 分辨率
        for pat, tag in ((r"(?:4320|8K)[PI]?", "8K"), (r"(?:2160|4K)[PI]?", "4K"),
                         (r"1080[PI]", "1080P"), (r"720[PI]", "720P"),
                         (r"480[PI]", "480P")):
            if re.search(pat, u):
                res.add(tag)
                break
        # HDR / 杜比视界
        if re.search(r"HDR\s*10\s*\+", u):
            hdr.add("HDR10+")
        elif re.search(r"HDR\s*10", u):
            hdr.add("HDR10")
        elif "HDR" in u:
            hdr.add("HDR")
        if "DOLBY VISION" in u or re.search(r"\bDV\b", u) or "DOVI" in u:
            dv.add("杜比视界")
        # 编码
        if re.search(r"\b(H\s?265|HEVC|X265|10-BIT|10BIT)\b", u):
            cod.add("H265")
        # 色深（10bit / 8bit）
        if re.search(r"10[- ]?BIT", u):
            bitdepth_set.add("10bit")
        elif re.search(r"\b8[- ]?BIT\b", u):
            bitdepth_set.add("8bit")
        elif re.search(r"\b(H\s?264|AVC|X264)\b", u):
            cod.add("H264")
        if "AV1" in u:
            cod.add("AV1")
        # 音轨
        if "ATMOS" in u:
            aud.add("Atmos")
        if re.search(r"TRUE\s?HD", u):
            aud.add("TrueHD")
        if re.search(r"\bDDP\b|DOLBY\s?DIGITAL\s?PLUS|\bDD\+", u):
            aud.add("DDP5.1")
        if re.search(r"\bDTS\b", u):
            aud.add("DTS")
        if re.search(r"\bAAC\b", u):
            aud.add("AAC")
        if re.search(r"\bFLAC\b", u):
            aud.add("FLAC")
        # 来源：★ REMUX / BDMV 先判，别让「BluRay.REMUX」被压成 Bluray
        if "REMUX" in u or "BDMV" in u:
            src.add("REMUX")
        elif "BLURAY" in u or "BLU-RAY" in u:
            src.add("Bluray")
        elif re.search(r"WEB[- ]?DL", u):
            src.add("WEB-DL")
        elif "HDTV" in u:
            src.add("HDTV")
    # ★ 2026-10-15 按 MT 命名规则排序：分辨率 → 压制/来源 → 编码 → 音频 →
    #   动态范围 → 其他；且不再截断到 6 个（用户：FPS 只写 F = 截断太狠）
    tags = []
    for group in (res, src, cod, aud, dv, hdr):
        for t in sorted(group):
            if t not in tags:
                tags.append(t)
    return " · ".join(tags[:12]) or "—"


# ══════════════════════════════════════════════════════════════════
#  版本画像：体积 / 质量分 / 目标版本（用户 2026-10-13 要求「按文件大小
#  自动比对，申请高于 Emby 库现有资源的版本」）
# ──────────────────────────────────────────────────────────────────
#  strm 库里 MediaSources / Size 恒为空，拿不到真实字节数，只能**推算**：
#    ① 文件名里显式写了体积（…55.2GB… / [55.2G]）→ 直接用
#    ② 有码率（…20.9Mbps…）+ 时长（Emby RunTimeTicks）→ 体积 = 码率 × 时长
#    ③ 都沒有 → 按「分辨率 + 来源」查表估一个典型体积
#  三种都给不出时返回 0（表示未知），文案里显示「体积未知」而不是瞎编。
# ══════════════════════════════════════════════════════════════════
_RES_LV = {"480P": 1, "720P": 2, "1080P": 3, "4K": 5, "8K": 6}
_SRC_LV = {"HDTV": 0, "WEB-DL": 1, "BLURAY": 2, "REMUX": 3}
# 典型体积（GB）：(分辨率, 来源) -> 单部电影 / 单集
_TYPICAL_GB = {
    ("8K", "REMUX"): 120, ("8K", "BLURAY"): 80, ("8K", "WEB-DL"): 50,
    ("4K", "REMUX"): 60, ("4K", "BLURAY"): 40, ("4K", "WEB-DL"): 20,
    ("1080P", "REMUX"): 25, ("1080P", "BLURAY"): 15, ("1080P", "WEB-DL"): 5,
    ("1080P", "HDTV"): 3,
    ("720P", "REMUX"): 10, ("720P", "BLURAY"): 4, ("720P", "WEB-DL"): 2.5,
    ("720P", "HDTV"): 1.5,
    ("480P", "WEB-DL"): 1.2, ("480P", "HDTV"): 1.0, ("480P", "BLURAY"): 2.0,
}
# 比现有版本更高的目标（分辨率档位递进）
_NEXT_RES = {"480P": "720P", "720P": "1080P", "1080P": "2160P(4K)",
             "4K": "2160P(4K) 高码率版", "8K": "8K 高码率版"}


def _ver_profile(names: list[str], runtime_min: float = 0.0) -> dict:
    """从文件名（+ 可选时长）解析出版本画像。

    返回 {'quals','res','src','bitrate','size_gb','score','dv','hdr','cod',
          'aud','runtime_min'}
      size_gb  估算体积（GB，0=未知）
      score    质量分（越大越好，用于「只申请更高的」比对）
    """
    quals = _qual_from_names(names)
    res_set, src_set, dv, hdr, cod, aud = set(), set(), False, "", set(), set()
    bitdepth_set: set[str] = set()
    bitrates: list[float] = []
    sized: list[float] = []

    for nm in names:
        s = str(nm or "")
        if not s:
            continue
        u = s.upper()
        for pat, tag in ((r"(?:4320|8K)[PI]?", "8K"), (r"(?:2160|4K)[PI]?", "4K"),
                         (r"1080[PI]", "1080P"), (r"720[PI]", "720P"),
                         (r"480[PI]", "480P")):
            if re.search(pat, u):
                res_set.add(tag)
                break
        # ★ 顺序要紧：REMUX/BDMV 必须**先于** BLURAY 判断，
        #   否则「BluRay.REMUX」会被判成 BLURAY，把原盘档位压低一级。
        if "REMUX" in u or "BDMV" in u:
            src_set.add("REMUX")
        elif "BLURAY" in u or "BLU-RAY" in u:
            src_set.add("BLURAY")
        elif re.search(r"WEB[- ]?DL", u):
            src_set.add("WEB-DL")
        elif "HDTV" in u:
            src_set.add("HDTV")
        if "DOLBY VISION" in u or re.search(r"\bDV\b", u) or "DOVI" in u:
            dv = True
        if re.search(r"HDR\s*10\s*\+", u):
            hdr = "HDR10+"
        elif re.search(r"HDR\s*10", u) and hdr != "HDR10+":
            hdr = "HDR10"
        elif "HDR" in u and not hdr:
            hdr = "HDR"
        if re.search(r"\b(H\s?265|HEVC|X265|10-BIT|10BIT)\b", u):
            cod.add("H265")
        elif re.search(r"\b(H\s?264|AVC|X264)\b", u):
            cod.add("H264")
        if "AV1" in u:
            cod.add("AV1")
        # 音轨（完整画质信息要列出来）
        if "ATMOS" in u:
            aud.add("Atmos")
        if re.search(r"TRUE\s?HD", u):
            aud.add("TrueHD")
        if re.search(r"\bDDP\b|DOLBY\s?DIGITAL\s?PLUS|\bDD\+", u):
            aud.add("DDP5.1")
        if re.search(r"\bDTS\b", u):
            aud.add("DTS")
        if re.search(r"\bAAC\b", u):
            aud.add("AAC")
        if re.search(r"\bFLAC\b", u):
            aud.add("FLAC")
        # 码率 20.9Mbps / 20.9mbps
        m = re.search(r"(\d+(?:\.\d+)?)\s*MBPS", u)
        if m:
            try:
                bitrates.append(float(m.group(1)))
            except Exception:
                pass
        # 显式体积 55.2GB / 55.2G / 108.5gb / [55.2 GB]
        m = re.search(r"(\d+(?:\.\d+)?)\s*G\s*B\b", u)
        if m:
            try:
                sized.append(float(m.group(1)))
            except Exception:
                pass
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*GB(?!\w)", u)
            if m:
                try:
                    sized.append(float(m.group(1)))
                except Exception:
                    pass

    res = max(res_set, key=lambda x: _RES_LV.get(x, 0)) if res_set else ""
    src = max(src_set, key=lambda x: _SRC_LV.get(x, -1)) if src_set else ""
    br = max(bitrates) if bitrates else 0.0

    size = 0.0
    if sized:
        size = max(sized)
    elif br and runtime_min:
        size = br / 8.0 * 60.0 * float(runtime_min) / 1024.0
    elif res and src:
        size = float(_TYPICAL_GB.get((res, src), 0.0))
    elif res:
        size = float(_TYPICAL_GB.get((res, "WEB-DL"), 0.0))

    score = (_RES_LV.get(res, 0) * 10.0
             + _SRC_LV.get(src, 0) * 3.0
             + (2.0 if dv else 0.0)
             + (1.5 if hdr == "HDR10+" else 1.0 if hdr else 0.0)
             + (1.0 if ("H265" in cod or "AV1" in cod) else 0.0)
             + min(br / 20.0, 6.0))
    return {"quals": quals, "res": res, "src": src, "bitrate": br,
            "size_gb": round(size, 1), "score": round(score, 2),
            "dv": dv, "hdr": hdr, "cod": sorted(cod), "aud": sorted(aud),
            "bitdepth": sorted(bitdepth_set),
            "runtime_min": round(float(runtime_min or 0.0), 1)}


def _target_of(ver: dict) -> str:
    """按现有版本算出「比它更高的目标版本」文案"""
    res, src = ver.get("res") or "", ver.get("src") or ""
    if not res:
        return "1080P 及以上（现有版本信息不全，尽量选体积更大的）"
    extra: list[str] = []
    if res in ("4K", "8K"):
        tgt = _NEXT_RES.get(res, "更高码率版本")
        if src != "REMUX":
            extra.append("REMUX 原盘")
        if not ver.get("dv"):
            extra.append("杜比视界")
        if ver.get("hdr") != "HDR10+":
            extra.append("HDR10+")
    else:
        tgt = _NEXT_RES.get(res, "1080P")
        extra.append("WEB-DL 起" if res in ("480P", "720P")
                     else "优先 REMUX / BluRay 原盘")
    return tgt + ("　/ " + " / ".join(extra) if extra else "")


def _emby_tmdb_of(it: dict) -> str:
    """取条目的 TMDB id：优先 ProviderIds.Tmdb，其次从 Path 里的 [tmdbid-xxxx] 解析。

    strm 库里 ProviderIds 常常是空的，但整理时目录名带了 `[tmdbid-224263]`，
    从路径解析可以把「同名不同片」的误判降到最低。
    """
    tid = str(((it.get("ProviderIds") or {}) or {}).get("Tmdb") or "").strip()
    if tid.isdigit():
        return tid
    m = re.search(r"tmdbid-(\d+)", str(it.get("Path") or it.get("FileName") or ""),
                  re.I)
    return m.group(1) if m else ""


def _seasons_of(series_id: str) -> list[dict]:
    """取某剧的全部季（strm 库 ChildCount 为 null，只能一条条拉）"""
    sj = _emby_api("/emby/Items", ParentId=series_id,
                   IncludeItemTypes="Season", Recursive="true",
                   Fields="IndexNumber", Limit=60)
    return [(s if isinstance(s, dict) else {})
            for s in ((sj or {}).get("Items") or [])]


def _episodes_of(parent_id: str, limit: int = 10) -> list[dict]:
    """取某季（或某剧）下的集，带 Path / 时长 —— 体积推算全靠文件名"""
    ej = _emby_api("/emby/Items", ParentId=parent_id,
                   IncludeItemTypes="Episode", Recursive="true",
                   Limit=limit, Fields="Path,FileName,RunTimeTicks")
    return [(e if isinstance(e, dict) else {}) for e in ((ej or {}).get("Items") or [])]


def _season_counts(raw: list[dict]) -> list[tuple[int, int]]:
    """逐季统计集数 —— ★按「唯一集号」去重（2026-10-14 修复）：
    strm 库同一集存在多版本（1080p/4K 各一条 strm）时，TotalRecordCount
    会把重复条目一起计入（实测吞噬星空 S1：274 条目 = 245 集 + 29 个重复版本），
    导致「库里已有 274 集」这种远超真实集数的误报。改为分页拉全量、
    按 IndexNumber 去重统计；极端长季（>3000 集）退化为原始计数。"""
    seas: list[tuple[int, int]] = []
    for s in raw:
        try:
            sn = int(s.get("IndexNumber") or 0)
        except Exception:
            sn = 0
        if sn <= 0:
            continue
        uniq: set[int] = set()
        total = 0
        for start in range(0, 3000, 500):
            ej = _emby_api("/emby/Items", ParentId=s.get("Id"),
                           IncludeItemTypes="Episode", Recursive="true",
                           StartIndex=start, Limit=500, Fields="IndexNumber")
            if ej is None:
                break
            total = int(ej.get("TotalRecordCount") or 0)
            items = [e for e in (ej.get("Items") or []) if isinstance(e, dict)]
            for e in items:
                try:
                    n = int(e.get("IndexNumber") or 0)
                except Exception:
                    continue
                if n > 0:
                    uniq.add(n)
            if len(items) < 500 or start + 500 >= total:
                break
        seas.append((sn, len(uniq) if uniq else total))
    seas.sort()
    return seas


def _cand_profile(it: dict, is_tv: bool, season: int = 0) -> dict:
    """给一个候选条目算「版本画像 + 季集信息」。

    剧集：抽目标季（没有就第一季）的样本集名来解析，这样
    「第 2 季是 4K、第 1 季只有 1080p」也能反映成不同的基准。
    """
    info = {"seasons": [], "sample": "", "eps_n": 0}
    if (it.get("Type") == "Series") or is_tv:
        raw = _seasons_of(str(it.get("Id") or ""))
        info["seasons"] = _season_counts(raw)
        tgt = next((s.get("Id") for s in raw
                    if int(s.get("IndexNumber") or 0) == int(season or 0)), None)
        if tgt is None and raw:
            tgt = raw[0].get("Id")
        if tgt:
            eps = _episodes_of(str(tgt), 10)
            names = [str(e.get("Path") or e.get("FileName") or "") for e in eps]
            rt = 0.0
            for e in eps:
                try:
                    rt = max(rt, int(e.get("RunTimeTicks") or 0) / 6e8)
                except Exception:
                    pass
            info["eps_n"] = len(eps)
            info["sample"] = _basename_of(names[0]) if names else ""
            info["ver"] = _ver_profile(names, rt)
        else:
            info["ver"] = _ver_profile(
                [str(it.get("Path") or it.get("FileName") or "")])
    else:
        nm = str(it.get("Path") or it.get("FileName") or "")
        info["sample"] = _basename_of(nm)
        info["ver"] = _ver_profile([nm], _runtime_min(it))
    return info


def _basename_of(p: str) -> str:
    """取文件名（去目录、去 .strm 后缀）—— 展示「参考文件」用"""
    s = str(p or "").replace("\\", "/")
    s = s.rsplit("/", 1)[-1] if "/" in s else s
    return re.sub(r"\.(strm|mkv|mp4|ts|iso|m2ts)$", "", s, flags=re.I).strip()


def _qual_block(ver: dict, unit: str = "", sample: str = "") -> list[str]:
    """完整画质信息块 —— ★ 2026-10-15 用户定稿：
    第一行 = 画质全要素，顺序严格按 MT 命名规则：
      分辨率 → 压制/来源 → 动态范围 → 色深 → 编码 → 音频 → 码率
    第二行 = 文件体积（**独立行**，不与画质挤同一行）
    其余（时长/参考文件）按需附在后面。
    """
    lines = [f"🎞 画质：{_qual_line(ver)}"]
    lines.append(_size_line(ver, unit))
    extra: list[str] = []
    if ver.get("runtime_min"):
        extra.append(f"⏱ {_runtime_txt(float(ver['runtime_min']))}")
    if extra:
        lines.append("　" + "　".join(extra))
    else:
        lines.append("💾 体积：未知（strm 库无真实字节数，文件名里也推算不出）")
    if sample:
        lines.append(f"📄 参考文件：{_t(sample, 46)}")
    return lines


def _runtime_txt(minutes: float) -> str:
    m = float(minutes or 0)
    if m <= 0:
        return "未知"
    if m >= 60:
        return f"{int(m // 60)} 小时 {int(m % 60)} 分"
    return f"{int(round(m))} 分钟"


def _emby_hit(title: str, tmdb_id: str = "", mtype: str = "",
              season: int = 0) -> dict:
    """查 Emby 里这部片是否已有。

    ★ 2026-10-13 改版（用户要求）：**以库里已有的最大文件作为参考基准**。
      同一部片库里常常同时存在 1080p / 4K / REMUX 多个版本，
      以前取「匹配到的第一个」，很可能拿到最小的那个 ——
      拿 720p 当基准去「求更高版本」，会求回一个还不如现有的版本。
      现在改成：把所有候选都算一遍版本画像，**取体积最大（同体积比质量分）**。

    返回 {'hit','name','year','id','detail','seasons','quals',
          'res','src','bitrate','size_gb','score','has_season',
          'cand_n'(候选版本数),'sample'(参考文件名)}
    """
    out = {"hit": False, "name": "", "year": "", "id": "", "detail": "",
           "seasons": [], "quals": "", "has_season": False,
           "res": "", "src": "", "bitrate": 0.0, "size_gb": 0.0,
           "score": 0.0, "cand_n": 0, "sample": ""}
    title = str(title or "").strip()
    if not title:
        return out
    ck = f"{title}|{tmdb_id}|{mtype}|{season}"
    c = _emby_hit_cache.get(ck)
    if c and time.time() - c[0] < _EMBY_HIT_TTL:
        return dict(c[1])

    mt = (mtype or "").strip()
    is_tv = (mt == "电视剧")
    itype = ("Series" if is_tv else ("Movie" if mt == "电影" else "Movie,Series"))
    # strm 库里 MediaSources / ChildCount 恒空，只能靠 Path / FileName
    fields = "ProviderIds,ProductionYear,Path,FileName,Type,RunTimeTicks"
    j = _emby_api("/emby/Items", Recursive="true", IncludeItemTypes=itype,
                  SearchTerm=title, Limit=12, Fields=fields)
    items = [(x if isinstance(x, dict) else {}) for x in ((j or {}).get("Items") or [])]
    if not items:
        _emby_hit_cache[ck] = (time.time(), out)
        return out

    tid = str(tmdb_id or "").strip()
    nt = _norm_name(title)

    # ① 首选：TMDB id 精确匹配（避免「怪奇物语」误配「怪奇物语：1985故事集」）
    cands: list[dict] = []
    if tid:
        cands = [it for it in items if _emby_tmdb_of(it) == tid]
    # ② 次选：片名归一化**完全相等**
    if not cands:
        cands = [it for it in items
                 if nt and _norm_name(it.get("Name")) == nt]
    # ③ 兜底：互含匹配，但**仅在两边都没有 TMDB id 可比**时才用，
    #    否则宁可判为未命中（漏报可以补，误报会让用户以为白求了）
    if not cands:
        for it in items:
            ni = _norm_name(it.get("Name"))
            if not (nt and ni):
                continue
            if not (nt in ni or ni in nt):
                continue
            if tid and _emby_tmdb_of(it) and _emby_tmdb_of(it) != tid:
                continue          # 有 id 却对不上 ⇒ 同名不同片，跳过
            if max(len(nt), len(ni)) < 4:
                continue          # 太短的互含不可靠
            cands.append(it)

    if not cands:
        _emby_hit_cache[ck] = (time.time(), out)
        return out

    # ★ 逐个算版本画像（最多 4 个，控制 Emby 请求量），取体积最大的
    profs: list[dict] = []
    for it in cands[:4]:
        try:
            p = _cand_profile(it, is_tv, season)
        except Exception as e:
            logger.debug(f"版本画像失败: {e}")
            continue
        p["_it"] = it
        profs.append(p)
    if not profs:
        _emby_hit_cache[ck] = (time.time(), out)
        return out

    best = max(profs, key=lambda p: (float((p["ver"] or {}).get("size_gb") or 0),
                                     float((p["ver"] or {}).get("score") or 0)))
    bit = best["_it"]
    ver = best["ver"] or {}
    out["hit"] = True
    out["cand_n"] = len(profs)
    out["name"] = str(bit.get("Name") or title)
    out["year"] = str(bit.get("ProductionYear") or "")
    out["id"] = str(bit.get("Id") or "")
    out["seasons"] = best.get("seasons") or []
    out["sample"] = best.get("sample") or ""
    out.update({k: ver.get(k) for k in
                ("quals", "res", "src", "bitrate", "size_gb", "score",
                 "dv", "hdr", "cod", "aud", "bitdepth", "runtime_min")})

    if bit.get("Type") == "Series" or is_tv:
        seas = out["seasons"]
        if seas:
            txt = "　".join(f"S{s}（{n} 集）" for s, n in seas[:8])
            if len(seas) > 8:
                txt += f" …共 {len(seas)} 季"
            out["detail"] = f"已有 {len(seas)} 季：{txt}"
        else:
            out["detail"] = "库里已有该剧"
        if season:
            out["has_season"] = any(s == int(season) for s, _ in seas)
            if out["has_season"]:
                n = next((c2 for s, c2 in seas if s == int(season)), 0)
                out["detail"] += f"\n✅ 你求的第 {season} 季已有（{n} 集）"
            else:
                out["detail"] += f"\n⚠️ 你求的第 {season} 季库里还没有"
        unit = f"（单集，共比对 {best.get('eps_n') or 0} 集样本）"
    else:
        out["detail"] = ("库里已有该片"
                         + (f"（{out['year']}）" if out["year"] else ""))
        unit = "（整片）"

    if len(profs) > 1:
        out["detail"] += (f"\n🔎 库里检出 <b>{len(profs)}</b> 个版本，"
                          f"已按<b>体积最大</b>的作比对基准")
    out["detail"] += "\n" + "\n".join(_qual_block(ver, unit, out["sample"]))

    _emby_hit_cache[ck] = (time.time(), out)
    return out


def _runtime_min(it: dict) -> float:
    """Emby 时长（RunTimeTicks，1 分钟 = 6e8 tick）→ 分钟"""
    try:
        return float(int(it.get("RunTimeTicks") or 0)) / 6e8
    except Exception:
        return 0.0


async def _group_have_it(aid: int, hit: dict) -> None:
    """群里播报「库里已有」+ 下一步按钮

    ★ 2026-10-13：命中后**自动给出升级目标**（按体积/质量分比对），
      按钮直接写清楚「会求到什么版本」，求片人一眼能决定要不要继续。
    """
    from ..helper import send_group, send_group_photo
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        return
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    tgt = _target_of(hit)
    sz = float(hit.get("size_gb") or 0)
    lines = [
        "📦 <b>这部片库里已经有了</b>", _div(),
        f"🎬 影片：{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}{y}",
    ]
    if hit.get("name") and _norm_name(hit["name"]) != _norm_name(a.get("title")):
        lines.append(f"　（库内名：{_t(hit['name'], 30)}）")
    lines.append(_div())
    # ★ 完整画质信息逐行输出（detail 内部已带 HTML，不要再用 _t 截断）
    lines += [ln for ln in str(hit.get("detail") or "库里已有").split("\n") if ln]
    if sz:
        lines.append(f"⬆️ 自动比对后可求：<b>{_t(tgt, 40)}</b>"
                     f"（以现有 {sz:.1f} GB 为基准往上找）")
    # ★ 2026-10-15 用户要求：洗版失败风险必须明说。
    #   「继续求同一部」时，MP 搜到的资源若体积/质量 ≤ 库内现有，
    #   Emby 整理入库阶段会判定「已有更优版本」而**不入库**（洗版失败）。
    #   这里把「库内现有质量」和「继续求的风险」摊开讲，交给求片人决定。
    lines.append(_div())
    lines.append("⚠️ <b>洗版风险提示</b>：继续求同一部，若 MP 搜到的资源"
                 f"体积/码率<b>不大于现有版本</b>（约 {sz:.1f} GB"
                 f"{' · ' + str(hit.get('res') or '') if hit.get('res') else ''}"
                 f"{' · ' + str(hit.get('src') or '') if hit.get('src') else ''}），"
                 "入库时会被判为「已有更优版本」而<b>不会入库</b>（洗版失败）。"
                 "建议点「⬆️ 求更高版本」让它按现有基准往上找。")
    lines.append(_div())
    # _ucard 可能返回含空行的两行（没绑 Emby / 没用户名时），空行会让排版塌一块
    lines += [x for x in _ucard(a.get('tg'), a.get('tg_name') or '',
                                a.get('tg_username') or '', "求片人")
              if str(x).strip()]
    lines.append(f"📄 想求：{_t(a.get('scope') or '全片', 40)}")
    lines.append(_div())
    lines.append("👇 请选下一步（求片人或管理员都能点）：")
    text = ui.clip_html("\n".join(lines), limit=3500)
    kb = KM([
        [B("⬆️ 求更高版本（自动比对）", f"qh:alt:{aid}")],
        [B("▶️ 就要现在这版（洗版有风险）", f"qh:gox:{aid}"),
         B("❌ 取消求片", f"qh:cancel:{aid}")],
        [B("📝 我自己写版本要求", f"qn:ask:{aid}")],
        [x for x in (_ctb_btn(aid),) if x],     # ★ 我手里有资源，直接投
    ])
    pic = await asyncio.to_thread(_fetch_img, a.get("poster") or a.get("backdrop"))
    if pic:
        if not await send_group_photo(bot, pic, text, parse_mode=PM, reply_markup=kb):
            await send_group(bot, text, parse_mode=PM,
                             disable_web_page_preview=True, reply_markup=kb)
    else:
        await send_group(bot, text, parse_mode=PM,
                         disable_web_page_preview=True, reply_markup=kb)


async def _dm_have_it(uid: int, aid: int, hit: dict) -> None:
    """私聊求片人：「库里已有」+ 三个按钮（与群播报同款，方便他在私聊里选）"""
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        return
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    tgt = _target_of(hit)
    sz = float(hit.get("size_gb") or 0)
    lines = [
        "📦 <b>这部片库里已经有了</b>", _div(),
        f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 30)}{y}",
        _div(),
    ]
    lines += [ln for ln in str(hit.get("detail") or "库里已有").split("\n") if ln]
    if sz:
        lines.append(f"⬆️ 点「求更高版本」会自动按 <b>{sz:.1f} GB</b> 这个基准，"
                     f"去找 <b>{_t(tgt, 36)}</b>")
    lines.append(_div())
    lines.append("⚠️ <b>洗版风险</b>：继续求同一部，若 MP 资源不超过现有"
                 f"（约 {sz:.1f} GB）→ 入库判「已有更优」→ <b>不会入库</b>。"
                 "求更高版本成功率更高。")
    lines.append(_div())
    lines.append("👇 选一个（不选就一直挂着，不会自动下载）：")
    kb = KM([
        [B("⬆️ 求更高版本（自动比对）", f"qh:alt:{aid}")],
        [B("▶️ 就要现在这版（洗版有风险）", f"qh:gox:{aid}"),
         B("❌ 取消求片", f"qh:cancel:{aid}")],
        [B("📝 我自己写版本要求", f"qn:ask:{aid}")],
        [x for x in (_ctb_btn(aid),) if x],
    ])
    try:
        await bot.send_message(int(uid), ui.clip_html("\n".join(lines), limit=3500),
                               parse_mode=PM, disable_web_page_preview=True,
                               reply_markup=kb)
    except Exception as e:
        logger.debug(f"私聊库里已有通知失败 {uid}: {e}")


@bot.on_callback_query(filters.regex(r"^qh:(cancel|go|gox|goconfirm|alt):(\d+)$"))
async def cb_haveit(client, cb):
    """「库里已有」通知下的三个按钮：取消 / 继续 / 求其他质量版本"""
    u = cb.from_user
    act, aid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await cb.answer("⌛ 这条申请已不存在", show_alert=True)
        return
    # 权限：求片人本人 或 管理员
    mine = int(a.get("tg") or 0) == int(u.id)
    if not (mine or is_admin(u.id)):
        await cb.answer("⛔ 只有求片人本人或管理员能操作", show_alert=True)
        return
    if str(a.get("status") or "") not in ("auditing",):
        await cb.answer("ℹ️ 这条申请已处理过了", show_alert=True)
        return

    if act == "cancel":
        await asyncio.to_thread(
            db.set_audit, aid, "rejected", int(u.id), "库里已有，求片人取消")
        await cb.answer("已取消求片")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        from ..helper import send_group
        await send_group(
            bot,
            ui.clip_html("\n".join([
                "🚫 <b>求片已取消</b>", _div(),
                f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
                f"👤 {_ucard_one(u.id, u.first_name or '', u.username or '')}"
                f"　理由：库里已有",
            ])), parse_mode=PM, disable_web_page_preview=True)
        return

    # act == "gox" → ★ 2026-10-15 用户要求：洗版风险二次确认。
    #   先摊「MP 质量 vs 库内质量」的对比，让求片人明确知道风险再决定。
    if act == "gox":
        hit = await asyncio.to_thread(
            _emby_hit, a.get("title") or "", str(a.get("tmdb_id") or ""),
            a.get("mtype") or "", int(a.get("season") or 0))
        sz = float(hit.get("size_gb") or 0)
        tgt = _target_of(hit)
        lines = [
            "⚠️ <b>洗版风险 · 二次确认</b>", _div(),
            f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
            _div(),
            "📚 <b>库内现有（比对基准）</b>",
        ]
        lines += _qual_block(hit, "", hit.get("sample") or "")
        lines += [
            _div(),
            "🔍 <b>继续求会怎样</b>：按上面的基准去 MP 搜资源，"
            f"目标 <b>{_t(tgt, 36)}</b>；",
            "· 若搜到的资源<b>更大/更清晰</b> → 正常入库（洗版成功）✅",
            f"· 若搜到的资源<b>不超过现有的 {sz:.1f} GB</b> → "
            "入库时判「已有更优版本」→ <b>不会入库（洗版失败）</b>❌",
            _div(),
            "仍要继续吗？（仍继续就按现有基准搜，搜不到更优的就不入库）",
        ]
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.reply(
            ui.clip_html("\n".join(lines), limit=3500), parse_mode=PM,
            disable_web_page_preview=True,
            reply_markup=KM([
                [B("✅ 仍要继续（已知洗版可能失败）", f"qh:goconfirm:{aid}")],
                [B("⬆️ 算了，求更高版本", f"qh:alt:{aid}"),
                 B("❌ 取消求片", f"qh:cancel:{aid}")],
            ]))
        return

    if act == "goconfirm":
        # 已确认风险 → 照常送审 + 群里播报（并注明可能洗版失败）
        await cb.answer("已继续求片，等待管理员确认")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        from ..helper import send_group
        await send_group(
            bot,
            ui.clip_html("\n".join([
                "▶️ <b>库里已有，求片人确认仍继续（已知洗版风险）</b>", _div(),
                f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
                f"📄 {_t(a.get('scope') or '全片', 40)}",
                _div(),
                "⚠️ 已提示：MP 资源若不超过库内现有，将不会入库。",
                "⏳ 等待管理员确认",
            ])), parse_mode=PM, disable_web_page_preview=True)
        await _notify_admins(aid)
        return

    if act == "go":
        # 继续求片：照常送审 + 群里播报
        await cb.answer("已继续求片，等待管理员确认")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        from ..helper import send_group
        await send_group(
            bot,
            ui.clip_html("\n".join([
                "▶️ <b>库里已有，但求片人仍要继续求</b>", _div(),
                f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
                f"📄 {_t(a.get('scope') or '全片', 40)}",
                _div(), "⏳ 等待管理员确认",
            ])), parse_mode=PM, disable_web_page_preview=True)
        await _notify_admins(aid)
        return

    # act == "alt" → ★ 自动比对：读出 Emby 现有版本 → 算出更高目标 → 直接提交
    await cb.answer("⏳ 正在比对 Emby 现有版本…")
    hit = await asyncio.to_thread(
        _emby_hit, a.get("title") or "", str(a.get("tmdb_id") or ""),
        a.get("mtype") or "", int(a.get("season") or 0))
    sz = float(hit.get("size_gb") or 0)
    tgt = _target_of(hit)
    if sz:
        auto = (f"求更高版本：现有 {hit.get('res') or '未知分辨率'}"
                f" {hit.get('src') or ''} 约 {sz:.1f} GB"
                f" → 目标 {tgt}；请优先选比现有体积更大、质量更高的版本")
    else:
        auto = f"求更高版本：目标 {tgt}（Emby 现有版本体积未知，按更高分辨率/码率找）"
    await asyncio.to_thread(db.set_audit_remark, aid, auto, int(u.id))
    await asyncio.to_thread(db.set_audit_note, aid, "求更高版本（已自动比对）",
                            int(u.id))

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from ..helper import send_group
    g_lines = [
        "⬆️ <b>求更高版本 · 已按 Emby 现有版本自动比对</b>", _div(),
        f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
        f"📄 {_t(a.get('scope') or '全片', 40)}",
        _div(),
        "📚 <b>库内现有（以体积最大的那个作基准）</b>",
    ]
    g_lines += _qual_block(hit, "", hit.get("sample") or "")
    g_lines += [f"🎯 目标版本：{_t(tgt, 40)}", _div(), "⏳ 等待管理员确认"]
    await send_group(
        bot,
        ui.clip_html("\n".join(g_lines)), parse_mode=PM,
        disable_web_page_preview=True)
    # 给求片人一条可改的回执（想自己指定版本就点「改备注」）
    try:
        await cb.message.reply(
            ui.clip_html("\n".join([
                "✅ <b>已按更高版本提交</b>", _div(),
                f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 30)}",
                f"🎯 自动目标：{_t(tgt, 40)}",
                _div(),
                "📝 想自己指定版本（如「要 REMUX 原盘」「要内封简中」）"
                "点下面按钮改，不改就按上面的目标找。",
            ])), parse_mode=PM, disable_web_page_preview=True,
            reply_markup=_remark_kb(
                aid, extra=[[x] for x in (_ctb_btn(aid),) if x]))
    except Exception:
        pass
    await _notify_admins(aid)


@bot.on_callback_query(filters.regex(r"^qh:skip:(\d+)$"))
async def cb_haveit_skip(client, cb):
    """跳过备注，直接按原样继续求片"""
    u = cb.from_user
    aid = int(cb.data.split(":")[2])
    _ALT_WAIT.pop(u.id, None)
    _ALT_TS.pop(u.id, None)
    await cb.answer("已跳过备注")
    a = await asyncio.to_thread(db.get_audit, aid)
    if a:
        await asyncio.to_thread(
            db.set_audit_note, aid,
            "求其他质量版本（未填具体要求）", int(u.id))
        from ..helper import send_group
        await send_group(
            bot,
            ui.clip_html("\n".join([
                "▶️ <b>继续求片 · 求其他质量版本</b>", _div(),
                f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
                "📝 备注：求其他质量版本（未填具体要求）",
                _div(), "⏳ 等待管理员确认",
            ])), parse_mode=PM, disable_web_page_preview=True)
        await _notify_admins(aid)


# ══════════════════════════════════════════════════════════════════
#  备注（标注指定版本）—— 申请消息旁常驻「📝 备注」按钮
# ──────────────────────────────────────────────────────────────────
#  用户 2026-10-13 要求：申请消息旁加备注按钮，可标注「要 4K / REMUX /
#  内封简中」这类**指定版本**。备注存 kfc_req_audit.remark（与 note 分开，
#  状态变更不会把它冲掉），管理员面板与下发通知都能看到。
# ══════════════════════════════════════════════════════════════════
def _remark_kb(aid: int, extra: list[list[B]] | None = None) -> KM:
    """常驻「📝 备注 / 改备注」按钮（可叠在别的键盘上）"""
    rows: list[list[B]] = [[B("📝 备注指定版本", f"qn:ask:{aid}")]]
    if extra:
        rows = extra + rows
    return KM(rows)


@bot.on_callback_query(filters.regex(r"^qn:(ask|clear):(\d+)$"))
async def cb_remark(client, cb):
    """点「📝 备注」→ 进入输入态，等用户打一行版本要求"""
    u = cb.from_user
    act, aid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await cb.answer("⌛ 这条申请已不存在", show_alert=True)
        return
    mine = int(a.get("tg") or 0) == int(u.id)
    if not (mine or is_admin(u.id)):
        await cb.answer("⛔ 只有求片人本人或管理员能备注", show_alert=True)
        return

    if act == "clear":
        await asyncio.to_thread(db.set_audit_remark, aid, "", int(u.id))
        await cb.answer("已清除备注")
        try:
            await cb.message.reply("🧹 备注已清除，按默认版本找。", parse_mode=PM)
        except Exception:
            pass
        return

    _ALT_WAIT[u.id] = aid
    _ALT_TS[u.id] = time.time()
    await cb.answer("请直接发一行版本要求 ✍️")
    tip = "\n".join([
        "📝 <b>标注指定版本</b>", _div(),
        f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 30)}",
        f"📄 {_t(a.get('scope') or '全片', 30)}",
        _div(),
        "直接回复这条消息，写清楚你要的版本，例如：",
        "　<code>2160p HDR 杜比视界 内封简中字幕 国语音轨</code>",
        "　<code>只要 REMUX 原盘，1080p 以下不要</code>",
        "　<code>要国语配音版</code>",
        _div(),
        "⏳ 15 分钟内有效；写错再点一次「📝 备注」就能改。",
    ])
    try:
        await cb.message.reply(
            ui.clip_html(tip), parse_mode=PM, disable_web_page_preview=True,
            reply_markup=KM([[B("🧹 清除备注", f"qn:clear:{aid}")]]))
    except Exception:
        pass


@bot.on_message(filters.text, group=1)
async def on_alt_remark(client, msg: Message):
    """备注输入捕获（group=1，早于 group=2 的通用输入）"""
    u = msg.from_user
    if not u or u.is_bot:
        raise ContinuePropagation
    aid = _ALT_WAIT.get(u.id)
    if not aid:
        raise ContinuePropagation
    if time.time() - (_ALT_TS.get(u.id) or 0) > _ALT_TTL:
        _ALT_WAIT.pop(u.id, None)
        _ALT_TS.pop(u.id, None)
        await msg.reply("⌛ 备注输入已超时，请重新点「📝 备注」", parse_mode=PM)
        return  # 不再向下一组传播
    txt = (msg.text or "").strip()
    if not txt or txt[0] in "/!！":
        raise ContinuePropagation
    _ALT_WAIT.pop(u.id, None)
    _ALT_TS.pop(u.id, None)

    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await msg.reply("⌛ 这条申请已不存在", parse_mode=PM)
        return
    # ★ 版本要求写 remark（不会被状态变更冲掉），note 留一句短的方便列表查看
    await asyncio.to_thread(db.set_audit_remark, aid, txt[:500], int(u.id))
    await asyncio.to_thread(db.set_audit_note, aid, f"版本备注：{txt[:120]}",
                            int(u.id))

    from ..helper import send_group
    await msg.reply(
        ui.clip_html("\n".join([
            "✅ <b>版本备注已收到</b>", _div(),
            f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 30)}",
            f"📝 {_t(txt, 200)}",
            _div(), "⏳ 管理员会按这个版本去找；还能再点「📝 备注」修改。",
        ])), parse_mode=PM, disable_web_page_preview=True,
        reply_markup=_remark_kb(aid))
    await send_group(
        bot,
        ui.clip_html("\n".join([
            "📝 <b>求片已标注指定版本</b>", _div(),
            f"🎬 {_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}",
            f"📄 范围：{_t(a.get('scope') or '全片', 40)}",
            f"📝 版本要求：{_t(txt, 160)}",
            _div(),
            f"👤 {_ucard_one(u.id, u.first_name or '', u.username or '')}",
            "⏳ 等待管理员确认",
        ])), parse_mode=PM, disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^qpok:(\d+):(\d+):(\d+)$"))
async def cb_submit(client, cb):
    u = cb.from_user
    c = _cache_of(u.id)
    if not c:
        await cb.answer("⌛ 会话已过期，请重新 /qp 搜索", show_alert=True)
        return
    _, idx, sea, ep = cb.data.split(":")
    idx, sea, ep = int(idx), int(sea), int(ep)
    r = c["results"][idx]
    scope, n = _scope_txt(r, sea, ep, c, idx)
    # 勾选式选集：把勾中的集写成清单（≥2 集才写），下发时按连续段拆订阅
    eps = _eps_of(c, sea) if sea else []
    ep_list = ",".join(str(x) for x in eps) if len(eps) > 1 else ""
    if eps:
        start_ep, total_ep = eps[0], eps[-1]
    else:
        start_ep, total_ep = ep, (ep or _sea_total(c, idx, sea))

    aid = await asyncio.to_thread(db.add_audit, **{
        "tg": u.id,
        "tg_name": u.first_name or "",
        "tg_username": u.username or "",
        "title": r.get("title") or "",
        "year": r.get("year") or "",
        "mtype": r.get("type") or "",
        "poster": r.get("poster") or "",
        "backdrop": r.get("backdrop") or "",
        "overview": (r.get("overview") or "")[:1500],
        "genres": r.get("genres") or "",
        "vote": str(r.get("vote") or ""),
        "release_date": r.get("release") or "",
        "original": r.get("original") or "",
        "link": r.get("link") or "",
        "tmdb_id": r.get("media_id") or "",
        "media_source": r.get("media_source") or "themoviedb",
        "season": sea, "start_ep": start_ep,
        "total_ep": total_ep,
        "ep_count": n,
        "ep_list": ep_list,
        "scope": scope,
    })
    if not aid:
        await cb.answer("⚠️ 提交失败，请稍后再试", show_alert=True)
        return

    pic = await asyncio.to_thread(
        _fetch_img, r.get("poster") or r.get("backdrop"))
    await _show(cb.message, pic, _submit_caption(r, sea, ep, c, idx, aid),
                _after_kb(aid))
    await cb.answer("📨 已提交，等待管理员确认")

    # ── 求片前先查 Emby：库里已有 → 群里立即通知 + 给三个按钮 ──
    # 命中时**不**直接送审，等求片人/管理员在按钮上选了下一步再走。
    hit = None
    try:
        hit = await asyncio.to_thread(
            _emby_hit, r.get("title") or "", str(r.get("media_id") or ""),
            r.get("type") or "", int(sea or 0))
    except Exception as e:
        logger.debug(f"求片查 Emby 失败（按未命中处理）: {e}")
        hit = None

    # ★ 2026-10-15：只有「请求的集**全都已在库里」才走「库里已有」拦截；
    #   只要还有缺失集（哪怕已收 69 集、缺 13 集），就该照常送审——否则
    #   用户求《逆天邪神》全季 82 集会被一句「库里已有 69 集」拦死。
    #   全有 → 走「库里已有」三选一（取消/继续/更高版本）。
    #   部分有 → 直接送审，求片人已在确认页看过「只求缺集」按钮。
    all_have = bool(hit and hit.get("hit")) and not (c.get("_miss") or [])
    if hit and hit.get("hit") and all_have:
        await asyncio.to_thread(
            db.set_audit_note, aid, "库里已有，等待求片人确认是否继续")
        await _group_have_it(aid, hit)
        # ★ 同时私聊求片人一份（他不一定盯着群，私聊选完就能继续）
        try:
            await _dm_have_it(u.id, aid, hit)
        except Exception as e:
            logger.debug(f"私聊「库里已有」失败: {e}")
        return

    # ★ 部分已有时：给管理员备注里点明「已有 X 集 / 只求缺失 Y 集」，
    #   让审核的人也知道别重复下已有集。
    if hit and hit.get("hit") and (c.get("_miss") or []):
        hn = len(c.get("_have") or [])
        mn = len(c.get("_miss") or [])
        await asyncio.to_thread(
            db.set_audit_note, aid,
            f"库里已有 {hn} 集，本次只求缺失 {mn} 集")

    await _notify_admins(aid)
    await _group_new_req(aid)


async def _notify_admins(aid: int) -> None:
    """把待审核申请私聊发给所有管理员（带图 + 通过/驳回按钮）"""
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        return
    admins = admin_ids()
    if not admins:
        logger.warning("未配置管理员，求片申请无人可审")
        return
    cap = _audit_caption(a)
    kb = _audit_kb(aid, int(a.get("prio") or 0))
    pic = await asyncio.to_thread(_fetch_img, a.get("poster") or a.get("backdrop"))
    for ad in admins:
        try:
            if pic:
                buf = io.BytesIO(pic)
                buf.name = "p.jpg"
                await bot.send_photo(ad, buf, caption=cap, reply_markup=kb, parse_mode=PM)
            else:
                await bot.send_message(ad, cap, reply_markup=kb, parse_mode=PM,
                                       disable_web_page_preview=True)
        except Exception as e:
            logger.debug(f"私聊管理员 {ad} 失败: {e}")


async def _group_new_req(aid: int) -> None:
    """群里播报一条「谁求了什么，等待审核」"""
    from ..helper import send_group, send_group_photo
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        return
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    lines = [
        "📨 <b>新求片 · 等待管理员确认</b>", _div(),
    ]
    lines += _ucard(a.get('tg'), a.get('tg_name') or '',
                    a.get('tg_username') or '', "求片人")
    lines += [
        f"🎬 影片：{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}{y}",
        f"📄 范围：{_t(a.get('scope') or '全片', 40)}",
        _div(),
        f"🆔 申请号：#{aid}　⏳ 管理员确认后才会开始下载",
    ]
    text = ui.clip_html("\n".join(lines))
    # ★ 群播报常驻「📝 备注」+「📮 投稿」：谁手里有资源都能直接投
    kb = _remark_kb(aid, extra=[[x] for x in (_ctb_btn(aid),) if x])
    pic = await asyncio.to_thread(_fetch_img, a.get("poster") or a.get("backdrop"))
    if pic:
        if not await send_group_photo(bot, pic, text, parse_mode=PM, reply_markup=kb):
            await send_group(bot, text, parse_mode=PM,
                         disable_web_page_preview=True, reply_markup=kb)
    else:
        await send_group(bot, text, parse_mode=PM,
                         disable_web_page_preview=True, reply_markup=kb)


@bot.on_callback_query(filters.regex(r"^qa:(ok|no|prio):(\d+)$"))
async def cb_audit(client, cb):
    u = cb.from_user
    if not is_admin(u.id):
        await cb.answer("⛔ 只有管理员能审核", show_alert=True)
        return
    act, aid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await cb.answer("申请不存在", show_alert=True)
        return

    # ⭐ 优先处理：置顶到待审核面板顶部，并立刻触发一轮搜索
    if act == "prio":
        cur = int(a.get("prio") or 0)
        new = 0 if cur else 2
        await asyncio.to_thread(db.set_audit_prio, aid, new)
        try:
            await cb.message.edit_reply_markup(
                reply_markup=_audit_kb(aid, new))
        except Exception:
            pass
        await cb.answer("⭐ 已标为优先，会排在待审核列表最前" if new
                        else "已取消优先", show_alert=True)
        return

    if a.get("status") != "auditing":
        await cb.answer(f"该申请已处理（{a.get('status')}）", show_alert=True)
        return

    if act == "no":
        await asyncio.to_thread(db.set_audit, aid, "rejected", u.id, "管理员驳回")
        await cb.answer("已驳回")
        await _edit_txt(cb.message,
                        _audit_caption(a) + "\n" + _div() + "\n❌ <b>已驳回</b>",
                        None)
        await _tell_user(a, False, u)
        return

    await cb.answer("✅ 已通过，正在推送 MoviePilot…")
    await asyncio.to_thread(db.set_audit, aid, "approved", u.id, "管理员通过")

    # ★ 走全项目唯一的推送实现（reqmgr._approve 也用这一个），
    #   勾选式选集 / 扣费 / 求片清单 / 立即搜索 全在里面，不会再各跑各的。
    ok, note, sub_id, n_sub, kicked = await push_audit_to_mp(a)
    if ok:
        await asyncio.to_thread(db.set_audit, aid, "done", u.id, note, sub_id)
        await asyncio.to_thread(db.clear_idle_notified, aid)
        tail = "⚡ 已立即触发 MoviePilot 搜索下载" if kicked \
            else "⏳ 已加入订阅，等下一轮自动搜索"
        if n_sub > 1:
            tail = f"🧩 已按连续段拆成 <b>{n_sub}</b> 条订阅　" + tail
    else:
        await asyncio.to_thread(db.set_audit, aid, "failed", u.id, note)
        tail = f"⚠️ 推送失败：{_t(note, 60)}"

    await _edit_txt(cb.message,
                    _audit_caption(a) + "\n" + _div() + "\n"
                    + (f"✅ <b>已通过</b>　{tail}" if ok
                       else f"⚠️ <b>通过但推送失败</b>　{tail}"),
                    None)
    await _tell_user(a, ok, u, note)
    if ok:
        await _group_approved(a, u)


async def _tell_user(a: dict, ok: bool, admin, note: str = "") -> None:
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    head = "✅ <b>求片已通过</b>" if ok else (
        "❌ <b>求片被驳回</b>" if a.get("status") == "rejected"
        else "⚠️ <b>求片未能下发</b>")
    lines = [
        head, _div(),
        f"🎬 影片：{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}{y}",
        f"📄 范围：{_t(a.get('scope') or '全片', 40)}",
    ]
    lines += _ucard(admin.id, admin.first_name or '', admin.username or '',
                    "审核", "🛠")
    if note:
        lines.append(f"📌 结果：{_t(note, 50)}")
    lines.append(_div())
    lines.append("📥 已推送 MoviePilot 搜索下载，下好会自动传 115 并入库"
                 if ok else "💡 换个关键词再试，或联系管理员人工补齐")
    try:
        await bot.send_message(int(a["tg"]), ui.clip_html("\n".join(lines)),
                               parse_mode=PM, disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"通知求片者失败 {a.get('tg')}: {e}")


async def _group_approved(a: dict, admin) -> None:
    from ..helper import send_group, send_group_photo
    y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
    lines = [
        "✅ <b>求片已通过 · 开始下载</b>", _div(),
    ]
    lines += _ucard(a.get('tg'), a.get('tg_name') or '',
                    a.get('tg_username') or '', "求片人")
    lines += _ucard(admin.id, admin.first_name or '', admin.username or '',
                    "审核", "🛠")
    lines += [
        # ★ 2026-10-13 用户要求：开始下载时群里自动 @ 求片人
        #   （有 TG 用户名 → 真 @ 会通知到他；没有则链到他的主页）
        f"📣 {_mention(a.get('tg'), a.get('tg_name') or '', a.get('tg_username') or '')}"
        f"　你求的片已开始下载 ⬇️",
        f"🎬 影片：{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 34)}{y}",
        f"📄 范围：{_t(a.get('scope') or '全片', 40)}",
        _div(),
        "⚡ 已推送 MoviePilot，进度用 /dl 查看",
    ]
    text = ui.clip_html("\n".join(lines))
    pic = await asyncio.to_thread(_fetch_img, a.get("poster") or a.get("backdrop"))
    if pic:
        if not await send_group_photo(bot, pic, text, parse_mode=PM):
            await send_group(bot, text, parse_mode=PM,
                         disable_web_page_preview=True)
    else:
        await send_group(bot, text, parse_mode=PM,
                         disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^qa:list$"))
async def cb_audit_list(client, cb):
    await cb.answer()
    rows = await asyncio.to_thread(db.list_audits, "auditing", 10)
    if not rows:
        await _edit_txt(cb.message, "📭 当前没有待审核的求片", None)
        return
    lines = [f"📋 <b>待审核求片 · {len(rows)} 条</b>", _div()]
    for a in rows:
        y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
        star = "⭐ " if int(a.get("prio") or 0) else ""
        lines.append(f"{star}#{a['id']}　🎬 "
                     f"{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 24)}{y}")
        lines += _ucard(a.get('tg'), a.get('tg_name') or '',
                        a.get('tg_username') or '', indent="　 ")
        lines.append(f"　 📄 {_t(a.get('scope') or '全片', 20)}")
        if str(a.get("remark") or "").strip():
            lines.append(f"　 📝 {_t(a['remark'], 24)}")
    lines.append(_div())
    lines.append("💡 用 /reqs 或 /reqmgr 打开管理台直接操作")
    await _edit_txt(cb.message, ui.clip_html("\n".join(lines), limit=3500), None)


@bot.on_callback_query(filters.regex(r"^qpmine$"))
async def cb_mine(client, cb):
    await cb.answer()
    await cb.message.reply(await _mine_text(cb.from_user.id), parse_mode=PM,
                           disable_web_page_preview=True)


# ═══════════════════════════════════════════════ 命令
_STATUS_TXT = {
    "auditing": "⏳ 待审核",
    "approved": "✅ 已通过",
    "rejected": "❌ 已驳回",
    "done": "🚀 已下发下载",
    "failed": "⚠️ 下发失败",
}


async def _mine_text(uid: int) -> str:
    rows = await asyncio.to_thread(db.my_audits, uid, 10)
    if not rows:
        return ("📋 <b>我的求片申请</b>\n" + _div() + "\n还没有求片记录\n"
                + _div() + "\n💡 直接发片名，或 /qp 片名 开始求片")
    lines = [f"📋 <b>我的求片申请 · {len(rows)} 条</b>", _div()]
    for a in rows:
        y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
        st = _STATUS_TXT.get(a.get("status") or "", a.get("status") or "")
        lines.append(f"#{a['id']}　{st}　🎬 <b>"
                     f"{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 22)}"
                     f"</b>{y}")
        lines.append(f"　 📄 {_t(a.get('scope') or '全片', 30)}")
    lines.append(_div())
    lines.append("💡 用 /dl 查看下载进度")
    return ui.clip_html("\n".join(lines))


@bot.on_message(filters.command(["myreqs", "myqa", "我的申请", "求片记录"], **CMD))
async def cmd_myreqs(client, msg: Message):
    await msg.reply(await _mine_text(msg.from_user.id), parse_mode=PM,
                   disable_web_page_preview=True)


@bot.on_message(filters.command(["reqs", "审核", "待审核", "audit", "待审核求片"],
                                **CMD))
@perm.gate("reqaudit")
async def cmd_reqs(client, msg: Message):
    """查看待审核求片（需「求片审核」权限）

    ★ 2026-10-13：不再只吐一段**纯文本列表**（没法点按钮），
      直接打开求片管理台的「⏳ 待审核」面板 —— 群里也能开，
      通过 / 驳回 / ⭐优先 / 🔁重复下发 全在点一下的事。
    """
    try:
        from .reqmgr import _panel, ST_PENDING
        text, kb = await _panel(ST_PENDING, group_mode=True)
    except Exception as e:
        logger.debug(f"打开待审核面板失败，退回纯文本: {e}")
        rows = await asyncio.to_thread(db.list_audits, "auditing", 15)
        if not rows:
            await msg.reply("📭 当前没有待审核的求片", parse_mode=PM)
            return
        lines = [f"📋 <b>待审核求片 · {len(rows)} 条</b>", _div()]
        for a in rows:
            y = f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""
            star = "⭐ " if int(a.get("prio") or 0) else ""
            lines.append(f"{star}#{a['id']}　🎬 <b>"
                         f"{_tlink(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 24)}"
                         f"</b>{y}")
            lines += _ucard(a.get('tg'), a.get('tg_name') or '',
                            a.get('tg_username') or '', indent="　 ")
            lines.append(f"　 📄 {_t(a.get('scope') or '全片', 30)}")
            lines.append(_div())
        await msg.reply(ui.clip_html("\n".join(lines), limit=3500), parse_mode=PM,
                        disable_web_page_preview=True)
        return
    await msg.reply(text, reply_markup=kb, parse_mode=PM,
                    disable_web_page_preview=True)


# ═══════════════════════════════════════════════ /mystatus（兼容旧命令）
@bot.on_message(filters.command(["mystatus"], **CMD))
async def cmd_mystatus(client, msg: Message):
    await msg.reply(await _mine_text(msg.from_user.id), parse_mode=PM,
                   disable_web_page_preview=True)
