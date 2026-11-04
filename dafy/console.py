"""测试环境与安装版共用控制台的业务适配；复用产品权限和真实操作回执。"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from .paths import LOCK


class Console:
    FIELDS = {
        "REPLY_MODE": ("mode", "群聊参与方式", str, None, None),
        "MAX_LINES": ("max_messages", "一次回复最多文字消息条数", int, 1, 8),
        "MAX_LINE": ("reference_chars", "单条参考字数，完整语义允许超长", int, 0, 10000),
        "REPLY_EVERY": ("reply_every", "每几条群友消息最多回复一次", int, 1, 1000),
        "UNRELATED_IDLE_SECS": ("unrelated_idle_seconds", "接无关话题前安静时长（秒）", int, 0, 86400),
        "MERGE_IDLE_SECS": ("merge_seconds", "消息合并空闲等待（秒）", float, 0, 10000),
        "MERGE_MAX_SECS": ("max_wait_seconds", "消息合并最长等待（秒）", float, 0, 10000),
        "REPLY_GAP": ("reply_gap", "多条回复间隔（秒）", float, 0, 10000),
        "PROACTIVE": ("proactive", "允许主动关心和发起话题（1开启，0关闭）", int, 0, 1),
    }

    def __init__(self, engine, scope):
        self.app, self.scope = engine, scope

    def rooms(self):
        account = self.app.store.state("account_identity", "")
        return [{"room": r["id"], "name": r["name"], "account": account, "enabled": r["enabled"]}
                for r in self.app.settings.read()["rooms"]]

    def admins(self):
        return [{"wxid": value, "name": ""} for value in self.app.settings.read()["admins"]]

    def resolve(self, value):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("请填写有效微信 ID 或唯一名称")
        if value.startswith("wxid_") or value.endswith("@chatroom"):
            return value
        rows = list(self.app.native.pages("/threads")) + list(self.app.native.pages("/contacts"))
        matches = {r.get("peerId") or r.get("username") or r.get("id") for r in rows
                   if value in {r.get("displayName"), r.get("remark"), r.get("nickname")}}
        matches.discard(None)
        if len(matches) != 1:
            raise ValueError("名称无法唯一识别，请先登录微信并填写准确微信 ID：" + value)
        return matches.pop()

    def tuning(self):
        from .settings import DEFAULTS
        config = self.app.settings.read()
        spec, values = {}, {}
        for key, (field, description, kind, minimum, maximum) in self.FIELDS.items():
            spec[key] = {"desc": description, "type": kind.__name__, "min": minimum,
                         "max": maximum, "default": int(DEFAULTS["interaction"][field]) if field == "proactive" else DEFAULTS["interaction"][field]}
            values[key] = int(config["interaction"][field]) if field == "proactive" else config["interaction"][field]
        spec["REPLY_MODE"]["options"] = ["活跃参与", "情绪参与", "与我有关", "点名参与"]
        spec["CURFEW_PERIODS"] = {"default": [DEFAULTS["quiet"]]}
        values["CURFEW_PERIODS"] = [] if config["quiet"]["start"] == config["quiet"]["end"] else [config["quiet"]]
        return {"ok": True, "spec": spec, "values": values, "file": "控制台设置",
                "groups": [{"title": "回复形态", "keys": list(self.FIELDS)}], "singlePeriod": True}

    def mutes(self):
        return [{"scope": key, "left": math.ceil(until-time.time())}
                for key, until in self.app.store.state("console_mutes", {}).items() if until > time.time()]

    def permitted_rooms(self):
        result = []
        account = self.app.store.state("account_identity", "")
        with self.app.store.connect() as db:
            recorded = {r[0] for r in db.execute("SELECT room FROM events WHERE account=? UNION SELECT room FROM notes WHERE account=?", (account, account))}
        for row in self.rooms():
            if row["room"] not in recorded:
                continue
            try:
                self.scope(row["room"])
            except PermissionError:
                continue
            result.append(row["room"])
        return result

    def get(self, path, query):
        app, config = self.app, self.app.settings.read()
        account = app.store.state("account_identity", "")
        if path == "/api/overview":
            status = app.native.status()
            collected = app.store.state("collection", {})
            age = max(0, int(time.time()-collected["at"])) if collected.get("at") else None
            return {"ok": True, "product": True, "backend": "native", "backend_label": "独立微信组件（非官方）",
                    "bot": {"running": app.enabled, "pid": os.getpid(), "age": age, "round": None},
                    "hook": {"ok": bool(status.get("connected")), "wxid": status.get("wxid", "")},
                    "wechat": [], "mutes": self.mutes(),
                    "curfew": {"periods": self.tuning()["values"]["CURFEW_PERIODS"], "active": app.settings.quiet()},
                    "rooms": {r["room"]: r["name"] for r in self.rooms()},
                    "admins": self.admins(), "whitelist": sorted(app.enabled_rooms()),
                    "now": app.settings.now().strftime("%Y-%m-%d %H:%M:%S"), "version": __import__("dafy").__version__}
        if path == "/api/rooms":
            return {"ok": True, "rooms": self.rooms()}
        if path == "/api/admins":
            return {"ok": True, "admins": self.admins()}
        if path == "/api/whitelist":
            return {"ok": True, "whitelist": sorted(app.enabled_rooms())}
        if path == "/api/tuning":
            return self.tuning()
        if path == "/api/apikey":
            return {"ok": True, "set": bool(config["models"]["chat"]["key"]), "masked": "已配置" if config["models"]["chat"]["key"] else "",
                    "source": "本机控制台设置", "allowDsh": False}
        if path == "/api/cloud":
            return {"ok": True, "configured": bool(config["models"]["asr"]["key"]),
                    "voice": bool(config["models"]["asr"]["key"]) and config["models"]["asr"].get("enabled", True),
                    "voiceReply": bool(config["models"]["tts"]["key"]) and config["models"]["tts"].get("enabled", True),
                    "web": config["web"]["enabled"], "webConfigured": bool(config["models"]["chat"]["key"]),
                    "voiceId": config["models"]["tts"]["voice"], "voiceModel": config["models"]["tts"]["model"]}
        if path == "/api/persona":
            return dict(app.persona.snapshot(), ok=True)
        if path == "/api/taught-rules":
            text = app.persona.snapshot()["teaching"]
            return {"ok": True, "text": text, "revision": hashlib.sha256(text.encode()).hexdigest()}
        if path == "/api/stickers":
            return {"ok": True, "items": [dict(r, what=r["description"], keys=r["keywords"]) for r in app.sticker_entries()]}
        if path == "/api/usage":
            days = int(query.get("days", "14"))
            if not 1 <= days <= 90:
                raise ValueError("统计天数无效")
            stats = app.store.statistics(config["timezone"], days)
            today = app.settings.now().date()
            rows = [dict(row, provider=row["model"], key_id=row["feature"], key_label=row["feature"]) for row in stats["rows"]]
            with app.store.connect() as db:
                started = db.execute("SELECT MIN(at) FROM calls").fetchone()[0] or app.boot
            return {"ok": True, "days": [(today-dt.timedelta(days=n)).isoformat() for n in reversed(range(days))],
                    "rows": rows, "started_at": dt.datetime.fromtimestamp(started, dt.timezone.utc).isoformat(), "grouping": "model"}
        if path == "/api/memory/status":
            rooms = self.permitted_rooms()
            with app.store.connect() as db:
                events = sum(db.execute("SELECT COUNT(*) FROM events WHERE account=? AND room=?", (account, room)).fetchone()[0] for room in rooms)
                pending = sum(db.execute("SELECT COUNT(*) FROM events WHERE account=? AND room=? AND done=0", (account, room)).fetchone()[0] for room in rooms)
                topics = sum(db.execute("SELECT COUNT(DISTINCT topic) FROM notes WHERE account=? AND room=?", (account, room)).fetchone()[0] for room in rooms)
                tasks = []
                for room in rooms:
                    tasks.extend(dict(r) for r in db.execute("SELECT topic,body FROM notes WHERE account=? AND room=? AND kind='task' ORDER BY id DESC LIMIT 20", (account, room)))
            return {"ok": True, "events": events, "topics": topics, "pending": pending, "tasks": tasks[:20],
                    "state": {"整理": app.store.state("compression"), "说明": "仅展示绑定管理员有权访问的会话"}}
        if path == "/api/memory/search":
            result = []
            for room in self.permitted_rooms():
                result.extend(app.store.search(account, room, query.get("q", ""), limit=30))
            return {"ok": True, "items": [{k: r[k] for k in ("id", "room", "actor", "body", "at", "kind") if k in r} for r in result[:60]]}
        if path == "/api/log":
            from .diagnostics import read
            return {"ok": True, "lines": [json.dumps(r, ensure_ascii=False) for r in read(app.paths)[-400:]]}
        if path == "/api/wechat-login":
            return dict(app.native.login_status(query.get("attempt")), ok=True)
        return None

    def post(self, path, body):
        app, config = self.app, self.app.settings.read()
        if path == "/api/rooms":
            rows = body.get("rooms")
            if not isinstance(rows, list):
                raise ValueError("会话必须为列表")
            result = []
            account = app.store.state("account_identity", "")
            for r in rows:
                if not isinstance(r, dict) or type(r.get("enabled")) is not bool:
                    raise ValueError("会话格式无效")
                if r.get("account") and r["account"] != account:
                    raise ValueError("机器人账号与当前已登录账号不一致")
                ident = self.resolve((r.get("room") or r.get("name") or "").strip())
                result.append({"id": ident, "name": r.get("name") or ident, "enabled": r["enabled"]})
            app.settings.save({"rooms": result})
            return {"ok": True, "rooms": self.rooms()}
        if path == "/api/admins/save":
            rows = body.get("admins")
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError("管理员必须为对象列表")
            ids = [self.resolve((r.get("wxid") or r.get("name") or "").strip()) for r in rows]
            if any(x.endswith("@chatroom") for x in ids):
                raise ValueError("管理员必须是个人微信账号")
            actor = config["console_actor"] if config["console_actor"] in ids else (ids[0] if ids else "")
            app.settings.save({"admins": ids, "console_actor": actor})
            return {"ok": True, "admins": self.admins()}
        if path == "/api/tuning":
            patch = body.get("patch", {})
            if not isinstance(patch, dict) or set(patch)-set(self.FIELDS)-{"CURFEW_PERIODS"}:
                raise ValueError("互动参数无效")
            value = {}
            interaction = {}
            for key, raw in patch.items():
                if key == "CURFEW_PERIODS":
                    if not isinstance(raw, list) or len(raw) > 1:
                        raise ValueError("当前安装版支持一个每日免打扰时段")
                    value["quiet"] = raw[0] if raw else {"start": "00:00", "end": "00:00"}
                else:
                    field, _, kind, _, _ = self.FIELDS[key]
                    converted = kind(raw)
                    if field == "proactive":
                        if converted not in (0, 1):
                            raise ValueError("主动联系开关只允许0或1")
                        converted = bool(converted)
                    interaction[field] = converted
            if interaction:
                value["interaction"] = interaction
            app.settings.save(value)
            return dict(self.tuning(), changed=list(patch))
        if path == "/api/apikey":
            if body.get("allowDsh"):
                raise ValueError("安装版只使用买家自己配置的 API 密钥")
            key = body.get("key", "").strip()
            if key:
                app.settings.save({"models": {"chat": {"key": key}}, "web": {"key": key}})
            return self.get(path, {})
        if path == "/api/apikey/test":
            if body.get("key") and body["key"].strip() != config["models"]["chat"]["key"]:
                raise ValueError("请先保存新的聊天密钥，再检查连接")
            result = app.models.chat([{"role": "user", "content": "只回复：连接正常"}], max_tokens=32)
            return {"ok": True, "detail": result.get("content", "")}
        if path == "/api/cloud/save":
            models = {}
            for label, kind in (("voice", "asr"), ("voiceReply", "tts")):
                item = {}
                if label in body:
                    if type(body[label]) is not bool:
                        raise ValueError("语音开关必须是布尔值")
                    item["enabled"] = body[label]
                if body.get("apiKey", "").strip():
                    item["key"] = body["apiKey"].strip()
                if kind == "tts" and "voiceId" in body:
                    item["voice"] = body["voiceId"].strip()
                if item:
                    models[kind] = item
            patch = {"models": models} if models else {}
            if "web" in body:
                patch["web"] = {"enabled": body["web"], "key": config["models"]["chat"]["key"]}
            if models.get("tts", {}).get("enabled") and not models["tts"].get("voice", config["models"]["tts"]["voice"]):
                raise ValueError("启用语音回复前，请填写属于你百炼账号且匹配合成模型的音色标识")
            app.settings.save(patch)
            return self.get("/api/cloud", {})
        if path == "/api/cloud/test":
            rows = app.models.search("今天的北京时间日期是什么？")
            return {"ok": True, "sourceCount": len(rows)}
        if path == "/api/bot":
            if body.get("action") not in {"start", "stop"}:
                raise ValueError("机器人操作无效")
            app.set_enabled(body["action"] == "start")
            return {"ok": True, "bot": {"running": app.enabled}}
        if path == "/api/wechat-login/start":
            app.native.start()
            result = app.native.login()
            if result.get("id") and not result.get("operation"):
                result["operation"] = {"id": result["id"]}
            return dict(result, ok=True)
        if path == "/api/persona/save" and "text" in body:
            app.persona.save(body["kind"], body["text"], "控制台管理员")
            return {"ok": True}
        if path == "/api/taught-rules/save":
            text = body.get("text")
            if not isinstance(text, str) or len(text) > 30000:
                raise ValueError("教导文本无效")
            before = self.get("/api/taught-rules", {})
            if body.get("revision") != before["revision"]:
                raise ValueError("教导已被修改，请刷新后重试")
            app.persona.save("teaching", text, "控制台管理员")
            return self.get("/api/taught-rules", {})
        if path in {"/api/send", "/api/stickers/send"}:
            room = body.get("room", "")
            if room not in app.enabled_rooms():
                raise PermissionError("目标会话未启用")
            attachment = None
            text = body.get("text", "")
            if path.endswith("stickers/send"):
                name = body.get("file", "")
                if name not in {r["file"] for r in app.sticker_entries()}:
                    raise ValueError("素材不存在")
                attachment = app.paths.path("assets/"+name)
                if attachment.suffix.lower() == ".gif":
                    raise ValueError("当前接入组件不支持发送 GIF 表情")
            elif not isinstance(text, str) or not text.strip() or len(text) > 10000:
                raise ValueError("请填写有效消息正文")
            state = app.deliver("console-"+uuid.uuid4().hex, room, text, attachment=attachment, kind="image" if attachment else None)
            if state != "confirmed":
                raise RuntimeError("发送状态为"+state+"，尚未确认成功，请核对后再操作")
            return {"ok": True}
        if path == "/api/stickers/delete":
            name = body.get("file", "")
            if not isinstance(name, str) or Path(name).name != name or name.startswith("."):
                raise ValueError("素材文件名无效")
            with LOCK:
                target = app.paths.path("assets/"+name)
                if not target.is_file():
                    raise ValueError("素材不存在")
                index = app.paths.path("assets/index.md")
                if index.exists():
                    lines = [l for l in index.read_text(encoding="utf-8-sig").splitlines() if l.strip().strip("|").split("|")[0].strip().strip("`") != name]
                    app.paths.atomic("assets/index.md", "\n".join(lines)+"\n")
                target.unlink()
            return {"ok": True}
        if path == "/api/mute":
            minutes = float(body.get("minutes", 0))
            if not math.isfinite(minutes) or not 0 <= minutes <= 1440:
                raise ValueError("静音分钟数须为0到1440")
            with LOCK:
                mutes = app.store.state("console_mutes", {})
                if not minutes:
                    mutes = {}
                else:
                    room = body.get("room", "")
                    if room not in app.enabled_rooms():
                        raise ValueError("请选择已启用的会话")
                    if not room.endswith("@chatroom") and room not in config["admins"]:
                        raise PermissionError("全体静音请选择管理员私聊")
                    mutes[room if room.endswith("@chatroom") else "global"] = time.time()+minutes*60
                app.store.state("console_mutes", mutes, write=True)
            return {"ok": True, "mutes": self.mutes()}
        return None
