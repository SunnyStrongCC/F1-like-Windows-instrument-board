# -*- coding: utf-8 -*-
"""无边框悬浮小窗 —— 还原 F1 方向盘那块**圆形**曲面屏。

严格按实车照片的几何关系重画：

                    ╭─────────────╮
    ▤▤▤▤  ┌───────┤  C P U L O A D ├───────┐  ▤▤▤▤
    ▤▤▤▤  │ R E C  │       27       │  D E P │  ▤▤▤▤
    ▤▤▤▤  │ H A R  │  ╭ F R E Q ─╮  │  L O Y │  ▤▤▤▤
    ▤▤▤▤  │ G E    │  ╰ N O R M A L ╯ │        │  ▤▤▤▤
    ▤▤▤▤  │        │       22       │        │  ▤▤▤▤
          └───────┤  G P U L O A D ├───────┘
                   ╰─────────────╯
                     ▓▓▓▓░░▶ 64.0 °C

几何要点（这几条之前都做错了）：

* **中央是一整块正圆**，不是圆角矩形。外围一圈细亮环，屏面是上亮下暗的冷色
  渐变 + 竖向拉丝纹理。
* 两侧是**环形扇区**（以圆心为圆心、按角度切出来的扇形块），越靠外越长：
    - 内圈一块**蓝色标签牌**（±30°），竖排深蓝英文压在牌面上；
    - 外圈一条**分格 LED 表**（±19°），横向分格，点亮时是绿色 —— 对应实车
      ERS 那条绿色电量条。
* 底部电池压在圆的底边上：蓝色渐变 + 闪电图标 + 右侧凸点 + 一个暗色副格。

数据映射：

    CPU 负载 -> 圆内上方大号白字      GPU 负载 -> 圆内下方大号白字
    内存     -> 左侧蓝牌 + LED       显存     -> 右侧蓝牌 + LED
    CPU 温度 -> 底部电池电量 + 右侧读数
"""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import theme as T

MINI_W = 700
MINI_H = 396
SS = 2

CX, CY, R = 350.0, 178.0, 164.0

PLATE_R = (168.0, 238.0)
PLATE_ANG = 30.0
GAUGE_R = (243.0, 330.0)
GAUGE_ANG = 19.0
GAUGE_BANDS = 12

LBL_TOP_DY = -128
NUM_TOP_DY = -80
STRIP_FREQ_DY = (-36, -8)
STRIP_STATE_DY = (0, 30)
NUM_BOT_DY = 78
LBL_BOT_DY = 128
STRIP_FREQ_HW = 116.0
STRIP_STATE_HW = 132.0

BATTERY = (278.0, 320.0, 422.0, 350.0)
BATTERY_NUB = (422.0, 328.0, 433.0, 342.0)
BATTERY_CELL2 = (438.0, 321.0, 490.0, 345.0)
TEMP_TEXT = (504.0, 335.0)

TEMP_LO, TEMP_HI = 30.0, 95.0

SCREEN_HI = (34, 42, 53)
SCREEN_LO = (10, 13, 18)
RING = (198, 210, 222)
PLATE_HI = (36, 152, 220)
PLATE_LO = (10, 72, 120)
PLATE_EDGE = (124, 218, 250)
PLATE_TEXT = (4, 36, 60)
WHITE = (240, 245, 250)
GRAY = (152, 160, 170)
GREEN_HI = (96, 230, 126)
GREEN_LO = (16, 104, 54)

# LED 表的三种配色（低负载绿 / 偏高琥珀 / 危险红）
PALETTES = (
    ((16, 104, 54), (96, 230, 126)),
    ((122, 78, 14), (255, 202, 98)),
    ((120, 26, 22), (255, 110, 98)),
)


