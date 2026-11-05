"""从 PT 种子名解析画质信息（供播报展示）

PT 命名五花八门，这里用「关键词优先命中」的宽松匹配，
解析不出来就返回空串，播报时自动省略，绝不因为解析失败阻断流程。
"""
import re

# 分辨率：从高到低，命中第一个
RES = [
    ("8K", r"\b(8k|4320p|4320i)\b"),
    ("4K", r"\b(4k|2160p|2160i|uhd)\b"),
    ("2K", r"\b(1440p|2k)\b"),
    ("1080P", r"\b(1080p|1080i|full\s?hd|fhd)\b"),
    ("720P", r"\b(720p|hd)\b"),
    ("480P", r"\b(480p|sd)\b"),
]

# 片源（全英文，PT 通行命名）：REMUX 无损原盘抓轨 / Full Disc 完整原盘镜像
SOURCE = [
    ("Blu-ray REMUX", r"\b(bdremux|remux)\b"),
    ("Full Disc", r"\b(bdmv|complete\s?blu-?ray|untouched|\biso\b|bd50|bd66|bd100)\b"),
    ("Blu-ray", r"\b(bluray|blu-ray|bdrip|brrip)\b"),
    ("WEB-DL", r"\b(web-?dl|webdl)\b"),
    ("WEBRip", r"\bwebrip\b"),
    ("WEB", r"\b(web)\b"),
    ("HDTV", r"\b(hdtv|pdtv|dsr)\b"),
    ("DVDRip", r"\b(dvdrip|dvdscr)\b"),
    ("DVD", r"\bdvd\b"),
    ("HDRip", r"\b(hdrip)\b"),
    ("TELESYNC", r"\b(ts|tc|cam)\b"),
]

# 视频编码
VCODEC = [
    ("AV1", r"\bav1\b"),
    ("H.265", r"\b(h\.?265|hevc|x265|h265)\b"),
    ("H.264", r"\b(h\.?264|avc|x264|h264)\b"),
    ("VC-1", r"\bvc-?1\b"),
    ("VP9", r"\bvp-?9\b"),
    ("MPEG-2", r"\bmpeg-?2\b"),
]

# 动态范围（英文显示，参考 MT 命名规则；可多命中：DV+HDR10 双格式很常见）
HDR = [
    ("Dolby Vision", r"\b(dolby\s?vision|dovi|dolby\.vision|\bdv\b)"),
    ("HDR10+", r"\bhdr10\+|hdr10plus\b"),
    ("HDR10", r"\bhdr10\b"),
    ("HLG", r"\bhlg\b"),
    ("HDR", r"\bhdr\b"),
    ("SDR", r"\bsdr\b"),
]

# DV Profile（P5/P7/P8）：DV.P7 / DoVi.P8 / Profile7 / dolby vision profile 8
DV_PROFILE_RE = [
    re.compile(r"\b(?:dv|dovi|dolby[ ._-]?vision)[ ._-]?(?:p|profile)?[ ._-]?([578])\b"),
    re.compile(r"\bprofile[ ._-]?([578])\b"),
]

# 色深
DEPTH = [
    ("12bit", r"\b12\s?-?bit\b"),
    ("10bit", r"\b10\s?-?bit\b|\bhi10p?\b"),
    ("8bit", r"\b8\s?-?bit\b"),
]

# 音频：格式 + 声道
ACODEC = [
    ("TrueHD Atmos", r"\b(truehd).{0,12}(atmos)\b|\b(atmos).{0,12}(truehd)\b"),
    ("Atmos", r"\batmos\b"),
    ("TrueHD", r"\btrue-?hd\b"),
    ("DTS-HD MA", r"\bdts[-. _]?hd[-. _]?ma\b"),
    ("DTS-X", r"\bdts[-. _]?x\b"),
    ("DTS", r"\bdts\b"),
    ("DD+", r"\b(dd\+|ddp|eac3|e-?ac-?3|dd5|ddp5)\b"),
    ("DD", r"\b(dd|ac3|ac-?3|dolby\s?digital)\b"),
    ("FLAC", r"\bflac\b"),
    ("APE", r"\bape\b"),
    ("AAC", r"\baac\b"),
    ("MP3", r"\bmp3\b"),
    ("LPCM", r"\b(lpcm|pcm)\b"),
]

CHANNEL = [
    ("7.1", r"\b7\.1\b|\b8ch\b"),
    ("5.1", r"\b5\.1\b|\b6ch\b|\bdd5\b|\bddp5\b"),
    ("2.0", r"\b2\.0\b|\b2ch\b|\bstereo\b"),
]

# 帧率：种子名里显式标注才有（60FPS / 23.976fps / 23976fps）；没有就留空不猜
FPS_RE = re.compile(r"\b(\d{2,6}(?:\.\d{1,3})?)\s?fps\b", re.I)

