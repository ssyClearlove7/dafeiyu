"""产品拥有的独立微信进程；仅本机接入，账号校验和幂等发送。"""
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urlparse
import uuid
import xml.etree.ElementTree as ET
import requests
from .paths import CODE


class NativeError(RuntimeError):
    pass


class Native:
    def __init__(self, paths, settings, store):
        self.paths, self.settings, self.store = paths, settings, store
        self.process = None
        self.lock = threading.RLock()
        self.profile = None
        self.last_error = ""
        self.job = None

    def base(self):
        return self.settings.read()["wechat"]["url"].rstrip("/")

    def token(self):
        path = self.paths.path("wechat-api-token")
        if not path.exists():
            self.paths.atomic("wechat-api-token", secrets.token_urlsafe(32) + "\n")
        return path.read_text(encoding="utf-8").strip()

    def request(self, method, path, **kwargs):
        if method not in {"GET", "POST"}:
            raise ValueError("接入适配仅使用GET和POST")
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(method, self.base() + path, headers={
                    "Authorization": "Bearer " + self.token(), **kwargs.pop("headers", {})},
                    timeout=kwargs.pop("timeout", (5, 30)), allow_redirects=False, **kwargs)
            if not 200 <= response.status_code < 300:
                try:
                    reason = response.json().get("error", {}).get("code", "request_failed")
                except (ValueError, AttributeError):
                    reason = "request_failed"
                raise NativeError("微信接入HTTP %d：%s" % (response.status_code, str(reason)[:80]))
            return response
        except requests.RequestException as exc:
            raise NativeError("微信接入未就绪：" + type(exc).__name__) from None

    def binary(self):
        name = "wx.exe" if os.name == "nt" else "wx"
        path = CODE / "bin" / name
        if not path.is_file():
            raise NativeError("独立微信组件尚未安装，请运行组件准备命令或使用完整安装包")
        return path

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            if self.job:
                self.job.close()
                self.job = None
            endpoint = urlparse(self.base())
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex((endpoint.hostname, endpoint.port)) == 0:
                    raise NativeError("接入端口已被其他进程占用，不接管未知服务；请在控制台调整端口")
            self.token()
            self.paths.atomic("wechat-server.toml", 'schemaVersion = 1\nlisten = ' +
                json.dumps("127.0.0.1:" + str(endpoint.port)) + '\ndataDir = ' +
                json.dumps(str(self.paths.path("wechat")), ensure_ascii=False) + '\ntokenFile = ' +
                json.dumps(str(self.paths.path("wechat-api-token")), ensure_ascii=False) + "\n")
            command = [str(self.binary()), "--server-config", str(self.paths.path("wechat-server.toml")), "serve"]
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             creationflags=0x08000000 if os.name == "nt" else 0)
            if os.name == "nt":
                from .lifecycle import Job
                self.job = Job(self.process)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise NativeError("独立微信组件启动失败，退出码：" + str(self.process.returncode))
                try:
                    self.request("GET", "/health", timeout=(1, 1))
                    self.last_error = ""
                    return
                except NativeError:
                    time.sleep(0.3)
            raise NativeError("独立微信组件未在启动期限内就绪")

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(5)
            self.process = None
            if self.job:
                self.job.close()
                self.job = None
            self.profile = None

    def accounts(self):
        return self.request("GET", "/v1/accounts").json()["items"]

    def selected(self, create=False):
        ident = self.settings.read()["wechat"]["account"]
        records = [row for row in self.accounts() if ident in (row.get("id"), row.get("alias"))]
        if not records and create:
            return self.request("POST", "/v1/accounts", json={"alias": ident}).json()
        if len(records) != 1:
            raise NativeError("请先在控制台创建并扫码登录指定微信实例")
        return records[0]

    def account(self):
        profile = self.selected()
        if not profile.get("authenticated") or not profile.get("ownUsername"):
            raise NativeError("微信尚未扫码登录")
        previous = self.store.state("account_identity", "")
        account = profile["ownUsername"]
        if previous and previous != account:
            raise NativeError("微信账号与原有记忆绑定账号不一致，已阻止启用")
        if not previous:
            self.store.state("account_identity", account, write=True)
        self.profile = profile
        return account

    def path(self, suffix):
        profile = self.profile or self.selected()
        return "/v1/accounts/" + quote(profile["id"], safe="") + suffix

    def pages(self, path, maximum=None):
        cursor, seen, count = None, set(), 0
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            value = self.request("GET", self.path(path), params=params).json()
            for item in value["items"]:
                if not item.get("deleted"):
                    yield item
                    count += 1
                    if maximum and count >= maximum:
                        return
            cursor = value.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise NativeError("微信接入返回重复分页游标")
            seen.add(cursor)

    def login(self):
        profile = self.selected(create=True)
        self.profile = profile
        if profile.get("authenticated"):
            return {"connected": True, "wxid": self.account()}
        return self.request("POST", self.path("/login-attempts"), headers={"Idempotency-Key": uuid.uuid4().hex},
            json={"strategy": "qr", "qrRefresh": {"mode": "auto", "maxRefreshes": 3}, "allowQr": True,
                  "forceQr": True, "timeoutSeconds": 300, "computerName": "大肥鱼"}).json()

    def login_status(self, ident=None):
        profile = self.selected()
        value = {"connected": bool(profile.get("authenticated")), "wxid": profile.get("ownUsername")}
        if ident:
            value["operation"] = self.request("GET", self.path("/login-attempts/" + quote(ident, safe=""))).json()
        return value

    def qr(self, ident):
        return self.request("GET", self.path("/login-attempts/" + quote(ident, safe="") + "/qr")).content

    def status(self):
        value = {"component_running": bool(self.process and self.process.poll() is None), "connected": False, "error": self.last_error,
                 "version": self.settings.read()["wechat"]["version"]}
        if value["component_running"]:
            try:
                value.update(self.login_status())
                if value["connected"]:
                    value["wxid"] = self.account()
            except NativeError as exc:
                value["error"] = str(exc)
        return value

    def send(self, ident, room, text, attachment=None, kind=None, quote_message=None, mentions=None):
        # 第一次发出前意图已由应用持久化，同一key可查询而不重复提交。
        try:
            original = self.request("GET", self.path("/sends/by-idempotency-key"), params={"key": ident}).json()
        except NativeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            original = None
        if original is None:
            payload = {"threadId": room}
            if attachment:
                with attachment.open("rb") as stream:
                    uploaded = self.request("POST", self.path("/uploads"), files={"file": (attachment.name, stream)}, timeout=(10, 90)).json()
                payload.update(kind=kind, sourceId=uploaded["id"])
            else:
                payload["text"] = text
                if quote_message:
                    payload["quote"] = {"messageId": quote_message["id"], "serverId": str(quote_message["serverId"])}
                if mentions:
                    payload["mentions"] = {"userIds": mentions}
            original = self.request("POST", self.path("/sends"), headers={"Idempotency-Key": ident}, json=payload).json()
        remote = original.get("id")
        deadline = time.monotonic() + 25
        while original.get("state") not in {"accepted", "sent", "confirmed", "succeeded", "failed", "cancelled", "rejected", "needs_review"}:
            if not remote or time.monotonic() > deadline:
                return "uncertain", original
            time.sleep(0.4)
            suffix = "/jobs/" if attachment else "/sends/"
            original = self.request("GET", self.path(suffix + quote(remote, safe=""))).json()
        state = original.get("state")
        return ("confirmed" if state in {"accepted", "sent", "confirmed", "succeeded"} else "uncertain" if state == "needs_review" else "failed"), original

    def media(self, message, folder, limit):
        ident = str(message["id"])
        if message.get("threadId") and message["threadId"] != message["_room"]:
            raise PermissionError("媒体不属于当前会话")
        suffix = "/messages/" + quote(ident, safe="")
        assets = self.request("GET", self.path(suffix + "/media")).json()["items"]
        ready = next((x for x in assets if x.get("state") == "available"), None)
        selected = ready or next(iter(assets), None)
        if not selected:
            raise NativeError("没有可读取的媒体，可能已过期")
        size = selected.get("byteLength") or selected.get("advertisedByteLength")
        if isinstance(size, int) and size > limit:
            raise ValueError("媒体文件超过配置大小")
        if ready is None:
            job = self.request("POST", self.path(suffix + "/media-transfers"),
                               json={"messageId": ident, "assetId": selected["id"], "quality": "best"}).json()
            deadline = time.monotonic() + 60
            while job.get("state") not in {"available", "succeeded", "failed", "expired", "unsupported", "cancelled", "needs_review"}:
                if time.monotonic() > deadline:
                    raise NativeError("媒体下载超时，尚未理解其内容")
                time.sleep(0.5)
                job = self.request("GET", self.path("/jobs/" + quote(job["id"], safe=""))).json()
            assets = self.request("GET", self.path(suffix + "/media")).json()["items"]
            ready = next((x for x in assets if x.get("state") == "available"), None)
        if ready is None:
            raise NativeError("原文件不可用或已过期，未进行内容分析")
        name = ready.get("filename") or ready.get("fileName") or message.get("filename") or ""
        if not name:
            try:
                xml = ET.fromstring(message.get("body") or "")
                name = (xml.findtext(".//title") or "file") + "." + (xml.findtext(".//fileext") or "")
            except ET.ParseError:
                pass
        kind = int(message.get("type") or 0) & 65535
        suffix = Path(str(name).replace("\\", "/")).suffix.lower()
        if not re_safe_suffix(suffix):
            suffix = {3: ".jpg", 34: ".silk", 43: ".mp4", 47: ".gif"}.get(kind, ".bin")
        target = folder / ("source-" + uuid.uuid4().hex + suffix)
        size = 0
        response = self.request("GET", self.path("/media/" + quote(ready["id"], safe="") + "/content"), stream=True, timeout=(10, 60))
        try:
            with response, target.open("wb") as output:
                for block in response.iter_content(65536):
                    size += len(block)
                    if size > limit:
                        raise ValueError("实际媒体大小超过配置范围")
                    output.write(block)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def stamp(message):
        try:
            return dt.datetime.fromisoformat(message["createdAt"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError):
            return time.time()


def re_safe_suffix(value):
    return value in {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".markdown", ".csv",
                     ".jpg", ".jpeg", ".png", ".webp", ".gif", ".wav", ".mp3", ".silk", ".mp4", ".mov", ".m4a"}
