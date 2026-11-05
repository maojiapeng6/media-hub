"""发布即汇报：每次升级上线后，自动把「升级要点 + 运行示例」私聊给群主本人

★ 为什么做这个（用户 2026-10-13 原话）
────────────────────────────────────────
「以后所有调试通过 bot 给我本人私聊发送升级要点并运行示例发送给我看成果」

以前每次改完只能自己翻日志、或者让我贴一大段文字到聊天里。
现在改成：**部署完机器人一上线，就自己跑一遍关键功能，把真实输出发给你**。
要点是给人看的说明，示例是**真跑出来的结果**（不是我编的），
一眼就能确认功能到底通没通。

★ 防刷屏
────────────────────────────────────────
只有 RELEASE_VER 变了才发一次，同一个版本重启容器不会再发。
"""
from __future__ import annotations

import asyncio
import os
import time

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KM
from pyrogram.types import Message

from .. import bot
from .. import db
from .. import settings as S
from .. import ui

PM = ParseMode.HTML
CMD = dict(prefixes=S.PREFIX)

# ★ 改版本号才会重发；新增要点时把它 +1
RELEASE_VER = "2026.10.13-b"

_MARK_DIR = "/app/keepalive"
_MARK = f"{_MARK_DIR}/.release_{RELEASE_VER}"

# ══════════════════════════════════════════════════════════ 升级要点
# (图标, 标题, 说明)
NOTES: list[tuple[str, str, str]] = [
    ("🔗", "PT 链接排版修复",
     "站点链接全部改成独占一行的 <a href> 标准格式，说明另起一行 —— "
     "以前「链接（私聊检查账号状态）」整段被 TG 判成无效链接点不动。"),
    ("📖", "规则咨询零门槛",
     "发电规则 / 保号规则免绑定、免权限，任何人发「保号规则」「发电」"
     "直接拿到完整说明，不再强制先绑 Emby。"),
    ("📏", "求片以库内最大文件为基准",
     "同一部片库里常有多个版本，现在会把全部候选都算一遍版本画像，"
     "取<b>体积最大的那个</b>作参考，再推导更高的目标版本，"
     "避免拿 720p 当基准求回一个还不如现有的版本。"),
    ("🎞", "完整画质信息",
     "命中库里已有时，列出分辨率 / 来源 / 编码 / 码率 / HDR / 杜比视界 / "
     "音轨 / 时长 / 体积 / 参考文件名，一眼看清现在有什么。"),
    ("📮", "投稿（新功能）",
     "求片卡片、群播报、私聊回执都加了「📮 投稿资源」按钮；"
     "也可 <code>/投稿</code> 直接投磁力 / 链接 / 电驴 / 文字。"
     "支持补片名、补说明、撤回，一次可投多条。"),
    ("🛎", "投稿审核 + 自动转存",
     "投稿进审核队列（<code>/ctbs</code>），管理员通过后会"
     "<b>自动把资源信息转给 MT 转存机器人</b>；失败自动降级不丢件。"),
    ("🧲", "qB 做种看得见了",
     "<code>/qb</code> 不再只报数量，按状态分组 + 按分类统计，"
     "并列出做种 Top；<code>/qb seed</code> 看每个种子的分享率 / 已传 / 时长。"),
    ("🆕", "发布即汇报",
     "本条消息就是：每次升级上线后自动私聊给你，附真实运行示例。"),
]


# ══════════════════════════════════════════════════════════ 运行示例（真跑）
def _t(s, n: int = 60) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))[:n]


def _div() -> str:
    return "━━━━━━━━━━━━━━━━━━"


