"""兑换阁 —— 用 V积分兑换注册码 / 续期码 / 白名单

设计要点：
  · **不设购买门槛**：任何段位都能购买，段位只决定折扣（见 `S.REALM_DISCOUNT`）
  · **只收 V积分**：灵石不能直接换码，需先 `/duihuan` 把灵石按比例换成 V积分
  · **安全**：码一律私聊发放，绝不在群里明文出现；私聊发不出去就整单取消，
             不会扣了积分却拿不到码，也不会白扔掉一枚码
  · **库存**：库存为空时明确提示缺货并拒绝下单，杜绝"空扣"
"""
from __future__ import annotations

import asyncio

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from . import perm

CMD = dict(prefixes=S.PREFIX)

KIND_NAME = {"invite": "注册码", "renew": "续期码", "whitelist": "白名单"}
# 允许中文写法
KIND_ALIAS = {"注册码": "invite", "invite": "invite", "注册": "invite",
              "续期码": "renew", "续期": "renew", "renew": "renew",
              "白名单": "whitelist", "whitelist": "whitelist", "wl": "whitelist"}


def _is_admin(uid: int) -> bool:
    return uid == S.OWNER or uid in S.ADMINS


# ══════════════════════════════════════════════════════ 渲染
def panel_lines(uid: int, realm: int) -> list[str]:
    """兑换阁面板。按用户要求：**不设任何购买门槛**，任何段位都能买，
    段位只决定折扣力度；价格一律以 **V积分** 计价。"""
    stock = db.code_stock()
    rl = S.realm_level(realm)
    lines = [
        f"🎟 **兑换阁**　{ui.divider()}",
        f"🌌 你的境界｜**{S.realm_by_level(rl)[1]}**（第 {rl} 重）",
        f"💎 段位折扣｜**{S.discount_text(rl)}**　_境界越高，兑同一件东西花得越少_",
        "💠 计价｜**V积分**（与 RT·V积分 1:1 互通）",
        ui.divider(),
    ]
    for _k, (name, emo, base, desc, kind, _need, paused) in S.REDEEM_ITEMS.items():
        price = S.price_for(rl, base)
        old = f" ~~{base}~~" if price < base else ""
        if paused:
            # 未开放的一律不报价：既不显示额度，也点不动
            lines.append(f"⏳ {emo} **{name}**｜**暂未开放**")
        else:
            lines.append(f"✅ {emo} **{name}**｜`{price}` V积分{old}")
        lines.append(f"　　_{desc}_")
        if not paused:
            lines.append("　　🏭 现铸即发 · boss/game 两端都能兑（全局码仓）")
    lines += [
        ui.divider(),
        "💡 兑换成功，**码会私信发给你**，群里不明文出现。",
        "💡 不设购买门槛：任何段位都能买，段位只决定折扣。",
        "💡 灵石不能直接换码 → 先 `/duihuan` 把灵石换成 V积分。",
        "",
        *S.vpoints_rules_md(),   # ★ V积分怎么赚/花多少（与 /duanwei 同一口径）
        "",
        "📖 看不懂段位/货币？点下面「段位·货币说明」。",
    ]
    return lines


def panel_menu(realm: int, points: int, is_owner: bool = False) -> KM:
    """兑换阁按钮。不再有 🔒 境界门槛（任何段位都能买），余额按 V积分 显示。"""
    stock = db.code_stock()
    rl = S.realm_level(realm)
    rows, cur = [], []
    for key, (name, emo, base, _d, kind, _need, paused) in S.REDEEM_ITEMS.items():
        price = S.price_for(rl, base)
        left = stock.get(key, 0)
        if paused:
            label = f"{emo} {name} · 暂未开放"      # 不显示价格
        else:
            label = f"{emo} {name} · {price} V积分"
            if left <= 0:
                label += " 缺货"
        cur.append(B(label, f"ex:{key}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([B(f"💰 余额 {'∞' if is_owner else points} V积分", "me"),
                 B("🎫 我领到的码", "ex:mine")])
    rows.append([B("💱 灵石换 V积分", "dh:panel")])
    rows.append([B("📖 段位·货币说明", "ex:help"), B("↩️ 返回主菜单", "menu")])
    return KM(rows)


