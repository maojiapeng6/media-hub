"""观影榜海报渲染 —— 用 PIL 把榜单画成一张高级感的图

设计要点：
  · 深色渐变底 + 微光晕，避免纯黑显得廉价
  · Top3 走「大卡片海报」，视觉重心放在最火的几部
  · 4~10 名走紧凑列表 + 时长占比条，信息密度高但不乱
  · 封面直接从 Emby 拉 Primary 图，拉不到自动占位（首字 + 图标底）
  · 全部素材带缓存：影片封面 1 小时、Emby 搜索结果复用
"""
from __future__ import annotations

import io
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from . import settings as S
from .ui import cn_no

FONT_PATH = os.environ.get("KFC_FONT", "/app/fonts/PingFangBold.ttf")

# ── 配色
BG      = (11, 15, 26)
BG2     = (17, 22, 38)
CARD    = (23, 29, 48)
CARD2   = (30, 37, 60)
GOLD    = (255, 199, 74)
SILVER  = (198, 206, 224)
BRONZE  = (214, 141, 84)
ACCENT  = (249, 219, 160)
WHITE   = (255, 255, 255)
GRAY    = (138, 147, 168)
DIM     = (96, 106, 128)
LINE    = (40, 48, 74)
BADGE   = (112, 134, 214)     # 4 名以后统一用的冷色徽章

_f = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    key = int(size)
    if key in _f:
        return _f[key]
    try:
        ft = ImageFont.truetype(FONT_PATH, size)
    except Exception as e:                                   # 字体缺失时退化，不至于崩
        logger.warning(f"字体加载失败，回退默认字体: {e}")
        ft = ImageFont.load_default(size)
    _f[key] = ft
    return ft


# ══════════════════════════════════════════════════════ Emby 封面
_raw_cache: dict[str, tuple[float, bytes | None]] = {}
_RAW_TTL = 3600


def _emby_url() -> str:
    return S.EMBY["url"].rstrip("/")


def _search_item_id(name: str) -> str | None:
    try:
        r = requests.get(
            f"{_emby_url()}/emby/Items",
            params={"api_key": S.EMBY["api_key"], "SearchTerm": name,
                    "IncludeItemTypes": "Movie,Series", "Recursive": "true",
                    "Limit": 1, "Fields": "Id"},
            timeout=10)
        arr = (r.json() or {}).get("Items") or []
        return arr[0].get("Id") if arr else None
    except Exception:
        return None


def _raw(name: str) -> bytes | None:
    now = time.time()
    c = _raw_cache.get(name)
    if c and now - c[0] < _RAW_TTL:
        return c[1]
    data = None
    iid = _search_item_id(name)
    if iid:
        for attempt in range(2):
            try:
                r = requests.get(
                    f"{_emby_url()}/emby/Items/{iid}/Images/Primary",
                    params={"api_key": S.EMBY["api_key"], "maxWidth": 400, "quality": 92},
                    timeout=15)
                if r.status_code == 200 and r.content[:4] != b"<htm":
                    data = r.content
                    break
            except Exception:
                pass
    _raw_cache[name] = (now, data)
    return data


# ══════════════════════════════════════════════════════ 绘图小工具
def _round(im: Image.Image, radius: int) -> Image.Image:
    w, h = im.size
    m = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * 4 - 1, h * 4 - 1],
                                        radius=radius * 4, fill=255)
    im.putalpha(m.resize((w, h), Image.LANCZOS))
    return im


def _clip(d: ImageDraw.Draw, text: str, ft, max_w: int) -> str:
    if d.textbbox((0, 0), text, font=ft)[2] <= max_w:
        return text
    t = text
    while t and d.textbbox((0, 0), t + "…", font=ft)[2] > max_w:
        t = t[:-1]
    return (t or "?") + "…"


