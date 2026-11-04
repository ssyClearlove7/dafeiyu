"""程序只读资源与用户数据分离；所有删除限制在已登记的数据目录内。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import subprocess
import ctypes
from functools import lru_cache


@lru_cache(maxsize=1)
def windows_sid():
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                            text=True, check=True, creationflags=0x08000000)
    import re
    match = re.search(r"S-1-[0-9-]+", result.stdout)
    if not match:
        raise RuntimeError("无法确认运行用户身份，未放宽数据权限")
    return match.group(0)

CODE = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()


def default_home():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Dafeiyu"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dafeiyu"


class Paths:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("DAFY_DATA_DIR") or default_home()).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.protect(self.root)
        for name in ("memory", "assets", "temporary", "backups", "exports", "logs", "wechat", "installed_skills"):
            self.path(name).mkdir(exist_ok=True, mode=0o700)
            self.protect(self.path(name))

    def path(self, name):
        value = (self.root / name).resolve()
        if not value.is_relative_to(self.root) or value == self.root:
            raise ValueError("路径必须位于大肥鱼数据目录内")
        return value

    @staticmethod
    def protect(path):
        if os.name == "nt":
            from ctypes import wintypes
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            descriptor = ctypes.c_void_p()
            inheritance = "OICI" if path.is_dir() else ""
            sddl = "O:" + windows_sid() + "D:P(A;" + inheritance + ";FA;;;SY)(A;" + inheritance + ";FA;;;" + windows_sid() + ")"
            convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
            convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            convert.restype = wintypes.BOOL
            if not convert(sddl, 1, ctypes.byref(descriptor), None):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
                get_acl = advapi.GetSecurityDescriptorDacl
                get_acl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
                if not get_acl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
                    raise ctypes.WinError(ctypes.get_last_error())
                owner = ctypes.c_void_p()
                get_owner = advapi.GetSecurityDescriptorOwner
                get_owner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
                if not get_owner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)):
                    raise ctypes.WinError(ctypes.get_last_error())
                setter = advapi.SetNamedSecurityInfoW
                setter.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                setter.restype = wintypes.DWORD
                result = setter(str(path), 1, 0x80000005, owner, None, acl, None)
                if result:
                    raise ctypes.WinError(result)
            finally:
                kernel.LocalFree.argtypes = [ctypes.c_void_p]
                kernel.LocalFree(descriptor)
        else:
            path.chmod(0o700 if path.is_dir() else 0o600)

    def atomic(self, name, data):
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        with LOCK:
            descriptor, temp = tempfile.mkstemp(prefix=".write-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                self.protect(Path(temp))
                os.replace(temp, target)
            finally:
                Path(temp).unlink(missing_ok=True)
        return target

    def json(self, name, default=None):
        try:
            return json.loads(self.path(name).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default

    def save(self, name, value):
        return self.atomic(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def secret(self, name):
        target = self.path(name)
        with LOCK:
            if not target.exists():
                self.atomic(name, secrets.token_bytes(32))
            value = target.read_bytes()
            if len(value) != 32:
                raise ValueError("本机保护密钥损坏，请从完整备份恢复")
            return value

    @contextmanager
    def temporary(self):
        with tempfile.TemporaryDirectory(prefix="media-", dir=self.path("temporary")) as folder:
            yield Path(folder)

    def clear_temporary(self):
        # 仅在进程启动、尚未接收工作时调用。
        for child in self.path("temporary").iterdir():
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                if not child.resolve().is_relative_to(self.path("temporary")):
                    raise ValueError("临时目录越界")
                shutil.rmtree(child)
            else:
                child.unlink()
