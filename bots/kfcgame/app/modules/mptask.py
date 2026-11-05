"""管理员：PT 任务面板

设计目标（按群主要求重做）：
  1. **一条任务一张卡片**：第几季第几集、状态、**实时进度条**、海报全部摆出来
  2. **一屏看全**：当前页所有任务的海报合成一张总览图，下面再配文字明细
  3. **勾选优先下载**：每条任务就是一个 **✅/⬜ 勾选开关**，点一下切换；
     点「✅ 只下载勾选」后 —— 勾中的 resume，没勾的 pause，
     ⇒ **只有被勾选的任务才真正下载**，方便手动指定优先下载的片
  4. **全局控制**：⏸ 全部暂停 / ▶️ 全部开始（在「清空所有下载」
     与「清空全部订阅」之外新增），都带二次确认
  5. **进度条自己走**：面板每 20 秒自动刷新一次，不用一直点刷新
  6. **点一下就生效**：全部走按钮，不用手打 ID

/ptasks   打开任务面板（管理员）
/ptdel    兼容旧习惯的批量删除：/ptdel all 或 /ptdel 3,5,7
"""
from __future__ import annotations

import asyncio
import io
import re
import time

import requests
from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import InputMediaPhoto
from pyrogram.types import Message

# 同 request.py：本模块文案用 HTML 标签，必须显式指定 parse_mode
PM = ParseMode.HTML

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from .inbox import QB_STATE
from .inbox import eta_short
from .inbox import hum_eta
from .inbox import hum_size
from .inbox import hum_speed
from .request import _fetch_img
from .request import is_admin
from .request import mp_sub_ids
from .request import mp_sub_ids_fresh
from . import perm
from .request import mp_recognize
from .request import mp_sub_url
from .request import mp_subscribe_del
from .request import mp_subscribe_list
from .request import push_audit_to_mp

CMD = dict(prefixes=S.PREFIX)

_QB = S.CFG.get("qb") or {}
QB_URL = (_QB.get("url") or "http://127.0.0.1:8080").rstrip("/")
QB_USER = _QB.get("username") or ""
QB_PASS = _QB.get("password") or ""

_FONT = "/app/fonts/PingFangBold.ttf"

_PAGE_SIZE = 8
_JOBS: dict = {"ts": 0.0, "list": []}
_JOBS_TTL = 15

_SUB_STATE = {"R": "订阅中", "N": "新建", "P": "已暂停"}

# ───────────────────────────────────────────── 勾选状态
# uid -> 已勾选任务的 key 集合。用「订阅 ID / 种子 hash」当 key，
# 这样翻页、刷新、任务增减之后勾选状态都不会串位。
_SEL: dict[int, set[str]] = {}

# 需要定时自动刷新的面板消息： (chat, msg) -> (是否图片, ts, 页码, 管理员 uid)
_LIVE: dict[tuple[int, int], tuple[bool, float, int, int]] = {}
_LIVE_TTL = 1800


def _jkey(j: dict) -> str:
    """一条任务的稳定标识（勾选状态靠它存活）"""
    if j.get("kind") == "arc":
        return f"arc:{j.get('id') or 0}"
    if j.get("kind") == "sub":
        return f"sub:{j.get('id') or 0}"
    h = ""
    for t in (j.get("tors") or []):
        if t.get("hash"):
            h = str(t["hash"])
            break
    return f"tor:{h}" if h else f"tor:{_norm(j.get('name'))[:24]}"


def _sel_of(uid) -> set[str]:
    try:
        u = int(uid or 0)
    except Exception:
        u = 0
    return _SEL.setdefault(u, set())


def _esc(s) -> str:
    """HTML 转义。必须**先转义再截断**：先截断会把 &lt; 砍成 &l 这种半成品实体，
    导致 Telegram 报 Can't parse entities，整条消息发不出去。"""
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _t(s, n: int = 60) -> str:
    return _esc(s)[:n]


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


# ═════════════════════════════════════════════ qBittorrent
def qb_session() -> requests.Session | None:
    s = requests.Session()
    try:
        r = s.post(f"{QB_URL}/api/v2/auth/login",
                   data={"username": QB_USER, "password": QB_PASS}, timeout=10)
        if r.status_code == 200 and r.text.strip() == "Ok.":
            return s
        if "Fails" in r.text or r.status_code >= 400:
            return None
        return s
    except Exception as e:
        logger.debug(f"QB 登录失败: {e}")
        return None


def qb_torrents() -> list[dict]:
    s = qb_session()
    if not s:
        return []
    try:
        r = s.get(f"{QB_URL}/api/v2/torrents/info", timeout=15)
        return [t for t in r.json() if isinstance(t, dict)]
    except Exception as e:
        logger.debug(f"QB 取种子失败: {e}")
        return []


def qb_delete(hashes: list[str], with_files: bool = True) -> int:
    if not hashes:
        return 0
    s = qb_session()
    if not s:
        return 0
    try:
        r = s.post(f"{QB_URL}/api/v2/torrents/delete", timeout=20,
                   data={"hashes": "|".join(hashes),
                         "deleteFiles": "true" if with_files else "false"})
        return len(hashes) if r.status_code == 200 else 0
    except Exception as e:
        logger.debug(f"QB 删除种子失败: {e}")
        return 0


def qb_ctrl(path: str, hashes: list[str]) -> int:
    """暂停 / 恢复指定种子（qB 5.x 成功返回 204，200 也算成功）。

    ⚠️ 重大坑（2026-10-12 实测修复）：
      qBittorrent **5.x（本机 v5.2.3 / WebAPI 2.15.1）已经删掉了**
      `/api/v2/torrents/pause` 和 `/api/v2/torrents/resume`，改为
      `/api/v2/torrents/stop` 与 `/api/v2/torrents/start`。
      老端点会返回 **404「端点不存在」**，而老代码只判断 200/204 ⇒ 静默返回 0，
      表现就是「点了暂停/恢复，什么都没发生」。
    ⇒ 这里做**双端点自动降级**：新版优先，404 自动回到旧版，两个版本都能用。
    """
    if not hashes:
        return 0
    s = qb_session()
    if not s:
        return 0
    # 同一语义的新旧端点对照表
    _ALT = {"/api/v2/torrents/pause": "/api/v2/torrents/stop",
            "/api/v2/torrents/resume": "/api/v2/torrents/start",
            "/api/v2/torrents/stop": "/api/v2/torrents/pause",
            "/api/v2/torrents/start": "/api/v2/torrents/resume"}
    tries = [path]
    alt = _ALT.get(path)
    if alt:
        tries.append(alt)
    last = 0
    for p in tries:
        try:
            r = s.post(f"{QB_URL}{p}", timeout=20,
                       data={"hashes": "|".join(hashes)})
            if r.status_code in (200, 204):
                return len(hashes)
            last = r.status_code
        except Exception as e:
            logger.debug(f"QB {p} 失败: {e}")
    logger.debug(f"QB {path} 全部端点失败，最后状态码 {last}")
    return 0


def qb_pause(hashes: list[str]) -> int:
    """暂停种子（自动适配 qB 5.x 的 stop 端点）"""
    return qb_ctrl("/api/v2/torrents/stop", hashes)


def qb_resume(hashes: list[str]) -> int:
    """恢复种子（自动适配 qB 5.x 的 start 端点）"""
    return qb_ctrl("/api/v2/torrents/start", hashes)


