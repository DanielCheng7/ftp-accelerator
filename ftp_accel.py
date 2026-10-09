# -*- coding: utf-8 -*-
"""
FTP 加速下载器 (FTP Accelerator)

面向科研数据（如国家青藏高原科学数据中心 TPDC）的 FTP 多线程分段下载工具。
内置 aria2 引擎，图形界面：连接 FTP -> 浏览远程目录 -> 勾选文件 -> 多线程加速下载。

为什么快：普通 FTP 客户端（含 FileZilla）只能多文件并发，无法把单个大文件切开。
本工具通过 aria2 把一个大文件分成 N 段并行拉取，绕开服务器的单连接限速。

依赖：Python 3.8+（仅标准库），tkinter
引擎：aria2 1.37.0（third_party/aria2/aria2c.exe，GPLv2，见同目录 COPYING）
许可：MIT（本程序），aria2 引擎遵循其自身 GPLv2 许可
"""

import json
import os
import sys
import time
import uuid
import queue
import socket
import ftplib
import tempfile
import threading
import subprocess
import urllib.parse
import urllib.request
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

# ================================ 常量配置 ================================

APP_NAME = "FTP 加速下载器"
APP_VERSION = "1.5.0"

RPC_TIMEOUT = 10          # 单次 RPC 调用超时（秒）

DEFAULT_HOST = "ftp2.tpdc.ac.cn"   # 默认按 TPDC 填好，可改
DEFAULT_PORT = "6201"              # TPDC 的 FTP 端口不是 21
DEFAULT_CONN = "8"                 # 每文件连接数（分段数）
DEFAULT_JOBS = "1"                 # 同时下载文件数
MAX_CONN = 16                      # aria2 的 max-connection-per-server 硬上限就是 16，超了任务会被直接拒绝
MAX_JOBS = 10                      # 同时下载文件数上限

POLL_MS = 1000            # 队列刷新间隔（毫秒）

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "FTPAccelerator")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---- 设计令牌（与 word-hub / coord-kit / life-map 四应用共用那一套）----
BG = "#f5f5f7"            # --bg   页面背景
SURFACE = "#ffffff"       # --card 卡片
SURFACE_ALT = "#ececf0"   # 次级表面 / hover（近似 --soft2）
BORDER = "#e1e1e3"        # --line 细边框（rgba(0,0,0,.08) 的等效实色）
TEXT = "#1d1d1f"          # --text 正文
TEXT_MUTED = "#56565c"    # --muted 次要文字
BRAND = "#0071e3"         # --brand 品牌蓝
BRAND_HOVER = "#1a82e6"
BRAND_PRESS = "#0062c4"
BRAND_SOFT = "#e8f1fc"    # 近似 --brand-soft
SEL_BG = "#e8f1fc"        # 选中行

FG_OK = "#34c759"         # --ok   下载中
FG_DONE = "#56565c"       # 完成（用 muted）
FG_ERR = "#ff3b30"        # --err  出错
FG_WARN = "#ff9500"       # --warn 暂停

FONT_UI_FAMILY = "Microsoft YaHei UI"   # apply_theme() 会按字体栈挑本机可用的
FONT_UI = (FONT_UI_FAMILY, 9)
FONT_MONO = ("Consolas", 9)

# ---- 仿 Mac 窗口 ----
TITLEBAR_BG = "#f0f0f2"   # 标题栏（比画布略深一档）
DOT_CLOSE = "#FF5F57"     # 红：关闭
DOT_MIN = "#FEBC2E"       # 黄：最小化
DOT_MAX = "#28C840"       # 绿：最大化 / 还原
TITLEBAR_H = 38           # 标题栏高度


def resource_path(rel):
    """资源定位：兼容开发态与 PyInstaller 打包态。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


ARIA2_EXE = resource_path(os.path.join("third_party", "aria2", "aria2c.exe"))


def human_size(n):
    """字节数转可读字符串。"""
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"


def human_size_short(n):
    """表格用的紧凑体积（1.2M / 48.3G）—— 列宽紧张时用。"""
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)}B"
    for unit in ("K", "M", "G", "T"):
        n /= 1024
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}"


def human_duration(sec):
    """秒数转粗略的中文时长。"""
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分"
    if sec < 86400:
        return f"{sec // 3600}时{sec % 3600 // 60}分"
    return f"{sec // 86400}天{sec % 86400 // 3600}时"


def join_remote(base, name):
    """拼接 FTP 远程路径。"""
    if not base:
        base = "/"
    if base.endswith("/"):
        return f"{base}{name}"
    return f"{base}/{name}"


def free_port():
    """取一个本机空闲端口。

    不用固定端口：上次异常退出残留的 aria2c 会一直占着老端口，
    导致本次启动直接失败，而且现象是「启动超时」，很难排查。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clamp_int(text, lo, hi, default):
    """把界面上读到的数字收敛到合法区间。

    ttk.Spinbox 的输入框是可以手打的，`from_/to=` 只约束箭头不约束键盘。
    一旦手输 20，aria2 会以「max-connection-per-server 必须在 1-16 之间」为由
    直接把整个任务拒掉 —— 所以在送进引擎之前先钳住。
    """
    try:
        value = int(float(str(text).strip()))
    except (TypeError, ValueError):
        value = int(default)
    return max(lo, min(hi, value))


