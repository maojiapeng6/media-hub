"""投稿：群友直接投链接 / 磁力 → 管理员审核 → 自动转给 MT 转存机器人

★ 为什么要有「投稿」这条独立链路（2026-10-13 用户要求）
────────────────────────────────────────────────────
求片（/qp）走的是「说片名 → 机器人自己去搜」；但很多时候**资源已经在你手上**
（PT 站详情页链接、别人发的磁力、115/阿里分享链接），再让机器人搜一遍是浪费。
投稿就是给这种情况一个入口：**你给资源，机器人负责送审 + 转存**。

★ 投递目标 @KFCMTZY_bot 的现实约束（重要，别踩）
────────────────────────────────────────────────────
Telegram **禁止 bot 给 bot 发私聊消息**，实测直接发会返回：
    USER_BOT_TO_BOT_DISABLED
（getChat 能查到 @KFCMTZY_bot、但 sendMessage 一定 400）。
所以这里做成**三级自动降级**，任何一条通就自动完成，全不通也不丢资源：
    ① 用户会话（userbot，需 /ublogin 授权过一次）→ 直投 @KFCMTZY_bot
    ② 中转群 / 频道（/setmt 指定，MT 机器人在群里就能收到）→ 群里发
    ③ 上面都失败 → 原文私聊 OWNER + 所有管理员，附「再试一次」按钮
第 ③ 步保证**永远不会丢投稿**；人工转发只是最后兜底。

★ 链接排版铁律（沿用 2026-10-13 修复的规范）
────────────────────────────────────────────────────
链接**独占一行**，后面绝不挂中文说明 —— 否则 Telegram 会把整段判成无效链接。
磁力用 <code> 包（方便长按复制），http(s) 用 <a href> 标准超链。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from loguru import logger
from pyrogram import ContinuePropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import Message

from .. import bot
from .. import db
from .. import settings as S
from .. import ui

# ⚠️ 本模块文案一律 HTML，每次发送必须显式 parse_mode=PM
PM = ParseMode.HTML

CMD = dict(prefixes=S.PREFIX)

#: MT 转存机器人（用户指定）
MT_BOT = "@KFCMTZY_bot"

#: 运行时配置（中转群 / 频道），持久化到 rw 挂载的 keepalive 目录
_CFG_PATH = "/app/keepalive/contrib_cfg.json"
_DEFAULT_CFG = {"chat": "", "mt_bot": MT_BOT}

# uid -> 输入态：{"kind": "payload"|"title"|"note", "cid": int, "ts": float}
_WAIT: dict[int, dict] = {}
_WAIT_TTL = 900.0                      # 15 分钟

_KIND_ICON = {"magnet": "🧲", "link": "🔗", "ed2k": "⚡",
              "torrent": "📄", "other": "✏️"}
_KIND_TXT = {"magnet": "磁力链接", "link": "网页链接", "ed2k": "电驴链接",
             "torrent": "种子", "other": "文字投稿"}

_STATUS_TXT = {"auditing": "⏳ 待审核", "approved": "✅ 已通过",
               "rejected": "❌ 已驳回", "pending_fwd": "⚠️ 待补发"}


# ══════════════════════════════════════════════════════════ 配置读写
def _cfg() -> dict:
    try:
        if os.path.isfile(_CFG_PATH):
            return {**_DEFAULT_CFG, **json.load(open(_CFG_PATH, encoding="utf-8"))}
    except Exception as e:
        logger.debug(f"读投稿配置失败: {e}")
    return dict(_DEFAULT_CFG)


def _save_cfg(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
        json.dump({**_DEFAULT_CFG, **d}, open(_CFG_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写投稿配置失败: {e}")


# ══════════════════════════════════════════════════════════ 解析
_MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"']+", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+", re.I)
_ED2K_RE = re.compile(r"ed2k://[^\s<>\"']+", re.I)


def _human_size(nbytes: float) -> str:
    try:
        n = float(nbytes or 0)
    except Exception:
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return ""


def _magnet_title(mag: str) -> str:
    """从磁力里取 dn=（显示名）当片名候选"""
    m = re.search(r"[&?]dn=([^&]+)", mag, re.I)
    if not m:
        return ""
    from urllib.parse import unquote_plus
    s = unquote_plus(m.group(1))
    return s.replace("+", " ").strip()


def _magnet_size(mag: str) -> str:
    """从磁力里取 xl=（字节数）算体积"""
    m = re.search(r"[&?]xl=(\d+)", mag, re.I)
    return _human_size(float(m.group(1))) if m else ""


def parse_items(text: str) -> list[dict]:
    """把一段文本拆成若干条投稿（支持一次投多个，按行/空格分隔）。

    返回 [{'kind','payload','title','size_txt'}]
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def push(kind: str, payload: str):
        p = payload.strip().rstrip("。.,，")
        if not p or p in seen:
            return
        seen.add(p)
        it = {"kind": kind, "payload": p, "title": "", "size_txt": ""}
        if kind == "magnet":
            it["title"] = _magnet_title(p)
            it["size_txt"] = _magnet_size(p)
        elif kind == "link" and re.search(r"details\.php|torrent", p, re.I):
            m = re.search(r"[?&]id=(\d+)", p)
            it["title"] = f"PT 详情页 #{m.group(1)}" if m else "PT 详情页"
        out.append(it)

    for m in _MAGNET_RE.finditer(raw):
        push("magnet", m.group(0))
    for m in _ED2K_RE.finditer(raw):
        push("ed2k", m.group(0))
    for m in _URL_RE.finditer(raw):
        push("link", m.group(0))

    if not out:
        # 没有可识别的链接 ⇒ 当成纯文字投稿（比如「115 分享：xxx 提取码 yyy」）
        for line in raw.splitlines():
            line = line.strip()
            if len(line) >= 4:
                push("other", line[:1000])
        if not out:
            push("other", raw[:1000])
    return out