def qb_all(path: str) -> bool:
    """对**全部**种子执行 pause / resume（hashes=all）。

    ⚠️ 同样适配 qB 5.x：/torrents/pause→/stop、/resume→/start，
    老端点 404 时自动降级到新端点。
    """
    _ALT = {"/api/v2/torrents/pause": "/api/v2/torrents/stop",
            "/api/v2/torrents/resume": "/api/v2/torrents/start",
            "/api/v2/torrents/stop": "/api/v2/torrents/pause",
            "/api/v2/torrents/start": "/api/v2/torrents/resume"}
    s = qb_session()
    if not s:
        return False
    tries = [path] + ([_ALT[path]] if path in _ALT else [])
    last = 0
    for p in tries:
        try:
            r = s.post(f"{QB_URL}{p}", timeout=20, data={"hashes": "all"})
            if r.status_code in (200, 204):
                return True
            last = r.status_code
        except Exception as e:
            logger.debug(f"QB {p} all 失败: {e}")
    logger.debug(f"QB {path} all 全部端点失败，最后状态码 {last}")
    return False


# ═════════════════════════════════════════════ 任务清单
def _norm(s) -> str:
    """归一化：只留字母数字汉字，用于弱匹配"""
    return re.sub(r"[^0-9a-z一-鿿]+", "", str(s or "").lower())


def _guess_sea_ep(name: str) -> tuple[int | None, int | None]:
    """从种子名里猜（季, 集）；猜不出来返回 (None, None)"""
    n = " " + re.sub(r"[._\[\]()（）【】]", " ", str(name or "")) + " "
    m = re.search(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})", n)          # S01E09
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"[Ss]eason\s*(\d{1,2})", n, re.I)            # Season 1
    if m:
        return int(m.group(1)), None
    m = re.search(r"第\s*(\d{1,2})\s*季", n)                     # 第1季
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?<![0-9A-Za-z])[Ss](\d{1,2})(?![0-9A-Za-z])", n)
    if m and 0 < int(m.group(1)) < 40:                          # S01（整季包）
        return int(m.group(1)), None
    m = re.search(r"第\s*(\d{1,3})\s*集", n)
    if m:
        return None, int(m.group(1))
    return None, None


def _sub_job(s: dict) -> dict:
    return {
        "kind": "sub",
        "id": int(s.get("id") or 0),
        "hash": "",
        "name": str(s.get("name") or "").strip(),
        "year": str(s.get("year") or ""),
        "type": str(s.get("type") or ""),
        "season": int(s.get("season") or 0) or None,
        "start": int(s.get("start_episode") or 0) or None,
        "total": int(s.get("total_episode") or 0) or None,
        "lack": int(s.get("lack_episode") or 0),
        "state": str(s.get("state") or ""),
        "img": mp_sub_url(s.get("poster")) or mp_sub_url(s.get("backdrop"), "w500"),
        "tmdb": str(s.get("tmdb_id") or s.get("media_id") or ""),
        "tors": [],
    }


def _tor_job(t: dict, rc: dict) -> dict:
    sea, ep = _guess_sea_ep(str(t.get("name") or ""))
    return {
        "kind": "tor",
        "id": 0,
        "hash": str(t.get("hash") or ""),
        "name": (rc.get("title") or str(t.get("name") or "")).strip(),
        "year": str(rc.get("year") or ""),
        "type": rc.get("type") or "",
        "season": sea,
        "start": ep,
        "total": None,
        "lack": 0,
        "state": "",
        "img": mp_sub_url(rc.get("poster")) if rc.get("poster") else "",
        "tmdb": str(rc.get("tmdb_id") or ""),
        "tors": [t] if t.get("hash") else [],
    }


def _linked(job: dict, t: dict, rc: dict) -> bool:
    """订阅 ↔ 种子是否说的是同一部"""
    sid = str(job.get("tmdb") or "")
    rid = str(rc.get("tmdb_id") or "")
    if sid and rid and sid == rid:
        jsea = job.get("season")
        if jsea and t.get("_sea") and jsea != t["_sea"]:
            return False
        return True
    a, b = _norm(job.get("name")), _norm(rc.get("title"))
    if a and b and (a in b or b in a):
        return True
    a2 = _norm(job.get("name"))
    return bool(a2) and a2 in _norm(t.get("name"))


async def jobs(force: bool = False) -> list[dict]:
    """订阅 + 未关联下载，合成任务清单（15 秒内复用，别每次点按钮都打一遍接口）"""
    if not force and _JOBS["list"] and time.time() - _JOBS["ts"] < _JOBS_TTL:
        return _JOBS["list"]

    subs, tors = await asyncio.gather(
        asyncio.to_thread(mp_subscribe_list, 50),
        asyncio.to_thread(qb_torrents),
    )
    recogs: list[dict] = []
    for t in tors:
        recogs.append(await asyncio.to_thread(mp_recognize, str(t.get("name") or "")))
        sea, _ep = _guess_sea_ep(str(t.get("name") or ""))
        t["_sea"] = sea

    used: set[str] = set()
    out: list[dict] = []
    for s in subs:
        job = _sub_job(s)
        for t, rc in zip(tors, recogs):
            h = str(t.get("hash") or "")
            if h in used or not h:
                continue
            if _linked(job, t, rc):
                job["tors"].append(t)
                used.add(h)
        if not job["img"]:                       # 订阅没海报就借用已识别种子的
            for t, rc in zip(tors, recogs):
                if t in job["tors"] and rc.get("poster"):
                    job["img"] = mp_sub_url(rc["poster"])
                    break
        out.append(job)

    for t, rc in zip(tors, recogs):
        h = str(t.get("hash") or "")
        if not h or h in used:
            continue
        out.append(_tor_job(t, rc))

    out += await _archived_jobs()

    _JOBS["ts"] = time.time()
    _JOBS["list"] = out
    return out


async def _archived_jobs(limit: int = 10) -> list[dict]:
    """已下发、但 MoviePilot 订阅已被移除的求片（= 已完成归档）。

    ★ 用户 2026-10-11 反馈「求片通过审核后，任务列表里的记录又消失了」的修复。
      根因：MoviePilot 在**订阅完成**时会直接删掉 subscribe 行
      （源码 app/application/subscription/complete.py → stage_delete_sync），
      而本面板原来只从 MP 订阅列表取数据 ⇒ 订阅一完成，这一行就凭空消失，
      管理员以为求片没成功。
      现在把这类求片补回面板，标成「✅ 已完成并归档」，记录不会再消失。
    """
    try:
        rows = await asyncio.to_thread(db.list_audits, "done", 100)
    except Exception as e:
        logger.debug(f"读取已归档求片失败: {e}")
        return []
    live = await asyncio.to_thread(mp_sub_ids)
    ok = await asyncio.to_thread(mp_sub_ids_fresh)
    if not ok:
        return []                       # MP 取不到 ⇒ 别乱标"已完成"
    out: list[dict] = []
    for a in rows:
        try:
            sid = int(a.get("sub_id") or 0)
        except Exception:
            sid = 0
        if not sid or sid in live:
            continue                    # 没有订阅号 / 订阅还在 ⇒ 不算归档
        out.append({
            "kind": "arc",
            "id": sid,
            "aid": int(a.get("id") or 0),      # 原始申请号（重新下载时要用）
            "hash": "",
            "name": str(a.get("title") or ""),
            "year": str(a.get("year") or ""),
            "type": str(a.get("mtype") or ""),
            "season": int(a.get("season") or 0) or None,
            "start": int(a.get("start_ep") or 0) or None,
            "total": int(a.get("total_ep") or 0) or None,
            "lack": 0,
            "state": "",
            "img": "",
            "tmdb": str(a.get("tmdb_id") or ""),
            "tors": [],
        })
        if len(out) >= limit:
            break
    return out


