"""pt2cloud 主循环：QB 完成 -> 上传 115 -> 云端校验 -> 等待 MT 整理 -> 通知 -> 磁盘守护"""
import logging
import os
import time
from datetime import datetime
from urllib.parse import urlparse

import config
import db
import media as media_mod
import mt as mt_mod
import qb as qb_mod
import quality as q_mod
import uploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pt2cloud")

qb = qb_mod.QB()
mt = mt_mod.MT()

_last_daily = None
_round = 0


# ---------------------------------------------------------------- 工具

def norm(s):
    # ★ 分隔符（点/下划线/连字符/空格）统一折叠成单个空格，否则
    #   种子名 avatar.seven.havens 与求片关键词 avatar seven havens 永远匹配不上
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (s or "")).split())


def detect_site(t):
    """tracker 域名 -> 站点目录名。

    ★ 2026-10-13（用户要求）：**以 MP 里的中文站点名为准**。
      以前只查 config.SITE_MAP（只有一条咖啡站），其余一律取域名主段，
      结果 115 上出现 zmpt / tracker / www 这种看不出是哪个站的目录。
      现在顺序是：MP 站点表 → 本地 SITE_MAP → 域名主段（最后兜底）。
    """
    host = ""
    try:
        for tr in qb.trackers(t["hash"]):
            url = (tr.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            h = urlparse(url).hostname or ""
            if not h or h.startswith("127.") or h.startswith("192.168."):
                continue
            host = h
            nm = media_mod.site_name_of(h)
            if nm:
                return nm
            break
    except Exception:
        pass
    if host:
        return host.split(".")[0]
    return config.DEFAULT_SITE


def detect_origin(name, wish):
    """判定种子来源：求片 / 订阅。

    ⚠️ 长度门槛按「是否中文」区分：中文片名大量是两字词（沙丘/哪吒/长津湖），
    原来统一要求 len>=3，会把它们全漏掉、求片被误判成订阅，
    导致本地目录标「求片」而 115 云端标「订阅」的错位。
    拉丁文名（Solo Leveling）保持 >=3 才够区分度。
    kfcgame 的 sortpath.origin_of 用的是同一套规则。
    """
    n = norm(name)
    for kw in wish:
        k = norm(kw)
        if not k:
            continue
        cjk = any("一" <= c <= "鿿" for c in k)
        if (len(k) >= 2 if cjk else len(k) >= 3) and k in n:
            return config.ORIGIN_WISH
    return config.ORIGIN_SUB


def guess_category(name, content_path):
    import re
    low = (name or "").lower()
    if re.search(r"(s\d{1,2}[. _-]?e\d{1,3}|第\s?\d+\s?集)", low):
        return "电视剧"
    if any(k in low for k in ("anime", "animation", "bdrip", "jav")):
        return "动漫"
    if any(k in low for k in ("documentary", "纪录片")):
        return "纪录片"
    if any(k in low for k in ("variety", "show", "综艺")):
        return "综艺"
    if os.path.isdir(content_path or ""):
        try:
            subs = os.listdir(content_path)
            if len(subs) > 3:
                return "电视剧"
        except Exception:
            pass
    return "电影"


# 与 config（以及 kfcgame 的 sortpath）共用同一套分类词，避免两边对不上
TOP_CLASSES = config.TOP_CLASSES


def real_category(name, content_path, qb_cat=""):
    """分类优先级：MP 识别 type > 本地目录大类(bot已按识别排好) > 合法 QB category > guess_category。

    ⚠️ QB category 不能优先：BrushFlow 会把刷流种标成「订阅」——那是来源维度，
    不是大类维度。曾经因此 115 落点变成 订阅/咖啡站/订阅。
    """
    try:
        mi = media_mod.recognize(name) or {}
        if (mi.get("type") or "") in TOP_CLASSES:
            return mi["type"]
    except Exception:
        pass
    if content_path:
        for p in reversed(content_path.replace("\\", "/").rstrip("/").split("/")):
            if p in TOP_CLASSES:
                return p
    qb = (qb_cat or "").strip()
    if qb in TOP_CLASSES:
        return qb
    return guess_category(name, content_path)


def disk_free_gb(path=config.DISK_PATH):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 ** 3


def disk_used_gb(path=config.DISK_PATH):
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * st.f_frsize / 1024 ** 3


# ---------------------------------------------------------------- 上传流程

def _req_extra(rec, disp: str = "") -> dict:
    """求片人反查 → add_notify extra（bot 端据此 @求片人）。非求片返回 {}。"""
    if (rec.get("origin") or "") != "求片":
        return {}
    try:
        w = db.wish_owner([rec.get("media_title"), disp, rec.get("torrent_name")])
        if w.get("tg_id"):
            return {"req_tg": w["tg_id"], "req_name": w.get("tg_name") or "",
                    "req_username": w.get("tg_username") or ""}
    except Exception as e:
        log.debug("求片人反查失败: %s", e)
    return {}


def probe_media(name):
    """用 MoviePilot 反查中文名/年份/TMDBID/海报，同时从种子名解析画质。
    种子名里没有的画质参数（DV Profile/色深/动态范围）用 Emby 反查补齐。"""
    info = q_mod.parse(name)
    mi = media_mod.recognize(name) or {}
    if mi.get("title"):
        mi["media_title"] = mi["title"]
    if mi.get("tmdb_id"):
        info = media_mod.enrich_quality(info, mi.get("tmdb_id"), mi.get("type") or "")
    return mi, info, q_mod.short(info)


def _fill_poster(media, rec):
    """海报兜底：MP 没给海报时用 Emby 条目图，保证所有影片通知都带图"""
    if not media.get("poster") and rec.get("tmdb_id"):
        try:
            media["poster"] = media_mod.emby_poster(
                rec.get("tmdb_id") or "", rec.get("media_type") or "")
        except Exception:
            pass


def bitrate_str(rec, total_size, file_cnt=0):
    """估算平均码率：体积*8 / 时长秒。剧集按单集体积算（时长取第一集）。

    返回 "x.x Mbps"，查不到片长返回 ""（bot 侧就不显示这一行）。
    """
    try:
        rt = media_mod.runtime_min(rec.get("tmdb_id") or "",
                                   rec.get("media_type") or "")
        if rt > 0 and int(total_size or 0) > 0:
            per = int(total_size)
            if (rec.get("media_type") or "") == "电视剧" and int(file_cnt or 0) > 1:
                per = total_size / int(file_cnt)
            return f"{per * 8 / (rt * 60) / 1e6:.1f} Mbps"
    except Exception:
        pass
    return ""


def n_title(mi, name):
    disp = mi.get("media_title") or name
    yr = f"（{mi['year']}）" if mi.get("year") else ""
    return f"{disp}{yr}"


def ui_divider():
    return "────────────"


QB_ETA_UNKNOWN = 8640000        # QB 用这个值表示「剩余时间未知」


def _eta(t):
    v = int(t.get("eta") or 0)
    return 0 if v >= QB_ETA_UNKNOWN else v


def _is_active(t):
    """是否真的在下载。

    PT 站经常出现「0 做种者」的种子，加进去就 stalled，进度永远 0。
    这类种子不应该发群通知（否则一堆 0% 的卡片刷屏），
    只在 /dl 面板里显示为「等待种源」。
    """
    try:
        prog = float(t.get("progress") or 0)
    except Exception:
        prog = 0
    return int(t.get("dlspeed") or 0) > 0 or prog >= 0.005


def _rec_title(rec):
    disp = rec.get("media_title") or rec.get("torrent_name") or ""
    yr = f"（{rec['year']}）" if rec.get("year") else ""
    return f"{disp}{yr}"


def announce_downloading(t, rec):
    """发出「开始下载」通知（群内一条会原地刷新进度条的消息）"""
    mi = {k: (rec.get(k) or "")
          for k in ("tmdb_id", "poster", "media_title", "year", "media_type")}
    total = int(rec.get("total_size") or t.get("size") or 0)
    _fill_poster(mi, rec)
    nid = db.add_notify("downloading", _rec_title(rec), "",
                        rec.get("site") or "", rec.get("category") or "",
                        rec.get("origin") or "", "", mi, total / 1024 ** 3,
                        rec.get("quality") or "", rec.get("torrent_name") or "",
                        stage="downloading", total_size=total,
                        progress=float(rec.get("progress") or 0),
                        speed_bps=int(rec.get("speed_bps") or 0),
                        eta_sec=int(rec.get("eta_sec") or 0),
                        done_size=int(rec.get("dl_size") or 0),
                        bitrate=bitrate_str(rec, total))
    if nid:
        db.set_notify_id(rec["torrent_hash"], nid)
        log.info("[播报] 开始下载 %s",
                 (rec.get("media_title") or rec.get("torrent_name") or "")[:40])
    return nid


def ensure_downloading(t, wish, announce=True):
    """种子刚进 QB（还没下完）→ 登记。

    announce=False 时只登记不播报（用于 stalled 种子，避免刷屏）。
    """
    h = t["hash"]
    name = t.get("name") or ""
    cat = real_category(name, t.get("content_path"), t.get("category"))
    site = detect_site(t)
    origin = detect_origin(name, wish)
    _cloud_dir, _rel = uploader.cloud_target(origin, site, cat, name)

    mi, info, qstr = probe_media(name)
    total = int(t.get("size") or 0)
    prog = float(t.get("progress") or 0) * 100
    spd = int(t.get("dlspeed") or 0)
    eta = _eta(t)
    dl = int(t.get("downloaded") or 0)

    db.upsert_upload(h, name, cat, site, origin, t.get("content_path") or "",
                     _cloud_dir, total, 0, "downloading", "下载中", mi, qstr)
    db.mark_start(h)
    db.update_progress(h, prog, spd, eta, dl, int(t.get("uploaded") or 0),
                       t.get("state") or "")
    rec = db.get_upload(h)
    if rec and announce:
        announce_downloading(t, rec)
    log.info("[登记下载] %s | %s | %.2f GB | %s",
             (mi.get("media_title") or name)[:40], qstr or "画质未知",
             total / 1024 ** 3, t.get("state") or "-")
    return rec


def ensure_record(t, wish):
    """种子下完 → 补齐真实文件清单/体积，转入待上传。

    ⚠️ 这里**不发**「下载完成」通知：按用户要求，必须等 115 网盘里
    真的出现完整文件后才播报下载成功（见 do_verify）。
    """
    h = t["hash"]
    name = t.get("name") or ""
    cat = real_category(name, t.get("content_path"), t.get("category"))
    site = detect_site(t)
    origin = detect_origin(name, wish)
    files = uploader.scan_files(t.get("content_path") or "")
    total = sum(f[2] for f in files)
    cloud_dir, _rel = uploader.cloud_target(origin, site, cat, name)

    mi, info, qstr = probe_media(name)

    if not files:
        db.upsert_upload(h, name, cat, site, origin, t.get("content_path") or "",
                         cloud_dir, 0, 0, "skipped", "无可上传的视频文件",
                         mi, qstr)
        return None

    db.upsert_upload(h, name, cat, site, origin, t.get("content_path") or "",
                     cloud_dir, total, len(files), "pending", "待上传", mi, qstr)
    db.mark_done_time(h)
    rec = db.get_upload(h)

    # 进度消息翻转成「上传中」
    if rec and rec.get("notify_id"):
        db.mark_edit(rec["notify_id"], stage="uploading", progress=100.0,
                     done_size=total, total_size=total, speed_bps=0, eta_sec=0)
        db.update_progress(h, 100.0, 0, 0, total, 0, t.get("state") or "")
    log.info("[下载完成] %s | %s | %.2f GB / %d 个文件",
             (mi.get("media_title") or name)[:40], qstr or "画质未知",
             total / 1024 ** 3, len(files))
    return rec


def refresh_progress(t, rec):
    """下载中：刷新实时进度，并让 bot 就地刷新那条群消息"""
    h = t["hash"]
    prog = float(t.get("progress") or 0) * 100
    spd = int(t.get("dlspeed") or 0)
    eta = _eta(t)
    dl = int(t.get("downloaded") or 0)
    up = int(t.get("uploaded") or 0)
    db.update_progress(h, prog, spd, eta, dl, up, t.get("state") or "")

    # 进度没实质变化就不打扰 TG（避免无意义的消息编辑）
    try:
        old = float(rec.get("progress") or 0)
    except Exception:
        old = -1
    if abs(prog - old) < 0.3 and int(rec.get("speed_bps") or 0) == spd:
        return

    nid = rec.get("notify_id") or 0
    if nid:
        db.mark_edit(nid, stage="downloading", progress=prog, speed_bps=spd,
                     eta_sec=eta, done_size=dl,
                     total_size=int(rec.get("total_size") or t.get("size") or 0))


def _cloud_body(rec, nfile, size_gb):
    """下载完成（115 已落盘）播报正文"""
    lines = []
    if rec.get("quality"):
        lines.append(f"🎞 画质｜{rec['quality']}")
    lines.append(f"💾 体积｜{size_gb:.2f} GB　📄 {nfile} 个文件")
    return "\n".join(lines)


def do_upload(rec, t):
    files = uploader.scan_files(t.get("content_path") or "")
    if not files:
        db.set_status(rec["torrent_hash"], "skipped", "源文件已不存在")
        return
    dst = rec["cloud_dir"]
    done, moved = uploader.copy_to_cloud(files, t.get("content_path") or "", dst)
    log.info("上传 %s：%d/%d 个文件，%.1f GB -> %s",
             rec["torrent_name"][:40], done, len(files), moved / 1024 ** 3, dst)
    db.set_status(rec["torrent_hash"], "verifying",
                  f"已写入挂载点 {done}/{len(files)}，等待云端落盘")


def do_verify(rec, t=None):
    rel = os.path.relpath(rec["cloud_dir"], config.CLOUD_ROOT)
    cloud_path = uploader.mt_path(rel)
    res = mt.walk(cloud_path)
    if res is None:
        return
    cnt, total = res
    if cnt >= rec["file_count"] and total >= rec["total_size"]:
        h = rec["torrent_hash"]
        db.set_status(h, "uploaded", "云端校验通过，等待 MT 整理")
        qb.add_tags([h], [config.DONE_TAG])
        db.update_progress(h, 100.0, 0, 0, total, 0, "uploaded")

        # ── 115 里真的出现完整文件了，才播报「下载完成」 ──
        size_gb = total / 1024 ** 3
        media = {k: (rec.get(k) or "")
                 for k in ("tmdb_id", "poster", "media_title", "year", "media_type")}
        _fill_poster(media, rec)
        disp = media["media_title"] or rec["torrent_name"]
        yr = f"（{media['year']}）" if media["year"] else ""
        db.add_notify("downloaded", f"下载完成：{disp}{yr}",
                      _cloud_body(rec, cnt, size_gb),
                      rec.get("site") or "", rec.get("category") or "",
                      rec.get("origin") or "", "", media, size_gb,
                      rec.get("quality") or "", rec.get("torrent_name") or "",
                      stage="uploaded", total_size=total, progress=100.0,
                      done_size=total,
                      bitrate=bitrate_str(rec, total, cnt),
                      extra=_req_extra(rec, disp))
        if rec.get("notify_id"):
            # stage=finished 告诉 bot：撤掉进度卡片，改发带图的完成播报
            db.mark_edit(rec["notify_id"], stage="finished", progress=100.0,
                         done_size=total, total_size=total,
                         speed_bps=0, eta_sec=0)
        log.info("[OK] 云端校验通过 %s（%d 个 / %.2f GB）",
                 rec["torrent_name"][:40], cnt, size_gb)
        # ★ 2026-10-23：保号（标签/分类）永不删本地；其余（求片/手动/订阅/刷流）
        #   云端确认完整后删本地 + 移除 qB 种子，腾出下载盘空间。
        #   保留 pt_uploads 记录与完成状态（status=uploaded，后续照常等 MT 整理）。
        if config.REQ_DELETE_LOCAL:
            _keep_local = False
            if t is not None:
                _ttags = {x.strip() for x in str(t.get("tags") or "").split(",") if x.strip()}
                if ("保号" in _ttags) or (t.get("category") == "keepalive"):
                    _keep_local = True
            if not _keep_local:
                try:
                    qb.delete([h], with_files=True)
                    db.set_status(h, "uploaded", "云端已完整，本地已清理（不做种）")
                    log.info("[本地清理] 已删本地并移除种子 %s", rec["torrent_name"][:40])
                except Exception as e:
                    log.warning("[本地清理] 失败 %s: %s", rec["torrent_name"][:30], e)
        return
    age = 0
    if rec.get("updated_at"):
        try:
            age = (datetime.now() - rec["updated_at"]).total_seconds()
        except Exception:
            age = 0
    if age > config.VERIFY_TIMEOUT:
        db.set_status(rec["torrent_hash"], "failed", "云端校验超时",
                      f"期望 {rec['file_count']} 个/{rec['total_size']} 字节，"
                      f"实际 {cnt} 个/{total} 字节")
        db.add_notify("failed", f"上传超时：{rec['torrent_name']}",
                      f"云端只到 {cnt}/{rec['file_count']} 个文件", rec["site"])
        log.warning("[FAIL] 校验超时 %s", rec["torrent_name"][:40])


def check_organized(rec):
    """MT 是 move 模式：源目录被搬空即代表已整理（但 strm 可能还没生成）"""
    rel = os.path.relpath(rec["cloud_dir"], config.CLOUD_ROOT)
    items = mt.list_dir(uploader.mt_path(rel))
    # ⚠️ MT move 后可能把源目录连根删除（连空壳都不留）——目录消失比空目录更是铁证
    if items is None or len(items) == 0:
        db.set_status(rec["torrent_hash"], "organized", "MT 已整理，等待 strm 生成")
        log.info("[整理完成] %s，等待 strm", rec["torrent_name"][:40])


def find_strm(tmdb_id: str):
    """在 strm 目录里找 [tmdbid-xxx] 对应的条目；返回相对路径列表"""
    if not tmdb_id or not os.path.isdir(config.STRM_ROOT):
        return []
    tag = f"[tmdbid-{tmdb_id}]"
    hits = []
    for root, _dirs, files in os.walk(config.STRM_ROOT):
        if tag in os.path.basename(root):
            hits.append(os.path.relpath(root, config.STRM_ROOT))
            if len(hits) >= 3:
                break
    return hits


def check_archived(rec):
    """入库通知的**唯一出口**（2026-10-14 全面重写，替罪事故复盘）：

    ① strm 已生成（原有逻辑）
    ② ★ 可播校验：Emby 真的扫出了条目（电影=可播 MediaSource /
       剧集=≥1 集有 MediaSource）才发「可看了」。没扫出就每轮等，
       超过 PLAY_WAIT 仍不可播 → 降级为「入库处理中」中性通知（不误报可看）。
    ③ ★ 分集去重：同 tmdb_id 3 小时内只发一条「已入库」（替罪 5 个分集
       种子曾刷屏 6 条）。
    ④ ★ 带求片人：通知行附 req_tg/req_name/req_username，bot 端负责 @。
    """
    h = rec["torrent_hash"]
    tmdb = rec.get("tmdb_id") or ""
    age = 0
    if rec.get("updated_at"):
        try:
            age = (datetime.now() - rec["updated_at"]).total_seconds()
        except Exception:
            age = 0

    hits = find_strm(tmdb) if tmdb else []
    if not hits and tmdb and age < config.STRM_WAIT:
        return                                   # 再等等 strm
    if not hits and not tmdb and age < 300:
        return                                   # 没识别到 TMDBID 也稍等一下

    # ★ 分集去重：同 tmdb_id 3 小时内已发过「已入库」正式通知 → 本 hash 终结（防刷屏）
    if tmdb and db.notify_exists_recent(tmdb, "organized", minutes=180,
                                         title_prefix="已入库"):
        db.set_status(h, "archived", "strm 已生成（同片已播报，去重）")
        log.info("[去重] %s 同 tmdb=%s 已播报过，跳过", rec["torrent_name"][:36], tmdb)
        return

    # ★ 可播校验（用户拍板：Emby 真出可播集才喊能看）
    #   strm 已生成但 Emby 尚未扫出可播条目 → 本轮不通知、保持 organized，
    #   下轮（5 分钟）再查；超过 PLAY_WAIT 仍不可播 → 降级发中性「索引中」，
    #   且该 hash 不再重复校验（避免永久卡在 organized）。
    playable, eps_n = (True, 0)
    if tmdb:
        playable, eps_n = media_mod.emby_playable(tmdb, rec.get("media_type") or "")
        if not playable and age < config.PLAY_WAIT:
            return                               # Emby 还没扫出来，继续等（不通知）
    play_pending = tmdb and not playable         # 超时仍不可播 → 中性文案
    # 同片刚发过中性通知（30 分钟内）→ 不重复，等真正可播或被去重终结
    if play_pending and tmdb and db.notify_exists_recent(
            tmdb, "organized", minutes=30, title_prefix="入库索引中"):
        return

    media = {
        "tmdb_id": tmdb,
        "poster": rec.get("poster") or "",
        "media_title": rec.get("media_title") or "",
        "year": rec.get("year") or "",
        "media_type": rec.get("media_type") or "",
    }
    _fill_poster(media, rec)
    disp = media["media_title"] or rec["torrent_name"]
    yr = f"（{media['year']}）" if media["year"] else ""
    size_gb = (int(rec.get("total_size") or 0)) / 1024 ** 3
    body = []
    if rec.get("quality"):
        body.append(f"🎞 画质｜{rec['quality']}")
    body.append(f"💾 体积｜{size_gb:.2f} GB")
    if hits:
        body.append(f"📁 位置｜{hits[0]}")
    if eps_n:
        body.append(f"🎬 Emby 已就绪 {eps_n} 集")
    body.append(ui_divider())
    if play_pending:
        body.append("⏳ **已入库存档，Emby 正在建立索引，稍后即可观看**")
        _title = f"入库索引中：{disp}{yr}"
    else:
        body.append("✅ **已入库并可播放，Emby 里直接搜片名即可观看**")
        _title = f"已入库：{disp}{yr}"

    extra = _req_extra(rec, disp)      # ★ 求片人（bot 端据此 @）

    db.add_notify("organized", _title, "\n".join(body),
                  rec.get("site") or "", rec.get("category") or "",
                  rec.get("origin") or "", "", media, size_gb,
                  rec.get("quality") or "", rec.get("torrent_name") or "",
                  bitrate=bitrate_str(rec, int(rec.get("total_size") or 0),
                                      int(rec.get("file_count") or 0)),
                  extra=extra)
    db.set_status(h, "archived", "入库通知已发（可播校验通过）" if not play_pending
                  else "入库通知已发（超时未就绪，中性文案，不再重发）")
    log.info("[OK] 已入库 %s（%s）playable=%s eps=%s req=%s",
             disp[:40], hits[0] if hits else "无 strm 匹配", playable, eps_n,
             bool(extra))


# ---------------------------------------------------------------- 智能删种

def disk_guard(torrents):
    free = disk_free_gb()
    if free >= config.MIN_FREE_GB:
        return
    tier = None
    for th, ratio, days in reversed(config.DISK_TIERS):
        if free < th:
            tier = (th, ratio, days)
            break
    if not tier:
        return
    th, need_ratio, need_days = tier
    log.warning("磁盘紧张：可用 %.1f GB < %.0f GB，按 分享率>=%.1f 且做种>=%d天 清理",
                free, th, need_ratio, need_days)

    uploaded = {r["torrent_hash"]: r
                for r in db.get_by_status("uploaded", "organized", "archived")}
    cands = []
    for t in torrents:
        h = t["hash"]
        if h not in uploaded:
            continue
        # ★ 保号做种免疫：带「保号」标签（或 keepalive 分类）的种子**永不删**。
        #   站方规则：删掉/改动正在做种的文件 = 该种子判定未完成，掉上传与魔力，
        #   直接毁掉保号。这类种子的清理只由保号模块自己决定，磁盘回收必须跳过。
        _tags = {x.strip() for x in str(t.get("tags") or "").split(",") if x.strip()}
        if ("保号" in _tags) or (t.get("category") == "keepalive"):
            continue
        try:
            ratio = float(t.get("ratio") or 0)
        except Exception:
            ratio = 0.0
        seed_days = int(t.get("seeding_time") or 0) / 86400
        if ratio >= need_ratio and seed_days >= need_days:
            cands.append((ratio, seed_days, h, t.get("name") or "",
                          int(t.get("size") or 0)))
    cands.sort(reverse=True)  # 分享率高的先删（已充分回报站点）

    freed = 0.0
    for ratio, sd, h, name, size in cands:
        if disk_free_gb() >= config.MIN_FREE_GB * 1.5:
            break
        log.info("清理种子 %s（分享率 %.2f，做种 %.0f 天，%.2f GB）",
                 name[:40], ratio, sd, size / 1024 ** 3)
        db.disk_log(disk_free_gb(), disk_used_gb(), "prune",
                    f"{name} ratio={ratio:.2f} seed={sd:.0f}d size={size/1024**3:.2f}GB")
        qb.delete([h], with_files=True)
        db.set_status(h, "pruned", f"空间回收 ratio={ratio:.2f}")
        freed += size / 1024 ** 3
    if freed:
        log.info("本轮共释放 %.2f GB", freed)


# ---------------------------------------------------------------- 每日简报

def daily_report(torrents):
    global _last_daily
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_daily == today or datetime.now().hour != 21:
        return
    _last_daily = today
    n = len(torrents)
    up = sum(int(t.get("uploaded") or 0) for t in torrents) / 1024 ** 3
    ratios = []
    for t in torrents:
        try:
            ratios.append(float(t.get("ratio") or 0))
        except Exception:
            pass
    avg = sum(ratios) / len(ratios) if ratios else 0
    free = disk_free_gb()
    done = len(db.get_by_status("organized", "archived"))
    body = (f"做种中 {n} 个｜累计上传 {up:.1f} GB\n"
            f"平均分享率 {avg:.2f}｜已入库 {done} 个\n"
            f"下载盘剩余 {free:.0f} GB")
    db.add_notify("daily", "PT 保号日报", body)
    log.info("已生成保号日报：%s", body.replace("\n", " | "))


# ---------------------------------------------------------------- 主循环

def tick():
    global _round
    _round += 1
    if not qb.login():
        log.warning("QB 登录失败")
        return
    torrents = qb.torrents()
    if torrents is None:
        torrents = []
    wish = db.wish_keywords()

    by_hash = {t["hash"]: t for t in torrents}
    skip = set(config.SKIP_TAGS)

    _site_cache = {}
    for t in torrents:
        h = t["hash"]
        tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
        if skip & set(tags):
            continue
        # ★ 2026-10-20：分类级跳过（保号做种种永不上 115）
        if config.SKIP_CATEGORIES and (t.get("category") or "") in config.SKIP_CATEGORIES:
            continue
        # ★ 2026-10-23：保号「标签」（不止 keepalive 分类）也永不上 115
        if "保号" in tags:
            continue
        # ★ 2026-10-20：站点级跳过（老师站/NicePT 资源永不上 115）
        if config.SKIP_SITES:
            _s = _site_cache.get(h)
            if _s is None:
                try:
                    _s = detect_site(t) or ""
                except Exception:
                    _s = ""
                _site_cache[h] = _s
            if _s and any(k and k in _s for k in config.SKIP_SITES):
                log.debug("站点黑名单跳过：%s (%s)", (t.get("name") or "")[:40], _s)
                continue
        try:
            prog = float(t.get("progress") or 0)
        except Exception:
            prog = 0

        rec = db.get_upload(h)

        # ── 下载中：登记 + 实时刷新进度 ──
        if prog < 1.0:
            if rec is None:
                if config.DONE_TAG in tags:
                    continue  # 历史种子，未登记且已标记过，忽略
                # 无种源的 stalled 种子不播报，只登记（/dl 面板可见）
                ensure_downloading(t, wish, announce=_is_active(t))
            elif rec["status"] == "downloading":
                refresh_progress(t, rec)
                # 之前因为没种源没播报，现在真开始下了 → 补发通知
                if not rec.get("notify_id") and _is_active(t):
                    rec2 = db.get_upload(h)
                    if rec2:
                        announce_downloading(t, rec2)
            continue

        # ── 已下完 ──
        if rec is None:
            if config.DONE_TAG in tags:
                continue
            rec = ensure_record(t, wish)
            if rec is None:
                continue
        elif rec["status"] == "downloading":
            # 首次观察到下载完成：补齐真实文件清单并转入待上传
            rec = ensure_record(t, wish) or rec

        st_ = rec["status"]
        if st_ == "pending":
            do_upload(rec, t)
        elif st_ == "verifying":
            do_verify(rec, t)
        elif st_ == "uploaded":
            if _round % 5 == 0:
                check_organized(rec)
        elif st_ == "organized":
            if _round % 5 == 0:
                check_archived(rec)

    # 已登记但 QB 里已不存在的种子无需处理
    daily_report(torrents)
    disk_guard(torrents)
    _ = by_hash


def main():
    log.info("pt2cloud 启动 | QB=%s MT=%s DB=%s:%s/%s",
             config.QB_URL, config.MT_URL, config.DB_HOST, config.DB_PORT, config.DB_NAME)
    log.info("本地根=%s 云端根=%s 落点=%s 轮询=%ss",
             config.LOCAL_ROOT, config.CLOUD_ROOT, config.DEST_BASE, config.POLL_INTERVAL)
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("主循环异常：%s", e)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
