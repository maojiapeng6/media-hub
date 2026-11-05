"""KFC 修仙 bot —— 包入口，创建 Pyrogram 客户端"""
from __future__ import annotations

import sys

from loguru import logger
from pyrogram import Client
from pyrogram.enums import ParseMode

from . import settings as S

logger.remove()
logger.add(sys.stdout, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")
logger.add("/app/log/kfcgame.log", rotation="20 MB", retention=5, level="DEBUG")

proxy = dict(S.PROXY) if S.PROXY else None

bot = Client(
    "kfcgame",
    api_id=S.API_ID,
    api_hash=S.API_HASH,
    bot_token=S.BOT_TOKEN,
    proxy=proxy,
    parse_mode=ParseMode.MARKDOWN,
    workers=64,
    sleep_threshold=30,
    # 会话文件放到挂载出来的 log 目录，重启后能保留 peer 缓存，也少登录几次
    workdir="/app/log",
)

# ★ 换掉 Pyrogram 自带的富文本解析器（详见 md.py 顶部注释）
# Pyrogram 解析完会remove_surrogates()，把 emoji 从代理对压回单字符，
# 但实体 offset/length 没同步回退⇒ emoji 后面的粗体/斜体/代码全部错位，
# 表现就是「加粗好像没生效」。这里换成自己按 UTF-16 code unit 算offset 的实现，
# 所有发送方法（send_message / edit_text / photo caption）一次性全修好。
from .md import install as _install_md_parser   # noqa: E402

_install_md_parser(bot)
logger.info("富文本解析器已替换为 UTF-16 修正版（修复 emoji 导致加粗错位）")
