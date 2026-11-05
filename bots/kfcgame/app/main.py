"""KFC 修仙 bot —— 入口"""
from __future__ import annotations

import asyncio
import re
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from pyrogram import filters, idle
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from . import bot
from . import db
from . import settings as S

# ⚠️ perm 模块**不能**在这里 import —— 它在导入时就会注册 handler，
# 而本模块是在 asyncio.run 之前被加载的，那时候注册的 handler 会挂到
# 「永远不会运行」的旧循环上（Pyrogram 的经典坑）。
# 所有用到 perm 的地方都在 register_handlers / setup_commands 内部再导入。

# 已知命令（用于识别真正「未处理」的消息，避免把正常命令误报成未处理）
# 这里要把所有「命令别名」也列进来，否则私聊里打别名会被误报成未知命令
_KNOWN: set[str] = {
    "start", "choose", "name", "me", "sign", "cultivate", "breakthrough",
    "shop", "exchange", "duihuan", "duanwei", "mycodes", "tasks",
    "achievements", "rank", "myrank", "visit", "rob", "duel", "claim",
    "hb", "give", "dayrank", "weekrank", "monthrank", "top", "myview",
    "now", "bind", "qp", "mp", "myreqs", "myreq", "dl", "ptreport",
    "ptstatus", "help", "syncbind", "callbind", "unbound", "zb", "online",
    "reqs", "ptasks", "ptdel", "ptclean", "admin", "mpsearch", "mpsubs",
    "mpsites", "mpplugin", "qb", "emby", "mt", "cd2", "addcodes", "codes",
    "syncsite", "站点同步", "站点", "赠送", "转赠",
    "在线", "tupo", "stat", "task", "ex", "dh", "stock",
    "day", "周榜", "日榜", "月榜", "month", "rankboard", "榜单",
    "search", "找片", "request", "求片", "mrank", "steal", "fight", "pk",
    "claimreward", "redenvelope", "段位说明", "货币说明", "兑换说明",
    "换积分", "兑换积分", "我的码", "绑定", "我的观看", "myviewrank",
    "addcodes", "pt", "myqa", "我的申请", "求片记录", "mystatus",
    "审核", "待审核", "audit", "任务", "订阅任务", "pttask", "取消任务",
    "deltask", "下载", "下载进度", "进度", "jd", "我的求片", "求片进度",
    "setbind", "代绑", "claimreward",
    "我的片", "今日求片", "今日片", "ptreport", "pt日报", "日报",
    "ptclean", "sync", "同步绑定", "call", "喊绑定", "未绑定",
    "没绑定", "管理员", "权限", "赠送", "give",
    # 2026-10-13 新增：投稿（投链接/磁力 → 审核 → 转 MT 转存机器人）
    "投稿", "tougao", "contribute", "ctb", "投递", "投资源",
    "myctb", "我的投稿", "mycontrib", "ctbs", "投稿审核", "待审核投稿",
    "setmt", "chatid", "groupid", "release", "升级要点", "更新说明",
    # 2026-10-13 新增：规则咨询（免绑定、免权限）
    "karule", "保号规则", "发电", "发电规则", "power", "fadian",
    "待审核求片", "reqmgr", "reqadmin", "myreqd",
}

# 注意：AsyncIOScheduler 也不能在模块级创建 —— 它同样会在构造时
# asyncio.get_event_loop()，抓到那个永远不会运行的旧循环。
scheduler: AsyncIOScheduler | None = None


# ════════════════════════════════════════════════════════════════════
#  核心修复
# ════════════════════════════════════════════════════════════════════
def bind_loop() -> None:
    """把 Pyrogram 的 client / dispatcher 绑到「当前真正在跑」的事件循环。

    Pyrogram 的 Dispatcher.add_handler() 内部并不是同步挂载，而是：

        self.loop.create_task(fn())     # 异步地把 handler 塞进 dispatcher.groups

    而 self.loop 是 Client(...) 构造那一刻用 asyncio.get_event_loop() 抓下来的
    「旧」循环。asyncio.run() 之后会另建一个新循环，于是所有在 import 阶段注册的
    handler 都被投递到一个永远不会运行的循环上。

    表现就是：机器人能连上 Telegram、命令菜单也注册成功、日志一切正常，
    却对任何消息都毫无反应 —— 因为 dispatcher 里一条 handler 都没有。

    因此必须在 asyncio.run 的循环里先修正 loop，再去导入 / 注册任何 handler。
    """
    loop = asyncio.get_running_loop()
    bot.loop = loop
    bot.dispatcher.loop = loop


