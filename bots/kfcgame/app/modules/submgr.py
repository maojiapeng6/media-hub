"""管理员：订阅与求片管理中心（与 Web 端口 5678 的「订阅与求片管理」通道功能对齐）

用户需求（2026-10-12）：
  Web 端做了「订阅与求片管理」通道后，机器人端要**同步一套功能基本一致的命令**：
    · 查看订阅列表（在途订阅 / 已完成归档 / 求片申请）
    · 勾选 / 全选 / 全不选 / 全选本页
    · 批量操作：⬇️ 全部下载 · ⏸ 全部暂停 · ▶️ 全部恢复 · 🔝 优先下载
    · 每次操作**立即反馈**：自动搜索 → 「未找到」/「已找到资源并开始下载」+ 进度条 + 完整状态

命令：
  /subs  /订阅  /submgr     打开订阅与求片管理中心（管理员）
  /subdl /全部下载          对勾选项：恢复订阅 + 立即搜索
  /subpause /全部暂停       对勾选项：暂停订阅 + 暂停对应种子
  /subresume /全部恢复      对勾选项：恢复订阅 + 恢复对应种子
  /subprio /优先下载        只跑勾选项：其余订阅与种子全部暂停

设计要点（与 Web 端保持一致，两端口径必须相同）：
  1. **一条一张卡**：片名用粗体超链接（ui.title_link），状态 + 进度条实时显示
  2. **勾选 = 按钮切换**：回调里的序号**一律 1 基**，取值必须 `-1`（本项目踩过的坑）
  3. **归档不能勾选**：MoviePilot 完成订阅后会删掉 subscribe 行，
     归档条目只能「🔄 重新下载」（复用 push_audit_to_mp，且不重复扣灵石）
  4. **搜索有明确结果**：踢完 search 后重新拉种子比对，有变化 → 「已找到资源并开始下载」，
     没变化 → 「未找到资源（站点尚未收录）」，绝不静默
  5. **进度条自己走**：每 20 秒自动刷新（复用 mptask 的 live 机制风格）
"""
from __future__ import annotations

import asyncio
import re
import time

import requests
from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import Message

PM = ParseMode.HTML

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from . import perm
from .inbox import hum_size
from .inbox import hum_speed
from .request import is_admin
from .request import _req as _mp_req          # MP 通用请求（Bearer 自动处理）
from .request import mp_kick_search
from .request import mp_sub_state
from .request import mp_subscribe_list

CMD = dict(prefixes=S.PREFIX)

_PAGE = 6
_PAGE_SIZE = _PAGE
_SEL: dict[int, set[str]] = {}
_CACHE: dict = {"ts": 0.0, "items": [], "ttl": 15.0}

_QB = S.CFG.get("qb") or {}
QB_URL = (_QB.get("url") or "http://127.0.0.1:8080").rstrip("/")

# MoviePilot 订阅状态：N-新建，R-订阅中，P-已暂停，S-已停止
# 来源 /app/app/schemas/subscribe.py:213 + db/models/subscribe.py:78（默认 N）
_STATE_ZH = {"N": "新建", "R": "订阅中", "P": "已暂停", "S": "已停止",
             "ARCHIVED": "已完成归档", "": "—"}


# ═════════════════════════════════════════════ 基础工具
def _esc(s) -> str:
    return ui.esc(str(s or ""))


def _t(s, n: int = 40) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _div() -> str:
    return ui.divider()


def _bar(pct) -> str:
    try:
        pct = float(pct)
    except Exception:
        pct = 0.0
    return ui.bar(max(0.0, min(100.0, pct)))


def _sel_of(uid) -> set[str]:
    return _SEL.setdefault(int(uid), set())


# ── qBittorrent：**直接复用 mptask 的实现**，不再重复造一份 ──
# 教训：自己另写一份时把配置键写成 qb.user / qb.pass，而实际是
#       qb.username / qb.password ⇒ 静默登录失败、种子数恒为 0。
#       唯一实现原则：qB 的会话/开关都走 mptask。
from .mptask import qb_pause as _qb_pause          # noqa: E402
from .mptask import qb_resume as _qb_resume        # noqa: E402
from .mptask import qb_torrents as _qb_tors        # noqa: E402


def _qb_sess():                                     # 兼容旧调用名
    from .mptask import qb_session
    return qb_session()


