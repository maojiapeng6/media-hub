"""修仙玩法核心命令"""
from __future__ import annotations

import asyncio
import datetime as dt
import io

from loguru import logger
from pyrogram import filters
from pyrogram.types import CallbackQuery, Message

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from ..helper import (cooldown_left, fmt_delta, in_group, notify, rand,
                      require_group, sect_bonus, week_period, month_period)

CMD = dict(prefixes=S.PREFIX)

# 角色创建过程中的临时状态（内存暂存，重启即失效，不影响已创建角色）
PENDING: dict[int, dict] = {}


# ═══════════════════════════════════════════════════ 工具
def _guard(cv: dict | None):
    """返回 (cv, 错误文本)"""
    if not cv:
        return None, ("🌌 你尚未踏入仙途。\n先私聊机器人发送 `/start` 创建角色。")
    return cv, None


def check_achievements(tg: int, cv: dict) -> list[str]:
    """检查并解锁成就，返回新解锁名称列表"""
    got: list[str] = []
    cond = {
        "realm_3": cv["realm"] >= 3,
        "realm_5": cv["realm"] >= 5,
        "realm_10": cv["realm"] >= 10,
        "rich_1k": db.get_ls(tg) >= 1000,
        "rob_master": db.count_action(tg, "rob") >= 10,
        "duel_master": db.count_action(tg, "duel") >= 10,
        "sign_7": cv["sign_streak"] >= 7,
        "sign_30": cv["sign_streak"] >= 30,
    }
    for k, ok in cond.items():
        if ok and db.unlock_ach(tg, k):
            got.append(S.ACHIEVEMENTS[k][0])
    return got


# ═══════════════════════════════════════════════════ /start
def _create_text() -> str:
    """未创建角色时「修仙面板」的引导文案"""
    return "\n".join([
        f"🌌 **欢迎来到 {S.SERVER_NAME}·修仙录**",
        ui.divider(),
        "修仙一事，起于微末。两步即可踏上仙途：",
        "",
        "① **点下面的按钮**选择门派（也可发 `/choose 序号`）",
        "② 发送 `/name 你的道号` 定下名号",
        "",
        "⚠️ 门派选定后不可更改，请谨慎。",
        "_选好门派后会自动提示你下一步_",
    ])


@bot.on_message(filters.command("start", **CMD))
async def cmd_start(client, msg: Message):
    uid = msg.from_user.id
    # ★ 2026-11-04 用户要求：/start 先出双面板选择（EMBY / 修仙）
    await msg.reply(ui.start_hub_text(), reply_markup=ui.start_hub_menu())