_UNKNOWN_CD: dict[int, float] = {}


async def _reply_unknown(msg, uid: int) -> None:
    """私聊里收到不认识的命令 → 回一张带按钮的引导卡片（防刷屏 60 秒一次）"""
    import time as _t

    from pyrogram.enums import ParseMode
    from pyrogram.types import InlineKeyboardButton as Btn
    from pyrogram.types import InlineKeyboardMarkup as KMk

    now = _t.time()
    if now - _UNKNOWN_CD.get(uid, 0) < 60:
        return
    _UNKNOWN_CD[uid] = now
    lines = [
        "🤔 <b>没认出这个命令</b>",
        "━━━━━━━━━━━━━━━━━━",
        "最常用的几个入口在下面，点一下就行：",
        "　🔍 <code>/qp 片名</code>　求片（也可以直接打片名）",
        "　📋 <code>/myreqs</code>　我的求片进度",
        "　📥 <code>/dl</code>　下载进度",
        "　⚡ <code>/power</code>　发电规则与价格（Emby）",
        "　📺 <code>保号</code>　Emby 服保号规则（免绑定可查）",
        "　🎟 <code>/exchange</code>　兑换阁",
        "━━━━━━━━━━━━━━━━━━",
        "💡 完整清单点下面「📜 全部命令」。",
    ]
    try:
        await msg.reply(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=KMk([
                [Btn("🔍 怎么求片", callback_data="qhhelp:qp"),
                 Btn("📋 我的求片", callback_data="qpmine")],
                [Btn("⚡ 发电规则", callback_data="pwr:show"),
                 Btn("📺 Emby保号规则", callback_data="pwr:embyka")],
            ]))
    except Exception as e:
        logger.debug(f"未知命令引导发送失败: {e}")


# 即时反馈：待删除的回执消息（key = (chat_id, msg_id)）
_ACKS: dict = {}
# 已注册的即时反馈 handler（诊断用）
_ACK_HOOKS: list = []


