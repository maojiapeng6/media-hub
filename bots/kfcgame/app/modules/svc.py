"""群主专属：外部服务管理（MoviePilot / qBittorrent / Emby / MediaTidy / 115）

★ 权限：这些命令全部是 perm.OWNER_ONLY ——
  群里看不到、管理员也看不到，**只有群主本人在私聊里能用**。
  每处入口都挂了 `@perm.gate("xxx")`，硬敲命令也会被静默拒绝。

★ 设计原则
  · 只读优先：默认只「看」，危险动作（删种、整理）必须再点一次确认按钮；
  · 每个服务一个命令，私聊里点按钮操作，不把一堆参数塞进命令行；
  · 所有请求都带超时，服务挂了只影响这一条命令，不会拖垮 bot。
"""
from __future__ import annotations

import asyncio
import io
import time

import requests
from loguru import logger
from pyrogram import filters
from pyrogram.types import (InlineKeyboardButton as B,
                            InlineKeyboardMarkup as KM, Message)

from .. import bot
from .. import settings as S
from .. import ui
from . import perm

CMD = dict(prefixes=S.PREFIX)

# 二次确认缓存："{uid}:{act}:{arg}" -> ts（10 分钟内有效）
_CONFIRM: dict[str, float] = {}
_TTL = 600

# /mpsearch 的结果缓存（点按钮要用）
_MPQ: dict[int, dict] = {}

_TIMEOUT = 12


# ════════════════════════════════════════════════════════════════════
#  通用
# ════════════════════════════════════════════════════════════════════
def _need(uid) -> bool:
    return perm.is_owner(uid)


def _ask(key: str) -> bool:
    """危险动作二次确认：第一次点只是「标记」，再点一次才真做"""
    now = time.time()
    for k in list(_CONFIRM):
        if now - _CONFIRM[k] > _TTL:
            _CONFIRM.pop(k, None)
    if key in _CONFIRM:
        _CONFIRM.pop(key, None)
        return True
    _CONFIRM[key] = now
    return False


def _g(u, p, **kw):
    try:
        kw.setdefault("timeout", _TIMEOUT)
        return requests.get(u.rstrip("/") + p, **kw)
    except Exception as e:
        logger.debug(f"GET {p} 失败: {e}")
        return None


def _hum(b) -> str:
    try:
        v = float(b or 0)
    except Exception:
        return "--"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} PB"


def _spd(bps) -> str:
    try:
        v = float(bps or 0)
    except Exception:
        return "--"
    return _hum(v) + "/s"


# ════════════════════════════════════════════════════════════════════
#  MoviePilot
# ════════════════════════════════════════════════════════════════════
def _mp():
    from . import request as R
    return R


