# -*- coding: utf-8 -*-
"""验证「FTP 控制连接被踢掉后能自动重连」—— 对应「加入队列后报列目录失败」的问题。

做法：连上本地迷你 FTP，列一次目录，然后强制关掉控制连接（模拟服务器空闲踢人），
再列一次目录，应当自动重连并成功。

运行：E:\\Python\\python.exe _test\\test_reconnect.py
"""

import os
import sys
import shutil
import tempfile
import threading
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_ftp_download as tfd
import ftp_accel


def main():
    root_dir = tempfile.mkdtemp(prefix="reconroot_")
    tfd.SERVER_ROOT = root_dir
    for name, size in (("a.bin", 1024), ("b.bin", 2048)):
        with open(os.path.join(root_dir, name), "wb") as fh:
            fh.write(b"x" * size)

    server = tfd.MiniFTPServer(("127.0.0.1", 0), tfd.FTPHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tk_root = tk.Tk()
    tk_root.withdraw()
    app = ftp_accel.App(tk_root)

    results = []
    ok = False
    try:
        app.var_host.set("127.0.0.1")
        app.var_port.set(str(port))
        app.var_user.set("")
        app.var_pass.set("")

        app._connect_worker("127.0.0.1", str(port), "", "")
        for _ in range(20):
            tk_root.update()
        n1 = len(app.remote_entries)
        results.append(("首次连接后列目录", n1))

        # 模拟服务器主动断开控制连接
        try:
            app.ftp.close()
        except Exception:
            pass
        results.append(("已强制断开控制连接", -1))

        app.refresh_remote()
        for _ in range(20):
            tk_root.update()
        n2 = len(app.remote_entries)
        results.append(("断开后自动重连 + 列目录", n2))

        ok = (n1 == 2) and (n2 == 2)
    finally:
        try:
            app.on_close()
        except Exception:
            pass
        server.shutdown()
        shutil.rmtree(root_dir, ignore_errors=True)

    for label, n in results:
        shown = "-" if n < 0 else f"{n} 项"
        print(f"  {label}: {shown}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
