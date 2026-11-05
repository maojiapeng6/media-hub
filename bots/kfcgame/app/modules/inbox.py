"""PT 全链路播报：消费 pt2cloud 写入的 kfc_pt_notify 队列，推送到主群

播报分三类：
    downloading  开始下载（群内一条「进度消息」，全程原地刷新进度条/速度/ETA）
    downloaded   115 网盘里真的出现完整文件 → 才算下载成功，此时播报
    organized    MT 整理完生成 strm → 播报入库
    failed       异常，需要人工看一眼
    daily        每天 21:00 的保号日报

带海报的通知走「图片 + caption」，没有海报降级为纯文字。
所有数字（体积/速度/进度）都来自 qBittorrent 与 115 云端实测值，不做估算。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import re
import time

import requests
from loguru import logger
from pyrogram import filters
from pyrogram.types import (InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import db
from .. import helper
from .. import settings as S
from .. import ui
from . import perm

CMD = dict(prefixes=S.PREFIX)

KIND_ICON = {
    "downloading": "🔽",
    "downloaded": "📥",
    "organized": "🎬",
    "failed": "⚠️",
    "daily": "📊",
    "uploaded": "☁️",
}
KIND_HEAD = {
    "downloading": "开始下载",
    "downloaded": "下载完成 · 等待入库",
    "organized": "已入库 · 可以看了",
    "failed": "链路异常 · 需人工确认",
    "daily": "PT 保号日报",
    "uploaded": "已上传云端 · 整理中",
}
# 进度阶段（用于「进度消息」原地刷新）
STAGE_ICON = {
    "downloading": "🔽",
    "uploading": "☁️",
    "uploaded": "📥",
    "organized": "🗂",
    "archived": "🎬",
}
STAGE_HEAD = {
    "downloading": "正在下载",
    "uploading": "正在上传 115",
    "uploaded": "下载完成 · 等待整理入库",
    "organized": "已整理 · 生成 strm 中",
    "archived": "已入库 · 可以看了",
}
# pt_uploads.status -> 展示阶段
STATUS2STAGE = {
    "downloading": "downloading",
    "pending": "uploading",
    "verifying": "uploading",
    "uploaded": "uploaded",
    "organized": "organized",
    "archived": "archived",
}
ORIGIN_TEXT = {"求片": "群友求片", "订阅": "自动追新"}
# 播报分流（2026-10-10 用户拍板）：
#   · 群友求片       → 发群（带求片人链接，群友都能看到进度）
#   · 自己 MP 订阅   → 只私聊 owner（不刷群）
#   · 异常 failed    → 只私聊 owner
#   · 保号日报 daily → 只私聊 owner（上传/下载/分享率/磁盘属于私人运营数据）
ROUTE_PRIVATE_KINDS = {"failed", "daily"}
# qBittorrent 原始状态 → 中文（真实反映「有没有种源」）
QB_STATE = {
    "stalledDL": "⏸ 等待种源",
    "stalledUP": "✅ 做种中",
    "downloading": "🔽 下载中",
    "uploading": "☁️ 上传中",
    "missingFiles": "⚠️ 源文件缺失",
    "pausedDL": "⏸ 已暂停",
    "pausedUP": "⏸ 暂停做种",
    "queuedDL": "⏳ 排队中",
    "queuedUP": "⏳ 排队做种",
    "checkingDL": "🔍 校验中",
    "checkingUP": "🔍 校验中",
    "forcedDL": "🔽 强制下载",
    "allocating": "📦 分配空间",
    "metaDL": "🔍 获取元数据",
    "error": "❌ 出错",
}

# 海报缓存 url -> (ts, bytes)
_IMG: dict[str, tuple[float, bytes]] = {}
_IMG_TTL = 3600
_IMG_MAX = 60

# 需要定时自动刷新的面板消息： (chat, msg) -> (is_photo, ts, builder)
_LIVE: dict[tuple[int, int], tuple[bool, float, str]] = {}
_LIVE_TTL = 1800


# ---------------------------------------------------------------- 编码兜底
def dm(s):
    """修复双重编码中文 —— 唯一实现在 db.dm()，这里只是转调，别再复制一份"""
    return db.dm(s)


def _md(s):
    """保留 . - · ｜ 等可读符号，只剔除会破坏 Markdown 解析的字符"""
    if not s:
        return ""
    out = dm(s)
    for ch in ("*", "_", "`", "[", "]", "\\"):
        out = out.replace(ch, "")
    return out


# ---------------------------------------------------------------- 格式化
# ★ 进度条与「在线播报」完全同款（█ 实心 / ░ 空心，width=14），
#   以前这里用的是 ▓░，同一屏里两种风格 ⇒ 视觉不统一。
def bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    n = int(round(pct / 100 * width))
    return "█" * n + "░" * (width - n)


def speed_short(bps) -> str:
    """速度压缩写法：5.2MB/s —— 面板里要跟进度条并排放，必须短"""
    try:
        v = float(bps or 0)
    except Exception:
        return "--"
    if v <= 0:
        return "0B/s"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.0f}{unit}/s" if unit == "B" else f"{v:.1f}{unit}/s"
        v /= 1024
    return f"{v:.1f}PB/s"


def eta_short(sec) -> str:
    """剩余时间压缩写法：12分 / 1时20分 —— 与 speed_short 配对"""
    try:
        s = int(sec or 0)
    except Exception:
        return ""
    if s <= 0:
        return ""
    if s > 86400 * 2:
        return ">2天"
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分"
    return f"{s // 3600}时{(s % 3600) // 60}分"


def hum_speed(bps) -> str:
    try:
        v = float(bps or 0)
    except Exception:
        return "--"
    if v <= 0:
        return "0 B/s"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}/s"
        v /= 1024
    return f"{v:.1f} PB/s"


def hum_size(b) -> str:
    try:
        v = float(b or 0)
    except Exception:
        return "--"
    gb = v / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = v / 1024 ** 2
    if mb >= 1:
        return f"{mb:.0f} MB"
    return f"{v / 1024:.0f} KB"


def hum_eta(sec) -> str:
    try:
        s = int(sec or 0)
    except Exception:
        return "--"
    if s <= 0:
        return "--"
    if s > 86400 * 2:
        return ">2 天"
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒"
    h, m = s // 3600, (s % 3600) // 60
    return f"{h} 小时 {m} 分"


# ---------------------------------------------------------------- 海报
def _fetch(url: str) -> bytes | None:
    if not url:
        return None
    hit = _IMG.get(url)
    if hit and time.time() - hit[0] < _IMG_TTL:
        return hit[1]
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and r.content and len(r.content) < 6 * 1024 * 1024:
            if len(_IMG) >= _IMG_MAX:
                _IMG.pop(next(iter(_IMG)), None)
            _IMG[url] = (time.time(), r.content)
            return r.content
    except Exception as e:
        logger.debug(f"播报海报下载失败 {url}: {e}")
    return None


def _emby_poster_url(tmdb_id: str, media_type: str = "") -> str:
    """bot 端海报兜底：通知行没带图时直接找 Emby 条目拿 Primary 图 URL"""
    if not tmdb_id:
        return ""
    try:
        r = requests.get(
            f'{S.EMBY["url"].rstrip("/")}/emby/Items',
            params={"Recursive": "true",
                    "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                    "Limit": "1", "api_key": S.EMBY["api_key"]},
            timeout=8)
        items = (r.json() or {}).get("Items") or []
        if items:
            return (f'{S.EMBY["url"].rstrip("/")}/emby/Items/{items[0]["Id"]}'
                    f"/Images/Primary?maxWidth=500&quality=85"
                    f'&api_key={S.EMBY["api_key"]}')
    except Exception as e:
        logger.debug(f"Emby 海报兜底失败 tmdb={tmdb_id}: {e}")
    return ""


def _norm_fps(v: float) -> str:
    """帧率规范化：个别流的 RealFrameRate 把 23.976 存成 23976（tick 值），
    超过 240 一律先 /1000，修完仍不合理的丢弃，绝不让「两万多 fps」出群。"""
    try:
        fv = float(v)
    except Exception:
        return ""
    if fv > 240:
        fv /= 1000.0
    if 1 < fv <= 240:
        return f"{fv:g}fps"
    return ""


# ---------------------------------------------------------------- 分流 / 求片人
def _origin_of(n: dict) -> str:
    """判定这条通知属于「求片」还是「订阅」。

    origin 列偶尔是空的或双重编码乱码（老数据），这时用 wishlist 反查兜底：
    能对上求片清单就算「求片」，对不上就是用户自己订阅刷流的量。
    """
    o = dm(n.get("origin") or "").strip()
    if o in ("求片", "订阅"):
        return o
    try:
        if db.wish_owner(n.get("media_title"), n.get("torrent_name"),
                         n.get("title")):
            return "求片"
    except Exception:
        pass
    return "订阅"


def _is_private(n: dict) -> bool:
    """这条通知是否只私聊 owner（不进群）"""
    if (n.get("kind") or "") in ROUTE_PRIVATE_KINDS:
        return True
    return _origin_of(n) == "订阅"


def _who(n: dict) -> dict:
    """求片人（用于播报里「谁求的」可点链接）。

    ★ 2026-10-14：优先用通知行自带的 req_tg/req_name/req_username
    （pt2cloud 写通知时反查好），字段缺失才回退 wishlist 反查——
    之前只有小🔗没@、且 tg_name 乱码导致纯文本点不动，都在这修。
    """
    try:
        tg = int(str(n.get("req_tg") or "0") or 0)
    except Exception:
        tg = 0
    if tg:
        return {"tg_id": tg, "tg_name": dm(n.get("req_name") or ""),
                "tg_username": dm(n.get("req_username") or "")}
    try:
        return db.wish_owner(n.get("media_title"), n.get("torrent_name"),
                             n.get("title")) or {}
    except Exception:
        return {}


def _req_at(n: dict) -> str:
    """求片人 **@ 提及**（放通知最前，像"回复他"）。通知走 Markdown，用
    `[@名字](链接)` 形式（safe() 会保留 markdown 链接）。

    规则（用户 2026-10-14 拍板：关于他的回答都要@他）：
      · 有 tg_username → [@用户名](https://t.me/用户名)（真@提醒）
      · 无用户名       → [@名字](tg://user?id=)（可点、跳主页）
      · 清洗控制字符乱码（\x7f 那种），乱码/无名字返回空不显示
    """
    w = _who(n)
    uid = int(w.get("tg_id") or 0)
    if not uid:
        return ""
    import re as _re
    u = _re.sub(r"[\x00-\x1f\x7f]", "", str(w.get("tg_username") or "")).lstrip("@").strip()
    if u:
        return f"[@{u}](https://t.me/{u})"
    nm = _re.sub(r"[\x00-\x1f\x7f]", "", str(w.get("tg_name") or "")).strip()[:20]
    if not nm:
        return ""
    return f"[@{ui.safe(nm)}](tg://user?id={uid})"


def _who_line(n: dict) -> list[str]:
    """求片人身份卡片 —— **两行**（与全站统一）

    第一行：👤 求片人｜**Emby 用户名**　[🔗](tg://user?id=xxx)
    第二行：　　**TG 用户名**（前面不带 @）
    """
    w = _who(n)
    if not w.get("tg_id"):
        return []
    try:
        emby = ui.emby_name_of(w["tg_id"])
    except Exception:
        emby = None
    return ui.user_card(w["tg_id"], dm(w.get("tg_name") or ""), "",
                        emby or "", label="求片人", icon="👤", mode="md")


def _title_link(n: dict) -> str:
    """片名 → 可点的 TMDB 超链接（播报里不再出现纯文本片名）"""
    return ui.title_link(_title_of(n), n.get("tmdb_id"),
                         n.get("media_type"), 60, mode="md")


# ---------------------------------------------------------------- 渲染
def _title_of(n: dict) -> str:
    t = dm(n.get("media_title") or "")
    if not t:
        t = dm((n.get("title") or "").split("：", 1)[-1])
    return ui.safe(t)[:60]


def _meta_line(n: dict) -> str:
    meta = []
    o = dm(n.get("origin") or "")
    if o:
        meta.append(ORIGIN_TEXT.get(o, o))
    if n.get("site"):
        meta.append(f"站点 {ui.safe(dm(n['site']))}")
    if n.get("category"):
        meta.append(ui.safe(dm(n["category"])))
    return "🏷 " + "｜".join(meta) if meta else ""


def _tmdb_line(n: dict) -> str:
    """TMDB ID 超链接行：显示「TMDB ID：xxx」点击直达 TMDB 条目页"""
    tid = dm(n.get("tmdb_id") or "")
    if not tid or not tid.isdigit():
        return ""
    path = "tv" if (n.get("media_type") or "") == "电视剧" else "movie"
    return f"[TMDB ID：{tid}](https://www.themoviedb.org/{path}/{tid})"


def _ql(s: str) -> str:
    """画质串清洗：只剔除真正会让解析器炸的字符（反斜杠/反引号/星号/下划线/方括号等），
    保留 . + - 这些「数值/规格」字符 —— 否则 23.976fps 会被清成 23976fps、
    H.265 变 H265、7.1 变 71（血泪教训：别用 ui.safe 处理画质串）。"""
    return re.sub(r"[\\`*_\[\]{}()]", "", str(s or ""))


def _quality_lines(q: str, bold: bool) -> list[str]:
    """把画质串拆成美观的多行：画质主线 + 动态范围 + 色深 + 帧率。

    quality 串由 pt2cloud quality.short() 生成，形如：
    「4K · Dolby Vision P8 + HDR10 · 10bit · 23.976fps · Blu-ray REMUX · H.265 · TrueHD Atmos 7.1」
    旧格式自动兼容——解析不到就只显示一行画质。
    """
    if not q:
        return []
    s = dm(q)
    toks = [t.strip() for t in s.split("·") if t.strip()]
    _dr = re.compile(r"杜比视界|Dolby|HDR|SDR|HLG|\bDV\b", re.I)
    _dep = re.compile(r"^\d+bit$")
    _fps = re.compile(r"^\d+(?:\.\d+)?fps$", re.I)
    main, dr, dep, fps = [], [], [], []
    for t in toks:
        if _dr.search(t):
            dr.append(t)
        elif _dep.match(t):
            dep.append(t)
        elif _fps.match(t):
            fps.append(t)
        else:
            main.append(t)
    lines = [f"🎞 画质｜**{_ql(' · '.join(main))}**" if bold
             else f"🎞 画质｜{_ql(' · '.join(main))}"]
    if dr:
        lines.append(f"🌈 动态范围｜**{_ql(' + '.join(dr))}**" if bold
                     else f"🌈 动态范围｜{_ql(' + '.join(dr))}")
    if dep:
        lines.append(f"🎨 色深｜**{_ql(dep[0])}**" if bold
                     else f"🎨 色深｜{_ql(dep[0])}")
    if fps:
        # 渲染端兜底：fps 值异常（如 23976）时规范化，修不好就丢弃这一行
        m = re.match(r"^(\d+(?:\.\d+)?)fps$", fps[0], re.I)
        shown = _norm_fps(m.group(1)) if m else fps[0]
        if shown:
            lines.append(f"⏱ 帧率｜**{_ql(shown)}**" if bold
                         else f"⏱ 帧率｜{_ql(shown)}")
    return lines


def _render_progress(n: dict) -> str:
    """可原地刷新的进度消息"""
    stage = dm(n.get("stage") or "") or "downloading"
    icon = STAGE_ICON.get(stage, "🔽")
    head = STAGE_HEAD.get(stage, "正在下载")

    year = dm(n.get("year") or "")

    lines = [
        f"{icon} **{head}**",
        ui.divider(),
        # 片名本身就是一个可点的 TMDB 链接（点一下直达该片详情页）
        f"🎬 {_title_link(n)}" + (f"（{year}）" if year else ""),
    ]
    tml = _tmdb_line(n)
    if tml:
        lines.append(tml)
    lines += _who_line(n)
    meta = _meta_line(n)
    if meta:
        lines.append(meta)
    if n.get("quality"):
        lines += _quality_lines(n.get("quality"), bold=False)
    if n.get("bitrate"):
        lines.append(f"📡 码率｜{_md(n['bitrate'])}")

    pct = float(n.get("progress") or 0)
    total = int(n.get("total_size") or 0) or int(float(n.get("size_gb") or 0) * 1024 ** 3)
    done = int(n.get("done_size") or 0)
    lines.append(ui.divider())

    if stage == "downloading":
        lines.append(f"│　📶 `[{bar(pct)}]` **{pct:.1f}%**")
        lines.append(f"│　💾 **{hum_size(done)}** / **{hum_size(total)}**")
        spd = int(n.get("speed_bps") or 0)
        if spd <= 0 and pct < 0.5:
            lines.append("│　⏸ 暂无可用种源，正在等待")
        elif spd <= 0:
            lines.append("│　⏸ 速度暂停（做种者下线）")
        else:
            tail = f"│　⚡ **{hum_speed(spd)}**"
            # 没速度就不显示剩余时间（静止种子算出「>2天」是误导）
            e = eta_short(n.get("eta_sec"))
            if e:
                tail += f"　⏳ 剩余 **{e}**"
            lines.append(tail)
    else:
        lines.append(f"│　✅ `[{bar(100)}]` **100%**　💾 **{hum_size(total or done)}**")
        if stage == "uploading":
            lines.append("│　☁️ 正在写入 115，校验完成后播报")
        else:
            lines.append("│　⏳ 已存 115，等待 MediaTidy 整理入库")

    lines.append(ui.divider())
    lines.append("💡 整理生成 strm 后会再播报一次「已入库」")
    return ui.clip_html("\n".join(lines))


def _render_plain(n: dict) -> str:
    """普通播报（下载完成 / 已入库 / 异常 / 日报）"""
    kind = n.get("kind") or ""
    icon = KIND_ICON.get(kind, "📦")
    head = KIND_HEAD.get(kind, "PT 动态")

    year = dm(n.get("year") or "")

    lines = [f"{icon} **{head}**"]
    # ★ 求片人 @ 提及放最前（像"回复他"）——用户 2026-10-14：关于他的回答都要@他
    _at = _req_at(n)
    if _at:
        lines.append(_at)
    lines += [ui.divider(),
              f"🎬 {_title_link(n)}" + (f"（{year}）" if year else "")]
    tml = _tmdb_line(n)
    if tml:
        lines.append(tml)
    lines += _who_line(n)
    meta = _meta_line(n)
    if meta:
        lines.append(meta)
    lines += _quality_lines(n.get("quality"), bold=True)
    if n.get("size_gb"):
        lines.append(f"💾 体积｜**{float(n['size_gb']):.2f} GB**")
    if n.get("bitrate"):
        lines.append(f"📡 码率｜**{_md(n['bitrate'])}**")

    body = dm(n.get("body") or "").strip()
    if body:
        # body 里可能重复了画质/体积/码率行，剔掉
        keep = [l for l in body.splitlines()
                if "画质" not in l and "体积" not in l and "码率" not in l
                and "动态范围" not in l and "色深" not in l]
        body = "\n".join(keep).strip()
    if body:
        lines.append(ui.divider())
        lines.append(body)

    if kind == "downloaded":
        lines.append(ui.divider())
        lines.append("⏳ **已存入 115，整理完成会自动通知你**")
    elif kind == "organized":
        # pt2cloud 已把可播校验做在前面：标题「已入库」= Emby 确认能播
        lines.append(ui.divider())
        if "索引中" in str(n.get("title") or ""):
            lines.append("⏳ **Emby 建立索引中，就绪后再通知你**")
        else:
            lines.append("✅ **已确认可播放，Emby 里直接搜片名即可观看**")
    return ui.clip_html("\n".join(lines))


def _fmt(n: dict) -> str:
    if n.get("kind") == "downloading":
        return _render_progress(n)
    return _render_plain(n)


# ---------------------------------------------------------------- 发送
async def _send_one(n: dict):
    """发一条播报；有海报就发图，没海报用 Emby 兜底（保证所有影片通知带图）

    分流（用户 2026-10-10 拍板）：
      群友求片 → 主群（带「求片人」可点链接）
      自己订阅 / 异常 / 保号日报 → 只私聊 owner
    """
    text = _fmt(n)
    poster = dm(n.get("poster") or "")
    data = await asyncio.to_thread(_fetch, poster) if poster else None
    if data is None and n.get("tmdb_id"):
        url = await asyncio.to_thread(_emby_poster_url,
                                      n.get("tmdb_id") or "",
                                      n.get("media_type") or "")
        if url:
            data = await asyncio.to_thread(_fetch, url)
    await helper.warmup(bot)
    gid = _target_of(n)
    if gid is None:
        return "skip"                 # 无处可发（owner 未配置）→ 直接丢弃
    # ★ 求片通知（群发）附「继续求片」按钮 —— 用户 2026-10-14：
    #   群友可能不止求一部，入库后直接给下一步入口（qpnew=全项目求片起点，
    #   对所有群友开放，无需权限）。caption 挂按钮只有带图这条路；无图时
    #   第一次发送就带 kb（send_message 也支持），失败降级才裸发。
    kb = None
    if not _is_private(n) and (n.get("kind") or "") in ("organized", "failed"):
        kb = KM([[B("🔍 继续求片", "qpnew"), B("📋 我的求片", "qpmine")]])
    try:
        if data:
            try:
                bio = io.BytesIO(data)
                bio.name = "poster.jpg"
                return await bot.send_photo(gid, bio, caption=text, reply_markup=kb)
            except Exception as e1:
                logger.debug(f"带图发送失败，转纯文本 id={n.get('id')}: {e1}")
        return await bot.send_message(gid, text, disable_web_page_preview=True,
                                      reply_markup=kb)
    except Exception as e:
        logger.warning(f"播报发送失败 id={n.get('id')} →{gid}: {e}")
        try:
            return await bot.send_message(gid, text, disable_web_page_preview=True)
        except Exception as e2:
            logger.warning(f"播报降级仍失败 id={n.get('id')}: {e2}")
            return None


def _target_of(n: dict) -> int | None:
    """这条通知该发到哪：主群 还是 owner 私聊"""
    if _is_private(n):
        return int(S.OWNER or 0) or None
    return int(S.GROUP[0]) if S.GROUP else None


async def poll_notifies(client=None) -> int:
    """拉取并播报待发通知；返回本次发出的条数"""
    try:
        rows = await asyncio.to_thread(db.pending_notifies, 10)
    except Exception as e:
        logger.debug(f"读取入库通知失败：{e}")
        return 0
    if not rows:
        return 0

    done = []
    dropped = []
    for n in rows:
        msg = await _send_one(n)
        if msg == "skip":               # 无处可发（oid 未配置）→ 丢弃，别堵队列
            dropped.append(int(n["id"]))
            continue
        if msg is None:
            # 私聊 owner 发不出去（比如 owner 还没点过 start）会永久失败，
            # 不能像群播报那样「下轮再试」——否则整条队列被这一条卡死。
            if _is_private(n):
                logger.warning(f"私聊播报失败，丢弃 id={n.get('id')}（owner 未开启私聊？）")
                dropped.append(int(n["id"]))
                continue
            break                       # 群播报发不出去就停下，下轮再试
        done.append((int(n["id"]), msg.chat.id, msg.id))
        # 下载中的通知后续要原地刷新，先清掉待刷标记避免立刻重复编辑
        if n.get("kind") == "downloading":
            await asyncio.to_thread(db.clear_edit, [int(n["id"])])
        await asyncio.sleep(0.4)

    if dropped:
        try:
            await asyncio.to_thread(db.mark_notify_sent, dropped)
        except Exception as e:
            logger.warning(f"丢弃通知回写失败：{e}")

    if done:
        try:
            await asyncio.to_thread(db.mark_notify_sent, done)
        except Exception as e:
            logger.warning(f"通知状态回写失败：{e}")
        logger.info(f"PT 播报：发出 {len(done)} 条")
    return len(done)


async def poll_edits(client=None) -> int:
    """原地刷新进度消息（进度条/速度/ETA），不新增群内消息"""
    try:
        rows = await asyncio.to_thread(db.editable_notifies, 10)
    except Exception as e:
        logger.debug(f"读取待刷新通知失败：{e}")
        return 0
    if not rows:
        return 0
    done = []
    for n in rows:
        try:
            chat = int(str(n.get("chat_id") or "0"))
            mid = int(n.get("msg_id") or 0)
        except Exception:
            chat, mid = 0, 0
        if not chat or not mid:
            done.append(int(n["id"]))
            continue
        if dm(n.get("stage")) == "finished":
            # 已完成：撤掉进度卡片（紧接着会有带图的「下载完成」播报）
            try:
                await bot.delete_messages(chat, mid)
            except Exception as e:
                logger.debug(f"撤除进度卡片失败 id={n['id']}: {e}")
            done.append(int(n["id"]))
            await asyncio.sleep(0.3)
            continue

        text = _fmt(n)
        try:
            if n.get("poster"):
                await bot.edit_message_caption(chat, mid, text)
            else:
                await bot.edit_message_text(chat, mid, text,
                                            disable_web_page_preview=True)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.debug(f"刷新进度消息失败 id={n['id']}: {e}")
        done.append(int(n["id"]))
        await asyncio.sleep(0.4)
    if done:
        try:
            await asyncio.to_thread(db.clear_edit, done)
        except Exception as e:
            logger.warning(f"清除刷新标记失败：{e}")
    return len(done)


def ql_short(q: str, limit: int = 34) -> str:
    """画质串紧凑写法：按 ` · ` 分段取，**绝不按字符硬切**。

    硬切会把 `23.976fps` 砍成 `2`（血泪教训：`_md(q)[:40]` 就是这么翻车的）。
    """
    toks = [t.strip() for t in dm(q).split("·") if t.strip()]
    out: list[str] = []
    for t in toks:
        cand = " · ".join(out + [t])
        if len(cand) > limit:
            break
        out.append(t)
    s = " · ".join(out) or (toks[0] if toks else "")
    return _ql(s)


# ---------------------------------------------------------------- 实时面板
async def _build_panel() -> tuple[str, KM]:
    """构建「下载进度」面板

    ★ 排版与「在线播报」完全同款（用户要求）：
      在途 ≤2 部 → 详细竖线版（每个字段独占一行，绝不越过竖线）
      在途 ≥3 部 → 紧凑单行版（`标题　63% █████░░░░　⚡12.4MB/s ⏳8分`）
    """
    rows = await asyncio.to_thread(db.pt_active, 20)
    # ★ 同名去重（保留进度最大的那条）：实测 20 条在途里有 9 条都叫
    #   「炼气十万年」（同一季被切成多集各建一行），全列出来毫无信息量，
    #   还把面板撑到 1024 被截。相同片名合并显示，标题后缀集数区间。
    merged: dict[str, dict] = {}
    for r in rows:
        t = ui.safe(dm(r.get("media_title") or r.get("torrent_name") or ""))[:34]
        if not t:
            continue
        cur = merged.get(t)
        if cur is None or float(r.get("progress") or 0) > float(cur.get("progress") or 0):
            merged[t] = r
    rows = list(merged.values())
    dl = [r for r in rows
          if STATUS2STAGE.get(dm(r.get("status") or ""), "downloading")
          == "downloading"]
    arc = await asyncio.to_thread(db.pt_recent_archived, 5)
    lines = ["📡 **下载进度**", ui.divider()]
    if not rows:
        lines.append("当前没有在途任务")
        lines.append(ui.divider())
        lines.append("💡 用 `/qp 片名` 求片，或等订阅自动追新")
        return "\n".join(lines), KM([[B("🔄 刷新", "dlref")]])

    lines.append(f"⏬ 在途｜**{len(dl)}** 部下载中"
                 + (f"　🗂 **{len(rows) - len(dl)}** 部整理中"
                    if len(rows) > len(dl) else ""))
    tot_sp = sum(int(r.get("speed_bps") or 0) for r in dl)
    if tot_sp > 0:
        lines.append(f"⚡ 总速度｜**{hum_speed(tot_sp)}**")

    detailed = len(rows) <= 2          # 与在线播报用同一个阈值
    # ★ 必须按条数收敛 —— Telegram 消息上限 1024 字，超了会把「最近入库」
    #   整段截掉（实测 9 条在途时就是这样）。紧凑版最多 8 条 + 溢出提示。
    show = rows if detailed else rows[:8]
    for r in show:
        stage = STATUS2STAGE.get(dm(r.get("status") or ""), "downloading")
        qbs = dm(r.get("qb_state") or "")
        icon = STAGE_ICON.get(stage, "🔽")
        stage_tx = STAGE_HEAD.get(stage, "处理中")
        if qbs and qbs in QB_STATE:
            stage_tx = QB_STATE[qbs]
            if qbs == "stalledDL":
                icon = "⏸"
            elif qbs == "missingFiles":
                icon = "⚠️"
        title = ui.title_link(
            dm(r.get("media_title") or r.get("torrent_name") or ""),
            r.get("tmdb_id"), r.get("media_type"), 34, mode="md")
        year = dm(r.get("year") or "")
        pct = float(r.get("progress") or 0)
        total = int(r.get("total_size") or 0)
        done = int(r.get("dl_size") or 0)
        q = _ql(dm(r.get("quality") or ""))

        if detailed:
            lines.append(f"{icon} {title}" + (f"（{year}）" if year else ""))
            if stage == "downloading":
                lines.append(f"│　📶 `[{bar(pct)}]` **{pct:.1f}%**")
                lines.append(f"│　💾 **{hum_size(done)}** / **{hum_size(total)}**")
                spd = int(r.get("speed_bps") or 0)
                if spd > 0:
                    tail = f"│　⚡ **{hum_speed(spd)}**"
                    e = eta_short(r.get("eta_sec"))
                    if e:
                        tail += f"　⏳ 剩余 **{e}**"
                    lines.append(tail)
                else:
                    lines.append(f"│　{stage_tx}")
            else:
                lines.append(f"│　✅ `[{bar(100)}]` **100%**"
                             f"　💾 **{hum_size(total or done)}**")
                lines.append(f"│　{stage_tx}")
            if q:
                lines.append(f"│　🎞 {_md(q)}")
            lines.append(ui.divider())
        else:
            # 紧凑版：一行塞下「标题 + 进度条 + 速度 + 剩余」
            if stage == "downloading":
                spd = int(r.get("speed_bps") or 0)
                # ★ 没有速度就不该出现「剩余时间」—— 静止的种子算出个
                #   「>2天」纯属误导（实测踩过）。
                #   状态文案自带图标（QB_STATE 里就有 ⏸/⏳），别再叠一个
                tail = f"⚡{speed_short(spd)}" if spd > 0 else stage_tx
                e = eta_short(r.get("eta_sec")) if spd > 0 else ""
                if e:
                    tail += f" ⏳{e}"
                lines.append(f"{icon} {title}　**{pct:.0f}%** "
                             f"`[{bar(pct)}]`　{tail}")
            else:
                lines.append(f"{icon} {title}　**100%**　{stage_tx}")
            if q:
                lines.append(f"　· 🎞 {ql_short(q)}")
    if len(rows) > len(show):
        lines.append(f"　_…还有 {len(rows) - len(show)} 部，用 "
                     f"`/ptstatus` 看全部_")

    if arc:
        lines.append("✅ **最近入库**")
        for a in arc[:3]:
            t = ui.title_link(dm(a.get("media_title") or ""),
                              a.get("tmdb_id"), a.get("media_type"),
                              20, mode="md")
            y = dm(a.get("year") or "")
            eps = a.get("eps") or ""
            mark = f"　**{eps}**" if eps else ""
            lines.append(f"　· {t}" + (f"（{y}）" if y else "") + mark +
                         f"　{hum_size(a.get('total_size'))}")
        if len(arc) > 3:
            lines.append(f"　_…还有 {len(arc) - 3} 部_")

    lines.append(ui.divider())
    lines.append("💡 整理生成 strm 后会自动播报「已入库」")
    out = "\n".join(lines)
    if len(out) > 1024:
        #仍超长就砍掉「最近入库」段，而不是 [:1024] 把标签劈成半截
        # （TG 会报 Can't find end tag，整条消息直接发不出去）
        cut = out.split("✅ **最近入库**")[0]
        out = (cut.rstrip() + "\n" + ui.divider() +
               "💡 整理生成 strm 后会自动播报「已入库」")
        out = out[:1024] if len(out) <= 1024 else cut[:1000]
    return out, KM([[B("🔄 刷新", "dlref")]])


async def _register_live(msg: Message, is_photo: bool, kind: str) -> None:
    _LIVE[(msg.chat.id, msg.id)] = (is_photo, time.time(), kind)


async def refresh_live() -> None:
    """定时自动刷新群内的实时面板（/dl 面板）"""
    if not _LIVE:
        return
    now = time.time()
    for key in list(_LIVE.keys()):
        is_photo, ts, kind = _LIVE[key]
        if now - ts > _LIVE_TTL:
            _LIVE.pop(key, None)
            continue
        chat, mid = key
        if kind == "panel":
            text, kb = await _build_panel()
        else:
            continue
        try:
            if is_photo:
                await bot.edit_message_caption(chat, mid, text, reply_markup=kb)
            else:
                await bot.edit_message_text(chat, mid, text, reply_markup=kb,
                                            disable_web_page_preview=True)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                logger.debug(f"刷新面板失败：{e}")
                if "MESSAGE_ID_INVALID" in str(e):
                    _LIVE.pop(key, None)
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------- 命令
@bot.on_message(filters.command(["dl", "下载", "下载进度", "进度", "jd"], **CMD))
async def cmd_dl(client, msg: Message):
    """查看当前下载进度（进度条 + 速度 + ETA），带刷新按钮"""
    wait = await msg.reply("⏳ 正在读取下载进度…")
    text, kb = await _build_panel()
    rows = await asyncio.to_thread(db.pt_active, 20)
    poster = ""
    for r in rows:
        if r.get("poster"):
            poster = dm(r["poster"])
            break
    sent = None
    if poster:
        data = await asyncio.to_thread(_fetch, poster)
        if data:
            try:
                await helper.warmup(bot)
                bio = io.BytesIO(data)
                bio.name = "poster.jpg"
                sent = await bot.send_photo(msg.chat.id, bio, caption=text,
                                            reply_markup=kb)
            except Exception as e:
                logger.debug(f"面板海报发送失败：{e}")
    if sent is None:
        sent = await msg.reply(text, reply_markup=kb,
                               disable_web_page_preview=True)
    await _register_live(sent, sent.photo is not None, "panel")
    try:
        await wait.delete()
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^dlpanel$"))
async def cb_dlpanel(client, q):
    """从菜单按钮直接拉出下载进度面板（新发一条，不改写原菜单）"""
    await q.answer("正在读取下载进度…")
    text, kb = await _build_panel()
    rows = await asyncio.to_thread(db.pt_active, 20)
    poster = ""
    for r in rows:
        if r.get("poster"):
            poster = dm(r["poster"])
            break
    sent = None
    if poster:
        data = await asyncio.to_thread(_fetch, poster)
        if data:
            try:
                await helper.warmup(bot)
                bio = io.BytesIO(data)
                bio.name = "poster.jpg"
                sent = await bot.send_photo(q.message.chat.id, bio,
                                            caption=text, reply_markup=kb)
            except Exception as e:
                logger.debug(f"面板海报发送失败：{e}")
    if sent is None:
        sent = await q.message.reply(text, reply_markup=kb,
                                     disable_web_page_preview=True)
    await _register_live(sent, sent.photo is not None, "panel")


@bot.on_callback_query(filters.regex(r"^dlref$"))
async def cb_dlref(client, q):
    """手动刷新下载进度面板"""
    text, kb = await _build_panel()
    try:
        if q.message.photo:
            await q.edit_message_caption(text, reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb,
                                      disable_web_page_preview=True)
        await _register_live(q.message, q.message.photo is not None, "panel")
        await q.answer("已刷新")
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.debug(f"刷新面板失败：{e}")
        await q.answer("已是最新")


def _who_link(tg_id, tg_name: str) -> str:
    """可点用户名：[名字](tg://user?id=xxx)"""
    try:
        uid = int(str(tg_id or "0"))
    except Exception:
        return ""
    if not uid:
        return ""
    nm = ui.safe(dm(tg_name or ""))[:18].strip() or f"UID{uid}"
    return f"[{nm}](tg://user?id={uid})"


async def _myreq_lines(uid: int, mine_only: bool = True) -> str:
    """求片进度清单（默认只看自己的；管理员加 all 看全部）"""
    if mine_only:
        rows = await asyncio.to_thread(db.my_wishes, uid, 20)
        head = "📋 **我的求片**"
        empty = ("📭 你还没有求过片\n\n"
                 "💡 在群里发 `/qp 片名` 就能求，求完这里会显示进度")
    else:
        rows = await asyncio.to_thread(db.today_wishes, 30)
        head = f"📋 **今日全部求片 · 共 {len(rows)} 部**"
        empty = "📭 今天还没有人求片\n\n💡 用 /qp 片名 求一部试试"
    if not rows:
        return head + "\n" + ui.divider() + "\n" + empty

    kws = []
    for r in rows:
        k = dm(r.get("keyword") or r.get("title") or "")
        if k and k not in kws:
            kws.append(k)
    stat = await asyncio.to_thread(db.wish_progress, kws)

    lines = [head, ui.divider()]
    for i, r in enumerate(rows, 1):
        title = ui.title_link(dm(r.get("title") or r.get("keyword") or ""),
                              "", "", 30, mode="md")
        year = dm(r.get("year") or "")
        lines.append(f"{i}. 🎬 {title}" + (f"（{year}）" if year else ""))
        if not mine_only:
            # 求片人：与全站统一的两行身份卡片（Emby 名 + TG 名）
            try:
                emby = ui.emby_name_of(r.get("tg_id"))
            except Exception:
                emby = None
            lines += ui.user_card(r.get("tg_id"), dm(r.get("tg_name") or ""),
                                  "", emby or "", icon="　👤", mode="md",
                                  indent="　　 ")
        hit = None
        for k in kws:
            if k and (k in dm(r.get("keyword") or "") or k in dm(r.get("title") or "")):
                hit = stat.get(k)
                if hit:
                    break
        if hit:
            st = STATUS2STAGE.get(dm(hit.get("status") or ""), "downloading")
            icon = STAGE_ICON.get(st, "🔽")
            head2 = STAGE_HEAD.get(st, "处理中")
            qbs = dm(hit.get("qb_state") or "")
            if st == "downloading" and qbs:
                head2 = QB_STATE.get(qbs, head2)
            pct = float(hit.get("progress") or 0)
            if st == "downloading":
                lines.append(f"　│　📶 {icon} {head2}　`[{bar(pct)}]` **{pct:.1f}%**")
                lines.append(f"　│　💾 {hum_size(hit.get('dl_size'))} / "
                             f"{hum_size(hit.get('total_size'))}　"
                             f"⚡ {hum_speed(hit.get('speed_bps'))}")
            else:
                lines.append(f"　│　{icon} {head2}")
        else:
            lines.append("　│　⏳ 已加入订阅，等待站点出种")
        lines.append(ui.divider())
    lines.append("💡 `/dl` 看实时速度　`/ptreport` 看今日日报")
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["myreq", "我的求片", "求片进度", "我的片"], **CMD))
async def cmd_myreq(client, msg: Message):
    """查我自己求的片：审核状态 + 下载进度（群友都能查，只看得到自己的）

    ★ 2026-10-11 起转发到 reqmgr._mine_panel —— 那边的信息更全
      （带审核状态、明确区分「还没审核/被驳回/站点暂无资源」，
      且带海报）。原来的 _myreq_lines 只按 wishlist 关键词匹配，
      看不出审核被驳回，也看不出「订阅已建但站点没资源」。
    """
    uid = msg.from_user.id if msg.from_user else 0
    if not uid:
        await msg.reply("❓ 读不到你的账号信息")
        return
    try:
        from .reqmgr import _mine_panel
        text, _kb, poster = await _mine_panel(uid)
    except Exception as e:
        logger.warning(f"我的求片面板异常，回退旧版：{e}")
        text = await _myreq_lines(uid, mine_only=True)
        poster = None
    if poster:
        try:
            bio = io.BytesIO(poster)
            bio.name = "poster.jpg"
            await msg.reply_photo(bio, caption=text,
                                  disable_web_page_preview=True)
            return
        except Exception as e:
            logger.debug(f"我的求片海报发送失败：{e}")
    await msg.reply(text, disable_web_page_preview=True)


@bot.on_message(filters.command(["今日求片", "今日片"], **CMD))
async def cmd_today_req(client, msg: Message):
    """今日群内全部求片（群友都能看，带求片人链接）"""
    uid = msg.from_user.id if msg.from_user else 0
    text = await _myreq_lines(uid, mine_only=False)
    await msg.reply(text, disable_web_page_preview=True)


# ---------------------------------------------------------------- 群版 PT 日报
async def _daily_group_text() -> str:
    """群版日报：只放「今日谁求了什么 + 入库成功数」，不含上传/下载/磁盘"""
    st = await asyncio.to_thread(db.today_req_stats)
    arc_all = await asyncio.to_thread(db.today_archived, 50)
    now = dt.datetime.now().strftime("%m-%d %H:%M")
    lines = [
        f"📊 **PT 日报 · {now}**",
        ui.divider(),
        f"🙋 今日求片｜**{st['req']}** 部",
        f"✅ 求片入库｜**{st['archived']}** 部",
        f"🚧 在途处理｜**{st['doing']}** 部",
    ]
    # ★ 2026-10-15 用户要求：日榜必须按人展示，且用**三行标准卡**
    #   （用户名 / UID / Emby用户名，全粗体+超链接到 TG 主页），
    #   不再只写一个光秃秃的名字。
    rank = await asyncio.to_thread(db.today_req_rank, 8)
    if rank:
        lines.append(ui.divider())
        lines.append("🏆 **今日活跃榜**（求片 + 入库）")
        for i, x in enumerate(rank, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            card = ui.user_card(int(x["tg"]), x.get("name") or "",
                                "", "", label=medal, icon="",
                                mode="md", indent="　")
            lines += card
            tail = []
            if x.get("n_req"):
                tail.append(f"求片 **{x['n_req']}** 部")
            if x.get("n_in"):
                tail.append(f"入库 **{x['n_in']}** 部")
            if tail:
                lines.append("　　" + " · ".join(tail))
    if st.get("failed"):
        lines.append(f"⚠️ 异常｜**{st['failed']}** 部（已私聊管理员）")
    if arc_all:
        lines.append(ui.divider())
        lines.append("🗂 **今日入库**")
        arc, rest = arc_all[:8], max(0, len(arc_all) - 8)
        for a in arc:
            t = ui.title_link(dm(a.get("media_title") or ""),
                              a.get("tmdb_id"), a.get("media_type"),
                              22, mode="md")
            y = dm(a.get("year") or "")
            n = int(a.get("n") or 1)
            eps = a.get("eps") or ""
            # 有集号就显示 S01E01-S01E05 区间；解析不到才退回 ×N
            mark = f" **{eps}**" if eps else (f" **×{n}**" if n > 1 else "")
            tail = f"　{hum_size(a.get('total_size'))}"
            lines.append(f"　· ✅ {t}" + (f"（{y}）" if y else "") + mark + tail)
        if rest:
            lines.append(f"　· …还有 **{rest}** 部")
    lines += [ui.divider(),
              "💡 `/myreq` 查我自己求的片进度　`/dl` 看实时下载"]
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["ptreport", "pt日报", "日报"], **CMD))
async def cmd_ptreport(client, msg: Message):
    """PT 日报（群友可查）：今日求片数 + 入库成功数"""
    await msg.reply(await _daily_group_text(), disable_web_page_preview=True)


async def push_daily_report(client=None) -> None:
    """每天定时往群里发一份「群版 PT 日报」（保号数据另发 owner 私聊）"""
    gid = int(S.GROUP[0]) if S.GROUP else 0
    if not gid:
        return
    try:
        await helper.warmup(bot)
        await bot.send_message(gid, await _daily_group_text(),
                               disable_web_page_preview=True)
        logger.info("群版 PT 日报已推送")
    except Exception as e:
        logger.warning(f"群版 PT 日报推送失败：{e}")


@bot.on_message(filters.command(["ptclean"], **CMD))
@perm.gate("ptctl")
async def cmd_ptclean(client, msg: Message):
    """清理群内「无种源/已失效」的下载中卡片（需 PT 控制权限）"""
    rows = await asyncio.to_thread(db.dl_cards, 50) or []
    dead = []
    for n in rows:
        if dm(n.get("kind")) != "downloading":
            continue
        try:
            pct = float(n.get("progress") or 0)
            spd = int(n.get("speed_bps") or 0)
        except Exception:
            continue
        if pct >= 0.5 or spd > 0:
            continue
        dead.append(n)
    if not dead:
        await msg.reply("✅ 没有需要清理的下载卡片")
        return
    ok = 0
    for n in dead:
        try:
            await bot.delete_messages(int(str(n.get("chat_id"))),
                                      int(n.get("msg_id") or 0))
            ok += 1
        except Exception as e:
            logger.debug(f"删除下载卡片失败 id={n['id']}: {e}")
        await asyncio.sleep(0.3)
    await asyncio.to_thread(db.clear_edit, [int(n["id"]) for n in dead])
    await msg.reply(f"🧹 已清理 {ok}/{len(dead)} 张无种源的下载卡片")


@bot.on_message(filters.command(["ptstatus", "pt"], **CMD))
async def cmd_pt(client, msg: Message):
    """手动查看 PT 链路当前状态"""
    try:
        pending = len(await asyncio.to_thread(db.pending_notifies, 3))
    except Exception:
        pending = 0
    try:
        stats = await asyncio.to_thread(db.pt_stats)
    except Exception:
        stats = {}
    lines = [
        "🔗 **PT 全自动化链路**", ui.divider(),
        "群友求片 → MoviePilot 搜刮 → qBittorrent 下载到 vol2",
        "→ pt2cloud 自动上传 115 → MediaTidy 整理 → strm → Emby",
        ui.divider(),
        f"📬 待播报通知：{pending} 条",
    ]
    for k, label in (("downloading", "下载中"), ("pending", "待上传"),
                     ("verifying", "云端校验中"),
                     ("uploaded", "已传云端"), ("organized", "已整理"),
                     ("archived", "已入库"), ("failed", "异常")):
        if k in stats:
            lines.append(f"　· {label}：{stats[k]} 个")
    lines += [
        ui.divider(),
        "💡 下载盘：/vol2/1000/pt/downloads（按分类分目录）",
        "💡 云端落点：115 待整理/PT/{求片|订阅}/{站点}/{分类}",
        "💡 /dl 看实时进度　/myreq 看今日求片",
    ]
    await msg.reply("\n".join(lines))
