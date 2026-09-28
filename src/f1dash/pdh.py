# -*- coding: utf-8 -*-
"""Windows 性能计数器（PDH）轻量封装。

用 PDH 而不是逐进程调用 psutil，是因为 Windows 上 ``Process.memory_info()``
和 ``Process.cpu_times()`` 每次都要 OpenProcess，300 个进程要花掉将近 1 秒；
而 PDH 一次查询就能拿到全部实例，耗时通常在 1~3 毫秒。

这里用到两组计数器：

* ``\\Process(*)\\% Processor Time`` / ``\\Process(*)\\Working Set - Private``
  —— 进程排行
* ``\\GPU Engine(*)\\Utilization Percentage`` / ``\\GPU Adapter Memory(*)\\Dedicated Usage``
  —— 显卡占用与显存（LibreHardwareMonitor 不可用时的兜底数据源）
"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes as w

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOSCALE = 0x00001000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001

_INST_SUFFIX = re.compile(r"#\d+$")


class PDH_FMT_COUNTERVALUE_UNION(ctypes.Union):
    _fields_ = [
        ("longValue", w.LONG),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", w.LPCSTR),
        ("WideStringValue", w.LPWSTR),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", w.DWORD), ("u", PDH_FMT_COUNTERVALUE_UNION)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", w.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


ITEM_SIZE = ctypes.sizeof(PDH_FMT_COUNTERVALUE_ITEM_W)


_pdh = ctypes.WinDLL("pdh.dll")
# PDH_STATUS 虽然是 LONG，但错误码最高位为 1（例如 PDH_MORE_DATA = 0x800007D2），
# 用有符号类型接收会变成负数，因此这里统一按无符号 32 位处理。
_pdh.PdhOpenQueryW.argtypes = [w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhOpenQueryW.restype = ctypes.c_ulong
_pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhAddEnglishCounterW.restype = ctypes.c_ulong
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhCollectQueryData.restype = ctypes.c_ulong
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),
    ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W)]
_pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong
_pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
_pdh.PdhCloseQuery.restype = ctypes.c_ulong


class PdhQuery:
    """一组 PDH 计数器的集合，按固定周期采集。"""

    def __init__(self, paths: list[str]):
        self.h = ctypes.c_void_p()
        self.counters: dict[str, ctypes.c_void_p] = {}
        self.ok = False
        if _pdh.PdhOpenQueryW(None, None, ctypes.byref(self.h)) != 0:
            return
        for p in paths:
            ch = ctypes.c_void_p()
            if _pdh.PdhAddEnglishCounterW(self.h, p, None, ctypes.byref(ch)) == 0:
                self.counters[p] = ch
        self.ok = bool(self.counters)
        # 第一次采集只是建立基线
        if self.ok:
            _pdh.PdhCollectQueryData(self.h)

    def collect(self) -> bool:
        if not self.ok:
            return False
        return _pdh.PdhCollectQueryData(self.h) == 0

    def read(self, path: str) -> list[tuple[str, float]]:
        """读某个计数器的全部实例，返回 [(实例名, 数值), ...]。

        注意：``PdhGetFormattedCounterArrayW`` 回报的所需缓冲区大小比
        ``实例数 × sizeof(ITEM)`` 更大（PDH 会把实例名等开销算进去），
        所以这里按每项 64 字节再加余量分配，避免反复收到 PDH_MORE_DATA。
        """
        ch = self.counters.get(path)
        if ch is None:
            return []
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOSCALE
        size = w.DWORD(0)
        count = w.DWORD(0)
        ret = _pdh.PdhGetFormattedCounterArrayW(ch, fmt, ctypes.byref(size),
                                                ctypes.byref(count), None)
        if ret != PDH_MORE_DATA or count.value == 0:
            return []
        for _ in range(3):
            cap = max(64, int(size.value) // ITEM_SIZE + 4)
            buf = (PDH_FMT_COUNTERVALUE_ITEM_W * cap)()
            size = w.DWORD(ctypes.sizeof(buf))
            cnt = w.DWORD(0)
            ret = _pdh.PdhGetFormattedCounterArrayW(ch, fmt, ctypes.byref(size),
                                                    ctypes.byref(cnt), buf)
            if ret == 0:
                out = []
                for i in range(min(cnt.value, cap)):
                    item = buf[i]
                    status = item.FmtValue.CStatus
                    if status not in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
                        continue
                    name = item.szName
                    if not name:
                        continue
                    out.append((name, float(item.FmtValue.u.doubleValue)))
                return out
            if ret != PDH_MORE_DATA:
                return []
        return []

    def close(self):
        try:
            if self.h:
                _pdh.PdhCloseQuery(self.h)
        except Exception:
            pass
        self.h = ctypes.c_void_p()
        self.ok = False


PROC_CPU = r"\Process(*)\% Processor Time"
PROC_MEM = r"\Process(*)\Working Set - Private"
PROC_MEM_ALT = r"\Process(*)\Working Set"
GPU_UTIL = r"\GPU Engine(*)\Utilization Percentage"
GPU_VRAM = r"\GPU Adapter Memory(*)\Dedicated Usage"

# 这些“进程”不是真实应用，出现在排行榜里没有意义
EXCLUDED_PROCS = {"idle", "system", "secure system", "memory compression",
                  "registry", "vmmem", "vmmemwsl"}


class SystemCounters:
    """面向仪表盘的计数器门面。"""

    def __init__(self):
        self.query = PdhQuery([PROC_CPU, PROC_MEM, GPU_UTIL, GPU_VRAM])
        self.has_proc_mem = PROC_MEM in self.query.counters
        if not self.has_proc_mem and PROC_MEM_ALT in self.query.counters:
            self.has_proc_mem = True
        self.ncpu = 1
        try:
            import psutil
            self.ncpu = psutil.cpu_count(logical=True) or 1
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self.query.ok

    def processes(self, top_n: int = 12) -> list[dict]:
        """返回按 CPU 占用排序的进程（按可执行文件名合并多实例）。"""
        if not self.query.ok or not self.query.collect():
            return []
        mem_path = PROC_MEM if PROC_MEM in self.query.counters else PROC_MEM_ALT
        agg: dict[str, dict] = {}
        for inst, cpu in self.query.read(PROC_CPU):
            base = _INST_SUFFIX.sub("", inst)
            low = base.lower()
            if low in EXCLUDED_PROCS or low.endswith("_total"):
                continue
            row = agg.get(low)
            if row is None:
                row = {"key": low, "name": base, "cpu": 0.0, "mem_mb": 0.0, "count": 0}
                agg[low] = row
            row["cpu"] += max(0.0, cpu)
            row["count"] += 1
        # 注意：实例名可能重复（同名多进程不带 #N 后缀），必须逐条累加，
        # 一旦先转成 dict 就会互相覆盖。
        for inst, byts in self.query.read(mem_path):
            base = _INST_SUFFIX.sub("", inst)
            row = agg.get(base.lower())
            if row is None:
                continue
            row["mem_mb"] += max(0.0, byts) / 1048576.0
        rows = list(agg.values())
        for r in rows:
            r["cpu"] = r["cpu"] / self.ncpu          # 折算为“占整机百分比”
            r["name"] = _display_name(r["name"])
        rows.sort(key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
        return rows[: max(1, top_n)]

    def gpu(self) -> dict:
        """GPU 占用率（%）与专用显存（MB）。"""
        out: dict = {}
        if not self.query.ok:
            return out
        self.query.collect()
        util: dict[str, float] = {}
        for inst, pct in self.query.read(GPU_UTIL):
            luid = _luid_of(inst)
            if luid is None or "engtype_" not in inst:
                continue
            eng = inst.split("engtype_")[-1]
            if eng not in ("3D", "Compute", "VideoDecode", "VideoEncode", "Copy"):
                continue
            util[luid] = util.get(luid, 0.0) + max(0.0, pct)
        if util:
            best = max(util.items(), key=lambda kv: kv[1])
            out["luid"] = best[0]
            out["load"] = min(100.0, best[1])
        vram: dict[str, float] = {}
        for inst, byts in self.query.read(GPU_VRAM):
            luid = _luid_of(inst)
            if luid is None:
                continue
            vram[luid] = max(0.0, byts) / 1048576.0
        if out.get("luid") in vram:
            out["vram_used_mb"] = vram[out["luid"]]
        elif vram:
            best = max(vram.items(), key=lambda kv: kv[1])
            out["luid"] = best[0]
            out["vram_used_mb"] = best[1]
        return out

    def close(self):
        self.query.close()


def _luid_of(instance: str) -> str | None:
    m = re.search(r"luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+", instance)
    if not m:
        return None
    return m.group(0).upper().replace("LUID_", "")


def _display_name(base: str) -> str:
    if "." in base.split("#")[0][-5:]:
        return base
    return base + ".exe"
