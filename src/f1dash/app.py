# -*- coding: utf-8 -*-
"""仪表盘主窗口：Tk 画布 + PIL 逐帧渲染 + 交互（置顶 / 悬浮 / 穿透 / 缩放）。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from . import render as R
from . import theme as T
from .mini import MiniRenderer
from .sensors import SensorHub

LOG = logging.getLogger("f1dash")


def setup_logging(base_dir: str) -> logging.Logger:
    """把运行日志写到 logs/f1dash.log，方便排查“启动后闪退”这类问题。"""
    path = os.path.join(base_dir, "logs", "f1dash.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.replace(path, path + ".1")
    except Exception:
        pass
    logger = logging.getLogger("f1dash")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
            logger.addHandler(fh)
        except Exception:
            pass
    return logger

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
HOTKEY_ID = 0xB1A5


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def screen_size():
    try:
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(0), u.GetSystemMetrics(1))
    except Exception:
        return (1920, 1080)


class DashboardApp:
    def __init__(self, base_dir: str, config: dict):
        self.base_dir = base_dir
        self.cfg = config
        self.log = setup_logging(base_dir)
        self._quitting = False
        self.log.info("=" * 60)
        self.log.info("启动: pid=%s python=%s", os.getpid(), sys.version.split()[0])
        self.log.info("配置: %s", config)
        self.log.info("管理员权限: %s", bool(ctypes.windll.shell32.IsUserAnAdmin()))
        self.scale = float(config.get("scale", 1.0))
        # 默认形态：mini = 无边框悬浮小窗；full = 完整仪表盘
        mode = config.get("mode", "mini")
        if mode in ("hud", "window"):
            mode = "full"
        self.mode = mode
        self.topmost = bool(config.get("always_on_top", True))
        self.click_through = False
        self.ui_ms = int(config.get("ui_ms", 500))

        self.mini_renderer = MiniRenderer(config)
        self.full_renderer = R.HudRenderer(config)
        self.renderer = self.mini_renderer if self.mode == "mini" else self.full_renderer
        self.win_w = int(self.renderer.W * self.scale)
        self.win_h = int(self.renderer.H * self.scale)

        self.root = tk.Tk()
        self.root.title("F1 遥测仪表盘 · PC Telemetry Dashboard")
        bg = "#010203" if self.mode == "mini" else "#080a0d"
        self.root.configure(bg=bg)
        self.root.minsize(240, 160)

        sw, sh = screen_size()
        pos = config.get("position")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
        elif self.mode == "mini":
            x = max(0, sw - self.win_w - 48)
            y = 48
        else:
            x = max(0, (sw - self.win_w) // 2)
            y = max(0, (sh - self.win_h) // 2)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

        self.canvas = tk.Canvas(self.root, width=self.win_w, height=self.win_h,
                                highlightthickness=0, bd=0, bg=bg)
        self.canvas.pack(fill="both", expand=True)

        self._img = self.renderer.frame
        if self.scale != 1.0:
            self._img = self._img.resize((self.win_w, self.win_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(self._img)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self._apply_window_flags()
        self.root.wm_attributes("-alpha", float(config.get("opacity", 1.0)))

        self.hub = SensorHub(base_dir, config.get("sensors", {}))
        self.hub.start()

        self._drag = None
        self._bind_events()
        self._hotkey_thread = None
        self._start_hotkey()

        self.root.after(120, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------------------------------------------------------------- 窗口属性
    def _hwnd(self):
        self.root.update_idletasks()
        return ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()

    def _apply_window_flags(self):
        self.root.attributes("-topmost", bool(self.topmost))
        mini = (self.mode == "mini")
        try:
            self.root.overrideredirect(mini)
        except tk.TclError:
            pass
        if mini:
            try:
                self.root.wm_attributes("-transparentcolor", "#010203")
            except tk.TclError:
                pass
        else:
            try:
                self.root.wm_attributes("-transparentcolor", "")
            except tk.TclError:
                pass
        hwnd = self._hwnd()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if mini:
            # 悬浮窗不进任务栏、不参与 Alt+Tab，当作真正的 OSD
            style |= WS_EX_TOOLWINDOW
        else:
            style &= ~WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        if not mini:
            self.root.deiconify()
            self.root.lift()

    def _set_click_through(self, on: bool):
        try:
            hwnd = self._hwnd()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self.click_through = bool(on)
        except Exception:
            pass

    def _start_hotkey(self):
        """Ctrl+Alt+L 切换鼠标穿透（穿透后仍可用快捷键切回来）。"""
        def worker():
            u = ctypes.windll.user32
            if not u.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, 0x4C):
                self.log.warning("Ctrl+Alt+L 全局热键注册失败（已被其他程序占用），鼠标穿透需用右键菜单")
                return
            self.log.info("全局热键 Ctrl+Alt+L 已注册（鼠标穿透开关）")
            msg = wt.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.root.after(0, lambda: self.toggle_click_through())
            u.UnregisterHotKey(None, HOTKEY_ID)

        import threading
        self._hotkey_thread = threading.Thread(target=worker, daemon=True)
        self._hotkey_thread.start()

    # ---------------------------------------------------------------- 事件
    def _bind_events(self):
        self.root.bind("<Escape>", lambda e: self.on_escape())
        self.root.bind("<KeyPress-h>", lambda e: self.toggle_mode())
        self.root.bind("<KeyPress-H>", lambda e: self.toggle_mode())
        self.root.bind("<KeyPress-t>", lambda e: self.toggle_topmost())
        self.root.bind("<KeyPress-T>", lambda e: self.toggle_topmost())
        self.root.bind("<KeyPress-p>", lambda e: self.toggle_procs())
        self.root.bind("<KeyPress-P>", lambda e: self.toggle_procs())
        self.root.bind("<KeyPress-r>", lambda e: self.reset_size())
        self.root.bind("<KeyPress-R>", lambda e: self.reset_size())
        self.root.bind("<KeyPress-s>", lambda e: self.save_config())
        self.root.bind("<KeyPress-S>", lambda e: self.save_config())
        self.root.bind("<F11>", lambda e: self.toggle_mode())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_mode())
        self.canvas.bind("<Button-3>", self._popup)
        self.root.bind("<Button-3>", self._popup)

    def on_escape(self):
        """Esc：悬浮窗形态下退出，完整形态下切回悬浮窗。"""
        if self.mode == "mini":
            self.quit()
        else:
            self.show_mini()

    def _on_press(self, ev):
        self._drag = (ev.x_root, ev.y_root,
                      self.root.winfo_x(), self.root.winfo_y(), False)

    def _on_drag(self, ev):
        if not self._drag:
            return
        sx, sy, wx, wy, _ = self._drag
        dx, dy = ev.x_root - sx, ev.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._drag = (sx, sy, wx, wy, True)
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_release(self, ev):
        self._drag = None

    def _popup(self, ev):
        self.log.info("右键菜单 @ (%s, %s), 模式=%s", ev.x_root, ev.y_root, self.mode)
        m = tk.Menu(self.root, tearoff=0, bg="#141a21", fg="#e8eef4",
                    activebackground="#27313c", activeforeground="#ffffff", bd=0,
                    font=("Microsoft YaHei UI", 10))
        if self.mode == "mini":
            m.add_command(label="显示全部信息", command=self.show_full)
        else:
            m.add_command(label="收起为悬浮小窗", command=self.show_mini)
        m.add_command(label="设置…", command=self.open_settings)
        m.add_separator()
        m.add_command(label=f"{'✔ ' if self.topmost else '   '}窗口置顶",
                      command=self.toggle_topmost)
        m.add_command(label=f"{'✔ ' if self.click_through else '   '}鼠标穿透 (Ctrl+Alt+L)",
                      command=self.toggle_click_through)
        if self.mode != "mini":
            m.add_command(label=f"{'✔ ' if self.renderer.show_procs else '   '}显示进程排行",
                          command=self.toggle_procs)
        m.add_separator()
        m.add_command(label="打开程序目录", command=self.open_folder)
        m.add_command(label="关闭悬浮窗", command=self.quit)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    # ---------------------------------------------------------------- 模式切换
    def show_mini(self):
        self._switch_mode("mini")

    def show_full(self):
        self._switch_mode("full")

    def toggle_mode(self):
        self._switch_mode("full" if self.mode == "mini" else "mini")

    def _switch_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        # 两个渲染器在启动时都已经建好静态层，这里直接换引用即可
        self.renderer = self.mini_renderer if mode == "mini" else self.full_renderer
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self._resize_to_renderer(x, y)

    def _resize_to_renderer(self, x=None, y=None):
        sw, sh = screen_size()
        self.win_w = int(self.renderer.W * self.scale)
        self.win_h = int(self.renderer.H * self.scale)
        if x is None:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        mini = (self.mode == "mini")
        # 完整面板有边框和标题栏，位置要再多留一点余量，否则会顶出屏幕右边
        margin = 0 if mini else 22
        x = max(0, min(int(x), max(0, sw - self.win_w - margin)))
        y = max(0, min(int(y), max(0, sh - self.win_h - margin)))
        bg = "#010203" if mini else "#080a0d"
        try:
            self.root.overrideredirect(mini)
        except tk.TclError:
            pass
        self.root.configure(bg=bg)
        self.canvas.configure(bg=bg, width=self.win_w, height=self.win_h)
        try:
            self.root.wm_attributes("-transparentcolor", "#010203" if mini else "")
        except tk.TclError:
            pass
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")
        self._rebuild_photo()
        self._apply_window_flags()

    # ---------------------------------------------------------------- 动作
    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)

    def toggle_procs(self):
        if hasattr(self.renderer, "show_procs"):
            self.renderer.show_procs = not self.renderer.show_procs
        else:
            self._toast("进程排行只能在完整面板里显示")

    def toggle_click_through(self):
        self._set_click_through(not self.click_through)

    def reset_size(self):
        self.scale = 1.0
        self._resize_to_renderer()

    def open_settings(self):
        SettingsDialog(self)

    def open_folder(self):
        try:
            os.startfile(self.base_dir)
        except Exception:
            pass

    def apply_settings(self, scale=None, opacity=None, ui_ms=None):
        """设置对话框改完即时生效。"""
        if scale is not None:
            self.scale = float(scale)
        if opacity is not None:
            self.root.wm_attributes("-alpha", float(opacity))
        if ui_ms is not None:
            self.ui_ms = int(ui_ms)
        self._resize_to_renderer()

    def _rebuild_photo(self):
        img = self.renderer.render(self.hub.snapshot())
        if self.scale != 1.0:
            img = img.resize((self.win_w, self.win_h), Image.LANCZOS)
        self._img = img
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

    def save_config(self):
        import json
        self.cfg["always_on_top"] = self.topmost
        self.cfg["mode"] = self.mode
        self.cfg["scale"] = self.scale
        self.cfg["opacity"] = float(self.root.wm_attributes("-alpha"))
        self.cfg["ui_ms"] = self.ui_ms
        if hasattr(self.full_renderer, "show_procs"):
            self.cfg["show_processes"] = self.full_renderer.show_procs
        self.cfg["position"] = [self.root.winfo_x(), self.root.winfo_y()]
        path = os.path.join(self.base_dir, "config.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
            self._toast("设置已保存")
        except Exception as exc:
            self._toast(f"保存失败: {exc}")
        return self.cfg

    def _toast(self, text):
        self._toast_text = text
        self._toast_until = time.time() + 2.2

    # ---------------------------------------------------------------- 主循环
    def _tick(self):
        try:
            snap = self.hub.snapshot()
            img = self.renderer.render(snap)
            if self.scale != 1.0:
                img = img.resize((self.win_w, self.win_h), Image.LANCZOS)
            self.photo.paste(img)
            for note in self.hub.drain_notes():
                self._toast(note)
            if getattr(self, "_toast_until", 0) > time.time():
                self.canvas.delete("toast")
                self.canvas.create_text(
                    self.win_w // 2, self.win_h - 8, text=self._toast_text,
                    fill="#ffd400", font=("Microsoft YaHei UI", 11), anchor="s", tags="toast")
            else:
                self.canvas.delete("toast")
        except Exception as exc:
            self.log.error("刷新失败: %s\n%s", exc, traceback.format_exc())
        self.root.after(self.ui_ms, self._tick)

    def run(self):
        self.log.info("进入主循环, 窗口 %dx%d @ %s,%s", self.win_w, self.win_h,
                      self.root.winfo_x(), self.root.winfo_y())
        try:
            self.root.mainloop()
        except BaseException:
            self.log.error("主循环异常:\n%s", traceback.format_exc())
            raise
        self.log.info("主循环已退出")

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.log.info("收到退出指令, 调用栈:\n%s", "".join(traceback.format_stack()[:-1]))
        try:
            self.hub.stop()
        except Exception:
            self.log.error("停止采集层失败:\n%s", traceback.format_exc())
        try:
            self.root.destroy()
        except Exception:
            pass
        self.log.info("已退出")


STARTUP_CMD = "F1遥测仪表盘.cmd"


def startup_path() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        STARTUP_CMD)


def startup_enabled() -> bool:
    try:
        return os.path.exists(startup_path())
    except Exception:
        return False


def set_startup(enable: bool, base_dir: str, log=None):
    """用启动文件夹里的一个 .cmd 实现开机自启，不需要管理员权限，也不用 COM。"""
    p = startup_path()
    try:
        if enable:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="gbk", errors="replace") as fh:
                fh.write("@echo off\r\n")
                fh.write('start "" powershell -NoProfile -ExecutionPolicy Bypass '
                         '-WindowStyle Hidden -File "{}"\r\n'
                         .format(os.path.join(base_dir, "scripts", "launch.ps1")))
        else:
            if os.path.exists(p):
                os.remove(p)
        return True
    except Exception:
        if log:
            log.error("设置开机自启失败:\n%s", traceback.format_exc())
        return False


class SettingsDialog:
    """浅色深底的小设置窗，改完立即生效。"""

    def __init__(self, app: "DashboardApp"):
        self.app = app
        top = tk.Toplevel(app.root)
        self.top = top
        top.title("F1 遥测仪表盘 · 设置")
        top.configure(bg="#141a21")
        top.resizable(False, False)
        top.attributes("-topmost", True)
        try:
            top.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        cfg = app.cfg
        pad = {"padx": 14, "pady": 6}

        def label(text, r):
            tk.Label(top, text=text, bg="#141a21", fg="#93a1b0",
                     font=("Microsoft YaHei UI", 10)).grid(row=r, column=0, sticky="w", **pad)

        def menu(row, var, values, width=8):
            om = tk.OptionMenu(top, var, *values)
            om.configure(bg="#232c36", fg="#e8eef4", activebackground="#2e3946",
                         activeforeground="#ffffff", bd=0, highlightthickness=0,
                         font=("Consolas", 10), width=width, anchor="w")
            om["menu"].configure(bg="#1b222b", fg="#e8eef4", activebackground="#27f4d2",
                                 activeforeground="#06231f", bd=0,
                                 font=("Microsoft YaHei UI", 10))
            om.grid(row=row, column=1, sticky="w", **pad)
            return om

        # 缩放
        label("界面缩放", 0)
        self.var_scale = tk.StringVar(value=str(cfg.get("scale", 1.0)))
        menu(0, self.var_scale, ("0.6", "0.75", "0.9", "1.0", "1.25", "1.5"))

        # 不透明度
        label("不透明度", 1)
        self.var_opacity = tk.DoubleVar(value=float(cfg.get("opacity", 0.98)))
        tk.Scale(top, from_=0.3, to=1.0, resolution=0.02, orient="horizontal", length=170,
                 variable=self.var_opacity, bg="#141a21", fg="#e8eef4",
                 troughcolor="#232c36", highlightthickness=0, bd=0,
                 activebackground="#27f4d2").grid(row=1, column=1, sticky="w", **pad)

        # 刷新间隔
        label("刷新间隔", 2)
        self.var_ui = tk.StringVar(value=str(cfg.get("ui_ms", 500)))
        menu(2, self.var_ui, ("250", "500", "1000", "2000"), width=8)

        # 置顶
        self.var_top = tk.BooleanVar(value=bool(app.topmost))
        tk.Checkbutton(top, text="窗口置顶", variable=self.var_top, bg="#141a21",
                       fg="#e8eef4", selectcolor="#232c36", activebackground="#141a21",
                       activeforeground="#e8eef4", font=("Microsoft YaHei UI", 10),
                       highlightthickness=0).grid(row=3, column=1, sticky="w", **pad)

        # 进程排行（只对完整面板有意义）
        self.var_procs = tk.BooleanVar(value=bool(cfg.get("show_processes", True)))
        tk.Checkbutton(top, text="完整面板显示进程排行", variable=self.var_procs, bg="#141a21",
                       fg="#e8eef4", selectcolor="#232c36", activebackground="#141a21",
                       activeforeground="#e8eef4", font=("Microsoft YaHei UI", 10),
                       highlightthickness=0).grid(row=4, column=1, sticky="w", **pad)

        # 开机自启
        self.var_boot = tk.BooleanVar(value=startup_enabled())
        tk.Checkbutton(top, text="开机自动启动（悬浮窗）", variable=self.var_boot, bg="#141a21",
                       fg="#e8eef4", selectcolor="#232c36", activebackground="#141a21",
                       activeforeground="#e8eef4", font=("Microsoft YaHei UI", 10),
                       highlightthickness=0).grid(row=5, column=1, sticky="w", **pad)

        tk.Frame(top, bg="#232c36", height=1).grid(row=6, column=0, columnspan=2,
                                                   sticky="ew", padx=10, pady=6)

        btns = tk.Frame(top, bg="#141a21")
        btns.grid(row=7, column=0, columnspan=2, sticky="e", padx=12, pady=(2, 12))
        tk.Button(btns, text="恢复默认形态", command=self._reset_mode, bg="#232c36",
                  fg="#e8eef4", activebackground="#2e3946", activeforeground="#ffffff",
                  bd=0, padx=12, pady=5, font=("Microsoft YaHei UI", 10)).pack(side="left", padx=4)
        tk.Button(btns, text="保存", command=self._save, bg="#27f4d2", fg="#06231f",
                  activebackground="#5ffade", activeforeground="#06231f", bd=0, padx=18,
                  pady=5, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=4)

        top.update_idletasks()
        x = app.root.winfo_x() + max(0, (app.win_w - top.winfo_width()) // 2)
        y = app.root.winfo_y() + max(0, (app.win_h - top.winfo_height()) // 2)
        sw, sh = screen_size()
        top.geometry(f"+{max(0, min(x, sw - top.winfo_width()))}"
                     f"+{max(0, min(y, sh - top.winfo_height()))}")

    def _reset_mode(self):
        self.app._switch_mode("mini")
        self.app.scale = 1.0
        self.app.root.wm_attributes("-alpha", 0.98)
        self.app._resize_to_renderer()
        self.app._toast("已恢复默认悬浮小窗")
        self.top.destroy()

    def _save(self):
        app = self.app
        try:
            scale = float(self.var_scale.get())
        except ValueError:
            scale = 1.0
        app.topmost = bool(self.var_top.get())
        app.root.attributes("-topmost", app.topmost)
        if hasattr(app.full_renderer, "show_procs"):
            app.full_renderer.show_procs = bool(self.var_procs.get())
        try:
            ui = int(self.var_ui.get())
        except ValueError:
            ui = 500
        app.apply_settings(scale=scale, opacity=float(self.var_opacity.get()), ui_ms=ui)
        ok = set_startup(bool(self.var_boot.get()), app.base_dir, app.log)
        if self.var_boot.get() and not ok:
            app._toast("开机自启设置失败，详见日志")
        app.save_config()
        app._toast("设置已保存")
        self.top.destroy()


def load_config(base_dir: str) -> dict:
    import json
    defaults = {
        "mode": "mini",            # mini = 悬浮小窗（默认），full = 完整仪表盘
        "scale": 1.0,
        "always_on_top": True,
        "opacity": 0.98,
        "ui_ms": 500,
        "show_processes": True,
        "language": "zh",
        "sensors": {
            "poll_ms": 1000,
            "process_ms": 2000,
            "top_processes": 12,
            "enable_lhm": True,
        },
    }
    path = os.path.join(base_dir, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            for k, v in user.items():
                if k == "sensors" and isinstance(v, dict):
                    defaults["sensors"].update(v)
                else:
                    defaults[k] = v
        except Exception:
            pass
    return defaults


def main(base_dir: str):
    set_dpi_awareness()
    cfg = load_config(base_dir)
    app = DashboardApp(base_dir, cfg)
    app.run()
