"""下载目录自动归类：把 QB 里的种子按「来源/站点/大类」搬到正确目录。

★ 目录结构（2026-10-09 用户拍板，勿改顺序）
─────────────────────────────────────
    /downloads/{客户端}/{求片|订阅}/{站点}/{电影|电视剧|动漫|演唱会|其他}

为什么「来源」在最外层：求片/订阅是**最稳定的维度**（一张 wishlist 表决定），
站点会随用户新增而增加，放在里面才能做到「加站点只多一层、不动其他」。

★ 为什么站点层完全由本模块负责
─────────────────────────────────────
MoviePilot 的目录配置（`DirectoryConf`）只有两个开关：
    download_type_folder    按类型加一层
    download_category_folder 按类别加一层
**没有「站点」维度** —— MP 压根不知道种子来自哪个站。
所以 MP 的下载根设在 `/downloads`（最简），所有层级都由这里排。

★ 站点来源以 MP 的 site 表为唯一权威源
─────────────────────────────────────
早先这里硬编码 `SITE_MAP = {"ptcafe": "咖啡站", ...}`，
结果用户在 MP 里新增站点后，本地目录不会自动出现，得改代码 —— 不可持续。
现在改成：**每次同步都去读 MP 的 site 表**，目录名按「域名」自动生成
（ptcafe.club → 咖啡站），并把映射结果缓存到 settings.json，
既不用改代码，也不会每次都查库。

判定种子来自哪个站：QB 的种子标签里有 tracker 的域名，据此反查站点目录。
查不到就落 `未知站点`（宁可落兜底，也不要丢文件）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
from loguru import logger

from .. import settings as S

# 容器内下载盘根（三方挂载同一路径，不可改）
ROOT = "/downloads"

#: 来源（求片/订阅）——最外层，最稳定
ORIGINS = ["求片", "订阅"]

#: 7 个大类 —— 与 pt2cloud（115 云端）用同一套词，本地/云端才对得上。
#: ★ 2026-10-13 从 5 类扩到 7 类：
#:   以前「纪录片」被塞进电影、「综艺」被塞进其他，
#:   结果纪录片进了电影库、综艺进了其他目录，MT 侧也没法按目录区分。
#:   现在单独成类，MT 的 categoryFFRules 里本来就有
#:   「电视剧/纪录片」「电视剧/综艺」两条，能精确落到对应子目录。
TOP_CLASSES = ["电影", "电视剧", "动漫", "纪录片", "综艺", "演唱会", "其他"]

_CLIENTS = ["qBittorrent", "Transmission"]

#: 站点目录名兜底（判定不出站点时用）
UNKNOWN_SITE = "未知站点"

#: 域名 -> 目录名的人工覆盖（只写「机器推导不好听」的，其余自动生成）
#: 键是域名主域，值是目录名。留空表示全自动按 MP 的站点名推导。
SITE_OVERRIDE: dict[str, str] = {
    "ptcafe.club": "咖啡站",
}

#: 站点目录名缓存 —— 避免每轮都查 MP 的库
_SITE_CACHE: dict = {"ts": 0.0, "map": {}}


# ------------------------------------------------------------------ 工具

def _norm(s: str) -> str:
    """归一化：小写 + 只保留字母数字（去掉空格/点/连字符，避免 '日韩 电影' 匹配不上）。"""
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def _host(url: str) -> str:
    """从 URL 里取主域（去掉 www. 和端口）。"""
    u = (url or "").strip()
    u = re.sub(r"^[a-z]+://", "", u, flags=re.I)
    u = u.split("/")[0].split(":")[0].lower()
    return u[4:] if u.startswith("www.") else u


def _site_dir_name(site_name: str, url: str) -> str:
    """站点目录名：优先人工覆盖，其次用 MP 里的站点名。

    MP 里站点名是「咖啡」，但目录用「咖啡站」更直观，所以对少数站点做了覆盖。
    覆盖表里没有的站点，直接用 MP 的名称 —— 新增站点零改动。
    """
    host = _host(url)
    for dom, name in SITE_OVERRIDE.items():
        if host == dom or host.endswith("." + dom):
            return name
    nm = (site_name or "").strip()
    return nm or host or UNKNOWN_SITE


def mp_sites(ttl: float = 300.0) -> dict[str, str]:
    """读 MP 的 site 表，返回 {主域: 目录名}，结果缓存 5 分钟。

    ★ MP 用 PostgreSQL，本模块跑在 kfcgame 容器里（连的是 MySQL），
      不能直接查 —— 走 MP 的 API。
      ★ 认证复用 `request.mp_login()`（账号密码换 token），**不要用 app.env 里的
        API_TOKEN**：那个可能已失效，实测直接用它调API 会 401。
      拿不到就返回空 dict，让上层回退到「按域名猜测」。
    """
    import time
    if time.time() - _SITE_CACHE["ts"] < ttl and _SITE_CACHE["map"]:
        return _SITE_CACHE["map"]

    out: dict[str, str] = {}
    try:
        from .request import _req
        j = _req("GET", "/api/v1/site/", params={"source": "site"})
        items = None
        if isinstance(j, dict):
            items = (j.get("data") if isinstance(j.get("data"), list)
                     else j.get("data", {}).get("items") if isinstance(j.get("data"), dict)
                     else None)
            if items is None:
                items = j.get("data")
        if isinstance(items, list) and items:
            for s in items:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name") or "").strip()
                url = str(s.get("url") or "")
                host = _host(url)
                if host:
                    out[host] = _site_dir_name(name, url)
    except Exception as e:
        logger.debug(f"读 MP 站点表失败（将按域名猜测）: {e}")

    # 兜底：至少保证已知的覆盖站点在列表里
    for dom, nm in SITE_OVERRIDE.items():
        out.setdefault(dom, nm)

    if out:
        _SITE_CACHE.update(map=out, ts=time.time())
    return out


def site_of(seed_name: str, tags: str = "", save_path: str = "") -> str:
    """判定种子来自哪个站点，返回目录名。

    依据优先级：
      1. QB 种子的 tags 里有 tracker 域名（`https://ptcafe.club/announce`）—— 最准
      2. 现有落点路径里反推 —— 迁移旧结构时用
      3. 兜底「未知站点」（宁可落兜底，也不丢文件）
    """
    sites = mp_sites()

    # 1) tags 里的 tracker 域名（最长匹配优先，避免 ptcafe 被别的包含）
    blob = f"{tags} {seed_name}".lower()
    for host in sorted(sites, key=len, reverse=True):
        dom = host.split(".")[0]
        if host in blob or (len(dom) >= 4 and dom in blob):
            return sites[host]

    # 2) 从路径反推。两种旧/新结构都要认：
    #    新结构 /downloads/{客户端}/{来源}/{站点}/{大类}
    #    旧结构 /downloads/{客户端}/{站点}/{来源}/{大类}
    #    ⇒ 扫描路径里第一个「既不是来源/大类/客户端，也不是裸域名」的段
    parts = [p for p in (save_path or "").split("/") if p]
    skip = (set(TOP_CLASSES) | set(ORIGINS) | set(_CLIENTS)
            | {"downloads", UNKNOWN_SITE})          # ★ UNKNOWN_SITE 是兜底值本身，
    #必须排除 —— 否则一旦落进「未知站点」，就再也不会被纠正回真实站点
    # 裸域名段（ptcafe.club）先收集起来：跳过它，但当没有别的候选时用它反查
    bare: list[str] = []
    for p in parts:
        if p in skip:
            continue
        if "." in p and not p.endswith(("/", "\\")):      # 像域名
            bare.append(p)
            continue
        return p
    #全 是裸域名段 ⇒ 拿它反查站点表
    for b in bare:
        host = _host(b)
        if host in sites:
            return sites[host]
        dom = host.split(".")[0]
        if len(dom) >= 4:
            for h, nm in sites.items():
                if h.split(".")[0] == dom:
                    return nm

    # 3) 兜底：当前只配了一个站点时，直接用它 —— 总比落「未知站点」好，
    #    等用户加了第二个站点，这里才会退化成按 tags/路径判定。
    if len(sites) == 1:
        return next(iter(sites.values()))
    return UNKNOWN_SITE


def sync_site_dirs() -> dict:
    """按当前站点列表，在本地把缺的所有目录补齐。

    ★ 加了新站点后调这个（或等定时任务自动跑）就会把目录建出来。
      返回 {"sites": [...], "created": [...]} 便于播报。
    """
    import time
    sites = mp_sites(ttl=0) or {"ptcafe.club": "咖啡站"}
    names = sorted(set(sites.values()))
    created: list[str] = []

    for client in _CLIENTS:
        for origin in ORIGINS:
            for site in names + [UNKNOWN_SITE]:
                for top in TOP_CLASSES:
                    p = Path(ROOT) / client / origin / site / top
                    try:
                        if not p.is_dir():
                            p.mkdir(parents=True, exist_ok=True)
                            created.append(str(p))
                    except Exception as e:
                        logger.warning(f"建目录失败 {p}: {e}")

    # 站点变了就立刻失效缓存，下一轮重新读
    _SITE_CACHE["ts"] = 0.0
    if created:
        logger.info(f"[站点同步] 新建 {len(created)} 个目录，站点：{names}")
    return {"sites": names, "created": created}


def classify(name: str, content_path: str = "", media_title: str = "",
             media_type: str = "") -> str:
    """判定种子该落哪个大类，返回 TOP_CLASSES 之一。

    ★ 唯一可靠的依据是 MoviePilot 的识别结果：
        - media_type   电影 / 电视剧 / 动漫 / 综艺 / 纪录片 / 演唱会
        - media_title  中文片名（含「纪录片」「演唱会」等题材词时最准）
      英文片名（Dune / Avengers）本身不含类型线索，光看种子名判不出来，
      所以优先吃 MP 结果，MP 失败时退回按关键词猜，猜不出就落「其他」——
      **宁可落其他，也不要猜错进错库**。
    """
    n = _norm(name)
    t = _norm(media_title)
    probe = n + t
    mtype = (media_type or "").strip()

    is_ep = bool(re.search(r"s\d{1,2}e\d{1,3}", n)) or \
        bool(re.search(r"\d{2}x\d{2}", n)) or \
        bool(re.search(r"第\d+集", name or ""))

    def has(*ks: str) -> bool:
        return any(k in probe for k in ks)

    # 1) MP 直接给出大类之一 —— 最可靠，直接用
    if mtype in TOP_CLASSES:
        return mtype

    # 2) MP 给了细分类型，映射到 7 大类
    #    ★ 2026-10-13：纪录片/综艺不再被吞掉，各自单独成类
    if mtype in ("综艺", "纪录片"):
        return mtype
    if mtype == "电视剧":
        return "电视剧"

    # 3) MP 没识别出来，用种子名 + 中文片名关键词兜底
    if has("演唱会", "音乐会", "concert", "巡演", "音乐现场") and not is_ep:
        return "演唱会"
    if has("动漫", "国漫", "日番", "新番", "番剧", "anime", "cartoon"):
        return "动漫"
    if has("纪录片", "documentary", "纪录电影") and not is_ep:
        return "纪录片"
    if is_ep:
        return "电视剧"
    if has("综艺", "variety", "真人秀", "脱口秀"):
        return "综艺"
    if not probe:
        return "其他"
    return "电影"


def target_dir(origin: str, site: str, top: str,
               client: str = _CLIENTS[0]) -> str:
    """拼出目标绝对路径：/{客户端}/{来源}/{站点}/{大类}"""
    top = top if top in TOP_CLASSES else "其他"
    origin = origin if origin in ORIGINS else "订阅"
    site = site or UNKNOWN_SITE
    return f"{ROOT}/{client}/{origin}/{site}/{top}"


# ------------------------------------------------------------------ 求片/订阅

_wish_cache: dict = {"ts": 0.0, "words": set()}


def wish_words(ttl: float = 60.0) -> set[str]:
    """读 kfc_wishlist 的关键词 —— 与 pt2cloud 判定「求片」的数据源和规则完全一致。

    pt2cloud 的 wish_keywords()（app/db.py）取 keyword + title 两列、
    按 id DESC 限 500 条、不过滤 status，长度 >= 2 才收。
    这里照抄这套规则，保证本地目录和 115 云端落点两边判定永远一致，
    不会出现「本地叫求片、云端叫订阅」的错位。
    读不到就退化成「全部算订阅」——宁可分错到订阅，也绝不丢文件。
    """
    import time
    if time.time() - _wish_cache["ts"] < ttl:
        return _wish_cache["words"]
    words: set[str] = set()
    try:
        from .. import db
        for w in db.wish_keywords(limit=500):
            n = _norm(w)
            if len(n) >= 2:
                words.add(n)
    except Exception as e:
        logger.warning(f"读 wishlist 失败，本轮全部按订阅处理: {e}")
    _wish_cache["words"] = words
    _wish_cache["ts"] = time.time()
    return words


def origin_of(name: str, words: set[str] | None = None) -> str:
    """判定种子来源：命中求片关键词表 =>「求片」，否则「订阅」。

    ⚠️ 长度门槛按「是否中文」区分：中文片名大量是两字词（沙丘/哪吒/长津湖），
    用 >=3 的门槛会把它们全漏掉、求片被误判成订阅，导致
    「本地目录标求片、115 云端标订阅」的错位（历史踩过）。
    """
    if not words:
        return "订阅"
    n = _norm(name)
    for w in words:
        if not w:
            continue
        cjk = any("一" <= c <= "鿿" for c in w)
        if (len(w) >= 2 if cjk else len(w) >= 3) and w in n:
            return "求片"
    return "订阅"


# ------------------------------------------------------------------ QB 动作

def _qb_url() -> str:
    cfg = S.CFG.get("qb") or {}
    return (cfg.get("url") or "http://127.0.0.1:8080").rstrip("/")


def _qb_session() -> requests.Session | None:
    """登录 QB 拿SID 会话。

    ⚠️ qBittorrent v5.x 登录成功返回 **204 No Content**（老版本才是 200 "Ok."），
    这里两种都放行；只要不是 4xx/5xx 就算登录成功，session 里已带 SID cookie。
    另外必须复用同一个 Session，否则后续 API 全 403（没带 SID）。
    """
    cfg = S.CFG.get("qb") or {}
    url = (cfg.get("url") or "http://127.0.0.1:8080").rstrip("/")
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    s = requests.Session()
    try:
        r = s.post(f"{url}/api/v2/auth/login",
                   data={"username": user, "password": pwd}, timeout=10)
        if r.status_code < 400:          # 200/204 都算成功（v5 是 204）
            return s
        logger.warning(f"QB 登录被拒: HTTP {r.status_code} {r.text.strip()[:80]}")
    except Exception as e:
        logger.warning(f"QB 登录失败: {e}")
    return None


def sync_once(dry_run: bool = False) -> dict:
    """扫一遍 QB，把落错目录的种子搬到正确的 {来源}/{站点}/{大类}/ 下面。

    同时顺手补齐站点目录（用户加了新站点时自动建）。
    返回 {"ok","moved","skip","err","details","sites"}。
    """
    s = _qb_session()
    if s is None:
        return {"ok": 0, "moved": 0, "skip": 0, "err": "qb login failed"}

    site_info = sync_site_dirs()          # 每轮顺手同步站点目录
    words = wish_words()
    try:
        r = s.get(f"{_qb_url()}/api/v2/torrents/info", timeout=20)
        torrents = r.json()
    except Exception as e:
        return {"ok": 0, "moved": 0, "skip": 0, "err": str(e)}

    moved = skipped = 0
    details = []
    for t in torrents:
        try:
            name = t.get("name") or ""
            save = (t.get("save_path") or "").rstrip("/")

            # ★ 保号种子免疫（2026-10-14）：带「保号」标签或 keepalive 分类的种子
            #   **永不搬动**。setLocation 是真实文件 move —— 做种中的种子被搬动/
            #   改名，站方按「未完成/违规」处理，掉上传掉魔力，直接毁保号。
            #   （曾经的事故：辅种种被搬进订阅后被整理流水线扫走。）
            _tags = {x.strip() for x in str(t.get("tags") or "").split(",") if x.strip()}
            if ("保号" in _tags) or (t.get("category") == "keepalive"):
                skipped += 1
                continue

            # ★ 用 MP 识别结果做分类依据（中文片名 + type）
            mi = {}
            try:
                from .request import mp_recognize
                mi = mp_recognize(name) or {}
            except Exception:
                mi = {}

            top = classify(
                name,
                t.get("content_path") or "",
                media_title=mi.get("title") or "",
                media_type=mi.get("type") or "",
            )
            origin = origin_of(name, words)
            site = site_of(name, t.get("tags") or "", save)
            want = target_dir(origin, site, top)
            if save == want.rstrip("/"):
                skipped += 1
                continue
            if not dry_run:
                rr = s.post(f"{_qb_url()}/api/v2/torrents/setLocation",
                            data={"hashes": t["hash"], "location": want},
                            timeout=30)
                if rr.status_code != 200:
                    details.append(f"setLocation {rr.status_code} {name[:30]}")
                    continue
                s.post(f"{_qb_url()}/api/v2/torrents/setCategory",
                       data={"hashes": t["hash"], "category": origin},
                       timeout=15)
            moved += 1
            details.append(f"{origin}/{site}/{top}  {name[:40]}")
        except Exception as e:
            logger.warning(f"归类失败: {e}")
            skipped += 1

    return {"ok": 1, "moved": moved, "skip": skipped, "details": details,
            "sites": site_info.get("sites") or [],
            "site_dirs": len(site_info.get("created") or [])}


# ------------------------------------------------------------------ 定时任务

async def auto_sort() -> None:
    """定时任务入口：把落错目录的种子搬到位 + 同步新站点目录。

    单独包一层 async，是为了能直接挂到 AsyncIOScheduler 上
    （同步的 sync_once 里含 requests 阻塞调用，不能占事件循环）。
    """
    import asyncio
    try:
        r = await asyncio.to_thread(sync_once)
        if r.get("site_dirs"):
            logger.info(f"[站点同步] 本轮新建 {r['site_dirs']} 个目录")
        if r.get("moved"):
            logger.info(f"[自动归类] 本轮搬动 {r['moved']} 个种子")
            for d in (r.get("details") or [])[:10]:
                logger.info(f"    {d}")
    except Exception as e:
        logger.warning(f"[自动归类] 失败: {e}")


def describe() -> str:
    """给 /syncsite 命令用的一行摘要。"""
    info = mp_sites(ttl=0)
    return "、".join(sorted(set(info.values()))) if info else UNKNOWN_SITE
