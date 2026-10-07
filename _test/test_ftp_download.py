# -*- coding: utf-8 -*-
"""
FTP 加速下载器 —— 端到端测试

用一个自研的迷你 FTP 服务器（标准库 socket 实现，支持 REST 分段）配合 aria2，
验证：多连接分段下载、进度上报、落盘完整性（MD5）。

运行：E:/Python/python.exe _test/test_ftp_download.py
"""

import os
import sys
import time
import socket
import hashlib
import threading
import socketserver
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ftp_accel

SERVER_ROOT = None
EVENTS = []          # 记录服务器收到的 REST / RETR，用于证明"分段"真的发生了


class MiniFTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        # aria2 下完一段会立即关闭数据连接，服务器此时仍在写入属正常现象，静默即可
        pass


class FTPHandler(socketserver.StreamRequestHandler):
    THROTTLE_BPS = 0   # >0 时按此速率限速（字节/秒，每连接），用于模拟慢速服务器

    def setup(self):
        super().setup()
        self.pasv = None
        self.rest = 0

    def reply(self, text):
        self.wfile.write((text + "\r\n").encode("utf-8"))
        self.wfile.flush()

    def handle(self):
        self.reply("220 MiniFTP ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            cmd = parts[0].upper()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "USER":
                self.reply("331 need password")
            elif cmd == "PASS":
                self.reply("230 logged in")
            elif cmd == "SYST":
                self.reply("215 UNIX Type: L8")
            elif cmd == "FEAT":
                self.wfile.write(
                    b"211-Features:\r\n REST STREAM\r\n SIZE\r\n UTF8\r\n211 End\r\n"
                )
                self.wfile.flush()
            elif cmd in ("OPTS", "TYPE", "MODE", "STRU"):
                self.reply("200 ok")
            elif cmd == "PWD":
                self.reply('257 "/"')
            elif cmd == "CWD":
                self.reply("250 ok")
            elif cmd == "SIZE":
                path = os.path.join(SERVER_ROOT, arg.lstrip("/"))
                if os.path.isfile(path):
                    self.reply(f"213 {os.path.getsize(path)}")
                else:
                    self.reply("550 not found")
            elif cmd == "REST":
                self.rest = int(arg)
                self.reply("350 restarting at " + arg)
            elif cmd == "PASV":
                self.do_pasv()
            elif cmd == "EPSV":
                self.do_epsv()
            elif cmd in ("MLSD", "LIST", "NLST"):
                self.do_list()
            elif cmd == "RETR":
                self.do_retr(arg)
            elif cmd == "QUIT":
                self.reply("221 bye")
                break
            else:
                self.reply("502 not implemented")

    def do_pasv(self):
        self.pasv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.pasv.bind(("127.0.0.1", 0))
        self.pasv.listen(1)
        port = self.pasv.getsockname()[1]
        self.reply(f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})")

    def do_epsv(self):
        self.pasv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.pasv.bind(("127.0.0.1", 0))
        self.pasv.listen(1)
        port = self.pasv.getsockname()[1]
        self.reply(f"229 Entering Extended Passive Mode (|||{port}|)")

    def accept_data(self):
        conn, _ = self.pasv.accept()
        return conn

    def do_list(self):
        self.reply("150 opening data")
        conn = self.accept_data()
        try:
            for name in sorted(os.listdir(SERVER_ROOT)):
                path = os.path.join(SERVER_ROOT, name)
                if os.path.isdir(path):
                    conn.sendall(f"type=dir; {name}\r\n".encode())
                else:
                    conn.sendall(f"type=file;size={os.path.getsize(path)}; {name}\r\n".encode())
        finally:
            conn.close()
            self.pasv.close()
        self.reply("226 done")

    def do_retr(self, name):
        path = os.path.join(SERVER_ROOT, name.lstrip("/"))
        if not os.path.isfile(path):
            self.reply("550 not found")
            return
        start = self.rest
        self.rest = 0
        EVENTS.append(("RETR", name, start))
        self.reply("150 opening data")
        conn = self.accept_data()
        sent = 0
        throttle = self.THROTTLE_BPS
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    sent += len(chunk)
                    if throttle > 0:
                        time.sleep(len(chunk) / throttle)
        finally:
            conn.close()
            self.pasv.close()
        self.reply(f"226 transfer complete ({sent} bytes)")


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    global SERVER_ROOT

    SERVER_ROOT = tempfile.mkdtemp(prefix="ftp_root_")
    workdir = tempfile.mkdtemp(prefix="ftp_out_")

    # 造一个 8MB 测试文件（够触发多段切分：min-split-size 设为 1M -> 最多 8 段）
    src = os.path.join(SERVER_ROOT, "sample.bin")
    payload = os.urandom(1 << 20)
    with open(src, "wb") as fh:
        for _ in range(8):
            fh.write(payload)
    src_md5 = md5_of(src)
    print(f"测试文件：{src}  大小 {os.path.getsize(src)}  MD5 {src_md5}")

    server = MiniFTPServer(("127.0.0.1", 0), FTPHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"迷你 FTP 服务器：127.0.0.1:{port}")

    engine = ftp_accel.Aria2Engine(print)
    engine.start()

    try:
        url = f"ftp://127.0.0.1:{port}/sample.bin"
        gid = engine.add_uri(url, {
            "dir": workdir,
            "out": "sample.bin",
            "split": "8",
            "max-connection-per-server": "8",
            "min-split-size": "1M",
            "continue": "true",
            "max-tries": "2",
            "timeout": "30",
        })
        print(f"任务 gid：{gid}")

        state = None
        for _ in range(60):
            time.sleep(0.5)
            for task in engine.tell_active() + engine.tell_waiting(0, 20) + engine.tell_stopped(0, 20):
                if task["gid"] == gid:
                    state = task
            if state and state["status"] in ("complete", "error", "removed"):
                break
            if state:
                print(f"  {state['status']}  {state.get('completedLength')}/{state.get('totalLength')}"
                      f"  {state.get('downloadSpeed')} B/s")

        print("最终状态：", state["status"], "errorCode", state.get("errorCode"), state.get("errorMessage"))
    finally:
        engine.stop()
        server.shutdown()

    out = os.path.join(workdir, "sample.bin")
    if not os.path.exists(out):
        print("!! 文件未落盘")
        return 1
    out_md5 = md5_of(out)
    print(f"下载文件：{out}  大小 {os.path.getsize(out)}  MD5 {out_md5}")
    print(f"MD5 一致：{out_md5 == src_md5}")

    starts = sorted(s for (_, _, s) in EVENTS if s > 0)
    print(f"服务器收到的分段起点（>0 表示用了 REST 分段）：{starts}")
    print(f"RETR 次数：{len(EVENTS)}")

    ok = out_md5 == src_md5 and len(starts) >= 1
    print("RESULT:", "PASS" if ok else "FAIL")

    import shutil
    shutil.rmtree(SERVER_ROOT, ignore_errors=True)
    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