# ═════════════════════════════════════════════ 文案
def _range_txt(j: dict) -> str:
    """求片范围：第几季第几集"""
    if j["kind"] == "tor":
        sea, ep = j.get("season"), j.get("start")
        if sea and ep:
            return f"第 {sea} 季 · 第 {ep} 集"
        if sea:
            return f"第 {sea} 季（整包）"
        if ep:
            return f"第 {ep} 集"
        return "整包 / 电影"
    if (j.get("type") or "") == "电视剧":
        s, st, tot = j.get("season"), j.get("start"), j.get("total")
        if s and st and tot:
            if st == tot:
                return f"第 {s} 季 · 第 {st} 集"
            return f"第 {s} 季 · 第 {st}-{tot} 集"
        if s and tot:
            return f"第 {s} 季 · 全集（共 {tot} 集）"
        if s:
            return f"第 {s} 季 · 全集"
        return "全季全集 · 自动追新"
    return "全片（单部电影）"


def _tor_line(t: dict) -> str:
    """单个种子的下载状态行（带进度条）"""
    p = float(t.get("progress") or 0) * 100
    st = QB_STATE.get(t.get("state") or "", t.get("state") or "")
    spd = int(t.get("dlspeed") or 0)
    bits = [f"[{ui.bar(p, 10)}] {p:.0f}%"]
    sz = hum_size(t.get("size"))
    if sz:
        bits.append(sz)
    line = f"{'　'.join(bits)}"
    if spd:
        line += f" ⚡{hum_speed(spd)}"
    elif st:
        line += f" {st}"
    if p < 100 and spd:
        eta = int(t.get("eta") or 0)
        if 0 < eta < 8640000:
            line += f" ⏳{hum_eta(eta)}"
    return line


def _prog_txt(j: dict) -> str:
    """实时进度条（面板主视觉）：[████░░░░░░] 42%　⚡3.2 MB/s　⏳12分

    ★ 与「在线播报 / /dl 面板」同款（█ 实心 + ░ 空心，宽 10）；
      没有速度就不显示剩余时间 —— 静止种子算出「>2天」纯属误导。
    """
    tors = [t for t in (j.get("tors") or []) if t.get("hash")]
    if not tors:
        return "暂无下载"
    best = max(tors, key=lambda x: float(x.get("progress") or 0))
    p = float(best.get("progress") or 0) * 100
    line = f"[{ui.bar(p, 10)}] {p:.0f}%"
    spd = int(best.get("dlspeed") or 0)
    if spd:
        line += f"　⚡{hum_speed(spd)}"
        e = eta_short(best.get("eta"))
        if e:
            line += f"　⏳{e}"
    else:
        st = QB_STATE.get(best.get("state") or "", "")
        if st:
            line += f"　{st}"
    if len(tors) > 1:
        line += f"　共 {len(tors)} 个种子"
    return line


def _status_txt(j: dict) -> str:
    """整体状态：订阅状态 + 有没有在下载 + 进度速度"""
    if j.get("kind") == "arc":
        return "✅ 已完成并归档"
    bits: list[str] = []
    if j["kind"] == "sub":
        bits.append(_SUB_STATE.get(j.get("state") or "", j.get("state") or "订阅中"))
        if j.get("lack"):
            bits.append(f"缺 {j['lack']} 集")
    tors = [t for t in (j.get("tors") or []) if t.get("hash")]
    if tors:
        best = max(tors, key=lambda x: float(x.get("progress") or 0))
        bits.append(_tor_line(best))
        if len(tors) > 1:
            bits.append(f"共 {len(tors)} 个种子")
    else:
        bits.append("暂无下载")
    return "　·　".join(bits)


def _btn_name(j: dict) -> str:
    n = str(j.get("name") or "未知")
    return n[:7] + ("…" if len(n) > 7 else "")


def _ov_text(jl: list[dict], page: int, pages: int, items: list[dict],
             uid: int = 0) -> str:
    n_sub = sum(1 for j in jl if j["kind"] == "sub")
    n_arc = sum(1 for j in jl if j["kind"] == "arc")
    n_tor = len(jl) - n_sub - n_arc
    n_dl = sum(1 for j in jl if j.get("tors"))
    sel = _sel_of(uid) if uid else set()
    n_sel = sum(1 for j in jl if _jkey(j) in sel)
    # 全局总速度：只统计真正在下载的种子 —— 一屏就能看出"整机现在跑多快"
    tot_spd = sum(int(t.get("dlspeed") or 0)
                  for j in jl for t in (j.get("tors") or []))
    head = (f"📌 订阅 <b>{n_sub}</b>　📥 下载 <b>{n_tor}</b>"
            + (f"　✅ 已归档 <b>{n_arc}</b>" if n_arc else "")
            + (f"　🔗 配对 <b>{n_dl}</b>" if n_sub else ""))
    stat = f"{ui.ON} 已勾选 <b>{n_sel}</b>/{len(jl)}"
    if tot_spd:
        stat += f"　⚡ 总速 <b>{hum_speed(tot_spd)}</b>"
    lines = [
        f"🛠 <b>PT 任务面板</b>",
        _div(),
        head,
        stat,
        _div(),
    ]
    if not jl:
        lines.append("🎉 当前没有任何订阅和下载任务")
        lines.append(_div())
        lines.append("💡 用 /qp 片名 求一部片试试")
        return ui.clip_html("\n".join(lines))

    base = page * _PAGE_SIZE
    for k, j in enumerate(items, 1):
        idx = base + k
        y = f"（{j['year']}）" if j.get("year") else ""
        tag = "📺" if (j.get("type") or "") == "电视剧" else "🎬"
        # 片名一律做成可点的 TMDB 超链接（点一下直达该片详情页）
        tl = ui.title_link(j.get("name"), j.get("tmdb"), j.get("type"), 20)
        if j.get("kind") == "arc":
            # ★ 已完成归档：记录必须还在（这正是用户反馈"消失"的那一类）
            lines.append(f"{ui.ON}<b>{idx}.</b> {tag} <b>{tl}</b>{_t(y, 8)}")
            lines.append("　　✅ <b>已完成并归档</b>　MoviePilot 已自动移除订阅")
            lines.append(f"　　📄 范围：{_t(_range_txt(j), 26)}"
                         f"　📌 订阅 #{j['id']}（已归档）")
            continue
        mark = ui.mark(_jkey(j) in sel)
        lines.append(f"{mark}<b>{idx}.</b> {tag} <b>{tl}</b>{_t(y, 8)}")
        lines.append(f"　　📶 进度：{_t(_prog_txt(j), 46)}")
        lines.append(f"　　📄 范围：{_t(_range_txt(j), 26)}"
                     f"　📊 {_t(_status_txt(j), 24)}")
        # ★ 来源行只在「有订阅」时才写全 —— 纯下载任务占多数，8 条各写一遍
        #   「仅下载（无订阅）」要白占 100 多字，把面板挤到 1024 上限之外。
        lines.append(f"　　📌 订阅 #{j['id']}" if j["kind"] == "sub"
                     else "　　📌 仅下载")
    lines.append(_div())
    # ★ 尾部三行是「怎么用」的关键提示，**必须保留**：
    #   实测 8 条任务时全文 1047 字，超出 Telegram 1024 上限 23 字，
    #   直接截断会把「每 20 秒自动刷新进度」这行砍掉 —— 用户就不知道面板会自己动。
    #   做法：先给尾部预留额度，再让正文在剩余空间里整行取舍。
    tail: list[str] = []
    if pages > 1:
        tail.append(f"📄 第 <b>{page + 1}</b> / {pages} 页，用 ⬅️ ➡️ 翻页")
    tail.append("💡 点任务按钮可<b>勾选/取消</b>；点「✅ 只下载勾选」"
                "就只让勾中的任务跑，其余全部暂停")
    if n_arc:
        tail.append(f"📦 归档 <b>{n_arc}</b> 条已完成（点开可看详情 / 重新下载）")
    tail.append(f"🕒 {time.strftime('%H:%M')}　（每 20 秒自动刷新进度）")
    tail_txt = "\n".join(tail)
    room = max(200, 1024 - ui.visible_len(tail_txt) - 1)
    return ui.clip_html("\n".join(lines), room) + "\n" + tail_txt


