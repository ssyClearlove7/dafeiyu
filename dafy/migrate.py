"""从当前版本导入到空产品目录；旧目录只读，不携带会话媒体缓存。"""
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import time
import os
from .store import packed
from .paths import LOCK


def import_legacy(source, service):
    if any(service.store.counts()[name] for name in ("events", "notes", "reminders")) or service.store.state("legacy_import"):
        raise ValueError("目标已有业务数据，禁止覆盖或重复导入")
    from .engine import Engine
    from .paths import Paths
    with service.maintenance(), service.paths.temporary() as temporary:
        backup = service.archives.create(automatic=True, reason="legacy-import", already_paused=True)
        stage = temporary / "legacy-stage"
        stage.mkdir()
        service.archives.snapshot(stage)
        staged = Engine(Paths(stage))
        result = _import_legacy(source, staged)
        staged.store.integrity()
        service.paths.save("import-journal.json", {"state": "applying", "backup": backup["path"]})
        try:
            for item in sorted(stage.rglob("*")):
                relative = item.relative_to(stage)
                if relative.parts[0] in {"temporary", "logs", "backups", "exports"} or not item.is_file() or item.name.endswith(("-wal", "-shm")):
                    continue
                destination = service.paths.path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item.suffix in {".sqlite3", ".sqlite", ".db"}:
                    for suffix in ("-wal", "-shm"):
                        Path(str(destination) + suffix).unlink(missing_ok=True)
                os.replace(item, destination)
                service.paths.protect(destination)
            service.store.integrity()
        except Exception:
            service.archives.restore_live(Path(backup["path"]), already_paused=True)
            service.paths.save("import-journal.json", {"state": "rolled_back", "backup": backup["path"]})
            raise
        service.paths.save("import-journal.json", {"state": "completed", "backup": backup["path"]})
        return result


