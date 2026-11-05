"""MySQL 访问层：pt2cloud 记录上传状态，并写入 kfcgame 通知队列"""
import logging

import pymysql
from pymysql.cursors import DictCursor

import config

log = logging.getLogger("pt2cloud")


def conn():
    return pymysql.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, charset="utf8mb4",
        autocommit=True, connect_timeout=10, cursorclass=DictCursor,
    )


def _exec(sql, args=None, fetch=False, many=False):
    c = conn()
    try:
        with c.cursor() as cur:
            if many:
                cur.executemany(sql, args or [])
            else:
                cur.execute(sql, args or [])
            if fetch:
                return cur.fetchall()
            return True
    except Exception as e:
        log.warning("DB 失败 %s | %s", sql[:60], e)
        return None
    finally:
        try:
            c.close()
        except Exception:
            pass


def upsert_upload(h, name, category, site, origin, local_path, cloud_dir,
                  total_size, file_count, status, stage="", media=None,
                  quality=""):
    """media: {tmdb_id, poster, media_title, year, media_type}"""
    m = media or {}
    _exec("""
        INSERT INTO pt_uploads
          (torrent_hash, torrent_name, category, site, origin,
           tmdb_id, poster, media_title, year, media_type, quality,
           local_path, cloud_dir, total_size, file_count, status, stage)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          torrent_name=VALUES(torrent_name), category=VALUES(category),
          site=VALUES(site), origin=VALUES(origin),
          tmdb_id=VALUES(tmdb_id), poster=VALUES(poster),
          media_title=VALUES(media_title), year=VALUES(year),
          media_type=VALUES(media_type), quality=VALUES(quality),
          local_path=VALUES(local_path), cloud_dir=VALUES(cloud_dir),
          total_size=VALUES(total_size), file_count=VALUES(file_count),
          status=VALUES(status), stage=VALUES(stage)
    """, (h, name[:500], category, site, origin,
          (m.get("tmdb_id") or "")[:32], (m.get("poster") or "")[:512],
          (m.get("media_title") or "")[:255], (m.get("year") or "")[:16],
          (m.get("media_type") or "")[:16], (quality or "")[:255],
          local_path[:1000], cloud_dir[:1000], total_size, file_count,
          status, stage))


def set_status(h, status, stage="", error=""):
    _exec("UPDATE pt_uploads SET status=%s, stage=%s, error=%s WHERE torrent_hash=%s",
          (status, stage, (error or "")[:1000], h))


def update_progress(h, progress, speed_bps=0, eta_sec=0, dl_size=0,
                    up_size=0, qb_state=""):
    """刷新下载实时进度（供 bot 画进度条）"""
    _exec("""UPDATE pt_uploads
             SET progress=%s, speed_bps=%s, eta_sec=%s, dl_size=%s,
                 up_size=%s, qb_state=%s
             WHERE torrent_hash=%s""",
          (float(progress or 0), int(speed_bps or 0), int(eta_sec or 0),
           int(dl_size or 0), int(up_size or 0), (qb_state or "")[:64], h))


def set_notify_id(h, nid):
    """把上传记录与群内那条「进度消息」绑定，后续原地编辑刷新"""
    _exec("UPDATE pt_uploads SET notify_id=%s WHERE torrent_hash=%s", (nid, h))


def mark_start(h):
    _exec("UPDATE pt_uploads SET start_at=NOW() WHERE torrent_hash=%s "
          "AND start_at IS NULL", (h,))


def mark_done_time(h):
    _exec("UPDATE pt_uploads SET done_at=NOW() WHERE torrent_hash=%s "
          "AND done_at IS NULL", (h,))


def get_by_status(*statuses):
    if not statuses:
        return []
    ph = ",".join(["%s"] * len(statuses))
    return _exec(f"SELECT * FROM pt_uploads WHERE status IN ({ph}) ORDER BY id",
                 list(statuses), fetch=True) or []


def get_upload(h):
    r = _exec("SELECT * FROM pt_uploads WHERE torrent_hash=%s", (h,), fetch=True)
    return r[0] if r else None