def mp_overview() -> str:
    R = _mp()
    lines = ["🎬 **MoviePilot**", ui.divider()]
    j = R._req("GET", "/api/v1/dashboard/statistic")
    d = (j or {}).get("data") or {} if isinstance(j, dict) else {}
    if d:
        lines.append(f"🎞 电影 **{d.get('movie_count', 0)}** 部　"
                     f"📺 剧集 **{d.get('tv_count', 0)}** 部")
        lines.append(f"🧩 剧集单集 **{d.get('episode_count', 0)}** 集　"
                     f"👥 用户 **{d.get('user_count', 0)}** 人")
    else:
        lines.append("⚠️ 读不到统计（MP 未就绪？）")
    subs = R.mp_subscribe_list(30) or []
    lines.append(f"📌 订阅 **{len(subs)}** 条")
    if subs:
        for s in subs[:5]:
            nm = ui.safe(str(s.get("name") or ""))[:22]
            st = "⏸ 暂停" if s.get("state") in ("R", "r", False) else "▶️ 进行中"
            lines.append(f"　· {nm}　{st}")
    lines += [ui.divider(),
              "💡 `/mpsearch 片名` 搜并直接下　`/mpsubs` 订阅管理",
              "💡 `/mpsites` 站点　`/mpplugin` 插件"]
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["mpsearch"], **CMD))
@perm.gate("mp")
async def cmd_mpsearch(client, msg: Message):
    """搜索并直接下载（不走审核，群主自用）"""
    kw = (msg.text or "").split(maxsplit=1)
    kw = kw[1].strip() if len(kw) > 1 else ""
    if not kw:
        await msg.reply("用法：`/mpsearch 片名`\n\n"
                        "搜出来点一下就订阅自动下载，不用走审核。")
        return
    wait = await msg.reply(f"🔍 正在搜「{ui.safe(kw)[:20]}」…")
    R = _mp()
    res = await asyncio.to_thread(R.mp_search, kw, 8, False)
    if not res:
        await wait.edit(f"😕 没搜到「{ui.safe(kw)[:20]}」")
        return
    uid = msg.from_user.id
    _MPQ[uid] = {"kw": kw, "list": res, "ts": time.time()}
    lines = [f"🔍 **{ui.safe(kw)[:24]}** · 搜到 {len(res)} 条", ui.divider()]
    for i, r in enumerate(res):
        y = f"（{r.get('year')}）" if r.get("year") else ""
        lines.append(f"{i + 1}. **{ui.safe(r.get('title'))[:26]}**{y}　{r.get('type') or ''}")
        if r.get("vote"):
            lines.append(f"　 ⭐ {float(r['vote']):.1f}")
    lines.append(ui.divider())
    lines.append("👇 点片名直接订阅（MP 会自动搜种下载）")
    kb = []
    for i, r in enumerate(res[:8]):
        kb.append([B(f"{i + 1}. {(r.get('title') or '')[:20]}", f"mps:{i}")])
    kb.append([B("❌ 关闭", "close")])
    await wait.edit("\n".join(lines)[:3500], reply_markup=KM(kb),
                    disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^mps:"))
@perm.gate_cb("mp")
async def cb_mps(client, q):
    uid = q.from_user.id
    c = _MPQ.get(uid) or {}
    lst = c.get("list") or []
    try:
        idx = int((q.data or "").split(":")[1])
    except Exception:
        await q.answer(); return
    if idx >= len(lst):
        await q.answer("结果已过期，请重新搜索", show_alert=True)
        return
    r = lst[idx]
    R = _mp()
    ok, tip, _sid = await asyncio.to_thread(R.mp_subscribe, r, None)
    await q.answer()
    await q.message.reply(
        (f"✅ **已订阅 · 自动下载**\n{ui.divider()}\n"
         f"🎬 {ui.safe(r.get('title'))[:30]}"
         + (f"（{r.get('year')}）" if r.get("year") else "") +
         f"\n{tip}\n{ui.divider()}\n"
         "💡 MP 出种后自动下载 → 完成会**私聊**通知你（不刷群）")
        if ok else
        f"⚠️ 订阅失败：{ui.safe(str(tip))[:100]}",
        disable_web_page_preview=True)


@bot.on_message(filters.command(["mpsubs"], **CMD))
@perm.gate("mp")
async def cmd_mpsubs(client, msg: Message):
    """订阅管理：立即搜索 / 删除"""
    R = _mp()
    subs = await asyncio.to_thread(R.mp_subscribe_list, 30) or []
    if not subs:
        await msg.reply("📭 当前没有订阅\n\n💡 `/mpsearch 片名` 加一条")
        return
    lines = [f"📺 **订阅管理 · {len(subs)} 条**", ui.divider()]
    kb = []
    for s in subs[:12]:
        sid = int(s.get("id") or 0)
        nm = ui.safe(str(s.get("name") or ""))[:24]
        y = f"（{s.get('year')}）" if s.get("year") else ""
        lines.append(f"· **{nm}**{y}　`#{sid}`")
        kb.append([B(f"🔍 {nm[:14]} 立即搜索", f"mpu:kick:{sid}"),
                   B("❌ 删除", f"mpu:del:{sid}")])
    kb.append([B("❌ 关闭", "close")])
    lines += [ui.divider(), "⚠️ 删除要点两次（防手滑）"]
    await msg.reply("\n".join(lines)[:3500], reply_markup=KM(kb),
                    disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^mpu:"))
@perm.gate_cb("mp")
async def cb_mpu(client, q):
    seg = (q.data or "").split(":")
    act = seg[1] if len(seg) > 1 else ""
    try:
        sid = int(seg[2])
    except Exception:
        await q.answer(); return
    R = _mp()
    if act == "kick":
        ok = await asyncio.to_thread(R.mp_kick_search, sid)
        await q.answer("已触发搜索" if ok else "触发失败", show_alert=True)
        return
    if act == "del":
        key = f"{q.from_user.id}:mpsubdel:{sid}"
        if not _ask(key):
            await q.answer("再点一次确认删除", show_alert=True)
            return
        ok = await asyncio.to_thread(R.mp_subscribe_del, sid)
        await q.answer("已删除" if ok else "删除失败", show_alert=True)
        return
    await q.answer()


@bot.on_message(filters.command(["mpsites"], **CMD))
@perm.gate("mp")
async def cmd_mpsites(client, msg: Message):
    """站点管理：站点列表 + 启用状态"""
    R = _mp()
    j = await asyncio.to_thread(R._req, "GET", "/api/v1/site/")
    data = ((j or {}).get("data") or []) if isinstance(j, dict) else []
    if not data:
        await msg.reply("⚠️ 没读到站点（MP 未就绪？）")
        return
    lines = [f"🌐 **PT 站点 · {len(data)} 个**", ui.divider()]
    for s in data:
        nm = ui.safe(str(s.get("name") or ""))[:18]
        dom = ui.safe(str(s.get("domain") or ""))[:26]
        on = "✅" if s.get("is_active") else ("⏸" if s.get("is_active") is False else "✅")
        lines.append(f"{on} **{nm}**　`{dom}`")
        rss = "有" if s.get("rss") else "无"
        lines.append(f"　 RSS {rss}　Cookie {'有' if s.get('cookie') else '无'}"
                     f"　限流 {s.get('limit_interval') or '-'}s")
        lines.append(ui.divider())
    lines.append("💡 站点增删请到 MP 网页版（改完用 `/syncsite` 同步下载盘目录）")
    await msg.reply("\n".join(lines)[:3500], disable_web_page_preview=True)


@bot.on_message(filters.command(["mpplugin"], **CMD))
@perm.gate("mp")
async def cmd_mpplugin(client, msg: Message):
    """插件与下载器状态"""
    R = _mp()
    j = await asyncio.to_thread(R._req, "GET", "/api/v1/plugin/")
    data = ((j or {}).get("data") or []) if isinstance(j, dict) else []
    inst = [p for p in data if p.get("installed")]
    on = [p for p in inst if p.get("state")]
    lines = [f"🧩 **插件 · 已装 {len(inst)} / 启用 {len(on)}**", ui.divider()]
    for p in inst:
        nm = ui.safe(str(p.get("plugin_name") or p.get("id") or ""))[:24]
        st = "✅" if p.get("state") else "⏸"
        lines.append(f"{st} {nm}　`v{p.get('plugin_version') or '-'}`")
    d = await asyncio.to_thread(R._req, "GET", "/api/v1/dashboard/statistic")
    lines += [ui.divider(),
              f"📊 媒体库：电影 {(d or {}).get('data', {}).get('movie_count', 0)} 部"
              if isinstance(d, dict) else "📊 媒体库读取失败",
              "💡 插件开关请到 MP 网页版（这边只做状态速览）"]
    await msg.reply("\n".join(lines)[:3500], disable_web_page_preview=True)


# ════════════════════════════════════════════════════════════════════
#  qBittorrent
# ════════════════════════════════════════════════════════════════════
_QB = {"s": None, "ts": 0.0}


def qb_session():
    """登录并保持 Session（★ qB 5.x 登录成功返回 204，且后续必须复用同一会话）"""
    cfg = getattr(S, "QB", {}) or {}
    if not cfg.get("url"):
        return None
    now = time.time()
    if _QB["s"] is not None and now - _QB["ts"] < 1500:
        return _QB["s"]
    try:
        s = requests.Session()
        r = s.post(cfg["url"].rstrip("/") + "/api/v2/auth/login",
                   data={"username": cfg.get("username", ""),
                         "password": cfg.get("password", "")},
                   timeout=_TIMEOUT)
        if r.status_code in (200, 204):      # 5.x 是 204 No Content
            _QB.update(s=s, ts=now)
            return s
        logger.warning(f"QB 登录失败 {r.status_code}")
    except Exception as e:
        logger.warning(f"QB 连接异常: {e}")
    return None


def qb_get(path, **kw):
    s = qb_session()
    if s is None:
        return None
    cfg = getattr(S, "QB", {}) or {}
    try:
        kw.setdefault("timeout", _TIMEOUT)
        r = s.get(cfg["url"].rstrip("/") + path, **kw)
        if r.status_code == 403:
            _QB.update(s=None, ts=0.0)
            s = qb_session()
            if s is None:
                return None
            r = s.get(cfg["url"].rstrip("/") + path, **kw)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"QB GET {path} 失败: {e}")
        return None


# qBittorrent 5.x（本机 v5.2.3 / WebAPI 2.15.1）删掉了 pause/resume，
# 改名为 stop/start，老端点返回 404 ⇒ 这里做自动降级，两个版本都能用。
_QB_ALT_PATH = {
    "/api/v2/torrents/pause": "/api/v2/torrents/stop",
    "/api/v2/torrents/resume": "/api/v2/torrents/start",
    "/api/v2/torrents/stop": "/api/v2/torrents/pause",
    "/api/v2/torrents/start": "/api/v2/torrents/resume",
}


def qb_post(path, data=None, **kw):
    s = qb_session()
    if s is None:
        return False
    cfg = getattr(S, "QB", {}) or {}
    for p in (path, _QB_ALT_PATH.get(path)):
        if not p:
            continue
        try:
            kw.setdefault("timeout", _TIMEOUT)
            r = s.post(cfg["url"].rstrip("/") + p, data=data, **kw)
            # 202 = qB 5.x 异步受理（torrents/add 等），种子实际会入队
            if r.status_code in (200, 202, 204):
                return True
            logger.debug(f"QB POST {p} -> {r.status_code}")
        except Exception as e:
            logger.debug(f"QB POST {p} 失败: {e}")
    return False


# qB 的做种状态（v5.x 官方状态机）：哪些算「在做种」
_SEED_STATES = ("uploading", "stalledUP", "pausedUP", "queuedUP",
                "forcedUP", "checkingUP")
_STATE_TXT = {
    "uploading": "⬆️ 正在上传", "stalledUP": "⏸ 做种中(无连接)",
    "pausedUP": "⏹ 已暂停(做种)", "queuedUP": "🕒 排队做种",
    "forcedUP": "⚡ 强制做种", "checkingUP": "🔍 校验中",
    "downloading": "⬇️ 下载中", "stalledDL": "⏸ 下载中(无连接)",
    "pausedDL": "⏹ 已暂停(下载)", "queuedDL": "🕒 排队下载",
    "forcedDL": "⚡ 强制下载", "checkingDL": "🔍 校验中",
    "error": "⚠️ 出错", "missingFiles": "⚠️ 文件缺失",
    "checkingResumeData": "🔍 恢复校验", "moving": "📦 移动中",
    "unknown": "❓ 未知",
}


def _state_txt(s: str) -> str:
    return _STATE_TXT.get(str(s or ""), str(s or "-"))


def _dur(sec) -> str:
    """秒 → 「3天5小时」这类可读时长"""
    try:
        n = int(sec or 0)
    except Exception:
        return "—"
    if n <= 0:
        return "—"
    d, n = divmod(n, 86400)
    h, n = divmod(n, 3600)
    m = n // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分"


def seed_torrents() -> list[dict]:
    """正在做种的种子（按已上传排序）"""
    tors = qb_get("/api/v2/torrents/info") or []
    seed = [t for t in tors if str(t.get("state") or "") in _SEED_STATES]
    seed.sort(key=lambda t: -int(t.get("uploaded") or 0))
    return seed


def seed_text(limit: int = 10) -> str:
    """做种明细（用户 2026-10-13：「做种我看不到，只能看到数量」）"""
    tors = qb_get("/api/v2/torrents/info") or []
    if not tors:
        return "📭 qB 里没有种子"
    seed = seed_torrents()
    lines = [f"🌱 **做种明细 · 共 {len(seed)} 个做种 / {len(tors)} 个种子**",
             ui.divider()]
    if not seed:
        lines.append("当前没有正在做种的种子")
        return "\n".join(lines)[:3500]
    tot_up = sum(int(t.get("uploaded") or 0) for t in seed)
    lines.append(f"📤 做种累计上传 **{_hum(tot_up)}**　"
                 f"（前 {min(limit, len(seed))} 个明细如下）")
    lines.append(ui.divider())
    for i, t in enumerate(seed[:limit], 1):
        r = float(t.get("ratio") or 0)
        lines.append(f"{i}. **{ui.safe(str(t.get('name') or ''))[:34]}**")
        lines.append(f"　 分享率 `{r:.2f}`　已传 `{_hum(t.get('uploaded'))}`"
                     f"　大小 `{_hum(t.get('size'))}`")
        lines.append(f"　 {_state_txt(t.get('state'))}　做种时长 "
                     f"`{_dur(t.get('seeding_time'))}`"
                     f"　分类 `{t.get('category') or '-'}`")
        lines.append(ui.divider())
    if len(seed) > limit:
        lines.append(f"…… 另有 {len(seed) - limit} 个在做种")
    lines.append("💡 `/qb seed 20` 看更多　`/qb cat` 按分类统计")
    return "\n".join(lines)[:3500]


def cat_text() -> str:
    """按分类统计做种/下载（一眼看出哪个站在传）"""
    tors = qb_get("/api/v2/torrents/info") or []
    if not tors:
        return "📭 qB 里没有种子"
    agg: dict[str, dict] = {}
    for t in tors:
        c = str(t.get("category") or "未分类")
        d = agg.setdefault(c, {"n": 0, "seed": 0, "up": 0, "size": 0})
        d["n"] += 1
        if str(t.get("state") or "") in _SEED_STATES:
            d["seed"] += 1
        d["up"] += int(t.get("uploaded") or 0)
        d["size"] += int(t.get("size") or 0)
    lines = ["🗂 **分类统计**", ui.divider()]
    for c, d in sorted(agg.items(), key=lambda x: -x[1]["up"]):
        lines.append(f"**{ui.safe(c)}**　种子 {d['n']}　做种 {d['seed']}")
        lines.append(f"　 已传 `{_hum(d['up'])}`　总量 `{_hum(d['size'])}`")
    return "\n".join(lines)[:3500]


def qb_overview() -> str:
    info = qb_get("/api/v2/sync/maindata") or {}
    st = (info.get("server_state") or {}) if isinstance(info, dict) else {}
    tors = qb_get("/api/v2/torrents/info") or []
    dl = [t for t in tors if t.get("dlspeed", 0) > 0]
    up = [t for t in tors if t.get("upspeed", 0) > 0]
    dls = sum(int(t.get("dlspeed") or 0) for t in tors)
    ups = sum(int(t.get("upspeed") or 0) for t in tors)
    seed = seed_torrents()
    tot_up = sum(int(t.get("uploaded") or 0) for t in tors)
    lines = ["🧲 **qBittorrent**", ui.divider(),
             f"🔽 下载 **{len(dl)}** 个　⚡ {_spd(dls)}",
             f"🔼 做种 **{len(seed)}** 个（有速度 {len(up)}）　⚡ {_spd(ups)}",
             f"📦 种子总数 **{len(tors)}**　📤 累计上传 **{_hum(tot_up)}**"]
    # ★ 用户 2026-10-13：只看得到数量看不到是哪些 ⇒ 概览里直接列做种 Top3
    if seed:
        lines.append(ui.divider())
        lines.append("🌱 **做种 Top 3**")
        for i, t in enumerate(seed[:3], 1):
            r = float(t.get("ratio") or 0)
            lines.append(f"　{i}. {ui.safe(str(t.get('name') or ''))[:26]}")
            lines.append(f"　　 分享率 `{r:.2f}`　已传 "
                         f"`{_hum(t.get('uploaded'))}`　"
                         f"`{_dur(t.get('seeding_time'))}`")
    free = st.get("free_space_on_disk")
    if free is not None:
        lines.append(f"💽 下载盘剩余 **{_hum(free)}**")
    if st.get("use_alt_speed_limits"):
        lines.append("🐢 限速模式：**已开启**")
    lines += [ui.divider(),
              "💡 `/qb seed` 做种明细　`/qb cat` 分类统计",
              "💡 `/qb list` 看下载　`/qb pause|resume` 暂停/恢复全部",
              "💡 `/qb alt` 切换限速"]
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["qb"], **CMD))
@perm.gate("qb")
async def cmd_qb(client, msg: Message):
    """qBittorrent 管理（群主私聊）"""
    arg = (msg.text or "").split()
    a = arg[1].lower() if len(arg) > 1 else ""
    if a == "list":
        tors = await asyncio.to_thread(qb_get, "/api/v2/torrents/info") or []
        if not tors:
            await msg.reply("📭 没有种子")
            return
        tors = sorted(tors, key=lambda t: -(int(t.get("dlspeed") or 0)))[:15]
        lines = [f"🧲 **种子 · 前 {len(tors)} 个**", ui.divider()]
        for i, t in enumerate(tors, 1):
            nm = ui.safe(str(t.get("name") or ""))[:30]
            p = float(t.get("progress") or 0) * 100
            sp = int(t.get("dlspeed") or 0)
            lines.append(f"{i}. **{nm}**")
            lines.append(f"　 `{p:.0f}%`　{_spd(sp) if sp else '⏸ 无速度'}"
                         f"　{_hum(t.get('size'))}")
            lines.append(ui.divider())
        await msg.reply("\n".join(lines)[:3500], disable_web_page_preview=True)
        return
    if a == "seed":
        # ★ 用户要「看得到做种是哪些」，不是只有数量
        try:
            n = int(arg[2]) if len(arg) > 2 else 10
        except Exception:
            n = 10
        await msg.reply(await asyncio.to_thread(seed_text, max(1, min(n, 40))),
                        disable_web_page_preview=True)
        return
    if a == "cat":
        await msg.reply(await asyncio.to_thread(cat_text),
                        disable_web_page_preview=True)
        return
    if a in ("pause", "resume", "alt"):
        if a == "pause":
            ok = await asyncio.to_thread(qb_post, "/api/v2/torrents/pause",
                                         {"hashes": "all"})
            tip = "⏸ 已暂停全部种子" if ok else "⚠️ 暂停失败"
        elif a == "resume":
            ok = await asyncio.to_thread(qb_post, "/api/v2/torrents/resume",
                                         {"hashes": "all"})
            tip = "▶️ 已恢复全部种子" if ok else "⚠️ 恢复失败"
        else:
            st = await asyncio.to_thread(qb_get, "/api/v2/sync/maindata") or {}
            cur = bool((st.get("server_state") or {}).get("use_alt_speed_limits"))
            ok = await asyncio.to_thread(qb_post, "/api/v2/torrents/toggleSpeedLimitsMode")
            tip = f"🐢 限速已{'关闭' if cur else '开启'}" if ok else "⚠️ 切换失败"
        await msg.reply(tip)
        return
    await msg.reply(await asyncio.to_thread(qb_overview),
                    disable_web_page_preview=True)


