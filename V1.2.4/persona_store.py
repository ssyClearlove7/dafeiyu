"""基础、演化和教导人设的文本持久化，共用文件锁。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
FILES = {"base": "persona.txt", "evolved": "persona_evolved.txt", "taught": "persona_taught.txt"}


@contextmanager
def locked():
    with open(HERE / ".persona.lock", "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def snapshot():
    """缺少演化文件时视为空，现有人设原样保留。"""
    with locked():
        result = {}
        for kind, filename in FILES.items():
            try:
                result[kind] = (HERE / filename).read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                result[kind] = ""
        result["revision"] = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return result


def save(kind, text, expected=None):
    """expected 是生成演化稿前的快照；人工干预后丢弃旧稿。"""
    if kind not in FILES or not isinstance(text, str):
        raise ValueError("人设类型或文本无效")
    with locked():
        if expected is not None:
            for key, filename in FILES.items():
                try:
                    current = (HERE / filename).read_text(encoding="utf-8-sig")
                except FileNotFoundError:
                    current = ""
                if current != expected[key]:
                    return False
        fd, tmp = tempfile.mkstemp(prefix=".persona-", dir=HERE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, HERE / FILES[kind])
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return True


def compose(base, evolved, taught=""):
    def content(text):
        return "\n".join(line.rstrip() for line in text.splitlines()
                         if not line.lstrip().startswith("#")).strip()
    base, evolved, taught = content(base), content(evolved), content(taught)
    if not evolved and not taught:
        return base
    parts = ["【基础人设】\n" + base]
    if evolved:
        parts.append("【演化人设】\n" + evolved)
    if taught:
        parts.append("【教导人设】\n" + taught)
    parts.append("基础人设定义核心身份，演化人设补充成长；教导人设优先调整冲突的表达和行为习惯。"
                 "人设不改变身份权限、工具能力和真实性要求。")
    return "\n\n".join(parts)