def get_status(h):
    r = _exec("SELECT status FROM pt_uploads WHERE torrent_hash=%s", (h,), fetch=True)
    return r[0]["status"] if r else None


def wish_keywords(limit=500):
    """返回 bot 求片关键词列表（用于判定来源=求片）"""
    rows = _exec("SELECT keyword, title FROM kfcgame.kfc_wishlist "
                 "ORDER BY id DESC LIMIT %s", (limit,), fetch=True) or []
    out = []
    for r in rows:
        for v in (r.get("keyword"), r.get("title")):
            if v and len(v) >= 2:
                out.append(str(v).strip().lower())
    return out


def wish_owner(names) -> dict:
    """★ 2026-10-14：按片名/种子名反查求片人（与 bot db.wish_owner 同口径）。

    返回 {"tg_id": str, "tg_name": str, "tg_username": str}；查不到返回 {}。
    用于入库/下载完成通知里 @求片人。
    """
    import re as _re
    def norm(s):
        return _re.sub(r"[^0-9a-z一-鿿]+", "", str(s or "").lower())
    cands = [norm(x) for x in (names or []) if norm(x)]
    if not cands:
        return {}
    rows = _exec("SELECT tg_id, tg_name, keyword, title "
                 "FROM kfcgame.kfc_wishlist ORDER BY id DESC LIMIT 300",
                 fetch=True) or []
    best, blen = None, 0
    for r in rows:
        for col in ("keyword", "title"):
            k = norm(r.get(col))
            if len(k) < 2:
                continue
            for n in cands:
                if k in n:
                    score = len(k)
                elif len(n) >= 4 and n in k:
                    score = len(n)
                else:
                    continue
                if score > blen:
                    best, blen = r, score
    if not best:
        return {}
    out = {"tg_id": str(best.get("tg_id") or ""),
           "tg_name": str(best.get("tg_name") or ""), "tg_username": ""}
    # kfc_wishlist 没存 tg_username → 用 tg 反查 kfc_request（bot 登记时全量存）
    try:
        rr = _exec("SELECT tg_username FROM kfcgame.kfc_request "
                   "WHERE tg=%s AND tg_username<>'' ORDER BY id DESC LIMIT 1",
                   (out["tg_id"],), fetch=True)
        if rr:
            out["tg_username"] = str(rr[0].get("tg_username") or "")
    except Exception:
        pass
    return out


def notify_exists_recent(tmdb_id, kind, minutes=180, title_prefix=""):
    """同类 + 同 tmdb 的通知在 N 分钟内已发过 → True（分集种子去重用）。
    title_prefix 非空时再要求 title 以它开头（区分「已入库」正式通知 vs
    「入库索引中」中性通知，后者不能顶掉前者的补发机会）。"""
    if not tmdb_id:
        return False
    sql = ("SELECT id FROM kfcgame.kfc_pt_notify "
           "WHERE tmdb_id=%s AND kind=%s "
           "  AND created_at > (NOW() - INTERVAL %s MINUTE)")
    args = [str(tmdb_id), kind, int(minutes)]
    if title_prefix:
        sql += " AND title LIKE %s"
        args.append(title_prefix + "%")
    sql += " LIMIT 1"
    r = _exec(sql, args, fetch=True)
    return bool(r)


