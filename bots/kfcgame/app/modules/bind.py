# -*- coding: utf-8 -*-
"""Telegram 账号绑定与验证系统（Emby 账号 + 密码核验）

需求（2026-10-12）：
  1. 所有用户必须先在私聊绑定 Telegram 账号，未绑定则拦截并拒绝任何命令；
  2. 使用 Emby 功能前须绑定 Emby 账号：提交用户名 + 密码完成身份验证，
     仅验证通过才允许绑定；敏感凭据加密存储，严禁明文；
  3. 状态机：未绑定 / 待验证 / 验证失败 / 已绑定 / 已禁用 / 已解绑，
     各类失败场景有明确提示、可重试、可恢复。

设计：
  · /bind 私聊发起 → 输入 Emby 用户名 → 输入密码 →
    Emby AuthenticateByName 验证 → 通过才落库（密码 AES 加密，见 db.bind_ok）
  · 群闸门（group=-1）：任何消息（命令/文本）先过闸，
    未绑定（且不在白名单/绑定流程中）→ 拒绝并 StopPropagation
  · owner 豁免闸门（防止群主自己被锁在外面）；数据库异常时放行并记日志
  · 密码消息在读到时即删除（私聊里 bot 可删），不在聊天里留明文
"""
from __future__ import annotations

import asyncio
import re
import time

import requests
from loguru import logger
from pyrogram import ContinuePropagation, StopPropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import db
from .. import settings as S
from .. import ui
from . import perm

PM = ParseMode.HTML
CMD = dict(prefixes=S.PREFIX)

# 绑定流程中的中间态（进程内，重启后需重新 /bind，可接受）
_WAIT: dict[int, dict] = {}          # tg -> {"step": "user"|"pass", "user": name, "ts": ts}
_FLOW_TTL = 600                       # 流程 10 分钟有效
_MAX_FAIL = 5                         # 连续失败 5 次 → 冷却
_FAIL_CD = 1800                       # 冷却 30 分钟

# 闸门白名单：这些命令未绑定也能用（否则没法开始绑定）
_ALLOW = {"start", "bind", "help", "unbind", "bindstatus", "claim",
          # ★ 2026-10-13：规则咨询**免绑定**（用户明确要求降低门槛）。
          #   发电规则是面向群友的服务说明，保号规则也是公开价格/细则，
          #   未绑定的人问一句就被「请先绑定」挡回来，等于把咨询者赶走。
          "power", "fadian", "发电", "ka", "保号", "karule", "保号规则",
          # ★ 2026-11-04：开号/兑换自助链路免绑定——新人本来就没有 Emby
          #   账号可绑，要求先绑定=死锁（用户实测投诉）。
          "uscode", "redeem", "兑码", "exchange", "ex", "兑换", "duihuan",
          "dh", "换积分", "兑换积分", "mycodes", "duanwei", "段位说明",
          "货币说明", "兑换说明", "account", "账号", "我的账号"}

# ★ 免绑定的**纯文本**触发词（无斜杠命令走不到 _ALLOW 那条判断）
#   —— 与 power.py / keepalive.py 的触发词保持一致。
_FREE_TEXT = {
    "发电", "发电规则", "发电帮助", "发电价格", "发电说明", "power", "fadian",
    "保号规则", "保号帮助", "保号价格", "pt保号规则", "ka规则", "保号说明",
}

_STATE_ZH = {
    "none": "未绑定", "pending": "待验证", "failed": "验证失败",
    "bound": "已绑定", "disabled": "已禁用", "unbound": "已解绑",
}


# ── Emby 验证 ──────────────────────────────────────────────────
def emby_auth(name: str, pw: str) -> tuple[bool, str, str]:
    """Emby 用户名+密码验证。返回 (是否通过, 提示, Emby用户ID)"""
    e = getattr(S, "EMBY", None) or {}
    url, key = e.get("url"), e.get("api_key")
    if not url or not key:
        return False, "服务器未配置 Emby，暂时无法验证", ""
    try:
        r = requests.post(
            url.rstrip("/") + "/emby/Users/AuthenticateByName",
            params={"api_key": key, "format": "json"},
            json={"Username": name, "Pw": pw}, timeout=20)
        if r.status_code == 200:
            j = r.json() or {}
            u = j.get("User") or {}
            return True, "验证通过", str(u.get("Id") or "")
        if r.status_code in (401, 400):
            return False, "用户名或密码不正确（请确认是 Emby 登录账号，不是邮箱）", ""
        return False, f"Emby 返回 HTTP {r.status_code}", ""
    except Exception as ex:
        logger.warning(f"Emby 验证异常: {ex}")
        return False, "验证服务暂时不可用，请稍后再试", ""


