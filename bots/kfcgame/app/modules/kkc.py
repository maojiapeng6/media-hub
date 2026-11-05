"""kkc —— KFC 修仙 bot 的开号/派送/入群建档模块（2026-10-15 统一层）。

· /kk [UID|回复某人]  群主/管理员：引导对方走开号（自备115·30天·需保号）
· 开号成功 → **本机器人在群里发播报**（三行标准卡：用户名/UID/Emby名）
· /pay 金额 @某人     群主：V积分无限额度派送（写流水，两边机器人余额同步）
· 入群自动建档        NEW_CHAT_MEMBERS → unibot.ensure_member
· /wallet             个人总资产 = 灵石 + V积分 双列展示 + 折合
"""
from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import string

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot, db, helper, settings as S, ui
from . import perm, unibot

CMD = dict(prefixes=S.PREFIX)
PM = ParseMode.HTML

_INVITE_TTL = 1500          # 开号邀请有效期（秒）
_INVITE: dict[int, dict] = {}   # uid -> {target, by, exp}


def _div() -> str:
    return ui.divider()


# ───────────────────────── 开号邀请与执行 ─────────────────────────
async def _send_invite(target_uid: int, by_name: str) -> None:
    """私聊被邀请人：美观欢迎语 + 确认按钮"""
    kb = KM([[B("✅ 确认开通（自备115·30天）", f"kkc:go:{target_uid}"),
              B("❌ 暂不需要", f"kkc:no:{target_uid}")]])
    text = "\n".join([
        "🎉 <b>KFC 影视库 · 开通邀请</b>",
        _div(),
        f"👑 群主/管理 <b>{ui.safe(by_name)}</b> 为你发放了一个开通资格：",
        "",
        "📺 <b>你能得到</b>",
        "　· KFC 影视 Emby 独立账号（4K 原盘 / 杜比视界 / HDR）",
        "　· 有效期 <b>30 天</b>，到期可用 V积分续期",
        "　· 支持 TG 群内求片、自动入库",
        "",
        "📋 <b>开通条件</b>（重要）",
        "　· <b>自备 115 会员账号</b>（提供 cookie 授权，用于 302 直连播放）",
        "　· 每周至少 <b>保号登录一次</b>（发「保号」随时查询状态）",
        _div(),
        "👇 点「确认开通」，我把下一步发给你。",
    ])
    await bot.send_message(target_uid, text, reply_markup=kb, parse_mode=PM)


