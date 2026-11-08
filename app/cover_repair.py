"""零凭据参考实现：Emby 缺失封面扫描 + TMDB 官方海报补齐。

本模块不绑定任何服务器地址 / API Key / 内网路径：所有外部调用通过回调注入，
既能在任意部署中复用，也能用纯标准库做单元测试（tests/test_cover_repair.py）。

线上 media-hub 的等价实现是 app.py 的 ``cover_scan`` / ``cover_fix`` 系列函数，
行为契约一致：

1. **扫描**：``ImageTags.Primary`` 为空的 Movie / Series（最近入库优先或全库分页）。
2. **匹配**：优先 ``ProviderIds.Tmdb`` **同类型**精确取；**绝不用同 ID 的另一类型**
   （TMDB movie/tv 的 ID 空间独立，实测同 ID 会是两部完全不同的作品）。
3. **兜底**：查同作品多语言海报集 → 片名+年份严格搜索；TMDB 确实无图则标注原因、不写库。
4. **写入**：Emby 4.10 的 ``POST /Items/{Id}/Images/Primary`` 要求 **base64 文本** +
   ``Content-Type: application/octet-stream``，返回 204；写后回读 ``ImageTags`` 校验。
5. **试运行**（dry）：只查 TMDB、绝不写库，便于先核对匹配质量。

与「缺图重读」类工具的区别：那只重读条目目录里已存在的本地图片，
strm 库里新片目录常无图，永远补不上；本模块把海报**写进媒体库本体**，
Emby 原生 UI 与所有客户端立即生效。
"""
from __future__ import annotations

import base64
import time

TMDB_API = "https://api.themoviedb.org/3"
IMAGE_SIZES = ("w185", "w300", "w342", "w500", "w780", "w1280", "original")
MAX_BATCH = 300


def _kind(item):
    """剧集 -> tv，其余 -> movie。"""
    return "tv" if str(item.get("type") or "").lower().startswith("series") else "movie"


def encode_for_emby(image_bytes):
    """Emby 4.10+ 图片上传：body 必须是 base64 文本，不是二进制流。"""
    if not image_bytes:
        raise ValueError("empty image")
    return base64.b64encode(image_bytes).decode("ascii"), "application/octet-stream"


def scan_missing(fetch_items, server="emby", scope="recent", limit=1000,
                 lib_id="", page_size=800, label=""):
    """扫描缺 Primary 封面的条目。

    :param fetch_items: 回调 ``(start, count, scope, lib_id) -> dict``，
        返回形如 ``{"Items": [...], "TotalRecordCount": int}``；由调用方注入
        Emby 只读请求（GET /Items?Recursive=true&IncludeItemTypes=Movie,Series…）。
    :param scope: ``recent`` 按入库时间倒序；``all`` 全库顺序分页。
    :returns: ``{ok, ts, servers, items, scanned, missing}``
    """
    limit = max(50, min(int(limit or 1000), 200000))
    page_size = max(1, min(int(page_size or 800), 1000))
    out = {"ok": True, "ts": time.strftime("%F %T"), "scope": scope,
           "servers": [], "items": [], "scanned": 0, "missing": 0, "err": ""}
    start, total, err = 0, 0, ""
    try:
        while start < limit:
            n = min(page_size, limit - start)
            data = fetch_items(start, n, scope, lib_id) or {}
            batch = data.get("Items") or []
            total = int(data.get("TotalRecordCount") or 0)
            for it in batch:
                out["scanned"] += 1
                if (it.get("ImageTags") or {}).get("Primary"):
                    continue
                pid = it.get("ProviderIds") or {}
                out["items"].append({
                    "id": str(it.get("Id") or ""),
                    "name": str(it.get("Name") or ""),
                    "type": str(it.get("Type") or ""),
                    "year": str(it.get("ProductionYear") or ""),
                    "tmdb": str(pid.get("Tmdb") or pid.get("tmdb") or ""),
                    "path": str(it.get("Path") or "")[-90:],
                    "server": server,
                })
            start += len(batch)
            if not batch or start >= total or start >= limit:
                break
    except Exception as e:  # 单个失败不让整页崩
        out["ok"] = False
        out["err"] = str(e)[:160]
        err = out["err"]
    out["servers"] = [{"server": server, "label": label or server,
                       "total": total, "scanned": out["scanned"],
                       "missing": len(out["items"]), "err": err}]
    out["missing"] = len(out["items"])
    out["items"] = out["items"][:1000]
    return out