# ════════════════════════════════════════════════════════════════════
#  Emby
# ════════════════════════════════════════════════════════════════════
def _emby(path, method="GET", **kw):
    cfg = S.EMBY or {}
    if not cfg.get("url") or not cfg.get("api_key"):
        return None
    url = cfg["url"].rstrip("/") + path
    p = dict(kw.pop("params", {}) or {})
    p["api_key"] = cfg["api_key"]
    try:
        kw.setdefault("timeout", _TIMEOUT)
        if method == "POST":
            return requests.post(url, params=p, **kw)
        return requests.get(url, params=p, **kw)
    except Exception as e:
        logger.debug(f"Emby {path} 失败: {e}")
        return None


def emby_overview() -> str:
    r = _emby("/emby/System/Info")
    if r is None or r.status_code != 200:
        return "📺 **Emby**\n" + ui.divider() + "\n⚠️ 连不上 Emby"
    info = r.json() or {}
    lines = ["📺 **Emby**", ui.divider(),
             f"🏷 版本 **{info.get('Version', '-')}**",
             f"📛 服务名 **{info.get('ServerName', '-')}**"]
    lib = _emby("/emby/Library/VirtualFolders")
    if lib is not None and lib.status_code == 200:
        items = lib.json() or []
        lines.append(f"🗂 媒体库 **{len(items)}** 个")
        for it in items[:10]:
            nm = ui.safe(str(it.get("Name") or ""))[:20]
            lines.append(f"　· {nm}")
    sess = _emby("/emby/Sessions")
    if sess is not None and sess.status_code == 200:
        ss = sess.json() or []
        play = [s for s in ss if s.get("NowPlayingItem")]
        lines.append(f"👥 在线会话 **{len(ss)}**　▶️ 正在播放 **{len(play)}**")
        for s in play[:5]:
            nm = ui.safe(str(s.get("UserName") or ""))[:12]
            itm = s.get("NowPlayingItem") or {}
            t = ui.safe(str(itm.get("Name") or ""))[:20]
            lines.append(f"　· {nm}　{t}")
    lines += [ui.divider(), "💡 `/emby refresh` 触发一次媒体库扫描"]
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["emby"], **CMD))
@perm.gate("emby")
async def cmd_emby(client, msg: Message):
    """Emby 管理（群主私聊）"""
    arg = (msg.text or "").split()
    a = arg[1].lower() if len(arg) > 1 else ""
    if a == "refresh":
        r = await asyncio.to_thread(_emby, "/emby/Library/Refresh", "POST")
        ok = r is not None and r.status_code in (200, 204)
        await msg.reply("🔄 已触发媒体库扫描" if ok else "⚠️ 触发失败")
        return
    await msg.reply(await asyncio.to_thread(emby_overview),
                    disable_web_page_preview=True)


