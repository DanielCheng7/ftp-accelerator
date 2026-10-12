# -*- coding: utf-8 -*-
"""探针：窗口从启动到稳定，一共被观察到几种「尺寸/位置」？

用来判断有没有"开场闪一下"——即窗口先以小尺寸（Tk 默认）出现在屏幕上、随后才跳到
最终尺寸与居中位置。有的话用户会看到明显的跳动。

做法：以 5 ms 间隔轮询 GetWindowRect，记录所有出现过的矩形（去重）。

用法：E:/Python/python.exe _test/probe_launch_flash.py [exe路径]
"""

import os
import sys
import time
import ctypes
import ctypes.wintypes
import subprocess

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if len(sys.argv) > 1 and sys.argv[1] == "--src":
    CMD = [sys.executable, os.path.join(APP_DIR, "ftp_accel.py")]     # 直接跑源码，省去重打包
else:
    CMD = [sys.argv[1] if len(sys.argv) > 1
           else os.path.join(APP_DIR, "dist", "FTPAccelerator.exe")]

u = ctypes.windll.user32
u.SetProcessDPIAware()          # 不做 DPI 感知会量到被虚拟化的坐标
WM_CLOSE = 0x0010


def find_window():
    out = []

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if "加速下载器" in buf.value:
                out.append(hwnd)
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    u.EnumWindows(CB(cb), 0)
    return out[0] if out else None


def rect(hwnd):
    r = ctypes.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - r.left, r.bottom - r.top, r.left, r.top)


def main():
    print("启动：", " ".join(CMD))
    proc = subprocess.Popen(CMD)
    seen = {}
    hwnd = None
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < 8.0:
            if hwnd is None:
                hwnd = find_window()
            if hwnd:
                r = rect(hwnd)
                if r not in seen:
                    seen[r] = time.perf_counter() - t0
            time.sleep(0.005)
    finally:
        if hwnd:
            u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.8)
        try:
            proc.terminate()
        except Exception:
            pass

    print("观察到的不同「宽x高 @ x,y」（按出现时间）：")
    for (w, h, x, y), t in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  {t * 1000:7.0f} ms   {w}x{h} @ {x},{y}")

    sizes = {k[:2] for k in seen}
    print()
    if len(seen) <= 1:
        print("结论：全程只有一个矩形 —— 没有开场跳动")
    else:
        print(f"结论：出现 {len(seen)} 个不同矩形、{len(sizes)} 种尺寸 —— 有跳动（用户会看到窗口跳一下）")
    return 0 if len(seen) <= 1 else 2


if __name__ == "__main__":
    sys.exit(main())
