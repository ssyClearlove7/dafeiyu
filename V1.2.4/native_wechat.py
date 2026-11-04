"""agent-wechat 原生 HTTP 接入，不加载 Windows DLL。"""
import datetime
import hashlib
import io
import os
from pathlib import Path
import time
from urllib.parse import quote
import uuid

import requests

import config

HERE = Path(__file__).resolve().parent


def image_variants(path):
    """给原生发送器提供有序 JPEG 副本，防止小图被后端放大后违反尺寸校验。"""
    from PIL import Image, ImageOps

    def bounded(frame, edge):
        frame = frame.copy()
        frame.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        for _ in range(4):
            for quality in (85, 70, 55, 40):
                output = io.BytesIO()
                frame.save(output, format="JPEG", quality=quality)
                data = output.getvalue()
                if len(data) <= 65520:
                    return frame, data
            frame.thumbnail((max(1, frame.width // 2), max(1, frame.height // 2)), Image.Resampling.LANCZOS)
        raise ValueError("图片无法压缩到微信发送大小限制内")

    with Image.open(path) as source:
        if source.width > 8192 or source.height > 8192 or source.width * source.height > 16_777_216:
            raise ValueError("图片尺寸超过原生微信接入限制")
        oriented = ImageOps.exif_transpose(source)
        rgba = oriented.convert("RGBA")
        frame = Image.new("RGB", rgba.size, "white")
        frame.paste(rgba, mask=rgba.getchannel("A"))
        original, original_data = bounded(frame, 1280)
        standard, standard_data = bounded(original, 640)
        _, thumbnail_data = bounded(standard, 160)
    return original_data, standard_data, thumbnail_data


class NativeHTTPError(RuntimeError):
    def __init__(self, status, code):
        self.status = status
        super().__init__("agent-wechat HTTP %d: %s" % (status, code))


class NativeNotReadyError(RuntimeError):
    """服务启动、扫码或自动重连尚未完成，可等待后重试。"""


class NativeClient:
    def __init__(self, account=None):
        cfg = config.settings().get("wechat") or {}
        self.base = (os.environ.get("AGENT_WECHAT_URL") or cfg.get("url") or
                     "http://127.0.0.1:6174").rstrip("/")
        self.profile_id = os.environ.get("AGENT_WECHAT_ACCOUNT") or cfg.get("account")
        token_file = os.environ.get("AGENT_WECHAT_TOKEN_FILE") or cfg.get("tokenFile")
        token = os.environ.get("AGENT_WECHAT_TOKEN", "").strip()
        if token_file:
            token = Path(config._expand(token_file)).read_text(encoding="utf-8-sig").strip()
        if not token:
            raise RuntimeError("未配置 agent-wechat Token：设置 AGENT_WECHAT_TOKEN_FILE")
        self.http = requests.Session()
        # 本地接入不经过机器的网页代理。
        self.http.trust_env = False
        self.http.headers["Authorization"] = "Bearer " + token
        self.expected_username = account
        self._profile = None

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.base + path, timeout=kwargs.pop("timeout", 30),
                                     allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            try:
                code = response.json().get("error", {}).get("code", "http_error")
            except (ValueError, AttributeError):
                code = "http_error"
            raise NativeHTTPError(response.status_code, code)
        return response

    def profile(self):
        if self._profile is None:
            rows = self.request("GET", "/v1/accounts").json()["items"]
            if self.profile_id:
                rows = [r for r in rows if self.profile_id in (r["id"], r.get("alias"))]
            elif self.expected_username:
                rows = [r for r in rows if r.get("ownUsername") == self.expected_username]
            if not rows:
                raise NativeNotReadyError("原生微信尚无可用账号，请先扫码登录")
            if len(rows) != 1:
                raise RuntimeError("请先扫码登录，并用 AGENT_WECHAT_ACCOUNT 指定唯一的账号 profile ID")
            selected = rows[0]
            if not selected.get("authenticated") or not selected.get("ownUsername"):
                raise NativeNotReadyError("原生微信正在等待登录或自动重连")
            if self.expected_username and selected["ownUsername"] != self.expected_username:
                raise RuntimeError("会话配置的 account 与当前微信账号不同，拒绝读取或发送")
            self._profile = selected
        return self._profile

    def path(self, suffix):
        return "/v1/accounts/" + quote(self.profile()["id"], safe="") + suffix

    def pages(self, suffix, maximum=None):
        rows, cursor, seen = [], None, set()
        while True:
            params = {"limit": min(200, maximum - len(rows)) if maximum else 200}
            if cursor:
                params["cursor"] = cursor
            page = self.request("GET", self.path(suffix), params=params).json()
            rows.extend(x for x in page["items"] if not x.get("deleted"))
            cursor = page.get("nextCursor")
            if not cursor or (maximum and len(rows) >= maximum):
                return rows[:maximum] if maximum else rows
            if cursor in seen:
                raise RuntimeError("agent-wechat 返回重复分页游标")
            seen.add(cursor)

    def close(self):
        self.http.close()

    def send(self, room, text=None, image=None, timeout=30, voice=None):
        payload = {"threadId": room}
        if voice:
            path = Path(voice)
            if not path.is_file() or not path.stat().st_size:
                return False, {"error": "语音不存在或为空"}
            with path.open("rb") as audio:
                source = self.request("POST", self.path("/uploads"),
                                      files={"file": (path.name, audio, "audio/wav")}, timeout=60).json()
            payload.update(kind="voice", sourceId=source["id"])
        elif image:
            path = Path(image)
            if path.suffix.lower() == ".gif":
                return False, {"error": "原生 Ubuntu 接入暂不支持发送动图表情"}
            if not path.is_file() or not path.stat().st_size:
                return False, {"error": "图片不存在或为空"}
            image_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            variants = image_variants(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != image_digest:
                return False, {"error": "图片内容已变化，请稍后重试"}
            for field, name, data in zip(("sourceId", "standardSourceId", "thumbnailSourceId"),
                                         ("image.jpg", "standard.jpg", "thumbnail.jpg"), variants):
                source = self.request("POST", self.path("/uploads"),
                                      files={"file": (name, data, "image/jpeg")}, timeout=60).json()
                payload[field] = source["id"]
            payload["kind"] = "image"
        else:
            payload["text"] = text
        send_path = self.path("/sends")
        try:
            result = self.request("POST", send_path, json=payload,
                                  headers={"Idempotency-Key": uuid.uuid4().hex}, timeout=timeout).json()
        except (requests.RequestException, NativeHTTPError) as e:
            if isinstance(e, NativeHTTPError) and e.status < 500:
                raise
            # POST 可能已被服务端接收。沿用原发送器的 rc=4，不自动重发。
            return True, {"state": "needs_review", "unverified": True,
                          "error": "发送结果未确认，请在微信核对，未自动重发"}
        suffix = "/jobs/" if image or voice else "/sends/"
        deadline = time.monotonic() + timeout
        while True:
            state = result.get("state")
            if state in ("accepted", "sent", "confirmed", "succeeded"):
                if image:
                    # 登记失败不能把已发送消息报成失败，否则调用方可能重复发送。
                    try:
                        import sticker_store
                        sticker_store.record_sent_image(image, self.profile()["ownUsername"], room,
                                                       result.get("serverMessageId"), image_digest)
                    except Exception as exc:
                        print("表情包引用定位记录失败：%s" % type(exc).__name__, flush=True)
                return True, result
            if state in ("failed", "cancelled", "canceled", "rejected"):
                return False, result
            if state == "needs_review" or time.monotonic() >= deadline:
                return True, dict(result, unverified=True)
            time.sleep(0.5)
            try:
                result = self.request("GET", self.path(suffix + quote(result["id"], safe=""))).json()
            except (requests.RequestException, RuntimeError):
                return True, dict(result, unverified=True)

    def media(self, message):
        mid = message["native_id"]
        suffix = "/messages/" + quote(mid, safe="")
        assets = self.request("GET", self.path(suffix + "/media")).json()["items"]
        ready = next((a for a in assets if a.get("state") == "available"), None)
        if ready is None:
            if not assets:
                raise RuntimeError("这条消息没有可下载的媒体")
            job = self.request("POST", self.path(suffix + "/media-transfers"), json={
                "messageId": mid, "assetId": assets[0]["id"], "quality": "best"}).json()
            deadline = time.monotonic() + 30
            while job.get("state") not in ("available", "failed", "cancelled", "needs_review", "expired", "unsupported"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("媒体仍在下载，稍后重试")
                time.sleep(0.5)
                job = self.request("GET", self.path("/jobs/" + quote(job["id"], safe=""))).json()
            assets = self.request("GET", self.path(suffix + "/media")).json()["items"]
            ready = next((a for a in assets if a.get("state") == "available"), None)
        if not ready:
            raise RuntimeError("媒体未就绪、已过期或当前后端不支持")
        digest = hashlib.sha256((self.profile()["id"] + mid + ready["id"]).encode()).hexdigest()
        target = HERE / "decoded" / ("native-" + digest + ".media")
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            response = self.request("GET", self.path("/media/" + quote(ready["id"], safe="") + "/content"), stream=True)
            temp = target.with_suffix(".tmp")
            try:
                size = 0
                with response, temp.open("wb") as f:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise RuntimeError("媒体超过当前机器人 64 MiB 处理限制")
                        f.write(chunk)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        return str(target)


class NativeDB:
    """把原生接口映射为当前聊天循环使用的读库方法。"""
    def __init__(self, account):
        self.client = NativeClient(account)
        try:
            self.client.profile()
        except requests.RequestException as e:
            self.client.close()
            raise NativeNotReadyError("原生微信服务尚不可达") from e
        except Exception:
            self.client.close()
            raise

    def get_sessions(self):
        try:
            rows = {r["peerId"]: {"username": r["peerId"], "nickname": r.get("displayName"),
                                "unread_count": r.get("unreadCount") or 0}
                    for r in self.client.pages("/threads")}
            # 新登录没有历史会话时，群通讯录仍可用于按名字配置会话。
            for group in self.client.pages("/groups"):
                room = group["roomId"]
                rows.setdefault(room, {"username": room, "nickname": group.get("displayName"),
                                       "unread_count": 0})
            return list(rows.values()), None
        except (requests.RequestException, RuntimeError) as e:
            return None, str(e)

    def get_contact(self, username):
        try:
            endpoint = "/groups/" if username.endswith("@chatroom") else "/contacts/"
            r = self.client.request("GET", self.client.path(endpoint + quote(username, safe=""))).json()
            return {"nickname": r.get("nickname") or r.get("displayName"),
                    "remark": r.get("remark"), "alias": r.get("alias")}, None
        except (requests.RequestException, RuntimeError) as e:
            return None, str(e)

    def get_messages(self, room, limit=50, offset=0):
        try:
            values = self.client.pages("/threads/" + quote(room, safe="") + "/messages", maximum=limit + offset)
            rows = []
            for r in values[offset:]:
                if not r.get("createdAt"):
                    continue
                when = datetime.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
                sender = r.get("senderId") or ""
                if r.get("direction") == "outgoing":
                    sender = self.client.profile()["ownUsername"]
                rows.append({"local_type": r["type"], "create_time": int(when.timestamp()),
                             "sender_username": sender, "message_content": r.get("body") or "",
                             "server_id": r.get("serverId") or "", "native_id": r["id"],
                             "native_account": self.client.profile()["id"],
                             "native_quote": r.get("quote"),
                             "native_mentions_self": (r.get("mentions") or {}).get("self") is True})
            return rows, None
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as e:
            return None, str(e)

    def close(self):
        self.client.close()


def self_profile():
    client = NativeClient()
    try:
        p = client.profile()
        return {"wxid": p["ownUsername"], "profileId": p["id"], "native": True}
    finally:
        client.close()


def send(room, text=None, image=None, account=None, timeout=30, voice=None):
    client = NativeClient(account)
    try:
        return client.send(room, text=text, image=image, timeout=timeout, voice=voice)
    finally:
        client.close()


def media_path(message, account=None):
    client = NativeClient(account)
    try:
        if client.profile()["id"] != message["native_account"]:
            raise RuntimeError("媒体所属账号不一致")
        return client.media(message)
    finally:
        client.close()


def _login_account(client, create=False):
    ident = client.profile_id or "daughter"
    accounts = client.request("GET", "/v1/accounts").json()["items"]
    selected = next((r for r in accounts if ident in (r["id"], r.get("alias"))), None)
    if selected is None and create:
        selected = client.request("POST", "/v1/accounts", json={"id": ident}).json()
    return selected


def login_status(attempt=None):
    client = NativeClient()
    try:
        record = _login_account(client)
        result = {"connected": bool(record and record.get("authenticated")),
                  "wxid": (record or {}).get("ownUsername")}
        if attempt and record:
            result["operation"] = client.request("GET", "/v1/accounts/" + quote(record["id"], safe="") +
                "/login-attempts/" + quote(attempt, safe="")).json()
        return result
    finally:
        client.close()


def start_login():
    client = NativeClient()
    try:
        record = _login_account(client, create=True)
        if record.get("authenticated"):
            return {"connected": True, "wxid": record.get("ownUsername")}
        operation = client.request("POST", "/v1/accounts/" + quote(record["id"], safe="") + "/login-attempts",
            headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                "strategy": "qr", "qrRefresh": {"mode": "auto", "maxRefreshes": 3},
                "allowQr": True, "forceQr": True, "timeoutSeconds": 300,
                "computerName": "大肥鱼 Ubuntu"}).json()
        return {"connected": False, "operation": operation}
    finally:
        client.close()


def login_qr(attempt):
    if not attempt:
        raise ValueError("缺少登录任务 ID")
    client = NativeClient()
    try:
        record = _login_account(client)
        if not record:
            raise RuntimeError("请先点击开始登录")
        return client.request("GET", "/v1/accounts/" + quote(record["id"], safe="") +
            "/login-attempts/" + quote(attempt, safe="") + "/qr").content
    finally:
        client.close()


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>大肥鱼微信登录</title>
<style>body{font:16px system-ui;background:#101720;color:#e7edf6;max-width:650px;margin:50px auto;padding:20px}button{padding:12px 24px}img{display:none;max-width:320px;background:white;padding:12px}a{color:#8ec8ff}#state{white-space:pre-wrap}</style>
<a href="/">← 返回控制面板</a><h1>微信扫码登录</h1>
<p>用大肥鱼的微信账号扫码并在手机确认。新登录不会自动导入 Windows 聊天历史；登录后到「名单」页添加需要回复的群或私聊。</p>
<button id="start">开始登录</button><p id="state">正在检查连接…</p><img id="qr" alt="微信登录二维码">
<script>
let attempt=null, busy=false;
const state=document.querySelector('#state'), qr=document.querySelector('#qr'), button=document.querySelector('#start');
async function status(){
 try{
  const r=await fetch('/api/wechat-login'+(attempt?'?attempt='+encodeURIComponent(attempt):''));const j=await r.json();
  if(!r.ok||!j.ok)throw Error(j.error||'读取失败');
  if(j.connected){state.textContent='已登录：'+j.wxid+'。请返回名单页配置会话。';qr.style.display='none';button.disabled=true;return;}
  state.textContent=attempt?'请扫码并在手机确认。登录状态：'+(j.operation?.state||'准备中'):'尚未登录，点击开始登录生成二维码。';
  if(attempt){qr.src='/api/wechat-login/qr?attempt='+encodeURIComponent(attempt)+'&t='+Date.now();}
 }catch(e){state.textContent=e.message;}
}
qr.onload=()=>qr.style.display='block';qr.onerror=()=>qr.style.display='none';
button.onclick=async()=>{
 if(busy)return;busy=true;button.disabled=true;
 try{const r=await fetch('/api/wechat-login/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'登录失败');attempt=j.operation?.id||null;await status();}
 catch(e){state.textContent=e.message;}finally{busy=false;if(!qr.complete||attempt)button.disabled=false;}
};status();setInterval(status,4000);
</script></html>"""