# ════════════════════════════════════════════════════════════════════
#  MediaTidy / 115
# ════════════════════════════════════════════════════════════════════
_MT = {"tok": "", "ts": 0.0}


def mt_token(force: bool = False) -> str:
    cfg = getattr(S, "MT", {}) or {}
    if not cfg.get("enable") or not cfg.get("url"):
        return ""
    if not force and _MT["tok"] and time.time() - _MT["ts"] < 3000:
        return _MT["tok"]
    try:
        r = requests.post(cfg["url"].rstrip("/") + "/api/auth/login",
                          json={"Username": cfg.get("username", ""),
                                "Password": cfg.get("password", "")},
                          timeout=_TIMEOUT)
        tok = ((r.json() or {}).get("data") or {}).get("token")
        if tok:
            _MT.update(tok=tok, ts=time.time())
            return tok
    except Exception as e:
        logger.debug(f"MT 登录失败: {e}")
    return ""


def mt_get(path, **kw):
    tok = mt_token()
    if not tok:
        return None
    cfg = getattr(S, "MT", {}) or {}
    try:
        kw.setdefault("timeout", _TIMEOUT)
        r = requests.get(cfg["url"].rstrip("/") + path,
                         headers={"Authorization": f"Bearer {tok}"}, **kw)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"MT GET {path} 失败: {e}")
        return None


