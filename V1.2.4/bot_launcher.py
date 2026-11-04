# -*- coding: utf-8 -*-
"""脱离会话启动 wx_multi（不经过 cmd 的重定向，避免「另一个程序正在使用此文件」）。

用法：python bot_launcher.py [日志名]
  - 已在跑就打印 pid 并退出（退出码 4）
  - 起得来就打印新 pid 并退出（退出码 0）

为什么不用 start_bot.bat：cmd 的 1>>log 在文件被占时会静默失败，整条输出链断掉，
进程秒退还看不到原因。这里由 Python 自己打开日志文件句柄交给子进程，稳。
"""
import json
import os
import subprocess
import sys
import time
from bot_running import running_pid

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable if os.path.basename(sys.executable).lower().startswith("python") else os.path.join(HERE, "python", "python.exe")
HB = os.path.join(HERE, "bot_heartbeat.json")

DETACHED = 0x00000008
NEW_GROUP = 0x00000200


def main():
    hit = running_pid()
    if hit:
        print("[launcher] 已经在跑，pid %s" % hit)
        return 4

    log_name = (sys.argv[1] if len(sys.argv) > 1 else "run_bot.log")
    log_path = os.path.join(HERE, log_name)
    err_path = os.path.splitext(log_path)[0] + ".err"

    out = open(log_path, "a", encoding="utf-8", errors="replace")
    err = open(err_path, "a", encoding="utf-8", errors="replace")
    out.write("==== start %s (launcher) ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out.flush()

    options = {"creationflags": DETACHED | NEW_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(
        [PY, "-u", os.path.join(HERE, "wx_multi.py"),
         "--rooms", os.path.join(HERE, "rooms.json"),
         "--rounds", "999999", "--gap", "2"],
        cwd=HERE,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        **options,
        close_fds=True,
    )
    out.close()
    err.close()          # 句柄留给子进程，父进程退出不影响

    time.sleep(6)
    if p.poll() is not None:
        print("[launcher] 起来了立刻退出，code=%s，看 %s" % (p.returncode, os.path.basename(err_path)))
        return 1
    print("[launcher] 已启动，pid %s" % p.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
