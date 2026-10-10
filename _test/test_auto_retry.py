# -*- coding: utf-8 -*-
"""验证「出错自动重试 / 自动断线重连」真的能把下载救回来（不再需要人工重连）。

故障注入用同目录的 probe_error_recovery.py（那台迷你 FTP 可以按需拒绝连接 / 掐断数据）。
被测对象是**真正的 App**（连 _poll 定时器一起跑），测试只做一件事：pump tk 事件循环。
—— 全程不调用任何重试逻辑，否则测的就不是「自动」，而是我自己手动调的那一行。

⚠️ 每个用例跑在**独立子进程**里：同一进程里反复创建/销毁 Tk 解释器会踩
`Tcl_AsyncDelete: async handler deleted by the wrong thread` 硬崩（实测）。
父进程只负责逐个拉起、汇总结果。

覆盖：
  A. 服务器回 421 Too many connections → 自动重试后下完，MD5 一致
  B. 服务器回 530 Sorry, the maximum number of clients…（TPDC 真实场景）→ 同上
  C. 服务器一直回 530 Login incorrect → 判定为账号问题并停止重试（不是无脑刷 N 次）
  D. aria2c 进程被强杀（对应「断电」）→ 自动重启引擎 + 断点续传下完
  E. 纯函数：退避曲线 + 致命错误码分类
  F. 账目：用户手动移除的任务不得被自动重试拉回来

运行：E:/Python/python.exe _test/test_auto_retry.py
"""

import os
import sys
import time
import hashlib
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import probe_error_recovery as probe
import ftp_accel