def _mt_data(j) -> list:
    """MT 所有接口都是 {"code":0,"data":[...]}，这里统一拆一层"""
    if isinstance(j, dict):
        d = j.get("data")
        return d if isinstance(d, list) else []
    return j if isinstance(j, list) else []


def mt_overview() -> str:
    lines = ["🗂 **MediaTidy**", ui.divider()]
    if not mt_token():
        lines.append("⚠️ 连不上 MediaTidy")
        return "\n".join(lines)
    lines.append("✅ 已连接")
    org = _mt_data(mt_get("/api/organize/tasks"))
    if org:
        run = [t for t in org if str(t.get("status") or "").lower() in
               ("running", "pending", "processing")]
        done = [t for t in org if str(t.get("status") or "").lower() in
                ("completed", "success", "done")]
        lines.append(f"🧾 整理任务 **{len(org)}** 条　在跑 **{len(run)}**　"
                     f"完成 **{len(done)}**")
        for t in run[:4]:
            tot = int(t.get("totalFiles") or 0)
            dn = int(t.get("doneFiles") or 0)
            err = int(t.get("errorFiles") or 0)
            pct = (dn / tot * 100) if tot else 0
            src = ui.safe(str(t.get("sourceDir") or ""))[:26]
            lines.append(f"　▶️ `{pct:.0f}%` {dn}/{tot}　⚠️{err}　{src}")
    prof = _mt_data(mt_get("/api/organize/profiles"))
    if prof:
        on = [p for p in prof if p.get("enabled")]
        lines.append(f"⚙️ 整理方案 **{len(prof)}** 套（启用 **{len(on)}**）")
    strm = _mt_data(mt_get("/api/strm/profiles"))
    if strm:
        lines.append(f"🔗 strm 方案 **{len(strm)}** 套")
    if not (org or prof or strm):
        lines.append("（接口没返回数据，可能 token 已过期）")
    lines += [ui.divider(), "💡 `/mt cloud` 看 115 云端待整理积压"]
    return "\n".join(lines)[:3500]


