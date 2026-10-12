# -*- coding: utf-8 -*-
"""窗口拖动性能对比：拖动逻辑处理「鼠标事件积压」要花多少时间。

为什么不用真实点击驱动：本机前台锁（`SetForegroundWindow` 返回 0）会让注入的点击落到别的窗口上
（实测被「词枢·英语记词器」抢走）—— 会打断用户，也复现不了。改用**进程内合成事件**：
`event_generate` 只是把事件**入队**，处理发生在 `update()` —— 正好可以精确计时。

两种测法：
  ① 积压模式：一次性排 90 个 <B1-Motion>，再用**一次** `update()` 排空，量总耗时
     （真实拖动里鼠标 125Hz、每个事件处理要几毫秒时，就是这个场景 -> 窗口跟不上光标）
  ② 逐步模式：一次一个事件 + update，量「窗口实际移动了多少个不同位置」

用法：E:/Python/python.exe _test/bench_drag.py
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

STEPS = 90
STEP_PX = 6

u = ctypes.windll.user32
u.SetProcessDPIAware()
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

MOVE_CALLS = [0]                     # 经 ctypes 发出的窗口移动次数
_real_setwindowpos = u.SetWindowPos


def _counting_setwindowpos(hwnd, after, x, y, cx, cy, flags):
    MOVE_CALLS[0] += 1
    return _real_setwindowpos(hwnd, after, x, y, cx, cy, flags)


def win_pos(hwnd):
    r = ctypes.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


def cursor():
    p = ctypes.wintypes.POINT()
    u.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def find_titlebar(app, root):
    """取标题栏控件：优先用 App 暴露的，旧版没有就按底色认。"""
    bar = getattr(app, "titlebar", None)
    if bar is not None:
        return bar
    for w in root.winfo_children():
        try:
            if isinstance(w, tk.Frame) and str(w.cget("bg")).lower() == ftp_accel.TITLEBAR_BG.lower():
                return w
        except Exception:
            continue
    return None


def main():
    backup = None
    if os.path.exists(ftp_accel.CONFIG_FILE):
        with open(ftp_accel.CONFIG_FILE, "rb") as fh:
            backup = fh.read()

    restore_cursor = cursor()
    prev_fg = u.GetForegroundWindow()

    root = tk.Tk()
    ftp_accel.apply_theme(root)
    app = ftp_accel.App(root)
    root.update()

    hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
    # 测试窗口会抢焦点，跑完还回去（此刻本进程已是前台，SetForegroundWindow 允许）
    if prev_fg and u.GetForegroundWindow() == hwnd:
        u.SetForegroundWindow(prev_fg)

    x0, y0 = win_pos(hwnd)
    print(f"窗口 hwnd   : {hwnd}")
    print(f"客户区      : {root.winfo_width()} x {root.winfo_height()}")
    print(f"是否被抢焦点: {'是（注意）' if u.GetForegroundWindow() == hwnd else '否'}")

    gx, gy = x0 + 400, y0 + 18          # 抓标题栏中部，避开圆点
    u.SetCursorPos(gx, gy)
    root.update()
    time.sleep(0.2)

    bar = find_titlebar(app, root)
    if bar is None:
        print("!! 找不到标题栏控件，无法派发事件")
        root.destroy()
        return 1

    bar.event_generate("<Button-1>", x=400, y=18, rootx=gx, rooty=gy)
    root.update()

    # ---------- (1) 积压模式 ----------
    u.SetWindowPos = _counting_setwindowpos      # 装上计数器（跑完恢复）
    MOVE_CALLS[0] = 0
    for i in range(1, STEPS + 1):
        u.SetCursorPos(gx + i * STEP_PX, gy)
        bar.event_generate("<B1-Motion>", x=400 + i * STEP_PX, y=18,
                           rootx=gx + i * STEP_PX, rooty=gy)
    queued_pos = win_pos(hwnd)

    cpu0 = time.process_time()
    t0 = time.perf_counter()
    root.update()                        # 一次排空全部已入队事件
    drain_ms = (time.perf_counter() - t0) * 1000
    drain_cpu = (time.process_time() - cpu0) * 1000
    moved_calls = MOVE_CALLS[0]

    time.sleep(0.3)
    after_pos = win_pos(hwnd)

    # ---------- (2) 逐步模式 ----------
    x1, y1 = win_pos(hwnd)
    gx2, gy2 = x1 + 400, y1 + 18
    u.SetCursorPos(gx2, gy2)
    root.update()
    bar.event_generate("<Button-1>", x=400, y=18, rootx=gx2, rooty=gy2)
    root.update()

    positions = []
    MOVE_CALLS[0] = 0
    cpu1 = time.process_time()
    t1 = time.perf_counter()
    for i in range(1, STEPS + 1):
        u.SetCursorPos(gx2 + i * STEP_PX, gy2)
        bar.event_generate("<B1-Motion>", x=400 + i * STEP_PX, y=18,
                           rootx=gx2 + i * STEP_PX, rooty=gy2)
        root.update()
        positions.append(win_pos(hwnd)[0])
    step_ms = (time.perf_counter() - t1) * 1000
    step_cpu = (time.process_time() - cpu1) * 1000
    step_calls = MOVE_CALLS[0]
    u.SetWindowPos = _real_setwindowpos

    jumps = [abs(b - a) for a, b in zip(positions, positions[1:])]

    print()
    print(f"(1) 积压模式：入队 {STEPS} 个 <B1-Motion>（光标共 {STEPS * STEP_PX} px），一次 update 排空")
    print(f"     入队完毕时位移 : {queued_pos[0] - x0} px   <- 事件还没处理，窗口没动")
    print(f"     排空耗时       : {drain_ms:.1f} ms    （CPU {drain_cpu:.1f} ms）")
    print(f"     折算每事件     : {drain_ms / STEPS:.2f} ms")
    print(f"     经 ctypes 的移动调用: {moved_calls} 次")
    print(f"     排空后位移     : {after_pos[0] - x0} px   （应为 {STEPS * STEP_PX} px，误差 {abs(after_pos[0] - (x0 + STEPS * STEP_PX))} px）")
    print()
    print("(2) 逐步模式：一次一个事件 + update")
    print(f"     总耗时         : {step_ms:.1f} ms    （CPU {step_cpu:.1f} ms）")
    print(f"     折算每事件     : {step_ms / STEPS:.2f} ms")
    print(f"     实际移动位置   : {len(set(positions))} 个 / {STEPS} 步")
    print(f"     经 ctypes 的移动调用: {step_calls} 次")
    print(f"     单步最大跳幅   : {max(jumps) if jumps else 0} px（注入步长 {STEP_PX} px）")

    u.SetCursorPos(*restore_cursor)
    root.destroy()

    if backup is not None:
        with open(ftp_accel.CONFIG_FILE, "wb") as fh:
            fh.write(backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