# 制作组/字幕组：种子名末尾常见 -XXX / @XXX / [XXX]
GROUP_RE = re.compile(r"(?:[-@]\s?)?\[?([A-Za-z0-9_\.]{2,20})\]?\s*$")


def _hit(name, table):
    for label, pat in table:
        if re.search(pat, name, re.I):
            return label
    return ""


def _hits(name, table):
    """多命中：按表顺序收集全部匹配项（用于动态范围——DV+HDR10 双格式常见）"""
    out = []
    for label, pat in table:
        if re.search(pat, name, re.I) and label not in out:
            out.append(label)
    if "SDR" in out:
        # SDR 与其他动态范围互斥：有 HDR/DV 就不要 SDR
        out = [x for x in out if x != "SDR"] or ["SDR"]
    return out


def _dv_profile(low: str) -> str:
    """从种子名提取 DV Profile（P5/P7/P8），提取不到返回空"""
    for pat in DV_PROFILE_RE:
        m = pat.search(low)
        if m:
            return "P" + m.group(1)
    return ""


def parse(name: str) -> dict:
    """解析种子名，返回 {分辨率, 片源, 编码, 动态范围, DV_profile, 色深, 音频, 声道, 制作组}

    动态范围英文显示（Dolby Vision / HDR10+ / HDR10 / HLG / SDR），
    DV 带 profile 时直接拼进串里：Dolby Vision P7 + HDR10。
    """
    n = (name or "").replace("_", " ")
    low = n.lower()
    g = GROUP_RE.search(n.replace(".", " ").strip())
    group = g.group(1).strip() if g else ""
    # 制作组里常见 "DepWeb"/"UBWEB"/"KyoGo"/"FROGWeb"，但也可能是年份或分辨率残片
    if group and re.fullmatch(r"(19|20)\d{2}|\d{3,4}[pi]|web|dl|x26\d", group, re.I):
        group = ""
    dr = _hits(low, HDR)
    prof = _dv_profile(low)
    if prof:
        if "Dolby Vision" in dr:
            dr = [f"Dolby Vision {prof}" if x == "Dolby Vision" else x for x in dr]
        else:
            dr.insert(0, f"Dolby Vision {prof}")
    fps = ""
    fm = FPS_RE.search(low)
    if fm:
        try:
            fv = float(fm.group(1))
            if fv > 240:                  # 个别把 23.976 写成 23976，修正
                fv /= 1000.0
            if 1 < fv <= 240:
                fps = f"{fv:g}fps"
        except Exception:
            fps = ""
    # UHD：4K 里再分一档 —— 2160p/UHD 且是蓝光原盘类片源才叫 4K UHD
    res = _hit(low, RES)
    src = _hit(low, SOURCE)
    uhd = bool(res == "4K" and re.search(
        r"\buhd\b|\b2160p\b|\b4k\b", low) and re.search(
        r"blu-?ray|remux|bdmv|iso|\bbd\b", low))
    return {
        "分辨率": "4K UHD" if uhd else res,
        "片源": src,
        "编码": _hit(low, VCODEC),
        "动态范围": " + ".join(dr) if dr else "",
        "DV_profile": prof,
        "色深": _hit(low, DEPTH),
        "帧率": fps,
        "音频": _hit(low, ACODEC),
        "声道": _hit(low, CHANNEL),
        "制作组": group,
    }


def short(info: dict) -> str:
    """拼成一行紧凑的画质串（全英文值）：
    「4K · Dolby Vision P8 + HDR10 · 10bit · 23.976fps · Blu-ray REMUX · H.265 · TrueHD Atmos 7.1」
    （动态范围/色深/帧率在 bot 侧会拆成独立行展示，这里保留完整信息）"""
    parts = []
    for k in ("分辨率", "动态范围", "色深", "帧率", "片源", "编码"):
        if info.get(k):
            parts.append(info[k])
    audio = " ".join(x for x in (info.get("音频"), info.get("声道")) if x)
    if audio:
        parts.append(audio)
    return " · ".join(parts)


def tags(info: dict) -> list[str]:
    """给播报用的徽章列表（只保留有意义的高亮项）"""
    out = []
    if info.get("分辨率"):
        out.append(info["分辨率"])
    if info.get("动态范围") and info["动态范围"] != "SDR":
        out.append(info["动态范围"])
    if info.get("色深"):
        out.append(info["色深"])
    if info.get("帧率"):
        out.append(info["帧率"])
    if info.get("片源"):
        out.append(info["片源"])
    if info.get("音频"):
        out.append(info["音频"])
    if info.get("编码"):
        out.append(info["编码"])
    return out[:5]