# ── 命令 ───────────────────────────────────────────────────────
@bot.on_message(filters.command(["bind", "绑定"], **CMD))
async def cmd_bind(client, msg: Message):
    u = msg.from_user
    if not u:
        return
    if msg.chat.type.name not in ("PRIVATE", "PRIVATE_CHAT", "private"):
        await msg.reply("🔐 绑定涉及密码，请**私聊我**发 `/bind`", parse_mode=PM)
        return
    st = await asyncio.to_thread(db.bind_state, u.id)
    if st and st["status"] == "bound":
        await msg.reply(
            f"✅ 你已绑定 Emby 账号 **{ui.safe(st['emby_name'])}**\n"
            f"{ui.divider()}\n解绑后可用 `/unbind` 重新绑定。",
            parse_mode=PM)
        return
    if st and st["status"] == "disabled":
        await msg.reply("⛔ 你的绑定已被禁用，请联系群主。", parse_mode=PM)
        return
    if st and st["fail_count"] >= _MAX_FAIL and time.time() - st["last_fail_ts"] < _FAIL_CD:
        left = int((_FAIL_CD - (time.time() - st["last_fail_ts"])) / 60) + 1
        await msg.reply(f"⏳ 失败次数过多，请 **{left} 分钟**后再试。", parse_mode=PM)
        return
    await asyncio.to_thread(db.bind_touch, u.id, u.first_name or "",
                            u.username or "")          # 落 pending 状态
    _WAIT[u.id] = {"step": "user", "ts": time.time()}
    await msg.reply(
        "🔐 <b>绑定 Emby 账号</b>\n" + ui.divider() + "\n"
        "请直接发送你的 <b>Emby 用户名</b>（不是邮箱）：\n"
        f"{ui.divider()}\n"
        "随时发「取消」中断。下一步会要密码，密码消息我会在读到时删掉。",
        parse_mode=PM)


@bot.on_message(filters.command(["bindstatus", "绑定状态"], **CMD))
async def cmd_bindstatus(client, msg: Message):
    u = msg.from_user
    if not u:
        return
    st = await asyncio.to_thread(db.bind_state, u.id)
    if not st or st["status"] == "none":
        await msg.reply(f"🔐 状态：**未绑定**\n私聊我发 `/bind` 完成绑定。",
                        parse_mode=PM)
        return
    zh = _STATE_ZH.get(st["status"], st["status"])
    extra = ""
    if st["status"] == "bound" and st.get("emby_name"):
        extra = f"\n📺 Emby 账号：**{ui.safe(st['emby_name'])}**"
    if st["status"] == "failed":
        extra = f"\n❌ 已失败 {st['fail_count']} 次，重新发 `/bind` 可再试"
    await msg.reply(f"🔐 绑定状态：**{zh}**{extra}", parse_mode=PM)


@bot.on_message(filters.command(["unbind", "解绑"], **CMD))
async def cmd_unbind(client, msg: Message):
    u = msg.from_user
    if not u:
        return
    await asyncio.to_thread(db.bind_unbind, u.id)
    _WAIT.pop(u.id, None)
    await msg.reply("🔓 已解绑。重新使用请私聊我发 `/bind`。", parse_mode=PM)