# ══════════════════════════════════════════════════════ 货币兑换：灵石 ⇄ V积分
def _duihuan_text(uid: int) -> str:
    ls = db.get_ls(uid)
    us = db.get_points(uid)
    _own = (int(uid) == int(S.OWNER or 0))
    ls_s = "∞ 无限" if _own else f"{ls:,}"
    us_s = "∞ 无限" if _own else us
    return "\n".join([
        f"💱 **灵石 ⇄ V积分 双向互转**　{ui.divider()}",
        f"🪙 你的灵石｜**{ls_s}**",
        f"💎 你的 V积分｜**{us_s}**",
        ui.divider(),
        f"📐 汇率｜**{S.LS_PER_V} 灵石 = 1 V积分**（双向同价）",
        "　_零头（不足 1 V积分 的部分）留在灵石里，不会丢失_",
        ui.divider(),
        "💡 V积分是**通用钱包**：EmbyBoss 签到、兑换阁买码、群主派送都进这里，",
        "　两边机器人余额实时一致。灵石是修仙游戏币，可随时折进来。",
    ])


def _duihuan_menu(ls: int, vp: int) -> KM:
    rows, cur = [], []
    for a in (500, 1000, 2000):
        if ls >= a:
            cur.append(B(f"🪙→💎 {a}灵石→{a // S.LS_PER_V}V", f"dhx:{a}"))
    if cur:
        rows.append(cur)
    if ls:
        rows.append([B(f"🪙→💎 全部 {ls:,} → {ls // S.LS_PER_V} V", "dhx:all")])
    cur = []
    for v in (5, 20, 50):
        if vp >= v:
            cur.append(B(f"💎→🪙 {v}V→{v * S.LS_PER_V:,}灵石", f"dhv:{v}"))
    if cur:
        rows.append(cur)
    if vp:
        rows.append([B(f"💎→🪙 全部 {vp} V → {vp * S.LS_PER_V:,} 灵石", "dhv:all")])
    rows.append([B("🎟 去兑换阁", "ex:panel"), B("↩️ 返回", "menu")])
    return KM(rows)


def _code_text(name: str, emo: str, kind: str, code: str) -> str:
    return (f"{emo} **{ui.safe(name)}** 兑换成功\n{ui.divider()}\n"
            f"🎫 类型｜{KIND_NAME.get(kind, kind)}\n"
            f"🔐 兑换码｜`{ui.code(code)}`\n"
            f"{ui.divider()}\n"
            f"⚠️ 请勿转发给他人，丢失不补。\n"
            f"有问题联系管理员。")