_CLOUD_BASE = "/主号/待整理/PT"


async def _cloud_text(path: str) -> str:
    cfg = getattr(S, "MT", {}) or {}
    acc = cfg.get("account_id", 2)
    j = await asyncio.to_thread(mt_get, "/api/drive/files",
                                params={"accountId": acc, "path": path})
    items = _mt_data(j)
    if not items:
        return (f"☁️ **{ui.safe(path)[-32:]}**\n{ui.divider()}\n"
                "📭 这里是空的（或读取失败）")
    lines = [f"☁️ **{ui.safe(path)[-34:]}**　共 {len(items)} 项", ui.divider()]
    for it in items[:20]:
        nm = ui.safe(str(it.get("name") or ""))[:30]
        isdir = bool(it.get("isDir") or it.get("is_dir"))
        kind = "📁" if isdir else "📄"
        size = _hum(it.get("size")) if it.get("size") else ""
        lines.append(f"{kind} {nm}" + (f"　{size}" if size else ""))
    lines.append(ui.divider())
    lines.append("💡 看下一层：`/mt cloud 求片`　（可继续 `/mt cloud 求片/咖啡站`）")
    return "\n".join(lines)[:3500]


@bot.on_message(filters.command(["mt"], **CMD))
@perm.gate("mt")
async def cmd_mt(client, msg: Message):
    """MediaTidy 管理（群主私聊）"""
    arg = (msg.text or "").split()
    a = arg[1].lower() if len(arg) > 1 else ""
    if a == "cloud":
        sub = " ".join(arg[2:]).strip().strip("/") if len(arg) > 2 else ""
        path = _CLOUD_BASE + ("/" + sub if sub else "")
        await msg.reply(await _cloud_text(path), disable_web_page_preview=True)
        return
    await msg.reply(await asyncio.to_thread(mt_overview),
                    disable_web_page_preview=True)


