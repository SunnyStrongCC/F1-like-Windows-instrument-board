# -*- coding: utf-8 -*-
"""F1 Halo 风格遥测面板渲染器。

整体版式对照 F1 转播的车载遥测叠加：

    ┌ 顶栏：F1 徽标 / 标题 / LIVE / 时钟 ─────────────────────────┐
    │ ┌ 计时塔 ─┐ ┌ 方向盘中央显示区 ─────────────────────────┐ │
    │ │ P1..P12 │ │ 换挡灯带 + CPU/GPU 竖条 + 大号读数        │ │
    │ │ 进程排行│ │ + 显存/温度卡片 + 八核 LED               │ │
    │ └─────────┘ └───────────────────────────────────────────┘ │
    │             ┌ 数据卡片：CPU GPU 内存 显存 磁盘 网络 ─────┐ │
    ├ 底栏：硬件型号 / 权限状态 / 运行时长 ─────────────────────┤
    └───────────────────────────────────────────────────────────┘

静态层（面板、边框、标签）用 3 倍超采样渲染一次并缓存，动态层每帧只画
数字、色条和 LED，保证在 10 FPS 下依然顺滑。
"""
from __future__ import annotations

import datetime
import math
import os
import time

from PIL import Image, ImageDraw

from . import theme as T

BASE_W = 1360
BASE_H = 760
SS = 3                      # 静态层超采样倍率

# ---- 版式常量（基于 1360x760 逻辑画布）
PAD = 14
HEADER = (PAD, PAD, BASE_W - PAD, PAD + 54)
BODY_TOP = HEADER[3] + 10
FOOTER_H = 40
BODY_BOTTOM = BASE_H - PAD - FOOTER_H - 8
TOWER = (PAD, BODY_TOP, PAD + 424, BODY_BOTTOM)
RX0 = TOWER[2] + 12
RX1 = BASE_W - PAD
DASH = (RX0, BODY_TOP, RX1, BODY_TOP + 410)
CHIPS = (RX0, DASH[3] + 12, RX1, BODY_BOTTOM)
FOOTER = (PAD, BASE_H - PAD - FOOTER_H, BASE_W - PAD, BASE_H - PAD)

TOWER_ROWS = 12
SHIFT_LEDS = 15
CORE_CELLS = 8


def _s(v) -> str:
    return "—" if v is None else str(v)


