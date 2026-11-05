"""权限分级体系 —— owner / 管理员（逐项授权） / 群友

★ 设计（2026-10-10 用户拍板）
────────────────────────────
· **owner（群主本人）**：恒有全部权限，且能看到/使用所有命令。
· **管理员**：由 owner 在 `/admin` 面板里添加，**逐项勾选**授权模块。
  没有勾选的模块 ⇒ 该管理员私聊菜单里**看不到**这些命令，硬敲也**执行不了**。
· **群友**：只有 level 0（公开）模块。

★ 为什么菜单能做到「看不到」
Telegram 的命令菜单支持按 scope 下发。给每个管理员的私聊单独下发
`BotCommandScopeChat(chat_id=管理员ID)`，只放他有权用的命令；
群组 scope 只放公开命令 ⇒ owner 专属命令（MP/qB/Emby/MT/CD2）在群里根本不出现。

★ 存储
`kfcgame.kfc_admin`（tg / grants / note）。owner 不入表。
"""
from __future__ import annotations

import functools

from loguru import logger
from pyrogram import filters
from pyrogram.types import (InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import db
from .. import settings as S
from .. import ui

CMD = dict(prefixes=S.PREFIX)

# 级别常量
PUBLIC = 0      # 群友
ADMIN = 1       # 可授权给管理员
OWNER_ONLY = 2  # 仅群主本人

# 模块 → (显示名, 级别, 说明)
MODULES: dict[str, tuple[str, int, str]] = {
    "game":     ("🎮 修仙玩法", PUBLIC, "打坐/闭关/坊市/斗法/红包"),
    "rank":     ("📊 榜单查询", PUBLIC, "天机榜/观看榜/在线播报"),
    # ★ "bind" 只能有一条：早前这里重复定义过两次（"🔗 绑定认领" 被后面的
    #   "🔐 账号绑定" 静默覆盖），导致字典里只剩后者、前者语义丢失。
    #   现合并为一条，绑定 + 认领都归它（PUBLIC：未绑定的人必须能用它自救）。
    "bind":     ("🔐 账号绑定与认领", PUBLIC,
                 "绑定/解绑/认领（未绑定无法用其他命令）"),
    "req":      ("🔍 求片",     PUBLIC, "搜片/订阅/我的求片"),
    # ★ 2026-10-13：投稿是**群友可用**的（手里有资源直接投），审核才需要权限。
    "contrib":  ("📮 投稿",     PUBLIC, "投链接/磁力 → 管理员审核 → 自动转 MT 转存"),
    "pt":       ("📡 PT 进度",  PUBLIC, "实时进度/日报/链路状态"),
    "exchange": ("🎟 兑换阁",   PUBLIC, "注册码/续期码/灵石换 V积分"),
    # ★ 2026-10-14：power 只承载「Emby 发电 + Emby 服保号规则」（群友公开）。
    #   PT 保号规则/总览归 ka（群主/授权管理员），群友菜单不再出现任何 PT。
    "power":    ("⚡ 发电 · Emby 服保号规则", PUBLIC,
                 "Emby 发电规则·价格 / Emby 服保号规则（免绑定可查）"),

    "bindctl":  ("🛠 绑定管理", ADMIN, "同步绑定 / 喊绑定 / 未绑定名单"),
    "reqaudit": ("🛎 求片审核", ADMIN, "求片管理台：改状态 / 驳回后可反悔同意"),
    "ptctl":    ("🛠 PT 控制",  ADMIN, "取消任务 / 清理卡片"),
    "notice":   ("📢 群内播报", ADMIN, "手动往群里发播报/公告"),
    "ka":       ("⚡ PT 保号",  ADMIN, "6大PT站保号：规则/进度/启停（群主默认独占）"),

    "mp":       ("🎬 MoviePilot", OWNER_ONLY, "搜索直下/订阅管理/站点/插件状态"),
    "qb":       ("🧲 qBittorrent", OWNER_ONLY, "种子增删/限速/暂停恢复"),
    "emby":     ("📺 Emby",     OWNER_ONLY, "媒体库刷新/在线会话"),
    "mt":       ("🗂 MediaTidy", OWNER_ONLY, "整理任务/方案/strm"),
    "cd2":      ("☁️ 115 云盘",  OWNER_ONLY, "CloudDrive2 挂载与容量"),
    "admin":    ("👑 权限管理",  OWNER_ONLY, "增删管理员 / 逐项授权"),
}

# 可授权给管理员的模块（/admin 面板里勾选的就是这些）
GRANTABLE = [k for k, v in MODULES.items() if v[1] == ADMIN]

# ── 命令总表：(命令, 说明, 模块) ──────────────────────────────────────
ALL_CMDS: list[tuple[str, str, str]] = [
    ("start", "踏入仙途 / 主菜单", "game"),
    ("choose", "选择门派", "game"),
    ("name", "取一个道号", "game"),
    ("me", "查看我的道途", "game"),
    ("sign", "每日打坐领灵石", "game"),
    ("cultivate", "闭关修炼涨修为", "game"),
    ("breakthrough", "⚡ 冲击更高境界（按钮可一键）", "game"),
    ("shop", "🏯 坊市（消耗 🪙灵石）", "game"),
    ("exchange", "🎟 兑换阁：注册码/续期（只收 V积分）", "exchange"),
    ("duihuan", f"💱 灵石兑换 V积分（{S.LS_PER_V}:1）", "exchange"),
    ("duanwei", "📖 段位折扣/货币关系说明", "exchange"),
    ("vpoints", "💎 如何获取V积分：签到/观影/兑换 全渠道", "exchange"),
    ("account", "📺 EMBY 面板：开号/线路/媒体库/统计/重置/删除", "exchange"),
    ("mycodes", "🎫 我领到的兑换码", "exchange"),
    ("ka", "⚡ PT保号：6大PT站进度/启停（群主/授权管理员）", "ka"),
    # ★ 2026-10-14：PT 保号规则归群主/授权管理员（ka），不再对群友公开。
    ("karule", "📖 PT保号规则·价格（群主）", "ka"),
    ("power", "⚡ 发电：Emby 发电规则·价格·V积分兑换（公开）", "power"),
    ("bind", "🔐 绑定 Emby 账号（私聊完成）", "bind"),
    ("bindstatus", "🔐 查看我的绑定状态", "bind"),
    ("unbind", "🔓 解绑", "bind"),
    # ★ /claim 归「认领我的名字」专用（bind 模块）。领奖命令已改名为
    #   /claimreward，见 social.py —— 同名命令挂两个模块会让菜单出现重复项，
    #   且 _CMD2MOD 是字典推导、后者覆盖前者，权限归属会悄悄错位。
    ("claim", "🙋 认领榜单上我的名字", "bind"),
    ("tasks", "每日任务", "game"),
    ("achievements", "我的成就", "game"),
    ("rank", "天机榜（境界/财富）", "rank"),
    ("myrank", "我的修行排名", "rank"),
    ("visit", "拜访道友", "game"),
    ("rob", "夺取他人灵石", "game"),
    ("duel", "斗法论道", "game"),
    ("claimreward", "🎁 领取周榜/月榜奖励", "game"),
    ("hb", "群内发拼手气红包", "game"),
    ("give", "🎁 赠送灵石给别人", "game"),
    ("dayrank", "今日播放榜（人人可查）", "rank"),
    ("weekrank", "本周播放榜", "rank"),
    ("monthrank", "本月播放榜", "rank"),
    ("top", "全部观看榜单（海报/按钮）", "rank"),
    ("myview", "我的观看统计", "rank"),
    ("now", "📡 在线播报（谁在看什么 + 画质）", "rank"),
    ("zb", "📡 在线播报（now 的别名）", "rank"),
    ("online", "📡 在线播报（now 的别名）", "rank"),
    # ★ 这里原有第二条 ("bind", "🔗 关联 Emby 名字")，是旧版 rank.py 那个
    #   **免核验**自助绑定的菜单项。该命令已下线（改为管理员 /setbind，
    #   群友自助走 /claim 输密码核验），菜单项随之删除，避免两条 /bind。
    ("qp", "🔍 求片（也可直接发片名）", "req"),
    ("mp", "🔍 求片（qp 的别名）", "req"),
    ("myreqs", "📋 我的求片申请与审批状态", "req"),
    # ★ 菜单项命令名**必须**是 [a-z0-9_]{1,32} —— Telegram 的 setBotCommands
    #   只接受小写字母/数字/下划线。写中文名会让**整份菜单**直接 400
    #   （BOT_COMMAND_INVALID），三个作用域 + 按人下发全部一起挂。
    #   handler 那边仍可保留中文别名（/我的求片台），只是不进菜单。
    ("myreqd", "📋 我的求片·审核状态+下载进度", "req"),
    ("myreq", "📋 我求的片 + 各自进度（带海报）", "pt"),
    ("dl", "📡 实时下载进度（进度条/速度/剩余时间）", "pt"),
    ("ptreport", "📊 PT 日报（今日求片/入库数）", "pt"),
    ("ptstatus", "🔗 PT 自动化链路状态", "pt"),
    ("help", "帮助说明", "game"),

    ("syncbind", "🔄 自动关联 Emby↔TG（管理员）", "bindctl"),
    ("callbind", "📢 群里喊大家认领名字（管理员）", "bindctl"),
    ("unbound", "📋 未关联 TG 的名单（管理员）", "bindctl"),
    ("setbind", "🛠 代绑 Emby 名↔TG（管理员）", "bindctl"),
    ("reqs", "🛎 待审核求片（管理员）", "reqaudit"),
    ("reqmgr", "🗂 求片管理台·改状态（管理员）", "reqaudit"),
    # ★ 2026-10-13 投稿：投稿本身 PUBLIC，审核/投递设置归 reqaudit
    ("ctb", "📮 投稿：把链接/磁力投给机器人", "contrib"),
    ("myctb", "📋 我的投稿与审核状态", "contrib"),
    ("ctbs", "🛎 待审核投稿（管理员）", "reqaudit"),
    ("setmt", "⚙️ 设置投稿投递目标（管理员）", "reqaudit"),
    ("release", "🆕 本次升级要点与运行示例", "contrib"),
    ("ptasks", "🛠 订阅/下载任务面板（管理员）", "ptctl"),
    ("ptdel", "❌ 批量取消任务（管理员）", "ptctl"),
    ("ptclean", "🧹 清理无种源下载卡片（管理员）", "ptctl"),
    ("syncsite", "🌐 同步 MP 站点目录（管理员）", "ptctl"),
    ("subs", "🎯 订阅与求片管理中心（管理员）", "ptctl"),
    ("subdl", "⬇️ 全部下载·立即搜索（管理员）", "ptctl"),
    ("subpause", "⏸ 全部暂停（管理员）", "ptctl"),
    ("subresume", "▶️ 全部恢复（管理员）", "ptctl"),
    ("subprio", "🔝 优先下载（管理员）", "ptctl"),

    ("admin", "👑 管理员与权限（群主）", "admin"),
    ("gaps", "🩺 查漏补缺 · 缺集补齐（群主）", "admin"),
    ("mpsearch", "🔎 MP 搜索并直接下载（群主）", "mp"),
    ("mpsubs", "📺 MP 订阅管理（群主）", "mp"),
    ("mpsites", "🌐 MP 站点管理（群主）", "mp"),
    ("mpplugin", "🧩 MP 插件/下载器状态（群主）", "mp"),
    ("qb", "🧲 qBittorrent 管理（群主）", "qb"),
    ("emby", "📺 Emby 管理（群主）", "emby"),
    ("mt", "🗂 MediaTidy 管理（群主）", "mt"),
    ("cd2", "☁️ 115 云盘管理（群主）", "cd2"),
]

_CMD2MOD: dict[str, str] = {c: m for c, _d, m in ALL_CMDS}


def mod_of(cmd: str) -> str:
    """命令 → 模块 key（未登记的按公开处理）"""
    return _CMD2MOD.get(str(cmd or "").lower().lstrip("/!"), "game")


# ════════════════════════════════════════════════════════════════════
#  判定
# ════════════════════════════════════════════════════════════════════
def is_owner(uid) -> bool:
    try:
        return int(uid or 0) == int(S.OWNER or 0)
    except Exception:
        return False


def level_of(uid) -> int:
    """0=群友 1=管理员 2=owner"""
    if is_owner(uid):
        return OWNER_ONLY
    try:
        if int(uid or 0) in {int(a["tg"]) for a in db.admins()}:
            return ADMIN
    except Exception:
        pass
    return PUBLIC


def grants_of(uid) -> set[str]:
    """该管理员被授权的模块（owner 返回全部）"""
    if is_owner(uid):
        return set(MODULES.keys())
    try:
        return db.grants_of(int(uid or 0))
    except Exception:
        return set()


def can(uid, mod: str) -> bool:
    """能否使用某个模块"""
    lv = MODULES.get(mod, ("", PUBLIC, ""))[1]
    if lv == PUBLIC:
        return True
    if is_owner(uid):
        return True
    return lv == ADMIN and mod in grants_of(uid)


def can_cmd(uid, cmd: str) -> bool:
    return can(uid, mod_of(cmd))


async def _deny(msg: Message, mod: str) -> None:
    """无权限时的反馈：owner 专属模块**静默**（别人不该知道它存在），
    管理员模块则明确提示差哪项权限，方便 owner 事后授权。"""
    lv = MODULES.get(mod, ("", PUBLIC, ""))[1]
    if lv == OWNER_ONLY:
        return                                   # 一声不响
    try:
        await msg.reply(f"⛔ 你没有「{MODULES.get(mod, ('该模块', 0, ''))[0]}」权限\n\n"
                        "_找群主在管理员面板里勾选即可_")
    except Exception:
        pass


def gate(mod: str):
    """命令级权限闸门（装饰器）。用法：

        @bot.on_message(filters.command(["ptclean"], **CMD))
        @perm.gate("ptctl")
        async def cmd_ptclean(client, msg): ...
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(client, msg: Message, *a, **kw):
            uid = msg.from_user.id if msg.from_user else 0
            if not can(uid, mod):
                await _deny(msg, mod)
                return None
            return await fn(client, msg, *a, **kw)
        return wrapper
    return deco


def gate_cb(mod: str):
    """回调按钮级权限闸门"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(client, q, *a, **kw):
            uid = q.from_user.id if q.from_user else 0
            if not can(uid, mod):
                try:
                    await q.answer("⛔ 你没有这项权限", show_alert=True)
                except Exception:
                    pass
                return None
            return await fn(client, q, *a, **kw)
        return wrapper
    return deco


# ════════════════════════════════════════════════════════════════════
#  命令菜单（按人下发）
# ════════════════════════════════════════════════════════════════════
def _sanitize(cmds: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """剔除 Telegram 不接受的命令名。

    setBotCommands 的 command 只允许 [a-z0-9_]{1,32}。只要列表里有
    一个非法项，**整份菜单**都会 400 BOT_COMMAND_INVALID —— 不是那条
    命令单独失败，是整个作用域都注册不上（踩过：菜单里写了中文
    「我的求片台」，三个作用域 + 按人下发全挂）。所以这里硬过滤。
    """
    import re as _re
    ok = [(c, d) for c, d in cmds
          if _re.fullmatch(r"[a-z0-9_]{1,32}", c)]
    bad = [c for c, _ in cmds if not _re.fullmatch(r"[a-z0-9_]{1,32}", c)]
    if bad:
        logger.warning(f"命令菜单已跳过非法命令名（TG 不接受中文/大写）：{bad}")
    return ok


def cmds_for(uid) -> list[tuple[str, str]]:
    """某人可见的命令列表（用于 set_bot_commands）"""
    out = []
    for c, d, m in ALL_CMDS:
        if can(uid, m):
            out.append((c, d))
    return _sanitize(out)


def public_cmds() -> list[tuple[str, str]]:
    return _sanitize([(c, d) for c, d, m in ALL_CMDS
                      if MODULES.get(m, ("", PUBLIC, ""))[1] == PUBLIC])


# ════════════════════════════════════════════════════════════════════
#  /admin 面板（owner 私聊专用）
# ════════════════════════════════════════════════════════════════════
async def _name_of(uid: int) -> str:
    """尽量拿到可读名字，拿不到就退回 ID"""
    try:
        ch = await bot.get_chat(int(uid))
        nm = (getattr(ch, "first_name", "") or "") + " " + (getattr(ch, "last_name", "") or "")
        nm = nm.strip() or (getattr(ch, "username", "") or "")
        if nm:
            return ui.safe(nm)[:24]
    except Exception as e:
        logger.debug(f"取用户名失败 {uid}: {e}")
    return f"UID{uid}"


def _link(uid: int, name: str) -> str:
    return f"[{ui.safe(name)[:20]}](tg://user?id={int(uid)})"


async def admin_text() -> tuple[str, KM]:
    """管理员列表面板"""
    rows = await _adm_rows()
    lines = ["👑 **管理员与权限**", ui.divider()]
    if not rows:
        lines.append("当前还没有管理员")
        lines.append("_只有你自己（群主）有全部权限_")
    else:
        lines.append(f"当前管理员 **{len(rows)}** 人：")
        for r in rows:
            mods = r["grants"]
            if mods:
                txt = "、".join(MODULES.get(m, (m, 0, ""))[0] for m in mods)
            else:
                txt = "（未勾选任何模块）"
            lines.append(f"· {r['link']}　{txt}")
    lines += [
        ui.divider(),
        "🟢 公开模块（所有群友可用）：",
        "　" + "、".join(v[0] for v in MODULES.values() if v[1] == PUBLIC),
        "🟡 可授权模块（勾选后管理员才能用）：",
        "　" + "、".join(MODULES[k][0] for k in GRANTABLE),
        "🔴 群主专属（任何管理员都看不到）：",
        "　" + "、".join(v[0] for v in MODULES.values() if v[1] == OWNER_ONLY),
        ui.divider(),
        "💡 添加：`/admin add <TG ID>` 或 **回复对方消息**发 `/admin add`",
        "💡 移除：`/admin del <TG ID>`，也可点下面的按钮",
    ]
    kb = []
    for r in rows:
        kb.append([B(f"⚙️ {r['short']}", f"adm:cfg:{r['tg']}"),
                   B("❌ 移除", f"adm:del:{r['tg']}")])
    kb.append([B("➕ 添加管理员", "adm:add"), B("🔄 刷新", "adm:list")])
    kb.append([B("❌ 关闭", "close")])
    return "\n".join(lines)[:3500], KM(kb)


async def _adm_rows() -> list[dict]:
    out = []
    for a in db.admins():
        try:
            tg = int(a["tg"])
        except Exception:
            continue
        nm = await _name_of(tg)
        g = [x.strip() for x in str(a.get("grants") or "").split(",") if x.strip()]
        out.append({"tg": tg, "name": nm, "short": nm[:12],
                    "link": _link(tg, nm), "grants": g})
    return out


async def grant_text(uid: int) -> tuple[str, KM]:
    """某个管理员的「逐项授权」面板"""
    nm = await _name_of(uid)
    cur = grants_of(uid)
    lines = [f"⚙️ **授权 · {nm}**", ui.divider(),
             f"TG ID：`{uid}`", "",
             f"点一下即切换（{ui.ON} 已授权 / {ui.OFF} 未授权）："]
    kb = []
    for key in GRANTABLE:
        label, _lv, desc = MODULES[key]
        on = key in cur
        kb.append([B(f"{ui.mark(on)} {label}", f"adm:tg:{uid}:{key}")])
        lines.append(f"　{ui.mark(on)} **{label}**｜_{desc}_")
    kb.append([B(f"{ui.ON} 全选", f"adm:all:{uid}"),
               B(f"{ui.OFF} 全不选", f"adm:none:{uid}")])
    kb.append([B("↩️ 返回", "adm:list"), B("❌ 关闭", "close")])
    return "\n".join(lines)[:3500], KM(kb)


@bot.on_message(filters.command(["admin", "管理员", "权限"], **CMD))
async def cmd_admin(client, msg: Message):
    """管理员与权限面板（仅群主，命令在别人菜单里根本不出现）"""
    uid = msg.from_user.id if msg.from_user else 0
    if not is_owner(uid):
        return                                   # 静默：别人不该知道有这个命令
    parts = (msg.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if arg == "add":
        await _do_add(msg, parts)
        return
    if arg == "del":
        await _do_del(msg, parts)
        return
    if arg == "grant":
        await _do_grant(msg, parts)
        return
    text, kb = await admin_text()
    await msg.reply(text, reply_markup=kb, disable_web_page_preview=True)


async def _target_uid(msg: Message, parts: list[str]):
    """从 `/admin add 123` 或「回复对方消息 + /admin add」取目标 ID"""
    if len(parts) > 2:
        try:
            return int(parts[2])
        except Exception:
            return None
    rt = getattr(msg, "reply_to_message", None)
    if rt is not None and rt.from_user and not rt.from_user.is_bot:
        return int(rt.from_user.id)
    return None


async def _do_add(msg: Message, parts: list[str]) -> None:
    uid = await _target_uid(msg, parts)
    if not uid:
        await msg.reply("用法：\n"
                        "　`/admin add <TG ID>`\n"
                        "　或**回复对方的消息**发 `/admin add`")
        return
    note = " ".join(parts[3:])[:64] if len(parts) > 3 else ""
    db.add_admin(uid, note)
    nm = await _name_of(uid)
    await msg.reply(f"✅ 已添加管理员：{_link(uid, nm)}\n\n"
                    f"现在点「⚙️ 授权」勾选他能用的模块（默认一个都不给）",
                    disable_web_page_preview=True)


async def _do_del(msg: Message, parts: list[str]) -> None:
    uid = await _target_uid(msg, parts)
    if not uid:
        await msg.reply("用法：`/admin del <TG ID>`")
        return
    db.del_admin(uid)
    await msg.reply(f"🗑 已移除管理员 `{uid}`")


async def _do_grant(msg: Message, parts: list[str]) -> None:
    if len(parts) < 4:
        await msg.reply("用法：`/admin grant <TG ID> <模块1,模块2>`\n"
                        f"可选模块：{', '.join(GRANTABLE)}")
        return
    try:
        uid = int(parts[2])
    except Exception:
        await msg.reply("TG ID 得是数字")
        return
    mods = [m.strip() for m in parts[3].split(",") if m.strip() in GRANTABLE]
    db.set_grants(uid, mods)
    await msg.reply(f"✅ 已更新 `{uid}` 的授权：" +
                    "、".join(MODULES[m][0] for m in mods))


@bot.on_callback_query(filters.regex(r"^adm:"))
@gate_cb("admin")
async def cb_admin(client, q):
    data = q.data or ""
    seg = data.split(":")
    act = seg[1] if len(seg) > 1 else ""

    if act == "list":
        text, kb = await admin_text()
        await _edit(q, text, kb)
        await q.answer()
        return

    if act == "add":
        await q.answer("回复对方消息发 /admin add，或直接 /admin add <TG ID>",
                       show_alert=True)
        return

    if act == "del" and len(seg) > 2:
        try:
            uid = int(seg[2])
        except Exception:
            await q.answer(); return
        db.del_admin(uid)
        text, kb = await admin_text()
        await _edit(q, text, kb)
        await q.answer(f"已移除 {uid}")
        await _resync()
        return

    if act == "cfg" and len(seg) > 2:
        try:
            uid = int(seg[2])
        except Exception:
            await q.answer(); return
        text, kb = await grant_text(uid)
        await _edit(q, text, kb)
        await q.answer()
        return

    if act in ("tg", "all", "none") and len(seg) > 2:
        try:
            uid = int(seg[2])
        except Exception:
            await q.answer(); return
        cur = set(grants_of(uid))
        if act == "tg":
            key = seg[3] if len(seg) > 3 else ""
            if key in GRANTABLE:
                cur.discard(key) if key in cur else cur.add(key)
        elif act == "all":
            cur |= set(GRANTABLE)
        else:
            cur -= set(GRANTABLE)
        db.set_grants(uid, sorted(cur))
        text, kb = await grant_text(uid)
        await _edit(q, text, kb)
        await q.answer("已更新")
        await _resync()
        return

    await q.answer()


async def _resync() -> None:
    """授权变更后立刻重刷各人的命令菜单（新管理员看不到未授权命令）"""
    try:
        from ..main import refresh_command_scopes
        await refresh_command_scopes()
    except Exception as e:
        logger.debug(f"命令菜单重刷失败：{e}")


async def _edit(q, text: str, kb) -> None:
    try:
        if q.message.photo:
            await q.edit_message_caption(text, reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb,
                                      disable_web_page_preview=True)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.debug(f"管理员面板刷新失败：{e}")