def _install_catch_all() -> None:
    """兜底 + 延迟观测。

    只在「私聊里收到了本机器人不认识的命令」时告警，并且顺带记录消息从
    Telegram 发出到处理完成的总耗时，用于定位卡顿。
    """
    async def _catch_all(client, msg):
        # 活跃度：群里任何「真人」发言都记一笔（命令也算），用于兑换门槛。
        # 后台线程写库，绝不阻塞事件循环（之前响应慢的教训）。
        chat_type = str(getattr(msg.chat, "type", "")).upper()
        u = msg.from_user
        if "GROUP" in chat_type and u and not u.is_bot:
            try:
                asyncio.create_task(
                    asyncio.to_thread(db.bump_activity, int(u.id), "msg"))
            except Exception:
                pass

        txt = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
        if not txt.startswith(("/", "!")):
            return
        cmd = txt[1:].split()[0].split("@")[0].lower()
        uid = msg.from_user.id if msg.from_user else "?"
        is_private = "PRIVATE" in str(getattr(msg.chat, "type", "")).upper()

        # 端到端延迟：Telegram 发出 -> 本处理器执行完
        if msg.date:
            lag = time.time() - msg.date.timestamp()
            if lag > 8:
                logger.warning(f"⏱ 消息处理耗时 {lag:.1f}s（from {uid}: {txt[:40]}）")

        if cmd in _KNOWN:
            return
        if is_private:
            logger.warning(f"未识别命令 from {uid}: {txt[:80]}")
            # ★ 私聊体验（2026-10-13）：以前「没反应」最容易被当成机器人坏了。
            #   现在私聊里敲了不存在的命令，给一张能点的小卡片把人接住。
            #   只认「纯 ascii 命令名」—— 中文命令多半是已注册的别名，
            #   宁可漏回也不能乱回，免得把正常流程打断。
            if re.fullmatch(r"[a-z0-9_]{1,32}", cmd):
                await _reply_unknown(msg, uid)

    async def _cb_help(client, cb):
        """引导卡片上的「怎么求片」：给一句能直接照做的话"""
        tips = {
            "qp": "直接发片名就行，比如「流浪地球」；\n"
                  "也可以 /qp 流浪地球 一步到位。",
        }
        await cb.answer(tips.get(cb.data.split(":")[1], ""), show_alert=True)

    bot.add_handler(CallbackQueryHandler(_cb_help, filters.regex(r"^qhhelp:\w+$")),
                    group=1)
    # ── 即时反馈（2026-10-16 用户要求）───────────────────────────────
    #   痛点：发 /now、/dayrank 等耗时命令后半天没动静，用户以为机器人失联。
    #   现在命令一收到立刻回一条「⏳ 正在查询…」，跑完自动删掉，只留最终结果。
    #   实现：group=0 前置 handler 先发回执并登记；group=9 收尾 handler 删回执。
    _SLOW_CMDS = {
        "now": "正在查询今日观看…", "today": "正在查询今日观看…",
        "dayrank": "正在生成今日榜单…", "day": "正在生成今日榜单…",
        "weekrank": "正在生成本周榜单…", "week": "正在生成本周榜单…",
        "周榜": "正在生成本周榜单…",
        "monthrank": "正在生成本月榜单…", "month": "正在生成本月榜单…",
        "月榜": "正在生成本月榜单…",
        "top": "正在生成榜单…", "榜单": "正在生成榜单…",
        "rankboard": "正在生成榜单…",
        "search": "正在搜索…", "搜索": "正在搜索…",
        "qp": "正在提交求片…", "求片": "正在提交求片…",
        "bind": "正在查询绑定状态…",
        "ka": "正在查询保号状态…", "保号": "正在查询保号状态…",
        "发电": "正在查询发电状态…",
        "session": "正在查询 Emby 在线播放…", "emby": "正在查询 Emby 状态…",
        "sortpath": "正在归类种子…", "sort": "正在归类种子…",
        "mp": "正在查询 MoviePilot…", "qb": "正在查询 qBittorrent…",
        "duihuan": "正在查询兑换信息…", "dh": "正在查询兑换信息…",
        "exchange": "正在打开兑换阁…", "ex": "正在打开兑换阁…",
    }

    def _akey(m):
        return (getattr(m.chat, "id", 0), getattr(m, "id", 0))

    async def _instant_ack(client, msg):
        """group=0 前置：耗时命令立刻回执并登记（key = chat_id,msg_id）。"""
        try:
            txt = (getattr(msg, "text", None) or "").strip()
            if not txt.startswith(("/", "!")):
                return
            cmd = txt[1:].split()[0].split("@")[0].lower()
            hint = _SLOW_CMDS.get(cmd)
            if not hint:
                return
            ack = await client.send_message(msg.chat.id, f"⏳ {hint}")
            key = _akey(msg)
            _ACKS[key] = ack
            # 兜底：有些命令是后台 async 发结果的，group=99 收尾时还没删掉回执，
            #  60 秒后强制清掉，绝不在群里留「正在查询…」的僵尸消息。
            async def _reap(k=key, a=ack):
                try:
                    await asyncio.sleep(60)
                    if _ACKS.pop(k, None) is a:
                        await a.delete()
                except Exception:
                    pass
            asyncio.create_task(_reap())
        except Exception:
            pass

    async def _ack_done(client, msg):
        """group=99 收尾：命令处理完删回执。"""
        ack = _ACKS.pop(_akey(msg), None)
        if ack is None:
            return
        try:
            await ack.delete()
        except Exception:
            try:
                await ack.edit_text("✅")
            except Exception:
                pass

    # group=-1 / 99：确保回执一定先于业务 handler（业务用 0/1/2），
    # 收尾一定晚于所有业务 handler（含 async 补发的结果消息）。
    bot.add_handler(MessageHandler(_instant_ack, filters.all), group=-1)
    bot.add_handler(MessageHandler(_ack_done, filters.all), group=99)
    _ACK_HOOKS.extend([_instant_ack, _ack_done])

    # ── 按钮即时反馈 ────────────────────────────────────────────────
    #   点「刷新 / 换页 / 榜单切换」同样要等（榜单要拉群成员+头像），
    #   立刻把按钮所在消息改成「⏳ 更新中…」，业务 handler 完成后它会
    #   自己 edit 回正式内容，这里只负责抢在前面给出反馈。
    _CB_HINTS = (
        (re.compile(r"^now:"), "正在刷新在线播报…"),
        (re.compile(r"^rk:"), "正在生成榜单…"),
        (re.compile(r"^(ex|exg):"), "正在打开兑换阁…"),
        (re.compile(r"^(dhx|dhv):"), "正在兑换…"),
        (re.compile(r"^(sort|sync):"), "正在处理…"),
        (re.compile(r"^req:"), "正在处理求片…"),
        (re.compile(r"^pt:"), "正在查询…"),
    )

    async def _cb_instant(client, cb):
        try:
            data = cb.data or ""
            hint = None
            for rx, h in _CB_HINTS:
                if rx.match(data):
                    hint = h
                    break
            if not hint:
                return
            await cb.answer(hint)          # 顶部 toast，1.5s 自动消失
            # 同时把按钮所在消息改成「⏳ …」，业务 handler 完成后会自己
            # edit 回正式内容，所以这里不留任何待删状态。
            try:
                await cb.message.edit_text(f"⏳ {hint}")
            except Exception:
                pass
        except Exception:
            pass

    bot.add_handler(CallbackQueryHandler(_cb_instant, filters.all), group=-1)
    _ACK_HOOKS.append(_cb_instant)

    bot.add_handler(MessageHandler(_catch_all, filters.all), group=1)