def find_poster(item, tmdb_get, loose=False, api_base=TMDB_API, api_key=""):
    """按 TMDB ID（严格同类型）→ 多语言海报集 → 片名严格搜索 找官方海报。

    :param tmdb_get: 回调 ``(url) -> dict``，由调用方注入代理/缓存/鉴权。
    :returns: ``(poster_path, match_desc, tmdb_title)``；找不到时 path 为空且
        ``match_desc`` 写明原因。
    """
    if not api_key:
        return "", "未配置 TMDB Key", ""
    tid = str(item.get("tmdb") or "").strip()
    name = str(item.get("name") or "").strip()
    year = str(item.get("year") or "").strip()
    kind = _kind(item)
    res = ("", "TMDB 无该片海报", "")

    if tid.isdigit():
        # ★ 关键红线：movie / tv 的 ID 空间独立，绝不跨类型回退。
        detail = tmdb_get("%s/%s/%s?api_key=%s&language=zh-CN"
                          % (api_base, kind, tid, api_key)) or {}
        if isinstance(detail, dict) and detail.get("poster_path"):
            res = (detail["poster_path"], "TMDB ID 精确匹配 (%s)" % kind,
                   str(detail.get("title") or detail.get("name") or ""))
        elif isinstance(detail, dict) and detail.get("__err"):
            res = ("", "TMDB 请求失败：%s" % str(detail["__err"])[:60], "")
        elif isinstance(detail, dict):
            images = tmdb_get("%s/%s/%s/images?api_key=%s"
                              "&include_image_language=zh,en,null"
                              % (api_base, kind, tid, api_key)) or {}
            for p in ((images.get("posters") or [])
                      if isinstance(images, dict) else [])[:6]:
                if p.get("file_path"):
                    res = (p["file_path"], "TMDB 多语言海报集 (%s)" % kind,
                           str(detail.get("title") or detail.get("name") or ""))
                    break
            if not res[0]:
                res = ("", "TMDB 无该片海报（不跨类型/不模糊匹配）", "")

    if not res[0] and name:
        sr = tmdb_get("%s/search/multi?api_key=%s&language=zh-CN&query=%s"
                      % (api_base, api_key, name)) or {}
        rows = (sr.get("results") or []) if isinstance(sr, dict) else []
        for r in rows[:10]:
            if not r.get("poster_path"):
                continue
            mt = str(r.get("media_type") or "")
            if mt and ((kind == "tv" and mt != "tv") or
                       (kind == "movie" and mt != "movie")):
                continue
            ry = str(r.get("first_air_date") or r.get("release_date") or "")[:4]
            rt = str(r.get("title") or r.get("name") or "")
            if not loose:
                if year and ry and year != ry:
                    continue
                if rt.lower() != name.lower():
                    continue
            label = ("宽松匹配（需人工核对）" if loose else
                     "片名%s匹配" % ("+年份" if (year and ry == year) else ""))
            res = (r["poster_path"], label, rt)
            break
        if not res[0] and loose:
            for r in rows[:10]:
                if r.get("poster_path"):
                    res = (r["poster_path"], "宽松匹配（需人工核对）",
                           str(r.get("title") or r.get("name") or ""))
                    break
    return res


def repair(items, tmdb_get, image_fetch, image_upload, *, dry=False,
           size="w500", loose=False, batch=MAX_BATCH, api_base=TMDB_API,
           api_key=""):
    """逐项补齐。全部外部动作通过回调注入，便于测试与替换实现。

    :param image_fetch: ``(poster_path, size) -> bytes | None``
    :param image_upload: ``(item_id, base64_text, content_type) -> (ok, msg)``
    :returns: ``{ok, ts, dry, total, fixed, failed, skipped, details, msg}``
    """
    picked = [x for x in (items or []) if str(x.get("id") or "").isdigit()][:batch]
    out = {"ok": True, "ts": time.strftime("%F %T"), "dry": bool(dry),
           "total": len(picked), "fixed": 0, "failed": 0,
           "skipped": 0, "details": [], "msg": ""}
    if not picked:
        out["msg"] = "没有可补齐的条目（请先扫描）"
        return out
    if size not in IMAGE_SIZES:
        size = "w500"
    for it in picked:
        iid, name = str(it.get("id")), str(it.get("name") or "")
        det = {"id": iid, "name": name, "server": it.get("server") or "",
               "ok": False, "dry": bool(dry), "msg": "", "match": ""}
        try:
            po, match, _ = find_poster(it, tmdb_get, loose=loose,
                                       api_base=api_base, api_key=api_key)
            det["match"] = match
            det["poster"] = po
            if not po:
                det["msg"] = match
                out["failed"] += 1
            elif dry:
                det["ok"] = True
                det["msg"] = "可补齐（试运行）：%s" % match
                out["skipped"] += 1
            else:
                img = image_fetch(po, size)
                if not img:
                    det["msg"] = "海报下载失败"
                    out["failed"] += 1
                else:
                    body, ctype = encode_for_emby(img)
                    ok, msg = image_upload(iid, body, ctype)
                    det["ok"] = bool(ok)
                    det["msg"] = ("已写入 Emby · %s" % match) if ok else str(msg)
                    det["bytes"] = len(img)
                    out["fixed" if ok else "failed"] += 1
        except Exception as e:
            det["msg"] = "异常：%s" % str(e)[:90]
            out["failed"] += 1
        out["details"].append(det)
    out["msg"] = ("%s成功 %d 张，失败 %d 张，试运行 %d 张（共 %d）"
                  % ("试运行：" if dry else "", out["fixed"],
                     out["failed"], out["skipped"], out["total"]))
    return out
