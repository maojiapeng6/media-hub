# -*- coding: utf-8 -*-
"""115 网盘扫码绑定（照 RT 的能力，绑定结果写进 RT 的 cookie115 字段）。

★ 2026-10-23 用户要求：「数据库那边要设置成可以绑定 115 cookie、可以扫码绑定的那种，像 RT 一样」。
  RT 的 `emby_users.cookie115` 就是干这个的，所以这里只负责「扫码拿 cookie」，
  拿到后调 `rtapi.set_cookie115()` 写回 RT，用户在 RT 面板立刻能看到已绑定。

115 扫码登录标准流程（实测 2026-10-23 可用）：
  1) GET  https://qrcodeapi.115.com/api/1.0/web/1.0/token
         -> {data:{uid,time,sign,qrcode}}
  2) 二维码图片：https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid={uid}
  3) 轮询：GET https://qrcodeapi.115.com/get/status/?uid=&time=&sign=
         state: 0=待扫码 1=已扫码待确认 2=已确认 -1=过期 -2=取消
  4) 确认后 POST https://passportapi.115.com/app/1.0/web/1.0/check/sso
         session 里会拿到 UID/CID/SEID/KID
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

import requests
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import settings as S
from .. import ui
from . import rtapi

logger = logging.getLogger(__name__)
CMD = dict(prefixes=S.PREFIX)
PM = ParseMode.MARKDOWN


def _div() -> str:
    return ui.divider()


def rtapi_enabled() -> bool:
    """RT 通道是否可用（account 面板按钮共用）。"""
    return bool(rtapi.enabled())

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

_QR_TOKEN = "https://qrcodeapi.115.com/api/1.0/web/1.0/token"
_QR_IMG = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid=%s"
_QR_STATUS = "https://qrcodeapi.115.com/get/status/?uid=%s&time=%s&sign=%s"
_SSO = "https://passportapi.115.com/app/1.0/web/1.0/check/sso"

# 正在扫码的会话（防重复开）
_PENDING: dict[int, float] = {}


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Referer": "https://115.com/"})
    return s


def _get_token(s: requests.Session) -> dict | None:
    try:
        r = s.get(_QR_TOKEN, timeout=20)
        d = (r.json() or {}).get("data") or {}
        return d if d.get("uid") else None
    except Exception as e:
        logger.warning(f"115 取二维码失败: {e}")
        return None


def _cookie_str(s: requests.Session) -> str:
    """把 session 里的 115 登录态拼成 cookie 字符串。"""
    c = s.cookies.get_dict()
    keep = ("UID", "CID", "SEID", "KID")
    parts = ["%s=%s" % (k, c[k]) for k in keep if c.get(k)]
    return "; ".join(parts)


async def _poll_and_bind(tg_id: int, chat_id: int, msg_id: int,
                         s: requests.Session, tok: dict) -> None:
    """后台轮询扫码状态，成功后写 RT 并编辑原消息。"""
    uid, t, sign = tok["uid"], tok["time"], tok["sign"]
    deadline = time.time() + 300          # 5 分钟
    last_state = -9
    while time.time() < deadline:
        await asyncio.sleep(2.5)
        try:
            r = await asyncio.to_thread(
                s.get, _QR_STATUS % (uid, t, sign), timeout=20)
            j = r.json() or {}
            st = int(j.get("state", 0))
        except Exception as e:
            logger.debug(f"115 轮询异常: {e}")
            continue
        if st == 1 and last_state != 1:
            last_state = 1
            try:
                await bot.edit_message_text(chat_id, msg_id,
                                            "📱 已扫码，请在手机上点「确认登录」…")
            except Exception:
                pass
        if st == 2:
            # 已确认 → 换 cookie
            try:
                await asyncio.to_thread(
                    s.post, _SSO, timeout=20,
                    headers={"Referer": "https://115.com/"})
            except Exception as e:
                logger.debug(f"115 sso 异常: {e}")
            ck = _cookie_str(s)
            if not ck or "UID=" not in ck:
                try:
                    await bot.edit_message_text(chat_id, msg_id,
                                                "⚠️ 扫码成功但没拿到 cookie，请重试 `/bind115`")
                except Exception:
                    pass
                _PENDING.pop(tg_id, None)
                return
            # 写 RT
            res = await asyncio.to_thread(rtapi.set_cookie115, tg_id, ck)
            uid115 = ck.split("UID=")[1].split("_")[0] if "UID=" in ck else "?"
            if res.get("ok"):
                txt = ("✅ <b>115 已绑定</b>\n" + ui.divider() +
                       "\n· 115 账号 | <code>%s</code>\n"
                       "· 已同步到 RT 面板（我的设置 → 115 网盘）\n"
                       "· 现在可以在 RT 里用自己的 115 转存/离线" % uid115)
            else:
                txt = ("⚠️ 扫码成功但写 RT 失败：%s\n"
                       "cookie 已拿到（UID=%s），请到 RT 面板手动粘贴。" %
                       (res.get("msg"), uid115))
            try:
                await bot.edit_message_text(chat_id, msg_id, txt)
            except Exception:
                pass
            _PENDING.pop(tg_id, None)
            return
        if st in (-1, -2):
            try:
                await bot.edit_message_text(
                    chat_id, msg_id,
                    "⌛ 二维码已%s，请重新发 `/bind115`" %
                    ("过期" if st == -1 else "取消"))
            except Exception:
                pass
            _PENDING.pop(tg_id, None)
            return
    try:
        await bot.edit_message_text(chat_id, msg_id, "⌛ 扫码超时（5 分钟），请重新发 `/bind115`")
    except Exception:
        pass
    _PENDING.pop(tg_id, None)


async def bind_menu(client, tg_id: int):
    """双选项菜单的公共构建：返回 (文本, 键盘)。account 面板与 /bind115 共用。"""
    panel = str(rtapi._cfg().get("panel_url") or "").strip()
    lines = ["🍪 **绑定 115 网盘**", _div()]
    u = await asyncio.to_thread(rtapi.find_user, tg_id)
    if not u:
        lines += ["⚠️ 你在 RT 还没有账号——先开号（🎟 使用兑换码 / 👑 创建账户）再绑。", _div()]
    else:
        cur = "已绑定 ✅" if (u.get("cookie115") or "") else "未绑定"
        pan = u.get("pan115_config_id") or 0
        lines += ["📌 当前状态｜115 Cookie：**%s**　盘位：%s"
                  % (cur, ("#%s" % pan) if pan else "个人自备"), _div()]
    lines += [
        "",
        "**① 📱 App 扫码绑定（推荐）**",
        "　用 **115 手机 App** 扫一扫，确认后 cookie 自动写入 RT，",
        "　RT 面板 / Boss / Game 三端即时同步，全程不用粘贴任何东西。",
        "",
        "**② 🌐 打开 RT 面板自助**",
        "　想用其它客户端扫码（TV / iPad / 电脑网页版）或手动粘贴 cookie，",
        "　点下面按钮打开 RT 面板，用 **Emby 同一套账号密码** 登录：",
        "　「我的设置 → 115 网盘」里扫码/粘贴，同样自动保存。"
        if panel else "　（RT 面板地址未配置，暂只能走扫码）",
    ]
    rows = [[B("📱 App 扫码绑定（自动同步RT）", "b15:qr")]]
    if panel:
        rows.append([B("🌐 打开 RT 面板", url=panel)])
    rows.append([B("↩️ 返回面板", "account")])
    return "\n".join(lines), KM(rows)


@bot.on_message(filters.command(["bind115", "绑定115"], **CMD))
async def cmd_bind115(client, msg: Message):
    """绑定 115：双选项菜单（① App 扫码自动同步 RT ② 打开 RT 面板自助）。"""
    if not rtapi.enabled():
        await msg.reply("⚠️ 未配置 RT 接口，无法绑定。", parse_mode=PM)
        return
    text, kb = await bind_menu(client, msg.from_user.id)
    await msg.reply(text, reply_markup=kb, parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^b15:qr$"))
async def cb_bind115_qr(client, cb):
    """选项①：web 通道二维码（实测可用端点；115 App 可扫，cookie 写 RT）。"""
    tg_id = cb.from_user.id
    if _PENDING.get(tg_id, 0) > time.time():
        await cb.answer("已有一个二维码在等扫码，先完成或等它过期", show_alert=True)
        return
    u = await asyncio.to_thread(rtapi.find_user, tg_id)
    if not u:
        await cb.answer("你在 RT 还没有账号，先开号再绑 115", show_alert=True)
        return
    await cb.answer("正在生成二维码…")
    s = _new_session()
    tok = await asyncio.to_thread(_get_token, s)
    if not tok:
        await cb.message.reply("⚠️ 取 115 二维码失败，稍后再试。", parse_mode=PM)
        return
    try:
        img = await asyncio.to_thread(
            lambda: s.get(_QR_IMG % tok["uid"], timeout=25).content)
    except Exception as e:
        await cb.message.reply("⚠️ 下载二维码失败：%s" % str(e)[:60], parse_mode=PM)
        return
    if not img or len(img) < 500:
        await cb.message.reply("⚠️ 二维码图片异常，稍后再试。", parse_mode=PM)
        return
    cap = ("🔐 <b>115 扫码绑定</b>\n" + ui.divider() +
           "\n1️⃣ 打开手机 <b>115 App</b>\n"
           "2️⃣ 右上角「扫一扫」扫这张码\n"
           "3️⃣ 手机上点「确认登录」\n\n"
           "✅ 确认后自动写入 RT（三端同步），无需任何粘贴。\n"
           "⏱ 二维码 5 分钟内有效；想用 TV/iPad 等其它端扫码，"
           "请回 /bind115 选「🌐 打开 RT 面板」自助操作。")
    sent = await cb.message.reply_photo(io.BytesIO(img), caption=cap, parse_mode=PM)
    _PENDING[tg_id] = time.time() + 300
    asyncio.create_task(_poll_and_bind(tg_id, sent.chat.id, sent.id, s, tok))