# ── 片名 ↔ 种子匹配（与 Web 端 _tor_key 同一套规则，两端结果必须一致）──
def _tor_key(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"\.(mkv|mp4|ts|avi|iso|torrent)$", "", s)
    s = re.sub(r"\b(s\d{1,2})?(e\d{1,3})\b", " ", s)
    s = re.sub(r"\bs\d{1,2}\b", " ", s)
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"\b(2160|1080|720|480|360)p?\b", " ", s)
    s = re.sub(r"\b(web[- ]?dl|bluray|blu-ray|hdrip|hdtv|remux|x26[45]|hevc|avc|"
               r"aac|ddp?\d?(\.\d)?|flac|hdr|dv|sdr|10bit|8bit|complete|repack|"
               r"proper|internal|multi|chs|cht|gb|big5)\b", " ", s)
    s = re.sub(r"[\[\]\(\)\{\}\.\-_·]+", " ", s)
    return re.sub(r"\s+", "", s)


def _match_tors(name, tors) -> list[dict]:
    k = _tor_key(name)
    if not k or len(k) < 2:
        return []
    out = []
    for t in tors:
        tk = _tor_key(t.get("name"))
        if tk and (k in tk or tk in k):
            out.append(t)
    return out


def _prog(tors: list[dict]) -> dict:
    if not tors:
        return {"n": 0, "size": 0, "done": 0, "pct": 0.0, "dl": 0,
                "state": "", "eta": 0, "seeds": 0}
    size = sum(int(t.get("size") or 0) for t in tors)
    done = sum(int(t.get("completed") or 0) for t in tors)
    dl = sum(int(t.get("dlspeed") or 0) for t in tors)
    seeds = sum(int(t.get("num_seeds") or 0) for t in tors)
    pct = (done / size * 100.0) if size else (
        100.0 if all(float(t.get("progress") or 0) >= 1 for t in tors) else 0.0)
    sts = [str(t.get("state") or "") for t in tors]
    if all(s in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP") for s in sts):
        st = "paused"
    elif any("error" in s or "missing" in s for s in sts):
        st = "error"
    elif all(float(t.get("progress") or 0) >= 1 for t in tors):
        st = "done"
    else:
        st = "downloading"
    rem = sum(max(0, int(t.get("size") or 0) - int(t.get("completed") or 0))
              for t in tors if float(t.get("progress") or 0) < 1)
    eta = int(rem / dl) if (rem and dl) else 0
    return {"n": len(tors), "size": size, "done": done, "pct": round(pct, 1),
            "dl": dl, "state": st, "eta": eta, "seeds": seeds}


def _eta(sec) -> str:
    if not sec or sec <= 0:
        return ""
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分"
    if sec < 86400:
        return f"{sec // 3600}时{(sec % 3600) // 60}分"
    return f"{sec // 86400}天"


# ═════════════════════════════════════════════ 数据聚合
def build_list(force: bool = False) -> dict:
    """订阅与求片管理总表（与 Web 端 submgmt_list 口径一致）。

    kind='sub' MP 现存订阅（可暂停/恢复/搜索）
    kind='arc' MP 订阅历史（已完成归档，可重新下载）
    kind='req' bot 求片申请（可审核）
    """
    if not force and _CACHE["ts"] and time.time() - _CACHE["ts"] < _CACHE["ttl"]:
        return _CACHE["items"]
    items: list[dict] = []
    tors = _qb_tors()

    # ① MP 现存订阅
    subs = []
    live_ids = set()
    try:
        for s in mp_subscribe_list(200):
            subs.append(s)
            try:
                live_ids.add(int(s.get("id") or 0))
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"读取 MP 订阅失败: {e}")

    # ② MP 订阅历史（归档）
    hist = []
    for mt in ("电视剧", "电影"):
        try:
            r = _mp_get(f"/api/v1/subscribe/history/{mt}",
                        params={"page": 1, "count": 200})
            hl = (r or {}).get("data") or []
            if isinstance(hl, dict):
                hl = hl.get("items") or []
            for x in hl:
                if isinstance(x, dict):
                    hist.append(dict(x, _mt=mt))
        except Exception as e:
            logger.debug(f"订阅历史 {mt} 读取失败: {e}")

    # ③ bot 求片申请
    audits = []
    try:
        if getattr(db, "_engine", None) is None:
            db.init()
        audits = db.list_audits_all(200)
    except Exception as e:
        logger.warning(f"读取求片申请失败: {e}")

    aid_by_sub: dict[int, int] = {}
    for a in audits:
        try:
            si = int(a.get("sub_id") or 0)
        except Exception:
            si = 0
        if si:
            aid_by_sub.setdefault(si, int(a.get("id") or 0))

    def mk(kind, name, year, mtype, state, sub_id, extra):
        mt = _match_tors(name, tors)
        pr = _prog(mt)
        it = {"kind": kind, "name": str(name or ""), "year": str(year or ""),
              "mtype": mtype or "", "state": state, "sub_id": int(sub_id or 0),
              "prog": pr, "tors": [t.get("hash") for t in mt if t.get("hash")]}
        it.update(extra or {})
        return it

    for s in subs:
        st = str(s.get("state") or "")
        sid = int(s.get("id") or 0)
        items.append(mk("sub", s.get("name"), s.get("year"),
                        s.get("type") or s.get("mtype"), st, sid,
                        {"state_zh": _STATE_ZH.get(st, st), "season": s.get("season"),
                         "poster": s.get("poster") or "",
                         "site": s.get("site_name") or "",
                         "start_ep": s.get("start_episode"),
                         "total_ep": s.get("total_episode")}))

    seen = set()
    for h in hist:
        sid = int(h.get("id") or 0)
        if sid in live_ids or sid in seen:
            continue
        seen.add(sid)
        items.append(mk("arc", h.get("name"), h.get("year"),
                        h.get("type") or h.get("_mt"), "ARCHIVED", sid,
                        {"state_zh": "已完成归档", "season": h.get("season"),
                         "poster": h.get("poster") or "",
                         "aid": aid_by_sub.get(sid, 0),
                         "archived_at": str(h.get("date")
                                            or h.get("last_search") or "")[:19]}))

    for a in audits:
        st = str(a.get("status") or "")
        sid = int(a.get("sub_id") or 0)
        archived = bool(sid) and sid not in live_ids and st == "done"
        stz = {"auditing": "待审核", "rejected": "已拒绝"}.get(st)
        if not stz:
            stz = "已完成归档" if archived else "下载中"
        items.append(mk("req", a.get("title"), a.get("year"), a.get("mtype"),
                        st, sid,
                        {"aid": int(a.get("id") or 0), "state_zh": stz,
                         "tg": a.get("tg"), "tg_name": a.get("tg_name") or "",
                         "season": a.get("season"), "start_ep": a.get("start_ep"),
                         "total_ep": a.get("total_ep"),
                         "scope": a.get("scope") or "",
                         "archived": archived,
                         "created_at": str(a.get("created_at") or "")[:19]}))

    _CACHE.update(ts=time.time(), items=items)
    return items