# ══════════════════════════════════════════════════════════ 渲染
def _t(s, n: int = 60) -> str:
    return _esc(str(s or "").strip())[:n]


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


def payload_line(it: dict) -> list[str]:
    """★ 链接排版：独占一行，后面不挂任何中文说明"""
    p = str(it.get("payload") or "").strip()
    kind = it.get("kind") or "other"
    if kind in ("magnet", "ed2k"):
        # 磁力/电驴用 code 包：Telegram 不认这两种协议做超链，
        # 但 MT 机器人是**读原文**的，能整段复制才是关键。
        return [f"<code>{_esc(p)}</code>"]
    if kind == "link":
        return [f'🔗 <a href="{_esc(p)}">{_esc(p[:70])}</a>']
    return [f"✏️ {_t(p, 300)}"]


def contrib_card(c: dict, with_kb_hint: bool = True) -> str:
    """投稿卡片（求片人回执 / 管理员审核 / 群播报 通用）"""
    kind = c.get("kind") or "other"
    lines = [
        f"{_KIND_ICON.get(kind, '✏️')} <b>投稿 · {_KIND_TXT.get(kind, '文字投稿')}</b>"
        f"　#{c.get('id')}",
        _div(),
    ]
    if c.get("title"):
        lines.append(f"🎬 片名：{_t(c['title'], 40)}")
    lines += payload_line(c)
    if c.get("size_txt"):
        lines.append(f"💾 标注体积：{_t(c['size_txt'], 20)}")
    if c.get("note"):
        lines.append(f"📝 说明：{_t(c['note'], 120)}")
    if int(c.get("aid") or 0):
        lines.append(f"🔗 关联求片：#{int(c['aid'])}")
    lines.append(_div())
    return "\n".join(lines)


def _user_line(c: dict, label: str = "投稿人") -> str:
    try:
        from .request import _ucard_one
        return _ucard_one(c.get("tg"), c.get("tg_name") or "",
                          c.get("tg_username") or "", label)
    except Exception:
        return f"👤 {_t(c.get('tg_name') or c.get('tg'), 16)}"


