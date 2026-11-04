"""停止本目录的 Linux 机器人，不停止微信接入或控制面板。"""
import os
import signal
import time

from bot_running import running_pid


def stop():
    pid = running_pid()
    if not pid:
        return 0
    if os.name == "nt":
        raise RuntimeError("Windows 请使用 stop_bot.bat")
    try:
        # bot_launcher 为机器人创建独立会话，同时停止它的发送子进程。
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    deadline = time.monotonic() + 5
    while running_pid() == pid and time.monotonic() < deadline:
        time.sleep(0.1)
    if running_pid() == pid:
        raise RuntimeError("机器人尚未退出，请检查日志")
    return pid


if __name__ == "__main__":
    print("stopped pid=%s" % stop())
