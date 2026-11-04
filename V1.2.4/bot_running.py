# -*- coding: utf-8 -*-
"""查 wx_multi.py 主循环是否已经在跑（给 start_bot.bat 做单实例检查用）。

判定靠 bot_heartbeat.json：
  wx_multi 每轮都会写 {"ts", "pid", "round"}，pid 还活着就说明主循环在跑。

退出码：1 = 已有实例（stdout 打印 pid）；0 = 没在跑。
纯 Python，不走 PowerShell（启动时再拉个 PowerShell 太慢，还容易被环境坑）。
"""
import ctypes
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HB = os.path.join(HERE, "bot_heartbeat.json")

STILL_ACTIVE = 259
QUERY_LIMITED = 0x1000


def pid_alive(pid):
    """检查进程是否存在，兼容 Windows 和 Linux。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    h = k32.OpenProcess(QUERY_LIMITED, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def running_pid():
    try:
        with open(HB, encoding="utf-8-sig") as f:
            pid = int((json.load(f) or {}).get("pid") or 0)
        if not pid_alive(pid):
            return 0
        if os.name != "nt":
            # 心跳里 PID 被复用时，不能停止无关服务。
            from pathlib import Path
            argv = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
            expected = os.fsencode(os.path.join(HERE, "wx_multi.py"))
            if expected not in argv:
                return 0
        return pid
    except (OSError, ValueError, TypeError):
        return 0


def main():
    pid = running_pid()
    if pid:
        print(pid)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