def _mp_get(path: str, params: dict | None = None) -> dict | None:
    try:
        j = _mp_req("GET", path, params=params)
        return j if isinstance(j, dict) else None
    except Exception as e:
        logger.debug(f"MP GET {path} 失败: {e}")
        return None


# ═════════════════════════════════════════════ 渲染
def _card(it: dict, idx: int, sel: set[str]) -> list[str]:
    tl = ui.title_link(it.get("name"), "", it.get("mtype"), 22)
    y = f"（{_t(it.get('year'), 6)}）" if it.get("year") else ""
    tag = "📺" if (it.get("mtype") or "") == "电视剧" else "🎬"
    ln: list[str] = []
    if it["kind"] == "arc":
        ln.append(f"{ui.ON}<b>{idx}.</b> {tag} <b>{tl}</b>{_t(y, 8)}")
        ln.append("　　📦 <b>已完成归档</b>　MoviePilot 已移除订阅（文件已在 Emby）")
        return ln
    mark = ui.mark(_key(it) in sel) if it["kind"] == "sub" else "　"
    stz = it.get("state_zh") or it.get("state")
    ln.append(f"{mark}<b>{idx}.</b> {tag} <b>{tl}</b>{_t(y, 8)}　<i>{_t(stz, 8)}</i>")
    pr = it.get("prog") or {}
    if pr.get("n"):
        spd = f" ⚡{hum_speed(pr['dl'])}" if pr.get("dl") else ""
        eta = f" ⏳{_eta(pr['eta'])}" if pr.get("eta") else ""
        ln.append(f"　　📶 <code>{_bar(pr['pct'])}</code> <b>{pr['pct']}%</b>"
                  f" {hum_size(pr['done'])}/{hum_size(pr['size'])}{spd}{eta}")
        ln.append(f"　　🗂 {pr['n']} 个任务 · 做种 {pr.get('seeds', 0)}")
    else:
        ln.append("　　📶 <code>" + _bar(0) + "</code> 无下载任务")
    if it["kind"] == "req":
        ln.append(f"　　👤 {_t(it.get('tg_name') or '—', 14)}"
                  f"　📄 {_t(it.get('scope') or '全片', 22)}")
        if it["state"] == "auditing":
            ln.append("　　💡 用 /reqs 审核这条申请")
    return ln