class HudRenderer:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.W = BASE_W
        self.H = BASE_H
        self.hud_mode = bool(cfg.get("mode", "window") == "hud")
        self.key_color = (1, 2, 3)
        self.show_procs = bool(cfg.get("show_processes", True))
        self.lang = cfg.get("language", "zh")
        self._rounded_cache: dict = {}
        self._rot_cache: dict = {}
        self.base = None
        self.frame = None
        self._build_static()

    # ==================================================================
    # 静态层
    # ==================================================================
    def _build_static(self):
        bg = self.key_color if self.hud_mode else T.BG
        img = Image.new("RGBA", (BASE_W * SS, BASE_H * SS), bg + (255,))
        d = ImageDraw.Draw(img)
        s = SS

        def B(box):
            return [v * s for v in box]

        # ---- 全局底纹：细微网格 + 暗角，增强“转播图形”质感
        if not self.hud_mode:
            for x in range(0, BASE_W, 40):
                d.line([(x * s, 0), (x * s, BASE_H * s)], fill=(16, 20, 26, 255), width=1)
            for y in range(0, BASE_H, 40):
                d.line([(0, y * s), (BASE_W * s, y * s)], fill=(16, 20, 26, 255), width=1)

        self._panel(img, HEADER, 10, T.CARBON_1, T.CARBON_0, edge=T.EDGE_SOFT)
        self._panel(img, TOWER, 12, T.CARBON_1, T.CARBON_0, edge=T.EDGE_SOFT)
        self._panel(img, DASH, 14, T.CARBON_1, T.CARBON_0, edge=T.TEAL_DEEP, edge_w=2)
        self._panel(img, CHIPS, 12, T.CARBON_1, T.CARBON_0, edge=T.EDGE_SOFT)
        self._panel(img, FOOTER, 10, T.CARBON_1, T.CARBON_0, edge=T.EDGE_SOFT)

        # ---- 顶栏
        bx0, by0, bx1, by1 = HEADER
        badge = (bx0 + 14, by0 + 10, bx0 + 14 + 62, by1 - 10)
        self._panel(img, badge, 8, T.RED, (150, 4, 0), edge=(255, 90, 80), edge_w=2)
        d.text(((badge[0] + badge[2]) / 2 * s, (badge[1] + badge[3]) / 2 * s),
               "F1", font=T.font(30 * s, "bold"), fill=T.WHITE + (255,), anchor="mm")
        d.text(((badge[2] + 16) * s, (by0 + 13) * s), "PC TELEMETRY",
               font=T.font(26 * s, "bold"), fill=T.WHITE + (255,), anchor="la")
        d.text(((badge[2] + 17) * s, (by0 + 40) * s), "硬件实时遥测 · 仿 F1 方向盘叠加界面",
               font=T.font_cjk(15 * s), fill=T.MUTED + (255,), anchor="la")

        d.line([(BASE_W - PAD - 486) * s, (by0 + 10) * s,
                (BASE_W - PAD - 486) * s, (by1 - 10) * s], fill=T.EDGE_SOFT + (255,), width=2 * s)

        # 右侧三列信息（时钟 / 运行时长 / 传感器状态），列宽 140 保证不越界
        live_x = BASE_W - PAD - 466
        self.header_cols = {
            "live": live_x,
            "clock": live_x + 88,
            "uptime": live_x + 222,
            "sensor": live_x + 356,
        }
        for key, label in (("clock", "CLOCK"), ("uptime", "UPTIME"), ("sensor", "SENSOR")):
            d.text(((self.header_cols[key] + 4) * s, (by0 + 13) * s), label,
                   font=T.font(14 * s), fill=T.MUTED + (255,), anchor="la")

        # ---- 计时塔表头
        tx0, ty0, tx1, ty1 = TOWER
        d.rectangle(B([tx0 + 2, ty0 + 6, tx0 + 6, ty0 + 46]), fill=T.RED + (255,))
        d.text(((tx0 + 18) * s, (ty0 + 12) * s), "TOP PROCESSES",
               font=T.font(24 * s, "bold"), fill=T.WHITE + (255,), anchor="la")
        d.text(((tx0 + 19) * s, (ty0 + 38) * s), "进程排行 · 按 CPU 占用",
               font=T.font_cjk(14 * s), fill=T.MUTED + (255,), anchor="la")
        d.text(((tx1 - 20) * s, (ty0 + 14) * s), "CPU", font=T.font(17 * s, "bold"),
               fill=T.SILVER + (255,), anchor="ra")
        d.text(((tx1 - 20) * s, (ty0 + 36) * s), "MEM", font=T.font(15 * s),
               fill=T.MUTED + (255,), anchor="ra")
        d.line([(tx0 + 14) * s, (ty0 + 62) * s, (tx1 - 14) * s, (ty0 + 62) * s],
               fill=T.EDGE + (255,), width=2 * s)

        rows_top = ty0 + 70
        rows_h = (ty1 - rows_top - 8) / float(TOWER_ROWS)
        self.row_h = rows_h
        self.rows_top = rows_top
        for i in range(TOWER_ROWS):
            ry0 = rows_top + i * rows_h
            ry1 = ry0 + rows_h - 4
            if i % 2 == 0:
                d.rectangle(B([tx0 + 10, ry0, tx1 - 10, ry1]), fill=(20, 25, 32, 190))
            color = T.TEAM_COLORS[i % len(T.TEAM_COLORS)]
            d.rectangle(B([tx0 + 10, ry0, tx0 + 15, ry1]), fill=color + (255,))
            pos_box = (tx0 + 22, ry0 + (rows_h - 4) * 0.18, tx0 + 22 + 40, ry1 - (rows_h - 4) * 0.18)
            d.rounded_rectangle(B(pos_box), radius=5 * s, fill=(34, 41, 50, 255))
            d.text(((pos_box[0] + pos_box[2]) / 2 * s, (pos_box[1] + pos_box[3]) / 2 * s),
                   str(i + 1), font=T.font(int(rows_h * 0.44) * s, "bold"),
                   fill=T.WHITE + (255,), anchor="mm")

        # ---- 方向盘区域：顶部换挡灯带
        dx0, dy0, dx1, dy1 = DASH
        d.text(((dx0 + 22) * s, (dy0 + 14) * s), "SHIFT", font=T.font(15 * s, "bold"),
               fill=T.MUTED + (255,), anchor="la")
        d.text(((dx1 - 22) * s, (dy0 + 14) * s), "CPU + GPU LOAD", font=T.font(15 * s, "bold"),
               fill=T.MUTED + (255,), anchor="ra")
        led_w, led_h, led_gap = 22, 13, 6
        total = SHIFT_LEDS * led_w + (SHIFT_LEDS - 1) * led_gap
        led_x0 = dx0 + (dx1 - dx0 - total) / 2
        led_y0 = dy0 + 30
        self.shift_geom = (led_x0, led_y0, led_w, led_h, led_gap)
        for i in range(SHIFT_LEDS):
            x = led_x0 + i * (led_w + led_gap)
            d.rounded_rectangle(B([x, led_y0, x + led_w, led_y0 + led_h]),
                                radius=3 * s, fill=(28, 33, 40, 255), outline=(44, 52, 62, 255),
                                width=1 * s)

        # ---- 方向盘区域：中央显示模块背景
        cx0, cy0, cx1, cy1 = self.center_box()
        self._panel(img, (cx0, cy0, cx1, cy1), 20, (14, 18, 23), (6, 8, 11),
                    edge=T.TEAL_DEEP, edge_w=2)
        d.rounded_rectangle(B([cx0 + 8, cy0 + 8, cx1 - 8, cy1 - 8]), radius=14 * s,
                            outline=(24, 62, 58, 255), width=1 * s)
        d.text((cx0 + 22, cy0 + 14), "", font=T.font(10 * s), fill=T.WHITE + (255,))

        # 顶部 / 底部装饰角标（模仿 F1 图形里的切角）
        for px, py, dirx, diry in ((cx0 + 8, cy0 + 8, 1, 1), (cx1 - 8, cy0 + 8, -1, 1),
                                   (cx0 + 8, cy1 - 8, 1, -1), (cx1 - 8, cy1 - 8, -1, -1)):
            d.line([(px * s, py * s), ((px + 22 * dirx) * s, py * s)], fill=T.TEAL + (140,), width=3 * s)
            d.line([(px * s, py * s), (px * s, (py + 22 * diry) * s)], fill=T.TEAL + (140,), width=3 * s)

        # 中央模块内的静态标签
        self._label(d, img, "CPU LOAD", (cx0 + 34, cy0 + 18), 20, T.MUTED, T.SILVER, anchor="la")
        self._label(d, img, "使用率", (cx1 - 34, cy0 + 20), 16, T.MUTED, T.MUTED, anchor="ra", cjk=True)

        # 底部信息条（温度 / 频率 / 功耗），三格等宽
        strip = self.info_strip_box()
        self._panel(img, strip, 10, (20, 25, 31), (12, 15, 19), edge=(38, 47, 57), edge_w=1)
        sx0, sy0, sx1, sy1 = strip
        cw = (sx1 - sx0) / 3.0
        for i in range(1, 3):
            d.line([((sx0 + i * cw) * s, (sy0 + 7) * s), ((sx0 + i * cw) * s, (sy1 - 7) * s)],
                   fill=(38, 47, 57, 255), width=1 * s)
        self.strip_cells = [(sx0 + i * cw, sy0, sx0 + (i + 1) * cw, sy1) for i in range(3)]

        # ---- 左右竖条外框
        for box, label in ((self.bar_box_left(), "CPU"), (self.bar_box_right(), "GPU")):
            bx0, by0, bx1, by1 = box
            d.rounded_rectangle(B(box), radius=8 * s, fill=(9, 11, 14, 255),
                                outline=(40, 49, 59, 255), width=2 * s)
            self._rot_text(img, label, (bx0 - 8, (by0 + by1) / 2), 17, T.SILVER, 90)

        # ---- 右侧卡片列
        for box, title, cjk in zip(self.side_card_boxes(),
                                   ("显存 VRAM", "GPU 温度", "内存 RAM"),
                                   (True, True, True)):
            self._panel(img, box, 10, (20, 25, 31), (12, 15, 19), edge=(38, 47, 57), edge_w=1)
            self._label(d, img, title, (box[0] + 10, box[1] + 6), 13, T.MUTED, T.MUTED,
                        anchor="la", cjk=cjk)

        # ---- 底部八核 GEARS 区
        gy0 = dy1 - 100
        self._label(d, img, "GEARS", (dx0 + 24, gy0 + 4), 18, T.MUTED, T.SILVER, anchor="la")
        self._label(d, img, "八核心负载", (dx0 + 24, gy0 + 28), 14, T.MUTED, T.MUTED,
                    anchor="la", cjk=True)
        self.core_boxes = []
        gx0 = dx0 + 130
        gx1 = dx1 - 24
        n = CORE_CELLS
        gap = 8
        cw = (gx1 - gx0 - (n - 1) * gap) / float(n)
        for i in range(n):
            x = gx0 + i * (cw + gap)
            box = (x, gy0 + 4, x + cw, gy0 + 72)
            self.core_boxes.append(box)
            d.rounded_rectangle(B(box), radius=8 * s, fill=(16, 20, 26, 255),
                                outline=(40, 49, 59, 255), width=2 * s)
            d.text(((box[0] + box[2]) / 2 * s, (box[1] + 16) * s), str(i + 1),
                   font=T.font(20 * s, "bold"), fill=T.MUTED + (255,), anchor="mm")

        # ---- 数据卡片静态部分
        self.chip_boxes = []
        cx = CHIPS[0] + 10
        chip_gap = 9
        n = 6
        cwid = (CHIPS[2] - CHIPS[0] - 20 - (n - 1) * chip_gap) / float(n)
        for i in range(n):
            box = (cx + i * (cwid + chip_gap), CHIPS[1] + 10,
                   cx + i * (cwid + chip_gap) + cwid, CHIPS[3] - 10)
            self.chip_boxes.append(box)
            self._panel(img, box, 10, (18, 23, 29), (10, 13, 17), edge=(36, 45, 55), edge_w=1)

        for box, title, cjk in zip(
            self.chip_boxes,
            ("CPU 处理器", "GPU 显卡", "内存 MEMORY", "显存 VRAM", "磁盘 STORAGE", "网络 NETWORK"),
            (True, True, True, True, True, True),
        ):
            self._label(d, img, title, (box[0] + 12, box[1] + 8), 15, T.MUTED, T.SILVER,
                        anchor="la", cjk=cjk)
            d.rectangle(B([box[0] + 12, box[1] + 28, box[2] - 12, box[1] + 30]),
                        fill=(40, 49, 59, 255))

        # ---- 底栏
        fx0, fy0, fx1, fy1 = FOOTER
        d.rectangle(B([fx0 + 12, fy0 + 11, fx0 + 16, fy1 - 11]), fill=T.TEAL + (255,))

        self.base = img.resize((BASE_W, BASE_H), Image.LANCZOS)
        self.frame = self.base.copy()

    # ---------------------------------------------------------------- 静态辅助
    def _panel(self, img, box, radius, top_color, bottom_color, edge=None, edge_w=2):
        s = SS
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        w, h = x1 - x0, y1 - y0
        if w <= 2 or h <= 2:
            return
        grad = T.vgradient((w * s, h * s), top_color, bottom_color)
        mask = T.rounded_mask((w * s, h * s), radius * s)
        img.paste(grad, (x0 * s, y0 * s), mask)
        if edge:
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([x0 * s, y0 * s, x1 * s - 1, y1 * s - 1],
                                radius=radius * s, outline=edge + (255,), width=max(1, edge_w) * s)

    def _label(self, d, img, text, xy, size, color, shadow_color, anchor="la", cjk=False):
        s = SS
        f = T.font_cjk(size * s) if cjk else T.font(size * s, "semi")
        x, y = xy[0] * s, xy[1] * s
        d.text((x, y), text, font=f, fill=shadow_color + (255,), anchor=anchor)
        d.text((x, y), text, font=f, fill=color + (255,), anchor=anchor)

    def _rot_text(self, img, text, xy, size, color, angle):
        key = (text, size, color, angle)
        surf = self._rot_cache.get(key)
        if surf is None:
            f = T.font(size * SS, "semi")
            tmp = Image.new("RGBA", (int(f.getlength(text)) + 8 * SS, size * SS + 10 * SS), (0, 0, 0, 0))
            td = ImageDraw.Draw(tmp)
            td.text((4 * SS, 5 * SS), text, font=f, fill=color + (255,))
            surf = tmp.rotate(angle, expand=True, resample=Image.BICUBIC)
            self._rot_cache[key] = surf
        px = int(xy[0] * SS - surf.width / 2)
        py = int(xy[1] * SS - surf.height / 2)
        img.alpha_composite(surf, (px, py))

    # ---------------------------------------------------------------- 几何
    def center_box(self):
        dx0, dy0, dx1, dy1 = DASH
        lb = self.bar_box_left()
        rb = self.bar_box_right()
        side_w = 150
        return (lb[2] + 18, dy0 + 56, rb[0] - side_w - 30, dy1 - 104)

    def bar_box_left(self):
        dx0, dy0, dx1, dy1 = DASH
        return (dx0 + 52, dy0 + 60, dx0 + 52 + 62, dy1 - 150)

    def bar_box_right(self):
        dx0, dy0, dx1, dy1 = DASH
        return (dx1 - 52 - 62, dy0 + 60, dx1 - 52, dy1 - 150)

    def side_card_boxes(self):
        rb = self.bar_box_right()
        cx1, cy0, cx2, cy1 = self.center_box()
        x0 = cx2 + 14
        x1 = rb[0] - 14
        h = (cy1 - cy0 - 16) / 3.0
        out = []
        for i in range(3):
            y0 = cy0 + i * (h + 8)
            out.append((x0, y0, x1, y0 + h))
        return out

    def sub_card_boxes(self):
        cx0, cy0, cx1, cy1 = self.center_box()
        h = 76
        y1 = cy1 - 18
        y0 = y1 - h
        w = (cx1 - cx0 - 40 - 16) / 2.0
        return [(cx0 + 20, y0, cx0 + 20 + w, y1),
                (cx0 + 20 + w + 16, y0, cx0 + 20 + w + 16 + w, y1)]

    def big_number_box(self):
        cx0, cy0, cx1, cy1 = self.center_box()
        return (cx0 + 20, cy0 + 34, cx1 - 20, cy1 - 92)

    def pill_box(self):
        cx0, cy0, cx1, cy1 = self.center_box()
        w, h = 176, 34
        y1 = cy1 - 46
        return (cx0 + 24, y1 - h, cx0 + 24 + w, y1)

    def info_strip_box(self):
        """中央模块底部的温度 / 频率 / 功耗信息条（替代原来的两个子卡片）。"""
        cx0, cy0, cx1, cy1 = self.center_box()
        return (cx0 + 20, cy1 - 40, cx1 - 20, cy1 - 8)

    # ==================================================================
    # 每帧渲染
    # ==================================================================
    def render(self, snap: dict) -> Image.Image:
        if self.frame is None:
            self.frame = self.base.copy()
        else:
            self.frame.paste(self.base, (0, 0))
        # 用 "RGBA" 模式绘图才会做 alpha 混合；默认模式是直接覆盖像素，
        # 半透明填充会在不透明底图上打出透明的洞。
        self._dyn = ImageDraw.Draw(self.frame, "RGBA")
        try:
            self._draw_header(snap)
            if self.show_procs:
                self._draw_tower(snap)
            self._draw_dash(snap)
            self._draw_chips(snap)
            self._draw_footer(snap)
        except Exception as exc:                      # pragma: no cover
            self._dyn.text((20, 20), f"render error: {exc}", font=T.font(16), fill=T.RED + (255,))
        return self.frame

    # ---------------------------------------------------------------- 顶栏
    def _draw_header(self, snap):
        bx0, by0, bx1, by1 = HEADER
        cols = self.header_cols
        pulse = 0.55 + 0.45 * abs(math.sin(time.time() * 2.2))
        r = 7
        cx, cy = cols["live"] + 10, by0 + 27
        glow = int(90 * pulse)
        self._dyn.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4],
                          fill=(225, 6, 0, glow))
        self._dyn.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 40, 30, 255))
        self._text(cols["live"] + 22, by0 + 27, "LIVE", 22, T.WHITE, "bold", anchor="lm")

        now = datetime.datetime.now()
        self._text(cols["clock"] + 4, by0 + 32, now.strftime("%H:%M:%S"), 22, T.WHITE, "bold",
                   anchor="la", mono=True)
        up = int(snap.get("uptime") or 0)
        up_s = f"{up // 3600:02d}:{(up % 3600) // 60:02d}:{up % 60:02d}"
        self._text(cols["uptime"] + 4, by0 + 32, up_s, 22, T.WHITE, "bold", anchor="la", mono=True)

        lhm = snap.get("lhm")
        cpu = snap.get("cpu") or {}
        if not lhm:
            dot, txt, col = "●", "已离线", T.RED
        elif not cpu.get("temp"):
            dot, txt, col = "●", "部分受限", T.AMBER
        else:
            dot, txt, col = "●", "全部在线", T.GREEN
        self._text(cols["sensor"] + 4, by0 + 32, f"{dot} {txt}", 19, col, "bold",
                   anchor="la", cjk=True)

    # ---------------------------------------------------------------- 计时塔
    def _draw_tower(self, snap):
        tx0, ty0, tx1, ty1 = TOWER
        procs = snap.get("procs") or []
        rows_top = self.rows_top
        rh = self.row_h
        max_cpu = 1.0
        for p in procs:
            max_cpu = max(max_cpu, p.get("cpu") or 0.0)
        max_cpu = max(max_cpu, 10.0)

        for i in range(TOWER_ROWS):
            ry0 = rows_top + i * rh
            ry1 = ry0 + rh - 4
            if i >= len(procs):
                continue
            p = procs[i]
            color = T.TEAM_COLORS[i % len(T.TEAM_COLORS)]
            # 占用比例条
            frac = T.clamp((p.get("cpu") or 0.0) / max_cpu)
            bar_w = int((tx1 - 10 - (tx0 + 74)) * frac)
            if bar_w > 2:
                self._dyn.rectangle([tx0 + 74, ry1 - 4, tx0 + 74 + bar_w, ry1 - 1],
                                    fill=color + (170,))
            name = p.get("name") or "?"
            cnt = p.get("count") or 1
            label = name if cnt <= 1 else f"{name} ×{cnt}"
            f = T.fit_font(label, 218, int(rh * 0.42), "semi", True)
            ty = ry0 + (rh - 4) / 2
            self._text(tx0 + 74, ty, label, 0, T.WHITE, "semi", anchor="lm", font=f)

            cpu = p.get("cpu") or 0.0
            self._text(tx1 - 20, ty - 1, f"{cpu:.1f}", 22, T.heat(min(1.0, cpu / 40.0)), "bold",
                       anchor="rm", mono=True)
            mem = (p.get("mem_mb") or 0.0) / 1024.0
            self._text(tx1 - 20, ty + 15, f"{mem:.2f}G", 13, T.MUTED, "regular", anchor="rm", mono=True)

    # ---------------------------------------------------------------- 方向盘
    def _draw_dash(self, snap):
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        mem = snap.get("mem") or {}

        # --- 换挡灯带
        led_x0, led_y0, led_w, led_h, led_gap = self.shift_geom
        load = max(cpu.get("load") or 0.0, gpu.get("load") or 0.0)
        lit = int(round(load / 100.0 * SHIFT_LEDS))
        for i in range(SHIFT_LEDS):
            x = led_x0 + i * (led_w + led_gap)
            if i < 5:
                col = (60, 235, 110)
            elif i < 10:
                col = (255, 60, 45)
            else:
                col = (176, 38, 255)
            if i < lit:
                self._dyn.rounded_rectangle([x, led_y0, x + led_w, led_y0 + led_h],
                                            radius=3, fill=col + (255,))
                self._dyn.rounded_rectangle([x - 1, led_y0 - 1, x + led_w + 1, led_y0 + led_h + 1],
                                            radius=4, outline=col + (70,), width=1)
            else:
                self._dyn.rounded_rectangle([x + 2, led_y0 + 2, x + led_w - 2, led_y0 + led_h - 2],
                                            radius=2, fill=(30, 36, 44, 255))

        # --- 左右竖条
        self._draw_vbar(self.bar_box_left(), cpu.get("load"), T.TEAL)
        self._draw_vbar(self.bar_box_right(), gpu.get("load"), T.CYAN)
        lb = self.bar_box_left()
        rb = self.bar_box_right()
        self._text((lb[0] + lb[2]) / 2, lb[3] + 12, self._pct(cpu.get("load")), 24,
                   T.heat(T.clamp((cpu.get("load") or 0) / 100.0)), "bold", anchor="ma", mono=True)
        self._text((rb[0] + rb[2]) / 2, rb[3] + 12, self._pct(gpu.get("load")), 24,
                   T.heat(T.clamp((gpu.get("load") or 0) / 100.0)), "bold", anchor="ma", mono=True)

        # --- 中央大号读数（CPU 使用率）
        bn = self.big_number_box()
        load = cpu.get("load") or 0.0
        big = f"{load:.0f}"
        f_big = T.font(158, "bold")
        f_unit = T.font(52, "bold")
        w_big = f_big.getlength(big)
        w_unit = f_unit.getlength("%")
        total = w_big + w_unit + 6
        bx = (bn[0] + bn[2]) / 2 - total / 2
        by = bn[1] + (bn[3] - bn[1]) * 0.52
        col = T.heat(T.clamp(load / 100.0))
        self.frame.alpha_composite(self._glow(big, f_big, col), (int(bx) - 6, int(by) - 4))
        self._dyn.text((bx, by), big, font=f_big, fill=col + (255,), anchor="lm")
        self._dyn.text((bx + w_big + 6, by + 12), "%", font=f_unit, fill=col + (150,), anchor="lm")

        # --- 状态药丸（对应 F1 的 BOOST / OVRTAKE）
        pb = self.pill_box()
        temp = cpu.get("temp")
        if temp is None:
            state, scol = "待解锁", T.MUTED
        elif temp < 55:
            state, scol = "COOL", T.GREEN
        elif temp < 70:
            state, scol = "NORMAL", T.TEAL
        elif temp < 82:
            state, scol = "WARM", T.YELLOW
        else:
            state, scol = "HOT", T.RED
        self._dyn.rounded_rectangle(list(pb), radius=8,
                                    fill=T.lerp_color(T.CARBON_0, scol, 0.30) + (255,),
                                    outline=scol + (210,), width=2)
        text_col = scol if scol != T.MUTED else T.SILVER
        self._text((pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2 + 1, state, 20, text_col, "bold",
                   anchor="mm", cjk=True)

        # --- 底部信息条：温度 / 频率 / 功耗（三格，标签用中文短词，数值右对齐，互不压字）
        cells = getattr(self, "strip_cells", None)
        if cells:
            temp_txt = self._fmt(cpu.get("temp"), 1, "°C")
            freq_txt = self._fmt((cpu.get("clock") or 0) / 1000.0 if cpu.get("clock") else None,
                                 2, "GHz")
            power_txt = self._fmt(cpu.get("power"), 0, "W")
            for box, label, value, col in zip(
                cells,
                ("温度", "频率", "功耗"),
                (temp_txt, freq_txt, power_txt),
                (T.temp_color(cpu.get("temp")), T.TEAL if cpu.get("clock") else T.MUTED,
                 T.SILVER),
            ):
                b0, b1, b2, b3 = box
                self._text(b0 + 12, b1 + 7, label, 12, T.MUTED, "regular", anchor="la", cjk=True)
                self._text(b2 - 12, b1 + 4, value, 19, col, "bold", anchor="ra", mono=True)

        # --- 右侧卡片
        cards = self.side_card_boxes()
        vram_pct = gpu.get("vram_pct")
        self._mini_stat(cards[0], self._pct(vram_pct), self._vram_text(gpu),
                        T.heat(T.clamp((vram_pct or 0) / 100.0)), vram_pct)
        gt = gpu.get("temp")
        self._mini_stat(cards[1], self._num(gt, 0), f"风扇 {self._num(gpu.get('fan'), 0)} RPM",
                        T.temp_color(gt), (gt or 0) / 100.0)
        self._mini_stat(cards[2], self._pct(mem.get("pct")),
                        f"{self._num(mem.get('used_gb'),1)} / {self._num(mem.get('total_gb'),1)} GB",
                        T.heat(T.clamp((mem.get("pct") or 0) / 100.0)), mem.get("pct"))

        # --- 八核 LED
        per = cpu.get("per_core") or []
        for i, box in enumerate(self.core_boxes):
            v = per[i] if i < len(per) else None
            ratio = T.clamp((v or 0) / 100.0)
            col = T.heat(ratio)
            ix0, ix1 = box[0] + 6, box[2] - 6
            bar_top = box[1] + 32
            bar_bot = box[3] - 8
            self._dyn.rectangle([ix0, bar_top, ix1, bar_bot], fill=(22, 27, 34, 255))
            hh = int((bar_bot - bar_top) * ratio)
            if hh > 1:
                self._dyn.rectangle([ix0, bar_bot - hh, ix1, bar_bot], fill=col + (255,))
            self._text((box[0] + box[2]) / 2, box[1] + 16,
                       f"{v:.0f}" if v is not None else "—", 20, col if v else T.DIM,
                       "bold", anchor="mm")

        # 硬件型号放在底栏，中央模块底部留白，避免与 GEARS 挤在一起

    def _mini_stat(self, box, value, sub2, color, ratio):
        x0, y0, x1, y1 = box
        self._text(x0 + 10, y0 + 24, value, 26, color, "bold", anchor="la", mono=True)
        self._text(x0 + 10, y0 + 52, sub2, 12, T.SILVER, "semi", anchor="la", mono=True)
        bar_y = y1 - 9
        self._dyn.rectangle([x0 + 10, bar_y, x1 - 10, bar_y + 5], fill=(30, 36, 44, 255))
        w = int((x1 - 10 - (x0 + 10)) * T.clamp((ratio or 0) / 100.0))
        if w > 1:
            self._dyn.rectangle([x0 + 10, bar_y, x0 + 10 + w, bar_y + 5], fill=color + (255,))

    def _draw_vbar(self, box, value, accent):
        x0, y0, x1, y1 = box
        n = 26
        gap = 4
        seg_h = (y1 - y0 - 16 - (n - 1) * gap) / float(n)
        seg_h = max(3.0, seg_h)
        inner_x0, inner_x1 = x0 + 5, x1 - 5
        ratio = T.clamp((value or 0) / 100.0)
        lit = int(round(ratio * n))
        for i in range(n):
            sy = y0 + 8 + i * (seg_h + gap)
            if n - 1 - i < lit:
                # 底部（低负载）偏青绿，顶部（高负载）转红，符合 F1 转速条观感
                t = (n - 1 - i) / float(n - 1)
                col = T.heat(t)
                self._dyn.rectangle([inner_x0, sy, inner_x1, sy + seg_h], fill=col + (255,))
            else:
                self._dyn.rectangle([inner_x0, sy, inner_x1, sy + seg_h], fill=(24, 29, 36, 255))

    def _glow(self, text, f, color):
        """给大号数字做一层柔光。"""
        key = ("glow", text, f.size, color)
        hit = self._rounded_cache.get(key)
        if hit is not None:
            return hit
        pad = 34
        w = int(f.getlength(text)) + pad * 2
        h = int(f.size * 1.5) + pad * 2
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text((pad, pad), text, font=f, fill=color + (90,))
        from PIL import ImageFilter
        layer = layer.filter(ImageFilter.GaussianBlur(14))
        self._rounded_cache[key] = layer
        return layer

    # ---------------------------------------------------------------- 数据卡片
    def _draw_chips(self, snap):
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        mem = snap.get("mem") or {}
        disks = snap.get("disks") or []
        dio = snap.get("disk_io") or {}
        net = snap.get("net") or {}
        board = snap.get("board") or {}
        boxes = self.chip_boxes

        # --- CPU
        b = boxes[0]
        self._kv(b, 0, "温度", self._num(cpu.get("temp"), 1) + " °C", T.temp_color(cpu.get("temp")))
        self._kv(b, 1, "功耗", self._num(cpu.get("power"), 0) + " W", T.SILVER)
        self._kv(b, 2, "核心", self._num(cpu.get("clock"), 0) + " MHz", T.SILVER)
        self._kv(b, 3, "线程", f"{cpu.get('threads', 0)} 线程 / {cpu.get('cores', 0)} 核", T.MUTED)
        self._bar(b, (cpu.get("load") or 0) / 100.0)

        # --- GPU
        b = boxes[1]
        self._kv(b, 0, "温度", self._num(gpu.get("temp"), 0) + " °C", T.temp_color(gpu.get("temp")))
        self._kv(b, 1, "功耗", self._num(gpu.get("power"), 0) + " W", T.SILVER)
        self._kv(b, 2, "风扇", self._num(gpu.get("fan"), 0) + " RPM", T.SILVER)
        self._kv(b, 3, "核心", self._num(gpu.get("core_mhz"), 0) + " MHz", T.SILVER)
        self._bar(b, (gpu.get("load") or 0) / 100.0)

        # --- 内存
        b = boxes[2]
        self._big_in_chip(b, self._pct(mem.get("pct")),
                          T.heat(T.clamp((mem.get("pct") or 0) / 100.0)))
        self._kv(b, 2, "已用", f"{self._num(mem.get('used_gb'),1)} GB", T.SILVER)
        self._kv(b, 3, "可用", f"{self._num(mem.get('avail_gb'),1)} GB", T.MUTED)
        self._bar(b, (mem.get("pct") or 0) / 100.0)

        # --- 显存
        b = boxes[3]
        vp = gpu.get("vram_pct")
        self._big_in_chip(b, self._pct(vp), T.heat(T.clamp((vp or 0) / 100.0)))
        self._kv(b, 2, "已用", f"{self._num((gpu.get('vram_used_mb') or 0)/1024.0, 2)} GB", T.SILVER)
        total_gb = (gpu.get("vram_total_mb") or 0) / 1024.0
        self._kv(b, 3, "总量", f"{total_gb:.1f} GB" if total_gb else "—", T.MUTED)
        self._bar(b, (vp or 0) / 100.0)

        # --- 磁盘
        b = boxes[4]
        main = disks[0] if disks else None
        if main:
            self._big_in_chip(b, f"{main['pct']:.0f}%", T.heat(main["pct"] / 100.0))
            self._kv(b, 2, "系统盘", f"{main['used_gb']:.0f}/{main['total_gb']:.0f} GB", T.SILVER)
        else:
            self._big_in_chip(b, "—", T.MUTED)
        self._kv(b, 3, "读写",
                 f"R {self._num(dio.get('read_mbs'),1)} / W {self._num(dio.get('write_mbs'),1)} MB/s",
                 T.MUTED)
        self._bar(b, (main["pct"] / 100.0) if main else 0.0)

        # --- 网络
        b = boxes[5]
        self._kv(b, 0, "下载", f"{self._num(net.get('down_mbs'),2)} MB/s", T.TEAL)
        self._kv(b, 1, "上传", f"{self._num(net.get('up_mbs'),2)} MB/s", T.CYAN)
        self._kv(b, 2, "累计下行", f"{self._num(net.get('total_recv_gb'),1)} GB", T.SILVER)
        self._kv(b, 3, "累计上行", f"{self._num(net.get('total_sent_gb'),1)} GB", T.MUTED)
        self._bar(b, T.clamp((net.get("down_mbs") or 0) / 100.0))

    def _big_in_chip(self, box, text, color):
        self._text(box[0] + 12, box[1] + 34, text, 36, color, "bold", anchor="la", mono=True)

    def _kv(self, box, row, key, value, color):
        """卡片内的一行键值。行距 22px，字号 13/15，保证上下行不会互相压字。"""
        x = box[0] + 12
        y = box[1] + 52 + row * 22
        self._text(x, y, key, 13, T.MUTED, "regular", anchor="la", cjk=True)
        self._text(box[2] - 12, y, value, 15, color, "semi", anchor="ra")

    def _bar(self, box, ratio):
        x0 = box[0] + 12
        x1 = box[2] - 12
        y = box[1] + 150
        self._dyn.rectangle([x0, y, x1, y + 7], fill=(28, 34, 42, 255))
        w = int((x1 - x0) * T.clamp(ratio))
        if w > 1:
            self._dyn.rectangle([x0, y, x0 + w, y + 7], fill=T.heat(T.clamp(ratio)) + (255,))

    # ---------------------------------------------------------------- 底栏
    def _draw_footer(self, snap):
        fx0, fy0, fx1, fy1 = FOOTER
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        board = snap.get("board") or {}
        admin = snap.get("admin")
        y = (fy0 + fy1) / 2
        self._text(fx0 + 26, y, "F1 TELEMETRY DASHBOARD", 15, T.MUTED, "semi", anchor="lm")
        name = f"{cpu.get('name','CPU')}  ·  {gpu.get('name','GPU')}"
        bt = board.get("sys_temp") or cpu.get("temp_board")
        if bt:
            name += f"  ·  主板 {bt:.0f}°C"
        elif gpu.get("fan"):
            name += f"  ·  GPU 风扇 {gpu['fan']:.0f} RPM"
        self._text(fx0 + 226, y, name, 15, T.SILVER, "semi", anchor="lm", cjk=True)

        if admin:
            st, col = "管理员模式 · 全部传感器可用", T.GREEN
        else:
            st, col = "普通权限 · CPU 温度需以管理员身份运行", T.AMBER
        self._text(fx1 - 20, y, st, 15, col, "semi", anchor="rm", cjk=True)

    # ---------------------------------------------------------------- 文本工具
    def _text(self, x, y, text, size, color, style="semi", anchor="la", mono=False,
              font=None, cjk=False):
        if text is None or text == "":
            return
        if font is None:
            if cjk:
                font = T.font_cjk(size, bold=style in ("semi", "bold"))
            else:
                font = T.pick_font(str(text), size, style, True, mono)
        self._dyn.text((x, y), str(text), font=font, fill=tuple(color) + (255,), anchor=anchor)

    @staticmethod
    def _num(v, digits=1):
        if v is None:
            return "—"
        try:
            if digits <= 0:
                return f"{v:.0f}"
            return f"{v:.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt(v, digits=1, unit=""):
        """数值 + 单位；没有读数时只返回一个破折号，不拼单位（避免出现 “—°C” 这种挤在一起的写法）。"""
        if v is None:
            return "—"
        try:
            txt = f"{v:.0f}" if digits <= 0 else f"{v:.{digits}f}"
        except (TypeError, ValueError):
            return "—"
        return f"{txt} {unit}".strip()

    @staticmethod
    def _pct(v):
        if v is None:
            return "—"
        return f"{v:.0f}%"

    @staticmethod
    def _vram_text(gpu):
        used = gpu.get("vram_used_mb") or 0
        total = gpu.get("vram_total_mb") or 0
        if not total:
            return "—"
        return f"{used/1024.0:.1f} / {total/1024.0:.1f} GB"
