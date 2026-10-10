# -*- coding: utf-8 -*-
"""探针：aria2 在 FTP 出错时到底会不会自己重试？

不做任何假设 —— 用的是**应用真正跑的那份参数**（ftp_accel.build_download_options /
build_engine_args，经 Aria2Engine 启动），对着一个可注入故障的迷你 FTP 服务器观察：
  场景 A1：服务器对前 N 条连接的 USER 回 "421 Too many connections"（Pure-FTPd 连接数满）
  场景 A2：服务器对前 N 条连接的 USER 回 "530 Sorry, the maximum number of clients ..."（同上，另一写法）
  场景 B ：数据连接传到一半被服务器掐断一次，之后正常
  场景 C ：每次都掐断（永久故障）—— 看它最终会不会落到 error（观察窗口有限，避免 max-tries=0 无限循环）

运行：E:/Python/python.exe _test/probe_error_recovery.py
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
from ftp_accel import build_download_options, build_engine_args, Aria2Engine

SERVER_ROOT = None
EVENTS = []

STATE = {
    "reject_left": 0,      # 还要拒绝多少条控制连接
    "reject_reply": "",    # 拒绝时回的应答
    "abort_left": 0,       # 还要掐断多少次数据传输
    "abort_after": 256 * 1024,
    "conns": 0,
    "throttle_bps": 0,     # >0 时按此速率限速（字节/秒/连接），给"运行中掐引擎"的用例留时间
}


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def hard_close(sock):
    """发 RST 直接砸断，模拟服务器/中间设备粗暴掉线。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        b"\x01\x00\x00\x00\x00\x00\x00\x00")
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


