"""MoviePilot 客户端：只用两个接口

  POST /api/v1/login/access-token  登录拿 JWT
  GET  /api/v1/media/recognize?title=种子名   反查中文名 / 年份 / TMDBID / 海报

海报交给 bot 侧下载（kfcgame 已有缓存），这里只负责把 URL 存进通知表。
"""
import time
import urllib.parse

import requests

import config

# Emby（码率估算用）：优先读 config.EMBY_*，没配就用内置默认值
_EMBY_URL = (getattr(config, "EMBY_URL", "") or "http://host.docker.internal:8096").rstrip("/")
_EMBY_KEY = getattr(config, "EMBY_API_KEY", "")

_rt_cache = {}
_play_cache = {}   # 可播校验结果缓存（60s）：{key: (ts, playable, eps_n)}

_tok = {"v": None, "ts": 0.0}


def _base():
    return config.MP_URL.rstrip("/")


def login(force: bool = False):
    if not config.MP_ENABLE:
        return None
    if not force and _tok["v"] and time.time() - _tok["ts"] < 1500:
        return _tok["v"]
    try:
        r = requests.post(
            f"{_base()}/api/v1/login/access-token",
            data={"username": config.MP_USER, "password": config.MP_PASS},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=12,
        )
        t = r.json().get("access_token")
        if t:
            _tok.update(v=t, ts=time.time())
            return t
    except Exception as e:
        print(f"[media] MP 登录失败: {e}")
    return None


def _get(path, **kw):
    tok = login()
    if not tok:
        return None
    kw.setdefault("timeout", 20)
    h = {"Authorization": f"Bearer {tok}"}
    try:
        r = requests.get(f"{_base()}{path}", headers=h, **kw)
        if r.status_code == 401:
            tok = login(force=True)
            if not tok:
                return None
            r = requests.get(f"{_base()}{path}", headers={"Authorization": f"Bearer {tok}"}, **kw)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception as e:
        print(f"[media] MP {path} 异常: {e}")
        return None


# ── MP 站点表（domain -> MP 里的中文站点名）────────────────────────────
# 115 上的站点目录名以 MP 为准：用户在 MP 里叫什么，115 目录就叫什么
# （织梦 / 馒头 / 老师 / 大青虫 / 高清视界 / PTS / 咖啡站）。
# 拉不到 MP 时退回 config.SITE_MAP，不至于出现 zmpt / tracker / www 这种域名碎片。
_site_cache = {"map": {}, "ts": 0.0}
_SITE_TTL = 1800


def mp_sites(force: bool = False) -> dict:
    """返回 {域名: MP站点中文名}"""
    if not force and _site_cache["map"] and time.time() - _site_cache["ts"] < _SITE_TTL:
        return _site_cache["map"]
    j = _get("/api/v1/site/")
    rows = ((j or {}).get("data") or []) if isinstance(j, dict) else []
    m = {}
    for s in rows:
        if not isinstance(s, dict):
            continue
        nm = (s.get("name") or "").strip()
        if not nm:
            continue
        for k in (s.get("domain"), s.get("url")):
            k = (k or "").strip()
            if not k:
                continue
            host = urllib.parse.urlparse(
                k if "//" in k else "https://" + k).hostname or ""
            host = host.lower()
            if host:
                m[host] = nm
                # 同时记住主域名，tracker 常带二级前缀（kp.m-team.cc / www.ptskit.org）
                parts = host.split(".")
                if len(parts) >= 2:
                    m[".".join(parts[-2:])] = nm
    if m:
        _site_cache.update(map=m, ts=time.time())
    return _site_cache["map"] or m


def site_name_of(host: str) -> str:
    """tracker 域名 -> 站点中文名。

    顺序：**人工覆盖表 → MP 站点表 → 空（调用方退回域名主段）**。
    覆盖表优先，是为了和 kfcgame 的 sortpath.SITE_OVERRIDE 完全对齐 ——
    MP 里这个站叫「咖啡」，但两边目录统一用「咖啡站」更直观；
    如果两边一个用 MP 名、一个用覆盖名，同一个站会在 115 和本地出现两个目录。
    """
    h = (host or "").lower().strip()
    if not h:
        return ""
    for k, v in config.SITE_MAP.items():
        if k in h:
            return v
    m = mp_sites()
    if h in m:
        return m[h]
    # 逐级去掉最左段再试：kp.m-team.cc -> m-team.cc
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        sub = ".".join(parts[i:])
        if sub in m:
            return m[sub]
    return ""


def poster(url: str, size: str = "w500") -> str:
    """TMDB 原图太大，缩到 w500"""
    if not url or not str(url).startswith("http"):
        return ""
    import re as _re
    return _re.sub(r"/t/p/[^/]+/", f"/t/p/{size}/", str(url))


