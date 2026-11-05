# -*- coding: utf-8 -*-
"""版本升级播报（GitHub Release 风格）

数据源：`KA_DIR/changelog.json`（rw 挂载，改它**免部署**）。结构：
    {
      "version": "1.4.0",              # 当前最新版
      "date": "2026-10-14",
      "title": "保号统计大修复 + 求片集数精准",
      "summary": "一句话副标题",
      "sections": [
        {"icon": "🐛", "name": "修复", "items": ["……", "……"]},
        {"icon": "✨", "name": "新增", "items": ["……"]}
      ]
    }

行为：bot 启动时读取 changelog.json，与 state 里记录的「已向群主展示过的版本」
比对；若是更新版本，则私聊 OWNER 推送一张排版精美的公告卡片，并记录已展示，
避免每次重启重复刷屏。Web（media-hub）读同一份文件在「更新公告」面板展示。
"""
from __future__ import annotations

import json
import os
import time

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from .. import bot
from .. import settings as S
from .. import ui
from . import keepalive as KA

CHANGELOG_PATH = os.path.join(KA.KA_DIR, "changelog.json")
# 记录「已播报到哪一版」，写在 state.json 顶层（与站点状态同文件，互不冲突）
_seen_key = "changelog_shown"


def load_changelog() -> dict:
    try:
        with open(CHANGELOG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("version") else {}
    except Exception:
        return {}


def _ver_tuple(v: str) -> tuple:
    """'1.4.0' → (1,4,0)；非法段按 0 处理，比较时缺位补 0。"""
    out = []
    for p in str(v or "").split(".")[:3]:
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def render_card(d: dict) -> str:
    """changelog dict → Telegram HTML 公告卡片（GitHub Release 风格）。"""
    ver = str(d.get("version") or "")
    date = str(d.get("date") or "")
    title = str(d.get("title") or "")
    summary = str(d.get("summary") or "")
    lines = [
        f"🚀 <b>系统更新 · v{ver}</b>",
        ui.divider(),
    ]
    if title:
        lines.append(f"<b>{ui.esc(title)}</b>")
    if summary:
        lines.append(f"<i>{ui.esc(summary)}</i>")
    lines.append(ui.divider())
    for sec in d.get("sections") or []:
        icon = str(sec.get("icon") or "•")
        name = str(sec.get("name") or "")
        lines.append(f"{icon} <b>{ui.esc(name)}</b>")
        for it in sec.get("items") or []:
            lines.append(f"　· {ui.esc(str(it))}")
        lines.append("")
    lines = [x for x in lines if x != ""] + [""]
    tail = []
    if date:
        tail.append(f"📅 {date}")
    tail.append("详情见 media-hub「更新公告」面板")
    lines.append(ui.divider())
    lines.append("　".join(tail))
    return ui.clip_html("\n".join(lines), limit=3900)


async def maybe_announce() -> None:
    """启动时：若 changelog 版本与「已播报版本」不同 → 私聊群主推送 + 记录。
    用字符串不等判定（而非数值比较）：这样 2026.10.14-a→-b 这种后缀版本
    也能各播报一次；改了公告文案想重新推送，只要改 version 字段即可。"""
    d = load_changelog()
    if not d:
        return
    ver = str(d.get("version") or "")
    owner = int(S.OWNER or 0)
    if not owner or not ver:
        return
    try:
        st = KA.load_state()
        shown = str(st.get(_seen_key) or "")
        if shown == ver:
            return  # 已经播报过完全相同的版本
        await bot.send_message(owner, render_card(d),
                               parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)
        st[_seen_key] = ver
        st[_seen_key + "_ts"] = time.time()
        KA.save_state(st)
        logger.info(f"版本升级公告已推送群主：v{ver}")
    except Exception as e:
        logger.warning(f"版本公告推送失败: {e}")


@bot.on_message(filters.command(["announce", "重发公告", "更新公告"],
                                prefixes=S.PREFIX))
async def cmd_announce(client, msg: Message):
    """管理员手动重发当前版本公告（强制，不受版本去重限制）。"""
    from .request import is_admin
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 仅管理员可用")
        return
    d = load_changelog()
    if not d:
        await msg.reply("⚠️ changelog.json 为空，无公告可发",
                        parse_mode=ParseMode.HTML)
        return
    try:
        await bot.send_message(int(S.OWNER), render_card(d),
                               parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)
        st = KA.load_state()
        st[_seen_key] = str(d.get("version") or "")
        KA.save_state(st)
        await msg.reply("✅ 已重发公告 v%s" % d.get("version"))
    except Exception as e:
        await msg.reply("⚠️ 发送失败：%s" % e)