def build_download_options(save_dir, conn, user="", pwd=""):
    """构造每个下载任务传给 aria2 的选项。

    单独抽出来是为了让「应用实际用的参数」和「测试断言用的参数」是同一份，
    否则测试很容易在自己复制的一份参数上通过，而应用里早就改歪了。
    取值依据见 third_party/aria2/aria2c.exe --help=#all。
    """
    conn = str(conn)
    opts = {
        "dir": save_dir,
        "split": conn,
        # aria2 硬上限 16（-x 的 Possible Values 就是 1-16），超了任务会被直接拒绝
        "max-connection-per-server": conn,
        # aria2 不会把文件切成小于 2*SIZE 的段。取 1M（即最小 2M 一段）后，
        # 中小文件也能拿满 conn 条连接；几十 GB 的大文件段数由 split 封顶，不受影响。
        "min-split-size": "1M",
        "continue": "true",
        # 不预分配：prealloc 会先把整个文件大小的空间写满，几十 GB 的包会瞬间打满磁盘
        "file-allocation": "none",
        "max-tries": "0",
        # 服务器 2 分钟会踢掉空闲连接；等 20 秒才重连是白等
        "retry-wait": "5",
        # 某条连接真的卡死（服务器不再吐数据）时果断弃掉重连，
        # 否则一条僵死的分段会把整个任务拖成龟速
        "lowest-speed-limit": "1K",
        "timeout": "60",
        "connect-timeout": "20",
        "auto-file-renaming": "false",
        "allow-overwrite": "false",
    }
    if user:
        opts["ftp-user"] = user
        opts["ftp-passwd"] = pwd
    return opts


def build_engine_args(port, secret):
    """aria2c 的完整启动命令行。抽成函数是为了让测试能拿到应用真正跑的那一份。"""
    return [
        ARIA2_EXE,
        "--enable-rpc",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        "--rpc-listen-all=false",
        "--no-proxy=localhost,127.0.0.1",
        # 关掉用户级配置文件：本版 aria2 默认会加载 ~/.config/aria2/aria2.conf，
        # 若那里残留 max-overall-download-limit 之类的设置，会在背后把速度掐住而难以察觉。
        "--no-conf=true",
        "--continue=true",
        # none：不预分配。prealloc 会在下载开始前先把整个文件大小的空间写满，
        # 几十 GB 的包会把磁盘瞬间打满并卡住系统，务必保持 none。
        "--file-allocation=none",
        "--disk-cache=128M",     # 内存写盘缓存，减少磁盘写次数
        "--max-tries=0",
        "--retry-wait=5",        # 掉线后 5 秒即重连（服务器 2 分钟会踢掉空闲连接，等 20 秒是白等）
        "--timeout=60",
        "--connect-timeout=20",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--summary-interval=0",
        "--quiet=true",
    ]


def pick_ui_font():
    """按 word-hub 的字体栈挑一个本机可用的（MiSans → PingFang → YaHei UI）。"""
    for name in ("MiSans", "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei"):
        try:
            if name in tkfont.families():
                return name
        except Exception:
            break
    return FONT_UI_FAMILY


def apply_theme(root):
    """四应用共用设计令牌：浅色画布 + 白卡 + 品牌蓝强调。"""
    global FONT_UI, FONT_UI_FAMILY

    style = ttk.Style(root)
    try:
        style.theme_use("clam")      # clam 可定制性最好；vista 改不动颜色
    except tk.TclError:
        pass

    FONT_UI_FAMILY = pick_ui_font()
    FONT_UI = (FONT_UI_FAMILY, 9)

    root.configure(bg=BG)

    # 默认白底：卡片（Labelframe）内部的所有 ttk 控件自动是卡片色
    style.configure(".", background=SURFACE, foreground=TEXT, font=FONT_UI)
    style.configure("TFrame", background=SURFACE)
    style.configure("Bg.TFrame", background=BG)          # 外层容器用画布色
    style.configure("TLabel", background=SURFACE, foreground=TEXT, font=FONT_UI)
    style.configure("Muted.TLabel", background=SURFACE, foreground=TEXT_MUTED)
    style.configure("Bg.TLabel", background=BG, foreground=TEXT_MUTED)

    style.configure("TLabelframe", background=SURFACE, bordercolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=TEXT_MUTED,
                    font=FONT_UI)

    style.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    insertcolor=TEXT, padding=5, relief="flat")
    style.map("TEntry", bordercolor=[("focus", BRAND)])

    style.configure("TSpinbox", fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    arrowcolor=TEXT_MUTED, insertcolor=TEXT, padding=4, relief="flat")

    style.configure("TButton", background=SURFACE_ALT, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=SURFACE_ALT, darkcolor=SURFACE_ALT,
                    relief="flat", padding=(12, 6), font=FONT_UI)
    style.map("TButton",
              background=[("active", "#e2e2e6"), ("pressed", "#d8d8dd")],
              bordercolor=[("active", TEXT_MUTED)])

    style.configure("Accent.TButton", background=BRAND, foreground="#ffffff",
                    bordercolor=BRAND, lightcolor=BRAND, darkcolor=BRAND,
                    relief="flat", padding=(14, 6), font=FONT_UI)
    style.map("Accent.TButton",
              background=[("active", BRAND_HOVER), ("pressed", BRAND_PRESS)])

    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, bordercolor=BORDER, rowheight=26,
                    relief="flat", font=FONT_UI)
    style.configure("Treeview.Heading", background=BG, foreground=TEXT_MUTED,
                    relief="flat", font=FONT_UI, padding=(4, 6))
    style.map("Treeview.Heading", background=[("active", SURFACE_ALT)])
    style.map("Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", TEXT)])

    style.configure("TProgressbar", background=BRAND, troughcolor="#e5e5e9",
                    bordercolor=BORDER, lightcolor=BRAND, darkcolor=BRAND, thickness=6)

    style.configure("TScrollbar", background="#c9c9ce", troughcolor=BG,
                    bordercolor=BG, arrowcolor=TEXT_MUTED, relief="flat", arrowsize=12)
    style.map("TScrollbar",
              background=[("active", "#b0b0b6")],
              arrowcolor=[("active", TEXT)])