def _cover(raw: bytes | None, name: str, w: int, h: int, radius: int = 14) -> Image.Image:
    """按 2:3 比例裁剪出封面；raw 为空则生成占位图"""
    if raw:
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            tr = w / h
            ir = im.width / im.height
            if ir > tr:                       # 太宽：横向居中裁
                nw = int(im.height * tr)
                x = (im.width - nw) // 2
                im = im.crop((x, 0, x + nw, im.height))
            else:                             # 太高：偏上裁（保住人脸）
                nh = int(im.width / tr)
                y = min(int((im.height - nh) * 0.18), im.height - nh)
                im = im.crop((0, y, im.width, y + nh))
            return _round(im.resize((w, h), Image.LANCZOS), radius)
        except Exception as e:
            logger.debug(f"封面处理失败 {name}: {e}")

    ph = Image.new("RGBA", (w, h), (34, 41, 66, 255))
    d = ImageDraw.Draw(ph)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=(34, 41, 66, 255))
    ch = (name or "?").strip()[:1] or "?"
    d.text((w / 2, h / 2 - h * 0.04), ch, font=font(int(h * 0.34)),
           fill=(104, 116, 148, 255), anchor="mm")
    d.text((w / 2, h * 0.80), "无封面", font=font(int(h * 0.075)),
           fill=(82, 92, 120, 255), anchor="mm")
    return ph


def _hm(minutes: float) -> str:
    m = int(max(minutes, 0))
    if m < 60:
        return f"{m} 分钟"
    return f"{m // 60} 时 {m % 60:02d} 分"


