"""mailbox —— Web(media-hub) → bot 的文件信箱（60 秒轮询）。

Web 侧把要私聊群主的内容写进 /app/keepalive/web_inbox.json（rw 挂载，
Web 用 /dockroot/kfcgame/keepalive/ 同一份文件），bot 轮询发现新条目就
私发 OWNER 并回执处理结果。备份播报、告警补发、跨系统通知都走这里。

文件格式：
  {"items": [{"id": "bk-20261015", "text": "<html>", "md": false, "ts": 179...}]}
bot 处理完把 item id 记到 _seen 并**重写文件删掉已处理项**（幂等：同 id 只发一次）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from loguru import logger

from .. import settings as S

FILE = os.path.join(os.path.dirname(os.path.abspath(S.__file__)),
                    "keepalive", "web_inbox.json")
# 与 media-hub 约定的真实路径：容器内 /app/keepalive/web_inbox.json
FILE = "/app/keepalive/web_inbox.json"

_SEEN: set[str] = set()
_LOOP_TASK = None


async def _send_owner(text: str, as_md: bool = False) -> bool:
    from .. import bot
    from .. import ui
    try:
        # Web 侧统一拼 HTML 标签；as_md 仅用于需要纯文本转义的场景
        clipped = ui.clip_html(text)
        await bot.send_message(int(S.OWNER), clipped, parse_mode="HTML",
                               disable_web_page_preview=True)
        return True
    except Exception as e:
        logger.debug(f"mailbox 发送失败: {e}")
        try:
            await bot.send_message(int(S.OWNER), text[:4000])
            return True
        except Exception:
            return False


def _read_items() -> tuple[list[dict], dict]:
    try:
        with open(FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return list(raw.get("items") or []), raw
    except FileNotFoundError:
        return [], {}
    except Exception as e:
        logger.debug(f"mailbox 读取失败: {e}")
        return [], {}


def _remove_items(ids: list[str]) -> None:
    """把已处理的条目删掉后回写（保留未处理的）。"""
    try:
        items, raw = _read_items()
        left = [x for x in items if str(x.get("id")) not in set(ids)]
        if len(left) == len(items):
            return
        raw["items"] = left
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, FILE)
    except Exception as e:
        logger.debug(f"mailbox 回写失败: {e}")


async def _run_cmd(cmd: str, it: dict) -> bool:
    """执行 Web 侧下发的动作条目。目前只有 ka_refresh（立刻重抓保号/魔力值）。"""
    try:
        if cmd == "ka_refresh":
            from . import keepalive as KA
            await KA.refresh_now()
            await _send_owner(
                "⚡ <b>已重抓保号数据</b>：各站魔力值 / 上传下载 / 分享率 已刷新。")
            return True
        logger.debug(f"信箱：未知命令 {cmd}")
        return False
    except Exception as e:
        logger.debug(f"信箱命令 {cmd} 执行失败: {e}")
        return False


async def poll_once() -> int:
    """扫一轮信箱。返回本轮发出条数。"""
    items, _ = _read_items()
    if not items:
        return 0
    sent = []
    for it in items:
        iid = str(it.get("id") or "")
        if not iid or iid in _SEEN:
            continue
        # ★ 2026-10-16：支持「动作」条目 —— Web 点「刷新魔力值」时写 {"cmd":"ka_refresh"}，
        #   bot 立刻重抓一轮保号数据（而不是干等 30 分钟的定时 tick）。
        cmd = str(it.get("cmd") or "")
        if cmd:
            ok = await _run_cmd(cmd, it)
        else:
            ok = await _send_owner(str(it.get("text") or ""), bool(it.get("md")))
        _SEEN.add(iid)                    # 无论成否都只试一次，防死循环
        if ok:
            sent.append(iid)
        if len(_SEEN) > 500:
            _SEEN.clear()
    if sent:
        _remove_items(sent)
    return len(sent)


async def loop() -> None:
    logger.info("Web 信箱循环启动（60s）")
    while True:
        try:
            n = await poll_once()
            if n:
                logger.info(f"Web 信箱：私发群主 {n} 条")
        except Exception as e:
            logger.debug(f"信箱异常: {e}")
        await asyncio.sleep(60)


def ensure_loop() -> None:
    """由 main 启动时调用一次。"""
    global _LOOP_TASK
    if _LOOP_TASK is None:
        _LOOP_TASK = asyncio.create_task(loop())