def _ex_emby() -> list[str]:
    """示例①：Emby 版本画像（真跑 _emby_hit）"""
    lines = ["① <b>Emby 版本画像</b>（真跑 <code>_emby_hit()</code>）"]
    try:
        from .request import (_emby_hit, _target_of, _ver_profile, _qual_block)
        # 拿一条最近的求片当样本；没有就用库里随便一部
        title, mtype, tid = "", "", ""
        try:
            for a in db.list_audits_all(20):
                if a.get("title"):
                    title, mtype = a["title"], (a.get("mtype") or "")
                    tid = str(a.get("tmdb_id") or "")
                    break
        except Exception:
            pass
        if not title:
            title, mtype = "流浪地球", "电影"
        hit = _emby_hit(title, tid, mtype, 0)
        lines.append(f"　样本：<b>{_t(title, 24)}</b>")
        if hit.get("hit"):
            lines.append(f"　候选版本：<b>{hit.get('cand_n')}</b> 个，"
                         f"取体积最大的作基准")
            for ln in str(hit.get("detail") or "").split("\n"):
                lines.append("　" + ln)
            lines.append(f"　🎯 目标版本：{_t(_target_of(hit), 40)}")
        else:
            lines.append("　库里没有这部片（未命中，走正常求片流程）")
            # 用文件名样例演示画像解析
            v = _ver_profile([
                "The.Wandering.Earth.2019.2160p.UHD.BluRay.REMUX.HDR."
                "HEVC.DTS-HD.MA.7.1-FGT.mkv"], 125)
            lines.append("　（用样例文件名演示画像解析）")
            for ln in _qual_block(v, "（整片）", "The.Wandering.Earth…REMUX.mkv"):
                lines.append("　" + ln)
            lines.append(f"　🎯 目标版本：{_t(_target_of(v), 40)}")
    except Exception as e:
        lines.append(f"　⚠️ 运行失败：{_t(e, 80)}")
    return lines


def _ex_contrib() -> list[str]:
    """示例②：投稿解析（真跑 parse_items）"""
    lines = ["② <b>投稿解析</b>（真跑 <code>parse_items()</code>）"]
    try:
        from .contrib import parse_items, payload_line, _human_size
        demo = ("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01"
                "&dn=The.Wandering.Earth.II.2023.2160p.WEB-DL.H265.DDP5.1"
                "&xl=68719476736")
        items = parse_items(demo)
        if not items:
            lines.append("　⚠️ 没解析出内容")
        for it in items[:2]:
            lines.append(f"　识别类型：<b>磁力链接</b>")
            lines.append(f"　自动片名：{_t(it.get('title'), 40)}")
            lines.append(f"　自动体积：{_t(it.get('size_txt') or _human_size(0), 20)}")
            lines.append("　排版输出（链接独占一行）：")
            lines.append("　　" + (payload_line(it)[0] if payload_line(it) else "")[:70]
                         + "…")
    except Exception as e:
        lines.append(f"　⚠️ 运行失败：{_t(e, 80)}")
    return lines


def _ex_qb() -> list[str]:
    """示例③：qB 做种统计（真跑 qb 接口）"""
    lines = ["③ <b>qB 做种统计</b>（真跑 qB WebAPI）"]
    try:
        from .svc import qb_get, _hum, _spd
        tors = qb_get("/api/v2/torrents/info") or []
        if not tors:
            lines.append("　（当前没有种子）")
            return lines
        seeding = [t for t in tors
                   if str(t.get("state") or "") in
                   ("uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP")]
        up_bytes = sum(int(t.get("uploaded") or 0) for t in tors)
        lines.append(f"　种子总数 <b>{len(tors)}</b>　做种中 <b>{len(seeding)}</b>")
        lines.append(f"　累计上传 <b>{_hum(up_bytes)}</b>")
        top = sorted(seeding, key=lambda t: -int(t.get("uploaded") or 0))[:3]
        for i, t in enumerate(top, 1):
            r = float(t.get("ratio") or 0)
            lines.append(f"　{i}. {_t(t.get('name'), 30)}")
            lines.append(f"　　分享率 {r:.2f}　已传 {_hum(t.get('uploaded'))}"
                         f"　{_t(t.get('category') or '-', 8)}")
        if not top:
            lines.append("　（当前没有正在做种的种子）")
    except Exception as e:
        lines.append(f"　⚠️ 运行失败：{_t(e, 80)}")
    return lines


def _ex_sites() -> list[str]:
    """示例④：站点与保号状态（真读 state.json）"""
    lines = ["④ <b>PT 保号状态</b>（真读 state.json）"]
    try:
        import json
        st = json.load(open("/app/keepalive/state.json", encoding="utf-8"))
        sites = (st.get("sites") or {}) if isinstance(st, dict) else {}
        if not sites:
            lines.append("　（state 为空，等第一轮 tick）")
            return lines
        ok = [k for k, v in sites.items() if v.get("bound")]
        lines.append(f"　已绑定 <b>{len(ok)}/{len(sites)}</b>："
                     + "、".join(_t(k, 10) for k in ok))
        for k, v in list(sites.items())[:4]:
            s = v.get("stats") or {}
            up = float(s.get("upload") or 0) / 1024 ** 3
            lines.append(f"　· {_t(k, 10)}　bound={bool(v.get('bound'))}"
                         f"　上传 {up:.1f}G"
                         f"　辅种 {len(v.get('seed_ids') or [])}")
    except Exception as e:
        lines.append(f"　⚠️ 读取失败：{_t(e, 80)}")
    return lines