class MiniRenderer:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.W = MINI_W
        self.H = MINI_H
        self.k = SS
        self.transparent = bool(cfg.get("transparent", True))
        self.key_color = (1, 2, 3)
        self.base = None
        self.frame = None
        # 每侧：外接框 + 每格 y 区间 + 三套预渲染的“点亮”图层
        self.gauge_box: dict = {}
        self.band_ys: dict = {}
        self.lit_layers: dict = {}
        self._build_static()

    def B(self, box):
        k = self.k
        return [int(round(v * k)) for v in box]

    # ==================================================================
    # 几何工具
    # ==================================================================
    @staticmethod
    def _pt(cx, cy, r, a_deg):
        a = math.radians(a_deg)
        return (cx + r * math.cos(a), cy + r * math.sin(a))

    @classmethod
    def _sector(cls, cx, cy, r0, r1, a0, a1, steps=56):
        """环形扇区多边形。角度 0° = 正右，顺时针为正（屏幕 y 向下）。"""
        pts = [cls._pt(cx, cy, r1, a0 + (a1 - a0) * i / steps) for i in range(steps + 1)]
        pts += [cls._pt(cx, cy, r0, a1 + (a0 - a1) * i / steps) for i in range(steps + 1)]
        return pts

    @staticmethod
    def _round_mask(mask, radius):
        """先高斯模糊再按阈值二值化 —— 等价于把所有尖角按 radius 倒圆，纯 C 速度。"""
        if radius <= 0:
            return mask
        blurred = mask.filter(ImageFilter.GaussianBlur(radius))
        lut = bytes(255 if i > 127 else 0 for i in range(256))
        return blurred.point(lut).filter(ImageFilter.GaussianBlur(max(1.0, SS * 0.8)))

    def _poly_mask(self, pts, radius=0.0, ref=None):
        ref = ref if ref is not None else (self.W * self.k, self.H * self.k)
        m = Image.new("L", ref, 0)
        ImageDraw.Draw(m).polygon([(x * self.k, y * self.k) for x, y in pts], fill=255)
        return self._round_mask(m, radius * self.k)

    @staticmethod
    def _angles(side, half):
        return (180.0 - half, 180.0 + half) if side == "left" else (-half, half)

    # ==================================================================
    # 静态层
    # ==================================================================
    def _build_static(self):
        k = self.k
        bg = self.key_color if self.transparent else T.BG
        img = Image.new("RGBA", (self.W * k, self.H * k), bg + (255,))

        self._draw_dial(img)
        for side in ("left", "right"):
            self._draw_gauge(img, side)
        for side, label in (("left", "MEMORY"), ("right", "VRAM")):
            self._draw_plate(img, side, label)
        self._draw_battery_shell(img)

        self.base = img
        self.frame = img.copy()

    # ---------------------------------------------------------------- 中央圆屏
    def _draw_dial(self, img):
        k = self.k
        size = (self.W * k, self.H * k)
        cx, cy, r = CX * k, CY * k, R * k

        disc = Image.new("L", size, 0)
        ImageDraw.Draw(disc).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

        img.paste(T.vgradient(size, SCREEN_HI, SCREEN_LO), (0, 0), disc)

        # 竖向拉丝：先做成带 alpha 的纹理，再用圆盘遮罩合成，避免把屏面整体提亮
        tex = Image.new("RGBA", size, (0, 0, 0, 0))
        td = ImageDraw.Draw(tex)
        for x in range(0, size[0], 3 * k):
            td.line([(x, 0), (x, size[1])], fill=(255, 255, 255, 12), width=1)
            td.line([(x + 1, 0), (x + 1, size[1])], fill=(0, 0, 0, 38), width=1)
        tex_masked = Image.new("RGBA", size, (0, 0, 0, 0))
        tex_masked.paste(tex, (0, 0), disc)
        img.alpha_composite(tex_masked)

        # 内圈暗影 + 细亮环
        inner = disc.filter(ImageFilter.MinFilter(2 * 4 * k + 1))
        img.paste(Image.new("RGBA", size, (0, 0, 0, 90)), (0, 0),
                  ImageChops.subtract(disc, inner))
        ring = ImageChops.subtract(disc, disc.filter(ImageFilter.MinFilter(2 * 2 * k + 1)))
        img.paste(T.vgradient(size, RING, (118, 130, 144), 250, 160), (0, 0), ring)

    # ---------------------------------------------------------------- 外圈 LED 表
    def _draw_gauge(self, img, side):
        k = self.k
        a0, a1 = self._angles(side, GAUGE_ANG)
        pts = self._sector(CX, CY, GAUGE_R[0], GAUGE_R[1], a0, a1)
        mask = self._poly_mask(pts, radius=7)

        img.paste(Image.new("RGBA", mask.size, (8, 11, 15, 255)), (0, 0), mask)
        edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(2 * 2 * k + 1)))
        img.paste(Image.new("RGBA", mask.size, (96, 108, 122, 225)), (0, 0), edge)

        # 横向分格
        y_top = CY - GAUGE_R[1] * math.sin(math.radians(GAUGE_ANG))
        y_bot = CY + GAUGE_R[1] * math.sin(math.radians(GAUGE_ANG))
        step = (y_bot - y_top) / GAUGE_ANG_BANDS_DIV
        band_h = step * 0.62
        ys = []
        for i in range(GAUGE_BANDS):
            yy = y_top + i * step
            ys.append((yy, yy + band_h))

        # 外接框（用于把预渲染的点亮图层裁下来贴回）
        xs = [p[0] for p in pts]
        ys_all = [p[1] for p in pts]
        bx0 = int(math.floor(min(xs) * k)) - 2
        by0 = int(math.floor(min(ys_all) * k)) - 2
        bx1 = int(math.ceil(max(xs) * k)) + 2
        by1 = int(math.ceil(max(ys_all) * k)) + 2
        self.gauge_box[side] = (bx0, by0, bx1, by1)
        self.band_ys[side] = ys

        # 熄灭态
        for (yy, ye) in ys:
            bm = Image.new("L", mask.size, 0)
            ImageDraw.Draw(bm).rectangle([0, yy * k, mask.size[0], ye * k], fill=255)
            bm = ImageChops.multiply(bm, mask)
            img.paste(Image.new("RGBA", mask.size, (24, 30, 38, 255)), (0, 0), bm)

        # 预渲染三套点亮图层（底绿顶亮，横向分格留缝）
        sub = mask.crop((bx0, by0, bx1, by1))
        layers = []
        for lo, hi in PALETTES:
            layer = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            for i, (yy, ye) in enumerate(ys):
                t = i / float(GAUGE_BANDS - 1)
                col = T.lerp_color(lo, hi, t)
                ld.rectangle([0, int(yy * k) - by0, layer.width, int(ye * k) - by0],
                             fill=col + (236,))
            layer.putalpha(ImageChops.multiply(layer.split()[3], sub))
            layers.append(layer)
        self.lit_layers[side] = layers

    def _draw_plate(self, img, side, label):
        k = self.k
        a0, a1 = self._angles(side, PLATE_ANG)
        pts = self._sector(CX, CY, PLATE_R[0], PLATE_R[1], a0, a1)
        mask = self._poly_mask(pts, radius=8)

        grad = T.hgradient((self.W * k, self.H * k),
                           PLATE_HI if side == "left" else PLATE_LO,
                           PLATE_LO if side == "left" else PLATE_HI)
        img.paste(grad, (0, 0), mask)
        edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(2 * 2 * k + 1)))
        img.paste(Image.new("RGBA", mask.size, PLATE_EDGE + (210,)), (0, 0), edge)

        f = T.font(30 * k, "bold", condensed=False)
        spaced = "  ".join(list(label))
        tmp = Image.new("RGBA", (int(f.getlength(spaced)) + 18 * k, int(42 * k)), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((9 * k, 8 * k), spaced, font=f, fill=PLATE_TEXT + (255,))
        rot = tmp.rotate(-90, expand=True, resample=Image.BICUBIC)
        px, py = self._pt(CX, CY, (PLATE_R[0] + PLATE_R[1]) / 2.0, (a0 + a1) / 2.0)
        img.alpha_composite(rot, (int(px * k - rot.width / 2.0), int(py * k - rot.height / 2.0)))

    def _draw_battery_shell(self, img):
        k = self.k
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = BATTERY
        d.rounded_rectangle(self.B([x0 - 2, y0 - 2, x1 + 2, y1 + 2]), radius=10 * k,
                            fill=(6, 8, 11, 255))
        d.rounded_rectangle(self.B([x0, y0, x1, y1]), radius=8 * k,
                            fill=(14, 18, 24, 255), outline=(108, 126, 142, 255), width=2 * k)
        nx0, ny0, nx1, ny1 = BATTERY_NUB
        d.rounded_rectangle(self.B([nx0, ny0, nx1, ny1]), radius=4 * k,
                            fill=(108, 126, 142, 255))
        c0, c1, c2, c3 = BATTERY_CELL2
        d.rounded_rectangle(self.B([c0, c1, c2, c3]), radius=7 * k,
                            fill=(15, 30, 38, 235), outline=(58, 96, 112, 220), width=2 * k)

    # ==================================================================
    # 每帧
    # ==================================================================
    def render(self, snap: dict) -> Image.Image:
        if self.frame is None:
            self.frame = self.base.copy()
        else:
            self.frame.paste(self.base, (0, 0))
        self._d = ImageDraw.Draw(self.frame, "RGBA")
        try:
            self._draw_numbers(snap)
            self._draw_strips(snap)
            self._draw_gauges(snap)
            self._draw_battery(snap)
        except Exception as exc:                       # pragma: no cover
            self._d.text((10 * self.k, 10 * self.k), f"render error: {exc}",
                         font=T.font(14 * self.k), fill=(255, 0, 0, 255))
        if self.k != 1:
            return self.frame.resize((self.W, self.H), Image.LANCZOS)
        return self.frame

    # ---------------------------------------------------------------- 读数
    def _draw_numbers(self, snap):
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        self._label(CY + LBL_TOP_DY, "C P U   L O A D")
        self._label(CY + LBL_BOT_DY, "G P U   L O A D")
        self._reading(CY + NUM_TOP_DY, cpu.get("load"))
        self._reading(CY + NUM_BOT_DY, gpu.get("load"))

    def _label(self, cy, text):
        k = self.k
        f = T.font(15 * k, "bold")
        x, y = CX * k, cy * k
        self._d.text((x + 1.5 * k, y + 1.5 * k), text, font=f, fill=(0, 0, 0, 215), anchor="mm")
        self._d.text((x, y), text, font=f, fill=(206, 216, 228, 248), anchor="mm")

    def _reading(self, cy, value):
        k = self.k
        d = self._d
        txt = "—" if value is None else f"{value:.0f}"
        if value is None:
            col = (152, 160, 170)
        elif value >= 92:
            col = (255, 108, 96)
        elif value >= 78:
            col = (255, 198, 94)
        else:
            col = WHITE
        f = T.font(92 * k, "bold", condensed=False)
        step = f.getlength("0") + 3 * k
        x = CX * k - (step * len(txt) - 3 * k) / 2.0
        for ch in txt:
            d.text((x + 2.4 * k, cy * k + 2.4 * k), ch, font=f, fill=(0, 0, 0, 220), anchor="lm")
            d.text((x - 1.2 * k, cy * k - 1.2 * k), ch, font=f, fill=(255, 255, 255, 48),
                   anchor="lm")
            d.text((x, cy * k), ch, font=f, fill=col + (255,), anchor="lm")
            x += step

    # ---------------------------------------------------------------- 双状态条
    def _draw_strips(self, snap):
        cpu = snap.get("cpu") or {}
        f0, f1 = CY + STRIP_FREQ_DY[0], CY + STRIP_FREQ_DY[1]
        self._vents(f0, f1)
        self._strip((CX - STRIP_FREQ_HW, f0, CX + STRIP_FREQ_HW, f1),
                    "F R E Q    " + self._freq(cpu), GRAY, dim=True)
        s0, s1 = CY + STRIP_STATE_DY[0], CY + STRIP_STATE_DY[1]
        state, col, deep = self._state(cpu.get("temp"))
        self._strip((CX - STRIP_STATE_HW, s0, CX + STRIP_STATE_HW, s1),
                    state, col, dim=False, deep=deep)

    def _vents(self, y0, y1):
        k = self.k
        d = self._d
        for side in (-1, 1):
            base = (CX - STRIP_FREQ_HW - 16) if side < 0 else (CX + STRIP_FREQ_HW + 16)
            for i in range(4):
                xx = base + side * i * 7
                d.line([((xx - side * 6) * k, y0 * k), ((xx + side * 3) * k, y1 * k)],
                       fill=(128, 140, 154, 155), width=max(1, k))

    @staticmethod
    def _freq(cpu):
        clk = cpu.get("clock") or 0
        return f"{clk / 1000.0:.2f} GHz" if clk else "—"

    @staticmethod
    def _state(temp):
        if temp is None:
            return "L O C K E D", (152, 160, 170), (44, 48, 54)
        if temp < 55:
            return "C O O L", (110, 214, 255), (14, 62, 92)
        if temp < 70:
            return "N O R M A L", GREEN_HI, GREEN_LO
        if temp < 82:
            return "W A R M", (255, 198, 94), (98, 64, 12)
        return "H O T", (255, 96, 84), (104, 24, 20)

    def _strip(self, box, text, col, dim=False, deep=None):
        k = self.k
        d = self._d
        x0, y0, x1, y1 = box
        w, h = int((x1 - x0) * k), int((y1 - y0) * k)
        if not dim:
            d.rounded_rectangle(self.B([x0 - 6, y0 - 6, x1 + 6, y1 + 6]), radius=11 * k,
                                fill=col + (30,))
            d.rounded_rectangle(self.B([x0 - 3, y0 - 3, x1 + 3, y1 + 3]), radius=9 * k,
                                fill=col + (44,))
        top = (62, 68, 76) if dim else T.lerp_color(deep or (10, 40, 20), col, 0.24)
        bot = (20, 24, 29) if dim else T.lerp_color((5, 9, 11), col, 0.08)
        self.frame.paste(T.vgradient((w, h), top, bot), (int(x0 * k), int(y0 * k)),
                         T.rounded_mask((w, h), 7 * k))
        d.rounded_rectangle(self.B(box), radius=7 * k,
                            outline=(col + (110,)) if dim else (col + (240,)), width=2 * k)
        f = T.font(17 * k, "bold", condensed=False)
        mx, my = ((x0 + x1) / 2) * k, ((y0 + y1) / 2) * k
        if dim:
            d.text((mx, my), text, font=f, fill=col + (240,), anchor="mm")
        else:
            d.text((mx + 1.4 * k, my + 1.4 * k), text, font=f, fill=(0, 0, 0, 200), anchor="mm")
            d.text((mx, my), text, font=f,
                   fill=T.lerp_color(col, (255, 255, 255), 0.35) + (255,), anchor="mm")

    # ---------------------------------------------------------------- LED 表
    def _draw_gauges(self, snap):
        mem = snap.get("mem") or {}
        gpu = snap.get("gpu") or {}
        self._gauge("left", (mem.get("pct") or 0) / 100.0)
        self._gauge("right", (gpu.get("vram_pct") or 0) / 100.0)

    def _gauge(self, side, ratio):
        """亮起的格子一定是从下往上连续的一段，所以直接裁剪预渲染图层再贴回，
        每帧只有一次 crop + 一次 alpha_composite。"""
        k = self.k
        ratio = T.clamp(ratio)
        lit = int(round(ratio * GAUGE_BANDS))
        if lit <= 0:
            return
        palette = 0 if ratio <= 0.80 else (1 if ratio <= 0.92 else 2)
        layer = self.lit_layers[side][palette]
        bx0, by0, _, _ = self.gauge_box[side]
        ys = self.band_ys[side]
        y_from = ys[GAUGE_BANDS - lit][0]
        y_to = ys[-1][1]
        c0 = max(0, int(y_from * k) - by0 - 2)
        c1 = min(layer.height, int(y_to * k) - by0 + 2)
        if c1 <= c0:
            return
        piece = layer.crop((0, c0, layer.width, c1))
        self.frame.alpha_composite(piece, (bx0, by0 + c0))

    # ---------------------------------------------------------------- 电池
    def _draw_battery(self, snap):
        cpu = snap.get("cpu") or {}
        k = self.k
        d = self._d
        x0, y0, x1, y1 = BATTERY
        temp = cpu.get("temp")
        ratio = T.clamp(((temp or TEMP_LO) - TEMP_LO) / (TEMP_HI - TEMP_LO)) if temp else 0.0

        ix0, ix1 = x0 + 5, x1 - 5
        iy0, iy1 = y0 + 5, y1 - 5
        d.rounded_rectangle(self.B([ix0, iy0, ix1, iy1]), radius=5 * k, fill=(11, 15, 20, 255))
        if ratio > 0.01:
            fx1 = ix0 + (ix1 - ix0) * ratio
            if fx1 - ix0 < 12:
                fx1 = ix0 + 12
            w, h = int((fx1 - ix0) * k), int((iy1 - iy0) * k)
            self.frame.paste(T.hgradient((w, h), (24, 118, 190), (128, 236, 250)),
                             (int(ix0 * k), int(iy0 * k)), T.rounded_mask((w, h), 5 * k))

        self._bolt((x0 + x1) / 2.0, (y0 + y1) / 2.0)

        tx, ty = TEMP_TEXT
        txt = "—" if temp is None else f"{temp:.1f}"
        f = T.font(26 * k, "bold", condensed=False)
        d.text((tx * k + 1.6 * k, ty * k + 1.6 * k), txt, font=f, fill=(0, 0, 0, 205), anchor="lm")
        d.text((tx * k, ty * k), txt, font=f,
               fill=(T.temp_color(temp) if temp else (152, 160, 170)) + (255,), anchor="lm")
        d.text((tx * k + f.getlength(txt) + 6 * k, ty * k + 4 * k), "°C",
               font=T.font(15 * k, "bold", condensed=False),
               fill=(162, 172, 184, 255), anchor="lm")

    def _bolt(self, cx, cy):
        k = self.k
        h = 9.0
        pts = [(0.10, -1.00), (-0.46, 0.06), (-0.02, 0.06), (-0.18, 1.00),
               (0.46, -0.16), (0.02, -0.16), (0.28, -1.00)]
        self._d.polygon([(int(cx * k + px * h * k), int(cy * k + py * h * k))
                         for px, py in pts], fill=(8, 14, 22, 235))


GAUGE_ANG_BANDS_DIV = float(GAUGE_BANDS)
