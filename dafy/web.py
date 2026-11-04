"""本机与局域网控制台；免登录，全部业务变更通过POST并校验CSRF。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse
import uuid
from .paths import CODE
from .native import NativeError
from .models import ModelError


class Jobs:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        with engine.store.connect() as db:
            for row in db.execute("SELECT key,value FROM state WHERE key LIKE 'job:%'").fetchall():
                item = json.loads(row["value"])
                if item.get("state") in {"pending", "running"}:
                    item.update(state="interrupted", error="服务重启，操作结果待核对；未自动重做")
                    db.execute("UPDATE state SET value=? WHERE key=?", (json.dumps(item, ensure_ascii=False), row["key"]))

    def start(self, title, function):
        ident = uuid.uuid4().hex
        self.engine.store.state("job:" + ident, {"id": ident, "title": title, "state": "pending", "created": time.time()}, write=True)
        def worker():
            with self.lock:
                self.engine.store.state("job:" + ident, {"id": ident, "title": title, "state": "running", "created": time.time()}, write=True)
                try:
                    result = function()
                    record = {"id": ident, "title": title, "state": "completed", "result": result, "at": time.time()}
                except Exception as exc:
                    record = {"id": ident, "title": title, "state": "failed",
                              "error": str(exc) if isinstance(exc, (ValueError, PermissionError, NativeError, ModelError, RuntimeError)) else type(exc).__name__, "at": time.time()}
                self.engine.store.state("job:" + ident, record, write=True)
        threading.Thread(target=worker, name="dafy-job-" + ident[:8], daemon=True).start()
        return {"id": ident, "state": "pending"}


def lan(peer):
    try:
        address = ipaddress.ip_address(peer)
        return address.is_loopback or address.is_private
    except ValueError:
        return False


def make_server(engine, host="0.0.0.0", port=8790):
    jobs = Jobs(engine)
    csrf = secrets.token_urlsafe(32)
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    try:
        allowed_hosts.update(x[4][0] for x in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    if host not in {"0.0.0.0", "::"}:
        allowed_hosts.add(host)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Dafeiyu"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # 不记录口令、原话、查询参数及媒体地址。

        def send(self, status, value, content_type="application/json; charset=utf-8", headers=None):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            for name, text in (headers or {}).items():
                self.send_header(name, text)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def admission(self, mutation=False):
            if not lan(self.client_address[0]):
                raise PermissionError("控制台仅供本机和局域网访问")
            hostname = urlparse("http://" + self.headers.get("Host", "")).hostname
            if hostname not in allowed_hosts:
                raise PermissionError("控制台Host无效，请使用本机或局域网IP")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise PermissionError("拒绝跨站请求")
            if mutation and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                raise PermissionError("请求校验失败，请刷新页面")

        def body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError("请求长度无效")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求须为JSON")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("请求须为对象")
            return value

        def scope(self, room):
            config = engine.settings.read()
            actor = config["console_actor"]
            if not actor:
                raise PermissionError("请先在设置中绑定控制台管理员的真实微信ID")
            if not room.endswith("@chatroom"):
                if room != actor:
                    raise PermissionError("私聊内容默认保密，控制台不读取其他人的私聊")
            else:
                try:
                    engine.native.request("GET", engine.native.path("/groups/" + __import__("urllib.parse", fromlist=["quote"]).quote(room, safe="") + "/members/" +
                                                                       __import__("urllib.parse", fromlist=["quote"]).quote(actor, safe=""))).json()
                except NativeError:
                    raise PermissionError("未确认绑定管理员属于此群，不读取群内容") from None
            return actor

        def route_get(self):
            path = urlparse(self.path).path
            query = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
            if path in {"/", "/app.js", "/app.css"}:
                self.admission()
                name = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}[path]
                file = Path(__file__).parent / "ui" / name
                return self.send(200, file.read_bytes(), mimetypes.guess_type(name)[0] + "; charset=utf-8")
            if path == "/api/console/state":
                self.admission()
                return self.send(200, {"csrf": csrf})
            self.admission()
            if path == "/api/status":
                return self.send(200, engine.status())
            if path == "/api/diagnostics":
                from .diagnostics import read
                return self.send(200, {"items": read(engine.paths)})
            if path == "/api/settings":
                return self.send(200, engine.settings.read(public=True))
            if path == "/api/statistics":
                return self.send(200, engine.store.statistics(engine.settings.read()["timezone"], int(query.get("days", 30))))
            if path == "/api/persona":
                return self.send(200, engine.persona.snapshot())
            if path == "/api/persona/history":
                return self.send(200, {"items": engine.persona.history()})
            if path == "/api/teaching":
                rows = engine.teaching.current()
                for row in rows:
                    if row["actor"] != engine.settings.read()["console_actor"]:
                        row["source"] = "其他管理员的教导原话不在此披露"
                return self.send(200, {"items": rows})
            if path == "/api/memory/search":
                room = query.get("room", "")
                self.scope(room)
                account = engine.store.state("account_identity", "")
                rows = engine.store.search(account, room, query.get("q", ""), limit=30)
                return self.send(200, {"items": [{key: row[key] for key in ("id", "actor", "body", "at", "kind")} for row in rows]})
            if path == "/api/reminders":
                room = query.get("room", "")
                actor = self.scope(room)
                return self.send(200, {"items": [engine.reminders.describe(row) for row in engine.reminders.list(engine.store.state("account_identity", ""), room, actor)]})
            if path == "/api/wechat/status":
                return self.send(200, engine.native.login_status(query.get("attempt")))
            if path == "/api/wechat/qr":
                return self.send(200, engine.native.qr(query["attempt"]), "image/png")
            if path in {"/api/wechat/threads", "/api/wechat/contacts"}:
                engine.native.account()
                suffix = "/threads" if path.endswith("threads") else "/contacts"
                fields = ("id", "peerId", "username", "displayName", "remark", "nickname", "kind", "type")
                return self.send(200, {"items": [{k: row[k] for k in fields if k in row} for row in engine.native.pages(suffix)]})
            if path == "/api/assets":
                return self.send(200, {"items": engine.sticker_entries()})
            if path == "/api/tools":
                engine.legacy.initialize()
                return self.send(200, {"skills": engine.legacy.modules["skill_manager"].installed(),
                                      "tools": [x["function"]["name"] for x in engine.legacy.modules["skill_manager"].TOOLS],
                                      "repair": "旧版执行器已保留；产品安装版的自修复部署链路待适配与验证"})
            if path == "/api/backups":
                return self.send(200, {"items": engine.archives.registered()})
            if path == "/api/jobs":
                ident = query.get("id", "")
                if not re.fullmatch(r"[a-f0-9]{32}", ident):
                    raise ValueError("任务编号无效")
                return self.send(200, engine.store.state("job:" + ident, {"state": "not_found"}))
            if path == "/api/archive/download":
                name = query.get("file", "")
                if not re.fullmatch(r"[a-zA-Z0-9-]+\.dafy", name):
                    raise ValueError("归档文件名无效")
                file = engine.paths.path("exports/" + name)
                if not file.exists():
                    registered = next((row for row in engine.archives.registered() if Path(row["path"]).name == name), None)
                    if not registered:
                        raise ValueError("归档未登记")
                    file = Path(registered["path"])
                if file.is_symlink() or not file.is_file():
                    raise ValueError("归档不可用")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file.stat().st_size))
                self.send_header("Content-Disposition", 'attachment; filename="' + name + '"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with file.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        self.wfile.write(block)
                return
            if path == "/api/updates":
                return self.send(200, engine.updates.check())
            return self.send(404, {"error": "没有这个入口"})

        def route_post(self):
            path = urlparse(self.path).path
            self.admission(mutation=True)
            if path == "/api/archive/upload":
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 512 * 1024 * 1024:
                    raise ValueError("归档上传须在512MiB以内")
                ident = uuid.uuid4().hex
                target = engine.paths.path("temporary/upload-" + ident + ".dafy")
                left = size
                with target.open("wb") as file:
                    while left:
                        block = self.rfile.read(min(left, 1024 * 1024))
                        if not block:
                            target.unlink(missing_ok=True)
                            raise ValueError("上传中断")
                        file.write(block)
                        left -= len(block)
                engine.paths.protect(target)
                return self.send(200, {"upload": ident})
            body = self.body()
            if path == "/api/settings/save":
                before = engine.settings.read()
                if "wechat" in body and any(body["wechat"].get(k, before["wechat"][k]) != before["wechat"][k] for k in ("url", "account")):
                    with engine.maintenance():
                        result = engine.settings.save(body)
                    return self.send(200, result)
                with engine.gate.work():
                    return self.send(200, engine.settings.save(body))
            if path == "/api/persona/save":
                with engine.gate.work():
                    return self.send(200, engine.persona.save(body["kind"], body["body"], "控制台管理员", body.get("revision")))
            if path in {"/api/memory/correct", "/api/memory/delete"}:
                room = body.get("room", "")
                actor = self.scope(room)
                account = engine.store.state("account_identity", "")
                with engine.gate.work():
                    if path.endswith("correct"):
                        engine.store.correct(account, room, actor, body["id"], body["text"])
                        return self.send(200, {"ok": True})
                    sources = engine.store.delete(account, room, actor, body["ids"])
                    engine.archives.purge_sources(sources)
                    return self.send(200, {"ok": True, "scope": "应用记忆及本机登记备份；接入组件内部数据清理待验收"})
            if path == "/api/persona/rollback":
                with engine.gate.work():
                    return self.send(200, engine.persona.rollback(body["id"]))
            if path == "/api/bot/start":
                return self.send(200, engine.set_enabled(True))
            if path == "/api/bot/stop":
                return self.send(200, engine.set_enabled(False))
            if path == "/api/wechat/start":
                with engine.gate.work():
                    engine.native.start()
                    return self.send(200, engine.native.status())
            if path == "/api/wechat/login":
                with engine.gate.work():
                    engine.native.start()
                    return self.send(200, engine.native.login())
            if path == "/api/models/check":
                feature = body.get("feature", "chat")
                if feature not in {"chat", "vision", "asr", "tts"}:
                    raise ValueError("模型能力无效")
                if feature != "chat":
                    return self.send(200, {"configured": bool(engine.settings.read()["models"][feature]["key"]), "note": "实际调用需使用对应媒体验证"})
                with engine.gate.work():
                    result = engine.models.chat([{"role": "user", "content": "只回复：连接正常"}], max_tokens=16)
                    return self.send(200, {"ok": True, "text": result.get("content", "")})
            if path == "/api/backups/create":
                return self.send(202, jobs.start("手动备份", lambda: engine.archives.create(reason="manual")))
            if path == "/api/migration/export":
                password = body.get("password", "")
                if len(password) < 8:
                    raise ValueError("迁移包密码至少8位")
                def export():
                    engine.enabled = False
                    engine.store.state("bot_enabled", False, write=True)
                    result = engine.archives.create(password=password, reason="migration")
                    engine.native.stop()
                    result["paused"] = True
                    return result
                return self.send(202, jobs.start("导出迁移包", export))
            if path == "/api/migration/import":
                upload = body.get("upload", "")
                if not re.fullmatch(r"[a-f0-9]{32}", upload):
                    raise ValueError("请先上传完整迁移包")
                file = engine.paths.path("temporary/upload-" + upload + ".dafy")
                if not file.is_file():
                    raise ValueError("上传包不存在或已清理")
                password, destination = body.get("password", ""), body.get("destination", "")
                if not destination:
                    raise ValueError("请选择一个空数据目录")
                return self.send(202, jobs.start("校验并导入迁移包", lambda: engine.archives.import_into_empty(file, destination, password)))
            if path == "/api/migration/import-legacy":
                from .migrate import import_legacy
                source = body.get("directory", "")
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("请填写旧版本运行数据所在目录")
                return self.send(202, jobs.start("导入旧版本数据", lambda: import_legacy(source, engine)))
            if path == "/api/migration/activate":
                record = engine.store.state("job:" + str(body.get("job", "")))
                if not record or record.get("state") != "completed" or not record.get("result", {}).get("destination"):
                    raise ValueError("请先完成迁移包校验和导入")
                destination = record["result"]["destination"]
                from .launch import write_location
                write_location(destination)
                self.send(200, {"ok": True, "restarting": True})
                engine.restart_data = destination
                engine.restart_requested.set()
                return
            if path == "/api/updates/install":
                upload = body.get("upload", "")
                if not re.fullmatch(r"[a-f0-9]{32}", upload):
                    raise ValueError("请先上传完整升级包")
                file = engine.paths.path("temporary/upload-" + upload + ".dafy")
                return self.send(202, jobs.start("安装新版本", lambda: engine.updates.install(file)))
            if path == "/api/updates/rollback":
                return self.send(202, jobs.start("回退上一版本并保留当前数据", engine.updates.rollback))
            return self.send(404, {"error": "没有这个操作"})

        def do_GET(self):
            try:
                self.route_get()
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError, NativeError, RuntimeError) as exc:
                self.send(400, {"error": str(exc)[:300]})
            except Exception as exc:
                self.send(500, {"error": "操作未完成：" + type(exc).__name__})

        def do_POST(self):
            try:
                self.route_post()
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError, NativeError, RuntimeError) as exc:
                self.send(400, {"error": str(exc)[:300]})
            except Exception as exc:
                self.send(500, {"error": "操作未完成：" + type(exc).__name__})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.jobs = jobs
    return server
