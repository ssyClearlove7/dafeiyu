"""只恢复未开放收发前中断的维护事务，不覆盖已恢复运行后的新数据。"""
import json
import os
from pathlib import Path
import time
from .archives import Archives
from .settings import Settings


def preflight(paths):
    archives = Archives(paths, Settings(paths), None)
    upgrade = paths.json("upgrade-state.json", {})
    restore = paths.json("restore-journal.json", {})
    importing = paths.json("import-journal.json", {})
    if importing.get("state") == "applying":
        archives.restore_live(Path(importing["backup"]))
        paths.save("import-journal.json", dict(importing, state="rolled_back"))
    if upgrade.get("state") == "validating":
        archives.restore_live(Path(upgrade["backup"]))
        install = os.environ.get("DAFY_INSTALL_DIR")
        if not install:
            raise RuntimeError("升级恢复缺少安装位置，请使用安装版入口启动")
        current = Path(install) / "current.json"
        temporary = current.with_suffix(".recovering")
        temporary.write_text(json.dumps(upgrade["from"]), encoding="utf-8")
        os.replace(temporary, current)
        paths.save("upgrade-state.json", dict(upgrade, state="rolled_back", recovered=time.time()))
        return True
    if restore.get("state") == "restoring":
        archives.restore_live(Path(restore["source"]))
    return False
