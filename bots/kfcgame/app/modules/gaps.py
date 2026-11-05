"""查漏补缺 / 缺集补齐（群主专属）

把 Emby 媒体库与 TMDB 逐季、逐集比对，找出缺失的季和集，私聊群主一条
**带勾选按钮**的通知；勾选后一键补下，后续 qB → pt2cloud → MT → strm → Emby
全部走原有自动化链路，无需额外处理。

用法（**只有群主**能看到/能用）：
    /gaps [N]    扫描最近 N 部剧（默认 30，上限 120），比对 TMDB 找缺失
    /gaps off    关闭当前扫描结果

设计要点：
  · Emby 给「我有什么」：/emby/Items?IncludeItemTypes=Series → 再取 Episode
  · TMDB 给「应该有什么」：走 MoviePilot 的 /api/v1/media/seasons
  · 差集就是缺的；缺的集按**连续段**拆订阅（MP 只认连续区间）
  · 只私聊群主，绝不发群、不发管理员
"""
from __future__ import annotations

import asyncio
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
from .. import settings as S
from .. import ui
from . import perm
from .request import _runs, _runs_txt, find_sub, mp_seasons, mp_sub_pause, \
    mp_sub_resume, mp_subscribe

CMD = dict(prefixes=S.PREFIX)

_PAGE = 6
_SCAN: dict = {"ts": 0.0, "items": [], "scanned": 0, "running": False}
_SEL: dict[int, set[str]] = {}
_SCAN_TTL = 1800


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _t(s, n: int = 60) -> str:
    return _esc(s)[:n]


def _owner_only(uid) -> bool:
    try:
        return perm.is_owner(uid)
    except Exception:
        return False


# ═══════════════════════════════════════════════ Emby
def _api(path: str, **params):
    try:
        params["api_key"] = S.EMBY["api_key"]
        r = requests.get(S.EMBY["url"].rstrip("/") + path, params=params, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"Emby 接口失败 {path}: {e}")
        return None


def _emby_series(limit: int) -> list[dict]:
    """最近添加的剧集（带 TMDB id）"""
    j = _api("/emby/Items", Recursive="true", IncludeItemTypes="Series",
             SortBy="DateCreated", SortOrder="Descending",
             Limit=int(limit), Fields="ProviderIds,ProductionYear")
    out = []
    for it in (j or {}).get("Items") or []:
        if not isinstance(it, dict):
            continue
        tmdb = str(((it.get("ProviderIds") or {}) or {}).get("Tmdb") or "").strip()
        if not tmdb.isdigit():
            continue
        out.append({"id": str(it.get("Id") or ""),
                    "name": str(it.get("Name") or ""),
                    "year": it.get("ProductionYear") or "",
                    "tmdb": tmdb})
    return out


def _emby_eps(series_id: str) -> set[tuple[int, int]]:
    """该剧在 Emby 里的 (季, 集) 集合"""
    out: set[tuple[int, int]] = set()
    j = _api("/emby/Items", Recursive="true", IncludeItemTypes="Episode",
             ParentId=series_id, Fields="ParentIndexNumber,IndexNumber",
             Limit=5000)
    for it in (j or {}).get("Items") or []:
        if not isinstance(it, dict):
            continue
        s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
        if s is None or e is None:
            continue
        try:
            out.add((int(s), int(e)))
        except Exception:
            continue
    return out


