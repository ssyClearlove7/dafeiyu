"""签名升级包：用户触发、停写快照、验证副本、原子切换与失败恢复。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import requests
from . import __version__
from .archives import safe_member
from .paths import CODE


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def verify_manifest(manifest, signature, key_path):
    if not key_path.is_file():
        raise ValueError("当前安装缺少可信发布公钥，不能安装升级包")
    key = ECC.import_key(key_path.read_bytes())
    try:
        eddsa.new(key, "rfc8032").verify(canonical(manifest), base64.b64decode(signature, validate=True))
    except (ValueError, TypeError):
        raise ValueError("升级包签名校验失败，未执行程序") from None


class Updates:
    def __init__(self, engine):
        self.engine = engine
        self.root = Path(os.environ["DAFY_INSTALL_DIR"]).resolve() if os.environ.get("DAFY_INSTALL_DIR") else None
        self.key = CODE / "packaging/release-public-key.pem"

    def check(self):
        address = self.engine.settings.read()["updates"]["manifest"]
        if not address:
            return {"current": __version__, "available": False, "message": "尚未配置发布来源，可手动上传可信升级包"}
        if not address.startswith("https://"):
            raise ValueError("版本清单须使用HTTPS地址")
        with requests.get(address, timeout=(10, 20), allow_redirects=False) as response:
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("版本清单过大")
            value = response.json()
        manifest, signature = value["manifest"], value["signature"]
        verify_manifest(manifest, signature, self.key)
        return {"current": __version__, "available": manifest["version"] != __version__, "release": manifest,
                "message": "发现新版本：" + manifest["version"] if manifest["version"] != __version__ else "当前已是此发布来源的版本"}

    def validate_package(self, source, stage):
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 200000 or sum(x.file_size for x in archive.infolist()) > 2 * 1024 ** 3:
                raise ValueError("升级包目录或规模异常")
            for name in names:
                safe_member(name)
            manifest = json.loads(archive.read("release.json"))
            signature = archive.read("release.sig").decode("ascii")
            verify_manifest(manifest, signature, self.key)
            if manifest.get("python") != "cp312" or manifest.get("platform") != ("windows-x64" if os.name == "nt" else "linux-x64"):
                raise ValueError("升级包运行时或平台不匹配")
            version = manifest.get("version", "")
            if not __import__("re").fullmatch(r"[a-zA-Z0-9._-]{1,64}", version):
                raise ValueError("版本标识无效")
            declared = {item["path"]: item for item in manifest["files"]}
            if set(declared) != set(names) - {"release.json", "release.sig"}:
                raise ValueError("升级包内容与签名清单不一致")
            stage.mkdir(parents=True, exist_ok=False)
            for name, item in declared.items():
                target = stage.joinpath(*safe_member(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(name) as incoming, target.open("wb") as output:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("升级文件内容校验失败")
                if os.name != "nt":
                    target.chmod(0o700 if item.get("executable") else 0o600)
            (stage / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
            (stage / "release.sig").write_text(signature, encoding="ascii")
        return manifest

    def check_data(self, stage, data):
        code = "import sys;sys.path[:0]=[sys.argv[1]+'/site-packages',sys.argv[1]];from dafy.__main__ import main;main(['--data',sys.argv[2],'--check-data'])"
        result = subprocess.run([sys.executable, "-c", code, str(stage), str(data)], capture_output=True, timeout=90,
                                creationflags=0x08000000 if os.name == "nt" else 0)
        if result.returncode:
            raise ValueError("新版本数据验证失败，未切换；校验退出码：" + str(result.returncode))

    def install(self, source):
        if not self.root or not (self.root / "current.json").exists():
            raise ValueError("当前是源码开发目录，请先构建并使用安装版验证升级")
        source = Path(source).resolve()
        if not source.is_file():
            raise ValueError("请先上传完整升级包")
        stage = self.root / "versions" / (".stage-" + uuid.uuid4().hex)
        manifest = self.validate_package(source, stage)
        destination = self.root / "versions" / manifest["version"]
        if destination.exists():
            raise ValueError("此版本目录已经存在，不覆盖已安装版本")
        current = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        if current.get("version") == manifest["version"]:
            raise ValueError("所选版本已经安装")
        app = self.engine
        with app.maintenance():
            backup = app.archives.create(automatic=True, reason="upgrade", already_paused=True)
            app.paths.save("upgrade-state.json", {"state": "validating", "from": current, "to": manifest["version"], "backup": backup["path"], "at": time.time()})
            try:
                with app.paths.temporary() as temp:
                    sample = temp / "validation"
                    app.archives.unpack(Path(backup["path"]), sample)
                    self.check_data(stage, sample)
            except Exception:
                app.paths.save("upgrade-state.json", {"state": "rejected", "from": current, "to": manifest["version"], "at": time.time()})
                raise
            # 正式数据转换前保留完整快照，验证过程不连接微信或模型。
            try:
                self.check_data(stage, app.paths.root)
                os.replace(stage, destination)
                pointer = self.root / (".current-" + uuid.uuid4().hex)
                pointer.write_text(json.dumps({"version": manifest["version"], "directory": "versions/" + manifest["version"]}), encoding="utf-8")
                os.replace(pointer, self.root / "current.json")
                history = app.paths.json("release-history.json", [])
                history.append({"from": current, "to": manifest["version"], "at": time.time()})
                app.paths.save("release-history.json", history)
                app.paths.save("upgrade-state.json", {"state": "completed", "from": current, "to": manifest["version"], "backup": backup["path"], "at": time.time()})
            except Exception:
                app.archives.restore_live(Path(backup["path"]), already_paused=True)
                pointer = self.root / (".current-" + uuid.uuid4().hex)
                pointer.write_text(json.dumps(current), encoding="utf-8")
                os.replace(pointer, self.root / "current.json")
                app.paths.save("upgrade-state.json", {"state": "rolled_back", "from": current, "to": manifest["version"], "backup": backup["path"], "at": time.time()})
                raise
            app.restart_requested.set()
            app.stop_event.set()
        return {"version": manifest["version"], "restart": True, "backup": backup["path"]}

    def rollback(self):
        if not self.root:
            raise ValueError("源码开发目录没有已安装的旧版本")
        app = self.engine
        history = app.paths.json("release-history.json", [])
        if not history:
            raise ValueError("当前没有可回退的上一安装版本")
        previous = history[-1]["from"]
        old_code = (self.root / previous["directory"]).resolve()
        if not old_code.is_relative_to(self.root / "versions") or not (old_code / "dafy/__main__.py").is_file():
            raise ValueError("上一版本目录不可用，未改变当前数据")
        current = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        with app.maintenance():
            backup = app.archives.create(automatic=True, reason="version-rollback", already_paused=True)
            with app.paths.temporary() as temp:
                sample = temp / "compatibility"
                app.archives.unpack(Path(backup["path"]), sample)
                self.check_data(old_code, sample)
            # 旧版本必须先证明能读取现有数据；不使用升级前快照覆盖新增经历。
            app.paths.save("upgrade-state.json", {"state": "validating", "from": current, "to": previous["version"], "backup": backup["path"], "kind": "rollback"})
            try:
                self.check_data(old_code, app.paths.root)
                pointer = self.root / (".current-" + uuid.uuid4().hex)
                pointer.write_text(json.dumps(previous), encoding="utf-8")
                os.replace(pointer, self.root / "current.json")
                app.paths.save("release-history.json", history[:-1])
                app.paths.save("upgrade-state.json", {"state": "version_rolled_back", "from": current, "to": previous["version"], "backup": backup["path"]})
            except Exception:
                app.archives.restore_live(Path(backup["path"]), already_paused=True)
                pointer = self.root / (".current-" + uuid.uuid4().hex)
                pointer.write_text(json.dumps(current), encoding="utf-8")
                os.replace(pointer, self.root / "current.json")
                app.paths.save("upgrade-state.json", {"state": "rollback_rejected", "from": current, "at": time.time()})
                raise
            app.restart_requested.set()
            app.stop_event.set()
        return {"version": previous["version"], "restart": True, "preserved_current_data": True}
