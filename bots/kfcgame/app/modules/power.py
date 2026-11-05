# -*- coding: utf-8 -*-
"""⚡ 发电 + 📺 Emby 服保号规则（群友公开，Emby 范畴）

★ 2026-10-14（用户拍板）分类口径：
  · **发电** = 只有 Emby 的发电（115 会员注册码/续期码 + V 积分兑换）。
  · **保号 / 保号规则**（群友发）= **我 Emby 服的保号规则**（账号活跃/防回收）。
  · **PT 站点保号** = 群主/授权管理员专属（modules/keepalive.py），
    群友的任何入口都**不再展示 PT 内容**。

无斜杠命令：发电 / 发电规则 / 发电帮助（群聊私聊均响应，公开）
斜杠别名：/power /fadian

★ 规则咨询**取消强制绑定**：未绑定 Emby 的群友问「发电 / 保号」也能直接
  拿到完整规则（bind.py 的闸门已把规则咨询类文本列入白名单）。
"""
from __future__ import annotations

import re

from loguru import logger
from pyrogram import ContinuePropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import settings as S
from .. import ui

PM = ParseMode.HTML
CMD = dict(prefixes=S.PREFIX)

# ── 发电规则文案（用户指定口径，原文照录） ──────────────────────
_PRICE = [
    "· 括号标注需自备 115 会员的注册码及续期码 —— 售价 ¥5",
    "· 无需自备 115 会员的注册码及续期码 —— 售价 ¥10/月",
]

# 触发词（bind.py 的绑定闸门从这里导入，改一处即可）
POWER_WORDS = ("发电", "发电规则", "发电帮助", "发电价格", "发电说明",
               "power", "fadian")
_POWER_WORDS = set(POWER_WORDS)


def power_text() -> str:
    """发电规则完整说明（★ Emby 范畴：115 会员码 + V 积分，公开免绑定）

    2026-10-14（用户拍板）：发电 = **只有 Emby 的发电**。
    移除原「6 大 PT 站账号养护」与「看 PT 保号规则」入口——
    PT 保号是群主专属，群友的发电卡片不再出现任何 PT 内容。
    """
    lines = [
        "⚡ <b>发电规则 · 价格 · 完整说明</b>", ui.divider(),
        "发电 = 本群 <b>Emby</b> 增值服务，包含两块：",
        "　① 115 会员注册码 / 续期码办理",
        "　② 群游戏赚 V 积分，直接兑换上面这些码", ui.divider(),
        "<b>💰 价格</b>",
    ]
    lines += _PRICE
    lines += [
        ui.divider(),
        "<b>💎 V积分怎么赚</b>（唯一权威口径）",
        f"　① <b>签到</b>｜Boss 机器人「🎯 签到」每天 <b>{S.CHECKIN_IV[0]}~{S.CHECKIN_IV[1]}</b> 分"
        "（与 Game 打坐互斥，一天一次）",
        f"　② <b>观影自动结算</b>｜每天 23:30 由 Game 统一核算（全群唯一结算源）：满 {S.WATCH_MIN} 分钟起算，"
        f"<b>每 {S.WATCH_PER} 分钟 = 1 分，上不封顶</b>",
        f"　③ <b>灵石兑换</b>｜{S.LS_PER_V} 灵石 = 1 分（/duihuan 随时换）",
        "　④ <b>群主派送</b>｜群主 /pay 直转（活动奖励等）",
        ui.divider(),
        "<b>🎮 怎么用更划算</b>",
        "　· 群里发言、打坐 /sign、闭关 /cultivate、每日任务 /tasks 都能攒灵石",
        "　· /duihuan 把灵石按比例换成 V积分",
        "　· /exchange 兑换阁里用 V积分换注册码 / 续期码",
        "　· 境界越高折扣越大（/duanwei 看完整折扣表）",
        ui.divider(),
        "<b>📺 Emby 服保号</b>",
        "　· 想让 Emby 账号保持活跃、不被回收 → 发「<b>保号</b>」看规则",
        "　· /bind 绑定 Emby 账号后才能参与保号认定与兑换",
        ui.divider(),
        "🛒 购买 / 咨询：私聊群主或管理员",
    ]
    return ui.clip_html("\n".join(lines), limit=3800)