@bot.on_message(filters.command(["bindmgr", "绑定管理"], **CMD))
@perm.gate("bindctl")
async def cmd_bindmgr(client, msg: Message):
    """管理员：绑定名单总览 + 禁用/解禁/重置单个用户"""
    parts = (msg.text or "").split()
    counts = await asyncio.to_thread(db.bind_counts)
    head = ("🛠 <b>绑定管理</b>\n" + ui.divider() + "\n"
            f"✅ 已绑定 <b>{counts.get('bound', 0)}</b> · "
            f"⏳ 待验证 {counts.get('pending', 0)} · "
            f"❌ 失败 {counts.get('failed', 0)} · "
            f"⛔ 禁用 {counts.get('disabled', 0)}\n" + ui.divider() + "\n")
    if len(parts) >= 3 and parts[1] in ("disable", "enable", "reset"):
        act = parts[1]
        try:
            tg = int(parts[2])
        except ValueError:
            await msg.reply("用法：`/bindmgr disable|enable|reset <TGID>`",
                            parse_mode=PM)
            return
        if act == "disable":
            await asyncio.to_thread(db.bind_disable, tg, True)
            await msg.reply(f"⛔ 已禁用 {tg} 的绑定")
        elif act == "enable":
            await asyncio.to_thread(db.bind_disable, tg, False)
            await msg.reply(f"▶️ 已恢复 {tg} 的绑定")
        else:
            await asyncio.to_thread(db.bind_unbind, tg)
            await msg.reply(f"🔓 已重置 {tg}（需重新绑定）")
        return
    await msg.reply(head + "用法：`/bindmgr disable|enable|reset <TGID>`",
                    parse_mode=PM)


# ── 私聊认领（身份核验后才能关联榜单名字） ──────────────────────
def _claim_free() -> list[str]:
    """近 30 天榜上还没被任何人关联的 Emby 名（一人限认一个）"""
    from . import rank
    try:
        users = rank.user_stats(30) or []
    except Exception:
        users = []
    try:
        taken = set(db.bind_map().keys())
    except Exception:
        taken = set()
    return [u["name"] for u in users
            if u.get("name") and u["name"] not in taken][:12]


