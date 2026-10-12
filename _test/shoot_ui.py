# -*- coding: utf-8 -*-
"""起一次打包好的 exe：核对窗口标题与版本、量非客户区（去标题栏有没有残留），并重拍界面截图。

⚠️ 两个坑（都踩过）：
  1) 本脚本若不做 DPI 感知，`GetWindowRect` 拿到的是被系统虚拟化过的尺寸
     （本机 150% 缩放 → 1322x856 显示成 881x571），据此抓图会抓错区域。
  2) 用 `ImageGrab` 抓屏幕会被别的窗口挡住 → 改用 `PrintWindow(PW_RENDERFULLCONTENT)`
     直接把窗口渲染进位图，遮挡不影响。

运行：E:\\Python\\python.exe _test\\shoot_ui.py [exe路径]
产物：apps/ftp-download/screenshot.png（README 引用）
"""

import os
import sys
import json
import time
import shutil
import ctypes
import ctypes.wintypes
import tempfile
import subprocess

# 必须在任何 GUI/截图库之前声明「系统 DPI 感知」，否则坐标全是被虚拟化过的
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(APP_DIR, "dist", "FTPAccelerator.exe")
OUT = os.path.join(APP_DIR, "screenshot.png")

u = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
WM_CLOSE = 0x0010
PW_RENDERFULLCONTENT = 0x00000002

# ⚠️ README 是公开的，截图里绝不能出现用户的真实账号名。
# 所以用一份**临时** APPDATA 启动 exe：既不动用户真实配置，也保证画面是干净的默认值。
DEMO_CONFIG = {
    "host": "ftp2.tpdc.ac.cn",
    "port": "6201",
    "user": "your_account",
    "save_dir": "D:/Download",
    "mirror": "ftp3.tpdc.ac.cn",
    "conn": "8",
    "jobs": "1",
    "autoretry": True,
    "max_retry": "20",
}


def make_demo_appdata():
    """造一个临时 APPDATA，里面放好演示用的 config.json，返回路径。"""
    base = tempfile.mkdtemp(prefix="ftp_shot_")
    cfg_dir = os.path.join(base, "FTPAccelerator")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(DEMO_CONFIG, fh, ensure_ascii=False, indent=2)
    return base


def text_of(hwnd):
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_window(title_hint="FTP", timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        found = []

        def cb(hwnd, _lparam):
            if u.IsWindowVisible(hwnd):
                t = text_of(hwnd)
                if title_hint in t:
                    found.append((hwnd, t))
            return True

        u.EnumWindows(WNDENUMPROC(cb), 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def grab_window(hwnd, path, crop=(10, 10, 10, 10)):
    """把窗口本身渲染成位图（PrintWindow），被别的窗口挡住也能拍全。

    crop = (上, 左, 右, 下) 要裁掉的边框（只留客户区），README 里更干净。
    """
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src_dc, w, h)
    mem_dc.SelectObject(bmp)
    try:
        u.PrintWindow(hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bits, "raw", "BGRX", 0, 1)
        if crop:
            t, l, r, b = crop
            img = img.crop((l, t, info["bmWidth"] - r, info["bmHeight"] - b))
        img.save(path)
        return img.size
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            mem_dc.DeleteDC()
        except Exception:
            pass
        try:
            src_dc.DeleteDC()
        except Exception:
            pass
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def main():
    if not os.path.exists(EXE):
        print("找不到 exe：", EXE)
        return 1
    print(f"启动：{EXE}")
    demo_appdata = make_demo_appdata()
    proc = subprocess.Popen([os.path.abspath(EXE)], env=dict(os.environ, APPDATA=demo_appdata))
    ok = True
    try:
        hit = find_window()
        if not hit:
            print("!! 没找到窗口")
            return 1
        hwnd, title = hit
        print(f"窗口标题：{title!r}")
        if "v1.7.0" not in title:
            print("!! 标题里没有 v1.7.0")
            ok = False

        # 挪到左上角拍图；带 SWP_NOACTIVATE，别去抢用户当前窗口的焦点
        u.SetWindowPos(hwnd, 0, 60, 60, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(2.0)

        rect = ctypes.wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        crect = ctypes.wintypes.RECT()
        u.GetClientRect(hwnd, ctypes.byref(crect))
        origin = ctypes.wintypes.POINT(0, 0)
        u.ClientToScreen(hwnd, ctypes.byref(origin))
        frame = {
            "上": origin.y - rect.top,
            "左": origin.x - rect.left,
            "右": rect.right - (origin.x + crect.right),
            "下": rect.bottom - (origin.y + crect.bottom),
        }
        print(f"窗口 {rect.right - rect.left}x{rect.bottom - rect.top}，"
              f"客户区 {crect.right - crect.left}x{crect.bottom - crect.top}")
        print(f"非客户区（四周，物理像素）：{frame}")
        if max(frame.values()) - min(frame.values()) > 6 or frame["上"] > 14:
            print("!! 四周非客户区差异过大（顶部可能残留标题栏空白）")
            ok = False

        size = grab_window(hwnd, OUT, crop=(frame["上"], frame["左"], frame["右"], frame["下"]))
        print(f"截图已保存：{OUT}  {size[0]}x{size[1]}")
        if size[0] < 1200 or size[1] < 700:
            print("!! 截图尺寸偏小，八成又抓错区域了")
            ok = False

        u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(1.0)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(demo_appdata, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
