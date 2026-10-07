# -*- coding: utf-8 -*-
"""FTP 下载速度诊断

对同一个文件用 1 / 2 / 4 / 8 / 16 条连接各下一小段，对比实际速度，用来判断瓶颈在哪：

  速度随连接数近似线性上升  -> 服务器按「单连接」限速，加连接有效
  速度几乎不变              -> 服务器按「单 IP/账号总量」限速，或整个服务器出口拥塞
                              此时再加连接也没用

用法：填好下面 CONFIG，然后运行
    E:\\Python\\python.exe _test\\diagnose_speed.py
"""

import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ftp_accel

# ================================ 改这里 ================================
CONFIG = {
    "host": "ftp2.tpdc.ac.cn",       # 也可以换成 ftp3.tpdc.ac.cn 对比
    "port": 6201,
    "user": "download_54928735",     # 数据页面给的账号
    "password": "91942660",          # 数据页面给的密码
    "remote_file": "/换成FTP上文件的完整路径/xxx.zip",   # 挑一个几百 MB 的文件来测
    "seconds": 20,                   # 每档连接数测多少秒
}

CONNECTION_LADDER = [1, 2, 4, 8, 16]   # 要扫描的连接数
WARMUP = 5                             # 预热秒数，不计入平均
# ========================================================================


def measure(engine, cfg, conn, out_dir):
    """用指定连接数下载一段时间，返回 (平均速度 B/s, 实测最大连接数)。"""
    out_name = f"speedtest_c{conn}.bin"
    host = cfg["host"]
    port = cfg["port"]
    remote = cfg["remote_file"]

    opts = {
        "dir": out_dir,
        "out": out_name,
        "split": str(conn),
        "max-connection-per-server": str(conn),
        "min-split-size": "4M",
        "continue": "false",
        "file-allocation": "none",
        "max-tries": "1",
        "timeout": "30",
        "connect-timeout": "20",
        "auto-file-renaming": "false",
        "allow-overwrite": "true",
    }
    if cfg["user"]:
        opts["ftp-user"] = cfg["user"]
        opts["ftp-passwd"] = cfg["password"]

    url = f"ftp://{host}:{port}{remote}"
    gid = engine.add_uri(url, opts)
    print(f"    任务 gid={gid}")

    samples = []
    max_conn = 0
    t0 = time.time()
    while time.time() - t0 < cfg["seconds"]:
        time.sleep(1)
        for task in engine.tell_active():
            if task["gid"] == gid:
                try:
                    max_conn = max(max_conn, int(task.get("connections", 0)))
                except (TypeError, ValueError):
                    pass
                if time.time() - t0 > WARMUP:
                    samples.append(int(task.get("downloadSpeed", 0) or 0))

    engine.remove(gid)
    time.sleep(1)

    for name in (out_name, out_name + ".aria2"):
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    if not samples:
        print("    [!] 没采到速度样本：任务可能在测试时段内已下完，或根本没启动。")
        print("        请换一个更大的测试文件，或把 seconds 调大。")
    avg = sum(samples) / len(samples) if samples else 0
    return avg, max_conn


def run_scan(cfg):
    if "换成" in cfg["remote_file"]:
        print("请先在脚本顶部 CONFIG 里填好 remote_file（FTP 上某个文件的完整路径）")
        return 1

    out_dir = tempfile.mkdtemp(prefix="ftpspeed_")
    engine = ftp_accel.Aria2Engine(print)
    engine.start()

    results = []
    try:
        print(f"\n开始测速：{cfg['host']}:{cfg['port']}{cfg['remote_file']}")
        print(f"每档 {cfg['seconds']} 秒（前 {WARMUP} 秒预热不计）\n")
        for conn in CONNECTION_LADDER:
            print(f"  [{conn} 连接] 测试中 ...")
            speed, max_conn = measure(engine, cfg, conn, out_dir)
            results.append((conn, speed, max_conn))
            print(f"    平均 {ftp_accel.human_size(speed)}/s   实测最大连接数 {max_conn}")
    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        engine.stop()
        shutil.rmtree(out_dir, ignore_errors=True)

    if not results:
        return 1

    valid = [r for r in results if r[1] > 0]
    if not valid:
        print("所有档位都没采到速度样本，无法判断 —— 请换一个更大的测试文件。")
        return 1

    base = valid[0][1]
    header = f"{'连接数':<8}{'平均速度':<16}{'相对首档':<12}{'实测连接':<10}"
    print("\n" + "=" * 54)
    print(header)
    print("-" * 54)
    for conn, speed, max_conn in results:
        if speed > 0:
            col_speed = ftp_accel.human_size(speed) + "/s"
            ratio = f"{speed / base:.1f}"
        else:
            col_speed = "无样本"
            ratio = "-"
        print(f"{conn:<8}{col_speed:<16}{ratio:<12}{max_conn:<10}")
    print("=" * 54)

    best = max(valid, key=lambda r: r[1])
    print(f"\n最快：{best[0]} 连接 -> {ftp_accel.human_size(best[1])}/s")
    for conn, _, max_conn in results:
        if max_conn < conn:
            print(f"注意：设定 {conn} 连接，实测只建立了 {max_conn} 条"
                  f" -> 服务器限制了单 IP 并发连接数。")
            break
    if best[1] / base > 2.5 and best[0] >= 4:
        print("结论：速度随连接数明显上升 -> 服务器是「单连接限速」，加连接有效。")
        print("      把「每文件连接数」设到速度不再上涨的那一档即可。")
    else:
        print("结论：连接数增加但速度基本没变 -> 瓶颈在服务器「单 IP/账号总限速」或出口拥塞。")
        print("      再加连接没用。可尝试：换 ftp2/ftp3 节点、换凌晨时段、或联系数据方提高配额。")
    return 0


def main():
    return run_scan(CONFIG)


if __name__ == "__main__":
    sys.exit(main())