PASS, FAIL = [], []
FAST_WAIT = (2, 4)   # 用例里把退避压到 2s/4s，只压时间轴、不改判定逻辑（曲线本身由 E 组断言）


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"：{detail}" if detail else ""))


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pump(root, cond, timeout):
    """pump 事件循环直到 cond 成立或超时（App 的自动重试全在 _poll 定时器里跑）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        root.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


def new_app(port, workdir, conn="4", max_retry="9"):
    root = tk.Tk()
    root.withdraw()
    ftp_accel.apply_theme(root)
    app = ftp_accel.App(root)
    app.var_host.set("127.0.0.1")
    app.var_port.set(str(port))
    app.var_user.set("")
    app.var_pass.set("")
    app.var_mirror.set("")
    app.var_save.set(workdir)
    app.var_conn.set(conn)
    app.var_autoretry.set(True)
    app.var_maxretry.set(max_retry)
    return root, app


def enqueue(app, remote, name):
    """走真实入口把文件加入队列（= 用户在界面上勾选后点「加入下载队列」）。"""
    app.checked = {remote: name}
    app.add_checked()


def logs_of(app):
    return app.txt_log.get("1.0", "end")


def start_file_server(size):
    """起一台可注入故障的迷你 FTP，并造一个 size 字节的测试文件。"""
    root_dir = tempfile.mkdtemp(prefix="ar_root_")
    probe.SERVER_ROOT = root_dir
    src = os.path.join(root_dir, "sample.bin")
    with open(src, "wb") as fh:
        fh.write(os.urandom(size))
    probe.EVENTS.clear()
    server = probe.Server(("127.0.0.1", 0), probe.FlakyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return root_dir, src, md5_of(src), server, port


def dump_logs(app, label):
    print(f"   —— {label}：出问题时的 App 日志 ——")
    for line in logs_of(app).splitlines():
        print("     ", line)


# ------------------------------- 用例 -------------------------------


def case_reject(title, reply, window=120):
    """服务器一直拒绝连接，直到看见 App 重试到第 3 次才放行；观察它是否自己救回来。

    ⚠️ 不按「拒绝 N 条连接」来定行为：aria2 单次尝试会并发开好几条控制连接，
    消耗掉几条拒绝是不固定的（实测时多时少）→ 按次数写就会偶发不触发降档。
    跟着应用自己的重试计数走，才是确定的。
    """
    print(f"\n=== {title} ===")
    root_dir, src, src_md5, server, port = start_file_server(1 << 20)
    workdir = tempfile.mkdtemp(prefix="ar_out_")
    probe.STATE.update({"reject_left": 10 ** 9, "reject_reply": reply,
                        "abort_left": 0, "throttle_bps": 0, "conns": 0})

    saved = (ftp_accel.RETRY_BASE_WAIT, ftp_accel.RETRY_MAX_WAIT)
    ftp_accel.RETRY_BASE_WAIT, ftp_accel.RETRY_MAX_WAIT = FAST_WAIT
    root, app = new_app(port, workdir)
    try:
        enqueue(app, "/sample.bin", "sample.bin")
        out = os.path.join(workdir, "sample.bin")
        # 等它重试到第 3 次（>=3 才会把每文件连接数降档），然后关掉「拒绝」放行
        saw3 = pump(root, lambda: "自动重试 第 3/" in logs_of(app), window)
        probe.STATE["reject_left"] = 0
        done = pump(root, lambda: os.path.exists(out) and md5_of(out) == src_md5, window)
        task = app.tasks.get(app._task_key(workdir, "sample.bin")) or {}
        # MD5 一致可能比 _poll 发现「完成」早一个 tick，多 pump 一会儿再断言注销
        pump(root, lambda: task.get("done") is True, 6)
        logs = logs_of(app)
        attempts = task.get("attempts", 0)
        check(f"{title}：无需人工干预即下完且 MD5 一致", done)
        check(f"{title}：自动重试确实发生过且走到了第 3 次", saw3 and attempts >= 3,
              f"attempts={attempts}")
        check(f"{title}：没被误判成致命错误", not task.get("gave_up"),
              f"gave_up={task.get('gave_up')}")
        check(f"{title}：撞连接数上限后把连接数降档了", "连接数降到" in logs)
        check(f"{title}：完成后注销了重试档案（不会再被拉起来）", task.get("done") is True)
        if FAIL:
            dump_logs(app, title)
    finally:
        ftp_accel.RETRY_BASE_WAIT, ftp_accel.RETRY_MAX_WAIT = saved
        try:
            app.on_close()
        except Exception:
            pass
        server.shutdown()
        shutil.rmtree(root_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


def case_a_persistent_overload():
    case_reject("A. 421 Too many connections", "421 Too many connections")


def case_b_tpdc_style():
    case_reject("B. 530 maximum number of clients",
                "530 Sorry, the maximum number of clients (5) from your host are already connected.")


def case_c_auth_fatal():
    print("\n=== C. 服务器一直拒绝登录（530 Login incorrect）→ 应停止重试，不无脑刷 ===")
    root_dir, _src, _md5, server, port = start_file_server(1 << 20)
    workdir = tempfile.mkdtemp(prefix="ar_out_")
    probe.STATE.update({"reject_left": 10 ** 9, "reject_reply": "530 Login incorrect.",
                        "abort_left": 0, "throttle_bps": 0, "conns": 0})
    root, app = new_app(port, workdir, max_retry="8")
    try:
        enqueue(app, "/sample.bin", "sample.bin")
        key = app._task_key(workdir, "sample.bin")
        got = pump(root, lambda: (app.tasks.get(key) or {}).get("gave_up"), 60)
        task = app.tasks.get(key) or {}
        check("C：识别为账号问题并停止自动重试（gave_up=True）", bool(got))
        check("C：没有把重试额度刷满", task.get("attempts", 99) <= 1,
              f"attempts={task.get('attempts')}")
        check("C：日志明确提示是登录被拒（不是网络抖动）", "拒绝了登录" in logs_of(app))
        if FAIL:
            dump_logs(app, "C")
    finally:
        try:
            app.on_close()
        except Exception:
            pass
        server.shutdown()
        shutil.rmtree(root_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


def case_d_engine_killed():
    print("\n=== D. aria2c 被强杀（对应「断电」）→ 应自动重启引擎并断点续传 ===")
    root_dir, src, src_md5, server, port = start_file_server(4 << 20)   # 4MB
    workdir = tempfile.mkdtemp(prefix="ar_out_")
    # 限速 256KB/s/连接：4MB 要下好几秒，中间来得及把引擎掐掉
    probe.STATE.update({"reject_left": 0, "abort_left": 0,
                        "throttle_bps": 256 * 1024, "conns": 0})
    root, app = new_app(port, workdir, conn="4")

    def in_flight_bytes():
        """aria2 自己报的已完成字节数。

        ⚠️ 不能看落盘文件的体积：引擎带 `--disk-cache=128M`，数据先攒在内存缓存里，
        4MB 这种小文件在下载完成前根本不落盘（实测文件一直是 0 字节）。
        """
        total = 0
        for t in app.engine.tell_active() + app.engine.tell_stopped(0, 20):
            total += int(t.get("completedLength", 0) or 0)
        return total

    try:
        enqueue(app, "/sample.bin", "sample.bin")
        out = os.path.join(workdir, "sample.bin")
        moving = pump(root, lambda: in_flight_bytes() > 0, 40)
        check("D：掐之前确实在传输中（aria2 报告已完成字节 > 0）", moving,
              f"{in_flight_bytes()} B")
        app.engine.proc.kill()                                # 模拟进程被干掉
        done = pump(root, lambda: os.path.exists(out) and md5_of(out) == src_md5, 120)
        check("D：引擎被强杀后仍自动下完且 MD5 一致", done)
        check("D：引擎被自动重启过", app.engine_restarts >= 1,
              f"engine_restarts={app.engine_restarts}")
        check("D：引擎重启后把没下完的任务重新入队了", "引擎重启后重新入队" in logs_of(app))
        if FAIL:
            dump_logs(app, "D")
    finally:
        try:
            app.on_close()
        except Exception:
            pass
        server.shutdown()
        shutil.rmtree(root_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


def case_e_pure_functions():
    print("\n=== E. 纯函数：退避曲线 + 致命错误码分类 ===")
    curve = [ftp_accel.retry_wait(n) for n in (1, 2, 3, 4, 5, 9)]
    check("E：退避 5/10/20/40/60（封顶 60）", curve == [5, 10, 20, 40, 60, 60], str(curve))
    check("E：errorCode 21（FTP 命令失败）可重试", ftp_accel.classify_error(21, "")[0])
    check("E：errorCode 2（超时）可重试", ftp_accel.classify_error(2, "timeout")[0])
    check("E：errorCode 9（磁盘满）不重试", not ftp_accel.classify_error(9, "")[0])
    check("E：errorCode 24（鉴权失败）不重试", not ftp_accel.classify_error(24, "")[0])
    check("E：errorCode 3（文件不存在）不重试", not ftp_accel.classify_error(3, "")[0])
    check("E：默认开启自动重试", ftp_accel.AUTORETRY_DEFAULT is True)
    check("E：默认最多重试 20 次", str(ftp_accel.MAX_RETRY_DEFAULT) == "20")
    check("E：引擎自动重启有上限（不会无限重启）", ftp_accel.MAX_ENGINE_RESTARTS > 0)


def case_f_bookkeeping():
    print("\n=== F. 账目：手动移除的任务不该被自动重试拉回来 ===")
    workdir = tempfile.mkdtemp(prefix="ar_bk_")
    root, app = new_app("1", workdir)          # 不需要真 FTP，只查账本逻辑
    try:
        task = app._register_task("fake-gid-1", "x.bin", ["ftp://h/x.bin"],
                                  {"out": "x.bin"}, 8, workdir, "", "")
        task["gave_up"] = True
        check("F：放弃重试的行显示为「需人工」",
              "需人工" in app._retry_label("fake-gid-1", {"errorCode": 21}, "出错 21"),
              app._retry_label("fake-gid-1", {"errorCode": 21}, "出错 21"))
        check("F：身份键 = 保存目录 + 文件名（换 gid 也不丢身份）",
              app._task_key(workdir, "x.bin") == os.path.normcase(os.path.join(workdir, "x.bin")),
              app._task_key(workdir, "x.bin"))
        task["gave_up"] = False
        app._forget_task("fake-gid-1")          # = 用户点了「移除选中」
        check("F：移除后标记 done", task.get("done") is True)
        n = app._handle_error_tasks({"fake-gid-1": {"status": "error", "errorCode": 21}})
        check("F：移除后自动重试不再动它", n == 0, f"retried={n}")
        app.var_maxretry.set("999")
        check("F：重试上限读的是界面上的框（1–999）", app._max_retry() == 999,
              str(app._max_retry()))
        app.var_maxretry.set("abc")
        check("F：非法输入回落到默认 20 次", app._max_retry() == 20, str(app._max_retry()))
    finally:
        try:
            app.on_close()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


CASES = {
    "A": ("A. 421 Too many connections（一直拒绝）", case_a_persistent_overload),
    "B": ("B. 530 maximum number of clients（TPDC 场景）", case_b_tpdc_style),
    "C": ("C. 530 Login incorrect（账号问题）", case_c_auth_fatal),
    "D": ("D. aria2c 被强杀（断电）", case_d_engine_killed),
    "E": ("E. 纯函数（退避/错误码分类）", case_e_pure_functions),
    "F": ("F. 账目（手动移除不被拉回）", case_f_bookkeeping),
}

CONFIG_BACKUP = None


def backup_config():
    global CONFIG_BACKUP
    if os.path.exists(ftp_accel.CONFIG_FILE):
        with open(ftp_accel.CONFIG_FILE, "rb") as fh:
            CONFIG_BACKUP = fh.read()


def restore_config():
    # App 关闭时会把界面上的连接/目录设置写回 config.json，跑测试别把用户的配置冲掉
    if CONFIG_BACKUP is not None:
        with open(ftp_accel.CONFIG_FILE, "wb") as fh:
            fh.write(CONFIG_BACKUP)


def run_child(name):
    """在独立子进程里跑一个用例（避免同进程反复建/销 Tk 解释器导致硬崩）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--case", name],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=HERE)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.startswith("  [") or line.startswith("===") or line.lstrip().startswith("——") \
                or line.startswith("     "):
            print(line)
    return proc.returncode == 0, out


def main():
    if "--case" in sys.argv:
        name = sys.argv[sys.argv.index("--case") + 1]
        backup_config()
        try:
            CASES[name][1]()
        finally:
            restore_config()
        print(f"\nCASE {name}：通过 {len(PASS)} 失败 {len(FAIL)}")
        return 0 if not FAIL else 1

    print("每个用例跑在独立子进程里（Tk 解释器不能在同一进程反复建/销）\n")
    ok_cases, bad_cases = [], []
    for name, (title, _fn) in CASES.items():
        ok, out = run_child(name)
        (ok_cases if ok else bad_cases).append(name)
        if not ok:
            print(f"  ^^ 用例 {name} 未通过，完整输出如下：")
            print(out[-2500:])
    print(f"\n用例通过 {len(ok_cases)}/{len(CASES)}：{', '.join(ok_cases)}")
    if bad_cases:
        print("未通过用例：" + ", ".join(bad_cases))
    print("RESULT:", "PASS" if not bad_cases else "FAIL")
    return 0 if not bad_cases else 1


if __name__ == "__main__":
    sys.exit(main())