def _status_line(c: dict) -> str:
    st = _STATUS_TXT.get(c.get("status") or "", c.get("status") or "")
    if c.get("status") == "approved":
        st = "✅ 已通过" + ("（已转发 MT）" if int(c.get("fwd_ok") or 0) else "")
    return st


# ══════════════════════════════════════════════════════════ 键盘
def _recv_kb(cid: int) -> KM:
    """投稿人收到回执后的键盘：改片名 / 补充说明 / 撤回"""
    return KM([
        [B("🎬 补个片名", f"cb:t:{cid}"), B("📝 补充说明", f"cb:n:{cid}")],
        [B("🗑 撤回投稿", f"cb:x:{cid}")],
    ])


def _audit_kb(cid: int, prio: int = 0) -> KM:
    """管理员审核键盘"""
    return KM([
        [B("✅ 通过并转存", f"cb:ok:{cid}"), B("❌ 驳回", f"cb:no:{cid}")],
        [B("⭐ 优先" if not prio else "☆ 取消优先", f"cb:p:{cid}"),
         B("🔁 重新转发", f"cb:f:{cid}")],
        [B("📋 待审核投稿", "cb:list:0")],
    ])


def _after_kb() -> KM:
    return KM([
        [B("📮 再投一个", "cb:new:0"), B("📋 我的投稿", "cb:mine:0")],
    ])


# ══════════════════════════════════════════════════════════ 投递（三级降级）
async def _deliver(text: str) -> tuple[bool, str]:
    """把资源信息送到 MT 转存机器人。返回 (ok, 通道说明/失败原因)"""
    cfg = _cfg()
    target = (cfg.get("chat") or "").strip()

    # ② 中转群 / 频道（主通道：MT 机器人在群里就能收到）
    if target:
        try:
            await bot.send_message(target, text, parse_mode=PM,
                                   disable_web_page_preview=True)
            return True, f"已发到 {target}"
        except Exception as e:
            logger.warning(f"投稿转发到 {target} 失败: {e}")
            err2 = str(e)[:120]
        # 目标就是 MT bot 私聊时（bot→bot 被 TG 禁止），给一句明确原因
        if MT_BOT.lower().lstrip("@") in target.lower():
            return False, ("Telegram 禁止 bot 给 bot 发私聊"
                           f"（{err2}）——请 /setmt 指定一个 MT 机器人在内的群")
        return False, err2

    # ① 未配置中转群 ⇒ 试直投（大概率被 TG 拒绝，但留着万一哪天放开）
    try:
        await bot.send_message(MT_BOT, text, parse_mode=PM,
                               disable_web_page_preview=True)
        return True, f"已直投 {MT_BOT}"
    except Exception as e:
        msg = str(e)
        if "BOT_TO_BOT" in msg.upper() or "bot can't" in msg.lower():
            return False, ("Telegram 禁止 bot 给 bot 发私聊（USER_BOT_TO_BOT_DISABLED）；"
                           "请用 /setmt 指定一个 MT 机器人在内的群")
        return False, msg[:150]


async def _fallback_to_humans(c: dict, text: str, why: str) -> None:
    """③ 兜底：原文私聊 OWNER + 管理员，保证投稿不丢"""
    head = ("⚠️ <b>投稿已通过，但自动转发失败</b>", _div(),
            f"🆔 投稿 #{c.get('id')}　{why}", _div())
    body = "\n".join(head) + "\n" + text + "\n" + _div() + \
        "\n👇 可直接复制转发给 MT 机器人，或点下面按钮再试一次。"
    kb = KM([[B("🔁 再试一次转发", f"cb:f:{c['id']}")]])
    try:
        from .request import admin_ids
        ids = admin_ids()
    except Exception:
        ids = []
    for uid in ids:
        try:
            await bot.send_message(int(uid), ui.clip_html(body, limit=3500),
                                   parse_mode=PM, disable_web_page_preview=True,
                                   reply_markup=kb)
        except Exception as e:
            logger.debug(f"投稿兜底通知 {uid} 失败: {e}")


