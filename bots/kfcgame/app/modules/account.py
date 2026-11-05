"""account —— Game Bot 的 EMBY 面板（照搬 Boss 用户面板 + 增强）。

· /start →「📺 EMBY 面板」；所有子页可「↩️ 返回面板」形成层级导航
· 无账号：👑 创建账户（消耗天数池）/ 🎟 使用兑换码 / 🔁 绑定TG(换绑) / 🔗 绑定115
· 有账号：状态 / 🎟 使用兑换码 / 👁 查看密码 / 🌐 服务器线路 / 📀 媒体库 /
          📊 服务器统计 / 🎬 媒体库显隐 / 💠 我的设备 / 💖 我的收藏 /
          🏪 兑换商店 / ✏️ 改用户名 / 🔑 重置密码(验安全码→自设新密码) /
          🗑 删除账号(验安全码) / 🔗 绑定115
· 开号成功卡片附服务器播放线路
· 删除对 Emby 404 容错并清理本地记录，永不卡死
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import time

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot, db, settings as S, ui
from . import unibot

CMD = dict(prefixes=S.PREFIX)
PM = ParseMode.MARKDOWN      # ★ 全站统一 Markdown（client 默认即 MD），
                             #   旧值 HTML 但文案写 ** → 字面星号裸露（用户投诉）

# 交互状态机：uid -> {"mode": ..., "step": ..., "ts": ...}
_FLOW: dict[int, dict] = {}

# 兑换码：KFC-30-Register-XXX / 兼容旧下划线与连号写法
_CODE_RE = re.compile(r"(?:KFC|SAKURA)[-_]?\d+[-_]?[A-Za-z]+[-_]?[A-Za-z0-9]+")

_TYPE_CN = {"Movie": "电影", "Series": "剧集", "Episode": "剧集",
            "Person": "演员", "Photo": "图片"}
_PAGE = 10
_BAD_NAME = set("<>@#&/\\`'\"")

_DEL_CONFIRM = ("🗑 **确认删除账号？**\n{div}\n"
                "删除后 Emby 账号与本地记录会清空，**不可恢复**！\n"
                "（若 Emby 服务端已无此账号，将直接清理本地悬空记录）")


def _div() -> str:
    return ui.divider()


def _acc(uid: int) -> dict:
    return unibot.emby_account_of(uid) or {}


def _acc_has(acc) -> bool:
    return bool(acc and acc.get("embyid"))


def _lines_md() -> str:
    """服务器线路 Markdown 块（开号卡片 / 🌐线路页共用）"""
    ls = unibot.server_lines()
    if not ls:
        return "　· 暂无线路配置，请咨询群主"
    return "\n".join(
        f"　· **{ui.safe(l.get('name') or '线路')}**｜`{ui.code(l.get('url') or '')}`"
        for l in ls if l.get("enabled", True))


def _ask_idpwd() -> str:
    """照搬 Boss create_user 的注册提示文案"""
    return "\n".join([
        "🤖 **进入注册状态**", _div(),
        "请在 **2 分钟**内输入：`[用户名][空格][安全码]`",
        "例子🌰：`苏苏 1234`",
        "",
        "· 用户名不限中/英文/emoji，🚫特殊字符（< > @ # & / \\）",
        "· 安全码为**敏感操作**（删除/重置/改名/换绑）时的附加验证，填 4~6 位数字",
        "· 退出请发 `/cancel`",
    ])


# ───────────────────────── 面板 ─────────────────────────
def panel_text(uid: int) -> str:
    acc = _acc(uid)
    if not _acc_has(acc):
        us = int(acc.get("us") or 0)
        qual = (f"🎫 开通资格｜**{us} 天**（可点「👑 创建账户」直接开号）"
                if us > 0 else "🎫 开通资格｜暂无（先到兑换阁换注册码）")
        return "\n".join([
            "📺 **EMBY 面板**", _div(),
            "你还没有 Emby 账号。",
            qual,
            "",
            "**开通方式**：",
            "· 👑 **创建账户** → 有资格（天数池）时一键开号",
            "· 🎟 **使用兑换码** → 注册码开号 / 续期码续期 / 白名单码升级",
            "· 🔁 **绑定TG** → 已在别处（Boss/RT）开过快照找回",
            "· 码在 /exchange 兑换阁用 V积分换取",
            "",
            "🌐 服务器线路、📊 统计等随开号后开放查询。",
        ])
    name = acc.get("name") or "—"
    ex = acc.get("ex")
    lv = acc.get("lv")
    lv_txt = {"a": "白名单", "b": "普通", "c": "已封禁", "d": "已删除"}.get(
        lv, str(lv) if lv else "—")
    ex_txt = (ex.strftime("%Y-%m-%d %H:%M")
              if isinstance(ex, dt.datetime) else (str(ex) if ex else "—"))
    rem = ""
    if isinstance(ex, dt.datetime):
        d = (ex - dt.datetime.now()).days
        rem = f"（剩 {d} 天）" if d >= 0 else "（⚠️ 已到期，用续期码可续）"
    return "\n".join([
        "📺 **EMBY 面板**", _div(),
        f"👤 用户名｜`{ui.code(name)}`",
        f"📅 到期｜**{ex_txt}**{rem}",
        f"🏷 状态｜{lv_txt}",
        "",
        "👇 点按钮进入对应功能，每个子页都能返回：",
    ])


def panel_kb(uid: int) -> KM:
    acc = _acc(uid)
    if not _acc_has(acc):
        return KM([
            [B("👑 创建账户", "acc:create"), B("🎟 使用兑换码", "acc:use")],
            [B("🔁 绑定TG(找回)", "acc:rebind"), B("🌐 服务器线路", "acc:lines")],
            [B("📊 服务器统计", "acc:stats"), B("🔗 绑定 115", "acc:bind")],
            [B("↩️ 返回主菜单", "menu")],
        ])
    return KM([
        [B("🌐 服务器线路", "acc:lines"), B("📀 媒体库", "acc:libs")],
        [B("📊 服务器统计", "acc:stats"), B("🎟 使用兑换码", "acc:use")],
        [B("👁 查看密码", "acc:pwd"), B("🎬 媒体库显隐", "acc:block")],
        [B("💠 我的设备", "acc:dev"), B("💖 我的收藏", "acc:fav")],
        [B("🏪 兑换商店", "acc:store"), B("✏️ 改用户名", "acc:rename")],
        [B("🔑 重置密码", "acc:rst"), B("🗑 删除账号", "acc:del")],
        [B("🔁 绑定TG(找回)", "acc:rebind"), B("🔗 绑定 115", "acc:bind")],
        [B("↩️ 返回主菜单", "menu")],
    ])


async def show_panel(client, cb_or_msg, uid: int) -> None:
    text = panel_text(uid)
    kb = panel_kb(uid)
    if isinstance(cb_or_msg, CallbackQuery):
        try:
            await cb_or_msg.message.edit_text(text, reply_markup=kb, parse_mode=PM)
        except Exception:
            await cb_or_msg.message.reply(text, reply_markup=kb, parse_mode=PM)
    else:
        await cb_or_msg.reply(text, reply_markup=kb, parse_mode=PM)


async def _sub_page(cb, text: str, kb: KM):
    """子功能页统一出口：就地编辑 + 底部返回面板（层级导航）"""
    await cb.message.edit_text(text, reply_markup=kb, parse_mode=PM)


# ───────────────────────── 命令入口 ─────────────────────────
@bot.on_message(filters.command(["account", "账号", "我的账号"], **CMD))
async def cmd_account(client, msg: Message):
    await show_panel(client, msg, msg.from_user.id)


# ───────────────────────── 🌐 线路 / 📀 媒体库 / 📊 统计 ─────────────────────────
def _fmt_num(v) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _progress_bar(cur: int, total: int, width: int = 8) -> str:
    if total <= 0:
        return ""
    filled = max(1, min(width, round(cur / total * width)))
    return "▰" * filled + "▱" * (width - filled)


@bot.on_callback_query(filters.regex(r"^acc:lines$"))
async def cb_lines(client, cb: CallbackQuery):
    await cb.answer()
    ls = [l for l in unibot.server_lines() if l.get("enabled", True) and l.get("url")]
    lines = ["🌐 **服务器播放线路**", _div(),
             "任选一条登录，哪个快用哪个", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, l in enumerate(ls):
        medal = medals[i] if i < len(medals) else "·"
        tag = "　**← 推荐**" if i == 0 else "　**← 慢**"
        nm = ui.safe(l.get("name") or f"线路{i + 1}")
        url = (l.get("url") or "").strip()
        lines.append(f"{medal} {nm}{tag}")
        lines.append(f"   `{ui.code(url)}`")
        lines.append("")
    lines += [_div(), "💡 开号成功时也会把线路一起私信发给你。"]
    await _sub_page(cb, "\n".join(lines), KM([[B("↩️ 返回面板", "account")]]))


@bot.on_callback_query(filters.regex(r"^acc:libs$"))
async def cb_libs(client, cb: CallbackQuery):
    await cb.answer("正在读取媒体库…")
    # ★ 2026-11-05 提速：清单与个人隐藏态并发取（原来串行 3 次 Emby 调用）
    res, hid = await asyncio.gather(
        asyncio.to_thread(unibot.libs_list),
        asyncio.to_thread(unibot.libs_hidden, cb.from_user.id))
    if not res.get("ok"):
        await cb.answer(res.get("msg") or "读取失败", show_alert=True)
        return
    libs = res.get("libs") or []
    hid = await asyncio.to_thread(unibot.libs_hidden, cb.from_user.id)
    hidden = set(hid.get("hidden") or []) if hid.get("ok") else set()
    show = [n for n in libs if n not in hidden]
    hide = [n for n in libs if n in hidden]
    # 两列排布：每行两个库，宽 14 字对齐
    def _cols(names):
        out, row = [], []
        for n in names:
            row.append(ui.safe((n or "")[:14]))
            if len(row) == 2:
                out.append("　" + " ｜ ".join(f"{x:<14}" for x in row))
                row = []
        if row:
            out.append("　" + row[0])
        return out or ["　（无）"]
    lines = ["📀 **媒体库总览**", _div(),
             f"🗂 全服 **{len(libs)}** 个库｜你可见 **{len(show)}** 个", "",
             "**✅ 你可见的库**"]
    lines += _cols(show)
    if hide:
        lines += ["", "**🕶 你已隐藏的库**"]
        lines += _cols(hide)
    lines += ["", _div(), "💡 想少点杂项？用下面的「媒体库显隐」一键切换。"]
    await _sub_page(cb, "\n".join(lines), KM([
        [B("🎬 媒体库显隐", "acc:block"), B("↩️ 返回面板", "account")]]))


@bot.on_callback_query(filters.regex(r"^acc:stats$"))
async def cb_stats(client, cb: CallbackQuery):
    await cb.answer("正在统计…")
    st = await asyncio.to_thread(unibot.media_stats)
    if not st.get("ok"):
        await cb.answer(st.get("msg") or "统计失败", show_alert=True)
        return
    mv, sv, ep = (int(st.get(k) or 0) for k in ("movies", "series", "episodes"))
    total_items = mv + sv + ep
    playing = int(st.get("playing") or 0)
    users = int(st.get("users") or 0)
    lines = [
        "📊 **服务器统计**", _div(),
        "**🍿 实时**",
        f"　正在播放｜**{playing}** 路 {_progress_bar(playing, max(users, 1))}",
        f"　注册账号｜**{users}** 个",
        "",
        "**🎬 内容量**",
        f"　电影｜**{_fmt_num(mv)}** 部",
        f"　剧集｜**{_fmt_num(sv)}** 部",
        f"　分集｜**{_fmt_num(ep)}** 集",
        f"　合计｜**{_fmt_num(total_items)}** 条目",
        "",
        "**🗂 媒体库**｜**{}** 个（明细见「媒体库」）".format(st.get("libs") or 0),
        "",
        _div(),
        f"　🌐 {ui.safe(st.get('server') or 'KFC')}　·　"
        f"Emby **{ui.safe(str(st.get('version') or '?'))}**",
        "　💡 数据每 5 分钟内现读，非缓存。",
    ]
    await _sub_page(cb, "\n".join(lines), KM([
        [B("📀 媒体库明细", "acc:libs"), B("↩️ 返回面板", "account")]]))


# ───────────────────────── 👑 创建账户 / 🎟 兑换码 / 🔁 绑定TG ─────────────────────────
@bot.on_callback_query(filters.regex(r"^acc:create$"))
async def cb_create(client, cb: CallbackQuery):
    """👑 创建账户（Boss 同语义）：先输「用户名 安全码」，再消耗天数池开号"""
    uid = cb.from_user.id
    if _acc_has(_acc(uid)):
        await cb.answer("你已有 Emby 账号，无需创建", show_alert=True)
        return
    us = int(_acc(uid).get("us") or 0)
    if us <= 0:
        await cb.answer("暂无开通资格（天数池 0）。先到 /exchange 兑换阁换注册码，"
                        "再点「🎟 使用兑换码」。", show_alert=True)
        return
    _FLOW[uid] = {"mode": "create", "step": "idpwd", "days": us, "ts": time.time()}
    await cb.answer()
    await cb.message.reply(_ask_idpwd(), parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:rebind$"))
async def cb_rebind(client, cb: CallbackQuery):
    """🔁 绑定TG（Boss changetg 同语义）：用户名+安全码 认领找回账号"""
    uid = cb.from_user.id
    if _acc_has(_acc(uid)):
        await cb.answer("你名下已有账号，如需改名请用「✏️ 改用户名」", show_alert=True)
        return
    _FLOW[uid] = {"mode": "rebind", "step": "idpwd", "ts": time.time()}
    await cb.answer()
    await cb.message.reply(
        "🔁 **绑定 / 找回你的账号**\n" + _div() +
        "\n适用于：以前在 Boss / RT 开过号，想在 Game 这边也能用。\n"
        "请发送：`[Emby用户名][空格][安全码]`（例：`苏苏 1234`）\n"
        "验证通过后该账号迁到你的 TG 名下。发 /cancel 取消。", parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:use$"))
async def cb_use(client, cb: CallbackQuery):
    uid = cb.from_user.id
    _FLOW[uid] = {"mode": "use", "step": "code", "ts": time.time()}
    await cb.answer()
    await cb.message.reply(
        "🎟 **发送你的兑换码**\n" + _div() +
        "\n支持三种，发我自动识别：\n"
        "· **注册码** → 自动开号（会再让你设用户名+安全码）\n"
        "· **续期码** → 自动续期\n"
        "· **白名单码** → 升级白名单\n\n"
        "码在 /exchange 兑换阁用 V积分换取。发 /cancel 取消。", parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:bind$"))
async def cb_bind(client, cb: CallbackQuery):
    """🔗 绑定 115：直接弹双选项菜单（扫码自动同步RT / 打开RT面板自助）。"""
    from . import bind115
    if not bind115.rtapi_enabled():
        await cb.answer("未配置 RT 接口，无法绑定", show_alert=True)
        return
    await cb.answer()
    text, kb = await bind115.bind_menu(client, cb.from_user.id)
    await cb.message.reply(text, reply_markup=kb, parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:pwd$"))
async def cb_pwd(client, cb: CallbackQuery):
    """查看当前账号密码+线路（私聊发送，不在群里露明文）"""
    uid = cb.from_user.id
    acc = _acc(uid)
    if not _acc_has(acc):
        await cb.answer("你还没有 Emby 账号", show_alert=True)
        return
    await cb.answer("已私聊发你密码")
    try:
        await client.send_message(uid, "\n".join([
            "🔐 **你的 Emby 账号**", _div(),
            f"👤 用户名｜`{ui.code(acc.get('name') or '')}`",
            f"🔑 密码｜`{ui.code(acc.get('pwd') or '')}`",
            "🌐 **播放线路**（选最快的）：",
            _lines_md(),
            "_请保存勿转发；忘记可「🔑 重置密码」。_",
        ]), parse_mode=PM)
    except Exception:
        await cb.answer("无法私信你，请先私聊我点一次 /start", show_alert=True)


# ───────────────────────── 🎬 显隐 / 💠 设备 / 💖 收藏 / 🏪 商店 ─────────────────────────
def _block_kb() -> KM:
    return KM([
        [B("🕶 隐藏杂项库", "acc:blockhide"), B("👁 全部显示", "acc:blockshow")],
        [B("↩️ 返回面板", "account")],
    ])


async def _block_view(cb, hidden, prefix: str = ""):
    txt = "\n".join([
        (prefix + "\n" if prefix else "") + "🎬 **媒体库显示 / 隐藏**", _div(),
        "用途：把「播放列表 / R级电影」等杂项库藏起来，界面更干净。",
        "",
        "当前隐藏：" + ("**" + "、".join(ui.safe(h) for h in hidden) + "**"
                     if hidden else "无（全部可见）"),
    ])
    await cb.message.edit_text(txt, reply_markup=_block_kb(), parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:block$"))
async def cb_block(client, cb: CallbackQuery):
    res = await asyncio.to_thread(unibot.libs_hidden, cb.from_user.id)
    if not res.get("ok"):
        await cb.answer(res.get("msg") or "读取失败", show_alert=True)
        return
    await cb.answer()
    await _block_view(cb, res.get("hidden") or [])


async def _do_block(client, cb: CallbackQuery, hide: bool):
    await cb.answer("正在处理…")
    res = await asyncio.to_thread(unibot.set_libs_hidden, cb.from_user.id, hide)
    if not res.get("ok"):
        await cb.answer(res.get("msg") or "操作失败", show_alert=True)
        return
    r2 = await asyncio.to_thread(unibot.libs_hidden, cb.from_user.id)
    await _block_view(cb, (r2.get("hidden") if r2.get("ok") else []) or [],
                      prefix=f"✅ {res.get('msg')}")


@bot.on_callback_query(filters.regex(r"^acc:blockhide$"))
async def cb_blockhide(client, cb: CallbackQuery):
    await _do_block(client, cb, True)


@bot.on_callback_query(filters.regex(r"^acc:blockshow$"))
async def cb_blockshow(client, cb: CallbackQuery):
    await _do_block(client, cb, False)


@bot.on_callback_query(filters.regex(r"^acc:dev$"))
async def cb_dev(client, cb: CallbackQuery):
    await cb.answer("正在获取设备信息…")
    res = await asyncio.to_thread(unibot.get_devices, cb.from_user.id)
    if not res.get("ok"):
        await cb.answer(res.get("msg") or "获取失败", show_alert=True)
        return
    devs = res.get("devices") or []
    ips = res.get("ips") or []
    lines = [f"💠 **我的设备**", _div(),
             f"共 **{len(devs)}** 个设备、**{len(ips)}** 个 IP", "", "**设备**"]
    lines += ([f"{i}. {ui.safe(d.replace('|', ' ｜ '))}"
               for i, d in enumerate(devs, 1)] or ["　（无播放记录）"])
    lines += ["", "**IP**"]
    lines += ([f"{i}. `{ui.safe(ip)}`" for i, ip in enumerate(ips, 1)] or ["　（无）"])
    await _sub_page(cb, "\n".join(lines), KM([[B("↩️ 返回面板", "account")]]))


@bot.on_callback_query(filters.regex(r"^acc:fav(?::(\d+))?$"))
async def cb_fav(client, cb: CallbackQuery):
    m = re.match(r"^acc:fav(?::(\d+))?$", cb.data)
    page = int(m.group(1) or 1)
    await cb.answer("正在读取收藏…")
    res = await asyncio.to_thread(unibot.get_favorites, cb.from_user.id,
                                  (page - 1) * _PAGE, _PAGE)
    if not res.get("ok"):
        await cb.answer(res.get("msg") or "读取失败", show_alert=True)
        return
    data = res.get("data") or {}
    items = data.get("Items") or []
    total = int(data.get("TotalRecordCount") or 0)
    pages = max(1, math.ceil(total / _PAGE))
    lines = [f"💖 **我的收藏**｜共 {total} 个", _div()]
    if not items:
        lines.append("　（还没有收藏，去 Emby 点❤️吧）")
    for it in items:
        t = _TYPE_CN.get(it.get("Type"), it.get("Type") or "未知")
        lines.append(f"· {t}｜{ui.safe(it.get('Name') or '')}")
    nav = []
    if page > 1:
        nav.append(B("⬅️ 上一页", f"acc:fav:{page - 1}"))
    if page < pages:
        nav.append(B("➡️ 下一页", f"acc:fav:{page + 1}"))
    rows = ([nav] if nav else []) + [[B("↩️ 返回面板", "account")]]
    await cb.message.edit_text("\n".join(lines), reply_markup=KM(rows), parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:store$"))
async def cb_store(client, cb: CallbackQuery):
    from . import redeem
    uid = cb.from_user.id
    cv = db.get_cultivator(uid)
    if not cv:
        await cb.answer("兑换阁在修仙面板，先发 /start 创建角色", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        "\n".join(redeem.panel_lines(uid, cv["realm"])),
        reply_markup=redeem.panel_menu(
            cv["realm"], db.get_points(uid),
            is_owner=(int(uid) == int(S.OWNER or 0))))


# ───────────────────────── ✏️ 改名 / 🔑 重置 / 🗑 删除（都验安全码） ─────────────────────────
def _start_verify(uid: int, mode: str) -> str:
    _FLOW[uid] = {"mode": mode, "ts": time.time()}
    what = {"verify_rst": "重置密码", "verify_rename": "修改用户名",
            "verify_del": "删除账号"}[mode]
    return "\n".join([
        "🔰 **账户安全验证**", _div(),
        f"{what}属于敏感操作，请发送你的**安全码**（注册时设置的）。",
        "发 /cancel 取消。",
    ])


@bot.on_callback_query(filters.regex(r"^acc:rst$"))
async def cb_rst(client, cb: CallbackQuery):
    uid = cb.from_user.id
    acc = _acc(uid)
    if not _acc_has(acc):
        await cb.answer("你还没有 Emby 账号", show_alert=True)
        return
    await cb.answer()
    if (acc.get("pwd2") or "").strip():
        await cb.message.reply(_start_verify(uid, "verify_rst"), parse_mode=PM)
        return
    _FLOW[uid] = {"mode": "setpwd", "ts": time.time()}
    await cb.message.reply(
        "🔑 **设置新密码**\n" + _div() +
        "\n请直接发送你的**新密码**（6~32 位）。发 /cancel 取消。", parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:rename$"))
async def cb_rename(client, cb: CallbackQuery):
    uid = cb.from_user.id
    acc = _acc(uid)
    if not _acc_has(acc):
        await cb.answer("你还没有 Emby 账号", show_alert=True)
        return
    await cb.answer()
    if (acc.get("pwd2") or "").strip():
        await cb.message.reply(_start_verify(uid, "verify_rename"), parse_mode=PM)
        return
    _FLOW[uid] = {"mode": "setname", "ts": time.time()}
    await cb.message.reply(
        "✏️ **修改用户名**\n" + _div() +
        "\n请直接发送**新用户名**（2~32 字，禁特殊字符）。发 /cancel 取消。",
        parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:rstno$"))
async def cb_rstno(client, cb: CallbackQuery):
    await cb.answer("已取消")
    await show_panel(client, cb, cb.from_user.id)


@bot.on_callback_query(filters.regex(r"^acc:del$"))
async def cb_del(client, cb: CallbackQuery):
    uid = cb.from_user.id
    acc = _acc(uid)
    if not _acc_has(acc):
        await cb.answer("你还没有 Emby 账号", show_alert=True)
        return
    await cb.answer()
    if (acc.get("pwd2") or "").strip():
        await cb.message.reply(_start_verify(uid, "verify_del"), parse_mode=PM)
        return
    await cb.message.edit_text(
        _DEL_CONFIRM.format(div=_div()),
        reply_markup=KM([[B("✅ 确认删除", "acc:delok"), B("❌ 取消", "acc:delno")]]),
        parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:delok$"))
async def cb_delok(client, cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer("正在删除…")
    res = await asyncio.to_thread(unibot.delete_emby_account, uid)
    if not res.get("ok"):
        await cb.message.edit_text(
            "😵 删除失败：" + _div() + ui.safe(res.get("msg") or "未知错误"),
            reply_markup=KM([[B("↩️ 返回面板", "account")]]), parse_mode=PM)
        return
    await cb.message.edit_text(
        "🗑️ **账号已删除**\n" + _div() +
        "\n愿来日各自安好，山高水长，我们有缘再见！",
        reply_markup=KM([[B("↩️ 返回主菜单", "menu")]]), parse_mode=PM)


@bot.on_callback_query(filters.regex(r"^acc:delno$"))
async def cb_delno(client, cb: CallbackQuery):
    await cb.answer("已取消")
    await show_panel(client, cb, cb.from_user.id)


# ───────────────────── 文本状态机（开号/用码/验安全码/自设密码/改名/换绑） ─────────────────────
# ★ group=-5：必须早于 bind.py 的闸门（group=-1），否则未绑定用户发码会被
#   「请先绑定」拦截，新人永远开不了号。
@bot.on_message(filters.text & filters.private, group=-5)
async def on_flow_text(client, msg: Message):
    uid = msg.from_user.id
    fl = _FLOW.get(uid)
    if not fl:
        return  # 不在流程中，交给其它处理器
    text = (msg.text or "").strip()

    if text.startswith("/"):        # 命令：/cancel 结束流程，其它命令直接放行
        if text.lower().startswith("/cancel"):
            _FLOW.pop(uid, None)
            await msg.reply("❌ 已取消。",
                            reply_markup=KM([[B("📺 返回面板", "account"),
                                              B("↩️ 主菜单", "menu")]]),
                            parse_mode=PM)
        return

    mode, step = fl.get("mode"), fl.get("step")

    # ── 安全码验证（删除/改名/重置前） ──
    if mode in ("verify_del", "verify_rst", "verify_rename"):
        real = (_acc(uid).get("pwd2") or "").strip()
        if real and text != real:
            await msg.reply("❌ 安全码不对，请重试（或 /cancel 取消）。")
            return
        if mode == "verify_del":
            _FLOW.pop(uid, None)
            await msg.reply(_DEL_CONFIRM.format(div=_div()),
                            reply_markup=KM([[B("✅ 确认删除", "acc:delok"),
                                              B("❌ 取消", "acc:delno")]]),
                            parse_mode=PM)
            msg.stop_propagation()
            return
        if mode == "verify_rst":
            fl.update(mode="setpwd", ts=time.time())
            await msg.reply("✅ 验证通过。请发送你的**新密码**（6~32 位）。"
                            "发 /cancel 取消。", parse_mode=PM)
            msg.stop_propagation()
            return
        fl.update(mode="setname", ts=time.time())
        await msg.reply("✅ 验证通过。请发送**新用户名**（2~32 字，禁特殊字符）。"
                        "发 /cancel 取消。", parse_mode=PM)
        msg.stop_propagation()
        return

    # ── 自设新密码 ──
    if mode == "setpwd":
        if not (6 <= len(text) <= 32):
            await msg.reply("⚠️ 密码需 6~32 位，请重发或 /cancel 取消。")
            return
        _FLOW.pop(uid, None)
        await msg.reply("🔧 正在设置新密码…", parse_mode=PM)
        res = await asyncio.to_thread(unibot.set_emby_password, uid, text)
        if res.get("ok"):
            acc = _acc(uid)
            try:
                await client.send_message(uid, "\n".join([
                    "🔐 **密码已更新**", _div(),
                    f"👤 用户名｜`{ui.code(acc.get('name') or '')}`",
                    f"🔑 新密码｜`{ui.code(text)}`",
                    "🌐 **播放线路**：", _lines_md(),
                    "_请保存勿转发；安全码不变。_",
                ]), parse_mode=PM)
            except Exception as e:
                logger.warning(f"新密码私发失败 uid={uid}: {e}")
            await msg.reply("✅ **密码已更新**（新密码+线路已私聊发你，安全码不变）。",
                            reply_markup=KM([[B("📺 返回面板", "account")]]),
                            parse_mode=PM)
        else:
            await msg.reply("😵 设置失败：" + ui.safe(res.get("msg") or "未知错误"),
                            reply_markup=KM([[B("📺 返回面板", "account")]]),
                            parse_mode=PM)
        msg.stop_propagation()
        return

    # ── 修改用户名 ──
    if mode == "setname":
        _FLOW.pop(uid, None)
        await msg.reply("🔧 正在改名…", parse_mode=PM)
        res = await asyncio.to_thread(unibot.rename_emby_user, uid, text)
        if res.get("ok"):
            await msg.reply("✅ **用户名已改为** `" + ui.code(res.get("name") or "") + "`",
                            reply_markup=KM([[B("📺 返回面板", "account")]]),
                            parse_mode=PM)
        else:
            await msg.reply("😵 改名失败：" + ui.safe(res.get("msg") or "未知错误"),
                            reply_markup=KM([[B("📺 返回面板", "account")]]),
                            parse_mode=PM)
        msg.stop_propagation()
        return

    # ── 收码（兑换码第一步） ──
    if step == "code":
        m = _CODE_RE.search(text)
        code = m.group(0) if m else text
        pk = await asyncio.to_thread(unibot.peek_boss_code, code)
        if not pk.get("ok"):
            await msg.reply("⚠️ " + ui.safe(pk.get("msg") or "码无效") +
                            "\n请重发兑换码，或 /cancel 取消。", parse_mode=PM)
            return
        if pk.get("kind") == "invite" and not _acc_has(_acc(uid)):
            fl.update(step="idpwd", code=code, days=pk.get("days") or 30,
                      ts=time.time())
            await msg.reply(
                f"✅ 注册码有效（**{pk.get('days')} 天**）。\n\n" + _ask_idpwd(),
                parse_mode=PM)
            return
        _FLOW.pop(uid, None)
        await msg.reply("🔧 正在核销码，请稍候…", parse_mode=PM)
        res = await asyncio.to_thread(unibot.redeem_boss_code_in_game, code, uid)
        await msg.reply(("✅ " + ui.safe(res.get("msg") or "操作成功"))
                        if res.get("ok") else
                        ("😵 核销失败：" + ui.safe(res.get("msg") or "未知错误")),
                        reply_markup=KM([[B("📺 返回面板", "account"),
                                          B("↩️ 主菜单", "menu")]]), parse_mode=PM)
        msg.stop_propagation()
        return

    # ── 收「用户名 安全码」→ 开号 / 换绑 ──
    if step == "idpwd":
        parts = text.split()
        if len(parts) != 2:
            await msg.reply("⚠️ 格式错误，请按「用户名 安全码」发送（例如 `苏苏 1234`），"
                            "或 /cancel 取消。", parse_mode=PM)
            return
        uname, sec = parts
        if mode == "rebind":
            # 换绑 TG：不需要重设用户名，安全码即注册时那个
            _FLOW.pop(uid, None)
            await msg.reply("🔁 正在验证并绑定…", parse_mode=PM)
            res = await asyncio.to_thread(unibot.rebind_tg_by_name, uid, uname, sec)
            if res.get("ok"):
                await msg.reply("✅ " + ui.safe(res.get("msg")) +
                                "　现在这张面板上的所有功能都归你了。",
                                reply_markup=KM([[B("📺 返回面板", "account")]]),
                                parse_mode=PM)
            else:
                await msg.reply("😵 " + ui.safe(res.get("msg") or "绑定失败"),
                                reply_markup=KM([[B("↩️ 返回面板", "account")]]),
                                parse_mode=PM)
            msg.stop_propagation()
            return
        if not (2 <= len(uname) <= 32) or any(c in _BAD_NAME for c in uname):
            await msg.reply("⚠️ 用户名需 2~32 个字符，且不能含特殊字符"
                            "（< > @ # & / \\ 等）。请重发或 /cancel。", parse_mode=PM)
            return
        if not re.fullmatch(r"\d{4,6}", sec):
            await msg.reply("⚠️ 安全码须为 **4~6 位数字**（例：1234）。请重发或 /cancel。",
                            parse_mode=PM)
            return
        _FLOW.pop(uid, None)
        await msg.reply("🆗 收到设置，正在开号，请稍候…", parse_mode=PM)

        if mode == "create":
            r = await asyncio.to_thread(unibot.open_account, uid,
                                         int(fl.get("days") or 30), uname, sec)
        else:  # use
            r = await asyncio.to_thread(unibot.redeem_boss_code_in_game,
                                        fl.get("code") or "", uid, uname, sec)
            if r.get("ok") and r.get("opened"):
                r = {"ok": True, "username": r["opened"]["username"],
                     "password": r["opened"]["password"],
                     "embyid": r["opened"]["embyid"], "days": r.get("days")}
            elif r.get("ok"):          # 核销成功但未开号（如仅入天数池）
                await msg.reply("✅ " + ui.safe(r.get("msg") or "操作成功"),
                                reply_markup=KM([[B("📺 返回面板", "account")]]),
                                parse_mode=PM)
                msg.stop_propagation()
                return
        if not r.get("ok"):
            await msg.reply("😵 开号失败：" + _div() +
                            ui.safe(r.get("msg") or "未知错误"),
                            reply_markup=KM([[B("↩️ 返回面板", "account")]]),
                            parse_mode=PM)
            return
        try:
            await asyncio.to_thread(
                unibot.bind_after_open, uid, r["embyid"], r["username"],
                r["password"], msg.from_user.first_name or "",
                msg.from_user.username or "")
        except Exception as e:
            logger.warning(f"开号后绑定入库失败 uid={uid}: {e}")
        # ★ 用户要求：开号卡片必须带服务器播放线路
        card = "\n".join([
            "🎉 **账户创建成功**", _div(),
            f"👤 用户名｜`{ui.code(r['username'])}`",
            f"🔑 密码｜`{ui.code(r['password'])}`",
            f"🔒 安全码｜`{ui.code(sec)}`（删除/重置/改名时验证，请记牢）",
            f"📅 有效期｜{r.get('days')} 天",
            "",
            "🌐 **播放线路**（任选一个，推荐最快的）：",
            _lines_md(),
            _div(),
            "⏳ 到期前用 V积分续期码自助续；Emby / RT 同一套账号密码。",
        ])
        try:
            await client.send_message(uid, card, parse_mode=PM)
        except Exception as e:
            logger.warning(f"开号卡私发失败 uid={uid}: {e}")
        await msg.reply("✅ **账户已开通**，账号/密码/安全码/线路已私聊发给你。",
                        reply_markup=KM([[B("📺 返回面板", "account"),
                                          B("↩️ 主菜单", "menu")]]), parse_mode=PM)
        msg.stop_propagation()
        return