def add_notify(kind, title, body="", site="", category="", origin="",
               chat_id="", media=None, size_gb=0.0, quality="",
               torrent_name="", stage="", total_size=0, progress=0.0,
               speed_bps=0, eta_sec=0, done_size=0, bitrate="", extra=None):
    """写一条待播报通知；media 为 MP 反查出的媒体信息 dict。返回新记录 id。
    extra：新增列的 dict（如求片人 tg_id/tg_name/tg_username），自愈加列。"""
    # ★ 用户规则（2026-10-10）：只有「求片」（群友点的片）才发群通知；
    #   自己在 MP 订阅/刷流下载的一律静默，别刷群。
    if kind in ("downloading", "downloaded", "organized", "failed") \
            and (origin or "") != "求片":
        return 0
    m = media or {}
    _ensure_notify_cols()
    ex = extra or {}
    cols = ["kind", "title", "body", "site", "category", "origin",
            "tmdb_id", "poster", "media_title", "year", "media_type",
            "size_gb", "quality", "torrent_name", "chat_id", "status",
            "stage", "total_size", "progress", "speed_bps", "eta_sec", "done_size",
            "bitrate"]
    args = [kind, title[:500], body or "", site, category, origin,
            (m.get("tmdb_id") or "")[:32], (m.get("poster") or "")[:512],
            (m.get("media_title") or "")[:255], (m.get("year") or "")[:16],
            (m.get("media_type") or "")[:16],
            float(size_gb or 0), (quality or "")[:255],
            (torrent_name or "")[:512], chat_id, 'pending',
            (stage or "")[:64], int(total_size or 0), float(progress or 0),
            int(speed_bps or 0), int(eta_sec or 0), int(done_size or 0),
            (bitrate or "")[:32]]
    for k, v in sorted(ex.items()):
        cols.append(k)
        args.append(str(v or "")[:255])
    sql = ("INSERT INTO kfcgame.kfc_pt_notify (%s) VALUES (%s)"
           % (",".join(cols), ",".join(["%s"] * len(args))))
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid
    except Exception as e:
        log.warning("写通知失败 %s", e)
        return 0
    finally:
        try:
            c.close()
        except Exception:
            pass


_NOTIFY_COLS_DONE = {"v": False}
_NOTIFY_NEW_COLS = (("req_tg", "BIGINT NULL"),
                    ("req_name", "VARCHAR(128) DEFAULT ''"),
                    ("req_username", "VARCHAR(64) DEFAULT ''"))


def _ensure_notify_cols():
    """自愈：kfc_pt_notify 缺求片人三列时 ALTER 补上（幂等、失败静默）。"""
    if _NOTIFY_COLS_DONE["v"]:
        return
    try:
        have = {r["Field"] for r in
                (_exec("SHOW COLUMNS FROM kfcgame.kfc_pt_notify", fetch=True) or [])}
        for col, ddl in _NOTIFY_NEW_COLS:
            if col not in have:
                _exec(f"ALTER TABLE kfcgame.kfc_pt_notify ADD COLUMN {col} {ddl}")
                log.info("kfc_pt_notify 自愈加列 %s", col)
        _NOTIFY_COLS_DONE["v"] = True
    except Exception as e:
        log.warning("通知表加列检查失败: %s", e)


def mark_edit(nid, stage=None, progress=None, speed_bps=None, eta_sec=None,
              done_size=None, total_size=None, body=None):
    """让 bot 去原地刷新这条群消息（进度条/速度/ETA）"""
    sets, vals = ["need_edit=1"], []
    for col, v in (("stage", stage), ("progress", progress),
                   ("speed_bps", speed_bps), ("eta_sec", eta_sec),
                   ("done_size", done_size), ("total_size", total_size),
                   ("body", body)):
        if v is None:
            continue
        sets.append(f"{col}=%s")
        vals.append(v if col != "stage" else (v or "")[:64])
    if len(sets) == 1:
        return
    vals.append(nid)
    _exec(f"UPDATE kfcgame.kfc_pt_notify SET {','.join(sets)} WHERE id=%s", vals)


def get_notify(nid):
    r = _exec("SELECT * FROM kfcgame.kfc_pt_notify WHERE id=%s",
              (nid,), fetch=True)
    return r[0] if r else None


def active_uploads():
    """在途任务（未终结的），供 /dl 查询"""
    return _exec("""SELECT * FROM pt_uploads
                    WHERE status NOT IN ('archived', 'pruned', 'skipped', 'failed')
                    ORDER BY id DESC""", fetch=True) or []


def disk_log(free_gb, used_gb, action, detail=""):
    _exec("INSERT INTO pt_disk_log (free_gb, used_gb, action, detail) "
          "VALUES (%s,%s,%s,%s)", (free_gb, used_gb, action[:120], (detail or "")[:2000]))
