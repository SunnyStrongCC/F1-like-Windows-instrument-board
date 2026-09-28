# -*- coding: utf-8 -*-
"""F1 遥测仪表盘 —— 配色、字体与绘图基础工具。

配色参考 F1 官方转播图形：碳纤维深色底 + 梅赛德斯青绿 + F1 红 + 荧光黄。
字体使用 Windows 自带的 Bahnschrift（可变字体，可取 Condensed 窄体），
数字/等宽场景使用 Consolas，中文回退到微软雅黑。
"""
from __future__ import annotations

import os
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

Color = Tuple[int, int, int]

# ---------------------------------------------------------------- 基础色板
BG = (8, 10, 13)
CARBON_0 = (11, 13, 17)
CARBON_1 = (18, 22, 28)
CARBON_2 = (26, 32, 40)
CARBON_3 = (34, 42, 52)
EDGE = (46, 56, 68)
EDGE_SOFT = (30, 37, 46)

TEAL = (39, 244, 210)
TEAL_DIM = (16, 116, 104)
TEAL_DEEP = (10, 62, 58)
RED = (225, 6, 0)
RED_DIM = (92, 12, 10)
YELLOW = (255, 212, 0)
AMBER = (255, 150, 0)
GREEN = (52, 211, 110)
GREEN_DIM = (16, 74, 44)
PURPLE = (176, 38, 255)
MAGENTA = (255, 45, 130)
CYAN = (56, 200, 255)

WHITE = (244, 248, 252)
SILVER = (186, 197, 209)
MUTED = (118, 132, 148)
DIM = (74, 86, 100)

# F1 车队色：用于进程排行榜的色条，视觉上还原计时塔
TEAM_COLORS = [
    (225, 6, 0),      # Ferrari 红
    (39, 244, 210),   # Mercedes 青
    (255, 128, 0),    # McLaren 橙
    (54, 113, 198),   # Red Bull 蓝
    (34, 156, 118),   # Aston Martin 绿
    (255, 135, 188),  # Alpine 粉
    (100, 196, 255),  # Williams 亮蓝
    (102, 146, 255),  # RB 蓝
    (82, 226, 82),    # Sauber 亮绿
    (182, 186, 189),  # Haas 银
    (255, 212, 0),    # 备用黄
    (176, 38, 255),   # 备用紫
]

# ---------------------------------------------------------------- 字体加载
_FONT_CACHE: dict = {}
_CJK_CACHE: dict = {}


def font(size: int, style: str = "semi", condensed: bool = True, mono: bool = False):
    """取一个字体对象。style: light/regular/semi/bold。"""
    key = (size, style, condensed, mono)
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit

    if mono:
        name = "consolab.ttf" if style in ("semi", "bold") else "consola.ttf"
        path = os.path.join(FONT_DIR, name)
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            f = ImageFont.load_default()
    else:
        path = os.path.join(FONT_DIR, "bahnschrift.ttf")
        try:
            f = ImageFont.truetype(path, size)
            variation = {
                "light": "Light",
                "regular": "Regular",
                "semi": "SemiBold",
                "bold": "Bold",
            }.get(style, "SemiBold")
            if condensed:
                variation += " Condensed"
            try:
                f.set_variation_by_name(variation)
            except Exception:
                pass
        except Exception:
            try:
                f = ImageFont.truetype(os.path.join(FONT_DIR, "ARIALNB.TTF"), size)
            except Exception:
                f = ImageFont.load_default()

    _FONT_CACHE[key] = f
    return f


def font_cjk(size: int, bold: bool = False):
    """中文字体（微软雅黑），仅在中英混排需要时使用。"""
    key = (size, bold)
    hit = _CJK_CACHE.get(key)
    if hit is not None:
        return hit
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    try:
        f = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except Exception:
        try:
            f = ImageFont.truetype(os.path.join(FONT_DIR, "msyh.ttc"), size)
        except Exception:
            f = font(size, "semi")
    _CJK_CACHE[key] = f
    return f


def pick_font(text: str, size: int, style: str = "semi", condensed: bool = True, mono: bool = False):
    """按文本内容自动选择能否渲染该文本的字体。"""
    if text and not text.isascii():
        return font_cjk(size, bold=style in ("semi", "bold"))
    return font(size, style, condensed, mono)