def strip_window_chrome(root):
    """摘掉 Windows 原生标题栏，但保留可缩放边框和任务栏图标（仿 Mac 无边框窗口）。

    只用 tkinter 的 overrideredirect(True) 会让窗口从任务栏消失、且无法最小化，
    所以改走 Win32 窗口样式：仅去掉 WS_CAPTION。

    ⚠️ 改完样式一定要用 SetWindowPos(SWP_FRAMECHANGED) 让系统重算非客户区。
    早先靠 withdraw()/deiconify() 糊弄，结果原标题栏的位置残留成一条空白，
    表现就是「第一次拖动窗口时顶部冒出一条多余的栏」。
    """
    if os.name != "nt":
        return
    import ctypes

    gwl_style = -16
    ws_caption = 0x00C00000
    swp_nosize, swp_nomove, swp_nozorder, swp_framechanged = 0x0001, 0x0002, 0x0004, 0x0020

    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    if not hwnd:
        hwnd = root.winfo_id()
    style = ctypes.windll.user32.GetWindowLongW(hwnd, gwl_style)
    ctypes.windll.user32.SetWindowLongW(hwnd, gwl_style, style & ~ws_caption)
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, 0, 0, 0, 0,
        swp_nosize | swp_nomove | swp_nozorder | swp_framechanged)


