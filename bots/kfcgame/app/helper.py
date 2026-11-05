"""通用工具：成员校验、冷却、随机、积分发放"""
from __future__ import annotations

import datetime as dt
import random
from typing import Iterable

from loguru import logger
from pyrogram import Client
from pyrogram.types import Message

from . import settings as S


# TG 侧的「僵尸号」特征：账号被注销/封禁/冻结/拉黑，或已被踢出群。
# 这些号留着只会占名额、领积分不发车，bot 直接拒绝提供服务。
# ★ 只拦这一类 —— 普通未绑定 TG ID的正常用户一律放行（用户 2026-10-09 明确要求）。
_ZOMBIE_EXC = (
    "UserDeactivated", "UserDeactivatedBan", "InputUserDeactivated",
    "UserIsBlocked", "UserBlocked", "UserKicked", "PeerIdInvalid",
    "UserBannedInChannel", "ChannelBanned",
)
_ZOMBIE_STATUS = ("BANNED", "LEFT")


def is_zombie(status: str | None) -> bool:
    """群成员状态是否属于僵尸（已退群/被踢）。"""
    return str(status or "").split(".")[-1] in _ZOMBIE_STATUS


def is_zombie_err(exc: BaseException) -> bool:
    """异常是否表示账号已注销/封禁。"""
    return any(n in type(exc).__name__ for n in _ZOMBIE_EXC)


async def check_zombie(client: Client, msg: Message) -> bool:
    """僵尸号拦截。返回 True = 是僵尸号（已回复提示，应终止处理）。

    只在「群成员状态明确是已退群/被踢」或「TG 明确回报账号已注销」时拦截；
    查不到成员信息（比如私聊场景）一律放行，绝不误伤。
    """
    uid = msg.from_user.id
    if uid == S.OWNER or uid in S.ADMINS:
        return False
    # is_deleted 是 TG 直接给的注销标记
    if getattr(msg.from_user, "is_deleted", False):
        await msg.reply(
            "🧟 **该账号已被 Telegram 注销**，修仙界容不下虚空之人。\n"
            "请换一个正常账号再来。", quote=True)
        return True
    for gid in S.GROUP:
        try:
            m = await client.get_chat_member(gid, uid)
        except Exception as e:
            if is_zombie_err(e):
                await msg.reply(
                    "🧟 **该账号已被 Telegram 封禁或注销**，无法继续使用。\n"
                    "请换一个正常账号再来。", quote=True)
                return True
            continue                       # 查不到 ≠ 僵尸，放行
        if is_zombie(getattr(m, "status", None)):
            await msg.reply(
                "🧟 **该账号已不在群内**，请先回到群里再使用修仙玩法。\n"
                f"👉 加群后回来说话即可解锁全部功能。", quote=True)
            return True
        return False                     # 查到且状态正常，直接放行
    return False


async def in_group(client: Client, uid: int) -> bool:
    """用户是否在主群里（owner/admin 直接放行）"""
    if uid == S.OWNER or uid in S.ADMINS:
        return True
    for gid in S.GROUP:
        try:
            m = await client.get_chat_member(gid, uid)
            if str(m.status).split(".")[-1] in ("ADMINISTRATOR", "MEMBER", "OWNER", "RESTRICTED"):
                return True
        except Exception:
            continue
    return False


async def require_group(client: Client, msg: Message) -> bool:
    #★ 先拦僵尸号（注销/封禁/已退群），再判是否在群 ——
    #   顺序很重要：僵尸号即使不在群，也该收到「账号已注销」而不是「请先加群」
    if await check_zombie(client, msg):
        return False
    if await in_group(client, msg.from_user.id):
        return True
    await msg.reply(
        f"⚠️ 需先加入 **{S.SERVER_NAME}** 官方群才能使用修仙玩法。\n"
        f"👉 加群后回来说话即可解锁全部功能。",
        quote=True,
    )
    return False


def rand(a: int, b: int) -> int:
    return random.randint(min(a, b), max(a, b))


def fmt_delta(mins: float) -> str:
    mins = max(int(mins), 0)
    if mins < 60:
        return f"{mins} 分钟"
    if mins < 1440:
        return f"{mins // 60} 小时 {mins % 60} 分"
    return f"{mins // 1440} 天 {mins % 1440 // 60} 小时"


def cooldown_left(last: dt.datetime | None, minutes: int) -> float:
    """返回还需等待的分钟数，0 表示可用"""
    if not last:
        return 0
    if isinstance(last, str):
        last = dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    delta = dt.datetime.now() - last
    left = minutes - delta.total_seconds() / 60
    return left if left > 0 else 0


def sect_bonus(sect: str, kind: str) -> float:
    v = S.SECTS.get(sect)
    if not v:
        return 0.0
    return v[4] if v[3] == kind else 0.0


def week_period(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def month_period(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    return f"{d.year}-{d.month:02d}"


async def notify(client: Client, uid: int, text: str) -> None:
    """尽量私聊通知，失败就忽略"""
    try:
        await client.send_message(uid, text)
    except Exception as e:
        logger.debug(f"私聊通知失败 {uid}: {e}")


# ────────────────────────────────────────────── 群 peer 预热
_peer_ready = False


async def warmup(client: Client) -> bool:
    """按 username 解析一次主群，把 peer 写入 Pyrogram 本地缓存。

    必须这么做的原因：Pyrogram 的 get_peer_type 只认定 32 位频道 ID，
    新版超群 ID（-100 后面超过 2^31）会被判为非法，只有缓存过才不受影响。
    """
    global _peer_ready
    if _peer_ready:
        return True
    ok = False
    for u in S.GROUP_USERNAMES:
        try:
            chat = await client.get_chat(u)
            logger.info(f"主群已缓存：{chat.title}（{chat.id}）")
            ok = True
        except Exception as e:
            logger.warning(f"按用户名解析主群失败 @{u}：{e}")
    if not ok:
        for gid in S.GROUP:
            try:
                chat = await client.get_chat(gid)
                logger.info(f"主群已缓存：{chat.title}（{chat.id}）")
                ok = True
            except Exception as e:
                logger.warning(f"数字 ID 解析主群失败 {gid}：{e}")
    _peer_ready = ok
    return ok


async def send_group(client: Client, text: str, **kw) -> bool:
    """发送到主群（自动先做 peer 预热）"""
    await warmup(client)
    sent = False
    for gid in S.GROUP:
        try:
            await client.send_message(gid, text, **kw)
            sent = True
        except Exception as e:
            logger.warning(f"群发失败 {gid}: {e}")
    return sent


async def send_group_photo(client: Client, png: bytes, caption: str = "",
                           **kw) -> bool:
    """向主群发一张海报图（PNG 字节），失败时返回 False 由调用方降级"""
    import io                                   # 局部导入：只有发图时才用到
    await warmup(client)
    sent = False
    for gid in S.GROUP:
        try:
            await client.send_photo(gid, io.BytesIO(png), caption=caption, **kw)
            sent = True
        except Exception as e:
            logger.warning(f"群发图片失败 {gid}: {e}")
    return sent
