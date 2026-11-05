"""每日观看 → V积分 转化 + 群广播（★ 全群唯一结算源，Boss 侧重复结算已关停）

逻辑：
  · 取 Emby PlaybackActivity 最近 24h 的真实观看时长（PlayDuration - PauseDuration，秒）
  · 按 Emby UserId 聚合为分钟，经 embyboss.emby.embyid → tg 反查到 TG 用户
  · 观看 >= 10 分钟者，V积分 = 分钟 // 30（每 30 分钟 1 分，上不封顶——用户 2026-11-05 拍板）
  · 写入 embyboss.emby.iv（db.add_iv），并向主群播报

Emby 播放库查询复用 rank._custom（Playback Reporting 自定义查询接口），不另起连接。
"""
from __future__ import annotations

import html
import logging

from loguru import logger
from pyrogram.enums import ParseMode
from sqlalchemy import text

from .. import db, settings as S
from ..helper import send_group
from .rank import _custom

THRESHOLD_MIN = 10          # 最少观看分钟数，才有资格
PER_BLOCK_MIN = 30          # 每 30 分钟换 1 分
DAILY_CAP = 0               # ★ 2026-11-05 用户拍板：0 = 每日上限不限，一直看一直赚
WINDOW_DAYS = 1             # 统计窗口（天）


def _embyid_to_tg() -> dict[str, int]:
    """embyboss.emby.embyid -> tg 反查表（统一的三端互通台账）。"""
    m: dict[str, int] = {}
    try:
        with db._engine.connect() as c:
            rows = c.execute(text(
                f"SELECT tg, embyid FROM `{S.SHARED_DB}`.emby "
                "WHERE embyid IS NOT NULL AND embyid <> ''")).all()
            for tg, eid in rows:
                if eid:
                    m[str(eid)] = int(tg)
    except Exception as e:
        logger.warning(f"读取 embyid→tg 映射失败: {e}")
    return m


def _watch_minutes_by_user() -> list[dict] | None:
    """最近 24h 每个 Emby 用户的真实观看分钟数。"""
    sql = (
        "SELECT UserId AS u, SUM(PlayDuration - PauseDuration) AS d "
        "FROM PlaybackActivity "
        f"WHERE DateCreated >= datetime('now', '-{WINDOW_DAYS} day') "
        "AND UserId NOT IN (SELECT UserId FROM UserList) "
        "GROUP BY UserId"
    )
    rows = _custom(sql)
    if rows is None:
        return None
    return rows


def _norm(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


async def watch_iv_daily(bot) -> None:
    """每日 23:30 执行：把当日观看时长折算成 V积分 并发群播报。"""
    try:
        emap = _embyid_to_tg()
        rows = _watch_minutes_by_user()
        if rows is None:
            logger.warning("每日观看→V积分：播放库不可用，跳过")
            return
        from .rank import user_map
        uname = user_map()

        stats: list[dict] = []          # 每位达标用户的明细（用于排行/播报）
        total_issued = 0
        for r in rows:
            uid = str(r.get("u") or "")
            mins = _norm(r.get("d")) / 60.0
            if mins < THRESHOLD_MIN:
                continue
            tg = emap.get(uid)
            if not tg:
                # 该 Emby 账号未绑定到任何 TG，无法发分，跳过
                continue
            v = int(mins) // PER_BLOCK_MIN
            if DAILY_CAP > 0:
                v = min(v, DAILY_CAP)
            if v <= 0:
                continue
            try:
                db.add_iv(tg, v)
            except Exception as e:
                logger.warning(f"每日观看→V积分：tg={tg} 入账失败: {e}")
                continue
            total_issued += v
            name = (uname.get(uid) or "?")
            stats.append({"tg": tg, "name": name, "minutes": mins, "v": v})

        watchers = len(stats)
        stats.sort(key=lambda x: x["minutes"], reverse=True)

        lines = [
            "📺 <b>每日观影 · V积分结算</b>",
            "━━━━━━━━━━━━",
            f"👥 达标观影人数｜<b>{watchers}</b> 人（≥{THRESHOLD_MIN} 分钟）",
            f"🎁 发放 V积分｜<b>{total_issued}</b> 分（每 {PER_BLOCK_MIN} 分钟 1 分"
            + (f"，每人日上限 {DAILY_CAP}）" if DAILY_CAP > 0 else "，上不封顶）"),
        ]
        if stats:
            lines.append("🏆 <b>今日观影 TOP3</b>")
            for i, s in enumerate(stats[:3], 1):
                lines.append(
                    f"{i}. {html.escape(str(s['name']))}｜{s['minutes']:.0f} 分钟 → +{s['v']} V")
        lines.append("━━━━━━━━━━━━")
        lines.append("💡 签到只产出 <b>灵石</b>（量级很小）；认真看片才赚 <b>V积分</b> —— 边看边攒。")

        text = "\n".join(lines)
        try:
            await send_group(bot, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"每日观看→V积分：群播报失败: {e}")

        logger.info(
            f"每日观看→V积分 完成：达标 {watchers} 人，发放 {total_issued} V积分")
    except Exception as e:
        logger.exception(f"每日观看→V积分 异常: {e}")
