"""社交与竞技：天机榜、拜访、夺取、斗法、奖励领取、红包"""
from __future__ import annotations

import asyncio
import datetime as dt
import random

from loguru import logger
from pyrogram import filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from ..helper import (cooldown_left, fmt_delta, month_period, notify, rand,
                      sect_bonus, week_period)
from .game import PENDING, check_achievements

CMD = dict(prefixes=S.PREFIX)

# 进行中的红包：msg_id -> {pool, left, claimers, owner}
ENVELOPES: dict[int, dict] = {}


def _target(msg: Message):
    """取被操作对象：优先回复的人，其次 @username（这里只支持回复）"""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        if not u.is_bot:
            return u.id, u.first_name
    return None, None


def _sect_name(k: str) -> str:
    v = S.SECTS.get(k)
    return f"{v[1]} {v[0]}" if v else "散修"


# ═══════════════════════════════════════════════════ 天机榜
@bot.on_message(filters.command(["rank", "ranking"], **CMD))
async def cmd_rank(client, msg: Message):
    await _send_rank(msg, "realm")


@bot.on_callback_query(filters.regex(r"^rank:"))
async def cb_rank(client, cb: CallbackQuery):
    await _send_rank(cb.message, cb.data.split(":", 1)[1], edit=True,
                     uid=cb.from_user.id)
    await cb.answer()


async def _send_rank(msg, mode: str, edit: bool = False, uid: int | None = None):
    if mode == "mine":                      # 我的排名（/myrank 与按钮共用）
        text = await asyncio.to_thread(myrank_text, uid or msg.from_user.id)
        kb = KM([[B("🌌 境界榜", "rank:realm"), B("💰 财富榜", "rank:rich")],
                 [B("↩️ 返回主菜单", "menu")]])
        if edit:
            await msg.edit_text(text, reply_markup=kb)
        else:
            await msg.reply(text, reply_markup=kb)
        return
    if mode == "rich":
        rows = db.top_cultivators(15, "cultivation DESC")
        pm = db.ls_map([c["tg"] for c in rows])          # 一次批量取灵石，别逐个查
        rows.sort(key=lambda c: pm.get(c["tg"], 0), reverse=True)
    else:
        rows = db.top_cultivators(15)
        pm = db.ls_map([c["tg"] for c in rows])
    lines = [f"🔮 **天机榜 · {'财富榜' if mode == 'rich' else '境界榜'}**",
             f"{ui.divider()}"]
    for i, c in enumerate(rows, 1):
        val = (f"{pm.get(c['tg'], 0)} {S.CURRENCY}" if mode == "rich"
               else S.realm_by_level(c["realm"])[1])
        lines.append(f"{ui.rank_tag(i)}｜**{c['daohao']}**｜"
                     f"{_sect_name(c['sect'])}｜{val}")
    if not rows:
        lines.append("_榜上无人，快去 `/start` 抢占头名_")
    kb = KM([[
        B("🌌 境界榜", "rank:realm"), B("💰 财富榜", "rank:rich"),
    ], [B("↩️ 返回主菜单", "menu")]])
    text = "\n".join(lines)
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.reply(text, reply_markup=kb)


def myrank_text(uid: int) -> str:
    """我的排名（同步：命令 /myrank 与按钮「📊 我的排名」共用）"""
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    r = db.rank_of(uid)
    total = len(db.all_ids())
    p = db.get_ls(uid)
    # 财富排名：一次批量取灵石再排序，避免上千次单独查询
    allc = db.top_cultivators(1000, "cultivation DESC")
    pm = db.ls_map([c["tg"] for c in allc])
    rich = sorted(allc, key=lambda c: pm.get(c["tg"], 0), reverse=True)
    rich_rank = next((i for i, c in enumerate(rich, 1) if c["tg"] == uid), None)
    return (
        f"📊 **{cv['daohao']} 的排名**\n{ui.divider()}\n"
        f"🌌 境界排名｜第 **{ui.cn_no(r)}** 名 / 共 **{total}** 位\n"
        f"💰 财富排名｜"
        + (f"第 **{ui.cn_no(rich_rank)}** 名 / 共 {total} 位" if rich_rank
           else "尚未上榜") + "\n"
        f"📈 修为｜**{cv['cultivation']}**\n"
        f"👛 身家｜**{p} {S.CURRENCY}**"
    )


