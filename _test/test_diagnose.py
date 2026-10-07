# -*- coding: utf-8 -*-
"""验证 diagnose_speed 的流程可用（用本地迷你 FTP 服务器，速度数值无实际意义）。

运行：E:\\Python\\python.exe _test\\test_diagnose.py
"""

import os
import sys
import shutil
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_ftp_download as tfd
import diagnose_speed as ds


def main():
    root = tempfile.mkdtemp(prefix="diagroot_")
    tfd.SERVER_ROOT = root

    # 造一个 200 MB 文件：必须足够大，否则高连接数档位会在采样前就下完
    payload = os.urandom(1 << 20)
    with open(os.path.join(root, "sample.bin"), "wb") as fh:
        for _ in range(200):
            fh.write(payload)

    server = tfd.MiniFTPServer(("127.0.0.1", 0), tfd.FTPHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 本地回环极快，给服务器加每连接 2MB/s 限速，模拟真实的慢速 FTP 场景
    tfd.FTPHandler.THROTTLE_BPS = 2 * 1024 * 1024

    ds.CONNECTION_LADDER = [1, 2, 4]
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "user": "",
        "password": "",
        "remote_file": "/sample.bin",
        "seconds": 8,
    }
    rc = ds.run_scan(cfg)

    server.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    print("run_scan 返回码:", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