# ══════════════════════════════════════════════════════ 命令
@bot.on_message(filters.command(["exchange", "ex", "兑换"], **CMD))
async def cmd_exchange(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    await msg.reply("\n".join(panel_lines(uid, cv["realm"])),
                    reply_markup=panel_menu(cv["realm"], db.get_points(uid),
                                            is_owner=(int(uid) == int(S.OWNER or 0))))


@bot.on_message(filters.command(["duanwei", "段位说明", "货币说明", "兑换说明"], **CMD))
async def cmd_duanwei(client, msg: Message):
    await msg.reply(S.duanwei_help(),
                    reply_markup=KM([[B("🎟 进入兑换阁", "ex:panel")]]))


@bot.on_message(filters.command(["vpoints", "v积分", "V积分", "怎么获取v积分",
                                 "获取积分", "赚积分"], **CMD))
async def cmd_vpoints(client, msg: Message):
    """★ 用户要求：任何人问「如何获取 V积分」都要能查到完整渠道口径"""
    await msg.reply("\n".join([
        "💎 **V积分 · 获取与用途**", ui.divider(),
        *S.vpoints_rules_md(),
        "",
        "_和 RT·V积分 1:1 互通，Boss / Game / RT 三端共同扣减。_",
    ]), reply_markup=KM([
        [B("🎟 兑换阁", "ex:panel"), B("📖 段位·货币说明", "ex:help")]]))


@bot.on_message(filters.command(["duihuan", "dh", "换积分", "兑换积分"], **CMD))
async def cmd_duihuan(client, msg: Message):
    """货币兑换：把灵石按比例换成 V积分（兑换码只认 V积分）"""
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    await msg.reply(_duihuan_text(uid),
                    reply_markup=_duihuan_menu(db.get_ls(uid), db.get_points(uid)))


@bot.on_callback_query(filters.regex(r"^dh:panel$"))
async def cb_duihuan_panel(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not db.get_cultivator(uid):
        await cb.answer("先发送 /start 创建角色", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text(_duihuan_text(uid),
                                   reply_markup=_duihuan_menu(db.get_ls(uid),
                                                              db.get_points(uid)))
    except Exception as e:
        logger.debug(f"打开货币兑换失败: {e}")


@bot.on_callback_query(filters.regex(r"^dhx:"))
async def cb_duihuan_do(client, cb: CallbackQuery):
    """执行 灵石 → V积分"""
    uid = cb.from_user.id
    if not db.get_cultivator(uid):
        await cb.answer("先发送 /start 创建角色", show_alert=True)
        return
    arg = cb.data.split(":", 1)[1]
    ls = db.get_ls(uid)
    amount = ls if arg == "all" else int(arg or 0)
    if amount <= 0 or amount > ls:
        await cb.answer(f"灵石不足，你只有 {ls}", show_alert=True)
        return
    ok, gained = db.convert_ls_to_us(uid, amount)
    if not ok or gained <= 0:
        await cb.answer(f"至少要 {S.LS_PER_V} 灵石才能换到 1 V积分", show_alert=True)
        return
    new_ls, new_us = db.get_ls(uid), db.get_points(uid)
    await cb.answer(f"✅ 换到 {gained} V积分")
    try:
        await cb.message.edit_text(
            f"💱 **兑换完成**　{ui.divider()}\n"
            f"🪙 灵石｜-{gained * S.LS_PER_V}（剩 **{new_ls}**）\n"
            f"💎 V积分｜+{gained}（现有 **{new_us}**）\n"
            f"{ui.divider()}\n"
            f"🎟 现在可以去 `/exchange` 换码了",
            reply_markup=KM([[B("🎟 去兑换阁", "ex:panel")],
                             [B("↩️ 返回", "menu")]]))
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^dhv:"))
async def cb_duihuan_v2l(client, cb: CallbackQuery):
    """执行 V积分 → 灵石（反向，2026-10-15 用户拍板双向同价）"""
    uid = cb.from_user.id
    if not db.get_cultivator(uid):
        await cb.answer("先发送 /start 创建角色", show_alert=True)
        return
    arg = cb.data.split(":", 1)[1]
    vp = db.get_points(uid)
    amount = vp if arg == "all" else int(arg or 0)
    if amount <= 0 or amount > vp:
        await cb.answer(f"V积分不足，你只有 {vp}", show_alert=True)
        return
    ok, gained = db.convert_us_to_ls(uid, amount)
    if not ok or gained <= 0:
        await cb.answer("兑换失败（V积分不足）", show_alert=True)
        return
    new_ls, new_us = db.get_ls(uid), db.get_points(uid)
    await cb.answer(f"✅ 换到 {gained} 灵石")
    try:
        await cb.message.edit_text(
            f"💱 **兑换完成**　{ui.divider()}\n"
            f"💎 V积分｜-{amount}（剩 **{new_us}**）\n"
            f"🪙 灵石｜+{gained}（现有 **{new_ls:,}**）\n"
            f"{ui.divider()}\n"
            f"⚠️ 换码只认 **V积分**，折成灵石就不能买码了。",
            reply_markup=KM([[B("↩️ 返回兑换面板", "dh:panel")],
                             [B("🎟 去兑换阁", "ex:panel")]]))
    except Exception:
        pass


@bot.on_message(filters.command(["mycodes", "我的码"], **CMD))
async def cmd_mycodes(client, msg: Message):
    uid = msg.from_user.id
    rows = db.my_codes(uid)
    if not rows:
        await msg.reply("🎫 你还没有兑换过任何码。\n发送 `/exchange` 看看有什么。")
        return
    private = "PRIVATE" in str(getattr(msg.chat, "type", "")).upper()
    lines = [f"🎫 **我领到的码**　共 {len(rows)} 枚　{ui.divider()}"]
    for r in rows:
        code = str(r["code"])
        # 用圆点脱敏：不能用 *，Markdown 清洗函数 safe() 会把星号删掉导致形同虚设
        shown = code if private else code[:3] + "••••" + code[-3:]
        ts = r.get("used_at")
        when = ts.strftime("%m-%d %H:%M") if ts else "-"
        lines.append(f"· `{ui.code(shown)}`　{KIND_NAME.get(r['kind'], r['kind'])}　`{when}`")
    if not private:
        lines += [ui.divider(), "_为安全起见群里只显示部分，私聊发 `/mycodes` 可看完整_"]
    await msg.reply("\n".join(lines))


# ══════════════════════════════════════════════════════ 兑换回调
@bot.on_callback_query(filters.regex(r"^ex:"))
async def cb_exchange(client, cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    cv = db.get_cultivator(uid)

    if key == "panel":                       # 从坊市点「兑换阁」进来
        await cb.answer()
        if not cv:
            await cb.answer("请先发送 /start 创建角色", show_alert=True)
            return
        try:
            await cb.message.edit_text(
                "\n".join(panel_lines(uid, cv["realm"])),
                reply_markup=panel_menu(cv["realm"], db.get_points(uid),
                                        is_owner=(int(uid) == int(S.OWNER or 0))))
        except Exception as e:
            logger.debug(f"打开兑换阁失败: {e}")
        return

    if key == "mine":
        await cb.answer()
        rows = db.my_codes(uid)
        if not rows:
            await cb.answer("你还没有兑换记录", show_alert=True)
            return
        lines = [f"🎫 **我领到的码**　共 {len(rows)} 枚　{ui.divider()}"]
        for r in rows:
            code = str(r["code"])
            ts = r.get("used_at")
            when = ts.strftime("%m-%d %H:%M") if ts else "-"
            lines.append(
                f"· `{ui.code(code)}`　{KIND_NAME.get(r['kind'], r['kind'])}　`{when}`")
        try:
            await cb.message.edit_text("\n".join(lines),
                                       reply_markup=KM([[B("↩️ 返回兑换阁", "menu")]]))
        except Exception:
            pass
        return

    if key == "help":
        await cb.answer()
        try:
            await cb.message.edit_text(
                S.duanwei_help(),
                reply_markup=KM([[B("↩️ 返回兑换阁", "ex:panel")]]))
        except Exception:
            pass
        return

    item = S.REDEEM_ITEMS.get(key)
    if not item:
        await cb.answer("物品不存在", show_alert=True)
        return
    if not cv:
        await cb.answer("请先发送 /start 创建角色", show_alert=True)
        return

    name, emo, base, _desc, kind, _need, paused = item
    realm = S.realm_level(cv["realm"])
    is_owner = (int(uid) == int(S.OWNER or 0))

    if paused:
        await cb.answer("🛡 白名单暂未开放兑换，敬请期待", show_alert=True)
        return

    # 不设购买门槛：任何段位都能买，段位只决定折扣力度（见 S.REALM_DISCOUNT）

    price = S.price_for(realm, base)
    if not is_owner and db.get_points(uid) < price:
        await cb.answer(f"V积分不足，本段位需 {price}", show_alert=True)
        return

    # ★ 2026-11-03 库存闸门：库存为 0 直接拒单（杜绝空扣 + 真实缺货提示）
    if db.code_stock().get(key, 0) <= 0:
        await cb.answer(f"「{name}」暂时缺货，请联系管理员补货", show_alert=True)
        return

    # ★ 2026-10-15 统一：兑换即**铸一枚全局 boss 码**（embyboss.Rcode），
    #   这样在 game 买的码，boss 那边也能兑、也能开号——"在哪买都一样"。
    #   days：invite/renew30=30，renew90=90，renew365=365。
    _DAYS = {"invite": 30, "renew30": 30, "renew90": 90, "renew365": 365,
             "renew": 30, "whitelist": 0}
    days = _DAYS.get(key, _DAYS.get(kind, 30))
    from . import unibot
    await cb.answer("正在铸码并私信…")        # ★ 立刻回执，避免铸码期间按钮一直转圈
    minted = await asyncio.to_thread(unibot.mint_boss_code, kind, days or 30,
                                     uid, S.OWNER)
    if not minted.get("ok"):
        try:
            await cb.message.reply("❌ 铸造失败：" + (minted.get("msg") or "未知错误"))
        except Exception:
            pass
        return
    code = minted["code"]
    # 先在本地码仓登记占位（失败也不阻断，主链是 boss Rcode）
    db.add_codes(kind, [code])

    # 先尝试私聊发码：发不出去就退 V积分并作废该码（防白扣）
    try:
        await client.send_message(uid, _code_text(name, emo, kind, code))
    except Exception as e:
        await asyncio.to_thread(unibot.void_boss_code, code)
        db.release_code(code)
        logger.info(f"兑换 {key} 给 {uid} 发码失败，已回滚：{e}")
        try:
            await cb.message.reply("无法私信你，请先私聊机器人点一次 /start 再试")
        except Exception:
            pass
        return

    db.try_spend(uid, price)             # 群主由 try_spend 自行放行（无限）
    db.stock_dec(key)                 # ★ 2026-11-03 真实消耗库存
    db.bump_task(uid, "shop")
    db.log_action("redeem", uid, amount=-price, detail=key)
    try:
        await cb.message.edit_text(
            "\n".join(panel_lines(uid, realm)),
            reply_markup=panel_menu(realm, db.get_points(uid), is_owner=is_owner))
    except Exception as e:
        logger.debug(f"刷新兑换阁失败: {e}")


# ══════════════════════════════════════════════════════ 管理员：补货与查库存
_USAGE = ("用法：\n"
          "`/addcode invite 码1 码2 码3`\n"
          "或 `/addcode renew` 后**回复一条多行文本**（每行一个码）\n"
          f"类型可选：`invite`(注册码) / `renew`(续期码) / `whitelist`(白名单)")


@bot.on_message(filters.command(["addcode", "addcodes"], **CMD))
@perm.gate("admin")
async def cmd_addcode(client, msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply(_USAGE)
        return
    kind = KIND_ALIAS.get(parts[1].lower())
    if not kind:
        await msg.reply(f"⚠️ 未知类型 `{ui.safe(parts[1])}`\n\n{_USAGE}")
        return

    codes = [c.strip() for c in parts[2:] if c.strip()]
    # 支持「回复一条多行文本」批量导入，一次几十枚最省事
    src = msg.reply_to_message
    if src:
        body = src.text or getattr(src, "caption", None) or ""
        codes += [ln.strip() for ln in body.splitlines() if ln.strip()]

    junk = [",", "，", ";", "；", "|", "、"]
    if codes and len(codes) == 1 and any(j in codes[0] for j in junk):
        raw = codes[0]                 # 允许用逗号/分号一次贴一串
        for j in junk:
            raw = raw.replace(j, " ")
        codes = [c.strip() for c in raw.split() if c.strip()]

    if not codes:
        await msg.reply(_USAGE)
        return

    n = db.add_codes(kind, codes)
    await msg.reply(
        f"✅ **{KIND_NAME[kind]}** 补货完成\n{ui.divider()}\n"
        f"提交 **{len(codes)}** 枚　实际入库 **{n}** 枚"
        + (f"（{len(codes) - n} 枚重复已跳过）" if n != len(codes) else "")
        + f"\n📦 当前库存：{db.code_stock().get(kind, 0)} 枚"
    )


@bot.on_message(filters.command(["codes", "stock"], **CMD))
@perm.gate("admin")
async def cmd_codes(client, msg: Message):
    stock = db.code_stock()
    lines = [f"📦 **兑换码库存（预铸）**　{ui.divider()}"]
    for key, it in S.REDEEM_ITEMS.items():
        name = it[0]
        left = stock.get(key, 0)
        tag = " · 暂未开放" if it[6] else ""
        lines.append(f"· {name}{tag}｜剩余 **{left}** 枚")
    lines += [ui.divider(),
              "补货：`/restock invite 20`（注册码 +20）",
              "类型：invite / renew30 / renew90 / renew365 / whitelist"]
    await msg.reply("\n".join(lines))


@bot.on_message(filters.command(["restock", "补货"], **CMD))
@perm.gate("admin")
async def cmd_restock(client, msg: Message):
    """管理员补货：给某类兑换物增加预铸库存。"""
    parts = (msg.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit():
        await msg.reply("用法：`/restock <invite|renew30|renew90|renew365|whitelist> <数量>`")
        return
    key = parts[1].lower()
    if key not in S.REDEEM_ITEMS:
        await msg.reply(f"⚠️ 未知类型 `{ui.safe(key)}`\n可用：invite / renew30 / renew90 / renew365 / whitelist")
        return
    n = int(parts[2])
    left = db.stock_inc(key, n)
    await msg.reply(
        f"✅ **{S.REDEEM_ITEMS[key][0]}** 补货 +{n}\n📦 当前库存：**{left}** 枚")


# ══════════════════════════════════════════════════════ boss 码全局代兑
@bot.on_message(filters.command(["uscode", "兑码", "redeem"], **CMD))
async def cmd_uscode(client, msg: Message):
    """兑 boss 码（注册/续期/白名单）。在 boss 或 game 买的码**这里也能兑**，
    防双花由 used IS NULL 条件更新保证。用法：/uscode 回复码 或 /uscode 码"""
    from . import unibot
    uid = msg.from_user.id
    code = ""
    if msg.reply_to_message and msg.reply_to_message.text:
        code = msg.reply_to_message.text.strip().splitlines()[0]
    elif len(msg.command) > 1:
        code = msg.command[1].strip()
    if not code:
        await msg.reply("📌 用法：<code>/uscode KFC-30-Register-XXXXXXXXXX</code>"
                        "（回复含码的消息发 <code>/uscode</code> 也可）",
                        parse_mode=ParseMode.HTML)
        return
    r = await asyncio.to_thread(unibot.redeem_boss_code_in_game, code, uid)
    if not r.get("ok"):
        await msg.reply("⚠️ " + ui.safe(r.get("msg") or "兑换失败"),
                        parse_mode=ParseMode.HTML)
        return
    await msg.reply(
        "✅ <b>码已核销</b>\n" + ui.divider() + "\n"
        f"🎫 {ui.safe(r.get('msg'))}\n"
        f"🔐 码｜<code>{ui.code(code[:12])}░░░</code>\n" + ui.divider() +
        "🤫 同一条码已全局作废（boss/game 双端防双花）。",
        parse_mode=ParseMode.HTML)