def emby_playable(tmdb_id, mtype):
    """★ 2026-10-14（用户拍板）：入库通知前的「可播校验」。

    替罪事故：strm 刚生成就通知「可以看了」，但 Emby 还没把 strm 扫成
    Episode 条目、MediaSource 没体积 —— 用户点开一直转圈。
    规则：**Emby 里该剧/该片真的有≥1 个可播媒体源才算入库完成**：
      · 电影   ：Series 维度查不到，查 Movie 条目 MediaSources，
                 有 Size>0 或 IsRemote=True 即可播；
      · 电视剧 ：Shows/{id}/Episodes 里至少 1 集有 MediaSource（Size>100MB
                 或 IsRemote）。
    返回 (playable: bool, eps_n: int)。Emby 完全查不到 → (False, 0)，
    调用方负责超时兜底（不能无限等）。结果缓存 60s，防每轮 tick 打爆 Emby。
    """
    if not tmdb_id:
        return False, 0
    ck = f"{mtype}:{tmdb_id}"
    hit = _play_cache.get(ck)
    if hit and time.time() - hit[0] < 60:
        return hit[1], hit[2]
    playable, eps_n = False, 0
    try:
        r = requests.get(
            f"{_EMBY_URL}/emby/Items",
            params={"Recursive": "true",
                    "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                    "IncludeItemTypes": "Movie,Series",
                    "Fields": "ProviderIds,Type", "Limit": "5",
                    "api_key": _EMBY_KEY}, timeout=10)
        items = (r.json() or {}).get("Items") or []
        if items:
            it = items[0]
            if it.get("Type") == "Movie":
                v = requests.get(f"{_EMBY_URL}/emby/Items/{it['Id']}",
                                 params={"Fields": "MediaSources",
                                         "api_key": _EMBY_KEY}, timeout=10)
                for m2 in (v.json() or {}).get("MediaSources") or []:
                    if int(m2.get("Size") or 0) > 1024**2 or m2.get("IsRemote"):
                        playable, eps_n = True, 1
                        break
            else:
                v = requests.get(
                    f"{_EMBY_URL}/emby/Shows/{it['Id']}/Episodes",
                    params={"Fields": "MediaSources", "Limit": "500",
                            "api_key": _EMBY_KEY}, timeout=12)
                for e in (v.json() or {}).get("Items") or []:
                    ok_ep = False
                    for m2 in e.get("MediaSources") or []:
                        if int(m2.get("Size") or 0) > 100 * 1024**2 or m2.get("IsRemote"):
                            ok_ep = True
                            break
                    if ok_ep:
                        eps_n += 1
                playable = eps_n >= 1
    except Exception as ex:
        print(f"[media] 可播校验失败: {ex}")
    _play_cache[ck] = (time.time(), playable, eps_n)
    return playable, eps_n


def runtime_min(tmdb_id, mtype):
    """Emby 反查片长（分钟）：电影取正片时长，剧集取第一集时长。失败返回 0。

    用于估算平均码率：bitrate = 体积*8 / (片长秒) 。Emby 按 tmdbid 反查
    （anyProviderIdEquals=tmdb.x），api key 走 config，缺省用内置值。
    """
    if not tmdb_id:
        return 0
    key = f"{mtype}:{tmdb_id}"
    if key in _rt_cache:
        return _rt_cache[key]
    try:
        r = requests.get(
            f"{_EMBY_URL}/emby/Items",
            params={"Recursive": "true",
                    "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                    "Fields": "ProviderIds,RunTimeTicks", "Limit": "5",
                    "api_key": _EMBY_KEY},
            timeout=8)
        items = (r.json() or {}).get("Items") or []
        if not items:
            _rt_cache[key] = 0
            return 0
        it = items[0]
        ticks = int(it.get("RunTimeTicks") or 0)
        if mtype == "电视剧" or it.get("Type") == "Series":
            # 剧集：Series 没有 RunTimeTicks，取第一集
            try:
                r2 = requests.get(
                    f"{_EMBY_URL}/emby/Shows/{it['Id']}/Episodes",
                    params={"Fields": "RuntimeTicks", "Limit": "1",
                            "api_key": _EMBY_KEY},
                    timeout=8)
                eps = (r2.json() or {}).get("Items") or []
                if eps:
                    ticks = int(eps[0].get("RunTimeTicks") or 0)
            except Exception:
                pass
        mins = ticks / 600000000.0 if ticks else 0    # 100ns ticks -> 分钟
        _rt_cache[key] = mins
        return mins
    except Exception as e:
        print(f"[media] Emby runtime 反查失败: {e}")
        return 0


_dv_cache = {}


