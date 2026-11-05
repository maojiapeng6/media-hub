"""pt2cloud 配置：全部来自环境变量（凭据一律不内置，未配置则相关功能不点亮）。"""
import os

# ---------- qBittorrent ----------
QB_URL = os.getenv("QB_URL", "http://host.docker.internal:8080")
QB_USER = os.getenv("QB_USER", "")
QB_PASS = os.getenv("QB_PASS", "")

# ---------- MediaTidy ----------
MT_URL = os.getenv("MT_URL", "http://host.docker.internal:2019")
MT_USER = os.getenv("MT_USER", "")
MT_PASS = os.getenv("MT_PASS", "")
MT_ACCOUNT_ID = int(os.getenv("MT_ACCOUNT_ID", "1"))

# ---------- MySQL ----------
DB_HOST = os.getenv("DB_HOST", "embyboss_mysql")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "pt2cloud")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "pt2cloud")

# ---------- MoviePilot（只用于反查媒体信息与海报） ----------
MP_ENABLE = os.getenv("MP_ENABLE", "1").lower() in ("1", "true", "yes")
# ★ 2026-10-13：MP 实际在 3002（3000 是别的服务，接口返回 Cannot GET /api/），
#   端口错了会导致「媒体识别」全部失败，分类只能靠猜，115 目录名也就跟着乱。
MP_URL = os.getenv("MP_URL", "http://host.docker.internal:3002")
MP_USER = os.getenv("MP_USER", "")
MP_PASS = os.getenv("MP_PASS", "")

# ---------- Emby（码率估算用；凭据一律外置，未配置则相关功能不点亮）----------
EMBY_URL = os.getenv("EMBY_URL", "http://host.docker.internal:8096")
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")

# ---------- strm 目录（判断「已入库」的关键证据） ----------
# 容器内只读挂载：/vol1/1000/docker/mediatidy/strm:/strm:ro
STRM_ROOT = os.getenv("STRM_ROOT", "/strm")
# MT 生成的目录名形如「片名 (年份) [tmdbid-12345]」，靠它精确匹配
STRM_WAIT = int(os.getenv("STRM_WAIT", "3600"))   # 整理完成后等 strm 的最长时间（秒）
# ★ 2026-10-14：入库通知的「Emby 可播校验」等待上限（秒）。strm 已生成但
#   Emby 还没扫出可播条目时，每轮继续等；超过此值仍未就绪 → 降级发
#   「入库处理中」中性通知（不误报「可看了」），下轮就绪后不再重复播报。
PLAY_WAIT = int(os.getenv("PLAY_WAIT", "2400"))

# ---------- 路径 ----------
# QB 容器内 /downloads 与 pt2cloud 容器内 /downloads 指向同一处，
# 因此 QB 返回的 content_path 可直接使用
LOCAL_ROOT = os.getenv("LOCAL_ROOT", "/downloads")
CLOUD_ROOT = os.getenv("CLOUD_ROOT", "/CloudNAS/115open")
DEST_BASE = os.getenv("DEST_BASE", "待整理/PT")          # 云端落点根
MT_CLOUD_PREFIX = os.getenv("MT_CLOUD_PREFIX", "/主号")   # MT 视角的云端根

# ---------- 调度 ----------
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))        # 主循环间隔（秒）
VERIFY_INTERVAL = int(os.getenv("VERIFY_INTERVAL", "60"))    # 云端校验间隔（秒）
VERIFY_TIMEOUT = int(os.getenv("VERIFY_TIMEOUT", "7200"))    # 单个任务校验最长等待（秒）
MAX_PARALLEL_UPLOAD = int(os.getenv("MAX_PARALLEL_UPLOAD", "1"))
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")

# ---------- 完成判定 ----------
DONE_TAG = os.getenv("DONE_TAG", "已传115")
SKIP_TAGS = [t for t in os.getenv("SKIP_TAGS", "不上传,跳过").split(",") if t]
# ★ 2026-10-20：站点级/分类级跳过（老师站黄片不进 115；保号做种种不进 115）
SKIP_SITES = [x.strip() for x in os.getenv("SKIP_SITES", "").split(",") if x.strip()]
SKIP_CATEGORIES = [x.strip() for x in os.getenv("SKIP_CATEGORIES", "").split(",") if x.strip()]