async def register_handlers() -> int:
    """在正确的循环内导入命令模块（导入即注册 handler），并返回挂载数量。"""
    # 导入即注册，顺序无关  # noqa: F401
    from .modules import (bind, contrib, game, gaps, inbox, keepalive, mptask,  # noqa: F401
                          perm, power, rank, redeem, release, reqmgr, request,
                          social, submgr, svc, changelog, unibot, kkc, mailbox,
                          selftest, bind115, account)  # noqa: F401

    _install_catch_all()

    # add_handler 是异步写入的，最多等 3 秒让它落位
    for _ in range(30):
        await asyncio.sleep(0.1)
        if bot.dispatcher.groups:
            break
    return sum(len(h) for h in bot.dispatcher.groups.values())


async def setup_commands() -> None:
    """按权限下发命令菜单。

    ★ 关键：管理员**没被授权的模块**，他的私聊菜单里一条都不会出现；
      owner 专属命令（MP/qB/Emby/MT/CD2）在群聊 scope 里压根不注册，
      ⇒ 群友点开机器人菜单永远看不到它们。
    """
    from pyrogram.types import BotCommandScopeChat

    from .modules import perm
    pub = [BotCommand(c, d) for c, d in perm.public_cmds()]
    ok = 0
    for label, cmds, scope in (
        ("默认", pub, None),
        ("私聊", pub, BotCommandScopeAllPrivateChats()),
        ("群聊", pub, BotCommandScopeAllGroupChats()),
    ):
        try:
            if scope is None:
                await bot.set_bot_commands(cmds)
            else:
                await bot.set_bot_commands(cmds, scope=scope)
            ok += 1
        except Exception as e:
            logger.warning(f"命令注册失败({label}): {e}")

    # 给 owner 和每个管理员的私聊单独下发「他有权用的那份」
    per = 0
    for uid in [int(S.OWNER or 0)] + [int(a["tg"]) for a in db.admins()]:
        if not uid:
            continue
        try:
            mine = [BotCommand(c, d) for c, d in perm.cmds_for(uid)]
            await bot.set_bot_commands(mine, scope=BotCommandScopeChat(chat_id=uid))
            per += 1
        except Exception as e:
            logger.debug(f"按人下发命令失败 {uid}: {e}")
    logger.info(f"命令菜单注册完成（{ok}/3 作用域 各 {len(pub)} 条公开命令"
                f"；另按人下发 {per} 份）")


