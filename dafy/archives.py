"""一致快照、加密归档、完整性验证与本机备份删除同步。"""
from contextlib import contextmanager, nullcontext, closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import struct
import tempfile
import time
import re
import uuid
import zipfile
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from . import __version__, DATA_VERSION
from .paths import LOCK, Paths
from .store import Store

MAGIC = b"DAFY-ARCHIVE-1\n"
EXCLUDED = {"temporary", "backups", "exports", "logs", ".instance.lock", "setup-token", "restore-journal.json", "upgrade-state.json", "import-journal.json"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_member(name):
    value = PurePosixPath(name)
    if not name or value.is_absolute() or ".." in value.parts or "\\" in name or ":" in name:
        raise ValueError("归档包含不安全路径")
    return value


def encrypt(source, target, key=None, password=None):
    salt = secrets.token_bytes(16)
    if password is not None:
        if len(password) < 8:
            raise ValueError("迁移包密码至少8位")
        key = scrypt(password.encode("utf-8"), salt, 32, N=32768, r=8, p=1)
    if key is None:
        raise ValueError("缺少归档保护密钥")
    nonce = secrets.token_bytes(12)
    header = json.dumps({"version": 1, "kdf": "scrypt" if password is not None else "local",
                         "salt": salt.hex(), "nonce": nonce.hex()}, separators=(",", ":")).encode()
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(MAGIC + header)
    temp = target.with_suffix(".pending")
    try:
        with source.open("rb") as incoming, temp.open("wb") as outgoing:
            outgoing.write(MAGIC + struct.pack(">I", len(header)) + header)
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(cipher.encrypt(block))
            outgoing.write(cipher.digest())
            outgoing.flush()
            os.fsync(outgoing.fileno())
        Paths.protect(temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def decrypt(source, target, key=None, password=None):
    with source.open("rb") as incoming:
        if incoming.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是大肥鱼迁移或备份包")
        raw_length = incoming.read(4)
        if len(raw_length) != 4:
            raise ValueError("归档头损坏")
        size = struct.unpack(">I", raw_length)[0]
        if not 10 <= size <= 4096:
            raise ValueError("归档头无效")
        header_raw = incoming.read(size)
        header = json.loads(header_raw)
        if header.get("version") != 1 or header.get("kdf") not in {"scrypt", "local"}:
            raise ValueError("归档版本不支持")
        if header["kdf"] == "scrypt":
            if password is None:
                raise ValueError("请输入迁移包密码")
            key = scrypt(password.encode("utf-8"), bytes.fromhex(header["salt"]), 32, N=32768, r=8, p=1)
        if key is None:
            raise ValueError("本机备份需要原备份保护密钥")
        cipher = AES.new(key, AES.MODE_GCM, nonce=bytes.fromhex(header["nonce"]))
        cipher.update(MAGIC + header_raw)
        remaining = source.stat().st_size - incoming.tell() - 16
        if remaining < 0:
            raise ValueError("归档不完整")
        try:
            with target.open("wb") as outgoing:
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("归档中途损坏")
                    remaining -= len(block)
                    outgoing.write(cipher.decrypt(block))
                cipher.verify(incoming.read(16))
        except Exception:
            target.unlink(missing_ok=True)
            raise ValueError("归档密码错误或完整性校验失败") from None


class Archives:
    def __init__(self, paths, settings, store, maintenance=None):
        self.paths, self.settings, self.store = paths, settings, store
        self.maintenance = maintenance or nullcontext

    def directory(self):
        configured = self.settings.read()["backup"]["directory"]
        target = Path(configured).expanduser().resolve() if configured else self.paths.path("backups")
        # 备份目录不得与数据目录重叠，默认专用子目录除外。
        if target == self.paths.root or self.paths.root.is_relative_to(target):
            raise ValueError("备份目录不能包含整个运行数据目录")
        if target.is_relative_to(self.paths.root) and target != self.paths.path("backups"):
            raise ValueError("数据目录内只能使用专用backups目录")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        return target

    def registered(self):
        return self.paths.json("backup-catalog.json", [])

    def snapshot(self, folder):
        self.assert_media_policy(self.paths.root)
        files = []
        for source in sorted(self.paths.root.rglob("*")):
            relative = source.relative_to(self.paths.root)
            if relative.parts[0] in EXCLUDED or relative.parts[0] == "backup-catalog.json":
                continue
            if source.is_symlink():
                raise ValueError("用户数据包含符号链接，请先处理")
            if not source.is_file() or source.name.endswith(("-wal", "-shm", ".pending", ".lock")):
                continue
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix in SQLITE_SUFFIXES:
                origin = sqlite3.connect("file:" + source.as_posix() + "?mode=ro", uri=True)
                dest = sqlite3.connect(target)
                try:
                    origin.backup(dest)
                    if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("数据库快照验证失败")
                finally:
                    dest.close()
                    origin.close()
            else:
                shutil.copy2(source, target)
            files.append({"path": relative.as_posix(), "bytes": target.stat().st_size, "sha256": digest(target)})
        return files

    def pack(self, folder, target):
        self.assert_media_policy(folder)
        files = []
        for source in sorted(folder.rglob("*")):
            if source.is_file() and source.name != "manifest.json":
                files.append({"path": source.relative_to(folder).as_posix(), "bytes": source.stat().st_size, "sha256": digest(source)})
        counts = {}
        database = folder / "memory/state.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database)) as db:
                counts = {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in ("events", "notes", "calls", "reminders", "tombstones")}
        manifest = {"format": 1, "application": __version__, "data_version": DATA_VERSION, "created": time.time(),
                    "platform": "windows-x64" if os.name == "nt" else "linux-x64",
                    "account": self.store.state("account_identity", ""), "counts": counts, "files": files}
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
            archive.write(folder / "manifest.json", "manifest.json")
            for item in files:
                archive.write(folder / item["path"], item["path"])
        return manifest

    @staticmethod
    def assert_media_policy(folder):
        # 固定组件尚无清理接口。不能把它内部的缓存当作凭据一起迁移并声称无媒体。
        for file in (folder / "wechat/accounts").glob("*/agent/control.sqlite"):
            with closing(sqlite3.connect("file:" + file.as_posix() + "?mode=ro", uri=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                cached = "media_assets" in tables and db.execute("SELECT EXISTS(SELECT 1 FROM media_assets WHERE payload IS NOT NULL OR kind='voice')").fetchone()[0]
                uploaded = "media_sources" in tables and db.execute("SELECT EXISTS(SELECT 1 FROM media_sources WHERE byte_length>0)").fetchone()[0]
                if cached or uploaded:
                    raise ValueError("接入组件仍留有媒体缓存；当前预览版不能生成符合无媒体要求的备份或迁移包，原数据未删除")

    def create(self, automatic=False, password=None, reason="manual", keep_paused=False, already_paused=False):
        root = self.directory() if password is None else self.paths.path("exports")
        ident = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        output = root / (ident + ".dafy")
        self.paths.secret("backup.key")
        with (nullcontext() if already_paused else self.maintenance()), self.paths.temporary() as temp:
            stage = temp / "snapshot"
            stage.mkdir()
            self.snapshot(stage)
            self.store.integrity()
            zipped = temp / "snapshot.zip"
            manifest = self.pack(stage, zipped)
            encrypt(zipped, output, key=self.paths.secret("backup.key"), password=password)
            # 发布前解密复验，备份失败不会报成功。
            verified = temp / "verified.zip"
            decrypt(output, verified, key=self.paths.secret("backup.key"), password=password)
            self.verify_zip(verified)
            if password is None:
                with LOCK:
                    rows = self.registered()
                    rows.append({"id": ident, "path": str(output.resolve()), "created": manifest["created"],
                                 "automatic": bool(automatic), "reason": reason, "sha256": digest(output)})
                    self.paths.save("backup-catalog.json", rows)
                    if automatic:
                        self.prune()
            self.store.state("last_backup", {"at": time.time(), "id": ident, "reason": reason, "ok": True}, write=True)
        return {"id": ident, "file": output.name, "path": str(output), "created": manifest["created"], "counts": manifest["counts"]}

    def prune(self):
        rows = self.registered()
        automatic = sorted((x for x in rows if x["automatic"]), key=lambda x: x["created"], reverse=True)
        expired = {x["id"] for x in automatic[self.settings.read()["backup"]["keep"]:]}
        for row in rows:
            if row["id"] in expired:
                path = Path(row["path"])
                # 只清理本产品登记的精确文件，不枚举删除任意目录。
                if path.suffix != ".dafy" or path.is_symlink() or path.name != row["id"] + ".dafy":
                    raise ValueError("备份登记路径异常")
                if path.exists() and digest(path) != row["sha256"]:
                    raise ValueError("旧备份被外部修改，未自动删除")
                path.unlink(missing_ok=True)
        self.paths.save("backup-catalog.json", [x for x in rows if x["id"] not in expired])

    def verify_zip(self, source):
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 200000 or sum(x.file_size for x in members) > 20 * 1024 ** 3:
                raise ValueError("归档规模超过恢复范围")
            names = [x.filename for x in members]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("归档目录重复或缺少清单")
            for name in names:
                safe_member(name)
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != 1 or manifest.get("data_version", 999) > DATA_VERSION:
                raise ValueError("迁移包数据版本不受支持，请升级程序")
            declared = {x["path"]: x for x in manifest["files"]}
            if set(declared) != set(names) - {"manifest.json"}:
                raise ValueError("归档文件与清单不一致")
            for name, item in declared.items():
                value, size = hashlib.sha256(), 0
                with archive.open(name) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        value.update(chunk)
                        size += len(chunk)
                if size != item["bytes"] or value.hexdigest() != item["sha256"]:
                    raise ValueError("归档文件校验失败")
        return manifest

    def unpack(self, source, target, password=None):
        zipped = target.parent / ("unpacked-" + uuid.uuid4().hex + ".zip")
        try:
            decrypt(source, zipped, key=self.paths.secret("backup.key"), password=password)
            manifest = self.verify_zip(zipped)
            target.mkdir(parents=True, exist_ok=False)
            Paths.protect(target)
            with zipfile.ZipFile(zipped) as archive:
                for item in manifest["files"]:
                    relative = safe_member(item["path"])
                    destination = target.joinpath(*relative.parts)
                    if not destination.resolve().is_relative_to(target.resolve()):
                        raise ValueError("归档路径越界")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item["path"]) as incoming, destination.open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    Paths.protect(destination)
            for directory in target.rglob("*"):
                if directory.is_dir():
                    Paths.protect(directory)
            return manifest
        finally:
            zipped.unlink(missing_ok=True)

    def import_into_empty(self, source, destination, password=None):
        destination = Path(destination).expanduser().resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("目标已有数据，禁止自动覆盖或合并，请指定空目录")
        if destination == self.paths.root or destination.is_relative_to(self.paths.root) or self.paths.root.is_relative_to(destination):
            raise ValueError("迁移目标不能与当前运行数据目录重叠")
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.parent / (".dafy-import-" + uuid.uuid4().hex)
        with self.maintenance():
            try:
                manifest = self.unpack(source, stage, password)
                current_account = self.store.state("account_identity", "")
                if current_account and manifest.get("account") and current_account != manifest["account"]:
                    raise ValueError("迁移包与当前绑定账号不一致，未启用")
                self.apply_privacy(stage)
                self.relocate_native(stage, destination)
                # 源机绝对目录不应成为目标机器的有效路径。
                settings_file = stage / "settings.json"
                config = json.loads(settings_file.read_text(encoding="utf-8"))
                config["backup"]["directory"] = ""
                settings_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                for name in ("sessions", "login_failures"):
                    db = sqlite3.connect(stage / "memory/state.sqlite3")
                    try:
                        db.execute("DELETE FROM " + name)
                        db.commit()
                    finally:
                        db.close()
                if destination.exists():
                    destination.rmdir()  # 已确认空目录，不递归移除。
                os.replace(stage, destination)
                return {"destination": str(destination), "manifest": manifest, "needs_login": True,
                        "reconfigure": ["备份目录已改为新机默认目录", "系统绑定或失效的凭据需要重新填写"]}
            finally:
                if stage.exists():
                    if stage.parent != destination.parent or not stage.name.startswith(".dafy-import-"):
                        raise ValueError("导入临时目录校验失败")
                    shutil.rmtree(stage)

    @staticmethod
    def relocate_native(stage, destination):
        database = stage / "wechat/registry.sqlite"
        if not database.exists():
            return
        with closing(sqlite3.connect(database)) as db:
            rows = db.execute("SELECT id,storage_mode FROM accounts").fetchall()
            for account, mode in rows:
                if mode != "managed" or not re.fullmatch(r"[a-zA-Z0-9_-]+", account):
                    raise ValueError("归档含外部微信目录，不能将新机接到旧机文件")
                relative = Path("wechat/accounts") / account
                profile = stage / relative / "profile"
                profile.mkdir(parents=True, exist_ok=True)
                Paths.protect(profile)
                db.execute("UPDATE accounts SET profile_path=? WHERE id=?", (str(destination / relative / "profile"), account))
                key_file = stage / relative / "agent/profile-keys.json"
                if key_file.exists():
                    db.execute("UPDATE account_settings SET keys_file=? WHERE account_id=?", (str(destination / relative / "agent/profile-keys.json"), account))
                else:
                    db.execute("UPDATE account_settings SET keys_file=NULL WHERE account_id=?", (account,))
            db.commit()

    def purge_sources(self, sources):
        """删除完成前同步重打所有本机登记旧备份；失败留下任务，不能报全部成功。"""
        pending = self.store.state("backup_purge", {"sources": []})
        union = sorted(set(pending.get("sources", [])) | set(sources))
        self.store.state("backup_purge", {"sources": union, "state": "pending"}, write=True)
        for row in self.registered():
            path = Path(row["path"])
            if not path.exists():
                continue
            journal = self.paths.json("backup-clean-journal.json", {})
            if journal.get("id") == row["id"] and digest(path) == journal.get("replacement_sha256"):
                row["sha256"] = journal["replacement_sha256"]
                registered = self.registered()
                for item in registered:
                    if item["id"] == row["id"]:
                        item["sha256"] = row["sha256"]
                self.paths.save("backup-catalog.json", registered)
            if path.is_symlink() or path.name != row["id"] + ".dafy" or digest(path) != row["sha256"]:
                raise ValueError("本机旧备份不完整，删除清理尚未完成")
            with self.paths.temporary() as temp:
                folder = temp / "backup"
                self.unpack(path, folder)
                database = folder / "memory/state.sqlite3"
                if database.exists():
                    db = sqlite3.connect(database)
                    db.row_factory = sqlite3.Row
                    try:
                        db.execute("PRAGMA secure_delete=ON")
                        Store.redact(db, union)
                        db.commit()
                        db.execute("VACUUM")
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("清理后的备份校验失败")
                    finally:
                        db.close()
                zipped = temp / "clean.zip"
                self.pack(folder, zipped)
                replacement = path.with_suffix(".cleaning")
                encrypt(zipped, replacement, key=self.paths.secret("backup.key"))
                self.paths.save("backup-clean-journal.json", {"id": row["id"], "replacement_sha256": digest(replacement)})
                os.replace(replacement, path)
                rows = self.registered()
                for existing in rows:
                    if existing["id"] == row["id"]:
                        existing["sha256"] = digest(path)
                self.paths.save("backup-catalog.json", rows)
                self.paths.save("backup-clean-journal.json", {})
        self.store.state("backup_purge", {"sources": [], "state": "completed", "at": time.time()}, write=True)

    def restore_live(self, source, password=None, already_paused=False):
        source = Path(source).resolve()
        # 原包必须位于备份登记或由管理员明确上传的目录内。
        known = any(Path(row["path"]).resolve() == source for row in self.registered())
        uploaded = source.is_relative_to(self.paths.path("temporary"))
        if not known and not uploaded:
            raise ValueError("恢复包不在本机登记备份或上传目录中")
        if source.is_symlink() or not source.is_file():
            raise ValueError("恢复包不可用")
        with (nullcontext() if already_paused else self.maintenance()), self.paths.temporary() as temp:
            stage = temp / "restore"
            manifest = self.unpack(source, stage, password)
            self.apply_privacy(stage)
            old_account = self.store.state("account_identity", "") if self.store is not None else ""
            if old_account and manifest.get("account") and old_account != manifest["account"]:
                raise ValueError("恢复包与当前账号不一致")
            self.paths.save("restore-journal.json", {"state": "restoring", "source": str(source), "at": time.time()})
            with LOCK:
                for item in manifest["files"]:
                    relative = safe_member(item["path"])
                    destination = self.paths.path(relative.as_posix())
                    origin = stage.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.suffix in SQLITE_SUFFIXES:
                        for suffix in ("-wal", "-shm"):
                            Path(str(destination) + suffix).unlink(missing_ok=True)
                    os.replace(origin, destination)
                    self.paths.protect(destination)
            self.paths.save("restore-journal.json", {"state": "completed", "source": str(source), "at": time.time()})
            return {"restored": True, "counts": manifest["counts"]}

    def apply_privacy(self, folder):
        ledger = self.paths.json("privacy-ledger.json", {"sources": []})
        previous = folder / "privacy-ledger.json"
        if previous.exists():
            prior = json.loads(previous.read_text(encoding="utf-8"))
            ledger["sources"] = sorted(set(ledger["sources"]) | set(prior["sources"]))
        if ledger["sources"]:
            with closing(sqlite3.connect(folder / "memory/state.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA secure_delete=ON")
                Store.redact(db, ledger["sources"])
                db.commit()
            previous.write_text(json.dumps(ledger), encoding="utf-8")