def _badge(canvas: Image.Image, x: int, y: int, no: int, size: int = 46):
    """名次徽章 —— 1金 2银 3铜，4 名以后统一用同一枚冷色徽章。

    ★ 徽章里写「第一名 / 第二名」中文名次，不再用 01 02 03 这种机械序号。
    返回徽章宽度，调用方据此摆放后面的内容，避免中文徽章变宽后压住封面。
    """
    if no == 1:
        col = GOLD
    elif no == 2:
        col = SILVER
    elif no == 3:
        col = BRONZE
    else:
        col = BADGE
    label = f"第{cn_no(no)}名"
    ft = font(size - 16)
    d = ImageDraw.Draw(canvas)
    w = int(ImageDraw.Draw(canvas).textlength(label, font=ft)) + size
    bg = Image.new("RGBA", (w, size), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle([0, 0, w - 1, size - 1],
                                         radius=size // 2, fill=(*col, 245))
    canvas.alpha_composite(bg, (int(x), int(y)))
    d.text((int(x) + w / 2, int(y) + size / 2 + 1), label,
           font=ft, fill=(14, 18, 30, 255), anchor="mm")
    return w


def _clock(d: ImageDraw.Draw, cx: float, cy: float, r: int = 8,
           col=(255, 255, 255, 210)):
    """手绘一枚小时钟图标 —— 苹方字体里没有 ⏱ 字形，直接画才不会出方块"""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=2)
    d.line([cx, cy - r * 0.45, cx, cy], fill=col, width=2)
    d.line([cx, cy, cx + r * 0.42, cy + r * 0.18], fill=col, width=2)


def _dur(d: ImageDraw.Draw, x_right: float, y_mid: float, minutes: float,
         ft, col=ACCENT):
    """右对齐的「时钟 + 时长」，时长旁统一带一枚小时钟"""
    txt = _hm(minutes)
    tw = d.textlength(txt, font=ft)
    _clock(d, x_right - tw - 15, y_mid, max(int(ft.size * 0.34), 7), col)
    d.text((x_right, y_mid), txt, font=ft, fill=col, anchor="rm")
def draw_board(title: str, items: list[dict], footnote: str = "") -> bytes | None:
    """返回 PNG 字节；items 形如 [{name, plays, minutes}]"""
    try:
        return _draw(title, items, footnote)
    except Exception as e:
        logger.warning(f"榜单海报渲染失败: {e}")
        return None


def _draw(title: str, items: list[dict], footnote: str) -> bytes:
    items = [i for i in items if i.get("name")][:10]
    W, PAD = 1160, 44

    HEAD_H   = 148
    CW, CH   = 336, 566
    GAP      = 30
    ROW_H    = 92
    rows     = items[3:]
    H = (HEAD_H + (CH + 26 if items else 0)
         + len(rows) * ROW_H + 78 + (0 if rows else 10))
    H = max(H, 420)

    cv = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    base = Image.new("RGB", (W, H), BG)
    bd = ImageDraw.Draw(base)
    for y in range(H):                                    # 上下轻微渐晕
        t = y / max(H - 1, 1)
        r = int(BG[0] + (BG2[0] - BG[0]) * (1 - t) * 0.55)
        g = int(BG[1] + (BG2[1] - BG[1]) * (1 - t) * 0.55)
        b = int(BG[2] + (BG2[2] - BG[2]) * (1 - t) * 0.55)
        bd.line([(0, y), (W, y)], fill=(r, g, b))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-260, -320, 520, 300], fill=(58, 84, 168, 46))
    base = Image.alpha_composite(base.convert("RGBA"), glow)
    cv.paste(base)
    d = ImageDraw.Draw(cv)

    # ── 头部
    d.rounded_rectangle([PAD, 52, PAD + 7, 96], radius=4, fill=ACCENT)
    d.text((PAD + 24, 52), title, font=font(46), fill=WHITE, anchor="la")
    sub = footnote or f"{S.SERVER_NAME} · {time.strftime('%Y-%m-%d')}"
    d.text((PAD + 24, 108), sub, font=font(21), fill=GRAY, anchor="la")

    total = sum(i.get("minutes", 0) for i in items) or 1
    if items:
        d.text((W - PAD, 62), f"共 {len(items)} 部", font=font(22),
               fill=GRAY, anchor="ra")
        d.text((W - PAD, 96), f"累计 {_hm(total)}", font=font(22),
               fill=DIM, anchor="ra")

    if not items:
        d.text((W / 2, H * 0.62), "暂无播放数据", font=font(38), fill=GRAY, anchor="mm")
        d.text((W / 2, H * 0.62 + 52), "看几部片就会自动上榜啦",
               font=font(22), fill=DIM, anchor="mm")
        out = io.BytesIO()
        cv.convert("RGB").save(out, "PNG", optimize=True)
        return out.getvalue()

    # ── Top3 卡片
    top = items[:3]
    names = [i["name"] for i in items]
    with ThreadPoolExecutor(max_workers=10) as ex:
        raws = list(ex.map(_raw, names))

    y0 = HEAD_H
    for idx, it in enumerate(top):
        x = PAD + idx * (CW + GAP)
        ImageDraw.Draw(cv).rounded_rectangle(
            [x, y0, x + CW, y0 + CH], radius=18, fill=CARD)
        pw = CW - 28
        ph = int(pw * 1.5)
        cover = _cover(raws[idx], it["name"], pw, ph, radius=13)
        cv.alpha_composite(cover, (x + 14, y0 + 14))

        # 底部信息条
        ty = y0 + 14 + ph + 8
        _badge(cv, x + 16, ty - 2, idx + 1, 40)
        _dur(d, x + CW - 16, ty + 29, it["minutes"], font(23))
        d.text((x + CW - 16, ty + 59), f"{it['plays']} 次播放", font=font(19),
               fill=GRAY, anchor="rm")

        # 片名（海报下方两行）
        nline = it["name"]
        d.text((x + 16, ty + 52), _clip(d, nline, font(25), CW - 150),
               font=font(25), fill=WHITE, anchor="lm")

    # ── 4~10 名列表 —— 名次徽章与 Top3 同款，保证视觉一致
    ry = y0 + CH + 26
    row_h = ROW_H - 12
    for k, it in enumerate(rows):
        no = k + 4
        yy = ry + k * ROW_H
        ImageDraw.Draw(cv).rounded_rectangle(
            [PAD, yy, W - PAD, yy + row_h], radius=14, fill=CARD2)

        bw = _badge(cv, PAD + 16, yy + (row_h - 40) / 2, no, 40)

        th = ROW_H - 32
        cw = int(th * 2 / 3)
        cover = _cover(raws[3 + k], it["name"], cw, th, radius=8)
        cx0 = PAD + 16 + bw + 20          # 徽章变宽后封面跟着右移，不会被压住
        cv.alpha_composite(cover, (cx0, yy + 8))

        tx = cx0 + cw + 18
        # 片名可用宽度：不能压到右侧的时长/占比条
        bar_x = W - PAD - 30 - 200
        name_w = int(min(W * 0.46, max(bar_x - 24 - tx, 200)))
        d.text((tx, yy + 24), _clip(d, it["name"], font(24), name_w),
               font=font(24), fill=WHITE, anchor="lm")
        d.text((tx, yy + 56), f"{it['plays']} 次播放", font=font(19),
               fill=DIM, anchor="lm")

        # 时长占比条（开方压缩，避免小数值几乎看不见）
        bw = 200
        bx = W - PAD - 30 - bw
        by = yy + 26
        ImageDraw.Draw(cv).rounded_rectangle([bx, by, bx + bw, by + 12],
                                             radius=6, fill=(18, 23, 38))
        ratio = max(min((it["minutes"] / max(items[0]["minutes"], 1)) ** 0.5, 1.0), 0.08)
        ImageDraw.Draw(cv).rounded_rectangle(
            [bx, by, bx + int(bw * ratio), by + 12], radius=6, fill=GOLD)
        _dur(d, W - PAD - 30, yy + 55, it["minutes"], font(22))

    # ── 页脚
    fy = H - 34
    d.text((PAD, fy), f"{S.SERVER_NAME} · 数据来自 Emby Playback Reporting",
           font=font(18), fill=DIM, anchor="lm")
    d.text((W - PAD, fy), time.strftime("%H:%M"), font=font(18),
           fill=DIM, anchor="rm")

    out = io.BytesIO()
    cv.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# ══════════════════════════════════════════════════════ 用户榜（谁看得最多）