@bot.on_message(filters.command(["cd2"], **CMD))
@perm.gate("cd2")
async def cmd_cd2(client, msg: Message):
    """115 云盘 / CloudDrive2 状态（群主私聊）"""
    cfg = getattr(S, "CD2", {}) or {}
    lines = ["☁️ **115 云盘**", ui.divider()]
    if cfg.get("url"):
        try:
            r = await asyncio.to_thread(requests.get, cfg["url"].rstrip("/"),
                                        {"timeout": 8})
            lines.append(f"🔌 CloudDrive2：{'✅ 在线' if r.status_code == 200 else '⚠️ ' + str(r.status_code)}")
        except Exception as e:
            lines.append(f"🔌 CloudDrive2：⚠️ {ui.safe(str(e))[:40]}")
    acc = (getattr(S, "MT", {}) or {}).get("account_id", 2)
    j = await asyncio.to_thread(mt_get, "/api/drive/files",
                                params={"accountId": acc, "path": "/主号"})
    items = _mt_data(j)
    if items:
        lines.append(f"🗂 云端根目录 **{len(items)}** 项")
        for it in items[:12]:
            nm = ui.safe(str(it.get("name") or ""))[:24]
            size = _hum(it.get("size")) if it.get("size") else ""
            kind = "📁" if bool(it.get("isDir") or it.get("is_dir")) else "📄"
            lines.append(f"　{kind} {nm}" + (f"　{size}" if size else ""))
    else:
        lines.append("（云端目录读取失败或未登录）")
    lines += [ui.divider(), "💡 `/mt cloud` 看待整理积压　`/ptstatus` 看链路"]
    await msg.reply("\n".join(lines)[:3500], disable_web_page_preview=True)