def mix_color(c1, c2, t):
    """两个 #rrggbb 之间按 t 线性插值（t=0 取 c1，t=1 取 c2）。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


class GlassButton(tk.Canvas):
    """自绘胶囊按钮，带玻璃质感（垂直渐变 + 顶部高光 + 底部收边）。

    ttk.Button 是矩形、既没圆角也没渐变，做不出 word-hub 那种胶囊按钮，
    所以这里用 Canvas 逐行扫描线把圆角矩形和垂直渐变一起画出来。
    用法与 ttk.Button 基本一致：GlassButton(parent, text=…, command=…).pack(…)
    """

    #                  normal上    normal下    hover上    hover下    press上    press下    文字色
    PALETTE = {
        "primary": ("#3d9bf0", "#0071e3", "#57a9f5", "#0a7fe8",
                    "#0062c4", "#006edb", "#ffffff"),
        "default": ("#ffffff", "#f2f2f6", "#ffffff", "#e9e9ef",
                    "#e4e4ec", "#dcdce4", TEXT),
    }

    def __init__(self, parent, text, command=None, kind="default",
                 padx=20, pady=9, bg=SURFACE):
        self.command = command
        self.kind = kind
        self.text = text
        self.enabled = True
        self._state = "normal"

        font = tkfont.Font(family=FONT_UI[0], size=FONT_UI[1])
        w = font.measure(text) + padx * 2
        h = font.metrics("linespace") + pady * 2

        super().__init__(parent, width=w, height=h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._font = font
        # 注意：别用 self._w / self._h —— 那是 tkinter 内部的 widget 路径名，覆盖会炸
        self._cw, self._ch = w, h
        self._radius = h / 2.0      # 胶囊

        self._render()
        self.bind("<Enter>", lambda e: self._set_state("hover"))
        self.bind("<Leave>", lambda e: self._set_state("normal"))
        self.bind("<Button-1>", lambda e: self._set_state("press"))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _set_state(self, state):
        if self.enabled:
            self._state = state
            self._render()

    def _on_release(self, event):
        if not self.enabled:
            return
        inside = 0 <= event.x <= self._cw and 0 <= event.y <= self._ch
        self._set_state("hover" if inside else "normal")
        if inside and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._render()

    def _row_inset(self, y):
        """圆角矩形在第 y 行左右各内缩多少像素。"""
        r, h = self._radius, self._ch
        if y < r:
            dy = r - y
        elif y > h - r:
            dy = y - (h - r)
        else:
            return 0.0
        return r - max(0.0, r * r - dy * dy) ** 0.5

    def _render(self):
        self.delete("all")
        w, h = self._cw, self._ch
        top, bottom, htop, hbot, ptop, pbot, fg = self.PALETTE[self.kind]

        if not self.enabled:
            top, bottom, fg = "#f0f0f4", "#e8e8ee", "#b4b4bc"
        elif self._state == "hover":
            top, bottom = htop, hbot
        elif self._state == "press":
            top, bottom = ptop, pbot

        # 逐行扫描线：一次画出圆角 + 垂直渐变（玻璃的立体感来源）
        for y in range(h):
            inset = self._row_inset(y)
            self.create_line(inset, y + 0.5, w - inset, y + 0.5,
                             fill=mix_color(top, bottom, y / max(1, h - 1)))

        # 顶部高光 + 底部收边：玻璃质感的两个关键细节
        self.create_line(self._radius * 0.6, 1.0, w - self._radius * 0.6, 1.0,
                         fill=mix_color(top, "#ffffff", 0.55))
        self.create_line(self._radius * 0.6, h - 1.5, w - self._radius * 0.6, h - 1.5,
                         fill=mix_color(bottom, "#000000", 0.10))

        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self._font)


# ============================ aria2 引擎封装 ============================


class Aria2Engine:
    """管理 aria2c 子进程，并通过 JSON-RPC 下发/查询任务。"""

    def __init__(self, log):
        self.log = log
        self.secret = uuid.uuid4().hex
        self.port = None
        self.proc = None

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if not os.path.exists(ARIA2_EXE):
            raise FileNotFoundError(f"未找到 aria2 引擎：{ARIA2_EXE}")

        self.port = free_port()          # 每次启动换空闲端口，避免被残留实例占死
        args = build_engine_args(self.port, self.secret)
        self.proc = subprocess.Popen(
            args,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(60):
            if self.proc.poll() is not None:
                # 进程秒退通常是端口被抢或引擎被拦，不必干等 12 秒
                raise RuntimeError(f"aria2 引擎启动后立即退出（返回码 {self.proc.returncode}）")
            try:
                self.rpc("aria2.getVersion")
                self.log(f"aria2 引擎已启动（端口 {self.port}）")
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("aria2 引擎启动超时")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.rpc("aria2.shutdown")
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    def rpc(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": "app",
            "method": method,
            "params": [f"token:{self.secret}"] + (params or []),
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/jsonrpc",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "RPC 调用失败"))
        return data.get("result")

    def add_uri(self, uris, options):
        """uris 可以是单个地址，也可以是同一文件的多个镜像地址。

        传多个时 aria2 会把不同分段分给不同节点并行拉取 —— 两个节点的限速是各自独立的，
        总带宽可以叠加（注意：分段总数由 --split 决定，多源只是把同样的分段摊给两台服务器）。
        """
        if isinstance(uris, str):
            uris = [uris]
        return self.rpc("aria2.addUri", [uris, options])

    def tell_active(self):
        return self.rpc("aria2.tellActive")

    def tell_waiting(self, offset=0, num=100):
        return self.rpc("aria2.tellWaiting", [offset, num])

    def tell_stopped(self, offset=0, num=100):
        return self.rpc("aria2.tellStopped", [offset, num])

    def get_global_stat(self):
        return self.rpc("aria2.getGlobalStat")

    def change_global(self, options):
        return self.rpc("aria2.changeGlobalOption", [options])

    def pause(self, gid):
        return self.rpc("aria2.pause", [gid])

    def unpause(self, gid):
        return self.rpc("aria2.unpause", [gid])

    def remove(self, gid):
        """活动任务用 forceRemove；已完成/出错的任务要用 removeDownloadResult，
        否则 aria2 会报错，记录也会残留在它的历史列表里。"""
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self.rpc(method, [gid])
                return
            except Exception:
                continue


# ============================== 主界面 ==============================


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry("1300x800")
        self.root.minsize(1040, 680)

        self.engine = Aria2Engine(self.log)

        self.ftp = None                   # 当前 FTP 连接
        self.ftp_lock = threading.Lock()  # ftp 对象跨线程使用，统一加锁
        self.remote_path = "/"            # 当前远程目录
        self.remote_entries = []          # [(name, is_dir, size)]
        self.checked = {}                 # {远程完整路径: 文件名}
        self.queue_order = []             # 队列 gid 顺序
        self.gid_names = {}               # {gid: 文件名}
        self.last_status = {}             # {gid: 最近一次状态}
        self.refreshing = False           # 列目录重入保护
        self.poll_errors = 0              # 队列刷新异常计数
        self.last_jobs = None             # 上次应用的并发数
        self.ui_queue = queue.Queue()     # 后台线程 -> 主线程 的 UI 操作队列
        self.maximized = False            # 窗口是否已最大化
        self.restore_geom = ""            # 还原用的几何串

        self._build_ui()
        strip_window_chrome(self.root)    # 摘掉原生标题栏，换成自绘的
        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            self.engine.start()
        except Exception as exc:
            self.log(f"引擎启动失败：{exc}")
            messagebox.showerror("引擎启动失败",
                                 f"{exc}\n\n若反复失败，请检查是否有残留的 aria2c.exe 进程。")

        self.root.after(POLL_MS, self._poll)
        self.root.after(60, self._drain_ui)

    # ---------------------------- 界面构建 ----------------------------

    def _build_titlebar(self):
        """仿 Mac 的标题栏：左侧三个圆点、中间标题，整条可拖动。"""
        bar = tk.Frame(self.root, bg=TITLEBAR_BG, height=TITLEBAR_H)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)

        dots = tk.Canvas(bar, width=72, height=TITLEBAR_H, bg=TITLEBAR_BG,
                         highlightthickness=0, cursor="hand2")
        dots.pack(side="left", padx=(14, 0))
        for i, color in enumerate((DOT_CLOSE, DOT_MIN, DOT_MAX)):
            x = 6 + i * 22
            dots.create_oval(x, 13, x + 13, 26, fill=color, outline="")
        dots.bind("<Button-1>", self._on_dot_click)

        title = tk.Label(bar, text=APP_NAME, bg=TITLEBAR_BG, fg=TEXT_MUTED, font=FONT_UI)
        title.place(relx=0.5, rely=0.5, anchor="center")

        for widget in (bar, title):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
        bar.bind("<Double-Button-1>", lambda e: self._toggle_maximize())

    def _on_dot_click(self, event):
        if event.x < 24:
            self.on_close()
        elif event.x < 46:
            self._minimize()
        else:
            self._toggle_maximize()

    def _start_drag(self, event):
        self._drag_dx = event.x_root - self.root.winfo_x()
        self._drag_dy = event.y_root - self.root.winfo_y()

    def _on_drag(self, event):
        x = event.x_root - self._drag_dx
        y = event.y_root - self._drag_dy
        self.root.geometry(f"+{x}+{y}")

    def _minimize(self):
        self.root.iconify()

    def _toggle_maximize(self):
        if self.maximized:
            self.root.state("normal")
            if self.restore_geom:
                self.root.geometry(self.restore_geom)
        else:
            self.restore_geom = self.root.geometry()
            self.root.state("zoomed")
        self.maximized = not self.maximized

    def _build_ui(self):
        self.var_host = tk.StringVar(value=DEFAULT_HOST)
        self.var_port = tk.StringVar(value=DEFAULT_PORT)
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_save = tk.StringVar()
        self.var_mirror = tk.StringVar()
        self.var_conn = tk.StringVar(value=DEFAULT_CONN)
        self.var_jobs = tk.StringVar(value=DEFAULT_JOBS)
        self.var_total = tk.StringVar(value="尚未开始")
        self.var_path = tk.StringVar(value="/")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_titlebar()

        # 原生标题栏随后会被摘掉，内容全部挂在 content 里
        self.content = tk.Frame(self.root, bg=BG)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=3)
        self.content.rowconfigure(3, weight=2)

        self._build_conn_area()
        self._build_middle_area()
        self._build_status_area()
        self._build_log_area()

    def _build_conn_area(self):
        frame = ttk.LabelFrame(self.content, text=" 连接设置 ")
        frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        for col in (1, 3, 5, 7):
            frame.columnconfigure(col, weight=1)

        ttk.Label(frame, text="主机", foreground=TEXT_MUTED).grid(
            row=0, column=0, padx=(10, 4), pady=7, sticky="e")
        ttk.Entry(frame, textvariable=self.var_host).grid(
            row=0, column=1, padx=(0, 10), pady=7, sticky="ew")

        ttk.Label(frame, text="端口", foreground=TEXT_MUTED).grid(
            row=0, column=2, padx=(4, 4), pady=7, sticky="e")
        ttk.Entry(frame, textvariable=self.var_port, width=10).grid(
            row=0, column=3, padx=(0, 10), pady=7, sticky="w")

        ttk.Label(frame, text="用户名", foreground=TEXT_MUTED).grid(
            row=0, column=4, padx=(4, 4), pady=7, sticky="e")
        ttk.Entry(frame, textvariable=self.var_user).grid(
            row=0, column=5, padx=(0, 10), pady=7, sticky="ew")

        ttk.Label(frame, text="密码", foreground=TEXT_MUTED).grid(
            row=0, column=6, padx=(4, 4), pady=7, sticky="e")
        ttk.Entry(frame, textvariable=self.var_pass, show="*").grid(
            row=0, column=7, padx=(0, 10), pady=7, sticky="ew")

        GlassButton(frame, text="连接", kind="primary",
                    command=self.do_connect).grid(row=0, column=8, padx=4, pady=7)
        GlassButton(frame, text="断开",
                    command=self.do_disconnect).grid(row=0, column=9, padx=(0, 10), pady=7)

        ttk.Label(frame, text="保存目录", foreground=TEXT_MUTED).grid(
            row=1, column=0, padx=(10, 4), pady=(0, 7), sticky="e")
        ttk.Entry(frame, textvariable=self.var_save).grid(
            row=1, column=1, columnspan=6, padx=(0, 10), pady=(0, 7), sticky="ew")
        GlassButton(frame, text="浏览…", command=self.choose_save_dir,
                    padx=16).grid(row=1, column=7, padx=(0, 4), pady=(0, 7), sticky="w")
        GlassButton(frame, text="打开目录", command=self.open_save_dir,
                    padx=16).grid(row=1, column=8, padx=4, pady=(0, 7))

        # 同一份数据在另一个节点上的镜像：aria2 会并行从两个源拉不同分段来提速
        ttk.Label(frame, text="镜像主机", foreground=TEXT_MUTED).grid(
            row=2, column=0, padx=(10, 4), pady=(0, 10), sticky="e")
        ttk.Entry(frame, textvariable=self.var_mirror).grid(
            row=2, column=1, columnspan=3, padx=(0, 10), pady=(0, 10), sticky="ew")
        ttk.Label(frame, text="可选。填同一份数据的另一个 FTP 节点，两个源并行拉取会更快（如 ftp2 配 ftp3）",
                  foreground=TEXT_MUTED).grid(
            row=2, column=4, columnspan=6, padx=(0, 10), pady=(0, 10), sticky="w")

    def _build_middle_area(self):
        mid = ttk.Frame(self.content, style="Bg.TFrame")
        mid.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        mid.columnconfigure(0, weight=4)
        mid.columnconfigure(1, weight=6)
        mid.rowconfigure(0, weight=1)

        # ---- 左：远程文件 ----
        left = ttk.LabelFrame(mid, text=" 远程文件 ")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        GlassButton(bar, text="← 上级", command=self.go_parent, padx=14).pack(side="left")
        GlassButton(bar, text="刷新", padx=14,
                    command=lambda: self.refresh_remote(force=True)).pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.var_path, foreground=TEXT_MUTED,
                  font=FONT_MONO).pack(side="left", padx=6)

        self.tree_remote = ttk.Treeview(left, columns=("chk", "name", "type", "size"),
                                        show="headings", height=15)
        for col, txt, width, anchor in (
            ("chk", "", 34, "center"),
            ("name", "名称", 240, "w"),
            ("type", "类型", 52, "center"),
            ("size", "大小", 86, "e"),
        ):
            self.tree_remote.heading(col, text=txt)
            self.tree_remote.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.tree_remote.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 6))
        self.tree_remote.bind("<Button-1>", self.on_remote_click)
        self.tree_remote.bind("<Double-1>", self.on_remote_double)

        sb1 = ttk.Scrollbar(left, orient="vertical", command=self.tree_remote.yview)
        sb1.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 6))
        self.tree_remote.configure(yscrollcommand=sb1.set)

        GlassButton(left, text="把勾选的文件加入下载队列", kind="primary",
                    command=self.add_checked).grid(
            row=2, column=0, sticky="w", padx=(8, 0), pady=(0, 10))

        # ---- 右：下载队列 ----
        right = ttk.LabelFrame(mid, text=" 下载队列 ")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.tree_queue = ttk.Treeview(
            right, columns=("name", "size", "pct", "speed", "eta", "status"),
            show="headings", height=15,
        )
        for col, txt, width, anchor in (
            ("name", "文件", 187, "w"),
            ("size", "大小", 62, "e"),
            ("pct", "进度", 48, "center"),
            ("speed", "速度", 72, "e"),
            ("eta", "剩余", 70, "center"),
            ("status", "状态", 68, "center"),
        ):
            self.tree_queue.heading(col, text=txt)
            self.tree_queue.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.tree_queue.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(0, 6))
        # 只用文字色区分状态，不铺背景色 —— 颜色是稀缺资源
        self.tree_queue.tag_configure("active", foreground=FG_OK)
        self.tree_queue.tag_configure("done", foreground=FG_DONE)
        self.tree_queue.tag_configure("error", foreground=FG_ERR)
        self.tree_queue.tag_configure("paused", foreground=FG_WARN)

        sb2 = ttk.Scrollbar(right, orient="vertical", command=self.tree_queue.yview)
        sb2.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(0, 6))
        self.tree_queue.configure(yscrollcommand=sb2.set)

        qbar = ttk.Frame(right)
        qbar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 10))
        GlassButton(qbar, text="暂停 / 继续", command=self.toggle_pause,
                    padx=16).pack(side="left")
        GlassButton(qbar, text="移除选中", command=self.remove_selected,
                    padx=16).pack(side="left", padx=6)
        GlassButton(qbar, text="清除已完成", command=self.clear_finished,
                    padx=16).pack(side="left")

    def _build_status_area(self):
        frame = ttk.Frame(self.content, style="Bg.TFrame")
        frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        frame.columnconfigure(0, weight=1)

        top = ttk.Frame(frame, style="Bg.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        settings = ttk.Frame(top, style="Bg.TFrame")
        settings.grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="每文件连接数", style="Bg.TLabel").pack(side="left")
        ttk.Spinbox(settings, from_=1, to=16, width=4,
                    textvariable=self.var_conn).pack(side="left", padx=(6, 16))
        ttk.Label(settings, text="同时下载文件数", style="Bg.TLabel").pack(side="left")
        ttk.Spinbox(settings, from_=1, to=10, width=4,
                    textvariable=self.var_jobs).pack(side="left", padx=(6, 12))
        GlassButton(settings, text="应用", command=self.apply_settings,
                    padx=16, bg=BG).pack(side="left")

        ttk.Label(top, textvariable=self.var_total, background=BG, foreground=TEXT).grid(
            row=0, column=1, sticky="e", padx=8)

        self.pbar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.pbar.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    def _build_log_area(self):
        frame = ttk.LabelFrame(self.content, text=" 日志 ")
        frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.txt_log = tk.Text(frame, height=7, wrap="none", state="disabled",
                               background=SURFACE, foreground=TEXT_MUTED,
                               relief="flat", borderwidth=0, font=FONT_MONO,
                               highlightthickness=0, selectbackground=SEL_BG)
        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
        self.txt_log.configure(yscrollcommand=sb.set)

    # ---------------------------- 日志 ----------------------------

    def post(self, fn):
        """从任意线程向主线程投递 UI 操作。

        tkinter 不是线程安全的：后台线程直接调 root.after / 改控件会抛
        "main thread is not in main loop"。统一走队列，由主线程定时消费。
        """
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.ui_queue.put(fn)

    def _drain_ui(self):
        """主线程侧：消费后台线程投递过来的 UI 操作。"""
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        self.root.after(60, self._drain_ui)

    def log(self, msg):
        """线程安全的日志输出。"""
        text = f"{time.strftime('%H:%M:%S')}  {msg}\n"

        def write():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", text)
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")

        self.post(write)

    # ---------------------------- FTP 连接 ----------------------------

    def do_connect(self):
        host = self.var_host.get().strip()
        port = self.var_port.get().strip()
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()
        if not host:
            messagebox.showwarning("提示", "请填写主机地址")
            return
        self.log(f"正在连接 {host}:{port or '21'} …")
        threading.Thread(target=self._connect_worker, args=(host, port, user, pwd),
                         daemon=True).start()

    def _connect_worker(self, host, port, user, pwd):
        try:
            ftp = ftplib.FTP()
            ftp.encoding = "utf-8"
            ftp.connect(host, int(port or 21), timeout=25)
            ftp.login(user, pwd)
            with self.ftp_lock:
                self.ftp = ftp
                self.remote_path = "/"
            self.log(f"已连接：{host}:{port}")
            self.post(lambda: self.refresh_remote(force=True))
        except Exception as exc:
            self.log(f"连接失败：{exc}")
            self.post(lambda: messagebox.showerror("连接失败", str(exc)))

    def do_disconnect(self):
        with self.ftp_lock:
            if self.ftp is not None:
                try:
                    self.ftp.quit()
                except Exception:
                    try:
                        self.ftp.close()
                    except Exception:
                        pass
                self.ftp = None
                self.log("已断开连接")
            self.remote_path = "/"
        self.remote_entries = []
        self._fill_remote([], "/")

    # ---------------------------- 远程浏览 ----------------------------

    def refresh_remote(self, force=False):
        """列目录放后台线程 —— FTP 往返慢时不能冻住界面。"""
        if self.ftp is None:
            if force:
                self.log("尚未连接，无法列目录")
            return
        if self.refreshing:
            return
        self.refreshing = True
        threading.Thread(target=self._refresh_worker, args=(self.remote_path,),
                         daemon=True).start()

    def _refresh_worker(self, path):
        try:
            try:
                entries = self._get_entries(path)
            except Exception as first_err:
                # FTP 服务器空闲一段时间会主动踢人，自动重连后重试一次
                self.log(f"列目录失败（{first_err}），尝试重新连接 …")
                try:
                    self._reconnect()
                    entries = self._get_entries(path)
                except Exception as exc:
                    self.log(f"列目录失败：{exc}（已保留上次的列表）")
                    return
            self.post(lambda: self._apply_entries(entries, path))
        finally:
            self.refreshing = False

    def _apply_entries(self, entries, path):
        self.remote_entries = entries
        self._fill_remote(entries, path)
        self.log(f"目录 {path}：{len(entries)} 项")

    def _get_entries(self, path):
        """列出目录内容，优先用 MLSD，服务器不支持时回退到 LIST 解析。"""
        with self.ftp_lock:
            entries = []
            try:
                for name, facts in self.ftp.mlsd(path):
                    if name in (".", ".."):
                        continue
                    typ = facts.get("type", "")
                    is_dir = typ in ("dir", "cdir", "pdir")
                    size = int(facts.get("size", 0)) if not is_dir else 0
                    entries.append((name, is_dir, size))
            except Exception:
                entries = self._list_fallback(path)
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries

    def _reconnect(self):
        """重建 FTP 控制连接（空闲被服务器断开、或连接被占满时用）。"""
        host = self.var_host.get().strip()
        port = self.var_port.get().strip()
        with self.ftp_lock:
            if self.ftp is not None:
                try:
                    self.ftp.close()
                except Exception:
                    pass
                self.ftp = None
            ftp = ftplib.FTP()
            ftp.encoding = "utf-8"
            ftp.connect(host, int(port or 21), timeout=25)
            ftp.login(self.var_user.get().strip(), self.var_pass.get())
            self.ftp = ftp
        self.log(f"已重新连接 {host}:{port}")

    def _list_fallback(self, path):
        """MLSD 不可用时回退到 LIST 解析（调用方已持锁）。"""
        entries = []
        lines = []
        self.ftp.retrlines(f"LIST {path}", lines.append)
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, name = parts[0], parts[8]
            is_dir = perms.startswith("d")
            try:
                size = int(parts[4])
            except ValueError:
                size = 0
            entries.append((name, is_dir, 0 if is_dir else size))
        return entries

    def _fill_remote(self, entries, path):
        self.var_path.set(path)
        self.tree_remote.delete(*self.tree_remote.get_children())
        for idx, (name, is_dir, size) in enumerate(entries):
            full = join_remote(path, name)
            checked = full in self.checked
            self.tree_remote.insert(
                "", "end", iid=str(idx),
                values=("☑" if checked else "☐", name, "目录" if is_dir else "文件",
                        "" if is_dir else human_size(size)),
            )

    def _clear_checks(self):
        """只清掉列表里的勾选标记，不重新拉目录 —— 下载中列目录容易被服务器拒。"""
        for item in self.tree_remote.get_children():
            self.tree_remote.set(item, "chk", "☐")

    def go_parent(self):
        if self.remote_path in ("/", ""):
            return
        parent = self.remote_path.rstrip("/").rsplit("/", 1)[0]
        self.remote_path = parent if parent else "/"
        self.refresh_remote(force=True)

    def on_remote_click(self, event):
        item = self.tree_remote.identify_row(event.y)
        if not item:
            return
        idx = int(item)
        name, is_dir, _ = self.remote_entries[idx]
        if is_dir:
            return
        full = join_remote(self.remote_path, name)
        if full in self.checked:
            del self.checked[full]
            mark = "☐"
        else:
            self.checked[full] = name
            mark = "☑"
        self.tree_remote.set(item, "chk", mark)

    def on_remote_double(self, event):
        item = self.tree_remote.identify_row(event.y)
        if not item:
            return
        idx = int(item)
        name, is_dir, _ = self.remote_entries[idx]
        if not is_dir:
            return
        self.remote_path = join_remote(self.remote_path, name)
        self.refresh_remote(force=True)

    # ---------------------------- 下载队列 ----------------------------

    def add_checked(self):
        if not self.checked:
            messagebox.showinfo("提示", "还没有勾选任何文件")
            return
        if self.engine.proc is None or self.engine.proc.poll() is not None:
            messagebox.showerror("错误", "aria2 引擎未运行")
            return

        save_dir = self.var_save.get().strip()
        if not save_dir:
            messagebox.showwarning("提示", "请先选择保存目录")
            return
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("目录不可用", str(exc))
            return

        host = self.var_host.get().strip()
        port = self.var_port.get().strip()
        conn = str(clamp_int(self.var_conn.get(), 1, MAX_CONN, DEFAULT_CONN))
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()

        base_opts = build_download_options(save_dir, conn, user, pwd)

        mirror = self.var_mirror.get().strip()
        added = 0
        for remote, name in list(self.checked.items()):
            path = urllib.parse.quote(remote)
            uris = [f"ftp://{host}:{port}{path}"]
            if mirror:
                # 同一文件的第二个源：两节点限速独立，总带宽可叠加
                uris.append(f"ftp://{mirror}:{port}{path}")
            opts = dict(base_opts)
            opts["out"] = name
            try:
                gid = self.engine.add_uri(uris, opts)
                self.gid_names[gid] = name
                self.queue_order.append(gid)
                self.tree_queue.insert("", "end", iid=gid,
                                       values=(name, "-", "0%", "-", "-", "排队中"))
                added += 1
            except Exception as exc:
                self.log(f"加入失败 {name}：{exc}")

        self.checked.clear()
        self._clear_checks()
        jobs = str(clamp_int(self.var_jobs.get(), 1, MAX_JOBS, DEFAULT_JOBS))
        self.log(f"已加入队列：{added} 个文件（引擎同时下载 {jobs} 个，其余排队中）")
        self.apply_settings(quiet=True)

    def toggle_pause(self):
        sel = self.tree_queue.selection()
        if not sel:
            self.log("先在队列里选中要操作的任务")
            return
        for gid in sel:
            status = self.last_status.get(gid, "")
            name = self.gid_names.get(gid, gid[:8])
            try:
                if status == "paused":
                    self.engine.unpause(gid)
                    self.log(f"继续：{name}")
                elif status in ("active", "waiting"):
                    self.engine.pause(gid)
                    self.log(f"暂停：{name}")
                else:
                    self.log(f"「{name}」当前状态是「{status or '未知'}」，无需暂停/继续")
            except Exception as exc:
                self.log(f"暂停/继续失败 {name}：{exc}")

    def remove_selected(self):
        sel = self.tree_queue.selection()
        if not sel:
            self.log("先在队列里选中要移除的任务")
            return
        for gid in sel:
            self.engine.remove(gid)
            try:
                self.tree_queue.delete(gid)
            except Exception:
                pass
            self.gid_names.pop(gid, None)
            self.last_status.pop(gid, None)
            if gid in self.queue_order:
                self.queue_order.remove(gid)

    def clear_finished(self):
        """从队列里清掉已完成 / 出错 / 已移除的记录。"""
        count = 0
        for gid in list(self.gid_names.keys()):
            if self.last_status.get(gid) in ("complete", "error", "removed"):
                self.engine.remove(gid)
                if self.tree_queue.exists(gid):
                    self.tree_queue.delete(gid)
                self.gid_names.pop(gid, None)
                self.last_status.pop(gid, None)
                if gid in self.queue_order:
                    self.queue_order.remove(gid)
                count += 1
        self.log(f"已清除 {count} 条已完成/出错的记录")

    def _poll(self):
        """定时从 aria2 拉取任务状态并刷新界面。"""
        try:
            if self.engine.proc is None or self.engine.proc.poll() is not None:
                self.root.after(POLL_MS, self._poll)
                return

            tasks = {}
            for group in (self.engine.tell_active(),
                          self.engine.tell_waiting(0, 200),
                          self.engine.tell_stopped(0, 200)):
                for t in group:
                    if t["gid"] in self.gid_names:
                        tasks[t["gid"]] = t

            sum_total = 0
            sum_done = 0
            finished = 0

            for gid, t in tasks.items():
                total = int(t.get("totalLength", 0) or 0)
                done = int(t.get("completedLength", 0) or 0)
                speed = int(t.get("downloadSpeed", 0) or 0)
                status = t.get("status", "")
                self.last_status[gid] = status

                pct = f"{done * 100 // total}%" if total else "0%"
                text = {
                    "active": "下载中", "waiting": "排队中", "paused": "已暂停",
                    "complete": "完成", "removed": "已移除",
                }.get(status, status)
                if status == "error":
                    text = f"出错 {t.get('errorCode', '')}"
                tag = {
                    "active": "active", "complete": "done",
                    "error": "error", "paused": "paused",
                }.get(status, "")

                eta = "-"
                if status == "active" and speed > 0 and total > done:
                    eta = human_duration((total - done) / speed)

                self.tree_queue.item(gid, values=(
                    self.gid_names.get(gid, ""),
                    human_size_short(total) if total else "-",
                    pct,
                    f"{human_size_short(speed)}/s" if speed else "-",
                    eta,
                    text,
                ), tags=(tag,) if tag else ())

                sum_total += total
                sum_done += done
                if status in ("complete", "error", "removed"):
                    finished += 1

            stat = self.engine.get_global_stat()
            speed_total = int(stat.get("downloadSpeed", 0) or 0)
            queued = sum(1 for g in self.gid_names if self.tree_queue.exists(g))

            self.pbar.configure(value=(sum_done / sum_total * 100) if sum_total else 0)

            if speed_total > 0 and sum_total > sum_done:
                eta_text = human_duration((sum_total - sum_done) / speed_total)
            elif queued and finished >= queued:
                eta_text = "已完成"
            else:
                eta_text = "-"

            self.var_total.set(
                f"已完成 {finished}/{queued}　总速度 {human_size(speed_total)}/s　"
                f"预计剩余 {eta_text}"
            )
        except Exception as exc:
            # 过去这里静默吞异常，界面不刷新却毫无线索；现在前几次给出提示
            self.poll_errors += 1
            if self.poll_errors <= 3:
                self.log(f"刷新队列出错（第 {self.poll_errors} 次）：{exc!r}")
        self.root.after(POLL_MS, self._poll)

    # ---------------------------- 设置 / 杂项 ----------------------------

    def apply_settings(self, quiet=False):
        jobs = str(clamp_int(self.var_jobs.get(), 1, MAX_JOBS, DEFAULT_JOBS))
        try:
            self.engine.change_global({"max-concurrent-downloads": jobs})
            if not quiet or self.last_jobs != jobs:
                self.log(f"同时下载文件数 = {jobs}，每文件连接数 {self.var_conn.get()}")
            self.last_jobs = jobs
        except Exception as exc:
            self.log(f"应用设置失败：{exc}")

    def choose_save_dir(self):
        path = filedialog.askdirectory(title="选择保存目录")
        if path:
            self.var_save.set(path)

    def open_save_dir(self):
        path = self.var_save.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showwarning("提示", "保存目录不存在")
            return
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    # ---------------------------- 配置持久化 ----------------------------

    def _load_config(self):
        cfg = {}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        self.var_host.set(cfg.get("host", DEFAULT_HOST))
        self.var_port.set(cfg.get("port", DEFAULT_PORT))
        self.var_user.set(cfg.get("user", ""))
        self.var_save.set(cfg.get("save_dir", os.path.join(os.path.expanduser("~"), "Downloads")))
        self.var_mirror.set(cfg.get("mirror", ""))
        self.var_conn.set(cfg.get("conn", DEFAULT_CONN))
        self.var_jobs.set(cfg.get("jobs", DEFAULT_JOBS))

    def _save_config(self):
        cfg = {
            "host": self.var_host.get(),
            "port": self.var_port.get(),
            "user": self.var_user.get(),
            "save_dir": self.var_save.get(),
            "mirror": self.var_mirror.get(),
            "conn": self.var_conn.get(),
            "jobs": self.var_jobs.get(),
        }
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def on_close(self):
        self._save_config()
        self.engine.stop()
        self.root.destroy()


def enable_hidpi():
    """Windows 高 DPI 下让界面不发虚。"""
    if os.name != "nt":
        return
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def run_selftest():
    """无界面自检：启动引擎并调用 RPC，验证打包产物是否完整。

    打包成 --windowed 后没有 stdout，结果写到临时文件，用退出码表示成败。
    """
    lines = []
    code = 0
    engine = None
    try:
        engine = Aria2Engine(lines.append)
        engine.start()
        lines.append("aria2 version: " + engine.rpc("aria2.getVersion")["version"])
        lines.append("global stat: " + json.dumps(engine.get_global_stat()))
        lines.append("SELFTEST OK")
    except Exception as exc:
        lines.append("SELFTEST FAIL: " + repr(exc))
        code = 1
    finally:
        if engine is not None:
            engine.stop()          # 失败也要收尸，别留下残留进程
    out = os.path.join(tempfile.gettempdir(), "ftp_accel_selftest.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return code


def main():
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    enable_hidpi()
    root = tk.Tk()
    apply_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