# ---------- 文件过滤 ----------
VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".mov",
    ".webm", ".rmvb", ".rm", ".mpg", ".mpeg", ".vob", ".iso", ".m4v", ".dat",
}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}
KEEP_EXT = VIDEO_EXT | SUB_EXT
# 这些名字/目录里的东西一律不上传（样片、花絮、广告）
EXCLUDE_NAME_HINTS = ("sample", "samples", "trailer", "preview", "extra",
                      "extras", "featurette", "behind.the.scenes", "广告")
EXCLUDE_SUFFIX = (".!qb", ".parts", ".torrent", ".nfo", ".db", ".ini")

# ---------- 站点识别 ----------
# tracker 域名 -> 站点目录名。
# ★ 2026-10-09 起：未命中的站点**自动用域名主段**（pt.sjtu.edu.cn → pt），
#   不再要求手动往这张表里加 —— 用户在 MP 里新增站点后，
#   115 上的目录会自动出现，不用改代码。
#   只有「机器推导不好听」的站点才需要写进下面这张表。
SITE_MAP = {
    # ★ 2026-10-13：全部补齐成 MP 里的中文站点名
    #   （用户要求「115 端的名字改成 MP 对应名字，比如织梦、馒头」）。
    #   这里只作**离线兜底**：正常情况下 main.detect_site() 会先查
    #   MP 的站点表（media.mp_sites()），以 MP 里的名字为准，
    #   用户在 MP 改名 / 加站，115 目录自动跟着变，不用改代码。
    "ptcafe.club": "咖啡站",
    "zmpt.cc": "织梦",
    "nicept.net": "老师",
    "cyanbug.net": "大青虫",
    "hdarea.club": "高清视界",
    "ptskit.org": "PTS",
    "m-team.cc": "馒头",       # 同时覆盖 kp.m-team.cc / m-team.cc
}
DEFAULT_SITE = os.getenv("DEFAULT_SITE", "咖啡站")

# 115 云端目录层级顺序（True = 分类在外层：PT/{分类}/{来源}/{站点}）
#: ★ 2026-10-14 改回「分类在最后」：云端落点 = 待整理/PT/{来源}/{站点}/{分类}/{种子名}
#:   用户拍板的最终结构：待整理 → PT →（订阅|求片）→ 站点 → 分类。
#:   本地 qB 下载盘本来就是 /downloads/qBittorrent/{来源}/{站点}/{分类}，
#:   云端必须和它对齐，否则「本地叫求片、云端叫订阅」错位。
#:   CAT_FIRST=1 是 2026-10-13 短暂试过、实测不好用的反向顺序，默认关闭。
CAT_FIRST = os.getenv("CAT_FIRST", "0").lower() in ("1", "true", "yes")

# ★ 分类集合：与 kfcgame 的 sortpath.TOP_CLASSES 保持一致，
#   两边同一个词，本地目录和 115 目录才对得上。
TOP_CLASSES = ("电影", "电视剧", "动漫", "纪录片", "综艺", "演唱会", "其他")

# ---------- 来源识别 ----------
# 命中 kfcgame.kfc_wishlist 的种子算「求片」，否则「订阅」
ORIGIN_WISH = "求片"
# ★ 2026-10-20：求片云端校验通过后删本地、不做种
REQ_DELETE_LOCAL = os.getenv("REQ_DELETE_LOCAL", "1").lower() in ("1", "true", "yes")
ORIGIN_SUB = "订阅"

# ---------- 智能删种（磁盘水位分级） ----------
# (可用空间低于 GB, 要求的分享率, 要求的最少做种天数)
DISK_TIERS = [
    (float(os.getenv("T1_FREE_GB", "500")), float(os.getenv("T1_RATIO", "2.0")), int(os.getenv("T1_DAYS", "30"))),
    (float(os.getenv("T2_FREE_GB", "200")), float(os.getenv("T2_RATIO", "1.5")), int(os.getenv("T2_DAYS", "14"))),
    (float(os.getenv("T3_FREE_GB", "80")), float(os.getenv("T3_RATIO", "1.0")), int(os.getenv("T3_DAYS", "7"))),
]
DISK_PATH = os.getenv("DISK_PATH", "/downloads")
MIN_FREE_GB = float(os.getenv("MIN_FREE_GB", "50"))   # 低于此值才考虑删种
