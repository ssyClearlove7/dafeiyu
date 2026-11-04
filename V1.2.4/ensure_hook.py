"""Load the installed 4.1.10.27 hook when normal DLL proxy loading is unavailable."""
import ctypes
import importlib.util
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_send


def main():
    import config
    if config.wechat_backend() == "native":
        ok = hook_send.available()
        print("[native] ready" if ok else "[native] 请先启动 agent-wechat 并扫码登录", flush=True)
        return 0 if ok else 1
    if hook_send.available():
        print("[hook] HTTP service is ready", flush=True)
        return 0
    helper = HERE / "wx41/hook/src/wx-basic-bridge-main/python/inject/wechat_inject.py"
    dll = HERE / "wx41/hook/aixed/version_411027.dll"
    expected = HERE / "Weixin-4.1.10.27/Weixin/4.1.10.27/Weixin.dll"
    if not all(p.is_file() for p in (helper, dll, expected)):
        print("[hook] Missing installed hook files or fixed-version WeChat", flush=True)
        return 1
    spec = importlib.util.spec_from_file_location("installed_wechat_loader", helper)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    candidates = set()

    def collect(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value == "微信":
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            candidates.add(pid.value)
        return True

    user32.EnumWindows(callback_type(collect), 0)
    for pid in sorted(candidates):
        if not loader._find_remote_module_base(pid, str(expected), timeout_seconds=0.3):
            continue
        loader._remote_load_library(pid, str(dll), timeout_ms=5000)
        for _ in range(20):
            if hook_send.available(timeout=0.5):
                print("[hook] HTTP service is ready (PID %d)" % pid, flush=True)
                return 0
            time.sleep(0.25)
    print("[hook] Open and log in to fixed-version WeChat, then restart the bot", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