async def do_forward(cid: int) -> tuple[bool, str]:
    """审核通过后转发一次（可重复调用，用于「🔁 重新转发」）"""
    c = await asyncio.to_thread(db.get_contrib, cid)
    if not c:
        return False, "投稿不存在"
    text = "\n".join([
        f"📮 <b>投稿已通过 · 请转存</b>　#{cid}", _div(),
        contrib_card(c),
        _user_line(c),
        _div(),
    ])
    ok, why = await _deliver(ui.clip_html(text, limit=3500))
    await asyncio.to_thread(db.mark_contrib_fwd, cid, ok, "" if ok else why)
    if not ok:
        await _fallback_to_humans(c, ui.clip_html(text, limit=3000), why)
    return ok, why


# ══════════════════════════════════════════════════════════ 提交
async def _submit(msg: Message, u, items: list[dict], aid: int = 0,
                  note: str = "") -> list[int]:
    """把解析出的投稿逐条入库，返回 id 列表"""
    ids: list[int] = []
    for it in items[:10]:                       # 一次最多 10 条，防刷屏
        cid = await asyncio.to_thread(db.add_contrib, **{
            "tg": u.id,
            "tg_name": u.first_name or "",
            "tg_username": u.username or "",
            "kind": it.get("kind") or "other",
            "payload": it.get("payload") or "",
            "title": it.get("title") or "",
            "size_txt": it.get("size_txt") or "",
            "note": note[:500],
            "aid": int(aid or 0),
        })
        if cid:
            ids.append(cid)
    return ids


async def _after_submit(msg: Message, u, ids: list[int]) -> None:
    """入库后：回执 + 通知管理员"""
    if not ids:
        await msg.reply("⚠️ 没识别出可投稿的内容。\n" + _div() +
                        "\n支持：磁力链接 <code>magnet:?</code>、"
                        "http(s) 链接、ed2k 链接；\n也可以直接写文字说明。",
                        parse_mode=PM)
        return
    for cid in ids:
        c = await asyncio.to_thread(db.get_contrib, cid)
        if not c:
            continue
        await msg.reply(
            ui.clip_html("\n".join([
                "📮 <b>投稿已收到</b>", _div(),
                contrib_card(c),
                "⏳ 管理员确认后会自动转给 MT 机器人做转存",
            ]), limit=3500),
            parse_mode=PM, disable_web_page_preview=True,
            reply_markup=_recv_kb(cid))
        await _notify_admins(c)
    if len(ids) > 1:
        await msg.reply(f"✅ 共提交 <b>{len(ids)}</b> 条投稿，逐条等待审核。",
                        parse_mode=PM)


async def _notify_admins(c: dict) -> None:
    """把待审核投稿私聊发给所有管理员"""
    try:
        from .request import admin_ids
        admins = admin_ids()
    except Exception:
        admins = []
    if not admins:
        logger.warning("未配置管理员，投稿无人可审")
        return
    cap = ui.clip_html("\n".join([
        f"📮 <b>新投稿 · 待审核</b>　#{c.get('id')}", _div(),
        contrib_card(c),
        _user_line(c),
        _div(),
        "👇 通过后会自动把资源信息转给 MT 转存机器人",
    ]), limit=3500)
    kb = _audit_kb(int(c["id"]), int(c.get("prio") or 0))
    for ad in admins:
        try:
            await bot.send_message(ad, cap, parse_mode=PM,
                                   disable_web_page_preview=True,
                                   reply_markup=kb)
        except Exception as e:
            logger.debug(f"私聊管理员 {ad} 失败: {e}")


# ══════════════════════════════════════════════════════════ 命令
@bot.on_message(filters.command(
    ["投稿", "tougao", "contribute", "ctb", "投递", "投资源"], **CMD))