# ════════════════════════════════════════════════════════════════════
#  赠送货币（/give）—— 群友之间也能送，但只能送自己的余额
# ════════════════════════════════════════════════════════════════════
@bot.on_message(filters.command(["give", "赠送", "转赠"], **CMD))
async def cmd_give(client, msg: Message):
    """赠送灵石给别人：`/give 100` 回复对方消息，或 `/give @用户名 100`"""
    from .. import db as _db

    uid = msg.from_user.id if msg.from_user else 0
    parts = (msg.text or "").split()
    rt = getattr(msg, "reply_to_message", None)
    target = None
    amount = None

    if rt is not None and rt.from_user and not rt.from_user.is_bot:
        target = int(rt.from_user.id)
        amount = parts[1] if len(parts) > 1 else ""
    else:
        # /give @user 100 或 /give 100 @user
        for p in parts[1:]:
            if p.isdigit():
                amount = p
            elif p.startswith("@"):
                try:
                    ch = await bot.get_chat(p)
                    target = int(ch.id)
                except Exception:
                    await msg.reply(f"⚠️ 找不到用户 {ui.safe(p)[:20]}")
                    return
            elif p.lstrip("-").isdigit() and target is None and len(p) > 6:
                target = int(p)
    if target is None or not amount or not amount.isdigit():
        await msg.reply(
            "🎁 **赠送灵石**\n" + ui.divider() + "\n"
            "用法（二选一）：\n"
            "　① **回复对方的消息**发 `/give 100`\n"
            "　② `/give @用户名 100`\n\n"
            "💡 只能送你自己账上的 🪙灵石，送完立刻到账。")
        return
    amount = int(amount)
    if amount <= 0:
        await msg.reply("⚠️ 数量得是正整数")
        return
    if target == uid:
        await msg.reply("🙃 不能送给自己")
        return
    mine = await asyncio.to_thread(_db.get_ls, uid)
    if mine < amount:
        await msg.reply(f"⚠️ 你的 🪙灵石只有 **{mine}**，不够送 {amount}")
        return
    if not await asyncio.to_thread(_db.try_spend_ls, uid, amount):
        await msg.reply("⚠️ 扣款失败，请稍后再试")
        return
    await asyncio.to_thread(_db.add_ls, target, amount)
    await asyncio.to_thread(_db.log_action, "give", uid, target, amount, "赠送灵石")
    who = f"[{ui.safe(_nm_of(rt or msg))[:18]}](tg://user?id={target})"
    await msg.reply(f"🎁 **赠送成功**\n{ui.divider()}\n"
                    f"送给 {who}　**{amount}** 🪙灵石\n"
                    f"你的余额：**{mine - amount}** 🪙",
                    disable_web_page_preview=True)
    try:
        await bot.send_message(
            target,
            f"🎁 **{_nm_of(msg)} 送了你 {amount} 🪙灵石**\n"
            f"{ui.divider()}\n余额：**{await asyncio.to_thread(_db.get_ls, target)}** 🪙")
    except Exception:
        pass


def _nm_of(msg) -> str:
    u = getattr(msg, "from_user", None)
    if u is None:
        return "有人"
    n = (getattr(u, "first_name", "") or "") + " " + (getattr(u, "last_name", "") or "")
    n = n.strip() or (getattr(u, "username", "") or "")
    return ui.safe(n)[:18] or f"UID{u.id}"
