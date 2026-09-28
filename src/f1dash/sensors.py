# -*- coding: utf-8 -*-
"""硬件数据采集层。

数据来源分两路，互为补充：

1. ``SensorBridge.exe``（自编译，链接 LibreHardwareMonitorLib）
   提供 CPU 每核心温度/功耗/频率/电压、GPU 温度/功耗/风扇/频率、
   显存占用、主板温度与风扇转速、硬盘温度。需要管理员权限才能读到
   CPU 核心温度等 MSR 数据。
2. ``psutil``
   提供 CPU / 内存 / 磁盘 / 网络的秒级统计和进程排行，无需任何权限。

两条路径都拿不到的数据会以 ``None`` 上报，界面统一显示为 “—”。
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque

import psutil

from .pdh import SystemCounters

CREATE_NO_WINDOW = 0x08000000

# LHM 在无驱动/无权限时会把温度、功耗返回 0，这类 0 必须当作“无数据”
_ZERO_INVALID_TYPES = {"Temperature", "Power", "Fan", "Clock", "Voltage"}


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# LibreHardwareMonitor 桥接器
# --------------------------------------------------------------------------
class LhmBridge:
    """常驻子进程 + 行协议解析。

    协议（见 vendor/bridge/SensorBridge.cs）::

        H|<json>   头部：硬件与传感器清单
        D|<json>   数据：[硬件序号, 传感器序号, 值]
        E|<text>   错误信息（stderr）
    """

    def __init__(self, exe: str, interval_ms: int = 1000):
        self.exe = exe
        self.interval_ms = interval_ms
        self.proc: subprocess.Popen | None = None
        self.meta: list[dict] = []            # 硬件清单
        self.sensor_meta: dict = {}           # (hw, sidx) -> {"name","type","unit","hw_name","hw_type"}
        self.values: dict = {}                # (hw, sidx) -> float
        self.ready = False
        self.last_error = ""
        self.started_at = 0.0
        self.restarts = 0
        self._q: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = False

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        self._stop = False
        self._spawn()

    def _spawn(self):
        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                [self.exe, "--interval", str(self.interval_ms)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                cwd=os.path.dirname(self.exe),
                creationflags=creationflags,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:            # pragma: no cover
            self.proc = None
            self.last_error = f"无法启动传感器桥接器: {exc}"
            return
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, args=(self.proc,), daemon=True)
        self._reader.start()

    def _read_loop(self, proc: subprocess.Popen):
        try:
            for line in proc.stdout:
                if self._stop:
                    break
                self._q.put(line)
        except Exception:
            pass
        finally:
            try:
                self._q.put(None)
            except Exception:
                pass

    def stop(self):
        self._stop = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.proc = None

    # ---------------------------------------------------------------- 解析
    def pump(self) -> bool:
        """把已到达的输出行全部消化掉，返回是否有新数据。"""
        got = False
        while True:
            try:
                line = self._q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                # 子进程退出
                if not self._stop:
                    self.ready = False
                    self._maybe_restart()
                continue
            line = line.strip()
            if not line:
                continue
            try:
                if line.startswith("H|"):
                    self._parse_header(line[2:])
                    got = True
                elif line.startswith("D|"):
                    self._parse_data(line[2:])
                    got = True
            except Exception as exc:
                self.last_error = f"协议解析失败: {exc}"
        return got

    def _maybe_restart(self):
        now = time.time()
        if now - self.started_at < 5:
            self.restarts += 1
            if self.restarts > 5:
                self.last_error = "传感器桥接器反复退出，已停止重启"
                return
        import json
        try:
            self._spawn()
        except Exception:
            pass

    def _parse_header(self, payload: str):
        import json
        data = json.loads(payload)
        with self._lock:
            self.meta = data
            sm = {}
            for hw in data:
                for s in hw.get("s", []):
                    sm[(hw["i"], s["i"])] = {
                        "name": s["n"],
                        "type": s["t"],
                        "unit": s.get("u", ""),
                        "hw_name": hw["n"],
                        "hw_type": hw["t"],
                    }
            self.sensor_meta = sm
            self.ready = True

    def _parse_data(self, payload: str):
        import json
        rows = json.loads(payload)
        with self._lock:
            vals = self.values
            for hw, sidx, val in rows:
                vals[(hw, sidx)] = val

    # ---------------------------------------------------------------- 查询
    def find(self, hw_type=None, hw_name=None, name=None, stype=None,
             name_contains=None, hw_name_contains=None):
        """按条件找传感器 key ``(hw, sidx)``；找不到返回 None。"""
        with self._lock:
            for key, m in self.sensor_meta.items():
                if hw_type is not None and m["hw_type"] != hw_type:
                    continue
                if stype is not None and m["type"] != stype:
                    continue
                if hw_name is not None and m["hw_name"] != hw_name:
                    continue
                if name is not None and m["name"] != name:
                    continue
                if name_contains is not None and name_contains not in m["name"]:
                    continue
                if hw_name_contains is not None and hw_name_contains not in m["hw_name"]:
                    continue
                return key
        return None

    def find_all(self, **kw):
        out = []
        with self._lock:
            items = list(self.sensor_meta.items())
        for key, m in items:
            ok = True
            for cond, want in kw.items():
                if cond == "name_contains":
                    ok = ok and (want in m["name"])
                elif cond == "hw_name_contains":
                    ok = ok and (want in m["hw_name"])
                elif cond == "hw_type":
                    ok = ok and (m["hw_type"] == want)
                elif cond == "stype":
                    ok = ok and (m["type"] == want)
                elif cond == "name":
                    ok = ok and (m["name"] == want)
                else:
                    ok = False
            if ok:
                out.append((key, m))
        return out

    def get(self, key):
        """读一个传感器的值，缺失或明显无效时返回 None。"""
        if key is None:
            return None
        with self._lock:
            val = self.values.get(key)
            meta = self.sensor_meta.get(key)
        if val is None:
            return None
        try:
            val = float(val)
        except (TypeError, ValueError):
            return None
        if meta and meta["type"] in _ZERO_INVALID_TYPES and val <= 0.0:
            # 桥接器在无权限时会把 MSR 类读数报成 0
            return None
        return val


# --------------------------------------------------------------------------
# 采集中心
# --------------------------------------------------------------------------
class SensorHub:
    def __init__(self, base_dir: str, config: dict | None = None):
        cfg = dict(config or {})
        self.base_dir = base_dir
        self.poll_ms = int(cfg.get("poll_ms", 1000))
        self.proc_ms = int(cfg.get("process_ms", 2000))
        self.top_n = int(cfg.get("top_processes", 12))
        self.enable_lhm = bool(cfg.get("enable_lhm", True))

        self.bridge = LhmBridge(
            os.path.join(base_dir, "vendor", "bridge", "SensorBridge.exe"),
            interval_ms=max(200, self.poll_ms),
        )

        self.admin = is_admin()
        self.notes: list[str] = []
        self._lock = threading.Lock()
        self._latest: dict = self._empty_snapshot()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bound: dict = {}

        # 速率计算用的上一次计数
        self._prev_disk = None
        self._prev_net = None
        self._prev_t = None
        self._proc_cache: dict = {}
        self._proc_tick = 0
        self._procs: list = []

        # Windows 性能计数器：进程排行 + GPU 兜底（一次查询约 7ms）
        try:
            self.syscounters = SystemCounters()
        except Exception:
            self.syscounters = None
        if self.syscounters is not None and not self.syscounters.available:
            self.syscounters = None

        # 历史曲线
        self.hist_cpu = deque(maxlen=150)
        self.hist_gpu = deque(maxlen=150)
        self.hist_mem = deque(maxlen=150)
        self.hist_vram = deque(maxlen=150)

        self._ncpu = psutil.cpu_count(logical=True) or 1

    # ---------------------------------------------------------------- 启动
    def start(self):
        if self.enable_lhm and os.path.exists(self.bridge.exe):
            self.bridge.start()
        else:
            self.notes.append("未找到 SensorBridge.exe，温度/功耗等传感器不可用")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.bridge.stop()

    def _empty_snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "admin": False,
            "lhm": False,
            "cpu": {},
            "gpu": {},
            "mem": {},
            "swap": {},
            "board": {},
            "disks": [],
            "net": {},
            "procs": [],
            "uptime": 0.0,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._latest

    def drain_notes(self) -> list:
        with self._lock:
            out = list(self.notes)
            self.notes = []
        return out

    # ---------------------------------------------------------------- 主循环
    def _loop(self):
        # 首帧填充 CPU 百分比基线
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        time.sleep(0.35)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.bridge.pump()
                snap = self._build()
                with self._lock:
                    self._latest = snap
            except Exception as exc:      # pragma: no cover
                with self._lock:
                    self.notes.append(f"采集异常: {exc}")
            dt = time.time() - t0
            self._stop.wait(max(0.05, self.poll_ms / 1000.0 - dt))

    # ---------------------------------------------------------------- 组装
    def _bind(self):
        """把常用传感器 key 缓存下来，避免每帧字符串匹配。"""
        b = self.bridge
        if not b.ready:
            return
        if self._bound.get("_stamp") == len(b.sensor_meta):
            return
        k = {}
        # CPU
        k["cpu_total"] = b.find(hw_type="Cpu", name="CPU Total", stype="Load")
        k["cpu_temp"] = b.find(hw_type="Cpu", stype="Temperature", name="Core (Tctl/Tdie)")
        if k["cpu_temp"] is None:
            k["cpu_temp"] = b.find(hw_type="Cpu", stype="Temperature", name_contains="Tctl")
        k["cpu_ccd"] = b.find(hw_type="Cpu", stype="Temperature", name_contains="CCD")
        k["cpu_power"] = b.find(hw_type="Cpu", name="Package", stype="Power")
        k["cpu_clock"] = b.find(hw_type="Cpu", name="Cores (Average)", stype="Clock")
        k["cpu_clock_eff"] = b.find(hw_type="Cpu", name="Cores (Average Effective)", stype="Clock")
        k["cpu_vcore"] = b.find(hw_type="Cpu", name="Core (SVI2 TFN)", stype="Voltage")
        k["cpu_cores"] = b.find_all(hw_type="Cpu", name_contains="CPU Core #", stype="Load")
        k["cpu_cores"] = sorted(k["cpu_cores"], key=lambda kv: _num_suffix(kv[1]["name"]))
        # GPU
        for gt in ("GpuIntel", "GpuNvidia", "GpuAmd"):
            if b.find(hw_type=gt) is not None:
                k["gpu_type"] = gt
                break
        gt = k.get("gpu_type")
        if gt:
            k["gpu_name"] = b.find(hw_type=gt)
            k["gpu_temp"] = b.find(hw_type=gt, name="GPU Core", stype="Temperature")
            k["gpu_load"] = b.find(hw_type=gt, name="GPU Core", stype="Load")
            k["gpu_render"] = b.find(hw_type=gt, name="GPU Render/Compute", stype="Load")
            k["gpu_media"] = b.find(hw_type=gt, name="GPU Media", stype="Load")
            k["gpu_mem_load"] = b.find(hw_type=gt, name="GPU Memory", stype="Load")
            k["gpu_vram_used"] = b.find(hw_type=gt, name="GPU Memory Used", stype="SmallData")
            k["gpu_vram_total"] = b.find(hw_type=gt, name="GPU Memory Total", stype="SmallData")
            k["gpu_power"] = b.find(hw_type=gt, name="GPU Package", stype="Power")
            if k["gpu_power"] is None:
                k["gpu_power"] = b.find(hw_type=gt, stype="Power")
            k["gpu_fan"] = b.find(hw_type=gt, name="GPU Fan", stype="Fan")
            k["gpu_core_mhz"] = b.find(hw_type=gt, name="GPU Core", stype="Clock")
            k["gpu_mem_mhz"] = b.find(hw_type=gt, name="GPU Memory", stype="Clock")
        # 主板 / SuperIO
        k["mb_cpu_temp"] = b.find(hw_type="Motherboard", name="CPU", stype="Temperature")
        k["mb_sys_temp"] = b.find(hw_type="Motherboard", name="System", stype="Temperature")
        k["mb_vrm_temp"] = b.find(hw_type="Motherboard", name="VRM MOS", stype="Temperature")
        k["mb_pch_temp"] = b.find(hw_type="Motherboard", name="PCH", stype="Temperature")
        k["mb_fans"] = b.find_all(hw_type="Motherboard", stype="Fan")
        # 硬盘
        k["disks"] = b.find_all(hw_type="Storage", stype="Temperature")
        k["_stamp"] = len(b.sensor_meta)
        self._bound = k

    # ---------------------------------------------------------------- 具体采集
    def _build(self) -> dict:
        self._bind()
        b = self.bridge
        k = self._bound
        get = b.get
        now = time.time()

        snap = self._empty_snapshot()
        snap["ts"] = now
        snap["admin"] = self.admin
        snap["lhm"] = b.ready

        # ---- CPU
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_load = sum(per_core) / len(per_core) if per_core else (get(k.get("cpu_total")) or 0.0)
        freq = None
        try:
            f = psutil.cpu_freq()
            if f and f.max:
                freq = f
        except Exception:
            pass
        base_mhz = int(freq.max) if freq else None
        clock = get(k.get("cpu_clock"))
        if clock is None and base_mhz:
            clock = float(base_mhz)
        snap["cpu"] = {
            "name": _short_cpu_name(psutil_name_cpu()),
            "cores": psutil.cpu_count(logical=False) or 0,
            "threads": self._ncpu,
            "load": cpu_load,
            "per_core": list(per_core),
            "lhm_core_load": [get(key) for key, _ in (k.get("cpu_cores") or [])],
            "temp": get(k.get("cpu_temp")),
            "temp_ccd": get(k.get("cpu_ccd")),
            "temp_board": get(k.get("mb_cpu_temp")),
            "power": get(k.get("cpu_power")),
            "vcore": get(k.get("cpu_vcore")),
            "clock": clock,
            "clock_eff": get(k.get("cpu_clock_eff")),
            "base_mhz": base_mhz,
        }

        # ---- GPU
        vram_used = get(k.get("gpu_vram_used"))
        vram_total = get(k.get("gpu_vram_total"))
        gpu_load = get(k.get("gpu_load"))
        pdh_gpu = {}
        # 桥接器拿不到 GPU 数据时（例如驱动不支持），退回 Windows 性能计数器
        if gpu_load is None or vram_used is None:
            if self.syscounters is not None:
                try:
                    pdh_gpu = self.syscounters.gpu()
                except Exception:
                    pdh_gpu = {}
            if gpu_load is None:
                gpu_load = pdh_gpu.get("load")
            if vram_used is None:
                vram_used = pdh_gpu.get("vram_used_mb")
        if vram_total is None:
            vram_total = _registry_vram_mb()
        gpu_name = _short_gpu_name(_lhm_hw_name(b, k.get("gpu_name")) or _win_gpu_name())
        vram_pct = None
        if vram_used is not None and vram_total:
            vram_pct = 100.0 * vram_used / vram_total
        snap["gpu"] = {
            "name": gpu_name,
            "type": k.get("gpu_type", ""),
            "load": gpu_load,
            "render": get(k.get("gpu_render")),
            "media": get(k.get("gpu_media")),
            "mem_load": get(k.get("gpu_mem_load")),
            "temp": get(k.get("gpu_temp")),
            "power": get(k.get("gpu_power")),
            "fan": get(k.get("gpu_fan")),
            "core_mhz": get(k.get("gpu_core_mhz")),
            "mem_mhz": get(k.get("gpu_mem_mhz")),
            "vram_used_mb": vram_used,
            "vram_total_mb": vram_total,
            "vram_pct": vram_pct,
            "source": "lhm" if get(k.get("gpu_load")) is not None else "pdh",
        }

        # ---- 内存
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        snap["mem"] = {
            "used_gb": vm.used / 1073741824.0,
            "total_gb": vm.total / 1073741824.0,
            "avail_gb": vm.available / 1073741824.0,
            "pct": vm.percent,
        }
        snap["swap"] = {
            "used_gb": sm.used / 1073741824.0,
            "total_gb": sm.total / 1073741824.0,
            "pct": sm.percent,
        }

        # ---- 主板
        fans = []
        for key, meta in (k.get("mb_fans") or []):
            v = get(key)
            if v:
                fans.append({"name": meta["name"], "rpm": v})
        snap["board"] = {
            "name": "MSI B550M PRO-VDH",
            "sys_temp": get(k.get("mb_sys_temp")),
            "vrm_temp": get(k.get("mb_vrm_temp")),
            "pch_temp": get(k.get("mb_pch_temp")),
            "fans": fans,
        }

        # ---- 磁盘：容量 + 温度 + 读写速率
        try:
            io = psutil.disk_io_counters()
        except Exception:
            io = None
        rd = wr = 0.0
        if io is not None and self._prev_disk is not None and self._prev_t:
            dt = max(1e-3, now - self._prev_t)
            rd = max(0.0, (io.read_bytes - self._prev_disk.read_bytes) / dt / 1048576.0)
            wr = max(0.0, (io.write_bytes - self._prev_disk.write_bytes) / dt / 1048576.0)
        self._prev_disk = io

        temps = {}
        for key, meta in (k.get("disks") or []):
            v = get(key)
            if not v or v <= 0:
                continue
            nm = meta["hw_name"]
            # 同一块盘可能有多个温度传感器，优先取 Composite
            if nm not in temps or meta["name"] == "Composite Temperature":
                temps[nm] = v
        disks = []
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        system_root = os.environ.get("SystemDrive", "C:").upper()
        for p in parts:
            if "cdrom" in p.opts or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            disks.append({
                "mount": p.mountpoint,
                "used_gb": u.used / 1073741824.0,
                "total_gb": u.total / 1073741824.0,
                "pct": u.percent,
                "system": p.mountpoint.upper().rstrip("\\") == system_root,
            })
        # 系统盘永远排第一，其余按容量倒序
        disks.sort(key=lambda d: (not d["system"], -d["total_gb"]))
        snap["disks"] = disks
        snap["disk_io"] = {"read_mbs": rd, "write_mbs": wr, "temps": temps}

        # ---- 网络
        try:
            nio = psutil.net_io_counters()
        except Exception:
            nio = None
        up = down = 0.0
        if nio is not None and self._prev_net is not None and self._prev_t:
            dt = max(1e-3, now - self._prev_t)
            up = max(0.0, (nio.bytes_sent - self._prev_net.bytes_sent) / dt / 1048576.0)
            down = max(0.0, (nio.bytes_recv - self._prev_net.bytes_recv) / dt / 1048576.0)
        self._prev_net = nio
        self._prev_t = now
        snap["net"] = {
            "up_mbs": up,
            "down_mbs": down,
            "total_sent_gb": (nio.bytes_sent / 1073741824.0) if nio else 0.0,
            "total_recv_gb": (nio.bytes_recv / 1073741824.0) if nio else 0.0,
        }

        # ---- 进程排行
        # PDH 一次查询就能拿到全部进程，约 7ms，可以每帧都刷新；
        # psutil 逐进程读取要 1 秒以上，只能降频采样。
        if self.syscounters is not None:
            try:
                self._procs = self.syscounters.processes(self.top_n)
            except Exception:
                pass
        else:
            self._proc_tick += 1
            step = max(1, int(self.proc_ms / max(1, self.poll_ms)))
            if self._proc_tick % step == 0:
                try:
                    self._procs = self._sample_procs()
                except Exception:
                    pass
        snap["procs"] = self._procs
        snap["uptime"] = now - psutil.boot_time()

        # ---- 历史
        self.hist_cpu.append(cpu_load)
        self.hist_gpu.append(snap["gpu"]["load"] or 0.0)
        self.hist_mem.append(vm.percent)
        self.hist_vram.append(vram_pct or 0.0)
        snap["hist"] = {
            "cpu": list(self.hist_cpu),
            "gpu": list(self.hist_gpu),
            "mem": list(self.hist_mem),
            "vram": list(self.hist_vram),
        }

        if not b.ready and self.enable_lhm and not self.notes:
            with self._lock:
                self.notes.append("传感器桥接器尚未就绪")
        return snap

    # ---------------------------------------------------------------- 进程
    def _sample_procs(self) -> list:
        cache = self._proc_cache
        fresh = {}
        agg: dict = {}
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            pid = p.pid
            try:
                obj = cache.get(pid)
                if obj is None:
                    obj = p
                    try:
                        obj.cpu_percent(None)
                    except Exception:
                        pass
                    cpu = 0.0
                else:
                    cpu = obj.cpu_percent(None)
                fresh[pid] = obj
                mi = p.info.get("memory_info")
                mem = (mi.rss / 1048576.0) if mi else 0.0
                name = p.info.get("name") or f"PID {pid}"
                row = agg.get(name)
                if row is None:
                    agg[name] = {"name": name, "pid": pid, "cpu": cpu, "mem_mb": mem, "count": 1}
                else:
                    row["cpu"] += cpu
                    row["mem_mb"] += mem
                    row["count"] += 1
                    if mem > 0:
                        row["pid"] = pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        self._proc_cache = fresh
        rows = list(agg.values())
        for r in rows:
            r["cpu"] = r["cpu"] / self._ncpu     # 折算成“占整机百分比”，与任务管理器一致
        rows.sort(key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
        return rows[: max(1, self.top_n)]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _num_suffix(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def _lhm_hw_name(bridge: LhmBridge, key) -> str | None:
    if key is None:
        return None
    with bridge._lock:
        m = bridge.sensor_meta.get(key)
    return m["hw_name"] if m else None


_CPU_NAME_CACHE = None


def psutil_name_cpu() -> str:
    global _CPU_NAME_CACHE
    if _CPU_NAME_CACHE:
        return _CPU_NAME_CACHE
    name = "CPU"
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        winreg.CloseKey(key)
    except Exception:
        pass
    _CPU_NAME_CACHE = name
    return name


def _short_cpu_name(name: str) -> str:
    if not name:
        return "CPU"
    n = name.replace("(R)", "").replace("(TM)", "").replace("(tm)", "")
    n = n.replace("Processor", "").replace("CPU", "")
    # 去掉 “8-Core” / “16-Core” 之类的后缀，排行榜里更好看
    n = re.sub(r"\b\d+-Core\b", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n or name


def _short_gpu_name(name: str) -> str:
    if not name:
        return "GPU"
    n = name.replace("(R)", "").replace("(TM)", "").replace("(tm)", "")
    n = n.replace("Graphics Adapter", "").replace("Graphics", "")
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n or name


_GPU_NAME_CACHE = None


def _win_gpu_name() -> str:
    global _GPU_NAME_CACHE
    if _GPU_NAME_CACHE:
        return _GPU_NAME_CACHE
    name = "GPU"
    try:
        import subprocess as sp
        out = sp.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController | Where-Object { $_.AdapterRAM -gt 0 -or $_.Name -notmatch 'Virtual|MuMu|GameViewer' } | Select-Object -First 1).Name"],
            capture_output=True, text=True, timeout=8, creationflags=CREATE_NO_WINDOW,
        )
        if out.returncode == 0 and out.stdout.strip():
            name = out.stdout.strip()
    except Exception:
        pass
    _GPU_NAME_CACHE = name
    return name


_VRAM_CACHE = None


def _registry_vram_mb() -> float | None:
    """从注册表读显卡专用显存总量（MB）。LHM 拿不到时的兜底。"""
    global _VRAM_CACHE
    if _VRAM_CACHE is not None:
        return _VRAM_CACHE
    best = None
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as sk:
                        desc = winreg.QueryValueEx(sk, "DriverDesc")[0]
                        if "Virtual" in desc or "MuMu" in desc or "GameViewer" in desc:
                            continue
                        size = winreg.QueryValueEx(sk, "HardwareInformation.qwMemorySize")[0]
                        mb = size / 1048576.0
                        if mb > 0 and (best is None or mb > best):
                            best = mb
                except OSError:
                    continue
    except Exception:
        pass
    _VRAM_CACHE = best
    return best