async def refresh_command_scopes() -> None:
    """管理员增删/授权变更后，重刷按人下发的菜单"""
    from pyrogram.types import BotCommandScopeChat

    from .modules import perm
    try:
        await bot.set_bot_commands(
            [BotCommand(c, d) for c, d in perm.public_cmds()],
            scope=BotCommandScopeAllPrivateChats())
        await bot.set_bot_commands(
            [BotCommand(c, d) for c, d in perm.public_cmds()],
            scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"公开菜单刷新失败：{e}")
    for a in db.admins():
        try:
            uid = int(a["tg"])
            mine = [BotCommand(c, d) for c, d in perm.cmds_for(uid)]
            await bot.set_bot_commands(mine, scope=BotCommandScopeChat(chat_id=uid))
        except Exception as e:
            logger.debug(f"刷新菜单失败 {a.get('tg')}: {e}")


def setup_jobs() -> None:
    """必须在事件循环内调用（AsyncIOScheduler 也要绑到正确的 loop）"""
    global scheduler
    from .modules import rank

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    rp = S.RANK_PUSH

    for t in rp.get("day_time", ["18:30"]):
        h, m = t.split(":")
        scheduler.add_job(
            rank.push_rank,
            CronTrigger(hour=int(h), minute=int(m), timezone="Asia/Shanghai"),
            args=[bot, 1, f"今日观看榜 · {S.SERVER_NAME}"],
            id=f"rank_day_{t}", replace_existing=True,
        )
    wd = rp.get("week_day", "sun")
    h, m = rp.get("week_time", "23:30").split(":")
    scheduler.add_job(
        rank.push_rank,
        CronTrigger(day_of_week=wd, hour=int(h), minute=int(m), timezone="Asia/Shanghai"),
        args=[bot, 7, f"本周观看榜 · {S.SERVER_NAME}"],
        id="rank_week", replace_existing=True,
    )
    h, m = rp.get("month_time", "10:00").split(":")
    scheduler.add_job(
        rank.push_rank,
        CronTrigger(day=int(rp.get("month_day", 1)), hour=int(h), minute=int(m),
                    timezone="Asia/Shanghai"),
        args=[bot, 30, f"本月观看榜 · {S.SERVER_NAME}"],
        id="rank_month", replace_existing=True,
    )
    logger.info(f"定时榜单已注册：日 {rp.get('day_time')} / 周 {wd} {rp.get('week_time')} "
                f"/ 月 {rp.get('month_day')}号 {rp.get('month_time')}")

    # ★ 每日观看 → V积分 转化 + 群广播：每天 23:30 结算当日观影时长并发群播报
    from .modules import watch_iv
    scheduler.add_job(
        watch_iv.watch_iv_daily,
        CronTrigger(hour=23, minute=30, timezone="Asia/Shanghai"),
        args=[bot], id="watch_iv_daily", replace_existing=True, max_instances=1,
    )
    logger.info("每日观看→V积分 已注册：每天 23:30")

    # ★ 多端到期对账：每 15 分钟把 boss 账本到期同步给 RT
    #   （Boss /renew、Game 续期码等任意一端续期后，RT 面板自动对齐）
    from .modules import unibot
    scheduler.add_job(
        unibot.sync_ex_to_rt_job,
        "interval", minutes=15,
        id="ex_sync_rt", replace_existing=True, max_instances=1,
    )
    logger.info("多端到期对账已注册：每 15 分钟（boss→RT 续期补齐）")

    # PT 播报：30s 消费一次 pt2cloud 的通知队列
    from .modules import inbox
    scheduler.add_job(
        inbox.poll_notifies,
        "interval", seconds=30,
        id="pt_inbox", replace_existing=True, max_instances=1,
    )
    # 进度条原地刷新（不新增消息，只编辑）
    scheduler.add_job(
        inbox.poll_edits,
        "interval", seconds=20,
        id="pt_edit", replace_existing=True, max_instances=1,
    )
    # /dl 实时面板自动刷新
    scheduler.add_job(
        inbox.refresh_live,
        "interval", seconds=20,
        id="pt_live", replace_existing=True, max_instances=1,
    )
    # ★ PT 任务面板（/ptasks）自动刷新：进度条自己走，不用一直点「🔄 刷新」。
    #   与 /dl 面板各自维护一份 _LIVE 登记表，互不干扰，所以必须**单独注册**；
    #   漏了这一步，面板上的进度条就只会在手动刷新时才动。
    from .modules import mptask
    scheduler.add_job(
        mptask.refresh_live,
        "interval", seconds=20,
        id="tk_live", replace_existing=True, max_instances=1,
    )
    logger.info("PT 播报已注册：通知 30s / 进度刷新 20s / 面板刷新 20s（含任务面板）")
    # ★ 2026-10-17 下载优先级守护：群友求片(一级) > 手动下载(二级) > 其他(三级)。
    #   一级/二级有下载任务时自动暂停其他，跑完自动恢复；保号种子免疫永不动。
    from .modules import prioq
    scheduler.add_job(
        prioq.tick,
        "interval", seconds=60,
        id="dl_prioq", replace_existing=True, max_instances=1,
    )
    logger.info("下载优先级守护已注册：求片>手动>其他，60s 调度（保号种子免疫）")

    # 群版 PT 日报：每天 21:05 发群（只含「今日求片 + 入库成功」；
    # pt2cloud 的保号日报含上传/下载/分享率/磁盘，那份只私聊 owner）
    scheduler.add_job(
        inbox.push_daily_report,
        CronTrigger(hour=21, minute=5, timezone="Asia/Shanghai"),
        id="pt_daily_group", replace_existing=True, max_instances=1,
    )
    logger.info("群版 PT 日报已注册：每天 21:05")

    # ★ PT 保号（发电）：登录保活 + 状态探测 + 辅种 + 群说明同步
    from .modules import keepalive
    keepalive.init()
    scheduler.add_job(
        keepalive.tick, "interval", minutes=30,
        id="keepalive_tick", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        keepalive.seed_tick, "interval", minutes=20,
        id="keepalive_seed", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        keepalive.group_sync, "interval", hours=6,
        id="keepalive_grp", replace_existing=True, max_instances=1,
    )
    logger.info("PT 保号已注册：保活 30min / 辅种 20min / 群说明 6h（保号=PT管理员命令；发电=Emby公开命令已生效）")

    # 站点暂无资源巡检：已下发订阅但久无进度的求片，主动到群里说明情况。
    # ★ 5 分钟一轮即可 —— reqmgr.check_idle_subs 内部还有 5 分钟防抖，
    #   且每条只播报一次（db.idle_notified_at 标记），不会刷屏。
    from .modules import reqmgr
    scheduler.add_job(
        reqmgr.check_idle_subs,
        "interval", minutes=5,
        id="pt_idle", replace_existing=True, max_instances=1,
    )
    logger.info(f"暂无资源巡检已注册：每 5 分钟（超过 "
                f"{reqmgr.IDLE_MINUTES} 分钟无进度即播报）")

    # 下载盘自动归类：把新种子搬到 /客户端/站点/{求片|订阅}/{大类}/{细分}
    # 3 分钟一轮足够 —— MP 订阅后几秒内就会把种子加进 QB，
    # 等一轮再搬，避免和 QB 正在写入的文件抢盘。
    from .modules import sortpath
    scheduler.add_job(
        sortpath.auto_sort,
        "interval", seconds=180,
        id="pt_sortpath", replace_existing=True, max_instances=1,
    )
    logger.info(f"下载盘归类已注册：每 3 分钟扫描一次"
                f"（{{求片|订阅}}/{{站点}}/{len(sortpath.TOP_CLASSES)} 大类）")
    logger.info("站点目录已与 MP 的站点表对齐：新增站点会自动建目录，"
                "也可发 /syncsite 手动触发")


