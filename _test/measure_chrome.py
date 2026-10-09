# -*- coding: utf-8 -*-
"""实测运行中窗口的非客户区，确认仿 Mac 去标题栏后顶部没有残留空栏。

判定标准：上/左/右/下 四个边框宽度应彼此接近（都是同一层缩放边框）。
若顶部明显大于左右，说明旧标题栏区域还留着一块空白。
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes
import subprocess

EXE = sys.argv[1] if len(sys.argv) > 1 else "dist/FTPAccelerator.exe"
u = ctypes.windll.user32

proc = subprocess.Popen([EXE])


def find_window():
    found = []

    def cb(hwnd, lparam):
        if u.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, buf, 256)
            if "加速下载器" in buf.value:
                found.append(hwnd)
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    u.EnumWindows(CB(cb), 0)
    return found[0] if found else None


hwnd = None
for _ in range(40):
    time.sleep(0.5)
    hwnd = find_window()
    if hwnd:
        break

if not hwnd:
    print("!! 没找到窗口")
    proc.kill()
    sys.exit(1)

time.sleep(1.5)   # 等 strip_window_chrome 跑完

wr = ctypes.wintypes.RECT()
u.GetWindowRect(hwnd, ctypes.byref(wr))
cr = ctypes.wintypes.RECT()
u.GetClientRect(hwnd, ctypes.byref(cr))

pt = ctypes.wintypes.POINT(0, 0)
u.ClientToScreen(hwnd, ctypes.byref(pt))

win_w, win_h = wr.right - wr.left, wr.bottom - wr.top
cli_w, cli_h = cr.right - cr.left, cr.bottom - cr.top
top = pt.y - wr.top
left = pt.x - wr.left
right = wr.right - (pt.x + cli_w)
bottom = wr.bottom - (pt.y + cli_h)

print(f"窗口矩形   : {wr.left},{wr.top} -> {wr.right},{wr.bottom}  ({win_w}x{win_h})")
print(f"客户区原点 : {pt.x},{pt.y}   客户区尺寸 {cli_w}x{cli_h}")
print(f"非客户区边框: 上 {top}px  左 {left}px  右 {right}px  下 {bottom}px")

ok = top <= max(left, right, bottom) + 2
print("判定:", "✓ 顶部无残留标题栏空栏（上边框与其余三边一致）" if ok
      else f"✗ 顶部仍多出 {top - max(left, right, bottom)}px 空栏")

proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()
sys.exit(0 if ok else 2)
