"""
TPDC（国家青藏高原科学数据中心）FTP 加速下载

背景：TPDC 的 FTP 单连接被限速（社区实测几十 KB/s），且服务器总连接数有限
（ftp2 上限 500 / ftp3 上限 250），大文件必须靠"单文件多连接分段"提速。

实测服务器能力（2026-10-07）：
    ftp2.tpdc.ac.cn:6201  Pure-FTPd  FEAT 返回 REST STREAM  -> 支持分段下载
    ftp3.tpdc.ac.cn:6201  Pure-FTPd  同上
    空闲 2 分钟自动断开 -> 必须开断点续传

用法：
    1. 装 aria2：  winget install aria2.aria2
    2. 改下面 ======== 之间那 6 个常量
    3. python tpdc_download.py
"""

import os
import subprocess
import sys

# ============================== 改这里 ==============================
FTP_HOST = "ftp2.tpdc.ac.cn"       # 备用：ftp3.tpdc.ac.cn（ftp1 实测不通）
FTP_PORT = 6201                    # TPDC 的 FTP 端口，不是 21
FTP_USER = "download_00000000"     # 数据下载页给的账号
FTP_PASS = "00000000"              # 数据下载页给的密码
SAVE_DIR = r"D:\tpdc_data"         # 保存目录（1TB 数据请预留 2TB+ 空间）

# 每个文件开几条连接做分段。TPDC 单连接被限速，8~16 提速最明显。
# ⚠️ 别一上来拉满：服务器总连接有限，且高并发可能被判为攻击封 IP。
#    可以从 8 开始，观察速度是否还有提升，不涨了就够了。
CONNECTIONS = 12

# 同时下载几个文件。1 = 一次只下一个包，对服务器最友好。
PARALLEL_FILES = 1
# ====================================================================

URLS_FILE = os.path.join(SAVE_DIR, "urls.txt")
SESSION_FILE = os.path.join(SAVE_DIR, "session.txt")

URLS_TEMPLATE = """ftp://ftp2.tpdc.ac.cn:6201/把这里换成远程完整路径/xxx.zip
ftp://ftp2.tpdc.ac.cn:6201/把这里换成远程完整路径/yyy.zip
"""


def main():
    os.makedirs(SAVE_DIR, exist_ok=True)

    if not os.path.exists(URLS_FILE):
        with open(URLS_FILE, "w", encoding="utf-8") as f:
            f.write(URLS_TEMPLATE)
        print(f"已生成清单模板：{URLS_FILE}")
        print("把里面每行的地址改成你的真实文件路径（每行一个），再重新运行本脚本。")
        sys.exit(0)

    cmd = [
        "aria2c",
        f"--ftp-user={FTP_USER}",
        f"--ftp-passwd={FTP_PASS}",
        f"--max-connection-per-server={CONNECTIONS}",   # 每服务器最多几条连接
        f"--split={CONNECTIONS}",                        # 单个文件切成几段并行下
        "--min-split-size=4M",
        "--continue=true",                               # 断点续传（必需）
        f"--max-concurrent-downloads={PARALLEL_FILES}",  # 同时下几个文件
        "--file-allocation=prealloc",                    # 预分配，避免半路空间不足
        "--disk-cache=64M",                              # 写盘缓存，机械硬盘尤其有用
        "--max-tries=0",                                 # 无限重试
        "--retry-wait=20",
        "--timeout=60",
        "--connect-timeout=20",
        f"--save-session={SESSION_FILE}",                # 会话存档
        "--save-session-interval=60",
        f"--input-file={URLS_FILE}",
        f"--dir={SAVE_DIR}",
    ]

    print(" ".join(cmd))
    print("-" * 60)

    try:
        subprocess.run(cmd)
    except FileNotFoundError:
        print("没找到 aria2c。请先安装：winget install aria2.aria2")
        sys.exit(1)

    print("-" * 60)
    print(f"全部完成。断点续传记录在：{SESSION_FILE}")


if __name__ == "__main__":
    main()