@bot.on_callback_query(filters.regex(r"^hub:(emby|game)$"))
async def cb_hub(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if cb.data.endswith("emby"):               # 📺 EMBY 面板
        from . import account as _account
        await cb.answer()
        await _account.show_panel(client, cb, uid)
        return
    await cb.answer()                          # 🧘 修仙面板
    cv = db.get_cultivator(uid)
    if not cv:
        db.unlock_ach(uid, "first_step")
        try:
            await cb.message.edit_text(_create_text(),
                                       reply_markup=ui.sect_menu())
        except Exception:
            await cb.message.reply(_create_text(), reply_markup=ui.sect_menu())
        return
    card = ui.cultivator_card(cv, db.get_ls(uid), db.get_points(uid),
                              db.rank_of(uid),
                              is_owner=(int(uid) == int(S.OWNER or 0)))
    try:
        await cb.message.edit_text(card, reply_markup=ui.main_menu())
    except Exception as e:
        logger.debug(f"修仙面板打开失败: {e}")


# ═══════════════════════════════════════════════════ /help
@bot.on_message(filters.command("help", **CMD))
async def cmd_help(client, msg: Message):
    await msg.reply(ui.help_text(msg.from_user.id if msg.from_user else None),
                    reply_markup=ui.back_menu())


SECT_KEYS = list(S.SECTS.keys())


def resolve_sect(t: str) -> str | None:
    """把用户输入解析成门派 key：支持序号(1-4) / 门派名 / 关键字"""
    t = t.strip()
    if t.isdigit():
        i = int(t) - 1
        return SECT_KEYS[i] if 0 <= i < len(SECT_KEYS) else None
    for k, v in S.SECTS.items():
        if t == k or t == v[0] or t in v[0] or v[0] in t:
            return k
    return None


def sect_list_text() -> str:
    return "\n".join(f"`{i}.` {v[1]} **{v[0]}**｜{v[2]}"
                     for i, v in enumerate(S.SECTS.values(), 1))


async def finish_sect(msg, uid: int, key: str) -> None:
    """记下门派，若已有道号则直接创建角色"""
    info = PENDING.setdefault(uid, {})
    info["sect"] = key
    name, emo = S.SECTS[key][0], S.SECTS[key][1]
    if info.get("daohao"):
        db.create_cultivator(uid, info["daohao"], key)
        PENDING.pop(uid, None)
        await msg.reply(
            f"{emo} 门派 **{name}** 已定，道号 **{info['daohao']}**。\n\n"
            f"🌌 **角色创建完成！**\n"
            f"点下面的「🧘 打坐」就能直接领第一笔灵石。",
            reply_markup=ui.main_menu(),
        )
    else:
        await msg.reply(
            f"{emo} 你选择了 **{name}**。\n\n"
            f"最后一步：发送 `/name 你的道号` 定下名号。"
        )


# ═══════════════════════════════════════════════════ /choose
@bot.on_message(filters.command("choose", **CMD))
async def cmd_choose(client, msg: Message):
    uid = msg.from_user.id
    cv = db.get_cultivator(uid)
    if cv:
        sect = S.SECTS.get(cv["sect"])
        await msg.reply(f"⚠️ 你已入 **{sect[0] if sect else cv['sect']}**，门派不可更改。\n"
                        f"（想换门派只能重新投胎了）")
        return
    # 支持 /choose 1 或 /choose 蜀山 直接选，避免只能点按钮
    parts = msg.text.split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():
        key = resolve_sect(parts[1])
        if not key:
            await msg.reply(f"❌ 没认出门派「{ui.safe(parts[1].strip())}」。\n\n"
                            f"{ui.divider()}\n{sect_list_text()}\n\n"
                            f"发送 `/choose 序号` 或直接点下面的按钮。",
                            reply_markup=ui.sect_menu())
            return
        await finish_sect(msg, uid, key)
        return
    await msg.reply(
        f"🏯 **择一门而入**\n{ui.divider()}\n"
        + sect_list_text()
        + "\n\n💡 直接点下面的按钮，或发送 `/choose 序号`。\n"
          "⚠️ 门派选定后不可更改，请谨慎。",
        reply_markup=ui.sect_menu(),
    )


@bot.on_callback_query(filters.regex(r"^sect:"))
async def cb_sect(client, cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]
    if key not in S.SECTS:
        await cb.answer("门派不存在", show_alert=True)
        return
    uid = cb.from_user.id
    if db.get_cultivator(uid):
        await cb.answer("你已有门派", show_alert=True)
        return
    name, emo = S.SECTS[key][0], S.SECTS[key][1]
    info = PENDING.setdefault(uid, {})
    info["sect"] = key
    if info.get("daohao"):
        db.create_cultivator(uid, info["daohao"], key)
        PENDING.pop(uid, None)
        await cb.message.edit_text(
            f"{emo} 门派 **{name}** 已定，道号 **{info['daohao']}**。\n\n"
            f"🌌 **角色创建完成！**\n点「🧘 打坐」直接领第一笔灵石。",
            reply_markup=ui.main_menu(),
        )
    else:
        await cb.message.edit_text(
            f"{emo} 你选择了 **{name}**。\n\n"
            f"最后一步：发送 `/name 你的道号` 定下名号。"
        )
    await cb.answer(f"已选 {name}")


# ═══════════════════════════════════════════════════ /name
@bot.on_message(filters.command("name", **CMD))
async def cmd_name(client, msg: Message):
    uid = msg.from_user.id
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.reply("用法：`/name 你的道号`（2~12 个字，不要空格）")
        return
    daohao = ui.safe(parts[1].strip().replace(" ", ""))[:12]
    if len(daohao) < 2:
        await msg.reply("道号至少 2 个字（不要用 ` * _ 等符号）。")
        return
    cv = db.get_cultivator(uid)
    if cv:
        db.update_cultivator(uid, daohao=daohao)
        await msg.reply(f"✅ 道号已改为 **{daohao}**。")
        return
    info = PENDING.setdefault(uid, {})
    info["daohao"] = daohao
    if info.get("sect"):
        sect_key = info["sect"]
        db.create_cultivator(uid, daohao, sect_key)
        PENDING.pop(uid, None)
        await msg.reply(
            f"📜 道号 **{daohao}** 已定。\n"
            f"{S.SECTS[sect_key][1]} 门派 **{S.SECTS[sect_key][0]}**。\n\n"
            f"🌌 角色创建完成！点「🧘 打坐」直接领第一笔灵石。",
            reply_markup=ui.main_menu(),
        )
    else:
        await msg.reply(
            f"📜 道号 **{daohao}** 已记下。\n"
            f"还差一步 —— 点下面的按钮选门派。",
            reply_markup=ui.sect_menu(),
        )


# ═══════════════════════════════════════════════════ /me
@bot.on_message(filters.command(["me", "stat"], **CMD))
async def cmd_me(client, msg: Message):
    cv = db.get_cultivator(msg.from_user.id)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    await msg.reply(ui.cultivator_card(cv, db.get_ls(msg.from_user.id),
                                       db.get_points(msg.from_user.id),
                                       db.rank_of(msg.from_user.id),
                                       is_owner=(int(msg.from_user.id) == int(S.OWNER or 0))),
                    reply_markup=ui.main_menu())


# ═══════════════════════════════════════════════════ 玩法内核
# 下面这些 do_* 是「纯逻辑」：命令和按钮共用同一份实现，
# 因此点按钮 = 直接执行，不需要再手打一遍口令。
def do_sign(uid: int) -> str:
    """打坐：命令 /sign 与按钮「🧘 打坐」共用。

    ★ 2026-10-15 统一签到：开签前先查共享 ch（EmbyBoss 也在用这列），
    今天已在另一边（EmbyBoss）签过 → 拒绝双开，杜绝跨 bot 刷奖。
    """
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    today = dt.date.today()
    if cv["last_sign"] == today:
        return ("🧘 **今日已打过坐**\n" + ui.divider() + "\n"
                f"🔥 已连续 **{cv['sign_streak'] or 0}** 天，明日再来可继续累积。\n"
                "💡 这段时间可以先 `/cultivate` 闭关涨修为。")
    # 跨 bot 互斥：读 EmbyBoss 的当日签到标记（ch 列，同一行记录）
    try:
        from . import unibot
        st = unibot.checkin_state(uid)
        if st.get("today"):
            return ("⚠️ **今日已在 EmbyBoss 签到过了**\n" + ui.divider() +
                    "\n💡 三机器人共享同一笔 V积分钱包，一天只能签一个。")
    except Exception:
        pass
    streak = (cv["sign_streak"] or 0) + 1
    if cv["last_sign"] and (today - cv["last_sign"]).days > 1:
        streak = 1
    base = S.realm_by_level(cv["realm"])[3]
    bonus = int(base * sect_bonus(cv["sect"], "sign") + cv["perm_sign_bonus"] / 100 * base)
    gain = rand(S.G["sign_reward"][0], S.G["sign_reward"][1]) + base + bonus
    gain += min(streak, 10) * S.G["sign_streak_bonus"]
    doubled = cv["sign_buff"] > 0
    if doubled:
        gain *= 2
    cult = S.G["sign_reward"][0] + cv["realm"] * 3
    db.update_cultivator(uid, last_sign=today, sign_streak=streak,
                         sign_buff=0, cultivation=cv["cultivation"] + cult,
                         shield_until=cv["shield_until"])
    total = db.add_ls(uid, gain)                       # 打坐 → 灵石
    db.bump_task(uid, "sign")
    # 反过来把共享 ch 写上今天 —— EmbyBoss 那边今日签到会被它自己拦住（互斥）
    try:
        from . import unibot
        unibot.checkin_mark(uid)
    except Exception:
        pass
    new_ach = check_achievements(uid, dict(cv, sign_streak=streak))
    txt = (f"🧘 {S.SECTS[cv['sect']][1]} **{cv['daohao']}** 盘膝而坐，吐纳周天…\n"
           f"{ui.divider()}\n"
           f"💰 获得灵石｜**+{gain}**\n"
           f"📈 修为提升｜**+{cult}**\n"
           f"🔥 连续打坐｜**{streak}** 天\n"
           f"👛 当前身家｜**{total} {S.CURRENCY}**")
    if doubled:
        txt += "\n🔮 聚灵符生效，本次收益翻倍！"
    if new_ach:
        txt += "\n🏅 解锁成就：" + "、".join(new_ach)
    return txt


def do_train(uid: int) -> str:
    """闭关：命令 /cultivate 与按钮「📖 闭关」共用"""
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    left = cooldown_left(cv["last_train"], S.G["train_cooldown_min"])
    if left > 0:
        return (f"⏳ **灵力未复**\n{ui.divider()}\n"
                f"还需等待 **{fmt_delta(left)}** 才能再次闭关。\n"
                "💡 等待期间可以先去 `/shop` 买丹药，或 `/tasks` 领日常奖励。")
    lo, hi = S.G["train_gain"]
    gain = rand(lo, hi) + cv["realm"] * 4
    new_cult = cv["cultivation"] + gain
    db.update_cultivator(uid, last_train=dt.datetime.now(), cultivation=new_cult)
    # 闭关同样产出灵石（与打坐 / 任务 / 红包 / 发言一致，都是灵石来源）
    ls_gain = rand(lo, hi) + cv["realm"] * 2
    new_ls = db.add_ls(uid, ls_gain)
    db.bump_task(uid, "train")
    nxt = S.realm_by_level(cv["realm"] + 1) if cv["realm"] < S.MAX_REALM else None
    txt = (f"📖 **{cv['daohao']}** 闭关入定，灵力流转…\n"
           f"{ui.divider()}\n"
           f"📈 修为 ｜**+{gain}**（累计 **{new_cult}**）\n"
           f"💰 灵石 ｜**+{ls_gain}**（当前 **{new_ls}**）")
    if nxt and new_cult >= nxt[2]:
        txt += f"\n\n⚡ 修为已足，可冲击 **{nxt[1]}**！点「⚡ 突破」即可。"
    elif nxt:
        txt += (f"\n\n🎯 距 **{nxt[1]}** 还需 "
                f"**{max(nxt[2] - new_cult, 0)}** 修为。")
    return txt


def do_tupo(uid: int) -> str:
    """突破：命令 /breakthrough 与按钮「⚡ 突破」共用"""
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    if cv["realm"] >= S.MAX_REALM:
        return "🌌 你已至仙人境，再无境界可破。"
    nxt = S.realm_by_level(cv["realm"] + 1)
    if cv["cultivation"] < nxt[2]:
        return (f"❌ **修为不足**\n{ui.divider()}\n"
                f"冲击 **{nxt[1]}** 需要 **{nxt[2]}** 修为，"
                f"当前 **{cv['cultivation']}**（还差 {nxt[2] - cv['cultivation']}）。\n"
                "💡 去「📖 闭关」或坊市买大还丹。")
    ok = rand(1, 100) <= (70 + cv["realm"] * 2)
    if ok:
        cur_name = S.realm_by_level(cv["realm"])[1]
        db.update_cultivator(uid, realm=cv["realm"] + 1)
        new_ach = check_achievements(uid, dict(cv, realm=cv["realm"] + 1))
        txt = (f"🎉 **突破成功！**\n{ui.divider()}\n"
               f"{S.SECTS[cv['sect']][1]} **{cv['daohao']}** 冲破桎梏，"
               f"自 **{cur_name}** 晋入 **{nxt[1]}**！\n"
               f"🌠 _{nxt[4]}_\n\n"
               f"🎁 打坐基础收益提升至 **{nxt[3]}** 灵石/天")
        if new_ach:
            txt += "\n🏅 解锁成就：" + "、".join(new_ach)
        nxt2 = S.realm_by_level(cv["realm"] + 2) if cv["realm"] + 1 < S.MAX_REALM else None
        if nxt2:
            txt += f"\n🎯 下一境 **{nxt2[1]}** 需 {nxt2[2]} 修为"
        return txt
    loss = int(nxt[2] * 0.15)
    db.update_cultivator(uid, cultivation=max(cv["cultivation"] - loss, 0))
    return (f"💥 **突破失败！**\n{ui.divider()}\n"
            f"气机紊乱，修为倒退 **-{loss}**（剩 "
            f"**{max(cv['cultivation'] - loss, 0)}**）。\n"
            "💡 修养片刻，多闭关几次再冲，成功率随境界提升。")


def do_tasks(uid: int) -> str:
    """日常：命令 /tasks 与按钮「📜 日常」共用"""
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    st = db.task_state(uid)
    lines = [f"📜 **每日任务**　{dt.date.today()}　{ui.divider()}"]
    total = 0
    for k, (desc, reward) in S.DAILY_TASKS.items():
        cnt, done = st.get(k, (0, 0))
        mark = "✅" if done else "⬜"
        lines.append(f"{mark} {desc}　+{reward} {S.CURRENCY}")
        if done:
            total += reward
    claimed = db.has_claimed(uid, f"task-{dt.date.today()}")
    lines.append(ui.divider())
    if total and not claimed:
        ok = db.record_claim(uid, f"task-{dt.date.today()}", 0, total)
        if ok:
            now = db.add_ls(uid, total)                # 任务 → 灵石
            lines.append(f"🎁 任务奖励已自动发放 **+{total}**，当前 {now} {S.CURRENCY}")
        else:
            lines.append("今日奖励已领取")
    elif claimed:
        lines.append("今日奖励已领取 ✅")
    else:
        lines.append("完成任意任务即可获得奖励")
    return "\n".join(lines)


def do_ach(uid: int) -> str:
    """成就：命令 /achievements 与按钮「🏅 成就」共用"""
    cv = db.get_cultivator(uid)
    if not cv:
        return "🌌 你尚未踏入仙途，发送 `/start`。"
    check_achievements(uid, cv)                        # 顺手补发可能漏掉的成就
    got = db.my_achievements(uid)
    lines = [f"🏅 **成就**　已完成 {len(got)}/{len(S.ACHIEVEMENTS)}　{ui.divider()}"]
    for k, (name, desc) in S.ACHIEVEMENTS.items():
        lines.append(f"{'🏆' if k in got else '🔒'} **{name}** — _{desc}_")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════ 命令（薄封装）
@bot.on_message(filters.command(["sign"], **CMD))
async def cmd_sign(client, msg: Message):
    if not await require_group(client, msg):
        return
    await msg.reply(do_sign(msg.from_user.id), reply_markup=ui.back_menu())


@bot.on_message(filters.command(["cultivate", "train"], **CMD))
async def cmd_cultivate(client, msg: Message):
    if not await require_group(client, msg):
        return
    await msg.reply(do_train(msg.from_user.id), reply_markup=ui.back_menu())


@bot.on_message(filters.command(["breakthrough", "tupo"], **CMD))
async def cmd_breakthrough(client, msg: Message):
    if not await require_group(client, msg):
        return
    await msg.reply(do_tupo(msg.from_user.id), reply_markup=ui.back_menu())


@bot.on_message(filters.command(["tasks", "task"], **CMD))
async def cmd_tasks(client, msg: Message):
    await msg.reply(do_tasks(msg.from_user.id), reply_markup=ui.back_menu())


@bot.on_message(filters.command(["achievements", "ach"], **CMD))
async def cmd_ach(client, msg: Message):
    await msg.reply(do_ach(msg.from_user.id), reply_markup=ui.back_menu())


# ═══════════════════════════════════════════════════ /shop 坊市
@bot.on_message(filters.command(["shop"], **CMD))
async def cmd_shop(client, msg: Message):
    cv = db.get_cultivator(msg.from_user.id)
    if not cv:
        await msg.reply("🌌 你尚未踏入仙途，发送 `/start`。")
        return
    p = db.get_ls(msg.from_user.id)          # 坊市只花灵石
    await msg.reply("\n".join(ui.shop_lines()), reply_markup=ui.shop_menu(p))


@bot.on_callback_query(filters.regex(r"^buy:"))
async def cb_buy(client, cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]
    item = S.SHOP_ITEMS.get(key)
    if not item:
        await cb.answer("物品不存在", show_alert=True)
        return
    uid = cb.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await cb.answer("先创建角色", show_alert=True)
        return
    name, emo, price, desc, kind = item
    if not db.try_spend_ls(uid, price):
        await cb.answer(f"灵石不足，需要 {price}", show_alert=True)
        return
    upd: dict = {}
    extra = ""
    if kind == "cultivation":
        g = rand(40, 80) if key == "dan_xiaohuan" else rand(200, 400)
        upd["cultivation"] = cv["cultivation"] + g
        extra = f"修为 +{g}"
    elif kind == "buff_sign":
        upd["sign_buff"] = 1
        extra = "下次打坐收益翻倍"
    elif kind == "shield":
        upd["shield_until"] = dt.datetime.now() + dt.timedelta(hours=12)
        extra = "12 小时内免疫一次夺取"
    elif kind == "buff_duel":
        upd["duel_buff"] = 1
        extra = "下次斗法必胜"
    elif kind == "perm_sign":
        upd["perm_sign_bonus"] = cv["perm_sign_bonus"] + 10
        extra = "打坐收益永久 +10%"
    if upd:
        db.update_cultivator(uid, **upd)
    db.bump_task(uid, "shop")
    db.log_action("shop", uid, amount=-price, detail=key)
    await cb.answer(f"购买成功！{extra}")
    p = db.get_ls(uid)
    await cb.message.edit_text(
        f"{emo} **{name}** 已入手\n{ui.divider()}\n{extra}\n"
        f"👛 剩余 {p} {S.CURRENCY}", reply_markup=ui.shop_menu(p))


# ═══════════════════════════════════════════════════ 回调：主菜单按钮（一键执行）
_MENU_RE = (r"^(menu|me|sign|train|tupo|shop|tasks|ach|rank|board|now|"
            r"ex|dh|bind|help|close|account)$")


async def _need_group(client, cb) -> bool:
    """按钮版「需在群里」校验：不在群就弹提示，不静默失败"""
    if await in_group(client, cb.from_user.id):
        return True
    await cb.answer(f"⚠️ 需先加入 {S.SERVER_NAME} 官方群才能修仙", show_alert=True)
    return False


@bot.on_callback_query(filters.regex(_MENU_RE))
async def cb_menu(client, cb: CallbackQuery):
    action = cb.data
    uid = cb.from_user.id
    cv = db.get_cultivator(uid)

    if action == "close":
        await cb.message.delete()
        await cb.answer()
        return

    if action == "help":
        await cb.message.edit_text(ui.help_text(uid), reply_markup=ui.back_menu())
        await cb.answer()
        return

    if action == "account":                      # 我的 Emby 账号面板（无需角色也可进）
        from . import account as _account
        await cb.answer()
        await _account.show_panel(client, cb, uid)
        return

    if action == "menu":                     # ★ 返回主菜单 = /start 的双面板
        await cb.message.edit_text(ui.start_hub_text(),
                                   reply_markup=ui.start_hub_menu())
        await cb.answer()
        return

    if action == "me":                       # 我的道途（修仙卡）
        if not cv:
            await cb.answer("先发送 /start 创建角色", show_alert=True)
            return
        await cb.message.edit_text(ui.cultivator_card(cv, db.get_ls(uid),
                                                      db.get_points(uid),
                                                      db.rank_of(uid),
                                                      is_owner=(int(uid) == int(S.OWNER or 0))),
                                   reply_markup=ui.main_menu())
        await cb.answer()
        return

    if not cv:
        await cb.answer("先发送 /start 创建角色", show_alert=True)
        return

    # ── 需要「在主群里」的修行动作：点一下就真的执行 ──
    if action in ("sign", "train", "tupo"):
        if not await _need_group(client, cb):
            return
        text = {"sign": do_sign, "train": do_train, "tupo": do_tupo}[action](uid)
        nxt = {"sign": [("📖 去闭关", "train"), ("🏯 坊市", "shop")],
               "train": [("⚡ 突破", "tupo"), ("🧘 打坐", "sign")],
               "tupo": [("📖 继续闭关", "train"), ("🧘 打坐", "sign")]}[action]
        await cb.answer()
        try:
            await cb.message.edit_text(text, reply_markup=ui.after_menu(nxt))
        except Exception:
            await cb.message.reply(text, reply_markup=ui.after_menu(nxt))
        return

    # ── 无需入群的查询类：点一下直接出结果 ──
    if action == "tasks":
        await cb.answer()
        await cb.message.edit_text(do_tasks(uid), reply_markup=ui.back_menu())
        return
    if action == "ach":
        await cb.answer()
        await cb.message.edit_text(do_ach(uid), reply_markup=ui.back_menu())
        return

    # ── 二级选择界面（需要用户再选一次） ──
    if action == "shop":                      # 坊市：选商品
        await cb.answer()
        await cb.message.edit_text("\n".join(ui.shop_lines()),
                                   reply_markup=ui.shop_menu(db.get_ls(uid)))
        return
    if action == "rank":                      # 天机榜：选境界榜 / 财富榜 / 我的排名
        await cb.answer()
        await cb.message.edit_text(
            "🔮 **天机榜**\n" + ui.divider() + "\n"
            "🌌 境界榜｜谁修得最高\n"
            "💰 财富榜｜谁灵石最多\n"
            "📊 我的排名｜我在群里的位置\n\n"
            "_点一下就出榜，不用再打命令_",
            reply_markup=ui.rank_pick_menu())
        return
    if action == "board":                     # 观看榜：选影片/用户 × 日/周/月
        await cb.answer()
        await cb.message.edit_text(ui.board_pick_text(),
                                   reply_markup=ui.board_pick_menu())
        return
    if action == "ex":                        # 兑换阁：选要换的东西
        from . import redeem                  # 延迟导入，避免循环依赖
        await cb.answer()
        await cb.message.edit_text(
            "\n".join(redeem.panel_lines(uid, cv["realm"])),
            reply_markup=redeem.panel_menu(cv["realm"], db.get_points(uid),
                                           is_owner=(int(uid) == int(S.OWNER or 0))))
        return
    if action == "dh":                        # 货币兑换：选换多少
        from . import redeem
        await cb.answer()
        await cb.message.edit_text(redeem._duihuan_text(uid),
                                   reply_markup=redeem._duihuan_menu(db.get_ls(uid), db.get_points(uid)))
        return
    if action == "bind":                      # 绑定 Emby：给出当前状态与用法
        from . import rank as _rank
        await cb.answer()
        await cb.message.edit_text(await asyncio.to_thread(_rank.bind_text, uid),
                                   reply_markup=ui.back_menu())
        return

    # ── 在线播报：直接发一条带剧照的播报 ──
    if action == "now":
        from . import rank as _rank
        await cb.answer("正在拉取播放状态…")
        text, png = await asyncio.to_thread(_rank._now_data)
        kb = _rank._now_kb()
        if png:
            try:
                await cb.message.reply_photo(io.BytesIO(png), caption=text[:1000],
                                             reply_markup=kb)
                return
            except Exception as e:
                logger.warning(f"播报剧照发送失败，退回文字: {e}")
        try:
            await cb.message.reply(text, reply_markup=kb)
        except Exception as e:
            logger.debug(f"播报发送失败: {e}")
        return
