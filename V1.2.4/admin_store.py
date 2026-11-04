"""管理员保存在 settings.json；旧文本名单仅在首次升级时导入。"""
import json
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent


def _write(path, settings):
    fd, temporary = tempfile.mkstemp(prefix=".admins-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _settings(root):
    path = Path(root) / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def load(root=HERE):
    settings = _settings(root)
    if "admins" not in settings:
        rows = []
        legacy = Path(root) / "admins.txt"
        if legacy.exists():
            for line in legacy.read_text(encoding="utf-8-sig").splitlines():
                who, _, note = line.partition("#")
                if who.strip():
                    rows.append({"wxid": who.strip(), "name": note.strip()})
        settings["admins"] = rows
        _write(Path(root) / "settings.json", settings)
    return settings["admins"]


def save(rows, root=HERE):
    settings = _settings(root)
    settings["admins"] = rows
    _write(Path(root) / "settings.json", settings)