def _ex_dirs() -> list[str]:
    """示例⑤：站点目录（真读 MP site 表）"""
    lines = ["⑤ <b>站点目录</b>（真读 MP 站点表）"]
    try:
        from .sortpath import mp_sites, TOP_CLASSES, ORIGINS
        m = mp_sites(ttl=0)
        if not m:
            lines.append("　（读不到 MP 站点表）")
            return lines
        names = sorted(set(m.values()))
        lines.append(f"　站点 <b>{len(names)}</b> 个：" + "、".join(_t(n, 10) for n in names))
        lines.append(f"　目录结构：/downloads/客户端/"
                     f"{'|'.join(ORIGINS)}/站点/{'/'.join(TOP_CLASSES)}")
    except Exception as e:
        lines.append(f"　⚠️ 运行失败：{_t(e, 80)}")
    return lines


async def build_report() -> str:
    """拼出完整汇报（要点 + 真跑出来的示例）"""
    head = [f"🆕 <b>升级完成 · {RELEASE_VER}</b>", _div(),
            "下面是本次改了什么，以及<b>在容器里真跑一遍</b>的结果：", ""]
    notes = []
    for icon, title, detail in NOTES:
        notes.append(f"{icon} <b>{title}</b>")
        notes.append(f"　{detail}")
    body: list[str] = []
    for fn in (_ex_emby, _ex_contrib, _ex_qb, _ex_sites, _ex_dirs):
        try:
            body += await asyncio.to_thread(fn)
        except Exception as e:
            body.append(f"⚠️ 示例运行异常：{_t(e, 80)}")
        body.append("")
    tail = [_div(), "💡 这些都是<b>刚才真跑出来</b>的，不是示例文案。",
            "　有哪一项看着不对，直接回我说就行。"]
    return ui.clip_html("\n".join(head + notes + [_div(), ""] + body + tail),
                        limit=3800)


async def send_release(force: bool = False) -> bool:
    """私聊给群主本人；同一版本只发一次（force 可强制重发）"""
    if not force and os.path.exists(_MARK):
        logger.debug(f"升级汇报 {RELEASE_VER} 已发过，跳过")
        return False
    try:
        text = await build_report()
    except Exception as e:
        logger.warning(f"生成升级汇报失败: {e}")
        return False
    try:
        await bot.send_message(
            int(S.OWNER), text, parse_mode=PM, disable_web_page_preview=True,
            reply_markup=KM([[B("📮 试试投稿", "cb:new:0"),
                              B("🧲 看做种", "qr:qb")],
                             [B("🔍 求片", "qpnew")]]))
    except Exception as e:
        logger.warning(f"发送升级汇报失败: {e}")
        return False
    try:
        os.makedirs(_MARK_DIR, exist_ok=True)
        open(_MARK, "w").write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    logger.info(f"升级汇报 {RELEASE_VER} 已私聊群主")
    return True


@bot.on_callback_query(filters.regex(r"^qr:qb$"))
async def cb_qr_qb(client, cb):
    """汇报卡片上的「看做种」——就地展开做种明细"""
    await cb.answer()
    try:
        from .svc import seed_text
        txt = await asyncio.to_thread(seed_text, 8)
    except Exception as e:
        txt = f"⚠️ 读取失败：{_t(e, 80)}"
    try:
        await cb.message.reply(txt, parse_mode=None,
                               disable_web_page_preview=True)
    except Exception:
        pass


@bot.on_message(filters.command(["release", "升级要点", "更新说明"], **CMD))
async def cmd_release(client, msg: Message):
    """手动重发一次升级汇报"""
    from .request import is_admin
    if not is_admin(msg.from_user.id):
        await msg.reply("⛔ 只有管理员能用", parse_mode=PM)
        return
    await msg.reply("⏳ 正在跑一遍示例…", parse_mode=PM)
    ok = await send_release(force=True)
    if not ok:
        await msg.reply("⚠️ 生成/发送失败，看日志排查。", parse_mode=PM)
