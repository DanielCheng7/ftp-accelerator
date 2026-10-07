# -*- coding: utf-8 -*-
"""一键打包：把 ftp_accel.py 和内置 aria2 引擎打成单文件 exe。

运行：E:\\Python\\python.exe build.py
产物：dist/FTPAccelerator.exe
"""

import os
import subprocess
import sys

# ============================== 改这里 ==============================
PYTHON = r"E:\Python\python.exe"   # 带 tkinter + PyInstaller 的解释器
APP_NAME = "FTPAccelerator"        # 生成的 exe 名（不含扩展名）
ENTRY = "ftp_accel.py"             # 程序入口
ICON = "app.ico"                   # 图标路径（.ico），留空用默认图标
# ===================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
ARIA2_EXE = os.path.join(HERE, "third_party", "aria2", "aria2c.exe")


def main():
    if not os.path.exists(ARIA2_EXE):
        print("缺少 aria2 引擎：", ARIA2_EXE)
        return 1

    args = [
        PYTHON, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", APP_NAME,
        "--add-binary", f"{ARIA2_EXE}{os.pathsep}third_party/aria2",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
    ]
    icon_path = os.path.join(HERE, ICON) if ICON else ""
    if icon_path and os.path.exists(icon_path):
        args += ["--icon", icon_path]
    args.append(os.path.join(HERE, ENTRY))

    print("执行：", " ".join(args))
    result = subprocess.run(args, cwd=HERE)
    if result.returncode != 0:
        return result.returncode

    exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
    if os.path.exists(exe):
        print(f"打包完成：{exe}  {os.path.getsize(exe) / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