_PALETTE = [(94, 129, 255), (147, 102, 255), (255, 110, 160), (255, 159, 67),
            (38, 198, 218), (76, 201, 120), (240, 98, 146), (255, 193, 7)]


def _avatar(name: str, size: int, raw: bytes | None = None) -> Image.Image:
    """优先用真人 TG 头像（裁成圆形），拿不到就退回彩色首字头像"""
    if raw:
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            s = min(im.width, im.height)
            x, y = (im.width - s) // 2, (int((im.height - s) * 0.28))
            im = im.crop((x, y, x + s, y + s)).resize((size, size), Image.LANCZOS)
            m = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(m).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
            im.putalpha(m.resize((size, size), Image.LANCZOS))
            return im
        except Exception as e:
            logger.debug(f"TG 头像处理失败 {name}: {e}")

    col = _PALETTE[sum(ord(c) for c in (name or "?")) % len(_PALETTE)]
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, size - 1, size - 1], fill=(*col, 255))
    d.ellipse([0, 0, size - 1, size - 1], outline=(255, 255, 255, 46), width=max(size // 26, 2))
    ch = (name or "?").strip()[:1].upper() or "?"
    d.text((size / 2, size / 2 + size * 0.02), ch, font=font(int(size * 0.44)),
           fill=(255, 255, 255, 255), anchor="mm")
    return im


def draw_user_board(title: str, items: list[dict], footnote: str = "") -> bytes | None:
    """用户榜：没有封面可用，改用「彩色头像 + 数据」的卡片式设计"""
    try:
        return _draw_user(title, items, footnote)
    except Exception as e:
        logger.warning(f"用户榜海报渲染失败: {e}")
        return None


def _draw_user(title: str, items: list[dict], footnote: str) -> bytes:
    items = [i for i in items if i.get("name")][:10]
    W, PAD = 1160, 44

    HEAD_H = 148
    CW, CH = 336, 330
    GAP = 30
    ROW_H = 92
    rows = items[3:]
    H = max(HEAD_H + (CH + 26 if items else 0) + len(rows) * ROW_H + 78, 420)

    cv = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    base = Image.new("RGB", (W, H), BG)
    bd = ImageDraw.Draw(base)
    for y in range(H):
        t = y / max(H - 1, 1)
        r = int(BG[0] + (BG2[0] - BG[0]) * (1 - t) * 0.55)
        g = int(BG[1] + (BG2[1] - BG[1]) * (1 - t) * 0.55)
        b = int(BG[2] + (BG2[2] - BG[2]) * (1 - t) * 0.55)
        bd.line([(0, y), (W, y)], fill=(r, g, b))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 520, -320, W + 260, 300], fill=(126, 68, 176, 44))
    cv.paste(Image.alpha_composite(base.convert("RGBA"), glow))
    d = ImageDraw.Draw(cv)

    d.rounded_rectangle([PAD, 52, PAD + 7, 96], radius=4, fill=ACCENT)
    d.text((PAD + 24, 52), title, font=font(46), fill=WHITE, anchor="la")
    d.text((PAD + 24, 108), footnote or f"{S.SERVER_NAME} · {time.strftime('%Y-%m-%d')}",
           font=font(21), fill=GRAY, anchor="la")
    total = sum(i.get("minutes", 0) for i in items) or 1
    if items:
        d.text((W - PAD, 62), f"共 {len(items)} 位", font=font(22), fill=GRAY, anchor="ra")
        d.text((W - PAD, 96), f"总时长 {_hm(total)}", font=font(22), fill=DIM, anchor="ra")

    if not items:
        d.text((W / 2, H * 0.62), "暂无观看数据", font=font(38), fill=GRAY, anchor="mm")
        out = io.BytesIO()
        cv.convert("RGB").save(out, "PNG", optimize=True)
        return out.getvalue()

    y0 = HEAD_H
    ring = {1: GOLD, 2: SILVER, 3: BRONZE}
    for idx, it in enumerate(items[:3]):
        x = PAD + idx * (CW + GAP)
        ImageDraw.Draw(cv).rounded_rectangle([x, y0, x + CW, y0 + CH],
                                             radius=18, fill=CARD)
        col = ring.get(idx + 1, GRAY)
        # 名次色描边的头像
        av = _avatar(it["name"], 150, it.get("avatar"))
        halo = Image.new("RGBA", (168, 168), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse([0, 0, 167, 167], outline=(*col, 255),
                                     width=6)
        halo.alpha_composite(av, (9, 9))
        cv.alpha_composite(halo, (x + CW // 2 - 84, y0 + 26))

        # 名次徽章（与列表行同款）+ 居中时长带小时钟
        _badge(cv, x + 18, y0 + 18, idx + 1, 40)
        # 名称行：Emby 用户名 + TG 用户名（图片无法点击，可点链接在文字说明里）
        nm = it["name"]
        un = str(it.get("username") or "")
        if un and un.lower() != str(nm).lower():   # 同名就不重复显示
            nm = f"{nm} · {un}"                    # 用户名前不加 @，保持干净
        d.text((x + CW / 2, y0 + 208), _clip(d, nm, font(30), CW - 40),
               font=font(30), fill=WHITE, anchor="mm")
        ft = font(27)
        cx = x + CW / 2
        tw = d.textlength(_hm(it["minutes"]), font=ft)
        _clock(d, cx - tw / 2 - 15, y0 + 252, 9, col)
        d.text((cx + 7, y0 + 252), _hm(it["minutes"]), font=ft,
               fill=col, anchor="mm")
        d.text((x + CW / 2, y0 + 288),
               f"{it['plays']} 次播放",
               font=font(20), fill=GRAY, anchor="mm")

    ry = y0 + CH + 26
    row_h = ROW_H - 12
    for k, it in enumerate(rows):
        no = k + 4
        yy = ry + k * ROW_H
        ImageDraw.Draw(cv).rounded_rectangle([PAD, yy, W - PAD, yy + row_h],
                                             radius=14, fill=CARD2)
        bw = _badge(cv, PAD + 16, yy + (row_h - 40) / 2, no, 40)
        av = _avatar(it["name"], 56, it.get("avatar"))
        ax0 = PAD + 16 + bw + 20          # 徽章变宽后头像跟着右移
        cv.alpha_composite(av, (ax0, yy + 12))
        # 名称行：Emby 用户名 + TG 用户名（同名不重复）
        nm = it["name"]
        un = str(it.get("username") or "")
        if un and un.lower() != str(nm).lower():
            nm = f"{nm} · {un}"
        bar_x = W - PAD - 30 - 200
        name_w = int(min(W * 0.40, max(bar_x - 24 - (ax0 + 66), 200)))
        d.text((ax0 + 66, yy + 24), _clip(d, nm, font(24), name_w),
               font=font(24), fill=WHITE, anchor="lm")
        # 数据行：次数 + 时长
        d.text((ax0 + 66, yy + 56),
               f"{it['plays']} 次播放 · {_hm(it['minutes'])}",
               font=font(19), fill=DIM, anchor="lm")

        bw = 200
        bx = W - PAD - 30 - bw
        by = yy + 26
        ImageDraw.Draw(cv).rounded_rectangle([bx, by, bx + bw, by + 12],
                                             radius=6, fill=(18, 23, 38))
        ratio = max(min((it["minutes"] / max(items[0]["minutes"], 1)) ** 0.5, 1.0), 0.08)
        ImageDraw.Draw(cv).rounded_rectangle(
            [bx, by, bx + int(bw * ratio), by + 12], radius=6, fill=GOLD)
        _dur(d, W - PAD - 30, yy + 55, it["minutes"], font(22))

    fy = H - 34
    d.text((PAD, fy), f"{S.SERVER_NAME} · 数据来自 Emby Playback Reporting",
           font=font(18), fill=DIM, anchor="lm")
    d.text((W - PAD, fy), time.strftime("%H:%M"), font=font(18), fill=DIM, anchor="rm")

    out = io.BytesIO()
    cv.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