@bot.on_message(filters.command(["claim", "认领"], **CMD))
async def cmd_claim(client, msg: Message):
    u = msg.from_user
    if not u:
        return
    # ★ 2026-11-04 修复「抽风」：认领要在群里发密码提示非常危险（密码消息会
    #   公开在群）。一律引导私聊。
    if msg.chat.type.name not in ("PRIVATE", "PRIVATE_CHAT", "private"):
        await msg.reply("🙋 认领需核验 Emby 密码，请**私聊我**发 /claim", parse_mode=PM)
        return
    cur = await asyncio.to_thread(db.my_bind, u.id)
    if cur:
        await msg.reply(f"✅ 你已经关联了 **{ui.safe(cur)}**（一人限一个）。",
                        parse_mode=PM)
        return
    free = await asyncio.to_thread(_claim_free)
    if not free:
        await msg.reply("😥 暂时没有可认领的名字（要么都已关联，要么还没看片记录）。",
                        parse_mode=PM)
        return
    rows, line = [], []
    for i, n in enumerate(free):
        line.append(B(n[:24], f"bd:cl:{i}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    await msg.reply(
        "🙋 <b>认领我的名字</b>\n" + ui.divider() + "\n"
        "点一下<b>你自己那个名字</b>，然后输一次该 Emby 账号的密码完成核验。\n"
        "核验通过后，榜单上你的名字就会变成可点的 TG 主页链接。",
        parse_mode=PM, reply_markup=KM(rows))


@bot.on_callback_query(filters.regex(r"^bd:cl:(\d+)$"))
async def cb_claim_pick(client, cb):
    u = cb.from_user
    free = await asyncio.to_thread(_claim_free)
    idx = int(cb.data.split(":")[2])
    if idx >= len(free):
        await cb.answer("列表已过期，重新发 /claim", show_alert=True)
        return
    name = free[idx]
    if await asyncio.to_thread(db.my_bind, u.id):
        await cb.answer("你已经关联过了（一人限一个）", show_alert=True)
        return
    _WAIT[u.id] = {"step": "claim_pw", "claim_name": name, "ts": time.time()}
    await cb.answer()
    await cb.message.reply(
        f"🔐 认领「<b>{ui.esc(name)}</b>」需要身份核验\n"
        "请直接发送该 Emby 账号的**密码**：\n"
        f"{ui.divider()}\n"
        "验证通过立即关联；认领他人身份会被拒绝。密码消息读完即删。",
        parse_mode=PM)


# ── 绑定流程（文本交互） ────────────────────────────────────────
# ★ 2026-11-04 修复「抽风」：只处理**私聊**文本。原来无 private 限定，群里
#   未绑定/流程中的用户随便发句话都会被当成 Emby 用户名/密码收集并在群里
#   公开追问（甚至把密码提示发进群），机器人看起来就像引用别人消息乱答。
@bot.on_message(filters.text & filters.private, group=0)
async def bind_flow(client, msg: Message):
    """用户名 / 密码两步收集（私聊）。不是本流程的消息直接放过。"""
    u = msg.from_user
    if not u or u.is_bot or not msg.text:
        raise ContinuePropagation
    w = _WAIT.get(u.id)
    if not w:
        raise ContinuePropagation
    if time.time() - w["ts"] > _FLOW_TTL:
        _WAIT.pop(u.id, None)
        raise ContinuePropagation
    txt = msg.text.strip()
    if txt in ("取消", "cancel", "算了"):
        _WAIT.pop(u.id, None)
        await msg.reply("已取消绑定。想绑定时私聊我发 `/bind`。", parse_mode=PM)
        return

    if w["step"] == "user":
        if len(txt) > 64 or not txt:
            await msg.reply("用户名看起来不太对，请重新发送（或发「取消」）。",
                            parse_mode=PM)
            return
        w["user"] = txt
        w["step"] = "pass"
        w["ts"] = time.time()
        await msg.reply(
            f"🔐 用户名：<b>{ui.esc(txt)}</b>\n"
            "现在请发送该 Emby 账号的<b>密码</b>：\n"
            f"{ui.divider()}\n"
            "密码只用于这一次验证，读到我就会把这条消息删掉，库里只存加密后的密文。",
            parse_mode=PM)
        return

    if w["step"] == "pass":
        pw = txt
        name = w["user"]
        # 输入框里立刻删掉密码消息，避免明文留在聊天记录
        try:
            await msg.delete()
        except Exception:
            pass
        wait = await msg.reply("⏳ 正在向 Emby 验证…", parse_mode=PM)
        ok, tip, uid = await asyncio.to_thread(emby_auth, name, pw)
        if ok:
            await asyncio.to_thread(db.bind_ok, u.id, name, uid, pw)
            _WAIT.pop(u.id, None)
            try:
                await wait.edit_text(
                    f"✅ <b>绑定成功</b>\n{ui.divider()}\n"
                    f"📺 Emby 账号：<b>{ui.esc(name)}</b>\n"
                    f"{ui.divider()}\n"
                    "现在可以使用全部功能了。解绑：`/unbind`。",
                    parse_mode=PM)
            except Exception:
                pass
            # 让榜单映射立刻重算
            try:
                from . import rank as _rk
                _rk._TGMAP["ts"] = 0.0
                _rk._CACHE.clear()
            except Exception:
                pass
        else:
            fc = await asyncio.to_thread(db.bind_fail, u.id)
            if fc >= _MAX_FAIL:
                _WAIT.pop(u.id, None)
                try:
                    await wait.edit_text(
                        f"❌ <b>验证失败</b>：{ui.esc(tip)}\n"
                        f"失败已达 {fc} 次，请 30 分钟后再 `/bind`。",
                        parse_mode=PM)
                except Exception:
                    pass
            else:
                w["step"] = "user"
                w["ts"] = time.time()
                try:
                    await wait.edit_text(
                        f"❌ <b>验证失败</b>：{ui.esc(tip)}\n"
                        f"已失败 {fc} 次（{_MAX_FAIL} 次后冷却 30 分钟）。\n"
                        "重新发送 Emby 用户名，或发「取消」。",
                        parse_mode=PM)
                except Exception:
                    pass
        return

    if w["step"] == "claim_pw":
        # 认领核验：密码对上这个名字才允许关联（严格禁止认领他人身份）
        pw, name = txt, (w.get("claim_name") or "")
        try:
            await msg.delete()
        except Exception:
            pass
        wait = await msg.reply("⏳ 正在核验身份…", parse_mode=PM)
        ok, tip, uid = await asyncio.to_thread(emby_auth, name, pw)
        if ok:
            await asyncio.to_thread(db.bind_touch, u.id, u.first_name or "",
                                    u.username or "")
            await asyncio.to_thread(db.bind_ok, u.id, name, uid, pw)
            _WAIT.pop(u.id, None)
            try:
                from . import rank as _rk
                _rk._TGMAP["ts"] = 0.0
                _rk._CACHE.clear()
            except Exception:
                pass
            try:
                await wait.edit_text(
                    f"✅ <b>认领成功</b>\n{ui.divider()}\n"
                    f"📺 <b>{ui.esc(name)}</b> 已关联到你的 TG 账号，"
                    "榜单上这个名字现在可以点开你的主页。", parse_mode=PM)
            except Exception:
                pass
        else:
            _WAIT.pop(u.id, None)
            try:
                await wait.edit_text(
                    f"❌ <b>核验失败</b>：{ui.esc(tip)}\n"
                    "密码和这个名字对不上，不能认领他人身份。\n"
                    "重新发 `/claim` 再试。", parse_mode=PM)
            except Exception:
                pass
        return
    raise ContinuePropagation


# ── 全局闸门：未绑定 → 拦截一切命令 ────────────────────────────
_deny_cd: dict[int, float] = {}          # 拒绝提示的防刷屏冷却


@bot.on_message(filters.text, group=-1)
async def bind_gate(client, msg: Message):
    """未绑定用户：拦截**命令**（白名单/owner/绑定流程中除外）。

    ★ 2026-11-04 修复「抽风」：原实现拦截**所有文本**——群里未绑定用户随便
      聊句话都被引用回「请先绑定」，机器人看起来像乱插话。现在只拦命令
      （以 / ! . ，。 等前缀开头），群内闲聊一律放行。
    """
    try:
        u = msg.from_user
        if not u or u.is_bot:
            raise ContinuePropagation
        if int(S.OWNER or 0) and u.id == int(S.OWNER):
            raise ContinuePropagation          # 群主豁免，防止把自己锁死
        text = (msg.text or "").strip()
        if not text:
            raise ContinuePropagation
        first = text.lower().split()[0].split("@")[0].lstrip("/!")
        # ★ 非命令的纯聊天：不拦截（尤其群聊，避免引用搭话）。命令只会以
        #   S.PREFIX（"/"、"!"）开头。
        if text[:1] not in S.PREFIX:
            raise ContinuePropagation
        if first in _ALLOW:
            raise ContinuePropagation           # 允许发起绑定自身
        # ★ 规则咨询类纯文本（发电 / 保号规则 …）一律免绑定放行
        if re.sub(r"\s+", "", text).rstrip("!.。~～,，?？") in _FREE_TEXT:
            raise ContinuePropagation
        # ★ 2026-11-04 兑换码放行：注册/续期码本身就是凭证，不该要求先绑定
        #   （否则新人想开号反被「请先绑定」卡死，逻辑死锁）
        if re.search(r"(KFC|SAKURA)[-\s]*\d", text.upper()):
            raise ContinuePropagation
        if u.id in _WAIT:
            raise ContinuePropagation           # 绑定流程中，交给 bind_flow
        bound = await asyncio.to_thread(db.is_bound, u.id)
        if bound:
            raise ContinuePropagation
        # 未绑定 → 拒绝（带 10 分钟冷却，避免群里被同一个人刷屏）
        now = time.time()
        if now - _deny_cd.get(u.id, 0) < 600:
            raise StopPropagation
        _deny_cd[u.id] = now
        try:
            await msg.reply(
                "🔐 <b>请先绑定再使用</b>\n" + ui.divider() + "\n"
                "为核对身份（防小号/防冒领），使用任何功能前需先绑定。\n"
                "请**私聊我**发 `/bind`，按提示完成 Emby 账号验证。\n"
                f"{ui.divider()}\n查状态 `/bindstatus` · 解绑 `/unbind`",
                parse_mode=PM)
        except Exception:
            pass
        raise StopPropagation
    except (ContinuePropagation, StopPropagation):
        raise
    except Exception as ex:
        # 闸门自身异常时放行（绝不让闸门把整个 bot 锁死），记日志待查
        logger.warning(f"绑定闸门异常，放行: {ex}")
        raise ContinuePropagation
