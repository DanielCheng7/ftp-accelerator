# -*- coding: utf-8 -*-
"""窗口尺寸 / 标题栏布局 / 拖动行为 的回归测试。

要点：拖动改走 Win32（SetWindowPos + GetCursorPos 现读）之后，
必须证明三件事 —— ① 位置算得对；② 事件积压时会被合并，不做无用功；
③ 最大化状态下能先还原再拖（最大化的窗口系统会忽略移动请求）。

只建一个 Tk 解释器（同进程反复建/销会 Tcl_AsyncDelete 硬崩）。

用法：E:/Python/python.exe _test/test_window_drag.py
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

u = ctypes.windll.user32
u.SetProcessDPIAware()
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

PASS, FAIL = [], []
MOVES = [0]
_real_setwindowpos = u.SetWindowPos


def _count_move(hwnd, after, x, y, cx, cy, flags):
    MOVES[0] += 1
    return _real_setwindowpos(hwnd, after, x, y, cx, cy, flags)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'BAD'}] {name}" + (f"  {detail}" if detail else ""))


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
    hwnd = app._hwnd
    if prev_fg and u.GetForegroundWindow() == hwnd:
        u.SetForegroundWindow(prev_fg)
    time.sleep(0.5)
    root.update()

    # ---------------- ① 默认窗口尺寸与位置 ----------------
    print("— 默认窗口尺寸 / 位置 —")
    x, y, w, h = ftp_accel.win_rect(hwnd)
    client_w = root.winfo_width()
    client_h = root.winfo_height()
    check("客户区宽度 = 1600", abs(client_w - ftp_accel.WIN_W) <= 2, f"实际 {client_w}")
    check("客户区高度 = 1000", abs(client_h - ftp_accel.WIN_H) <= 2, f"实际 {client_h}")
    screen_w = u.GetSystemMetrics(0)
    check("水平居中", abs(x - (screen_w - w) // 2) <= 6, f"x={x} 期望 {(screen_w - w) // 2}")
    check("大于旧默认（1302x800）", w > 1302 + 200 and h > 800 + 150, f"外框 {w}x{h}")

    # ---------------- ② 圆点在右侧 ----------------
    print("— 标题栏圆点位置 —")
    bar = app.titlebar
    dots = [c for c in bar.winfo_children() if isinstance(c, tk.Canvas)]
    check("找到圆点画布", len(dots) == 1, f"找到 {len(dots)} 个")
    if dots:
        d = dots[0]
        bar_w = bar.winfo_width()
        cx = d.winfo_x() + d.winfo_width() / 2
        check("圆点画布在标题栏右半侧", cx > bar_w * 0.75, f"中心 {cx:.0f} / 栏宽 {bar_w}")
        check("圆点画布贴右边（右侧留白 14px）",
              abs((bar_w - (d.winfo_x() + d.winfo_width())) - 14) <= 3,
              f"右侧留白 {bar_w - (d.winfo_x() + d.winfo_width())}px")
    # 标题仍然居中
    labels = [c for c in bar.winfo_children() if isinstance(c, tk.Label)]
    check("标题仍是居中放置", len(labels) == 1 and labels[0].place_info().get("relx") == "0.5")

    # ---------------- ③ 拖动：位置算得对 ----------------
    print("— 拖动：位移与光标一致 —")
    ui_before = (root.winfo_x(), root.winfo_y())
    x0, y0 = ftp_accel.win_rect(hwnd)[:2]
    gx, gy = x0 + 400, y0 + 18          # 标题栏中部，避开圆点
    u.SetCursorPos(gx, gy)
    root.update()

    STEPS, STEP_PX = 90, 6
    bar.event_generate("<Button-1>", x=400, y=18, rootx=gx, rooty=gy)
    check("拖动会话已建立", app._drag is not None)
    for i in range(1, STEPS + 1):
        u.SetCursorPos(gx + i * STEP_PX, gy)
        bar.event_generate("<B1-Motion>", x=400 + i * STEP_PX, y=18,
                           rootx=gx + i * STEP_PX, rooty=gy)
        root.update()
    bar.event_generate("<ButtonRelease-1>", x=400, y=18, rootx=gx, rooty=gy)
    root.update()

    fx, fy = ftp_accel.win_rect(hwnd)[:2]
    check("水平位移 = 光标位移", abs((fx - x0) - STEPS * STEP_PX) <= 2,
          f"实际 {fx - x0}px 期望 {STEPS * STEP_PX}px")
    check("垂直未漂移", abs(fy - y0) <= 2, f"实际 {fy - y0}px")
    check("拖动结束后释放了会话", app._drag is None)
    ui_after = (root.winfo_x(), root.winfo_y())
    check("Tk 自己报告的几何也跟着更新了（否则后续还原会跳回旧位置）",
          abs(ui_after[0] - (ui_before[0] + STEPS * STEP_PX)) <= 4,
          f"winfo_x {ui_before[0]} -> {ui_after[0]}")

    # ---------------- ④ 事件积压要合并 ----------------
    print("— 事件积压合并 —")
    bx0 = ftp_accel.win_rect(hwnd)[0]
    gx2, gy2 = bx0 + 400, ftp_accel.win_rect(hwnd)[1] + 18
    u.SetCursorPos(gx2, gy2)
    root.update()
    bar.event_generate("<Button-1>", x=400, y=18, rootx=gx2, rooty=gy2)
    root.update()

    u.SetWindowPos = _count_move
    MOVES[0] = 0
    N = 60
    for i in range(1, N + 1):
        u.SetCursorPos(gx2 + i * STEP_PX, gy2)
        bar.event_generate("<B1-Motion>", x=400 + i * STEP_PX, y=18,
                           rootx=gx2 + i * STEP_PX, rooty=gy2)
    root.update()
    u.SetWindowPos = _real_setwindowpos
    bar.event_generate("<ButtonRelease-1>", x=400, y=18, rootx=gx2, rooty=gy2)
    root.update()

    check(f"{N} 个事件被合并成 1 次移动", MOVES[0] == 1, f"实际移动 {MOVES[0]} 次")
    bx1 = ftp_accel.win_rect(hwnd)[0]
    check("合并后位置仍然正确", abs((bx1 - bx0) - N * STEP_PX) <= 2, f"实际 {bx1 - bx0}px")

    # ---------------- ⑤ 最大化状态下拖动要先还原 ----------------
    print("— 最大化 → 拖动应先还原 —")
    before = ftp_accel.win_rect(hwnd)
    app._toggle_maximize()
    root.update()
    time.sleep(0.3)
    check("已最大化", ftp_accel.is_zoomed(hwnd))

    mx = ftp_accel.win_rect(hwnd)[0]
    gx3, gy3 = mx + 400, ftp_accel.win_rect(hwnd)[1] + 18
    u.SetCursorPos(gx3, gy3)
    root.update()
    bar.event_generate("<Button-1>", x=400, y=18, rootx=gx3, rooty=gy3)
    root.update()
    check("拖动态已退出最大化", not ftp_accel.is_zoomed(hwnd))
    root.update()
    time.sleep(0.3)
    rx = ftp_accel.win_rect(hwnd)[0]        # 还原之后的位置才是叠加基准
    u.SetCursorPos(gx3 + 120, gy3)
    bar.event_generate("<B1-Motion>", x=520, y=18, rootx=gx3 + 120, rooty=gy3)
    root.update()
    bar.event_generate("<ButtonRelease-1>", x=520, y=18, rootx=gx3 + 120, rooty=gy3)
    root.update()
    moved = ftp_accel.win_rect(hwnd)[0] - rx
    check("还原后能正常拖动", abs(moved - 120) <= 3, f"实际位移 {moved}px 期望 120px")

    # ---------------- ⑥ 最大化/还原往返 ----------------
    print("— 最大化/还原往返 —")
    app._toggle_maximize()          # 再最大化
    root.update()
    time.sleep(0.3)
    check("再次最大化", ftp_accel.is_zoomed(hwnd))
    app._toggle_maximize()          # 还原
    root.update()
    time.sleep(0.3)
    check("还原后不再最大化", not ftp_accel.is_zoomed(hwnd))
    rr = ftp_accel.win_rect(hwnd)
    check("还原后尺寸回到 1600x1000 附近",
          abs(rr[2] - before[2]) <= 20 and abs(rr[3] - before[3]) <= 20,
          f"实际 {rr[2]}x{rr[3]} 期望 {before[2]}x{before[3]}")

    root.destroy()
    if backup is not None:
        with open(ftp_accel.CONFIG_FILE, "wb") as fh:
            fh.write(backup)

    print(f"\n通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("未通过：" + "、".join(FAIL))
    print("RESULT:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
