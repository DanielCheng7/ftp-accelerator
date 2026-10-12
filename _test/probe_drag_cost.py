# -*- coding: utf-8 -*-
"""分解探针：窗口移动的开销到底花在哪一步？

对比两种移动机制（都是从当前 x 出发每步 +6px）：
  A. Tk 的 `root.geometry("+x+y")`   —— 现版本的做法
  B. 直接 `SetWindowPos`             —— 打算改成的做法
每一步分别量「发起调用」与「排空事件循环 update()」各自耗时，看清代价在哪。

用法：E:/Python/python.exe _test/probe_drag_cost.py
"""

import os
import sys
import time
import ctypes
import ctypes.wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tkinter as tk
import ftp_accel

N = 30
STEP = 6

u = ctypes.windll.user32
u.SetProcessDPIAware()

SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


def win_pos(hwnd):
    r = ctypes.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


def bench(name, move_fn, root, hwnd):
    """移动 N 次，分别累计「发起调用」与「update 排空」的耗时。"""
    x, y = win_pos(hwnd)
    call_ms = drain_ms = 0.0
    for _ in range(N):
        t0 = time.perf_counter()
        move_fn(x, y)
        call_ms += (time.perf_counter() - t0) * 1000
        x += STEP
        t1 = time.perf_counter()
        root.update()
        drain_ms += (time.perf_counter() - t1) * 1000
    print(f"  {name}")
    print(f"      发起调用 : {call_ms / N:.2f} ms/次")
    print(f"      排空事件 : {drain_ms / N:.2f} ms/次")
    print(f"      合计     : {(call_ms + drain_ms) / N:.2f} ms/次")
    return call_ms, drain_ms


def main():
    backup = None
    if os.path.exists(ftp_accel.CONFIG_FILE):
        with open(ftp_accel.CONFIG_FILE, "rb") as fh:
            backup = fh.read()

    prev_fg = u.GetForegroundWindow()
    root = tk.Tk()
    ftp_accel.apply_theme(root)
    app = ftp_accel.App(root)
    root.update()
    hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
    if prev_fg and u.GetForegroundWindow() == hwnd:
        u.SetForegroundWindow(prev_fg)

    print(f"窗口 {hwnd}  客户区 {root.winfo_width()}x{root.winfo_height()}")
    print(f"共移动 {N} 次，每次 {STEP} px\n")

    bench("A. root.geometry('+x+y')", lambda x, y: root.geometry(f"+{x}+{y}"), root, hwnd)

    # 拖动条上再量一次（真实场景就是从标题栏拖）
    bench("A'. 同上（窗口已挪过一段，模拟真实拖动中段）",
          lambda x, y: root.geometry(f"+{x}+{y}"), root, hwnd)

    bench("B. SetWindowPos(不激活/不改尺寸)",
          lambda x, y: u.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                                      SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE),
          root, hwnd)

    bench("B'. 同上（再量一遍）",
          lambda x, y: u.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                                      SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE),
          root, hwnd)

    root.destroy()
    if backup is not None:
        with open(ftp_accel.CONFIG_FILE, "wb") as fh:
            fh.write(backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