async def main():
    logger.info("═" * 46)
    logger.info(f" {S.SERVER_NAME} · 修仙 bot 启动")
    logger.info("═" * 46)

    bind_loop()          # ★ 必须在导入任何命令模块 / 创建 scheduler 之前
    db.init()

    await bot.start()
    me = await bot.get_me()
    logger.info(f"已连接 Telegram：@{me.username}")

    n = await register_handlers()
    logger.info(f"已挂载消息处理器：{n} 个")
    if n == 0:
        logger.error("❌ 处理器挂载失败，机器人不会响应任何消息！")

    await setup_commands()

    from .helper import warmup
    if not await warmup(bot):
        logger.warning("⚠️ 主群 peer 预热失败，群内功能可能不可用。"
                       "请确认机器人已加入主群并被设为管理员。")

    async def _prefetch_members():
        """启动时后台预拉群成员，让第一次查榜不用临时等几十次 TG 请求。

        拉完顺手跑一次 auto_bind()：Emby 用户名能对上群里 TG 用户名的直接落库，
        这样绝大多数人不用做任何操作，榜单上名字就能点开个人主页。
        """
        try:
            from .modules import rank
            await asyncio.sleep(2)
            await rank.load_members(bot)
            r = await asyncio.to_thread(rank.auto_bind)
            if r["ok"]:
                logger.info(f"启动自动关联 Emby 名 → TG：{r['ok']} 条")
        except Exception as e:
            logger.warning(f"群成员预热/自动关联失败: {e}")

    asyncio.create_task(_prefetch_members())
    # Web→bot 信箱：私发群主的备份播报/跨系统通知（60s 轮询）
    try:
        from .modules import mailbox as _mb
        _mb.ensure_loop()
    except Exception as e:
        logger.info(f"信箱启动失败: {e}")

    # 启动后先归类一次，把存量落错目录的种子立刻摆正，不用等第一个 3 分钟周期
    async def _first_sort():
        try:
            await asyncio.sleep(12)      # 等 MP/QB 完全就绪
            from .modules import sortpath
            sortpath.sync_site_dirs()   # 建/对齐站点目录（旧版叫 ensure_dirs）
            await sortpath.auto_sort()
        except Exception as e:
            logger.warning(f"启动归类失败（不影响下载，等下一轮自动重试）: {e}")

    asyncio.create_task(_first_sort())

    setup_jobs()
    scheduler.start()

    # 保号群说明：启动 10 秒后立即同步一次（不用等 6 小时的定时轮）
    async def _ka_desc_once():
        try:
            await asyncio.sleep(10)
            from .modules import keepalive as _ka
            await _ka.group_sync()
        except Exception as e:
            logger.debug(f"保号群说明首同步失败: {e}")
    asyncio.create_task(_ka_desc_once())

    # ★ 发布即汇报（用户 2026-10-13 要求）：
    #   每次升级上线后，自动把「升级要点 + 真跑示例」私聊给群主本人。
    #   同一版本号只发一次（release.py 里有标记文件），重启不会刷屏。
    async def _release_once():
        try:
            await asyncio.sleep(25)     # 等示例里要用到的 qB/MP/Emby 都就绪
            from .modules import release
            await release.send_release()
        except Exception as e:
            logger.debug(f"升级汇报失败（不影响主流程）: {e}")
    asyncio.create_task(_release_once())

    # ★ 版本升级公告（GitHub Release 风格，数据驱动）：
    #   读 keepalive/changelog.json（rw 挂载，改公告无需重新部署），
    #   版本比 state 里记录的更新时 → 私聊群主一张精美公告卡。
    async def _changelog_once():
        try:
            await asyncio.sleep(30)
            from .modules import changelog
            await changelog.maybe_announce()
        except Exception as e:
            logger.debug(f"版本公告失败（不影响主流程）: {e}")
    asyncio.create_task(_changelog_once())

    # ★ 全命令自检（用户要求：每次部署后跑一遍所有命令，结果私聊群主 + 写 Web 日志）。
    #   检测到 rw 挂载上的 .selftest_pending 标记即自动跑；也可群主发 /selftest 手动触发。
    async def _selftest_once():
        try:
            await asyncio.sleep(35)     # 等 Emby/MP/QB/TG 成员等依赖就绪
            from .modules import selftest
            await selftest.maybe_selftest(bot)
        except Exception as e:
            logger.debug(f"自检启动钩子失败: {e}")
    asyncio.create_task(_selftest_once())

    await idle()
    scheduler.shutdown(wait=False)
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
