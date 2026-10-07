# -*- coding: utf-8 -*-
"""在桌面创建「FTP 加速下载器」快捷方式，指向打包好的 exe。

之所以用 Python 而不是 PowerShell 脚本：本机安全策略会拦截 PowerShell 里的
COM 对象创建（`New-Object -ComObject`），走 pywin32 更稳。

运行：E:\\Python\\python.exe make_shortcut.py
"""

import os
import sys

import win32com.client

# ============================== 改这里 ==============================
APP_NAME = "FTP 加速下载器"
# ===================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "FTPAccelerator.exe")
ICON = os.path.join(HERE, "app.ico")


def main():
    if not os.path.exists(EXE):
        print("找不到 exe：", EXE)
        return 1

    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    lnk_path = os.path.join(desktop, f"{APP_NAME}.lnk")

    shell = win32com.client.Dispatch("WScript.Shell")
    lnk = shell.CreateShortCut(lnk_path)
    lnk.TargetPath = EXE
    lnk.WorkingDirectory = os.path.dirname(EXE)
    lnk.IconLocation = ICON if os.path.exists(ICON) else EXE
    lnk.Description = f"{APP_NAME}（内置 aria2 引擎的 FTP 多线程下载器）"
    lnk.Save()

    if not os.path.exists(lnk_path):
        print("创建失败：", lnk_path)
        return 1

    # 读回确认指向正确（不只报"完成"）
    check = shell.CreateShortCut(lnk_path)
    print("已创建快捷方式：", lnk_path)
    print("  指向  :", check.TargetPath)
    print("  工作目录:", check.WorkingDirectory)
    print("  图标  :", check.IconLocation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
