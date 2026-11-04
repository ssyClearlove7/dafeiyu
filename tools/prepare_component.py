"""准备已锁定并校验的独立微信二进制，不读取用户配置。"""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def prepare(platform):
    lock = json.loads((ROOT / "packaging/wechat.lock.json").read_text())
    match = "win32" if platform == "windows-x64" else "linux"
    asset = next(x for x in lock["assets"] if x["platform"] == match)
    destination = ROOT / "bin" / ("wx.exe" if match == "win32" else "wx-linux-x64")
    destination.parent.mkdir(exist_ok=True)
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == asset["sha256"]:
        return destination
    url = "https://github.com/thisnick/agent-wechat/releases/download/v" + lock["version"] + "/" + asset["name"]
    temporary = destination.with_suffix(".download")
    try:
        with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as output:
            count, digest = 0, hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                if count > asset["bytes"]:
                    raise ValueError("组件大小超过锁定清单")
                digest.update(chunk)
                output.write(chunk)
        if count != asset["bytes"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError("组件校验失败，未启用")
        temporary.replace(destination)
        if match != "win32":
            destination.chmod(0o700)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["windows-x64", "linux-x64"], required=True)
    print(prepare(parser.parse_args().platform))