@bot.on_message(filters.command(["myrank", "mrank"], **CMD))
async def cmd_myrank(client, msg: Message):
    await msg.reply(await asyncio.to_thread(myrank_text, msg.from_user.id),
                    reply_markup=KM([[B("🌌 境界榜", "rank:realm"),
                                      B("💰 财富榜", "rank:rich")],
                                     [B("↩️ 返回主菜单", "menu")]]))


# ═══════════════════════════════════════════════════ 拜访
@bot.on_message(filters.command(["visit"], **CMD))
async def cmd_visit(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    tid, tname = _target(msg)
    if not tid:
        await msg.reply("用法：**回复**某个道友的消息，发送 `/visit`")
        return
    tcv = db.get_cultivator(tid)
    if not tcv:
        await msg.reply("对方尚未踏入仙途。")
        return
    db.bump_task(uid, "visit")
    await msg.reply(
        f"🚪 **{cv['daohao']}** 拜访了 **{tcv['daohao']}**\n{ui.divider()}\n"
        f"{_sect_name(tcv['sect'])}｜**{S.realm_by_level(tcv['realm'])[1]}**\n"
        f"💰 身家｜**{db.get_ls(tid)} {S.CURRENCY}**\n"
        f"📈 修为｜**{tcv['cultivation']}**\n"
        f"_宾主尽欢，各有所得。_"
    )


# ═══════════════════════════════════════════════════ 夺取
@bot.on_message(filters.command(["rob", "steal"], **CMD))
async def cmd_rob(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    tid, _ = _target(msg)
    if not tid:
        await msg.reply("用法：**回复**某个道友的消息，发送 `/rob`")
        return
    if tid == uid:
        await msg.reply("不能夺自己。")
        return
    tcv = db.get_cultivator(tid)
    if not tcv:
        await msg.reply("对方尚未踏入仙途。")
        return
    if tcv["shield_until"] and tcv["shield_until"] > dt.datetime.now():
        await msg.reply(f"🧿 对方有护身丹护体，无法夺取。")
        return
    tp = db.get_ls(tid)
    if tp < 10:
        await msg.reply("对方身无长物，无利可图。")
        return

    rate = S.G["rob_success_rate"] + sect_bonus(cv["sect"], "rob") \
        - sect_bonus(tcv["sect"], "defend")
    # 境界压制
    rate += (cv["realm"] - tcv["realm"]) * 0.06
    rate = min(max(rate, 0.05), 0.85)

    if random.random() < rate:
        amt = min(int(tp * S.G["rob_max_percent"]), 200) or 1
        amt = max(amt, 1)
        db.add_ls(tid, -amt)
        db.add_ls(uid, amt)
        db.update_cultivator(uid, rob_win=cv["rob_win"] + 1)
        db.log_action("rob", uid, tid, amt, "win")
        db.bump_task(uid, "rob")
        new_ach = check_achievements(uid, cv)
        txt = (f"🩸 **得手！**\n{ui.divider()}\n"
               f"**{cv['daohao']}** 趁 **{tcv['daohao']}** 不备，"
               f"掠走 **{amt}** {S.CURRENCY}。\n"
               f"👛 你的身家｜{db.get_ls(uid)} {S.CURRENCY}")
        if new_ach:
            txt += "\n🏅 解锁成就：" + "、".join(new_ach)
        await msg.reply(txt)
        await notify(client, tid, f"🩸 你被 **{cv['daohao']}** 夺走 **{amt}** {S.CURRENCY}！")
    else:
        fine = min(int(db.get_ls(uid) * 0.05), 50)
        if fine:
            db.add_ls(uid, -fine)
        db.update_cultivator(uid, shield_until=dt.datetime.now() + dt.timedelta(minutes=30))
        db.log_action("rob", uid, tid, -fine, "lose")
        await msg.reply(
            f"💢 **失手！**\n{ui.divider()}\n"
            f"**{tcv['daohao']}** 早有防备，你反被震退。\n"
            + (f"损失 **{fine}** {S.CURRENCY}\n" if fine else "")
            + f"🛡 你获得 30 分钟护身（免受夺取）"
        )


# ═══════════════════════════════════════════════════ 斗法
@bot.on_message(filters.command(["duel", "fight", "pk"], **CMD))
async def cmd_duel(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    tid, _ = _target(msg)
    if not tid or tid == uid:
        await msg.reply("用法：**回复**某个道友的消息，发送 `/duel`")
        return
    left = cooldown_left(cv["last_duel"], S.G["duel_cooldown_min"])
    if left > 0 and not cv["duel_buff"]:
        await msg.reply(f"⏳ 元气未复，还需 **{fmt_delta(left)}**。")
        return
    tcv = db.get_cultivator(tid)
    if not tcv:
        await msg.reply("对方尚未踏入仙途。")
        return

    if cv["duel_buff"]:
        win = True
    else:
        score_a = cv["realm"] * 100 + cv["cultivation"] / 10
        score_a *= 1 + sect_bonus(cv["sect"], "duel")
        score_b = tcv["realm"] * 100 + tcv["cultivation"] / 10
        score_b *= 1 + sect_bonus(tcv["sect"], "duel")
        win = random.random() < score_a / (score_a + score_b)

    stake = min(30, max(5, db.get_ls(tid) // 20))
    db.update_cultivator(uid, last_duel=dt.datetime.now(), duel_buff=0)
    db.bump_task(uid, "duel")

    if win:
        db.add_ls(tid, -stake)
        db.add_ls(uid, stake)
        db.update_cultivator(uid, duel_win=cv["duel_win"] + 1)
        db.log_action("duel", uid, tid, stake, "win")
        new_ach = check_achievements(uid, cv)
        txt = (f"⚔️ **斗法 · {cv['daohao']} 胜**\n{ui.divider()}\n"
               f"剑气纵横，**{tcv['daohao']}** 落了下风。\n"
               f"🏆 赢下 **{stake}** {S.CURRENCY}｜👛 {db.get_ls(uid)}")
        if new_ach:
            txt += "\n🏅 解锁成就：" + "、".join(new_ach)
        await msg.reply(txt)
        await notify(client, tid, f"⚔️ 你在斗法中败给 **{cv['daohao']}**，损失 **{stake}** {S.CURRENCY}。")
    else:
        db.add_ls(uid, -stake)
        db.add_ls(tid, stake)
        db.log_action("duel", uid, tid, -stake, "lose")
        await msg.reply(
            f"⚔️ **斗法 · {cv['daohao']} 败**\n{ui.divider()}\n"
            f"**{tcv['daohao']}** 技高一筹。\n"
            f"💸 损失 **{stake}** {S.CURRENCY}｜👛 {db.get_ls(uid)}"
        )
        await notify(client, tid, f"⚔️ 你战胜了 **{cv['daohao']}**，赢得 **{stake}** {S.CURRENCY}。")


# ═══════════════════════════════════════════════════ 领取周榜/月榜奖励
# ★ 这里**不能**再挂 "claim"：/claim 已被 modules/bind.py 的「认领我的名字」
#   占用（需求：未关联 TG 的用户用 /claim 把榜上名字变成个人主页链接）。
#   两个 handler 同时匹配 /claim 会各回一条消息，用户收到两条互不相干的回复。
#   领奖改用 /claimreward（原本就是它的别名，语义也不冲突）。
@bot.on_message(filters.command(["claimreward"], **CMD))
async def cmd_claim(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    wk, mo = week_period(), month_period()
    msgs = []
    for period, rewards in ((wk, S.G["weekly_reward"]), (mo, S.G["monthly_reward"])):
        if db.has_claimed(uid, period):
            continue
        top = db.top_cultivators(len(rewards))
        ranks = [c["tg"] for c in top]
        if uid in ranks:
            idx = ranks.index(uid)
            amt = rewards[idx]
            if db.record_claim(uid, period, idx + 1, amt):
                db.add_ls(uid, amt)                 # 周 / 月榜奖励 → 灵石
                msgs.append(f"🎁 {period} 第 {ui.cn_no(idx + 1)} 名 → "
                            f"**+{amt}** {S.CURRENCY}")
    if not msgs:
        await msg.reply("🎁 暂无可领取的奖励。\n"
                        "上榜后每周一 / 每月 1 号可来 `/claim` 领奖。")
        return
    await msg.reply("🎉 **奖励已发放**\n" + ui.divider() + "\n" + "\n".join(msgs)
                    + f"\n\n👛 当前身家｜{db.get_ls(uid)} {S.CURRENCY}")


# ═══════════════════════════════════════════════════ 拼手气红包
@bot.on_message(filters.command(["hb", "redenvelope"], **CMD))
async def cmd_hb(client, msg: Message):
    if msg.chat.type.name == "PRIVATE":
        await msg.reply("红包只能在群里发。")
        return
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    parts = msg.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.reply("用法：`/hb 总额 份数`\n例如 `/hb 200 10`（每人至少 1）")
        return
    pool, count = int(parts[1]), int(parts[2])
    if pool < count or count < 1 or count > 50:
        await msg.reply("参数不对：份数 1~50，且总额不能小于份数。")
        return
    if not db.try_spend_ls(uid, pool):
        await msg.reply(f"👛 灵石不足，你有 {db.get_ls(uid)} {S.CURRENCY}。")
        return
    text = (f"🧧 **{cv['daohao']}** 的拼手气红包\n{ui.divider()}\n"
            f"💰 总额｜**{pool}** {S.CURRENCY}\n"
            f"🎯 份数｜{count}\n"
            f"👥 已抢｜0 / {count}\n\n"
            f"_点击下方按钮抢夺，手快有手慢无_")
    sent = await msg.reply(text, reply_markup=KM([[B("🧧 抢！", "hb:grab")]]))
    ENVELOPES[sent.id] = {"pool": pool, "left": count, "total": count,
                          "owner": uid, "grabbers": {}, "msg": sent.id, "chat": msg.chat.id}
    db.log_action("hb", uid, amount=-pool, detail="create")


@bot.on_callback_query(filters.regex(r"^hb:grab$"))
async def cb_grab(client, cb: CallbackQuery):
    env = ENVELOPES.get(cb.message.id)
    if not env:
        await cb.answer("红包已被抢光或已过期", show_alert=True)
        return
    uid = cb.from_user.id
    if uid == env["owner"]:
        await cb.answer("不能抢自己的红包", show_alert=True)
        return
    if uid in env["grabbers"]:
        await cb.answer(f"你已抢到 {env['grabbers'][uid]}", show_alert=True)
        return
    if env["left"] <= 0:
        await cb.answer("手慢了，红包没了", show_alert=True)
        return

    left_people = env["left"]
    remaining = env["pool"]
    if left_people == 1:
        amt = remaining
    else:
        amt = max(1, random.randint(1, max(1, remaining // left_people * 2)))
        amt = min(amt, remaining - (left_people - 1))
    env["pool"] -= amt
    env["left"] -= 1
    env["grabbers"][uid] = amt
    db.add_ls(uid, amt)                      # 抢到红包 → 灵石
    cv = db.get_cultivator(uid)
    name = cv["daohao"] if cv else ui.safe(cb.from_user.first_name)

    best = max(env["grabbers"], key=env["grabbers"].get)
    lines = [f"🧧 **红包详情**", ui.divider(),
             f"👥 已抢｜{len(env['grabbers'])} / {env['total']}"]
    for u, a in sorted(env["grabbers"].items(), key=lambda x: -x[1]):
        c = db.get_cultivator(u)
        nm = c["daohao"] if c else str(u)
        crown = " 🏆" if u == best else ""
        lines.append(f"· {nm}　**{a}**{crown}")
    if env["left"] == 0:
        lines.append(ui.divider())
        lines.append("🎊 **红包已抢完**")

    try:
        await cb.message.edit_text("\n".join(lines),
                                   reply_markup=KM([[B("🧧 抢！", "hb:grab")]]) if env["left"] else None)
    except Exception:
        pass
    await cb.answer(f"恭喜抢到 {amt} {S.CURRENCY}！")