# ---------------------------------------------------------------- 绘图工具
def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(c1: Color, c2: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (
        int(round(lerp(c1[0], c2[0], t))),
        int(round(lerp(c1[1], c2[1], t))),
        int(round(lerp(c1[2], c2[2], t))),
    )


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def hexc(s: str) -> Color:
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def heat(ratio: float) -> Color:
    """负载色阶：青 -> 绿 -> 黄 -> 红"""
    r = clamp(ratio)
    stops = [
        (0.00, (39, 244, 210)),
        (0.45, (52, 211, 110)),
        (0.70, (255, 212, 0)),
        (0.88, (255, 140, 0)),
        (1.00, (255, 48, 40)),
    ]
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if r <= p1:
            t = (r - p0) / (p1 - p0) if p1 > p0 else 0.0
            return lerp_color(c0, c1, t)
    return stops[-1][1]


def temp_color(celsius: float | None) -> Color:
    """温度色阶（针对 CPU/GPU 的常见区间 30~95°C）。"""
    if celsius is None:
        return MUTED
    stops = [
        (30, (56, 200, 255)),
        (50, (39, 244, 210)),
        (65, (52, 211, 110)),
        (75, (255, 212, 0)),
        (85, (255, 140, 0)),
        (95, (255, 48, 40)),
    ]
    if celsius <= stops[0][0]:
        return stops[0][1]
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if celsius <= p1:
            return lerp_color(c0, c1, (celsius - p0) / (p1 - p0))
    return stops[-1][1]


def vgradient(size, top: Color, bottom: Color, alpha_top: int = 255, alpha_bottom: int = 255) -> Image.Image:
    """竖直渐变图。"""
    w, h = size
    img = Image.new("RGBA", (1, max(1, h)))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = lerp_color(top, bottom, t)
        a = int(round(lerp(alpha_top, alpha_bottom, t)))
        px[0, y] = (c[0], c[1], c[2], a)
    return img.resize((w, h), Image.BILINEAR)


def hgradient(size, left: Color, right: Color, alpha_left: int = 255, alpha_right: int = 255) -> Image.Image:
    """水平渐变图。"""
    w, h = size
    img = Image.new("RGBA", (max(1, w), 1))
    px = img.load()
    for x in range(w):
        t = x / max(1, w - 1)
        c = lerp_color(left, right, t)
        a = int(round(lerp(alpha_left, alpha_right, t)))
        px[x, 0] = (c[0], c[1], c[2], a)
    return img.resize((w, h), Image.BILINEAR)


def paste_gradient(base: Image.Image, box, img: Image.Image, mask=None):
    """把渐变图按圆角遮罩贴进 base。"""
    x0, y0, x1, y1 = [int(v) for v in box]
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)
    if img.size != (w, h):
        img = img.resize((w, h), Image.BILINEAR)
    if mask is not None:
        if mask.size != (w, h):
            mask = mask.resize((w, h), Image.LANCZOS)
        base.paste(img, (x0, y0), mask)
    else:
        base.paste(img, (x0, y0), img)


def rounded_mask(size, radius: int) -> Image.Image:
    w, h = size
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=radius * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def text_size(draw: ImageDraw.ImageDraw, text: str, f) -> Tuple[int, int]:
    if not text:
        return (0, 0)
    box = draw.textbbox((0, 0), text, font=f)
    return (box[2] - box[0], box[3] - box[1])


def draw_text(draw: ImageDraw.ImageDraw, xy, text: str, f, fill, anchor: str = "la"):
    """带一点点阴影的文字，提高在深色上的可读性。"""
    if not text:
        return
    x, y = xy
    draw.text((x + 1, y + 1), text, font=f, fill=(0, 0, 0, 150), anchor=anchor)
    draw.text((x, y), text, font=f, fill=fill, anchor=anchor)


def fit_font(text: str, max_w: int, start_size: int, style: str = "semi",
             condensed: bool = True, mono: bool = False, min_size: int = 8):
    """自动缩小字号直到文本宽度不超过 max_w。"""
    size = start_size
    while size > min_size:
        f = pick_font(text, size, style, condensed, mono)
        w = f.getlength(text)
        if w <= max_w:
            return f
        size -= 1
    return pick_font(text, min_size, style, condensed, mono)