def _confirm_text(j: dict, idx: int) -> str:
    tl = ui.title_link(j.get("name"), j.get("tmdb"), j.get("type"), 26)
    lines = [
        "⚠️ <b>确认取消这条任务？</b>",
        _div(),
        f"🎬 影片：<b>{tl}</b>" + (_t(f"（{j['year']}）", 8) if j.get("year") else ""),
        f"📄 范围：{_t(_range_txt(j), 30)}",
        f"📶 进度：{_t(_prog_txt(j), 46)}",
        f"📌 来源：{_t('MP 订阅 #' + str(j['id']) if j['kind'] == 'sub' else '仅下载（无订阅）', 24)}",
    ]
    tors = [t for t in (j.get("tors") or []) if t.get("hash")]
    if tors:
        lines.append(f"🗑 将同时删除 qBittorrent 种子 <b>{len(tors)}</b> 个（含文件）")
    lines.append(_div())
    if j["kind"] == "sub":
        lines.append("取消后该订阅会被删除，下一轮不会再自动搜索")
    else:
        lines.append("取消后只会删除下载种子，没有对应订阅，不会再次下载")
    lines.append(f"🆔 任务序号：<b>#{idx}</b>")
    return ui.clip_html("\n".join(lines))


# ═════════════════════════════════════════════ 总览拼图
def _font(size: int):
    try:
        return ImageFont.truetype(_FONT, size)
    except Exception:
        return ImageFont.load_default()


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    sw, sh = im.size
    if sw == 0 or sh == 0:
        return im.resize((w, h))
    scale = max(w / sw, h / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    return im.crop(((nw - w) // 2, (nh - h) // 2,
                    (nw - w) // 2 + w, (nh - h) // 2 + h))


def _job_collage(items: list[dict]) -> bytes | None:
    """把当前页任务的海报拼成一张总览图：序号角标 + 片名 + 季集"""
    n = len(items)
    if not n:
        return None
    pics = [_fetch_img(j.get("img") or "") for j in items]
    if not any(pics):
        return None

    cols = 1 if n == 1 else (2 if n <= 4 else 3)
    cw, ch, lab, pad = 300, 430, 74, 10
    rows = (n + cols - 1) // cols
    W = cols * cw + pad * (cols + 1)
    H = rows * (ch + lab) + pad * (rows + 1)
    canvas = Image.new("RGB", (W, H), (16, 17, 22))
    dr = ImageDraw.Draw(canvas)
    ft_t = _font(21)
    ft_s = _font(18)
    ft_i = _font(22)

    def clip(t: str, mx: int) -> str:
        while dr.textlength(t, font=ft_t) > mx and len(t) > 3:
            t = t[:-2]
        return t

    for i, data in enumerate(pics):
        j = items[i]
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
            dr.text((x + 16, y + 20), "无海报", font=ft_t, fill=(150, 152, 160))

        # 序号角标（和文字列表里的编号一一对应）
        bx, by, br = x + 24, y + 24, 21
        dr.ellipse([bx - br, by - br, bx + br, by + br], fill=(232, 74, 95))
        tw = dr.textlength(str(i + 1), font=ft_i)
        dr.text((bx - tw / 2, by - 15), str(i + 1), font=ft_i, fill=(255, 255, 255))

        # 底部标题条：片名 + 季/集
        ty = y + ch
        dr.rectangle([x, ty, x + cw, ty + lab], fill=(26, 27, 33))
        title = str(j.get("name") or "")
        if j.get("year"):
            title += f"（{j['year']}）"
        dr.text((x + 10, ty + 8), clip(title, cw - 20), font=ft_t,
                fill=(238, 240, 246))
        sub = f"{_range_txt(j)}　·　{_status_txt(j)}"
        while dr.textlength(sub, font=ft_s) > cw - 20 and len(sub) > 4:
            sub = sub[:-2]
        dr.text((x + 10, ty + 40), sub, font=ft_s, fill=(150, 155, 170))

    bio = io.BytesIO()
    canvas.save(bio, "JPEG", quality=88, optimize=True)
    return bio.getvalue()


# ═════════════════════════════════════════════ 键盘
def _ov_kb(jl: list[dict], page: int, pages: int,
           items: list[dict], uid: int = 0) -> KM:
    """面板键盘：任务按钮 = **勾选开关**（点一下切换 ✅/⬜）

    只有被勾选的任务才会真正下载（点「✅ 只下载勾选」时：
    勾中的 resume，没勾的 pause），方便手动指定优先下载的片。
    """
    rows: list[list[B]] = []
    base = page * _PAGE_SIZE
    sel = _sel_of(uid) if uid else set()
    row: list[B] = []
    for k, j in enumerate(items, 1):
        idx = base + k
        if j.get("kind") == "arc":
            # 已归档的不能勾选/下载 —— 按钮只用来解释状态
            row.append(B(f"{ui.ON}{idx}. {_btn_name(j)}", f"tk:arc:{idx}"))
        else:
            mark = ui.mark(_jkey(j) in sel)
            row.append(B(f"{mark}{idx}. {_btn_name(j)}", f"tk:sel:{idx}:{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️ 上一页", f"tk:p:{page - 1}"))
        nav.append(B(f"📄 {page + 1}/{pages}", "tk:noop"))
        if page < pages - 1:
            nav.append(B("下一页 ➡️", f"tk:p:{page + 1}"))
        rows.append(nav)

    rows.append([B("✅ 只下载勾选", f"tk:apply:{page}"),
                 B(f"{ui.ON} 全选", f"tk:all:{page}"),
                 B(f"{ui.OFF} 全不选", f"tk:none:{page}")])
    rows.append([B("⏸ 全部暂停", "tk:pauseall"),
                 B("▶️ 全部开始", "tk:resumeall"),
                 B("🗑 删除勾选", f"tk:del:{page}")])
    rows.append([B("🔄 刷新", "tk:home"), B("🧹 清空全部订阅", "tk:clearsub")])
    rows.append([B("🗑 清空所有下载", "tk:clearqb"), B("📋 待审核求片", "tk:reqs")])
    rows.append([B("❌ 关闭面板", "tk:close")])
    return KM(rows)


def _confirm_kb(idx: int, page: int) -> KM:
    return KM([
        [B("✅ 确认取消", f"tk:rmyes:{idx}:{page}"), B("↩️ 返回面板", f"tk:p:{page}")],
    ])


# ═════════════════════════════════════════════ 渲染
async def _display(msg: Message, pic, cap: str, kb: KM) -> None:
    """把一条消息变成「图 + 说明 + 按钮」；图文互转时降级重发一条"""
    cap = ui.clip_html(cap)
    try:
        if pic:
            buf = io.BytesIO(pic)
            buf.name = "p.jpg"
            if getattr(msg, "photo", None):
                await msg.edit_media(
                    InputMediaPhoto(buf, caption=cap, parse_mode=PM),
                    reply_markup=kb)
                return
            try:
                await msg.delete()
            except Exception:
                pass
            await bot.send_photo(msg.chat.id, buf, caption=cap,
                                 reply_markup=kb, parse_mode=PM)
            return
        if getattr(msg, "photo", None):
            try:
                await msg.delete()
            except Exception:
                pass
            await bot.send_message(msg.chat.id, cap, reply_markup=kb,
                                   parse_mode=PM, disable_web_page_preview=True)
            return
        await msg.edit_text(cap, reply_markup=kb, parse_mode=PM,
                            disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"任务面板渲染失败: {e}")


async def _edit_cap(msg: Message, cap: str, kb: KM) -> None:
    """只改文案，保留当前图片（怕丢大革命 every 次都重传图会很慢）"""
    cap = ui.clip_html(cap)
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(cap, reply_markup=kb, parse_mode=PM)
        else:
            await msg.edit_text(cap, reply_markup=kb, parse_mode=PM,
                                disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"改写任务面板文案失败: {e}")


async def render_panel(msg: Message, page: int = 0, force: bool = False,
                       uid: int = 0) -> None:
    jl = await jobs(force)
    pages = max(1, (len(jl) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    items = jl[page * _PAGE_SIZE: page * _PAGE_SIZE + _PAGE_SIZE]
    pic = await asyncio.to_thread(_job_collage, items) if items else None
    await _display(msg, pic, _ov_text(jl, page, pages, items, uid),
                   _ov_kb(jl, page, pages, items, uid))
    await _register_live(msg, page, uid)


async def _redraw(msg: Message, page: int, uid: int) -> None:
    """只改文案与按钮 —— 保留当前海报，切换勾选时不用重下拼图，秒回"""
    jl = await jobs()
    pages = max(1, (len(jl) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    items = jl[page * _PAGE_SIZE: page * _PAGE_SIZE + _PAGE_SIZE]
    await _edit_cap(msg, _ov_text(jl, page, pages, items, uid),
                    _ov_kb(jl, page, pages, items, uid))


async def _register_live(msg: Message, page: int, uid: int) -> None:
    """登记这条面板消息，交给定时任务每 20 秒自动刷进度条"""
    try:
        _LIVE[(msg.chat.id, msg.id)] = (
            bool(getattr(msg, "photo", None)), time.time(), int(page), int(uid or 0))
    except Exception:
        pass


async def refresh_live() -> None:
    """定时自动刷新 PT 任务面板（进度条会自己走，不用一直点刷新）"""
    if not _LIVE:
        return
    now = time.time()
    for key in list(_LIVE.keys()):
        is_photo, ts, page, uid = _LIVE[key]
        if now - ts > _LIVE_TTL:
            _LIVE.pop(key, None)
            continue
        chat, mid = key
        try:
            jl = await jobs(True)
            pages = max(1, (len(jl) + _PAGE_SIZE - 1) // _PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            items = jl[page * _PAGE_SIZE: page * _PAGE_SIZE + _PAGE_SIZE]
            text = _ov_text(jl, page, pages, items, uid)
            kb = _ov_kb(jl, page, pages, items, uid)
            if is_photo:
                await bot.edit_message_caption(chat, mid, text,
                                               reply_markup=kb, parse_mode=PM)
            else:
                await bot.edit_message_text(chat, mid, text, reply_markup=kb,
                                            parse_mode=PM,
                                            disable_web_page_preview=True)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.debug(f"刷新任务面板失败：{e}")
                if "MESSAGE_ID_INVALID" in str(e):
                    _LIVE.pop(key, None)
        await asyncio.sleep(0.5)


async def _cancel_job(j: dict) -> tuple[bool, int, str]:
    """取消一条任务；返回 (是否取消了订阅, 删掉的种子数, 提示)"""
    ok = False
    if j["kind"] == "sub" and j.get("id"):
        ok = bool(await asyncio.to_thread(mp_subscribe_del, j["id"]))
    hashes = [str(t.get("hash")) for t in (j.get("tors") or [])
              if t.get("hash")]
    n = await asyncio.to_thread(qb_delete, hashes) if hashes else 0
    if j["kind"] == "sub":
        tip = ("✅ 已删除订阅" if ok else "⚠️ 订阅删除失败") + f"，清理下载 {n} 个"
    else:
        tip = f"✅ 已删除下载种子 {n} 个" if n else "⚠️ 删除失败"
    return ok or j["kind"] != "sub", n, tip


# ═════════════════════════════════════════════ 命令
@bot.on_message(filters.command(["ptasks", "任务", "订阅任务", "pttask"], **CMD))
@perm.gate("ptctl")
async def cmd_ptasks(client, msg: Message):
    uid = msg.from_user.id if msg.from_user else 0
    wait = await msg.reply("⏳ 正在读取 MoviePilot 订阅与 qBittorrent 任务…",
                           parse_mode=PM)
    try:
        await render_panel(wait, 0, True, uid)
    except Exception as e:
        logger.warning(f"任务面板构建失败: {e}")
        await _display(wait, None, f"⚠️ 读取失败：{_esc(e)}",
                       KM([[B("🔄 重试", "tk:home")]]))


@bot.on_message(filters.command(["ptdel", "取消任务", "deltask"], **CMD))
@perm.gate("ptctl")
async def cmd_ptdel(client, msg: Message):
    arg = (msg.text or "").split(maxsplit=1)
    raw = arg[1].strip() if len(arg) > 1 else ""
    if not raw:
        await msg.reply(
            "❌ <b>取消任务</b>\n" + _div() + "\n"
            "推荐直接用面板逐个取消：<code>/ptasks</code>\n"
            "也可以给订阅 ID：<code>/ptdel 3,5,7</code>　或　<code>/ptdel all</code>",
            parse_mode=PM,
            reply_markup=KM([[B("📋 打开任务面板", "tk:home")]]))
        return
    jl = await jobs(True)
    subs = [j for j in jl if j["kind"] == "sub"]
    if raw.lower() in ("all", "全部", "*"):
        targets = subs
    else:
        want = set(int(x) for x in re.findall(r"\d+", raw))
        targets = [j for j in subs if j["id"] in want]
    if not targets:
        await msg.reply(f"⚠️ 没匹配到订阅 ID：{_t(raw, 20)}", parse_mode=PM)
        return
    done = 0
    for j in targets:
        ok, _n, _tip = await _cancel_job(j)
        done += 1 if ok else 0
    lines = [f"✅ <b>已处理 {done}/{len(targets)} 条订阅</b>", _div()]
    for j in targets[:12]:
        lines.append(f"　· {_t(j['name'], 22)}　{_t(_range_txt(j), 22)}")
    lines.append(_div())
    lines.append("💡 用 /ptasks 看最新状态")
    await msg.reply(ui.clip_html("\n".join(lines)), parse_mode=PM,
                    disable_web_page_preview=True)


# ═════════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^tk:p:(\d+)$"))
async def cb_page(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    await q.answer()
    try:
        await render_panel(q.message, int(q.data.split(":")[2]), False,
                           q.from_user.id)
    except Exception as e:
        logger.warning(f"翻页失败: {e}")
        await q.answer("翻页失败，请重试", show_alert=True)


@bot.on_callback_query(filters.regex(r"^tk:home$"))
async def cb_home(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    await q.answer("已刷新")
    try:
        await render_panel(q.message, 0, True, q.from_user.id)
    except Exception as e:
        logger.warning(f"刷新失败: {e}")


# ═════════════════════════════════════════════ 勾选（点任务按钮切换）
@bot.on_callback_query(filters.regex(r"^tk:sel:(\d+):(\d+)$"))
async def cb_sel(client, q):
    """点一下任务按钮 → 切换这条任务的勾选状态（✅ ⇄ ☐）

    ★ 序号约定（踩过的坑）：按钮上的编号是**从 1 开始**的（`idx = base + k`，
      k 由 `enumerate(items, 1)` 产生），而 `jl` 是 0 基列表。
      以前这里写成 `jl[_idx]`，于是「点第 1 个勾到第 2 个、点第 3 个勾到第 4 个」。
      统一：**回调里的序号一律 1 基，取值时必须 `-1`**。
    """
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx, page = (int(x) for x in q.data.split(":")[2:4])
    jl = await jobs()
    if not 1 <= _idx <= len(jl):
        await q.answer("该任务已不存在，先刷新一下", show_alert=True)
        await render_panel(q.message, page, True, q.from_user.id)
        return
    key = _jkey(jl[_idx - 1])
    sel = _sel_of(q.from_user.id)
    if key in sel:
        sel.discard(key)
        tip = f"{ui.OFF} 已取消勾选"
    else:
        sel.add(key)
        tip = f"{ui.ON} 已勾选"
    await q.answer(tip)
    await _redraw(q.message, page, q.from_user.id)


def _arc_card(j: dict) -> str:
    """归档详情卡 —— 把「归档是什么 / 文件在哪 / 何时才需要重下」讲清楚"""
    tl = ui.title_link(j.get("name"), j.get("tmdb"), j.get("type"), 26)
    y = f"（{_t(j['year'], 8)}）" if j.get("year") else ""
    return ui.clip_html("\n".join([
        "📦 <b>归档详情</b>", _div(),
        f"🎬 <b>{tl}</b>{y}",
        f"📄 范围：{_t(_range_txt(j), 30)}",
        f"📌 原订阅：<code>#{j['id']}</code>（MoviePilot 已自动移除）",
        _div(),
        "ℹ️ <b>「归档」是什么意思</b>",
        "MoviePilot 在<b>订阅完成</b>（要的集数全部下齐）时会自动删掉订阅记录，",
        "所以这条在 MP 里查不到了。但<b>影片文件早就下好并入库 Emby</b>，",
        "👉 <b>正常观看不需要重新下载</b>。",
        "",
        "🔄 <b>只有这两种情况才需要重下</b>",
        "　· 115 / Emby 里的文件被删了",
        "　· 想换版本（更高画质 / 别的压制组）",
        _div(),
        "💡 重新下载 = 重建 MoviePilot 订阅并立即搜索，会<b>完整下载一遍</b>；",
        "　　同一条申请<b>不重复扣灵石</b>。",
    ]))


def _arc_kb(idx: int) -> KM:
    return KM([
        [B("🔄 重新下载", f"tk:rarc:{idx}")],
        [B("↩️ 返回面板", "tk:home")],
    ])


def _arc_confirm_kb(idx: int) -> KM:
    return KM([
        [B("✅ 确认重新下载", f"tk:rarcok:{idx}")],
        [B("↩️ 取消", "tk:home")],
    ])


@bot.on_callback_query(filters.regex(r"^tk:arc:(\d+)$"))
async def cb_arc(client, q):
    """点归档行 → 打开「归档详情」（解释清楚 + 给出重新下载入口）

    ★ 以前这里只弹一句"不能再下载"，用户想重下就卡死了。
      现在改成详情卡，并说明「文件已在 Emby，通常不用重下」。
    """
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx = int(q.data.split(":")[2])
    jl = await jobs()
    if not 1 <= _idx <= len(jl) or jl[_idx - 1].get("kind") != "arc":
        await q.answer("这条不是归档记录，先刷新面板", show_alert=True)
        return
    await q.answer()
    await _edit_cap(q.message, _arc_card(jl[_idx - 1]), _arc_kb(_idx))


@bot.on_callback_query(filters.regex(r"^tk:rarc:(\d+)$"))
async def cb_arc_redl(client, q):
    """重新下载：先出二次确认（会完整下一遍，避免误点浪费空间）"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx = int(q.data.split(":")[2])
    jl = await jobs()
    if not 1 <= _idx <= len(jl) or jl[_idx - 1].get("kind") != "arc":
        await q.answer("这条不是归档记录，先刷新面板", show_alert=True)
        return
    j = jl[_idx - 1]
    tl = ui.title_link(j.get("name"), j.get("tmdb"), j.get("type"), 26)
    await q.answer()
    await _edit_cap(q.message, ui.clip_html("\n".join([
        "⚠️ <b>确认重新下载？</b>", _div(),
        f"🎬 <b>{tl}</b>",
        f"📄 范围：{_t(_range_txt(j), 30)}",
        _div(),
        "会重新在 MoviePilot 建立订阅并立即搜索下载，<b>完整下载一遍</b>。",
        "原文件若还在，<b>不会自动删除</b>，可能重复占用空间。",
        "",
        "💡 只是想在 Emby 里看片的话<b>不用点这里</b> —— 文件已经在库里了。",
    ])), _arc_confirm_kb(_idx))


@bot.on_callback_query(filters.regex(r"^tk:rarcok:(\d+)$"))
async def cb_arc_ok(client, q):
    """真正执行：把这条归档求片重新推给 MoviePilot"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx = int(q.data.split(":")[2])
    jl = await jobs()
    if not 1 <= _idx <= len(jl) or jl[_idx - 1].get("kind") != "arc":
        await q.answer("这条不是归档记录，先刷新面板", show_alert=True)
        return
    j = jl[_idx - 1]
    aid = int(j.get("aid") or 0)
    a = await asyncio.to_thread(db.get_audit, aid) if aid else None
    if not a:
        await q.answer("找不到原始申请记录，无法重新下发", show_alert=True)
        return

    # 同一条申请已经扣过灵石，重下不重复扣
    ok, note, sub_id, n_sub, kicked = await push_audit_to_mp(a, charge=False)
    if ok:
        await asyncio.to_thread(db.set_audit, aid, "done", q.from_user.id,
                                note, sub_id)
        await asyncio.to_thread(db.clear_idle_notified, aid)
    else:
        await asyncio.to_thread(db.set_audit, aid, "failed", q.from_user.id, note)
    logger.info(f"管理员 {q.from_user.id} 重新下载申请 #{aid} {j.get('name')}"
                f"｜ok={ok} n_sub={n_sub} note={note}")
    await jobs(True)
    try:
        await render_panel(q.message, 0, False, q.from_user.id)
    except Exception as e:
        logger.warning(f"重新下载后重绘失败: {e}")
    try:
        await q.answer(
            ("✅ 已重新建立订阅" + ("，并已触发立即搜索" if kicked
                                   else "，等下一轮自动搜索"))
            if ok else f"⚠️ 重新下发失败：{_t(note, 60)}",
            show_alert=True)
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^tk:all:(\d+)$"))
async def cb_sel_all(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    page = int(q.data.split(":")[2])
    jl = await jobs()
    sel = _sel_of(q.from_user.id)
    for j in jl:
        if j.get("kind") != "arc":       # 已归档的不可勾选
            sel.add(_jkey(j))
    await q.answer(f"已全选 {len(sel)} 条（已归档的不参与）")
    await _redraw(q.message, page, q.from_user.id)


@bot.on_callback_query(filters.regex(r"^tk:none:(\d+)$"))
async def cb_sel_none(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    page = int(q.data.split(":")[2])
    _sel_of(q.from_user.id).clear()
    await q.answer("已清空勾选")
    await _redraw(q.message, page, q.from_user.id)


@bot.on_callback_query(filters.regex(r"^tk:apply:(\d+)$"))
async def cb_apply(client, q):
    """只让勾中的任务下载：勾选的 resume，没勾选的 pause"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    page = int(q.data.split(":")[2])
    sel = _sel_of(q.from_user.id)
    if not sel:
        await q.answer("先勾选要优先下载的任务，再点这里", show_alert=True)
        return
    await q.answer("正在应用…")
    jl = await jobs(True)
    n_run = n_stop = 0
    for j in jl:
        hashes = [str(t.get("hash")) for t in (j.get("tors") or [])
                  if t.get("hash")]
        if not hashes:
            continue
        if _jkey(j) in sel:
            n_run += await asyncio.to_thread(qb_resume, hashes)
        else:
            n_stop += await asyncio.to_thread(qb_pause, hashes)
    await jobs(True)
    await render_panel(q.message, page, False, q.from_user.id)
    await q.answer(f"已开始 {n_run} 个 / 暂停 {n_stop} 个", show_alert=True)


@bot.on_callback_query(filters.regex(r"^tk:del:(\d+)$"))
async def cb_del_sel(client, q):
    """删除**勾中**的任务（二次确认）"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    page = int(q.data.split(":")[2])
    sel = _sel_of(q.from_user.id)
    jl = await jobs()
    targets = [j for j in jl if _jkey(j) in sel]
    if not targets:
        await q.answer("先勾选要删除的任务", show_alert=True)
        return
    await q.answer()
    lines = [f"🗑 <b>删除勾选任务 · {len(targets)} 条</b>", _div()]
    for j in targets[:8]:
        lines.append(f"　· {_t(j['name'], 24)}　{_t(_range_txt(j), 18)}")
    if len(targets) > 8:
        lines.append(f"　· …另有 {len(targets) - 8} 条")
    lines += [_div(), "⚠️ 会同时删除 MoviePilot 订阅与 qBittorrent 种子，不可撤销"]
    await _edit_cap(q.message, ui.clip_html("\n".join(lines)),
                    KM([[B("✅ 确认删除", f"tk:delok:{page}"),
                         B("↩️ 取消", f"tk:p:{page}")]]))


@bot.on_callback_query(filters.regex(r"^tk:delok:(\d+)$"))
async def cb_del_ok(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    page = int(q.data.split(":")[2])
    sel = _sel_of(q.from_user.id)
    jl = await jobs(True)
    targets = [j for j in jl if _jkey(j) in sel]
    if not targets:
        await q.answer("勾选项已不存在", show_alert=True)
        await render_panel(q.message, page, True, q.from_user.id)
        return
    await q.answer("正在删除…")
    ok = 0
    for j in targets:
        r = await _cancel_job(j)
        ok += 1 if r[0] else 0
        sel.discard(_jkey(j))
    await jobs(True)
    logger.info(f"管理员 {q.from_user.id} 批量删除 {ok}/{len(targets)} 条任务")
    await render_panel(q.message, page, False, q.from_user.id)
    await q.answer(f"已删除 {ok}/{len(targets)} 条", show_alert=True)


@bot.on_callback_query(filters.regex(r"^tk:pauseall$"))
async def cb_pause_all(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    tors = await asyncio.to_thread(qb_torrents)
    await q.answer()
    await _edit_cap(q.message,
                    f"⏸ <b>全部暂停</b>\n{_div()}\n"
                    f"将暂停 qBittorrent 里 <b>{len(tors)}</b> 个种子"
                    f"（已下载的文件会保留）。\n{_div()}\n"
                    "💡 想只跑某几部？点「↩️ 取消」，勾选后再点「✅ 只下载勾选」",
                    _confirm_kb_op("pauseall"))


@bot.on_callback_query(filters.regex(r"^tk:resumeall$"))
async def cb_resume_all(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    tors = await asyncio.to_thread(qb_torrents)
    await q.answer()
    await _edit_cap(q.message,
                    f"▶️ <b>全部开始</b>\n{_div()}\n"
                    f"将恢复 qBittorrent 里 <b>{len(tors)}</b> 个种子的下载。\n"
                    f"{_div()}\n"
                    "💡 只想跑其中几部就别点这个 —— 用勾选模式更精准",
                    _confirm_kb_op("resumeall"))


@bot.on_callback_query(filters.regex(r"^tk:rm:(\d+):(\d+)$"))
async def cb_rm(client, q):
    """点某条任务的 ❌ → 出二次确认"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx, page = (int(x) for x in q.data.split(":")[2:4])
    jl = await jobs()
    if not 1 <= _idx <= len(jl):
        await q.answer("该任务已不存在，先刷新一下", show_alert=True)
        await render_panel(q.message, 0, True, q.from_user.id)
        return
    await q.answer()
    await _edit_cap(q.message, _confirm_text(jl[_idx - 1], _idx),
                    _confirm_kb(_idx, page))


@bot.on_callback_query(filters.regex(r"^tk:rmyes:(\d+):(\d+)$"))
async def cb_rmyes(client, q):
    """确认取消某一条任务"""
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    _idx, page = (int(x) for x in q.data.split(":")[2:4])
    jl = await jobs()
    if not 1 <= _idx <= len(jl):
        await q.answer("该任务已不存在", show_alert=True)
        await render_panel(q.message, 0, True, q.from_user.id)
        return
    j = jl[_idx - 1]
    await q.answer("正在取消…")
    _ok, _n, tip = await _cancel_job(j)
    await jobs(True)                                   # 刷新缓存，回来看到的是最新
    logger.info(f"管理员 {q.from_user.id} 取消任务 #{_idx} {j.get('name')}｜{tip}")
    try:
        await render_panel(q.message, page, False, q.from_user.id)
        await q.answer(tip, show_alert=True)
    except Exception as e:
        logger.warning(f"取消后重绘失败: {e}")


@bot.on_callback_query(filters.regex(r"^tk:noop$"))
async def cb_noop(client, q):
    """「📄 1/2」这种只做展示的按钮：给个回音，别让人以为卡了"""
    await q.answer("这是页码提示，用 ⬅️ ➡️ 翻页")


def _confirm_kb_op(action: str) -> KM:
    return KM([[B("✅ 确认执行", f"tk:yes:{action}"),
                B("↩️ 取消", f"tk:p:0")]])


@bot.on_callback_query(filters.regex(r"^tk:clearsub$"))
async def cb_clearsub(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    subs = [j for j in (await jobs(True)) if j["kind"] == "sub"]
    await q.answer()
    await _edit_cap(q.message,
                    f"🧹 <b>清空全部订阅</b>\n{_div()}\n"
                    f"将删除 <b>{len(subs)}</b> 条订阅，并清掉对应的下载任务。\n"
                    f"⚠️ 此操作不可撤销\n{_div()}\n"
                    "💡 想只删某几条？点「↩️ 取消」回到面板逐条删除",
                    _confirm_kb_op("clearsub"))


@bot.on_callback_query(filters.regex(r"^tk:clearqb$"))
async def cb_clearqb(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    tors = await asyncio.to_thread(qb_torrents)
    await q.answer()
    await _edit_cap(q.message,
                    f"🗑 <b>清空所有下载</b>\n{_div()}\n"
                    f"将删除 qBittorrent 里 <b>{len(tors)}</b> 个种子（含已下载文件）。\n"
                    f"⚠️ 订阅还在的话，下一轮会重新开始下载\n{_div()}\n"
                    "💡 想只删某几个？点「↩️ 取消」回到面板逐条删除",
                    _confirm_kb_op("clearqb"))


@bot.on_callback_query(filters.regex(
    r"^tk:yes:(clearsub|clearqb|pauseall|resumeall)$"))
async def cb_yes(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    act = q.data.split(":")[2]
    await q.answer("正在执行…")

    if act in ("pauseall", "resumeall"):
        path = ("/api/v2/torrents/pause" if act == "pauseall"
                else "/api/v2/torrents/resume")
        ok = await asyncio.to_thread(qb_all, path)
        await jobs(True)
        await _display(q.message, None,
                       (f"⏸ <b>已暂停全部下载</b>" if act == "pauseall"
                        else f"▶️ <b>已恢复全部下载</b>") + "\n" + _div() + "\n"
                       + ("✅ qBittorrent 已收到指令，种子全部暂停"
                          if act == "pauseall"
                          else "✅ 已全部恢复，出种的任务会继续下载")
                       + "\n" + _div() + f"\n🕒 {time.strftime('%H:%M')}",
                       KM([[B("📋 回到任务面板", "tk:home")]]))
        if not ok:
            await q.answer("⚠️ qBittorrent 没响应，请检查是否在线", show_alert=True)
        return

    if act == "clearsub":
        subs = [j for j in (await jobs(True)) if j["kind"] == "sub"]
        ok = 0
        hashes: list[str] = []
        for j in subs:
            r = await _cancel_job(j)
            ok += 1 if r[0] else 0
            hashes += [str(t.get("hash")) for t in (j.get("tors") or [])
                       if t.get("hash")]
        # 剩下没配对的下载也一起清掉，避免残留
        tors = await asyncio.to_thread(qb_torrents)
        rest = [str(t.get("hash")) for t in tors
                if t.get("hash") and str(t.get("hash")) not in hashes]
        await asyncio.to_thread(qb_delete, rest)
        await jobs(True)
        await _display(q.message, None,
                       f"🧹 <b>已清空订阅</b>\n{_div()}\n"
                       f"订阅删除 <b>{ok}/{len(subs)}</b> 条\n"
                       f"下载任务一并清理完毕\n{_div()}\n"
                       f"🕒 {time.strftime('%H:%M')}",
                       KM([[B("📋 回到任务面板", "tk:home")]]))
        return

    tors = await asyncio.to_thread(qb_torrents)
    n = await asyncio.to_thread(qb_delete,
                                [t.get("hash") for t in tors if t.get("hash")])
    await jobs(True)
    await _display(q.message, None,
                   f"🗑 <b>已清空下载任务</b>\n{_div()}\n共删除 <b>{n}</b> 个种子\n"
                   f"{_div()}\n🕒 {time.strftime('%H:%M')}",
                   KM([[B("📋 回到任务面板", "tk:home")]]))


@bot.on_callback_query(filters.regex(r"^tk:reqs$"))
async def cb_reqs(client, q):
    from .. import db
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    rows = await asyncio.to_thread(db.list_audits, "auditing", 10)
    await q.answer()
    if not rows:
        await q.message.reply("📭 当前没有待审核的求片", parse_mode=PM)
        return
    txt = [f"📋 <b>待审核求片 · {len(rows)} 条</b>", _div()]
    for a in rows:
        y = f"（{a['year']}）" if a.get("year") else ""
        txt.append(f"#{a['id']}　🎬 {_t(str(a.get('title') or '')[:22])}{_t(y, 8)}")
    txt.append(_div())
    txt.append("💡 用 /reqs 查看详情并审核")
    await q.message.reply(ui.clip_html("\n".join(txt)), parse_mode=PM,
                          disable_web_page_preview=True,
                          reply_markup=KM([[B("↩️ 回到任务面板", "tk:home")]]))


@bot.on_callback_query(filters.regex(r"^tk:close$"))
async def cb_close(client, q):
    await q.answer("已关闭")
    try:
        await q.message.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════ 站点目录同步
@bot.on_message(filters.command(["syncsite", "站点同步", "站点"], **CMD))
@perm.gate("ptctl")
async def cmd_syncsite(client, msg: Message):
    """从 MP 的 site 表读站点列表，把本地下载盘缺的目录补齐。

    ★ 为什么需要：用户在 MP 里新增 PT 站点后，本地目录不会自动出现。
      这里把「站点列表」的唯一权威源定为 MP 的 site 表 —— 加站点只需在 MP 里
      加一次，本地目录 /downloads/{客户端}/{求片|订阅}/{站点}/{5大类} 自动补全，
      115 侧由 pt2cloud 按同一层级创建，MT 只需一套整理规则。
    """
    from . import sortpath

    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 只有管理员能用这个命令", parse_mode=PM)
        return

    txt = ["📡 <b>同步站点目录</b>", _div()]
    sites = sortpath.mp_sites(ttl=0)
    if not sites:
        txt.append("⚠️ 读不到 MP 的站点列表，请确认 MP 在线且 bot 配置正确")
        await msg.reply(ui.clip_html("\n".join(txt)), parse_mode=PM)
        return

    names = sorted(set(sites.values()))
    txt.append(f"🗂 站点 <b>{len(names)}</b> 个：" + "、".join(_t(n, 12) for n in names))

    info = await asyncio.to_thread(sortpath.sync_site_dirs)
    created = info.get("created") or []
    txt.append(_div())
    if created:
        txt.append(f"✅ 新建目录 <b>{len(created)}</b> 个")
        for c in created[:6]:
            txt.append("　" + _t(c.replace("/downloads", ""), 40))
        if len(created) > 6:
            txt.append(f"　…… 另有 {len(created) - 6} 个")
    else:
        txt.append("✅ 目录已齐全，无需新建")

    # 顺手跑一轮归类，把新站点下的种子归位
    r = await asyncio.to_thread(sortpath.sync_once, True)
    if r.get("moved"):
        txt.append(_div())
        txt.append(f"📦 有 <b>{r['moved']}</b> 个种子待归位，3 分钟内自动处理")
    txt.append(_div())
    txt.append("💡 115 云端目录由pt2cloud 按同层级自动创建")
    await msg.reply(ui.clip_html("\n".join(txt)), parse_mode=PM)
