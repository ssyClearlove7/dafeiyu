"""安装版固定入口；current.json 原子指向已经验签的版本目录。"""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
version = (root / pointer["directory"]).resolve()
if not version.is_relative_to(root / "versions") or not (version / "dafy/__main__.py").is_file():
    raise SystemExit("安装版本指向无效，请重新安装程序；用户数据仍在独立目录中")
os.environ["DAFY_INSTALL_DIR"] = str(root)
sys.path[:0] = [str(version / "site-packages"), str(version)]
from dafy.__main__ import main
main()
