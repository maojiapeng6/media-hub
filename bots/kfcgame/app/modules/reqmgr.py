"""求片管理台：按状态分组查看群友求片，可改状态、**拒绝后可反悔同意**

背景（2026-10-11 用户要求）
──────────────────────────────────────────────
  「加一个命令查看群友的求片，然后我可以修改状态，
    比如说我拒绝了，我后面还可以同意」

设计
──────────────────────────────────────────────
  /reqmgr  打开管理台（首页：五个状态入口 + 最近动态）
  入口按状态分组，每条求片都带操作按钮：
    · auditing（待审核） → ✅通过　❌驳回
    · rejected（已驳回） → ↩️**反悔同意**　（这是本次新增的关键能力）
    · approved / done（已通过/已下发） → ⏹️撤回（删 MP 订阅 + QB 种子）
    · failed（下发失败） → 🔁重试

★ 为什么同意要走完整链路而不是只改数据库状态：
  同意 = 真的重新推给 MoviePilot 搜片下载 ⇒ 复用 request.cb_audit 里的
  那套流程（mp_subscribe → mp_kick_search → add_wishlist → 扣灵石 →
  通知求片者 → 群播报）。之后 pt2cloud 自动上传 115、MT 自动整理入库、
  入库完成再自动播报 —— 整条链路不需要额外操作。
  ⚠️ 所以「同意」按钮是**真的重新下载**，不是把状态改成 approved 就完事。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import time

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import Message

# 本模块文案用 HTML 标签，必须显式指定 parse_mode
PM = ParseMode.HTML

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from . import perm
# ★ 双重编码修复的唯一实现在 db.dm()（不要在别处再复制一份）
from ..db import dm
from .request import _tell_user
from .request import _group_approved
from .request import _ulink
from .request import _fetch_img
from .request import push_audit_to_mp
from .request import sub_state
from .request import mp_sub_ids
from .request import mp_subscribe_del

CMD = dict(prefixes=S.PREFIX)

# ★ 已下发多久仍无进度就播报「站点暂无资源」（分钟）
IDLE_MINUTES = 45

# ═══════════════════════════════════════════ 状态定义
# 与 db.set_audit 的取值严格一致（见 request.py 顶部的注释）
ST_PENDING = "auditing"       # 待审核
ST_REJECT = "rejected"        # 已驳回
ST_APPROVED = "approved"      # 已通过（还没拿到订阅号）
ST_DONE = "done"              # 已下发订阅
ST_FAILED = "failed"          # 下发失败

# 五个管理台页签。★ 这里就是状态的完整定义，改一处即可。
TABS: list[tuple[str, str, str]] = [
    (ST_PENDING, "⏳ 待审核", "auditing"),
    (ST_REJECT, "↩️ 已驳回·可反悔", "rejected"),
    (ST_DONE, "🚀 已下发", "done"),
    (ST_APPROVED, "✅ 已通过", "approved"),
    (ST_FAILED, "⚠️ 失败·可重试", "failed"),
]

ST_ICON = {
    ST_PENDING: "⏳", ST_REJECT: "❌", ST_APPROVED: "✅",
    ST_DONE: "🚀", ST_FAILED: "⚠️",
}

# ── 勾选批量（2026-10-12 需求：待审核列表支持 通过/拒绝/勾选） ──
_SEL: set[int] = set()            # 已勾选的求片 id
_SEL_PAGE: dict[str, list[int]] = {}   # 各页签当前展示的行（供「全选本页」）

ST_TXT = {
    ST_PENDING: "待审核", ST_REJECT: "已驳回", ST_APPROVED: "已通过",
    ST_DONE: "已下发下载", ST_FAILED: "下发失败",
}


def _esc(s) -> str:
    """HTML 转义。必须**先转义再截断**（先截断会把 &lt; 砍成半成品实体）。"""
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _t(s, n: int = 60) -> str:
    return _esc(s)[:n]


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


def _year(a: dict) -> str:
    return f"（{_t(a.get('year'), 8)}）" if a.get("year") else ""


def _tlink(a: dict, n: int = 22) -> str:
    """片名 → 可点的 TMDB 超链接（求片管理台里不再出现纯文本片名）"""
    return ui.title_link(a.get("title"), a.get("tmdb_id"),
                         a.get("mtype"), n, mode="html")


def _line(a: dict) -> str:
    """一条求片的一行摘要（HTML）"""
    return (f"#{a['id']}　🎬 <b>{_tlink(a, 22)}</b>{_year(a)}"
            f"　📄 {_t(a.get('scope') or '全片', 18)}")


def _owner_line(a: dict) -> list[str]:
    """求片人身份卡片：**两行**（Emby 用户名 + TG 用户名）

    返回 list[str]，直接用 `lines += _owner_line(a)`。
    """
    try:
        tg = int(a.get("tg") or 0)
    except Exception:
        tg = 0
    if not tg:
        return ["　👤（用户已注销）"]
    try:
        emby = ui.emby_name_of(tg)
    except Exception:
        emby = None
    return ui.user_card(tg, a.get("tg_name") or "", a.get("tg_username") or "",
                        emby or "", label="", icon="　👤", mode="html",
                        indent="　　 ")


def _poster_of(a: dict) -> bytes | None:
    """取海报（poster 空则退回 backdrop），失败返回 None"""
    for k in ("poster", "backdrop"):
        u = str(a.get(k) or "")
        if not u:
            continue
        try:
            d = _fetch_img(u)
        except Exception:
            d = None
        if d:
            return d
    return None


def _stage_of(a: dict, prog: dict | None,
              archived: bool = False) -> tuple[str, str]:
    """求片当前阶段 → (图标, 说明文字)

    ★ 审核状态与下载进度是两回事，要分开看：
      auditing 还没批→ 无论有没有资源都显示「等待审核」
      rejected 被拒      → 显示「已驳回·可反悔」
      done 有 progress   → 显示真实下载阶段
      done 无 progress   → 显示「站点暂无资源」
      done 且订阅已被 MP 移除（archived）→ 显示「已完成并归档」

    ★ archived 必须优先于「站点暂无资源」：MoviePilot 在**订阅完成**时会
      直接删掉 subscribe 行，此时 pt2cloud 往往也没留记录 ——
      老逻辑就会把它显示成「站点暂无资源　订阅已挂着」，与事实完全相反。
    """
    st = dm(a.get("status") or "")
    if st == ST_PENDING:
        return "⏳", "等待管理员审核"
    if st == ST_REJECT:
        return "❌", "已驳回　↩️ 管理员可反悔同意"
    if st == ST_FAILED:
        return "⚠️", "下发失败　🔁 可重试"
    if prog:
        p = float(prog.get("progress") or 0)
        s = dm(prog.get("status") or "")
        if s in ("archived", "organized"):
            return "🎬", "已入库　Emby 可看"
        if s in ("uploaded", "uploading"):
            return "☁️", "已存 115　整理中"
        if p > 0:
            return "🔽", f"下载中　{p:.0f}%"
        return "🔽", "已找到资源　准备下载"
    if st == ST_DONE:
        if archived:
            return "✅", "已完成并归档　MoviePilot 已自动移除订阅"
        return "🔍", f"已订阅　站点暂无资源（{IDLE_MINUTES} 分钟无进度）"
    return "✅", "已通过"


def _idling_badge(a: dict, prog: dict | None, archived: bool = False) -> str:
    """「站点暂无资源」的醒目标记（已完成归档的不该带这个标记）"""
    if prog or archived or dm(a.get("status") or "") != ST_DONE:
        return ""
    return f"\n　 ⚠️ <b>咖啡站暂无资源</b>　订阅已挂着，出种即自动下载"


async def _live_ids() -> tuple[set[int], bool]:
    """取一次 MP 现存订阅 ID（含"是否取到过"的标记）

    只在 async 上下文里调一次，然后按行判断，避免每行都打一次 HTTP。
    """
    from .request import mp_sub_ids_fresh
    ids = await asyncio.to_thread(mp_sub_ids)
    ok = await asyncio.to_thread(mp_sub_ids_fresh)
    return ids, ok


def _arc_of(a: dict, live: set[int], ok: bool) -> bool:
    """这条求片的 MP 订阅是否已被移除（= 已完成归档）"""
    if not ok:
        return False                    # MP 取不到 ⇒ 不下结论
    try:
        sid = int(a.get("sub_id") or 0)
    except Exception:
        sid = 0
    return bool(sid) and sid not in live


# ═══════════════════════════════════════════ 我的求片（群友/ 管理员共用）
async def _mine_panel(uid: int) -> tuple[str, KM, bytes | None]:
    """某个人自己的全部求片 + 进度 + 状态（带海报）。

    ★ 每个人都能查自己的，管理员查自己的同理（要看全群用 /reqmgr）。
    """
    rows = await asyncio.to_thread(db.my_audits, uid, 20)
    name = ""
    try:
        c = db.get_cultivator(uid)
        name = str((c or {}).get("daohao") or "")
    except Exception:
        pass
    head = f"📋 <b>我的求片</b>" + (f"　—　{nm(name)}" if name else "")

    if not rows:
        return (head + "\n" + _div() + "\n📭 你还没有求过片\n" + _div()
                + "\n💡 群里发 <code>/qp 片名</code> 就能求\n"
                  "💡 求完在这里能看到审核状态与下载进度"), None, None

    # 一次批量取所有关键词的进度，避免 N 次查询
    keys = []
    for a in rows:
        for k in (a.get("title"), a.get("original")):
            k = dm(k or "")
            if len(k) >= 2 and k not in keys:
                keys.append(k)
    prog = await asyncio.to_thread(db.wish_progress, keys)
    live, live_ok = await _live_ids()

    def prog_of(a):
        for k in (a.get("title"), a.get("original")):
            k = dm(k or "")
            if len(k) >= 2 and prog.get(k):
                return prog[k]
        return None

    lines = [head, _div()]
    stat = {ST_PENDING: 0, ST_REJECT: 0, ST_DONE: 0,
            ST_APPROVED: 0, ST_FAILED: 0}
    for a in rows:
        st = dm(a.get("status") or "")
        if st in stat:
            stat[st] += 1
    lines.append(f"共 <b>{len(rows)}</b> 部　"
                 f"⏳待审 {stat[ST_PENDING]}　"
                 f"❌已驳 {stat[ST_REJECT]}　"
                 f"🚀已下 {stat[ST_DONE] + stat[ST_APPROVED]}")
    lines.append(_div())

    for i, a in enumerate(rows, 1):
        p = prog_of(a)
        arc = _arc_of(a, live, live_ok)
        icon, desc = _stage_of(a, p, arc)
        lines.append(f"{i}. {icon} <b>{_tlink(a, 24)}</b>{_year(a)}")
        lines.append(f"　 📄 {_t(a.get('scope') or '全片', 26)}")
        lines.append(f"　 {desc}{_idling_badge(a, p, arc)}")
        if p:
            pct = float(p.get("progress") or 0)
            lines.append(f"　 📶 `[{_bar(pct)}]` <b>{pct:.1f}%</b>")
        lines.append(f"　 🕒 {_t(str(a.get('created_at') or '')[5:16], 12)} 求")
        lines.append(_div())

    lines.append("💡 <code>/dl</code> 看实时速度　"
                 "出种后会自动下载并入库，无需再来催")
    text = ui.clip_html("\n".join(lines))
    return text, None, await asyncio.to_thread(_poster_of, rows[0])


# ═══════════════════════════════════════════ 今日总求片（管理员）
async def _today_panel() -> tuple[str, KM, bytes | None]:
    """今日全部求片：每条带状态与操作按钮，随时可改。"""
    rows = await asyncio.to_thread(db.today_audits, 40)
    now = dt.datetime.now().strftime("%m-%d %H:%M")
    lines = [f"🗓 <b>今日求片 · {now}</b>", _div()]
    if not rows:
        return ("🗓 <b>今日求片</b>\n" + _div()
                + "\n📭 今天还没有人求片\n" + _div()
                + "\n💡 群友发 <code>/qp 片名</code> 提交后会自动出现在这里"), \
            KM([[B("🗂 全部状态", "rm:home")]]), None

    # 统计
    cnt = {}
    for a in rows:
        st = dm(a.get("status") or "")
        cnt[st] = cnt.get(st, 0) + 1
    stat = "　".join(
        f"{ST_ICON.get(k, '')}{ST_TXT.get(k, k)} {v}"
        for k, v in cnt.items())
    lines.append(f"今日 <b>{len(rows)}</b> 部：{stat}")
    lines.append(_div())

    keys = []
    for a in rows:
        for k in (a.get("title"), a.get("original")):
            k = dm(k or "")
            if len(k) >= 2 and k not in keys:
                keys.append(k)
    prog = await asyncio.to_thread(db.wish_progress, keys)
    live, live_ok = await _live_ids()

    def prog_of(a):
        for k in (a.get("title"), a.get("original")):
            k = dm(k or "")
            if len(k) >= 2 and prog.get(k):
                return prog[k]
        return None

    for a in rows[:12]:
        st = dm(a.get("status") or "")
        p = prog_of(a)
        arc = _arc_of(a, live, live_ok)
        icon, desc = _stage_of(a, p, arc)
        lines.append(f"#{a['id']}　{icon} <b>{_tlink(a, 20)}</b>{_year(a)}")
        lines += _owner_line(a)
        lines.append(f"　 📄 {_t(a.get('scope') or '全片', 14)}")
        lines.append(f"　 {desc}")
        if p:
            pct = float(p.get("progress") or 0)
            lines.append(f"　 📶 `[{_bar(pct)}]` <b>{pct:.0f}%</b>")
        if st == ST_DONE and not p and not arc:
            lines.append(f"　 ⏱️ 已等待 {_t(str(a.get('created_at') or '')[5:16], 12)}")
        lines.append(_div())

    kb = [_row_btns(a, dm(a.get("status") or "")) for a in rows[:10]]
    kb.append([B("🗂 全部状态", "rm:home"), B("🔄 刷新", "rm:today")])
    return ui.clip_html("\n".join(lines)), KM(kb), \
        await asyncio.to_thread(_poster_of, rows[0])


def nm(s: str) -> str:
    return _esc(s)


def _bar(pct: float, width: int = 14) -> str:
    """进度条 —— 与 inbox.bar() 完全同款（█ 实心 / ░ 空心）"""
    pct = max(0.0, min(100.0, float(pct or 0)))
    n = int(round(pct / 100 * width))
    return "█" * n + "░" * (width - n)


# ═══════════════════════════════════════════ 面板
async def _panel(status: str = "", page: int = 0,
                 group_mode: bool = False) -> tuple[str, KM]:
    """求片管理台。status 为空 = 首页概览。

    group_mode=True ⇒ 在**群里**打开：隐藏「🗑 彻底删除」这类不可逆操作，
    免得在群聊里手滑把记录清掉（2026-10-13：待审核面板开放到群里）。
    """
    if not status:
        return await _home(group_mode=group_mode)

    rows = await asyncio.to_thread(db.list_audits, status, 30)
    title = dict((s, t) for s, t, _ in TABS).get(status, status)
    lines = [f"🗂 <b>求片管理 · {title}</b>", _div()]

    if not rows:
        lines.append("这里现在是空的")
        if status == ST_REJECT:
            lines.append("　（驳回过的求片会留在这里，随时可以反悔同意）")
        elif status == ST_PENDING:
            lines.append("　（群友 `/qp 片名` 提交后会自动出现在这里）")
    else:
        # 「已下发」页签要显示下载进度 + 哪些在等站点出种 ⇒ 批量查一次
        prog: dict = {}
        if status == ST_DONE:
            keys = []
            for a in rows:
                for k in (a.get("title"), a.get("original")):
                    k = dm(k or "")
                    if len(k) >= 2 and k not in keys:
                        keys.append(k)
            if keys:
                prog = await asyncio.to_thread(db.wish_progress, keys)

        def _prog_of(a):
            for k in (a.get("title"), a.get("original")):
                k = dm(k or "")
                if len(k) >= 2 and prog.get(k):
                    return prog[k]
            return None

        # ★ MP 现存订阅 ID：用来区分「在途」和「已完成归档」
        #   （MoviePilot 完成订阅时会直接删掉 subscribe 行）
        live, live_ok = await _live_ids()

        _SEL_PAGE[status] = [int(a["id"]) for a in rows[:12]]
        for a in rows:
            arc = _arc_of(a, live, live_ok)
            # 勾选态：待审核/已驳回两个页签支持批量操作
            if status in (ST_PENDING, ST_REJECT):
                mark = ui.ON if int(a["id"]) in _SEL else ui.OFF
            else:
                mark = "　"
            prio = int(a.get("prio") or 0)
            star = "⭐" if prio else ""
            # ST_ICON 在这里是有用的（这里没显示 TABS 的图标名）
            lines.append(f"{mark} {star}" + _line(a) + f"　{ST_ICON.get(status, '')}"
                         f"{ST_TXT.get(status, status)}")
            lines += _owner_line(a)
            # ★ 版本备注：求片人标注的指定版本，管理员照着找资源
            if str(a.get("remark") or "").strip():
                lines.append(f"　 📝 <b>指定版本</b>：{_t(a['remark'], 44)}")
            # 补充上下文：驳回的原因 / 失败的原因 / 已下发的订阅号
            if a.get("note"):
                lines.append(f"　 📌 {_t(a['note'], 44)}")
            if status == ST_DONE and a.get("sub_id"):
                sid = int(a["sub_id"])
                lines.append(f"　 🔗 MoviePilot 订阅 <code>#{sid}</code>"
                             + ("　✅ <b>已完成，MP 已自动移除</b>" if arc else ""))
            if a.get("ep_count"):
                lines.append(f"　 📦 约 {int(a['ep_count'])} 个下载任务")
            if status == ST_DONE:
                p = _prog_of(a)
                if p:
                    pct = float(p.get("progress") or 0)
                    lines.append(f"　 📶 `[{_bar(pct)}]` <b>{pct:.1f}%</b>")
                elif arc:
                    lines.append("　 ✅ <b>已完成并归档</b>"
                                 "　订阅已完成，MoviePilot 已自动移除订阅")
                else:
                    lines.append("　 ⚠️ <b>咖啡站暂无资源</b>"
                                 "　订阅已挂着，出种即自动下载")
            lines.append(_div())

    kb = []
    for a in rows[:12]:
        aid = int(a["id"])
        # 勾选行（仅待审核/已驳回页签）
        if status in (ST_PENDING, ST_REJECT):
            kb.append([B(f"{ui.ON if aid in _SEL else ui.OFF} "
                         f"{_t(a.get('title') or '', 18)}", f"rm:sel:{status}:{aid}")])
        kb.append(_row_btns(a, status, group_mode=group_mode))
    # 批量操作栏
    if status in (ST_PENDING, ST_REJECT) and rows:
        n = len(_SEL)
        kb.append([B("☑️ 全选本页", f"rm:selall:{status}"),
                   B("⬜ 全不选", f"rm:selnone:{status}")])
        if status == ST_PENDING:
            kb.append([B(f"✅ 批量通过 {n}", f"rm:batch:{status}:ok"),
                       B(f"❌ 批量驳回 {n}", f"rm:batch:{status}:no")])
        else:
            kb.append([B(f"↩️ 批量反悔同意 {n}", f"rm:batch:{status}:ok")])
    # 页签导航（当前所在页显示为「🔄 刷新」）
    nav = []
    for s, t, _ in TABS:
        nav.append(B(("🔄 " + t) if s == status else t, f"rm:tab:{s}"))
    nav.append(B("🏠 概览", "rm:home"))
    for i in range(0, len(nav), 3):
        kb.append(nav[i:i + 3])
    return ui.clip_html("\n".join(lines)), KM(kb)


def _row_btns(a: dict, status: str, group_mode: bool = False) -> list[B]:
    """每条求片的操作按钮 —— 状态不同，能做的事不同

    ★ 2026-10-13 新增两条（用户点名要的）：
      · ⭐ 优先 / ☆ 取消优先 —— 待审核条目置顶，管理员一眼看到要先处理谁
      · 🔁 再次下发 —— **重复通过**：已通过/已下发的还能再推一次
        （站点出了更高版本 / 之前失败想重来），且不重复扣灵石
    """
    aid = a["id"]
    prio = int(a.get("prio") or 0)
    # group_mode 下不暴露「彻底删除」（不可逆，群里容易手滑）
    dele = [] if group_mode else [B("🗑 删除", f"rm:del:{aid}")]

    if status == ST_PENDING:
        return [B("✅ 通过", f"rm:ok:{aid}"), B("❌ 驳回", f"rm:no:{aid}"),
                B("⭐ 优先" if not prio else "☆ 取消优先", f"rm:prio:{aid}")]
    if status == ST_REJECT:
        # ★ 反悔同意：重新走完整链路（真下载，不是改改数据库）
        return [B("↩️ 反悔同意", f"rm:ok:{aid}")] + dele
    if status in (ST_DONE, ST_APPROVED):
        # ★ 重复通过 = 再推一次 MoviePilot（不重复扣费）
        return [B("🔁 再次下发", f"rm:again:{aid}"),
                B("⏹️ 撤回", f"rm:stop:{aid}")] + dele
    if status == ST_FAILED:
        return [B("🔁 重试下发", f"rm:ok:{aid}")] + dele
    return dele or [B("🗑 删除", f"rm:del:{aid}")]


async def _home(group_mode: bool = False) -> tuple[str, KM]:
    """首页：今日概览 + 五个状态入口 + 等资源提醒"""
    counts = {}
    for s, _t2, _k in TABS:
        counts[s] = await asyncio.to_thread(db.count_audits, s)
    total = sum(counts.values())
    today_rows = await asyncio.to_thread(db.today_audits, 40)
    today = len(today_rows)

    lines = ["🗂 <b>求片管理台</b>", _div()]
    lines.append(f"📅 今日求片 <b>{today}</b> 部　│　全部 <b>{total}</b> 条")
    if total == 0:
        lines.append("还没有任何求片记录")
    else:
        for s, t, _k in TABS:
            n = counts[s]
            # TABS 里的名字自带图标，别再叠 ST_ICON（会出现「⏳ ⏳ 待审核」）
            lines.append(f"　{t}　<b>{n}</b> 条" if n else f"　{t}　0 条")
    lines.append(_div())

    # 有多少部正在「等资源」—— 这是用户最关心的信息之一
    try:
        idle = await asyncio.to_thread(db.idle_subscribed, IDLE_MINUTES, 20)
    except Exception:
        idle = []
    # ★ 已完成归档的（MP 已自动移除订阅）不该算「在等资源」
    live, live_ok = await _live_ids()
    if live_ok:
        idle = [a for a in idle if not _arc_of(a, live, live_ok)]
    if idle:
        lines.append(f"🔍 <b>{len(idle)}</b> 部在等站点出种：")
        for a in idle[:3]:
            lines.append(f"　· 🎬 {_tlink(a, 20)}{_year(a)}")
        if len(idle) > 3:
            lines.append(f"　· …还有 {len(idle) - 3} 部")
    lines.append(_div())
    lines.append("👉 点下面按钮按状态查看，<b>随时可改</b>")
    if counts.get(ST_REJECT):
        lines.append(f"💡 有 <b>{counts[ST_REJECT]}</b> 条被驳回，随时可反悔同意")
    if counts.get(ST_PENDING):
        lines.append("⭐ 待审核里点「优先」可把某条顶到最前；"
                     "已下发的还能「🔁 再次下发」重下一次")

    kb = [[B("📅 今日求片", "rm:today"), B("🗂 全部状态", "rm:home")]]
    for s, t, _k in TABS:
        n = counts[s]
        kb.append([B(f"{t}{f'· {n}' if n else ''}", f"rm:tab:{s}")])
    kb.append([B("🔄 刷新", "rm:home")])
    return ui.clip_html("\n".join(lines)), KM(kb)


# ═══════════════════════════════════════════ 编辑助手
async def _edit(msg: Message, text: str, kb: KM | None) -> None:
    """就地改卡片（图文都试一遍）"""
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(text, reply_markup=kb, parse_mode=PM)
        else:
            await msg.edit_text(text, reply_markup=kb, parse_mode=PM,
                                disable_web_page_preview=True)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.debug(f"编辑求片管理卡片失败: {e}")
        try:
            await msg.reply(text, reply_markup=kb, parse_mode=PM,
                           disable_web_page_preview=True)
        except Exception as e2:
            logger.warning(f"求片管理卡片重发仍失败: {e2}")


# ═══════════════════════════════════════════ 站点暂无资源 → 群播报
_IDLE_RUNNING = {"ts": 0.0}


async def _send_with_poster(text: str, poster: bytes | None) -> bool:
    """发一条带海报的群消息；没海报退回纯文字。返回是否成功"""
    from ..helper import send_group, send_group_photo, warmup
    try:
        await warmup(bot)
        if poster:
            if await send_group_photo(bot, poster, text, parse_mode=PM):
                return True
        return await send_group(bot, text, parse_mode=PM,
                                disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"暂无资源播报发送失败：{e}")
        return False


def _idle_text(a: dict) -> str:
    """「站点暂无资源」的播报文案（HTML，配海报发）"""
    try:
        tg = int(a.get("tg") or 0)
    except Exception:
        tg = 0
    lines = [
        "🔍 <b>咖啡站暂未找到资源</b>",
        _div(),
        f"🎬 <b>{_tlink(a, 26)}</b>{_year(a)}",
        f"📄 范围：{_t(a.get('scope') or '全片', 24)}",
    ]
    if tg:
        lines += _owner_line(a)
    lines += [
        _div(),
        f"⏱️ 订阅已建立，等待 <b>{IDLE_MINUTES}</b> 分钟仍无进度",
        "🔁 <b>订阅不会被撤销</b>　站点一出种就自动下载",
        "",
        "🌐 已搜过：<b>咖啡站 ptcafe.club</b>（站内暂无该资源）",
        "💡 可以换个关键词重求，或等站方补种",
        f"\n🕒 求片时间 {_t(str(a.get('created_at') or '')[5:16], 12)}",
    ]
    return ui.clip_html("\n".join(lines))


async def check_idle_subs() -> int:
    """定时巡检：把「已下发但久无进度」的求片播报到群里。返回播报条数。

    ★ 为什么必须 bot 主动查，而不是等 MP 通知：
      MoviePilot 搜不到资源时**完全不通知任何人**，只在日志写一行
      「分页搜索暂停（cooldown），稍后继续」⇒ 从外面看就是「求了没反应」。
      所以只能 bot 自己比对：status=done + pt_uploads 里查不到任何进度。
    """
    now = time.time()
    if now - _IDLE_RUNNING["ts"] < 300:      # 5 分钟防抖，别刷屏
        return 0
    _IDLE_RUNNING["ts"] = now
    if not (S.GROUP and int(S.GROUP[0])):
        return 0
    try:
        rows = await asyncio.to_thread(db.idle_subscribed, IDLE_MINUTES, 20)
    except Exception as e:
        logger.warning(f"巡检等资源失败：{e}")
        return 0
    if not rows:
        return 0
    # ★ 已完成归档的（MP 已自动移除订阅）绝不能再播报「站点暂无资源」——
    #   那是假消息：片子可能早就下好并入库了。
    live, live_ok = await _live_ids()
    if live_ok:
        rows = [a for a in rows if not _arc_of(a, live, live_ok)]
    if not rows:
        return 0
    n = 0
    for a in rows:
        poster = await asyncio.to_thread(_poster_of, a)
        if await _send_with_poster(_idle_text(a), poster):
            await asyncio.to_thread(db.mark_idle_notified, int(a["id"]))
            n += 1
            logger.info(f"已播报暂无资源：#{a['id']} {_esc(a.get('title'))}")
            await asyncio.sleep(1.5)
    if n and S.OWNER:
        # 顺手私聊 owner 一份，他未必在群里盯着
        try:
            await bot.send_message(
                int(S.OWNER or 0),
                f"🔍 有 <b>{n}</b> 部求片在咖啡站等资源，已在群里播报：\n"
                + "\n".join(f"· #{a['id']} {_tlink(a, 24)}"
                            for a in rows[:8]),
                parse_mode=PM, disable_web_page_preview=True)
        except Exception:
            pass
    return n


# ═══════════════════════════════════════════ 同意：走完整链路
async def _approve(client, msg: Message, aid: int, who,
                   note: str = "", charge: bool = True) -> None:
    """同意一条求片 —— **重新推给 MoviePilot 走全链路**。

    charge=False 用于「🔁 再次下发（重复通过）」：同一条申请已经扣过灵石，
    重下一次不能再扣一遍（与 push_audit_to_mp 的 charge 语义一致）。


    与 request.cb_audit 的通过分支完全一致（抽出来是为了让
    「反悔同意」和「正常通过」走同一段代码，不会各自跑偏）：
      mp_subscribe → 扣灵石 → add_wishlist → mp_kick_search → set_audit(done)
      → 通知求片者 → 群播报
    之后 pt2cloud 自动上传 115、MT 自动整理入库、入库完成自动播报。
    """
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await _edit(msg, "❌ 该申请不存在（可能已被清理）", None)
        return

    tail = ""
    if note:
        tail = f"\n（{_t(note, 60)}）"
    # 先给个「正在下发」的即时反馈，免得等MP 时看起来像没反应
    await _edit(msg, _line(a) + "\n" + _div()
                + "\n⏳ 正在推送给 MoviePilot…", None)

    await asyncio.to_thread(db.set_audit, aid, ST_APPROVED, who.id, "已同意")

    # ★ 走全项目唯一的推送实现 —— 勾选式选集（按连续段拆多条订阅）、
    #   扣灵石、登记求片清单、立即触发搜索全在里面。
    #   以前这里是 request.cb_audit 的复制粘贴版，加了选集后没同步 ⇒
    #   管理台审批多集请求会**只下第一段**，其余集静默丢失。
    ok, mnote, sub_id, n_sub, kicked = await push_audit_to_mp(a, charge=charge)

    if ok:
        await asyncio.to_thread(db.set_audit, aid, ST_DONE, who.id, mnote, sub_id)
        # ★ 处理完就取消「优先」标记，免得它一直顶在列表最前面
        await asyncio.to_thread(db.set_audit_prio, aid, 0)
        # ★ 重新下发 = 新的等待周期 ⇒ 清掉「已播报过暂无资源」标记，
        #   否则反悔同意后若站点仍没资源，就再也不会播报了。
        await asyncio.to_thread(db.clear_idle_notified, aid)
        res = ("⚡ 已触发 MoviePilot 立即搜索" if kicked
               else "⏳ 已加入订阅，等下一轮自动搜索")
        if n_sub > 1:
            res = f"🧩 已按连续段拆成 <b>{n_sub}</b> 条订阅　" + res
        tail = "\n" + _div() + f"\n✅ <b>已同意{tail}</b>\n{res}"
    else:
        await asyncio.to_thread(db.set_audit, aid, ST_FAILED, who.id, mnote)
        tail = "\n" + _div() + f"\n⚠️ <b>已同意，但推送 MoviePilot 失败</b>\n" \
                              f"　{_t(mnote, 80)}\n💡 可稍后点「🔁 重试下发」"

    await _edit(msg, _line(a) + tail, None)
    await _tell_user(a, ok, who, mnote)
    if ok:
        await _group_approved(a, who)


async def _reject(client, msg: Message, aid: int, who) -> None:
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await _edit(msg, "❌ 该申请不存在", None)
        return
    await asyncio.to_thread(db.set_audit, aid, ST_REJECT, who.id, "管理员驳回")
    await asyncio.to_thread(db.set_audit_prio, aid, 0)
    await _edit(msg, _line(a) + "\n" + _div() + "\n❌ <b>已驳回</b>"
                           "\n💡 以后想反悔，去「↩️ 已驳回」页签点「反悔同意」",
                None)
    await _tell_user(a, False, who)


async def _again(client, msg: Message, aid: int, who) -> None:
    """🔁 再次下发（重复通过）—— 已通过/已下发过的申请再推一次。

    典型场景：站点出了更高版本的资源、之前下载失败、或求片人又想要一份。
    ★ 与「反悔同意」的区别：这条**不重复扣灵石**（charge=False）。
    """
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await _edit(msg, "❌ 该申请不存在", None)
        return
    rm = str(a.get("remark") or "").strip()
    await _approve(client, msg, aid, who,
                   note=f"重复下发｜按备注找：{rm[:40]}" if rm else "重复下发",
                   charge=False)


async def _stop_sub(client, msg: Message, aid: int, who) -> None:
    """撤回已下发的求片：删 MP 订阅（QB 种子由 MP 自己管）。"""
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await _edit(msg, "❌ 该申请不存在", None)
        return
    sid = int(a.get("sub_id") or 0)
    live, live_ok = await _live_ids()
    if sid and _arc_of(a, live, live_ok):
        # ★ MP 已经自己把这条订阅删了（订阅完成）⇒ 别再去删一次，
        #   更别报「删除订阅失败，MP 可能未就绪」——那会把管理员带沟里。
        res = "该订阅已由 MoviePilot 自动移除（已完成），无需撤回"
    elif sid:
        ok = await asyncio.to_thread(mp_subscribe_del, sid)
        res = "已删除 MoviePilot 订阅" if ok else "⚠️ 删除订阅失败，MP 可能未就绪"
    else:
        res = "这条没有订阅号，无需撤回"
    await asyncio.to_thread(db.set_audit, aid, ST_APPROVED, who.id,
                            "已撤回订阅" + ("｜" + res if sid else ""))
    await _edit(msg, _line(a) + "\n" + _div() + f"\n⏹️ <b>已撤回</b>\n　{res}",
                None)
    try:
        await bot.send_message(int(a["tg"]),
                               "ℹ️ 你求的片已被管理员撤回（不再自动下载）"
                               f"\n\n🎬 {_tlink(a, 30)}\n"
                               "💡 需要的话可以重新求一次",
                               parse_mode=PM, disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"通知撤回失败 {a.get('tg')}: {e}")


async def _hard_del(client, msg: Message, aid: int, who) -> None:
    """彻底删除这条申请记录（不可恢复，慎用）"""
    a = await asyncio.to_thread(db.get_audit, aid)
    if not a:
        await _edit(msg, "❌ 该申请不存在", None)
        return
    try:
        await asyncio.to_thread(db.del_audit, aid)
    except AttributeError:
        # db 里没有 del_audit 时退化成「标记为已删除」而不是崩掉
        await asyncio.to_thread(db.set_audit, aid, "deleted", who.id, "已删除记录")
    await _edit(msg, f"🗑 已删除 #{aid}　🎬 {_tlink(a, 26)}",
                KM([[B("🏠 回到概览", "rm:home")]]))


# ═══════════════════════════════════════════ 命令
@bot.on_message(filters.command(
    ["reqmgr", "求片管理", "求片台", "reqadmin"], **CMD))
@perm.gate("reqaudit")
async def cmd_reqmgr(client, msg: Message):
    """求片管理台：按状态查看 + 改状态（驳回可反悔同意）

    `/reqmgr today` 直接看今日总求片；不带参数进概览。
    """
    parts = (msg.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("today", "今日", "今天"):
        await _reply_today(msg)
        return
    # ★ 群里打开时隐藏不可逆操作（避免群里手滑删记录）
    gm = "GROUP" in str(getattr(msg.chat, "type", "")).upper()
    text, kb = await _panel(group_mode=gm)
    await msg.reply(text, reply_markup=kb, parse_mode=PM,
                    disable_web_page_preview=True)


async def _reply_today(msg: Message) -> None:
    """今日总求片面板（带海报 + 逐条操作按钮）"""
    text, kb, poster = await _today_panel()
    sent = None
    if poster:
        try:
            bio = io.BytesIO(poster)
            bio.name = "poster.jpg"
            sent = await msg.reply_photo(bio, caption=text, reply_markup=kb,
                                         parse_mode=PM)
        except Exception as e:
            logger.debug(f"今日求片海报发送失败：{e}")
    if sent is None:
        sent = await msg.reply(text, reply_markup=kb, parse_mode=PM,
                               disable_web_page_preview=True)
    return sent


@bot.on_message(filters.command(["myreqd", "我的求片台"], **CMD))
async def cmd_myreqs_new(client, msg: Message):
    """我的求片：审核状态 + 下载进度（每人只能看自己的，带海报）

    ★ 命令名故意用不冲突的别名：`/myreq` 在 inbox.py、`/myreqs` 在
      request.py 都已注册，Pyrogram 里同名命令只有**先注册**的会生效，
      在这里重复注册等于死代码。所以新入口用 `/我的求片台`。
      inbox.cmd_myreq 里已改为转发到 _mine_panel()，效果一致。
    """
    uid = msg.from_user.id if msg.from_user else 0
    if not uid:
        await msg.reply("❓ 读不到你的账号信息")
        return
    text, _kb, poster = await _mine_panel(uid)
    if poster:
        try:
            bio = io.BytesIO(poster)
            bio.name = "poster.jpg"
            await msg.reply_photo(bio, caption=text, parse_mode=PM)
            return
        except Exception as e:
            logger.debug(f"我的求片海报发送失败：{e}")
    await msg.reply(text, parse_mode=PM, disable_web_page_preview=True)


# ═══════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^rm:"))
async def cb_reqmgr(client, cb):
    uid = cb.from_user.id if cb.from_user else 0
    if not perm.can(uid, "reqaudit"):
        await cb.answer("⛔ 只有管理员能用", show_alert=True)
        return
    parts = cb.data.split(":")
    act = parts[1]
    # ★ 面板在群里打开时同样隐藏不可逆操作（消息来自哪个聊天就跟哪个走）
    gm = "GROUP" in str(getattr(cb.message.chat, "type", "")).upper()

    if act == "home":
        await cb.answer()
        text, kb = await _panel(group_mode=gm)
        await _edit(cb.message, text, kb)
        return

    if act == "today":
        await cb.answer()
        text, kb, _p = await _today_panel()
        await _edit(cb.message, text, kb)
        return

    if act == "tab":
        st = parts[2]
        await cb.answer()
        _SEL.clear()               # 换页签清空勾选，避免跨状态误操作
        text, kb = await _panel(st, group_mode=gm)
        await _edit(cb.message, text, kb)
        return

    # ── 勾选 / 批量（rm:sel:<status>:<aid> / rm:selall:<status> /
    #    rm:selnone:<status> / rm:batch:<status>:<what>） ──
    if act in ("sel", "selall", "selnone", "batch"):
        cur = parts[2] if len(parts) > 2 else ST_PENDING
        page_ids = _SEL_PAGE.get(cur, [])
        if act == "sel":
            aid = int(parts[3])
            if aid in _SEL:
                _SEL.discard(aid)
                await cb.answer("⬜ 已取消勾选")
            else:
                _SEL.add(aid)
                await cb.answer("✅ 已勾选")
        elif act == "selall":
            _SEL.update(page_ids)
            await cb.answer(f"☑️ 已勾选本页 {len(page_ids)} 条")
        elif act == "selnone":
            _SEL.clear()
            await cb.answer("⬜ 已全部取消")
        elif act == "batch":
            what = parts[3] if len(parts) > 3 else "ok"
            ids = [i for i in _SEL]
            if not ids:
                await cb.answer("请先勾选再批量操作", show_alert=True)
                return
            await cb.answer(f"⏳ 正在处理 {len(ids)} 条…")
            ok_n = fail_n = 0
            for aid in ids:
                try:
                    if what == "ok":
                        await _approve(client, cb.message, aid, cb.from_user)
                        ok_n += 1
                    else:
                        await _reject(client, cb.message, aid, cb.from_user)
                        ok_n += 1
                except Exception as e:
                    logger.warning(f"批量 {what} #{aid} 失败: {e}")
                    fail_n += 1
            done = f"批量完成：{'通过' if what == 'ok' else '驳回'} {ok_n} 条"
            if fail_n:
                done += f"，失败 {fail_n} 条"
            _SEL.clear()
            try:
                await cb.message.reply(f"✅ <b>{done}</b>", parse_mode=PM)
            except Exception:
                pass
        text, kb = await _panel(cur, group_mode=gm)
        try:
            await cb.message.edit_text(text, parse_mode=PM, reply_markup=kb,
                                       disable_web_page_preview=True)
        except Exception:
            pass
        return

    aid = int(parts[2])
    if act == "ok":
        await cb.answer("正在下发…")
        await _approve(client, cb.message, aid, cb.from_user)
    elif act == "again":
        # ★ 重复通过：再推一次 MoviePilot（不重复扣灵石）
        await cb.answer("🔁 正在再次下发…")
        await _again(client, cb.message, aid, cb.from_user)
    elif act == "prio":
        # ★ 优先处理：置顶到待审核面板最前
        a = await asyncio.to_thread(db.get_audit, aid)
        cur = int((a or {}).get("prio") or 0)
        new = 0 if cur else 2
        await asyncio.to_thread(db.set_audit_prio, aid, new)
        await cb.answer("⭐ 已标为优先，会顶到列表最前" if new else "已取消优先")
        text, kb = await _panel((a or {}).get("status") or ST_PENDING,
                                group_mode=gm)
        try:
            await cb.message.edit_text(text, parse_mode=PM, reply_markup=kb,
                                       disable_web_page_preview=True)
        except Exception:
            pass
        return
    elif act == "no":
        await cb.answer("已驳回")
        await _reject(client, cb.message, aid, cb.from_user)
    elif act == "stop":
        await cb.answer("正在撤回…")
        await _stop_sub(client, cb.message, aid, cb.from_user)
    elif act == "del":
        #★ 删除不可恢复 ⇒ 二次确认，避免手滑把记录清掉
        a = await asyncio.to_thread(db.get_audit, aid)
        if not a:
            await cb.answer("记录已不存在", show_alert=True)
            return
        await cb.answer()
        await _edit(cb.message,
                    f"⚠️ <b>确认删除这条求片记录？</b>\n{_div()}\n"
                    + _line(a) + "\n" + "\n".join(_owner_line(a)) + "\n\n"
                    + "<b>删了就找不回来了</b>（想留着的话用「↩️ 反悔同意」"
                      "或「⏹️ 撤回订阅」）",
                    KM([[B("✅ 确认删除", f"rm:delok:{aid}"),
                         B("↩️ 取消", f"rm:tab:{a.get('status') or ST_PENDING}")]]))
    elif act == "delok":
        await cb.answer("已删除")
        await _hard_del(client, cb.message, aid, cb.from_user)
    else:
        await cb.answer("未知操作")