@bot.on_message(filters.command(["kk", "开号", "送号"], **CMD))
async def cmd_kk(client, msg: Message):
    """群主/管理员开号入口：/kk @某人 ｜ 回复某人 /kk ｜ /kk 数字UID"""
    uid = msg.from_user.id
    from .request import is_admin
    if not is_admin(uid):
        await msg.reply("⛔ 仅群主/授权管理员可用", parse_mode=PM)
        return
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    elif len(msg.command) > 1:
        t = msg.command[1].lstrip("@")
        if t.isdigit():
            try:
                target = await client.get_chat(int(t))
            except Exception:
                await msg.reply("⚠️ 这个 UID 我没见过（ta 没私聊过任一机器人）",
                                parse_mode=PM)
                return
        else:
            # 用户名 → 先查本地档案
            row = await asyncio.to_thread(unibot.find_by_username, t)
            if row:
                try:
                    target = await client.get_chat(int(row))
                except Exception:
                    pass
    if not target:
        await msg.reply("📌 用法：<b>回复某人</b>发 <code>/kk</code>，或 "
                        "<code>/kk @用户名 / UID</code>", parse_mode=PM)
        return
    tuid = int(target.id)
    _INVITE[tuid] = {"target": tuid, "by": uid,
                     "by_name": msg.from_user.first_name or "管理",
                     "exp": dt.datetime.now()}
    unibot.ensure_member(tuid, getattr(target, "first_name", "") or "",
                         getattr(target, "username", "") or "")
    await msg.reply(f"📨 已私聊 <a href='tg://user?id={tuid}'>ta</a> 发送开通邀请"
                    f"（UID <code>{tuid}</code>）", parse_mode=PM)
    try:
        await _send_invite(tuid, msg.from_user.first_name or "管理")
    except Exception as e:
        logger.info(f"开号邀请私聊失败 {tuid}: {e}")
        await msg.reply("⚠️ 对方没私聊过机器人，发不出去。让 ta 先私聊我发 /start",
                        parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^kkc:go:(\d+)$"))
async def cb_kk_go(client, cb: CallbackQuery):
    tuid = int(cb.data.split(":")[2])
    if cb.from_user.id != tuid:
        await cb.answer("这个邀请不是给你的哦", show_alert=True)
        return
    inv = _INVITE.get(tuid)
    if not inv or (dt.datetime.now() - inv["exp"]).total_seconds() > _INVITE_TTL:
        await cb.answer("邀请已过期，请让群主重新 /kk 一次", show_alert=True)
        return
    await cb.answer("开始开通…")
    await cb.message.edit_text(
        "🔧 <b>正在为你开通，请稍候…</b>", parse_mode=PM)
    name = _gen_emby_name(cb.from_user.first_name or str(tuid))
    pw = unibot.gen_password(10)
    res = await asyncio.to_thread(unibot.create_emby_user, name, 30, pw)
    if not res.get("ok"):
        await cb.message.edit_text(
            "😵 <b>开通失败</b>\n" + _div() + f"\n{ui.safe(res.get('msg') or '未知错误')}"
            "\n\n💡 把这条消息截图发给群主排查。", parse_mode=PM)
        return
    # 绑定关系入库（emby 名 + UID 对应），并顺手建档
    await asyncio.to_thread(unibot.bind_after_open, tuid, res["embyid"],
                            name, pw, cb.from_user.first_name or "",
                            cb.from_user.username or "")
    acc = unibot.emby_account_of(tuid) or {}
    lines = _welcome_card(cb.from_user.first_name or "", tuid, name,
                          res["ex"], cb.from_user.username or "")
    await cb.message.edit_text("\n".join(lines), parse_mode=PM)
    # 密码私发（不进群消息）
    try:
        # ★ 播放线路对照 RT（resourcetool）的 emby_access_lines 表：
        #   以表内启用线路为准（可配多条，用户自选最快的）。以前只发
        #   S.EMBY['public_url'] 单条，容易与实际主推线路脱节。现在发全部启用线路。
        _lines = [l for l in (S.EMBY.get('lines') or []) if l.get('enabled') and l.get('url')]
        if _lines:
            _line_txt = "\n".join(
                "　· <b>%s</b>｜<code>%s</code>" % (ui.safe(l.get('name') or '线路'),
                                                  ui.safe(l.get('url')))
                for l in _lines)
        else:
            _line_txt = "　· <code>%s</code>" % ui.safe(
                S.EMBY.get('public_url') or '群公告/群主官网')
        await client.send_message(
            tuid,
            "\n".join(["🔐 <b>你的 Emby 账号（请保存，勿转发）</b>", _div(),
                       f"👤 用户名｜<code>{ui.safe(name)}</code>",
                       f"🔑 密码｜<code>{ui.safe(res['pwd'])}</code>",
                       "🌐 <b>播放线路</b>（任选一个，推荐最快的）：",
                       _line_txt,
                       _div(),
                       "⏳ 30 天后到期，发「续期」可用 V积分自助续。"]),
            parse_mode=PM)
    except Exception as e:
        logger.debug(f"凭据私发失败: {e}")
    # 群内播报（谁开的号谁播报 = 本 bot）
    try:
        grp = "\n".join(_group_broadcast(inv["by_name"], cb.from_user.first_name or "",
                                         tuid, name, acc))
        await helper.send_group(client, ui.clip_html(grp), parse_mode=PM)
    except Exception as e:
        logger.warning(f"开号群播报失败: {e}")
    _INVITE.pop(tuid, None)


@bot.on_callback_query(filters.regex(r"^kkc:no:(\d+)$"))
async def cb_kk_no(client, cb: CallbackQuery):
    _INVITE.pop(int(cb.data.split(":")[2]), None)
    await cb.answer("好的，已取消")
    await cb.message.edit_text("ok 已取消开通邀请。想开了随时找群主。", parse_mode=PM)


def _gen_emby_name(display: str) -> str:
    """Emby 用户名：kfc + 随机，避免重名和暴露隐私"""
    alpha = string.ascii_lowercase + string.digits
    return "kfc" + "".join(secrets.choice(alpha) for _ in range(6))


def _welcome_card(name, uid, emby_name, ex, username) -> list[str]:
    head = ["🎉 <b>开通成功！欢迎登上 KFC 影视号</b>", _div()]
    head += ui.user_card(uid, name, username, emby_name, label="机主", mode="html")
    head += [_div(),
             f"📅 有效期至｜<b>{ex.strftime('%Y-%m-%d')}</b>",
             "🔑 账号密码｜已<b>单独私发</b>给你（注意保存）",
             "🛰️ 保号｜每周登录一次，发「保号」查状态",
             "🎬 求片｜群里发「求片 片名」，入库自动通知",
             _div(),
             "🍀 祝观影愉快 —— 遇到问题找群主"]
    return head


def _group_broadcast(by_name, name, uid, emby_name, acc) -> list[str]:
    head = ["🚀 <b>新友登船 · KFC 影视</b>", _div()]
    head += ui.user_card(uid, name, (acc or {}).get("tg_username") or "",
                         emby_name, label="船员", mode="html")
    ex = (acc or {}).get("ex")
    head += [_div(),
             f"🎫 开通方式｜<b>自备 115 · 30 天</b>",
             f"👑 签发管理｜<b>{ui.safe(by_name)}</b>",
             f"📅 首日到账｜<b>{(ex.strftime('%Y-%m-%d') if ex else '-')}</b>",
             "🔐 密码已私发 · 群里不展示"]
    return head


# ───────────────────────── 派送（群主无限币） ─────────────────────────
@bot.on_message(filters.command(["pay", "派送", "发币"], **CMD))
async def cmd_pay(client, msg: Message):
    """群主：/pay 50 回复某人 ｜ /pay 50 @用户名 ｜ /pay UID 50"""
    uid = msg.from_user.id
    if not perm.is_owner(uid):
        await msg.reply("⛔ 仅群主可派送", parse_mode=PM)
        return
    args = msg.command[1:]
    target_uid, amount = None, 0
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_uid = msg.reply_to_message.from_user.id
        amount = int(args[0]) if args and str(args[0]).lstrip("+").isdigit() else 0
    elif len(args) >= 2 and str(args[0]).isdigit():
        target_uid, amount = int(args[0]), int(args[1])
    elif len(args) >= 1 and str(args[0]).lstrip("@").isdigit():
        target_uid, amount = int(args[0].lstrip("@")), 0
    if not target_uid or amount <= 0:
        await msg.reply("📌 用法：<b>回复某人</b> <code>/pay 50</code> 或 "
                        "<code>/pay UID 50</code>（正数=派送，V积分）", parse_mode=PM)
        return
    r = unibot.transfer_points(uid, target_uid, amount, note=f"群主派送@{msg.chat.id}")
    if not r.get("ok"):
        await msg.reply("⚠️ " + ui.safe(r.get("msg") or "派送失败"), parse_mode=PM)
        return
    try:
        tn = await bot.get_chat(target_uid)
        tname = tn.first_name or ""
    except Exception:
        tname = ""
    await msg.reply(
        "💸 <b>派送成功</b>\n" + _div() + "\n"
        f"👑 群主 → <a href='tg://user?id={target_uid}'>{ui.safe(tname or 'TA')}</a>"
        f"　<b>+{amount} V积分</b>\n"
        f"💰 ta 当前 V积分｜<b>{r['to_balance']}</b>", parse_mode=PM)


# ───────────────────────── 个人钱包（双币展示） ─────────────────────────
@bot.on_message(filters.command(["wallet", "钱包", "资产"], **CMD))
async def cmd_wallet(client, msg: Message):
    uid = msg.from_user.id
    db.ensure_shared_user(uid)
    ls = db.get_ls(uid)
    vp = db.get_points(uid)
    days = await asyncio.to_thread(_my_days, uid)
    total_v = vp + ls // S.LS_PER_V
    txt = "\n".join([
        "💼 <b>我的资产</b>", _div(),
    ] + ui.user_card(uid, msg.from_user.first_name or "",
                     msg.from_user.username or "", "", label="", mode="html") + [
        _div(),
        f"🪙 灵石（EmbyGame）｜<b>{ls:,}</b>",
        f"💠 V积分（通用钱包）｜<b>{vp}</b>",
        f"📦 Emby 注册天数池｜<b>{days} 天</b>",
        _div(),
        f"🧮 折合总资产｜<b>≈ {total_v} V积分</b>"
        f"（{S.LS_PER_V} 灵石 = 1 V积分）",
        "💡 发「<code>/duihuan</code>」灵石⇄V积分双向互转",
    ])
    await msg.reply(txt, parse_mode=PM)


def _my_days(uid: int) -> int:
    acc = unibot.emby_account_of(uid)
    return int((acc or {}).get("us") or 0)


# ───────────────────────── 入群自动建档 ─────────────────────────
@bot.on_message(filters.new_chat_members & filters.chat(S.GROUP))
async def on_new_members(client, msg: Message):
    """新人入群 → UID/实时用户名自动落 kfc_members + boss 账本建行。"""
    for u in (msg.new_chat_members or []):
        await asyncio.to_thread(unibot.ensure_member, int(u.id),
                                u.first_name or "", u.username or "")