# ── Emby 服保号规则（群友发「保号」看到的就是这份，纯 Emby、无 PT）───
def emby_keepalive_text() -> str:
    """Emby 账号活跃 / 防回收规则（群友公开可查，免绑定）。

    ★ 2026-10-14：群友群里发「保号」想了解的是**我 Emby 服的保号规则**，
      与 PT 站点保号无关——PT 保号是群主专属，不在此展示。
    ⚠️ 下方天数为默认口径，群主可按自己 Emby 服实际策略调整。
    """
    lines = [
        "📺 <b>Emby 服保号规则</b>", ui.divider(),
        "「保号」= 让你的 Emby 账号保持活跃、不被回收。规则如下：", ui.divider(),
        "🟢 <b>怎么算活跃</b>",
        "　· 每 <b>30 天</b>内至少观看 / 播放 1 次即算活跃，账号安全",
        "　· 在群里发言、/sign 打坐、每日任务都记群活跃度", ui.divider(),
        "🟡 <b>回收红线</b>",
        "　· 连续 <b>30 天</b>无任何观看记录 → 账号被回收，名额释放给候补",
        "　· 回收后 <b>7 天</b>内可私聊群主申诉恢复，逾期作废", ui.divider(),
        "🔴 <b>设备与共享</b>",
        "　· 同一账号同时在线设备 ≤ <b>2</b>，超出会被踢下线并警告",
        "　· 禁止共享 / 转借账号，违规永久拉黑", ui.divider(),
        "🔗 <b>先绑定再保号</b>",
        "　· 私聊我发 /bind 绑定 Emby 账号（输一次账号密码核验）",
        "　· 绑定后才能参与榜单、兑换、保号认定", ui.divider(),
        "💡 保持活跃最简单的方法：每周来点开一部片看看就行。",
        "⚡ 想看发电 / 115 会员码：发「<b>发电</b>」",
    ]
    return ui.clip_html("\n".join(lines), limit=3800)


def emby_ka_kb() -> KM:
    return KM([[B("⚡ 看发电规则", callback_data="pwr:show")],
               [B("🔐 去绑定 /bind", callback_data="pwr:bind"),
                B("❌ 关闭", callback_data="pwr:close")]])


def power_kb() -> KM:
    return KM([[B("🎟 去兑换阁 /exchange", callback_data="pwr:go")],
               [B("📺 Emby 服保号规则", callback_data="pwr:embyka")],
               [B("❌ 知道了", callback_data="pwr:close")]])


@bot.on_message(filters.text, group=0)
async def power_text_trigger(client, msg: Message):
    """无斜杠触发：发电 / 发电规则 / 发电帮助"""
    try:
        if not msg.text:
            raise ContinuePropagation
        u = msg.from_user
        if not u or u.is_bot:
            raise ContinuePropagation
        t = re.sub(r"\s+", "", msg.text.strip()).rstrip("!.。~～,，?？")
        if t in _POWER_WORDS:
            await msg.reply(power_text(), parse_mode=PM,
                            reply_markup=power_kb(),
                            disable_web_page_preview=True)
            return
        raise ContinuePropagation
    except ContinuePropagation:
        raise
    except Exception:
        raise ContinuePropagation


@bot.on_message(filters.command(["power", "fadian", "发电"], **CMD))
async def power_cmd(client, msg: Message):
    await msg.reply(power_text(), parse_mode=PM, reply_markup=power_kb(),
                    disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^pwr:(go|close|show|ka|embyka|bind)$"))
async def cb_power(client, cb):
    act = cb.data.split(":")[1]
    if act == "close":
        try:
            await cb.message.delete()
        except Exception:
            await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer("已关闭")
        return
    if act == "show":
        try:
            await cb.message.edit_text(power_text(), parse_mode=PM,
                                       reply_markup=power_kb(),
                                       disable_web_page_preview=True)
        except Exception:
            pass
        await cb.answer()
        return
    if act == "embyka":
        try:
            await cb.message.edit_text(emby_keepalive_text(), parse_mode=PM,
                                       reply_markup=emby_ka_kb(),
                                       disable_web_page_preview=True)
        except Exception:
            pass
        await cb.answer()
        return
    if act == "bind":
        await cb.answer("请私聊我发 /bind，按提示完成 Emby 账号绑定",
                        show_alert=True)
        return
    if act == "ka":
        # PT 保号规则：群主 / 授权管理员专属（群友不再从发电卡进入 PT）
        from . import perm
        if not perm.can(cb.from_user.id if cb.from_user else 0, "ka"):
            await cb.answer("⚡ PT 保号是群主专属功能", show_alert=True)
            return
        try:
            from .keepalive import send_rules
            await send_rules(cb, public=False, edit=True)
        except Exception as e:
            logger.debug(f"推送保号规则失败: {e}")
            await cb.answer("规则加载失败", show_alert=True)
            return
        await cb.answer()
        return
    await cb.answer("在私聊里发 /exchange 打开兑换阁", show_alert=True)