class FlakyHandler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        STATE["conns"] += 1
        self.pasv = None
        self.rest = 0

    def reply(self, text):
        try:
            self.wfile.write((text + "\r\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def handle(self):
        self.reply("220 FlakyFTP ready")
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
                if STATE["reject_left"] > 0:
                    STATE["reject_left"] -= 1
                    self.reply(STATE["reject_reply"])
                    EVENTS.append(("REJECT", STATE["reject_reply"]))
                    # ⚠️ 这里必须"优雅关闭"，不能用 hard_close 发 RST：
                    # RST 会把还没被客户端读走的那行应答一起丢掉，对方看到的是
                    # "连接被重置"而不是"530 Login incorrect." —— 真实服务器也是优雅关闭。
                    return
                self.reply("331 need password")
            elif cmd == "PASS":
                self.reply("230 logged in")
            elif cmd == "SYST":
                self.reply("215 UNIX Type: L8")
            elif cmd == "FEAT":
                self.wfile.write(b"211-Features:\r\n REST STREAM\r\n SIZE\r\n UTF8\r\n211 End\r\n")
                self.wfile.flush()
            elif cmd in ("OPTS", "TYPE", "MODE", "STRU"):
                self.reply("200 ok")
            elif cmd == "PWD":
                self.reply('257 "/"')
            elif cmd == "CWD":
                self.reply("250 ok")
            elif cmd == "SIZE":
                path = os.path.join(SERVER_ROOT, arg.lstrip("/"))
                self.reply(f"213 {os.path.getsize(path)}" if os.path.isfile(path) else "550 not found")
            elif cmd == "REST":
                self.rest = int(arg or 0)
                self.reply("350 restarting at " + str(self.rest))
            elif cmd == "PASV":
                self.pasv = socket.socket()
                self.pasv.bind(("127.0.0.1", 0))
                self.pasv.listen(1)
                p = self.pasv.getsockname()[1]
                self.reply(f"227 Entering Passive Mode (127,0,0,1,{p // 256},{p % 256})")
            elif cmd == "EPSV":
                self.pasv = socket.socket()
                self.pasv.bind(("127.0.0.1", 0))
                self.pasv.listen(1)
                p = self.pasv.getsockname()[1]
                self.reply(f"229 Entering Extended Passive Mode (|||{p}|)")
            elif cmd in ("MLSD", "LIST", "NLST"):
                self.do_list()
            elif cmd == "RETR":
                self.do_retr(arg)
            elif cmd == "QUIT":
                self.reply("221 bye")
                break
            else:
                self.reply("502 not implemented")

    def do_list(self):
        self.reply("150 opening data")
        try:
            conn, _ = self.pasv.accept()
        except Exception:
            return
        try:
            for name in sorted(os.listdir(SERVER_ROOT)):
                p = os.path.join(SERVER_ROOT, name)
                typ = "dir" if os.path.isdir(p) else f"file;size={os.path.getsize(p)}"
                conn.sendall(f"type={typ}; {name}\r\n".encode())
        finally:
            conn.close()
            self.pasv.close()
        self.reply("226 done")

    def do_retr(self, name):
        path = os.path.join(SERVER_ROOT, name.lstrip("/"))
        if not os.path.isfile(path):
            self.reply("550 not found")
            return
        start, self.rest = self.rest, 0
        EVENTS.append(("RETR", name, start))
        self.reply("150 opening data")
        try:
            conn, _ = self.pasv.accept()
        except Exception:
            return

        abort = STATE["abort_left"] > 0
        if abort:
            STATE["abort_left"] -= 1

        sent = 0
        throttle = STATE["throttle_bps"]
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    sent += len(chunk)
                    if throttle:
                        time.sleep(len(chunk) / throttle)
                    if abort and sent >= STATE["abort_after"]:
                        EVENTS.append(("ABORT", name, start + sent))
                        hard_close(conn)
                        hard_close(self.connection)
                        return
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                self.pasv.close()
            except Exception:
                pass
        self.reply(f"226 transfer complete ({sent} bytes)")


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run_case(title, setup_fn, expect_recover, window=45):
    """跑一个场景；window 是观察窗口（秒），到点就停 —— 防止 max-tries=0 无限循环。"""
    for k in STATE:
        if k in ("reject_left", "abort_left"):
            STATE[k] = 0
    STATE["conns"] = 0
    EVENTS.clear()
    setup_fn()

    server = Server(("127.0.0.1", 0), FlakyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    workdir = tempfile.mkdtemp(prefix="probe_out_")
    engine = Aria2Engine(lambda m: None)
    engine.start()

    # ⚠️ 用应用真正跑的那份参数：引擎启动参数 + 每任务选项
    print(f"  引擎参数含 max-tries=0（不限次）: {'--max-tries=0' in ' '.join(build_engine_args(1, 'x'))}")
    opts = dict(build_download_options(workdir, 4, "", ""))
    opts["out"] = "sample.bin"

    state = None
    recovered = False
    try:
        gid = engine.add_uri(f"ftp://127.0.0.1:{port}/sample.bin", opts)
        t0 = time.time()
        last = None
        while time.time() - t0 < window:
            time.sleep(0.5)
            for task in engine.tell_active() + engine.tell_waiting(0, 20) + engine.tell_stopped(0, 20):
                if task["gid"] == gid:
                    state = task
            if state:
                cur = (state["status"], state.get("errorCode"))
                if cur != last:
                    print(f"  [{time.time() - t0:5.1f}s] status={state['status']}"
                          f" errorCode={state.get('errorCode', '')} "
                          f"{state.get('errorMessage', '')[:60]}")
                    last = cur
            if state and state["status"] == "complete":
                recovered = True
                break
    finally:
        engine.stop()
        server.shutdown()

    out = os.path.join(workdir, "sample.bin")
    ok_md5 = os.path.exists(out) and md5_of(out) == SRC_MD5
    print(f"  服务器侧事件：{EVENTS}")
    print(f"  控制连接数：{STATE['conns']}   最终 status={state and state['status']} "
          f"errorCode={state and state.get('errorCode')}   落盘MD5一致={ok_md5}")
    verdict = "自主恢复" if recovered else "未恢复（停在 error/窗口超时）"
    print(f"  >>> {verdict}    （期望：{'自主恢复' if expect_recover else '未恢复 -> 需要应用层兜底'}）\n")

    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
    return recovered


SRC_MD5 = None

if __name__ == "__main__":
    root = tempfile.mkdtemp(prefix="probe_root_")
    SERVER_ROOT = root
    payload = os.urandom(1 << 20)
    src = os.path.join(root, "sample.bin")
    with open(src, "wb") as fh:
        for _ in range(4):          # 4MB：min-split-size 1M -> 最多 4 段
            fh.write(payload)
    SRC_MD5 = md5_of(src)
    print(f"测试文件 {src}  4MB  MD5 {SRC_MD5}\n")

    print("=== 场景 A1：前 3 条连接被回 421 Too many connections，之后正常 ===")
    run_case("421", lambda: STATE.update(reject_left=3, reject_reply="421 Too many connections"), True)

    print("=== 场景 A2：前 3 条连接被回 530 maximum number of clients（Pure-FTPd 写法）===")
    run_case("530", lambda: STATE.update(
        reject_left=3,
        reject_reply="530 Sorry, the maximum number of clients (3) from your host are already connected."), True)

    print("=== 场景 B：数据传输被掐断一次，之后正常 ===")
    def _b():
        STATE.update(abort_left=1, abort_after=256 * 1024)
    run_case("abort-once", _b, True)

    print("=== 场景 C：每次数据传输都被掐断（永久故障，观察 45s）===")
    def _c():
        STATE.update(abort_left=10 ** 9, abort_after=256 * 1024)
    run_case("abort-always", _c, False)

    import shutil
    shutil.rmtree(root, ignore_errors=True)