def _key(it: dict) -> str:
    return f"{it['kind']}:{it['sub_id']}"


def _panel_text(items: list[dict], page: int, uid: int) -> str:
    sel = _sel_of(uid)
    sub = [x for x in items if x["kind"] == "sub"]
    arc = [x for x in items if x["kind"] == "arc"]
    req = [x for x in items if x["kind"] == "req"]
    n_dl = sum(1 for x in items if (x.get("prog") or {}).get("state") == "downloading")
    tot_dl = sum((x.get("prog") or {}).get("dl") or 0 for x in items)
    pages = max(1, (len(items) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    base = page * _PAGE
    head = (f"📌 在途订阅 <b>{len(sub)}</b>　📦 归档 <b>{len(arc)}</b>"
            f"　🙏 求片 <b>{len(req)}</b>")
    stat = f"{ui.ON} 已勾选 <b>{sum(1 for x in items if _key(x) in sel)}</b>/{len(items)}"
    if n_dl:
        stat += f"　⬇️ 下载中 <b>{n_dl}</b>"
    if tot_dl:
        stat += f"　⚡ 总速 <b>{hum_speed(tot_dl)}</b>"
    lines = ["🎯 <b>订阅与求片管理中心</b>", _div(), head, stat, _div()]
    if not items:
        lines += ["🎉 当前没有任何订阅、归档或求片", _div(),
                  "💡 用 /qp 片名 求一部片试试"]
        return ui.clip_html("\n".join(lines))
    for k, it in enumerate(items[base:base + _PAGE], 1):
        lines += _card(it, base + k, sel)
    tail = []
    if pages > 1:
        tail.append(f"📄 第 <b>{page + 1}</b>/{pages} 页，⬅️ ➡️ 翻页")
    tail.append("💡 点条目按钮切换 <b>勾选</b>；再用下方「全部下载 / 全部暂停 / "
                "优先下载」批量操作（归档与求片不参与批量）")
    tail.append(f"🕒 {time.strftime('%H:%M')}　（每 20 秒自动刷新进度）")
    room = max(220, 1024 - ui.visible_len("\n".join(tail)) - 1)
    return ui.clip_html("\n".join(lines), room) + "\n" + "\n".join(tail)


def _panel_kb(items: list[dict], page: int, uid: int) -> KM:
    sel = _sel_of(uid)
    pages = max(1, (len(items) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    base = page * _PAGE
    rows: list[list[B]] = []
    row: list[B] = []
    for k, it in enumerate(items[base:base + _PAGE], 1):
        idx = base + k
        nm = _t(it.get("name"), 12)
        if it["kind"] == "sub":
            row.append(B(f"{ui.mark(_key(it) in sel)}{idx}. {nm}",
                         f"sm:t:{idx}:{page}"))
        else:
            row.append(B(f"{'📦' if it['kind'] == 'arc' else '🙏'}{idx}. {nm}",
                         f"sm:d:{idx}:{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️ 上一页", f"sm:p:{page - 1}"))
        nav.append(B(f"📄 {page + 1}/{pages}", "sm:noop"))
        if page < pages - 1:
            nav.append(B("下一页 ➡️", f"sm:p:{page + 1}"))
        rows.append(nav)
    rows.append([B("☑️ 全选本页", f"sm:all:{page}"),
                 B("✅ 全选全部", f"sm:alla:{page}"),
                 B(f"{ui.OFF} 全不选", f"sm:none:{page}")])
    rows.append([B("⬇️ 全部下载", f"sm:act:search:{page}"),
                 B("⏸ 全部暂停", f"sm:act:pause:{page}")])
    rows.append([B("▶️ 全部恢复", f"sm:act:resume:{page}"),
                 B("🔝 优先下载", f"sm:act:priority:{page}")])
    rows.append([B("🔄 刷新", "sm:home"),
                 B("📋 待审核求片", "sm:reqs"),
                 B("❌ 关闭", "sm:close")])
    return KM(rows)


async def _redraw(msg: Message, page: int, uid: int, force: bool = False) -> None:
    items = await asyncio.to_thread(build_list, force)
    try:
        await msg.edit_text(_panel_text(items, page, uid), parse_mode=PM,
                            disable_web_page_preview=True,
                            reply_markup=_panel_kb(items, page, uid))
    except Exception as e:
        logger.debug(f"面板重绘失败（多半是内容没变）: {e}")


# ═════════════════════════════════════════════ 批量操作
async def _do_batch(act: str, ids: list[int]) -> dict:
    """执行批量操作，返回逐条回执（与 Web 端返回结构一致）。"""
    tors = await asyncio.to_thread(_qb_tors)
    detail = []

    def find_sub(sid):
        try:
            for s in mp_subscribe_list(200):
                if int(s.get("id") or 0) == int(sid):
                    return s
        except Exception:
            pass
        return None

    if act in ("search", "kick"):
        for sid in ids:
            s = find_sub(sid)
            if not s:
                detail.append({"id": sid, "ok": False, "found": False,
                               "msg": "订阅不存在（可能已完成归档）"})
                continue
            name = s.get("name") or ""
            before = _match_tors(name, tors)
            ok = await asyncio.to_thread(mp_kick_search, sid)
            if not ok:
                detail.append({"id": sid, "ok": False, "found": False,
                               "name": name, "msg": "搜索请求失败"})
                continue
            await asyncio.sleep(2.0)
            t2 = await asyncio.to_thread(_qb_tors)
            after = _match_tors(name, t2)
            pr = _prog(after)
            new = len(after) - len(before)
            if after:
                detail.append({"id": sid, "ok": True, "found": True, "name": name,
                               "new": new, "prog": pr,
                               "msg": "已找到资源并开始下载" if new > 0
                                      else "已在下载中（未发现新资源）"})
            else:
                detail.append({"id": sid, "ok": True, "found": False, "name": name,
                               "msg": "未找到资源（站点尚未收录，稍后自动重试）"})
    elif act in ("pause", "resume"):
        state = "P" if act == "pause" else "R"
        for sid in ids:
            s = find_sub(sid)
            if not s:
                detail.append({"id": sid, "ok": False,
                               "msg": "订阅不存在（可能已完成归档）"})
                continue
            ok, msg = await asyncio.to_thread(mp_sub_state, sid, state)
            name = s.get("name") or ""
            hs = [t.get("hash") for t in _match_tors(name, tors) if t.get("hash")]
            n = await asyncio.to_thread(_qb_pause if act == "pause" else _qb_resume, hs)
            detail.append({"id": sid, "ok": ok, "name": name,
                           "qb": n, "msg": ("已暂停" if act == "pause" else "已恢复")
                           + (f"（含 {n} 个下载任务）" if n else "")
                           if ok else (msg or "操作失败")})
    elif act == "priority":
        # ── 优先下载 ─────────────────────────────────────────────────
        # 语义：把勾中的条目提到最优先，**只暂停它们所属以外的任务**。
        # ⚠️ 老实现无差别暂停 qB 全部种子（paused_all=14 把无关真实任务也停了），
        #    2026-10-12 已修正。现在只碰「其它 MP 订阅名下的种子」，
        #    没有归属的种子（category 空）完全不碰。
        sel_h: set[str] = set()
        sel_names: list[str] = []
        for sid in ids:
            s = find_sub(sid)
            if not s:
                continue
            name = s.get("name") or ""
            sel_names.append(name)
            await asyncio.to_thread(mp_sub_state, sid, "R")
            await asyncio.to_thread(mp_kick_search, sid)
            for t in _match_tors(name, tors):
                if t.get("hash"):
                    sel_h.add(t.get("hash"))
        # 其它订阅名下的种子才允许暂停
        other_h: set[str] = set()
        keep = set(ids)
        try:
            for x in build_list(True):
                if x["kind"] != "sub" or x["sub_id"] in keep:
                    continue
                await asyncio.to_thread(mp_sub_state, x["sub_id"], "P")
                for t in _match_tors(x.get("name"), tors):
                    h = t.get("hash")
                    if h and h not in sel_h:
                        other_h.add(h)
        except Exception as e:
            logger.debug(f"优先下载：暂停其它订阅时出错 {e}")
        paused_n = 0
        if other_h:
            paused_n = await asyncio.to_thread(_qb_pause, sorted(other_h))
        if sel_h:
            await asyncio.to_thread(_qb_resume, sorted(sel_h))
        return {"ok": True, "detail": detail, "prio": len(sel_h),
                "paused_other": paused_n, "sel_names": sel_names[:20],
                "msg": f"已优先 {len(sel_h)} 个下载任务"
                       + (f"，暂停其它订阅的 {paused_n} 个任务" if paused_n
                          else "，其它订阅无需调整")}

    if act in ("search", "kick"):
        ok_n = sum(1 for x in detail if x.get("ok"))
        found_n = sum(1 for x in detail if x.get("found"))
        miss_n = sum(1 for x in detail if x.get("ok") and not x.get("found"))
        fail_n = len(detail) - ok_n
        parts = []
        if found_n:
            parts.append(f"{found_n} 条已找到资源并开始下载")
        if miss_n:
            parts.append(f"{miss_n} 条未找到")
        if fail_n:
            parts.append(f"{fail_n} 条失败")
        return {"ok": fail_n == 0, "detail": detail,
                "msg": "已触发搜索 %d 条：%s"
                       % (len(detail), "，".join(parts) or "无变化")}

    # pause / resume：措辞不能出现「未找到」
    ok_n = sum(1 for x in detail if x.get("ok"))
    miss_n = len(detail) - ok_n
    verb = "暂停" if act == "pause" else "恢复"
    msg = f"已{verb} {ok_n} 条订阅"
    if miss_n:
        msg += f"，{miss_n} 条不存在（可能已完成归档）"
    return {"ok": ok_n == len(detail), "detail": detail, "msg": msg}


def _result_text(act: str, r: dict) -> str:
    zh = {"search": "全部下载（立即搜索）", "pause": "全部暂停",
          "resume": "全部恢复", "priority": "优先下载"}
    ln = [f"🎯 <b>{zh.get(act, act)}</b>", _div(),
          f"📢 {_t(r.get('msg') or '完成', 60)}"]
    det = r.get("detail") or []
    is_dl = act in ("search", "kick")
    if det:
        ln.append(_div())
        for x in det[:10]:
            nm = _t(x.get("name") or f"#{x.get('id')}", 20)
            if not x.get("ok"):
                # 失败（订阅不存在等）
                ln.append(f"❌ <b>{nm}</b>：{_t(x.get('msg'), 40)}")
            elif is_dl and x.get("found"):
                pr = x.get("prog") or {}
                ln.append(f"✅ <b>{nm}</b>：已找到资源并开始下载")
                ln.append(f"　　<code>{_bar(pr.get('pct', 0))}</code> {pr.get('pct', 0)}%"
                          f" {hum_size(pr.get('done', 0))}/{hum_size(pr.get('size', 0))}"
                          + (f" ⚡{hum_speed(pr.get('dl') or 0)}" if pr.get('dl') else ""))
            elif is_dl:
                ln.append(f"⚠️ <b>{nm}</b>：未找到资源")
                ln.append(f"　　{_t(x.get('msg'), 40)}")
            else:
                # pause / resume：显示操作结果，不显示「未找到」
                n = x.get("qb") or 0
                ln.append(f"✅ <b>{nm}</b>：{_t(x.get('msg'), 40)}"
                          + (f" · 下载任务 {n} 个" if n else ""))
        if len(det) > 10:
            ln.append(f"…另有 {len(det) - 10} 条")
    ln.append(_div())
    ln.append(f"🕒 {time.strftime('%H:%M')}")
    return ui.clip_html("\n".join(ln))


# ═════════════════════════════════════════════ 命令
@bot.on_message(filters.command(["subs", "订阅", "submgr", "订阅管理"], **CMD))
@perm.gate("ptctl")
async def cmd_subs(client, msg: Message):
    """订阅与求片管理中心（管理员）"""
    items = await asyncio.to_thread(build_list, True)
    try:
        await msg.reply(_panel_text(items, 0, msg.from_user.id), parse_mode=PM,
                        disable_web_page_preview=True,
                        reply_markup=_panel_kb(items, 0, msg.from_user.id))
    except Exception as e:
        logger.warning(f"订阅面板发送失败: {e}")


async def _run_batch_cmd(msg: Message, act: str, name: str) -> None:
    """批量命令的公共实现：读当前勾选项 → 执行 → 回执（立即反馈）。"""
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 只有管理员能用", parse_mode=PM)
        return
    sel = _sel_of(msg.from_user.id)
    if not sel:
        await msg.reply(f"⚠️ 请先打开 /subs 勾选要操作的订阅，再用「{name}」",
                        parse_mode=PM)
        return
    ids = [int(k.split(":")[1]) for k in sel
           if k.startswith("sub:") and k.split(":")[1].isdigit()]
    if not ids:
        await msg.reply("⚠️ 勾选项里没有可操作的订阅（归档与求片不参与批量）",
                        parse_mode=PM)
        return
    m = await msg.reply(f"⏳ 正在执行「{name}」，共 {len(ids)} 条…", parse_mode=PM)
    r = await _do_batch(act, ids)
    try:
        await m.edit_text(_result_text(act, r), parse_mode=PM,
                          disable_web_page_preview=True,
                          reply_markup=KM([[B("↩️ 回到管理中心", "sm:home")]]))
    except Exception as e:
        logger.warning(f"批量回执编辑失败: {e}")


@bot.on_message(filters.command(["subdl", "全部下载", "subsearch"], **CMD))
@perm.gate("ptctl")
async def cmd_subdl(client, msg: Message):
    await _run_batch_cmd(msg, "search", "全部下载")


@bot.on_message(filters.command(["subpause", "全部暂停"], **CMD))
@perm.gate("ptctl")
async def cmd_subpause(client, msg: Message):
    await _run_batch_cmd(msg, "pause", "全部暂停")


@bot.on_message(filters.command(["subresume", "全部恢复"], **CMD))
@perm.gate("ptctl")
async def cmd_subresume(client, msg: Message):
    await _run_batch_cmd(msg, "resume", "全部恢复")


@bot.on_message(filters.command(["subprio", "优先下载"], **CMD))
@perm.gate("ptctl")
async def cmd_subprio(client, msg: Message):
    await _run_batch_cmd(msg, "priority", "优先下载")


# ═════════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^sm:"))
async def cb_sm(client, q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ 只有管理员能用", show_alert=True)
        return
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    # 回调格式（page 恒为最后一节数字）：
    #   sm:t:<idx>:<page>   sm:d:<idx>:<page>   sm:redl:<idx>:<page>
    #   sm:all:<page>  sm:alla:<page>  sm:none:<page>  sm:p:<page>
    #   sm:act:<which>:<page>  sm:home  sm:reqs  sm:noop  sm:close
    page = int(parts[-1]) if parts[-1].isdigit() else 0

    if act == "noop":
        await q.answer("这是页码提示，用 ⬅️ ➡️ 翻页")
        return
    if act == "close":
        await q.answer("已关闭")
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if act == "home":
        await q.answer("已刷新")
        await _redraw(q.message, 0, q.from_user.id, True)
        return
    if act == "reqs":
        await q.answer()
        rows = await asyncio.to_thread(db.list_audits, "auditing", 12)
        if not rows:
            await q.answer("当前没有待审核的求片", show_alert=True)
            return
        ln = [f"📋 <b>待审核求片 · {len(rows)} 条</b>", _div()]
        for a in rows:
            y = f"（{_t(a.get('year'), 6)}）" if a.get("year") else ""
            ln.append(f"#{a['id']}　🎬 <b>"
                      f"{ui.title_link(a.get('title'), a.get('tmdb_id'), a.get('mtype'), 22)}"
                      f"</b>{y}")
            ln.append(f"　👤 {_t(a.get('tg_name') or '—', 14)}"
                      f"　📄 {_t(a.get('scope') or '全片', 22)}")
        ln.append(_div())
        ln.append("💡 用 /reqs 查看详情并审核")
        await _redraw(q.message, page, q.from_user.id)
        await q.message.reply(ui.clip_html("\n".join(ln)), parse_mode=PM,
                              disable_web_page_preview=True)
        return

    items = await asyncio.to_thread(build_list, True)

    if act == "p":
        await q.answer()
        await _redraw(q.message, page, q.from_user.id)
        return

    if act == "t":
        # 切换勾选（序号 1 基 ⇒ 取值 -1）
        idx = int(parts[2])
        if not 1 <= idx <= len(items):
            await q.answer("该条目已不存在，刷新一下", show_alert=True)
            await _redraw(q.message, page, q.from_user.id, True)
            return
        it = items[idx - 1]
        if it["kind"] != "sub":
            await q.answer("归档/求片条目不能勾选（归档请用「重新下载」）",
                           show_alert=True)
            return
        sel = _sel_of(q.from_user.id)
        k = _key(it)
        if k in sel:
            sel.discard(k)
            await q.answer(f"{ui.OFF} 已取消勾选")
        else:
            sel.add(k)
            await q.answer(f"{ui.ON} 已勾选")
        items = await asyncio.to_thread(build_list, False)
        try:
            await q.message.edit_reply_markup(_panel_kb(items, page, q.from_user.id))
        except Exception:
            pass
        return

    if act == "d":
        # 详情：归档 → 重新下载；求片 → 提示去审核
        idx = int(parts[2])
        if not 1 <= idx <= len(items):
            await q.answer("该条目已不存在", show_alert=True)
            return
        it = items[idx - 1]
        if it["kind"] == "arc":
            await q.answer()
            tl = ui.title_link(it.get("name"), "", it.get("mtype"), 26)
            await q.message.reply(ui.clip_html("\n".join([
                "📦 <b>归档详情</b>", _div(),
                f"🎬 <b>{tl}</b>",
                f"📌 原订阅 <code>#{it['sub_id']}</code>（MoviePilot 已移除）",
                f"📅 {_t(it.get('archived_at') or '—', 20)}", _div(),
                "ℹ️ 归档 = 订阅已完成、文件已在 Emby，<b>正常观看不用重下</b>。",
                "只有文件被删 / 想换更高画质时才需要重新下载。",
                _div(),
                "💡 重新下载会重建订阅并立即搜索，完整下载一遍；"
                "同一条申请<b>不重复扣灵石</b>。",
            ])), parse_mode=PM, disable_web_page_preview=True,
                reply_markup=KM([
                    [B("🔄 重新下载", f"sm:redl:{idx}:{page}")],
                    [B("↩️ 返回管理中心", "sm:home")]]))
            return
        await q.answer("求片条目请用 /reqs 审核", show_alert=True)
        return

    if act == "redl":
        idx = int(parts[2])
        if not 1 <= idx <= len(items):
            await q.answer("该条目已不存在", show_alert=True)
            return
        it = items[idx - 1]
        aid = int(it.get("aid") or 0)
        if not aid:
            await q.answer("这条归档没有对应的求片申请记录，请在群里重新 /qp 求片",
                           show_alert=True)
            return
        await q.answer("正在重新下载…")
        a = await asyncio.to_thread(db.get_audit, aid)
        if not a:
            await q.answer("找不到原始申请，无法重下", show_alert=True)
            return
        from .request import push_audit_to_mp
        ok, note, sub_id, n_sub, kicked = await push_audit_to_mp(a, charge=False)
        if ok:
            await asyncio.to_thread(db.set_audit, aid, "done", q.from_user.id,
                                    note, sub_id)
        else:
            await asyncio.to_thread(db.set_audit, aid, "failed", q.from_user.id, note)
        logger.info(f"管理员 {q.from_user.id} 重下归档 #{aid} {it.get('name')}"
                    f"｜ok={ok} note={note}")
        await _redraw(q.message, page, q.from_user.id, True)
        await q.answer(("✅ 已重新建立订阅" + ("，并已触发立即搜索" if kicked
                                              else "，等下一轮自动搜索"))
                       if ok else f"⚠️ 重下失败：{_t(note, 50)}", show_alert=True)
        return

    if act == "all":
        sel = _sel_of(q.from_user.id)
        p0 = page * _PAGE
        for it in items[p0:p0 + _PAGE]:
            if it["kind"] == "sub":
                sel.add(_key(it))
        await q.answer(f"已全选本页（{len(sel)} 条）")
        await _redraw(q.message, page, q.from_user.id)
        return

    if act == "alla":
        sel = _sel_of(q.from_user.id)
        for it in items:
            if it["kind"] == "sub":
                sel.add(_key(it))
        await q.answer(f"已全选全部可操作条目（{len(sel)} 条）")
        await _redraw(q.message, page, q.from_user.id)
        return

    if act == "none":
        _sel_of(q.from_user.id).clear()
        await q.answer("已清空勾选")
        await _redraw(q.message, page, q.from_user.id)
        return

    if act == "act":
        which = parts[2] if len(parts) > 2 else ""
        sel = _sel_of(q.from_user.id)
        ids = [int(k.split(":")[1]) for k in sel
               if k.startswith("sub:") and k.split(":")[1].isdigit()]
        if not ids:
            await q.answer("请先点条目按钮勾选要操作的订阅", show_alert=True)
            return
        _zh_act = {"search": "下载", "pause": "暂停", "resume": "恢复",
                   "priority": "优先下载"}
        await q.answer("正在%s…" % _zh_act.get(which, which))
        r = await _do_batch(which, ids)
        try:
            await q.message.edit_text(_result_text(which, r), parse_mode=PM,
                                      disable_web_page_preview=True,
                                      reply_markup=KM([[B("↩️ 回到管理中心", "sm:home")]]))
        except Exception as e:
            logger.warning(f"批量结果编辑失败: {e}")
            await q.message.reply(_result_text(which, r), parse_mode=PM,
                                  disable_web_page_preview=True)
        return

    await q.answer()
