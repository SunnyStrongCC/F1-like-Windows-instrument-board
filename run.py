# -*- coding: utf-8 -*-
"""F1 遥测仪表盘启动入口。

用法::

    pythonw run.py              正常启动图形界面
    python  run.py --selftest   不开窗口，打印一次采集结果（排查用）
    python  run.py --dump       打印传感器原始清单
"""
from __future__ import annotations

import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))


def _selftest():
    from f1dash.sensors import SensorHub
    hub = SensorHub(BASE_DIR, {"poll_ms": 1000, "process_ms": 2000, "top_processes": 8})
    hub.start()
    print("采集 4 秒 ...\n")
    time.sleep(4.2)
    snap = hub.snapshot()
    hub.stop()
    printable = {k: v for k, v in snap.items() if k != "hist"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))


def _dump():
    from f1dash.sensors import LhmBridge
    exe = os.path.join(BASE_DIR, "vendor", "bridge", "SensorBridge.exe")
    b = LhmBridge(exe, 1000)
    b.start()
    t0 = time.time()
    while time.time() - t0 < 3 and not b.ready:
        b.pump()
        time.sleep(0.1)
    b.pump()
    print(f"{'HW TYPE':<12} {'HARDWARE':<28} {'SENSOR':<30} {'TYPE':<12} VALUE")
    print("-" * 100)
    for (hw, si), m in sorted(b.sensor_meta.items()):
        v = b.get((hw, si))
        print(f"{m['hw_type']:<12} {m['hw_name'][:28]:<28} {m['name'][:30]:<30} "
              f"{m['type']:<12} {'-' if v is None else round(v, 3)}")
    b.stop()


def main():
    args = set(sys.argv[1:])
    if "--dump" in args:
        _dump()
        return
    if "--selftest" in args or "--probe" in args:
        _selftest()
        return

    from f1dash.app import main as run_app

    # 记录 PID，方便“停止仪表盘”脚本精确结束进程
    try:
        with open(os.path.join(BASE_DIR, "logs", "f1dash.pid"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass
    run_app(BASE_DIR)


if __name__ == "__main__":
    main()
