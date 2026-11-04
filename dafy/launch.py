"""安装位置与数据位置各自持久化；换机激活无需手动编辑配置。"""
import json
import os
from pathlib import Path
import sys
import tempfile


def location_file():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    folder = base / "dafeiyu-launcher"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "location.json"


def read_location():
    explicit = os.environ.get("DAFY_DATA_DIR")
    if explicit:
        return explicit
    file = location_file()
    if file.exists():
        value = json.loads(file.read_text(encoding="utf-8"))
        return value["data_directory"]
    return None


def write_location(root):
    root = Path(root).expanduser().resolve()
    if not (root / "memory/state.sqlite3").is_file() or not (root / "settings.json").is_file():
        raise ValueError("目标不是已验证的大肥鱼数据目录")
    target = location_file()
    descriptor, name = tempfile.mkstemp(prefix=".location-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"data_directory": str(root)}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
