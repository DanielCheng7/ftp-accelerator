# -*- coding: utf-8 -*-
"""
FTP 加速下载器 —— 下载参数与引擎参数"真的生效"验证

不满足于"代码里写了参数"，本测试做两件事：
  1. 把参数交给真的 aria2，再用 aria2.getOption / getGlobalOption 把实际生效的值读回来；
  2. 对 --no-conf 做 A/B：先造一份用户级 aria2.conf 并证明它确实能把速度掐死（阴性对照），
     再证明应用的启动参数能挡住它 —— 否则这条"优化"只是自我安慰。

对照做法：aria2 的默认配置路径取自 HOME，所以把 HOME 指到一个临时目录、
在那里放 .config/aria2/aria2.conf，就能真实复现"用户机器上存在一份 aria2.conf"，
而不必去动用户自己的配置目录。

运行：E:/Python/python.exe _test/test_speed_opts.py
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import subprocess
import threading
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import ftp_accel
import test_ftp_download as mini        # 复用迷你 FTP 服务器与 MD5 工具

FAIL = 0


def check(label, got, want):
    global FAIL
    ok = str(got) == str(want)
    if not ok:
        FAIL += 1
    print(f"  [{'OK ' if ok else 'BAD'}] {label}：实际={got!r} 期望={want!r}")


# ----------------------------- aria2 直连小工具 -----------------------------

def rpc_call(port, secret, method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "probe",
        "method": method,
        "params": [f"token:{secret}"] + (params or []),
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/jsonrpc",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "RPC 失败"))
    return data.get("result")


def spawn(args, port, secret, env=None):
    """用给定的完整命令行拉起 aria2c，并等它 RPC 就绪。"""
    proc = subprocess.Popen(
        args,
        env=env,
        creationflags=ftp_accel.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(f"aria2 秒退，返回码 {proc.returncode}")
        try:
            rpc_call(port, secret, "aria2.getVersion")
            return proc
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("aria2 启动超时")


def read_global_limit(no_conf=True, home=None):
    """起一个 aria2，读回它的全局限速与并发数，然后关掉。

    no_conf=False 时把应用启动参数里的 --no-conf=true 摘掉，
    用来模拟"没有做这项优化"的老版本行为。
    """
    port = ftp_accel.free_port()
    secret = uuid.uuid4().hex
    args = list(ftp_accel.build_engine_args(port, secret))
    if not no_conf:
        args = [a for a in args if a != "--no-conf=true"]

    env = None
    if home:
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home

    proc = spawn(args, port, secret, env)
    try:
        opt = rpc_call(port, secret, "aria2.getGlobalOption")
        return opt.get("max-overall-download-limit"), opt.get("max-concurrent-downloads")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


# --------------------------------- 各项检查 ---------------------------------

def test_clamp_int():
    print("— clamp_int 边界收敛 —")
    check("空串 → 默认", ftp_accel.clamp_int("", 1, 16, "8"), 8)
    check("手输 20 → 钳到 16", ftp_accel.clamp_int("20", 1, 16, "8"), 16)
    check("手输 0 → 钳到 1", ftp_accel.clamp_int("0", 1, 16, "8"), 1)
    check("乱输 abc → 默认", ftp_accel.clamp_int("abc", 1, 16, "8"), 8)
    check("带空格 ' 4 ' → 4", ftp_accel.clamp_int(" 4 ", 1, 16, "8"), 4)
    check("小数 3.7 → 3", ftp_accel.clamp_int("3.7", 1, 16, "8"), 3)
    check("并发 99 → 钳到 10", ftp_accel.clamp_int("99", 1, ftp_accel.MAX_JOBS, "1"), 10)


def test_option_dict():
    print("— build_download_options 内容 —")
    opts = ftp_accel.build_download_options("C:/tmp/dl", "8", "u1", "p1")
    check("split", opts["split"], "8")
    check("max-connection-per-server", opts["max-connection-per-server"], "8")
    check("min-split-size", opts["min-split-size"], "1M")
    check("file-allocation", opts["file-allocation"], "none")
    check("retry-wait", opts["retry-wait"], "5")
    check("lowest-speed-limit", opts["lowest-speed-limit"], "1K")
    check("ftp-user", opts["ftp-user"], "u1")
    anon = ftp_accel.build_download_options("C:/tmp/dl", "8")
    check("匿名时不带 ftp-user 键", "ftp-user" in anon, False)


def test_no_conf_ab(tmp):
    """A/B 对照：证明 --no-conf 真的挡住了用户级配置。

    只断言"我们的参数里有 --no-conf"毫无意义 —— 必须证明：
      ① 不关 conf 时，那份 conf 里的限速真的被套上了（否则 decoy 根本没被读，测试就是安慰剂）
      ② 关掉 conf 后，限速不再生效
    """
    print("— --no-conf A/B 对照（HOME 指向临时目录，不触碰用户真实配置）—")

    fake_home = os.path.join(tmp, "fake_home")
    conf_dir = os.path.join(fake_home, ".config", "aria2")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "aria2.conf"), "w", encoding="utf-8") as fh:
        fh.write("max-overall-download-limit=1K\n")
        fh.write("max-concurrent-downloads=1\n")

    limit, jobs = read_global_limit(no_conf=False, home=fake_home)
    check("① 对照组：decoy 限速确实被读到（阴性对照有效）", limit, "1024")
    check("① 对照组：decoy 并发数确实被读到", jobs, "1")

    args = ftp_accel.build_engine_args(0, "x")
    check("应用启动参数里含 --no-conf=true", "--no-conf=true" in args, True)

    limit2, jobs2 = read_global_limit(no_conf=True, home=fake_home)
    check("② 实验组：关掉 conf 后不再有限速", limit2, "0")
    check("② 实验组：并发数回到 aria2 默认 5", jobs2, "5")

    check("HOME 指向的假配置目录未被程序读取后篡改",
          open(os.path.join(conf_dir, "aria2.conf"), encoding="utf-8").read().count("1K"), 1)


def test_download_options_live(tmp):
    """把 build_download_options 交给真的 aria2，再读回任务实际生效的选项。

    注意：aria2 回读时会把 1M / 1K 这类写法归一化成字节数（1M -> 1048576），
    所以这里按字节比对 —— 反而是"参数确实进了引擎"的更强证据。
    """
    print("— 下载任务参数读回（aria2.getOption）—")

    root = os.path.join(tmp, "srv")
    out = os.path.join(tmp, "out")
    os.makedirs(root, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    mini.SERVER_ROOT = root
    src = os.path.join(root, "sample.bin")
    payload = os.urandom(1 << 20)
    with open(src, "wb") as fh:
        for _ in range(8):
            fh.write(payload)
    src_md5 = mini.md5_of(src)

    server = mini.MiniFTPServer(("127.0.0.1", 0), mini.FTPHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    engine = ftp_accel.Aria2Engine(lambda m: None)
    engine.start()
    try:
        url = f"ftp://127.0.0.1:{port}/sample.bin"
        opts = dict(ftp_accel.build_download_options(out, "8"))
        opts["out"] = "sample.bin"
        gid = engine.add_uri(url, opts)

        time.sleep(1.5)
        live = engine.rpc("aria2.getOption", [gid])
        check("split", live.get("split"), "8")
        check("max-connection-per-server", live.get("max-connection-per-server"), "8")
        check("min-split-size（1M → 字节）", int(live.get("min-split-size")), 1048576)
        check("lowest-speed-limit（1K → 字节）", int(live.get("lowest-speed-limit")), 1024)
        check("file-allocation", live.get("file-allocation"), "none")
        check("retry-wait", live.get("retry-wait"), "5")
        check("continue", live.get("continue"), "true")

        state = None
        for _ in range(60):
            time.sleep(0.5)
            for task in engine.tell_active() + engine.tell_waiting(0, 20) + engine.tell_stopped(0, 20):
                if task["gid"] == gid:
                    state = task
            if state and state["status"] in ("complete", "error", "removed"):
                break
        check("下载状态", state["status"], "complete")
        check("落盘 MD5 一致", mini.md5_of(os.path.join(out, "sample.bin")), src_md5)
    finally:
        engine.stop()
        server.shutdown()


def main():
    global FAIL
    tmp = tempfile.mkdtemp(prefix="ftp_opts_")
    try:
        test_clamp_int()
        test_option_dict()
        test_no_conf_ab(tmp)
        test_download_options_live(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nRESULT: {'PASS' if FAIL == 0 else f'FAIL ({FAIL} 项不符)'}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