# ═══════════════════════════════════════════════ 扫描
def _scan_blocking(limit: int, max_items: int) -> dict:
    series = _emby_series(limit)
    logger.info(f"查漏补缺：开始扫描 {len(series)} 部剧")
    items: list[dict] = []
    done = 0
    skipped = 0
    for sv in series:
        try:
            have = _emby_eps(sv["id"])
            seasons = mp_seasons(sv["tmdb"], "themoviedb")
        except Exception as e:
            logger.debug(f"扫描 {sv['name']} 失败: {e}")
            continue
        done += 1
        for s in seasons:
            sn = int(s.get("season_number") or 0)
            total = int(s.get("episode_count") or 0)
            if sn == 0 or total <= 0:
                continue                      # 特别篇/未知集数跳过
            have_eps = {e for (ss, e) in have if ss == sn}
            if not have_eps:
                lack = list(range(1, total + 1))
                whole = True
            else:
                # ★ 集号体系防误报：Emby 有时存的是**绝对集号**（如 S2 里是 E14~E26），
                #   而 TMDB 给的是**季内集号**（E1~E13）。这种「数量够但编号对不上」
                #   会被算成整季缺失，纯属误报 ⇒ 直接跳过，不报缺。
                if not any(e in have_eps for e in range(1, total + 1)) \
                        and len(have_eps) >= max(1, total // 2):
                    skipped += 1
                    logger.debug(f"跳过（集号体系不一致）：{sv['name']} S{sn} "
                                 f"Emby={sorted(have_eps)[:3]}… TMDB=1~{total}")
                    continue
                lack = [e for e in range(1, total + 1) if e not in have_eps]
                whole = False
            if not lack:
                continue
            items.append({
                "key": f"{sv['tmdb']}:{sn}",
                "name": sv["name"], "year": sv["year"], "tmdb": sv["tmdb"],
                "season": sn, "eps": lack, "whole": whole,
                "total": total, "have": len(have_eps),
            })
        if len(items) >= max_items:
            break
    items.sort(key=lambda x: (-len(x["eps"]), x["name"]))
    logger.info(f"查漏补缺：扫完 {done} 部，发现 {len(items)} 条缺集"
                f"（跳过集号不一致 {skipped} 条）")
    return {"scanned": done, "total_series": len(series), "skipped": skipped,
            "items": items[:max_items]}


# ═══════════════════════════════════════════════ 渲染
def _sel_of(uid) -> set[str]:
    return _SEL.setdefault(int(uid or 0), set())


def _line(it: dict) -> str:
    tl = ui.title_link(it["name"], it["tmdb"], "电视剧", 22)
    y = f"（{_t(it['year'], 6)}）" if it.get("year") else ""
    rng = _runs_txt(it["eps"])
    tag = "整季缺失" if it["whole"] else f"缺 {len(it['eps'])} 集"
    return (f"S{it['season']} · {tag}\n"
            f"　　🎬 <b>{tl}</b>{y}\n"
            f"　　📄 缺：<b>{_t(rng, 60)}</b>（应有 {it['total']} 集，现有 {it['have']}）")


def _text(uid: int, page: int = 0) -> tuple[str, int]:
    items = _SCAN["items"]
    pages = max(1, (len(items) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    sel = _sel_of(uid)
    n_sel = sum(1 for it in items if it["key"] in sel)
    lines = [
        "🩺 <b>查漏补缺 · 缺集补齐</b>", _div(),
        f"已扫 <b>{_SCAN['scanned']}</b> 部剧　发现 <b>{len(items)}</b> 条缺集"
        + (f"　（跳过集号不一致 {_SCAN.get('skipped', 0)} 条）"
           if _SCAN.get("skipped") else ""),
        f"{ui.ON} 已勾选 <b>{n_sel}</b> / {len(items)} 条",
        _div(),
    ]
    if not items:
        lines += ["🎉 没发现缺集（或还没扫描）", _div(),
                  "💡 发 <code>/gaps 30</code> 扫描最近 30 部剧"]
        return ui.clip_html("\n".join(lines)), 0
    for k, it in enumerate(items[page * _PAGE:(page + 1) * _PAGE], 1):
        idx = page * _PAGE + k
        mark = ui.mark(it["key"] in sel)
        lines.append(f"{mark} <b>{idx}.</b> {_line(it)}")
        lines.append(_div())
    if pages > 1:
        lines.append(f"📄 第 <b>{page + 1}</b> / {pages} 页")
    lines.append("💡 点条目 = 勾选；勾完点「⬇️ 补下勾选」")
    lines.append("　 补下后 qB 下载 → 上云 115 → MT 整理 → Emby 全自动")
    return ui.clip_html("\n".join(lines)), page


def _kb(uid: int, page: int = 0) -> KM:
    items = _SCAN["items"]
    pages = max(1, (len(items) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    sel = _sel_of(uid)
    rows: list[list[B]] = []
    row: list[B] = []
    for k, it in enumerate(items[page * _PAGE:(page + 1) * _PAGE], 1):
        idx = page * _PAGE + k
        mark = ui.mark(it["key"] in sel)
        nm = f"{it['name'][:6]} S{it['season']}"
        row.append(B(f"{mark}{idx}. {nm}", f"gap:t:{idx}:{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("⬅️ 上一页", f"gap:p:{page - 1}"))
        nav.append(B(f"📄 {page + 1}/{pages}", "gap:noop"))
        if page < pages - 1:
            nav.append(B("下一页 ➡️", f"gap:p:{page + 1}"))
        rows.append(nav)
    rows.append([B(f"{ui.ON} 补下勾选", f"gap:go:{page}"),
                 B(f"{ui.ON} 全选", "gap:all"),
                 B(f"{ui.OFF} 全不选", "gap:none")])
    rows.append([B("⏸ 暂停勾选", "gap:pause"),
                 B("▶️ 恢复勾选", "gap:resume")])
    rows.append([B("🔍 重新扫描", "gap:scan"), B("❌ 关闭", "close")])
    return KM(rows)


async def _render(msg: Message, uid: int, page: int = 0) -> None:
    text, page = _text(uid, page)
    kb = _kb(uid, page)
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=PM,
                            disable_web_page_preview=True)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            return
        try:
            await msg.reply(text, reply_markup=kb, parse_mode=PM,
                            disable_web_page_preview=True)
        except Exception as e2:
            logger.warning(f"查漏补缺渲染失败: {e2}")


# ═══════════════════════════════════════════════ 命令
@bot.on_message(filters.command(["gaps", "查漏补缺", "缺集补齐", "buqi"], **CMD))
async def cmd_gaps(client, msg: Message):
    """查漏补缺（仅群主）"""
    uid = msg.from_user.id if msg.from_user else 0
    if not _owner_only(uid):
        return                                   # 静默：别人不该知道有这个命令
    parts = (msg.text or "").split()
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if arg in ("off", "close", "关闭"):
        _SCAN.update(items=[], ts=0.0, scanned=0)
        await msg.reply("✅ 已清空查漏补缺结果", parse_mode=PM)
        return

    limit = 30
    if arg.isdigit():
        limit = max(1, min(int(arg), 120))

    if _SCAN["running"]:
        await msg.reply("⏳ 上一次扫描还在跑，请稍等…", parse_mode=PM)
        return

    _SCAN["running"] = True
    hint = await msg.reply(
        f"🩺 <b>正在查漏补缺</b>\n{_div()}\n"
        f"扫描最近 <b>{limit}</b> 部剧，逐季逐集比对 TMDB…\n"
        f"⌛ 剧集多时需要几分钟，完成后会私聊给你",
        parse_mode=PM)
    try:
        res = await asyncio.to_thread(_scan_blocking, limit, 60)
    except Exception as e:
        _SCAN["running"] = False
        logger.warning(f"查漏补缺扫描失败: {e}")
        await hint.edit_text(f"⚠️ 扫描失败：{_t(e, 80)}", parse_mode=PM)
        return
    _SCAN["running"] = False
    _SCAN.update(items=res["items"], scanned=res["scanned"], ts=time.time(),
                 skipped=res.get("skipped", 0))
    _sel_of(uid).clear()
    try:
        await hint.delete()
    except Exception:
        pass

    # ★ 只私聊群主（即使命令是在群里发的，结果也只发给群主）
    target = int(S.OWNER or 0)
    if not target:
        await msg.reply("⚠️ 未配置群主 ID", parse_mode=PM)
        return
    text, page = _text(uid, 0)
    kb = _kb(uid, page)
    try:
        await bot.send_message(target, text, reply_markup=kb, parse_mode=PM,
                               disable_web_page_preview=True)
        if msg.chat.id != target:
            await msg.reply(f"✅ 扫描完成：发现 <b>{len(res['items'])}</b> 条缺集，"
                            f"已私聊发给你", parse_mode=PM)
    except Exception as e:
        logger.warning(f"查漏补缺私聊失败: {e}")
        await msg.reply(f"⚠️ 私聊发送失败（请先私聊我一次）：{_t(e, 60)}",
                        parse_mode=PM)


# ═══════════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^gap:"))
async def cb_gaps(client, q):
    uid = q.from_user.id if q.from_user else 0
    if not _owner_only(uid):
        await q.answer("⛔ 只有群主能用", show_alert=True)
        return
    parts = (q.data or "").split(":")
    act = parts[1] if len(parts) > 1 else ""

    if act == "noop":
        await q.answer()
        return

    if act == "scan":
        await q.answer("请发 /gaps 30 重新扫描")
        return

    items = _SCAN["items"]

    if act == "p" and len(parts) > 2:
        await q.answer()
        await _render(q.message, uid, int(parts[2]))
        return

    if act == "t" and len(parts) > 2:
        idx, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        if not 1 <= idx <= len(items):
            await q.answer("该条目已不存在，请重新扫描", show_alert=True)
            return
        key = items[idx - 1]["key"]
        sel = _sel_of(uid)
        if key in sel:
            sel.discard(key)
            tip = f"{ui.OFF} 已取消勾选"
        else:
            sel.add(key)
            tip = f"{ui.ON} 已勾选（共 {len(sel)} 条）"
        await q.answer(tip)
        await _render(q.message, uid, page)
        return

    if act == "all":
        sel = _sel_of(uid)
        for it in items:
            sel.add(it["key"])
        await q.answer(f"{ui.ON} 已全选 {len(sel)} 条")
        await _render(q.message, uid, 0)
        return

    if act == "none":
        _sel_of(uid).clear()
        await q.answer(f"{ui.OFF} 已清空")
        await _render(q.message, uid, 0)
        return

    if act in ("pause", "resume"):
        sel = _sel_of(uid)
        targets = [it for it in items if it["key"] in sel]
        if not targets:
            await q.answer("先点条目勾选要操作的季", show_alert=True)
            return
        verb = "暂停" if act == "pause" else "恢复"
        await q.answer(f"正在{verb} {len(targets)} 条…")
        ok_n, none_n, fail = 0, 0, []
        for it in targets:
            try:
                sub = await asyncio.to_thread(find_sub, it["name"], it["season"])
                if not sub:
                    none_n += 1
                    continue
                fn = mp_sub_pause if act == "pause" else mp_sub_resume
                good, note = await asyncio.to_thread(fn, int(sub.get("id") or 0))
                if good:
                    ok_n += 1
                else:
                    fail.append(f"{it['name']}（{_t(note, 18)}）")
            except Exception as e:
                fail.append(f"{it['name']}（{_t(e, 18)}）")
            await asyncio.sleep(0.4)
        logger.info(f"群主 {uid} {verb}订阅 {ok_n}/{len(targets)} 条")
        tip = f"{'⏸' if act == 'pause' else '▶️'} 已{verb} {ok_n}/{len(targets)} 条"
        if none_n:
            tip += f"\n💡 {none_n} 条没有对应的 MP 订阅（还没补下过）"
        if fail:
            tip += "\n⚠️ " + "、".join(fail[:3])
        await q.answer(tip, show_alert=True)
        await _render(q.message, uid, 0)
        return

    if act == "go":
        page = int(parts[2]) if len(parts) > 2 else 0
        sel = _sel_of(uid)
        targets = [it for it in items if it["key"] in sel]
        if not targets:
            await q.answer("先点条目勾选要补的集", show_alert=True)
            return
        await q.answer(f"正在补下 {len(targets)} 条…")
        ok_n, fail = 0, []
        for it in targets:
            try:
                item_d = {"title": it["name"], "year": str(it["year"] or ""),
                          "type": "电视剧", "media_source": "themoviedb",
                          "media_id": it["tmdb"]}
                good = True
                note = ""
                for (a, b) in _runs(sorted(it["eps"])):
                    o, nt, _sid = await asyncio.to_thread(
                        mp_subscribe, item_d, it["season"], a, b)
                    good = good and o
                    if nt:
                        note = nt
                if good:
                    ok_n += 1
                else:
                    fail.append(f"{it['name']} S{it['season']}（{_t(note, 20)}）")
            except Exception as e:
                fail.append(f"{it['name']} S{it['season']}（{_t(e, 20)}）")
            await asyncio.sleep(0.6)
        logger.info(f"群主 {uid} 补下缺集 {ok_n}/{len(targets)} 条")
        # 补下成功的从列表移除
        for it in targets:
            if it["key"] in sel and not any(it["name"] in f for f in fail):
                sel.discard(it["key"])
                items.remove(it)
        await _render(q.message, uid, page)
        msg = f"✅ 已发起补下 {ok_n}/{len(targets)} 条"
        if fail:
            msg += "\n⚠️ 失败：" + "、".join(fail[:3])
        await q.answer(msg, show_alert=True)
        return

    await q.answer()
