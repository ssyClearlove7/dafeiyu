"""同步代码到运行目录，保留用户配置、人设、素材和状态。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = Path(os.environ["WX_CODE_DIR"])
target = Path(os.environ["WX_RUNTIME_DIR"])
target.mkdir(parents=True, exist_ok=True)
for item in source.rglob("*"):
    if not item.is_file():
        continue
    dest = target / item.relative_to(source)
    if item.suffix == ".py" or item.relative_to(source).parts[0] == "skills" or not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, dest)

defaults = {"settings.json": {}, "keys.json": {"imageKeys": {}},
            "rooms.json": [], "whitelist.json": []}
for name, data in defaults.items():
    p = target / name
    if not p.exists():
        p.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
sys.path.insert(0, str(target))
import admin_store
admin_store.load(target)

os.chdir(target)
result = subprocess.run([sys.executable, str(target / "bot_launcher.py")])
if result.returncode not in (0, 4):
    print("机器人启动失败，面板仍会启动，请查看 run_bot.err", flush=True)
os.execv(sys.executable, [sys.executable, "-u", str(target / "panel.py"), "--host", "127.0.0.1", "--port", "8790"])