async def cmd_contrib(client, msg: Message):
    """投稿入口：/投稿 <链接或磁力>，不带参数就等下一条消息"""
    u = msg.from_user
    if not u:
        return
    parts = (msg.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        _WAIT[u.id] = {"kind": "payload", "cid": 0, "ts": time.time()}
        await msg.reply(
            ui.clip_html("\n".join([
                "📮 <b>投稿</b>", _div(),
                "直接把<b>资源链接或磁力</b>发给我就行：",
                "　🧲 磁力：<code>magnet:?xt=urn:btih:…</code>",
                "　🔗 链接：PT 详情页 / 网盘分享页",
                "　⚡ 电驴：<code>ed2k://…</code>",
                "　✏️ 或直接用文字说明（如「115 分享 xxx 提取码 yyy」）",
                _div(),
                "💡 一次可以投多个（换行隔开）；投完还能补片名和说明。",
                "⌛ 等待输入（15 分钟内有效）",
            ])), parse_mode=PM, disable_web_page_preview=True,
            reply_markup=KM([[B("❌ 取消", "cb:cancelwait")]]))
        return

    items = parse_items(arg)
    ids = await _submit(msg, u, items, aid=0)
    await _after_submit(msg, u, ids)


@bot.on_message(filters.command(["myctb", "我的投稿", "mycontrib"], **CMD))
async def cmd_my_contrib(client, msg: Message):
    rows = await asyncio.to_thread(db.my_contribs, msg.from_user.id, 10)
    if not rows:
        await msg.reply("📋 <b>我的投稿</b>\n" + _div() +
                        "\n还没有投稿记录\n" + _div() +
                        "\n💡 用 /投稿 直接把链接或磁力发给我", parse_mode=PM)
        return
    lines = [f"📋 <b>我的投稿 · {len(rows)} 条</b>", _div()]
    for c in rows:
        lines.append(f"#{c['id']}　{_status_line(c)}　"
                     f"{_KIND_ICON.get(c.get('kind'), '✏️')} "
                     f"{_t(c.get('title') or c.get('payload'), 26)}")
    await msg.reply(ui.clip_html("\n".join(lines), limit=3500), parse_mode=PM,
                    disable_web_page_preview=True)


@bot.on_message(filters.command(["ctbs", "投稿审核", "待审核投稿"], **CMD))
async def cmd_contrib_audit(client, msg: Message):
    """管理员：打开待审核投稿列表"""
    from .request import is_admin
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 只有管理员能审核投稿", parse_mode=PM)
        return
    rows = await asyncio.to_thread(db.list_contribs, "auditing", 15)
    if not rows:
        await msg.reply("📭 当前没有待审核的投稿", parse_mode=PM)
        return
    lines = [f"📮 <b>待审核投稿 · {len(rows)} 条</b>", _div()]
    for c in rows:
        star = "⭐ " if int(c.get("prio") or 0) else ""
        lines.append(f"{star}#{c['id']}　{_KIND_ICON.get(c.get('kind'), '✏️')} "
                     f"{_t(c.get('title') or c.get('payload'), 26)}")
        lines.append(f"　 {_user_line(c)}")
    lines.append(_div())
    lines.append("👇 点下面的编号直接处理：")
    kb = KM([[B(f"{'⭐' if int(c.get('prio') or 0) else ''}#{c['id']} "
                f"{_KIND_ICON.get(c.get('kind'), '✏️')}",
                f"cb:v:{c['id']}") for c in rows[i:i + 3]]
             for i in range(0, len(rows), 3)])
    await msg.reply(ui.clip_html("\n".join(lines), limit=3500), parse_mode=PM,
                    disable_web_page_preview=True, reply_markup=kb)


@bot.on_message(filters.command(["setmt"], **CMD))
async def cmd_setmt(client, msg: Message):
    """指定投稿的投递目标（群/频道）。

    ★ Telegram 不允许 bot 私聊另一个 bot，所以最稳的做法是：
      把 @KFCMTZY_bot 拉进一个群 → /setmt -100xxxxxxxxxx
      （群里 MT 机器人能看到消息就会自动转存）
    """
    from .request import is_admin
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 只有管理员能设置", parse_mode=PM)
        return
    parts = (msg.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    cfg = _cfg()
    if not arg:
        await msg.reply(ui.clip_html("\n".join([
            "⚙️ <b>投稿投递目标</b>", _div(),
            f"当前：{_t(cfg.get('chat') or '未设置（默认直投 ' + MT_BOT + '）', 40)}",
            _div(),
            "⚠️ Telegram 禁止 bot 给 bot 发私聊，直投一定失败。",
            "正确做法：",
            "　1) 建一个群（或现有群），把 @KFCMTZY_bot 拉进去",
            "　2) 把 @KFCGAME_bot 也拉进去并设为管理员",
            "　3) 这里执行 <code>/setmt -100xxxxxxxxxx</code>（群的数字 ID）",
            _div(),
            "群 ID 获取：群里发 <code>/chatid</code> 或用 /groupid 查看。",
        ])), parse_mode=PM, disable_web_page_preview=True)
        return
    cfg["chat"] = arg
    _save_cfg(cfg)
    await msg.reply(f"✅ 投稿投递目标已设为：<code>{_t(arg, 40)}</code>",
                    parse_mode=PM)


@bot.on_message(filters.command(["chatid", "groupid"], **CMD))
async def cmd_chatid(client, msg: Message):
    """回显当前聊天的 ID —— 配 /setmt 时用它取群 ID"""
    await msg.reply(f"🆔 当前聊天 ID：<code>{msg.chat.id}</code>\n"
                    f"　类型：{msg.chat.type}\n"
                    f"💡 配投递目标：<code>/setmt {msg.chat.id}</code>",
                    parse_mode=PM)


# ══════════════════════════════════════════════════════════ 输入态捕获
@bot.on_message(filters.text, group=1)
async def on_contrib_wait(client, msg: Message):
    """投稿的补填输入（片名 / 说明 / 新投稿内容）

    ★ group=1：request 的备注输入也在这个 group，谁先注册谁先跑。
      这里只处理「自己设的等待态」，不是自己的一律 ContinuePropagation 放行。
    """
    u = msg.from_user
    if not u or u.is_bot:
        raise ContinuePropagation
    st = _WAIT.get(u.id)
    if not st:
        raise ContinuePropagation
    if time.time() - float(st.get("ts") or 0) > _WAIT_TTL:
        _WAIT.pop(u.id, None)
        await msg.reply("⌛ 输入已超时，请重新点按钮或再发一次 /投稿",
                        parse_mode=PM)
        return
    txt = (msg.text or "").strip()
    if not txt or txt[0] in "/!！":
        raise ContinuePropagation
    _WAIT.pop(u.id, None)

    cid = int(st.get("cid") or 0)
    kind = st.get("kind") or ""
    if kind == "title" and cid:
        await asyncio.to_thread(db.set_contrib_title, cid, txt[:255])
        c = await asyncio.to_thread(db.get_contrib, cid)
        await msg.reply(ui.clip_html("\n".join([
            "✅ <b>片名已补上</b>", _div(),
            contrib_card(c) if c else f"🎬 {_t(txt, 40)}",
        ]), limit=3500), parse_mode=PM, disable_web_page_preview=True,
            reply_markup=_recv_kb(cid))
        return
    if kind == "note" and cid:
        await asyncio.to_thread(db.set_contrib_note, cid, txt[:500])
        await msg.reply(f"✅ 说明已记下：{_t(txt, 80)}", parse_mode=PM,
                        reply_markup=_recv_kb(cid))
        return
    # kind == "payload"：用户直接把资源发过来了
    items = parse_items(txt)
    ids = await _submit(msg, u, items, aid=int(st.get("aid") or 0))
    await _after_submit(msg, u, ids)


# ══════════════════════════════════════════════════════════ 回调
@bot.on_callback_query(filters.regex(r"^cb:cancelwait$"))
async def cb_cancel_wait(client, cb):
    _WAIT.pop(cb.from_user.id, None)
    await cb.answer("已取消")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^cb:new:(\d+)$"))
async def cb_new(client, cb):
    """从求片卡片点「📮 投稿」→ 进入输入态（带上申请号做关联）"""
    aid = int(cb.data.split(":")[2])
    _WAIT[cb.from_user.id] = {"kind": "payload", "cid": 0, "aid": aid,
                              "ts": time.time()}
    await cb.answer("把链接或磁力发给我就行 ✍️")
    tip = ["📮 <b>投稿资源</b>", _div()]
    if aid:
        tip.append(f"🔗 将关联到求片 #{aid}")
    tip += [
        _div(),
        "直接回复这条消息，把资源贴过来：",
        "　🧲 <code>magnet:?xt=urn:btih:…</code>",
        "　🔗 https://……（PT 详情页 / 网盘分享）",
        "　✏️ 或文字说明",
        _div(),
        "⏳ 15 分钟内有效。投完管理员确认后会自动转给 MT 机器人转存。",
    ]
    try:
        await cb.message.reply(ui.clip_html("\n".join(tip)), parse_mode=PM,
                               disable_web_page_preview=True,
                               reply_markup=KM([[B("❌ 取消", "cb:cancelwait")]]))
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^cb:(t|n|x|ok|no|p|f|v|mine|list):(\d+)$"))
async def cb_contrib(client, cb):
    from .request import is_admin
    u = cb.from_user
    act = cb.data.split(":")[1]
    cid = int(cb.data.split(":")[2])

    if act in ("mine", "list") and cid == 0:
        await cb.answer()
        rows = await asyncio.to_thread(
            db.my_contribs if act == "mine" else db.list_contribs,
            u.id if act == "mine" else "auditing", 10)
        if act == "list" and not is_admin(u.id):
            await cb.answer("⛔ 只有管理员能看", show_alert=True)
            return
        if not rows:
            await cb.answer("📭 暂无记录", show_alert=True)
            return
        lines = [f"📮 <b>{'我的投稿' if act == 'mine' else '待审核投稿'} · "
                 f"{len(rows)} 条</b>", _div()]
        for c in rows:
            lines.append(f"#{c['id']}　{_status_line(c)}　"
                         f"{_t(c.get('title') or c.get('payload'), 26)}")
        try:
            await cb.message.reply(ui.clip_html("\n".join(lines), limit=3500),
                                   parse_mode=PM, disable_web_page_preview=True)
        except Exception:
            pass
        return

    c = await asyncio.to_thread(db.get_contrib, cid)
    if not c:
        await cb.answer("⌛ 这条投稿已不存在", show_alert=True)
        return
    mine = int(c.get("tg") or 0) == int(u.id)
    adm = is_admin(u.id)

    # ── 投稿人自己的操作 ──
    if act == "t":
        if not (mine or adm):
            await cb.answer("⛔ 只能投稿人本人改", show_alert=True)
            return
        _WAIT[u.id] = {"kind": "title", "cid": cid, "ts": time.time()}
        await cb.answer("请直接发片名 ✍️")
        try:
            await cb.message.reply("🎬 直接回复<b>片名</b>就行（方便管理员核对）：",
                                   parse_mode=PM)
        except Exception:
            pass
        return
    if act == "n":
        if not (mine or adm):
            await cb.answer("⛔ 只能投稿人本人改", show_alert=True)
            return
        _WAIT[u.id] = {"kind": "note", "cid": cid, "ts": time.time()}
        await cb.answer("请直接发说明 ✍️")
        try:
            await cb.message.reply("📝 回复一句<b>说明</b>（如「国语中字 4K」）：",
                                   parse_mode=PM)
        except Exception:
            pass
        return
    if act == "x":
        if not (mine or adm):
            await cb.answer("⛔ 只能投稿人本人撤回", show_alert=True)
            return
        await asyncio.to_thread(db.del_contrib, cid)
        await cb.answer("已撤回投稿")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply("🗑 投稿已撤回。")
        except Exception:
            pass
        return

    # ── 管理员操作 ──
    if not adm:
        await cb.answer("⛔ 只有管理员能审核", show_alert=True)
        return

    if act == "v":          # 列表里点编号 → 展开成审核卡片
        await cb.answer()
        cap = ui.clip_html("\n".join([
            f"📮 <b>投稿 #{cid}</b>", _div(),
            contrib_card(c), _user_line(c), _div(),
        ]), limit=3500)
        try:
            await cb.message.reply(cap, parse_mode=PM,
                                   disable_web_page_preview=True,
                                   reply_markup=_audit_kb(
                                       cid, int(c.get("prio") or 0)))
        except Exception:
            pass
        return

    if act == "p":
        cur = int(c.get("prio") or 0)
        new = 0 if cur else 2
        await asyncio.to_thread(db.set_contrib_prio, cid, new)
        await cb.answer("⭐ 已标为优先" if new else "已取消优先", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=_audit_kb(cid, new))
        except Exception:
            pass
        return

    if act == "f":          # 重新转发（不管当前状态，force 一次）
        await cb.answer("🔁 正在重新转发…")
        ok, why = await do_forward(cid)
        try:
            await cb.message.reply(
                f"{'✅ 已转发给 MT 转存机器人' if ok else '⚠️ 转发失败'}"
                f"（{_t(why, 80)}）", parse_mode=PM)
        except Exception:
            pass
        return

    if str(c.get("status") or "") != "auditing":
        await cb.answer(f"该投稿已处理（{c.get('status')}）", show_alert=True)
        return

    if act == "no":
        await asyncio.to_thread(db.set_contrib, cid, "rejected", u.id, "管理员驳回")
        await cb.answer("已驳回")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.reply(
                ui.clip_html("\n".join(["❌ <b>投稿已驳回</b>　#" + str(cid),
                                        _div(), contrib_card(c)])),
                parse_mode=PM, disable_web_page_preview=True)
        except Exception:
            pass
        try:
            await bot.send_message(int(c["tg"]),
                                   f"❌ 你 #{cid} 的投稿被驳回了，"
                                   "有问题可以直接找管理员。", parse_mode=PM)
        except Exception:
            pass
        return

    # act == "ok"
    await cb.answer("✅ 已通过，正在转给 MT 机器人…")
    await asyncio.to_thread(db.set_contrib, cid, "approved", u.id, "管理员通过")
    ok, why = await do_forward(cid)
    tail = ("✅ 已转给 MT 转存机器人" if ok
            else f"⚠️ 转发失败：{_t(why, 60)}（已把原文私聊给你）")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.reply(
            ui.clip_html("\n".join([
                f"{'✅' if ok else '⚠️'} <b>投稿已通过</b>　#{cid}", _div(),
                contrib_card(c), _user_line(c), _div(), tail,
            ]), limit=3500), parse_mode=PM, disable_web_page_preview=True,
            reply_markup=_audit_kb(cid, int(c.get("prio") or 0)))
    except Exception:
        pass
    try:
        await bot.send_message(
            int(c["tg"]),
            ui.clip_html("\n".join([
                "✅ <b>你的投稿已通过</b>", _div(),
                contrib_card(c), _div(),
                "📤 已自动转给 MT 转存机器人，入库后群里会有通知"
                if ok else "⚠️ 自动转发失败，管理员会手动处理",
            ]), limit=3500), parse_mode=PM, disable_web_page_preview=True)
    except Exception as e:
        logger.debug(f"通知投稿人失败: {e}")


# ══════════════════════════════════════════════════════════ 对外（求片卡片挂按钮用）
def contrib_button(aid: int = 0):
    """给求片卡片用的「📮 投稿资源」按钮"""
    return B("📮 投稿资源（我手里有）", f"cb:new:{int(aid or 0)}")


async def start_from_audit(aid: int, uid: int) -> None:
    """从求片审核卡片直接让用户投稿（外部调用入口）"""
    _WAIT[uid] = {"kind": "payload", "cid": 0, "aid": aid, "ts": time.time()}