def _import_legacy(source, service):
    source = Path(source).expanduser().resolve()
    if not source.is_dir() or source == service.paths.root or source.is_relative_to(service.paths.root) or service.paths.root.is_relative_to(source):
        raise ValueError("旧版本目录不能与新数据目录重叠")
    counts = service.store.counts()
    if counts["events"] or counts["reminders"] or counts["notes"] or service.store.state("legacy_import"):
        raise ValueError("目标已有业务数据，禁止覆盖或重复导入")
    report = {"source": str(source), "events": 0, "notes": 0, "reminders": 0, "copied": [], "reconfigure": []}
    with service.maintenance():
        config = service.settings.read()
        old_settings = source / "settings.json"
        if old_settings.exists():
            old = json.loads(old_settings.read_text(encoding="utf-8-sig"))
            ds = old.get("deepseek") or {}
            config["models"]["chat"]["key"] = ds.get("apiKey", "")
            config["models"]["vision"]["key"] = ds.get("apiKey", "")
            if ds.get("model"):
                config["models"]["chat"]["model"] = ds["model"]
            room_file = source / "rooms.json"
            if room_file.exists():
                rooms = json.loads(room_file.read_text(encoding="utf-8-sig"))
                config["rooms"] = [{"id": row["room"], "name": row.get("name") or row["room"], "enabled": row.get("enabled") is True} for row in rooms if row.get("room")]
                accounts = {r["account"] for r in rooms if r.get("account")}
                if len(accounts) == 1:
                    service.store.state("account_identity", next(iter(accounts)), write=True)
                elif len(accounts) > 1:
                    raise ValueError("旧版含多个机器人账号，请分别导入独立数据目录")
            admins = source / "admins.txt"
            if admins.exists():
                config["admins"] = [x.strip() for x in admins.read_text(encoding="utf-8-sig").splitlines() if x.strip() and not x.strip().startswith("#")]
                config["console_actor"] = config["admins"][0] if config["admins"] else ""
            tuning = source / "tuning.json"
            if tuning.exists():
                values = json.loads(tuning.read_text(encoding="utf-8-sig"))
                for old_key, new_key in (("MAX_LINES", "max_messages"), ("MAX_LINE", "reference_chars"), ("REPLY_MODE", "mode")):
                    if old_key in values:
                        config["interaction"][new_key] = values[old_key]
            cloud = source / "cloud_settings.json"
            if cloud.exists():
                values = json.loads(cloud.read_text(encoding="utf-8-sig"))
                key = values.get("apiKey") or values.get("api_key") or ""
                config["models"]["asr"]["key"] = key
                config["models"]["tts"]["key"] = key
                config["web"]["enabled"] = bool(values.get("web"))
                config["web"]["key"] = config["models"]["chat"]["key"]
            config["wechat"]["url"] = service.settings.read()["wechat"]["url"]
            # 只读取固定常量，不执行旧目录代码，也不读取环境变量中的凭据。
            voice_file = source / "wx_voice.py"
            if voice_file.is_file():
                import ast
                for statement in ast.parse(voice_file.read_text(encoding="utf-8-sig")).body:
                    if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                        for target in statement.targets:
                            if isinstance(target, ast.Name) and target.id in {"VOICE_ID", "VOICE_MODEL"}:
                                config["models"]["tts"]["voice" if target.id == "VOICE_ID" else "model"] = statement.value.value
            config["backup"]["directory"] = ""
            service.settings.validate(config)
            service.paths.save("settings.json", config)
        for name in ("persona.txt", "persona_evolved.txt", "taught_rules.json", "tuning.json", "sticker_used.json", "sticker_saved.json", "sticker_hashes.json",
                     "sticker_skill_jobs.json", "sticker_catalog.json", "sticker_author_catalog.json", "sticker_delete_results.json", "conversation_state.json"):
            path = source / name
            if path.is_file() and not path.is_symlink():
                service.paths.atomic(name, path.read_bytes())
                report["copied"].append(name)
        for old_name, new_name in (("stickers/library", "assets"), ("installed_skills", "installed_skills")):
            folder = source / old_name
            if folder.is_dir():
                for path in folder.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("旧素材目录含链接，未进行不安全复制")
                    if path.is_file():
                        destination = service.paths.path(new_name) / path.relative_to(folder)
                        if destination.exists() and destination.read_bytes() != path.read_bytes():
                            raise ValueError("目标已有不同素材，未覆盖")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
        old_memory = source / "memory/memory.sqlite3"
        id_map, source_meta = {}, {}
        if old_memory.exists():
            origin = sqlite3.connect("file:" + old_memory.as_posix() + "?mode=ro", uri=True)
            origin.row_factory = sqlite3.Row
            try:
                for row in origin.execute("SELECT * FROM events ORDER BY id"):
                    payload = json.loads(row["payload"])
                    stamp = row["at"]
                    try:
                        at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except (ValueError, AttributeError):
                        at = row["created"]
                    identity = row["source_key"]
                    if identity.startswith("native:"):
                        identity = identity.split(":", 2)[2]
                    direction = (payload.get("message") or {}).get("direction") or "observation"
                    kind = row["kind"]
                    native = payload.get("message")
                    if isinstance(native, dict) and native.get("id"):
                        _, authored, kind = service.message_text(native)
                        payload = dict(payload, native=native, authored=authored, reply_eligible=False)
                    ident = service.store.record(row["account"], row["room"], row["sender"], direction,
                        kind, row["body"], payload, at, identity)
                    if ident:
                        id_map[row["id"]] = ident
                        source_meta[ident] = (row["account"], row["room"], row["sender"])
                        report["events"] += 1
                previous_map = {}
                for row in origin.execute("SELECT * FROM knowledge ORDER BY id"):
                    origins = [id_map[x] for x in json.loads(row["sources"]) if x in id_map]
                    scopes = {source_meta[x][:2] for x in origins}
                    account, room = next(iter(scopes)) if len(scopes) == 1 else ("__legacy_mixed__", "__legacy_mixed__")
                    # 保留跨会话旧知识正文，但在来源未拆分前禁止进入聊天检索。
                    with service.store.connect() as db:
                        cur = db.execute("INSERT INTO notes(account,room,actor,topic,body,origin,previous,kind,created) VALUES (?,?,?,?,?,?,?,?,?)",
                            (account, room, "", row["topic"], row["body"], packed(origins), previous_map.get(row["previous"]), row["kind"], row["created"]))
                        previous_map[row["id"]] = cur.lastrowid
                    report["notes"] += 1
                report["reconfigure"].append("跨会话旧摘要已保留，须按原始证据重新整理后才用于对话，避免泄露私聊")
                for meta in origin.execute("SELECT key,value FROM meta"):
                    service.store.state("legacy-memory:" + meta["key"], json.loads(meta["value"]), write=True)
            finally:
                origin.close()
        tasks_file = source / "memory/tasks.sqlite3"
        if tasks_file.exists():
            origin = sqlite3.connect("file:" + tasks_file.as_posix() + "?mode=ro", uri=True)
            origin.row_factory = sqlite3.Row
            try:
                destination = sqlite3.connect(service.paths.path("memory/legacy-tasks.sqlite3"))
                try:
                    origin.backup(destination)
                finally:
                    destination.close()
                for row in origin.execute("SELECT * FROM reminders"):
                    repeat = {"kind": "interval", "seconds": row["interval"], "timezone": "Asia/Shanghai"} if row["interval"] else {"kind": "once", "timezone": "Asia/Shanghai"}
                    with service.store.connect() as db:
                        db.execute("INSERT INTO reminders(id,request,account,room,actor,content,due,recurrence,status,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (row["id"], "legacy:" + row["id"], row["account"], row["room"], row["actor"], row["content"], row["due"], packed(repeat), row["status"], row["created"], time.time()))
                    report["reminders"] += 1
            finally:
                origin.close()
            report["reconfigure"].append("旧任务执行次数、发送时间及工具回执原样保存在 legacy-tasks.sqlite3，管理界面仍待接入")
        report["reconfigure"].append("微信登录态、环境变量中的密钥及机器绑定凭据未自动搬运，需要重新扫码或填写")
        service.store.state("legacy_import", report, write=True)
        service.store.integrity()
        return report