def dv_meta(tmdb_id, mtype):
    """Emby 反查画质元数据：{range, dv_profile, bit_depth}（查不到返回 {}）。

    种子名经常不带 DV Profile（P5/P7/P8）和色深，Emby 的 MediaStreams 里有
    VideoRangeType / DvProfile / BitDepth —— 用它补齐，参考 MT 探测的思路。
    """
    if not tmdb_id:
        return {}
    key = f"dv:{mtype}:{tmdb_id}"
    if key in _dv_cache:
        return _dv_cache[key]
    out = {}
    try:
        r = requests.get(
            f"{_EMBY_URL}/emby/Items",
            params={"Recursive": "true",
                    "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                    "Fields": "MediaStreams,RunTimeTicks", "Limit": "5",
                    "api_key": _EMBY_KEY},
            timeout=8)
        items = (r.json() or {}).get("Items") or []
        if items:
            it = items[0]
            streams = []
            if mtype == "电视剧" or it.get("Type") == "Series":
                try:
                    r2 = requests.get(
                        f"{_EMBY_URL}/emby/Shows/{it['Id']}/Episodes",
                        params={"Fields": "MediaStreams", "Limit": "1",
                                "api_key": _EMBY_KEY},
                        timeout=8)
                    eps = (r2.json() or {}).get("Items") or []
                    if eps:
                        streams = eps[0].get("MediaStreams") or []
                except Exception:
                    pass
            else:
                streams = it.get("MediaStreams") or []
            v = next((s for s in streams if s.get("Type") == "Video"), None)
            if v:
                vrt = str(v.get("VideoRangeType") or v.get("VideoRange") or "").upper()
                if "DOVI" in vrt or "DOLBY" in vrt:
                    out["range"] = "Dolby Vision"
                elif "HDR10PLUS" in vrt or "HDR10+" in vrt:
                    out["range"] = "HDR10+"
                elif "HDR10" in vrt:
                    out["range"] = "HDR10"
                elif "HLG" in vrt:
                    out["range"] = "HLG"
                elif "HDR" in vrt:
                    out["range"] = "HDR"
                elif "SDR" in vrt:
                    out["range"] = "SDR"
                p = v.get("DvProfile")
                if p:
                    try:
                        out["dv_profile"] = f"P{int(p)}"
                    except Exception:
                        pass
                bd = v.get("BitDepth")
                if bd:
                    try:
                        out["bit_depth"] = f"{int(bd)}bit"
                    except Exception:
                        pass
                fr = v.get("RealFrameRate") or v.get("FrameRate")
                if fr:
                    try:
                        fv = float(fr)
                        if fv > 240:      # 某些流把 23.976 存成 23976（tick 值）
                            fv /= 1000.0
                        if 1 < fv <= 240:
                            out["fps"] = f"{fv:g}fps"
                    except Exception:
                        pass
    except Exception as e:
        print(f"[media] Emby dv_meta 反查失败: {e}")
    _dv_cache[key] = out
    return out


def enrich_quality(info, tmdb_id, mtype):
    """种子名解析不到的画质参数（动态范围/DV Profile/色深）用 Emby 反查补齐。

    info 是 quality.parse() 的返回 dict，原地补齐后返回。
    """
    try:
        dr = info.get("动态范围") or ""
        need = (not dr) or (not info.get("色深")) or (not info.get("帧率")) or \
               ("Dolby Vision" in dr and not info.get("DV_profile"))
        if not need:
            return info
        m = dv_meta(tmdb_id, mtype)
        if not m:
            return info
        if not dr and m.get("range"):
            info["动态范围"] = m["range"]
            dr = info["动态范围"]
        if not info.get("DV_profile") and m.get("dv_profile"):
            info["DV_profile"] = m["dv_profile"]
            if "Dolby Vision" in dr and "P" not in dr.split("+")[0]:
                info["动态范围"] = dr.replace(
                    "Dolby Vision", f"Dolby Vision {m['dv_profile']}", 1)
        if not info.get("色深") and m.get("bit_depth"):
            info["色深"] = m["bit_depth"]
        if not info.get("帧率") and m.get("fps"):
            info["帧率"] = m["fps"]
    except Exception as e:
        print(f"[media] 画质补齐失败: {e}")
    return info


def emby_poster(tmdb_id, mtype):
    """Emby 海报 URL 兜底：MP 没识别出海报时，从 Emby 条目拿 Primary 图。

    返回可直接 GET 的 URL（带 api_key），查不到返回空串。
    """
    if not tmdb_id:
        return ""
    try:
        r = requests.get(
            f"{_EMBY_URL}/emby/Items",
            params={"Recursive": "true",
                    "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                    "Limit": "1", "api_key": _EMBY_KEY},
            timeout=8)
        items = (r.json() or {}).get("Items") or []
        if items:
            return (f"{_EMBY_URL}/emby/Items/{items[0]['Id']}/Images/Primary"
                    f"?maxWidth=500&quality=85&api_key={_EMBY_KEY}")
    except Exception as e:
        print(f"[media] Emby 海报兜底失败: {e}")
    return ""


def recognize(name: str) -> dict:
    """种子名 -> {title, year, tmdb_id, type, poster}；识别失败返回空 dict"""
    if not name:
        return {}
    # 注意：requests 会自己做 URL 编码，这里不要再 quote 一次，否则种子名里的
    # 空格/点会被二次转义，MP 识别不到任何东西
    j = _get("/api/v1/media/recognize", params={"title": name[:160]})
    if not isinstance(j, dict):
        return {}
    data = j.get("data") or {}
    mi = data.get("media_info") or {}
    if not mi:
        return {}
    return {
        "title": (mi.get("title") or "").strip(),
        "year": str(mi.get("year") or ""),
        "tmdb_id": str(mi.get("tmdb_id") or ""),
        "type": mi.get("type") or "",
        "poster": poster(mi.get("poster_path")),
    }
