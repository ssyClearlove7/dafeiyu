"""构建可审核的安装目录和签名更新包；只打包列入清单的程序资源。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
import tarfile
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dafy import __version__
from dafy.updates import canonical
from prepare_component import prepare


def build(platform, runtime=None, signing_key=None, output_name=None, reuse_dependencies=None):
    name = output_name or "product-" + platform
    if not __import__("re").fullmatch(r"[A-Za-z0-9._-]+", name) or name in {".", ".."}:
        raise ValueError("构建名称无效")
    output = ROOT / ".build" / name
    if output.exists():
        raise ValueError("构建目录已存在，请指定新的工作副本或核对后手动清理：" + str(output))
    version = output / "versions" / __version__
    version.mkdir(parents=True)
    for destination in (output / "LICENSE", version / "LICENSE"):
        shutil.copy2(ROOT / "LICENSE", destination)
    shutil.copytree(ROOT / "dafy", version / "dafy", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    legacy = version / "V1.2.4"
    legacy.mkdir()
    shutil.copytree(ROOT / "V1.2.4/console-assets", legacy / "console-assets")
    # 原项目仍在使用，不将其中的配置、凭据、缓存、聊天和当前人设打入程序包。
    for path in (ROOT / "V1.2.4").glob("*.py"):
        if not path.name.startswith("test_"):
            shutil.copy2(path, legacy / path.name)
    for path in (ROOT / "V1.2.4/skills").rglob("SKILL.md"):
        target = legacy / path.relative_to(ROOT / "V1.2.4")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # 使用仓库已提交的基线人设；现有机器的人设通过旧版导入保留。
    persona = subprocess.run(["git", "show", "HEAD:V1.2.4/persona.txt"], cwd=ROOT, capture_output=True, check=True).stdout
    (legacy / "persona.txt").write_bytes(persona)
    (version / "bin").mkdir()
    component = prepare(platform)
    shutil.copy2(component, version / "bin" / ("wx.exe" if platform == "windows-x64" else "wx"))
    (version / "packaging").mkdir()
    for name in ("wechat.lock.json", "WECHAT-THIRD-PARTY-NOTICES.txt"):
        shutil.copy2(ROOT / "packaging" / name, version / "packaging" / name)
    dependencies = version / "site-packages"
    command = [sys.executable, "-X", "utf8", "-m", "pip", "install", "--no-compile", "--only-binary=:all:", "--target", str(dependencies),
               "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312", "--platform"]
    command += ["win_amd64"] if platform == "windows-x64" else ["manylinux_2_28_x86_64", "--platform", "manylinux2014_x86_64"]
    if reuse_dependencies:
        from importlib.metadata import distributions
        normalize = lambda s: s.lower().replace("_", "-")
        existing = {normalize(d.metadata["Name"]): d.version for d in distributions(path=[str(Path(reuse_dependencies).resolve())])}
        required = [line.split("==") for line in (ROOT / "requirements-product.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
        if any(existing.get(normalize(name)) != version for name, version in required):
            raise ValueError("复用依赖与锁定清单不一致")
        shutil.copytree(reuse_dependencies, dependencies, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        subprocess.run(command + ["-r", str(ROOT / "requirements-product.txt")], check=True)
    keyfile = Path(signing_key) if signing_key else ROOT / ".build/release-private.pem"
    if not keyfile.exists():
        if (ROOT / "packaging/release-public-key.pem").exists():
            raise ValueError("缺少与已发布公钥对应的私钥，不能静默更换签名身份")
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text(ECC.generate(curve="Ed25519").export_key(format="PEM"), encoding="ascii")
        from dafy.paths import Paths
        Paths.protect(keyfile)
    key = ECC.import_key(keyfile.read_bytes())
    public = key.public_key().export_key(format="PEM")
    trusted = ROOT / "packaging/release-public-key.pem"
    if trusted.exists() and trusted.read_text(encoding="ascii").strip() != public.strip():
        raise ValueError("发布密钥与可信公钥不一致")
    trusted.write_text(public, encoding="ascii")
    shutil.copy2(trusted, version / "packaging/release-public-key.pem")
    shutil.copy2(ROOT / "packaging/launch.py", output / "launch.py")
    (output / "current.json").write_text(json.dumps({"version": __version__, "directory": "versions/" + __version__}), encoding="utf-8")
    if platform == "windows-x64":
        if not runtime:
            raise ValueError("Windows 构建需要官方 CPython 3.12 x64 embeddable ZIP：--runtime 路径")
        runtime_lock = json.loads((ROOT / "packaging/python-windows.lock.json").read_text())
        runtime_path = Path(runtime)
        if runtime_path.stat().st_size != runtime_lock["bytes"] or hashlib.sha256(runtime_path.read_bytes()).hexdigest() != runtime_lock["sha256"]:
            raise ValueError("Windows 运行时与锁定清单不一致")
        target = output / "runtime"
        target.mkdir()
        with zipfile.ZipFile(runtime) as archive:
            for name in archive.namelist():
                if "/" in name or "\\" in name or ":" in name or name in {".", ".."}:
                    raise ValueError("运行时 ZIP 路径异常")
            archive.extractall(target)
        (target / "python312._pth").write_text("python312.zip\n.\n..\nimport site\n", encoding="ascii")
    else:
        shutil.copy2(ROOT / "packaging/linux/install.sh", output / "install.sh")
        shutil.copy2(ROOT / "packaging/linux/run.sh", output / "run.sh")
        shutil.copy2(ROOT / "packaging/linux/uninstall.sh", output / "uninstall.sh")
        (version / "bin/wx").chmod(0o700)
        uv_lock = json.loads((ROOT / "packaging/uv.lock.json").read_text())
        uv_archive = ROOT / ".build/uv-x86_64-unknown-linux-gnu.tar.gz"
        if hashlib.sha256(uv_archive.read_bytes()).hexdigest() != uv_lock["sha256"]:
            raise ValueError("Linux 运行环境引导组件校验失败")
        shutil.copy2(uv_archive, output / "uv.tar.gz")
    files = [{"path": p.relative_to(version).as_posix(), "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
              "executable": p.name == "wx" or "imageio_ffmpeg/binaries/ffmpeg-" in p.as_posix()}
             for p in sorted(version.rglob("*")) if p.is_file()]
    manifest = {"version": __version__, "platform": platform, "python": "cp312", "files": files}
    signature = base64.b64encode(eddsa.new(key, "rfc8032").sign(canonical(manifest))).decode()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / ("Dafeiyu-" + __version__ + "-" + platform + ".update.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("release.json", json.dumps(manifest, ensure_ascii=False))
        zipped.writestr("release.sig", signature)
        for item in files:
            zipped.write(version / item["path"], item["path"])
    if platform == "linux-x64":
        sums = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(output).as_posix() + "\n" for p in sorted(output.rglob("*")) if p.is_file())
        (output / "SHA256SUMS").write_text(sums, encoding="utf-8")
        with tarfile.open(dist / ("Dafeiyu-" + __version__ + "-linux-x64.tar.gz"), "w:gz") as package:
            def permissions(info):
                info.mode = 0o755 if info.isdir() or info.name.endswith(("/wx", ".sh")) or "imageio_ffmpeg/binaries/ffmpeg-" in info.name else 0o644
                return info
            package.add(output, arcname="dafeiyu", filter=permissions)
    print(json.dumps({"payload": str(output), "update": str(archive), "files": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["windows-x64", "linux-x64"], required=True)
    parser.add_argument("--runtime")
    parser.add_argument("--signing-key")
    parser.add_argument("--output-name")
    parser.add_argument("--reuse-dependencies")
    options = parser.parse_args()
    build(options.platform, options.runtime, options.signing_key, options.output_name, options.reuse_dependencies)
