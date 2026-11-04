"""沿用现有公开 Skill 与表情管理实现；身份、配置和模型调用接到产品边界。"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from contextvars import ContextVar
import copy
import datetime as dt
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import types
from .paths import CODE

_CURRENT = ContextVar("dafy_legacy_actor", default=None)


class Legacy:
    def __init__(self, service):
        self.service = service
        self.ready = False
        self.modules = {}

    def initialize(self):
        if self.ready:
            return
        source = CODE / "V1.2.4"
        sys.path.insert(0, str(source))
        app = self.service
        config = types.ModuleType("config")
        def adapted():
            settings = app.settings.read()
            return {"wechat": {"backend": "native", "url": settings["wechat"]["url"], "account": settings["wechat"]["account"],
                               "tokenFile": str(app.paths.path("wechat-api-token"))},
                    "paths": {"sticker_lib": str(app.paths.path("assets")), "wechat_files": str(app.paths.path("wechat"))},
                    "deepseek": {"apiKey": settings["models"]["chat"]["key"]}, "voice": {"enabled": bool(settings["models"]["tts"]["key"])},
                    "curfew": {"periods": [settings["quiet"]]}}
        config.settings = adapted
        config.wechat_backend = lambda: "native"
        config.sticker_lib = lambda: str(app.paths.path("assets"))
        config.wechat_files = lambda: str(app.paths.path("wechat"))
        config.paths = lambda: adapted()["paths"]
        config.reload = lambda: None
        config._expand = lambda p: str(Path(p).expanduser())
        config.ffmpeg = lambda: importlib.import_module("dafy.media").ffmpeg()
        sys.modules["config"] = config
        memory = types.ModuleType("memory_store")
        memory.ROOT = app.paths.path("memory")
        memory.CONTEXT = ContextVar("legacy_memory_context", default={})
        def record(kind, key, body, payload, account="", room="", sender="", at="", admin=False):
            context = _CURRENT.get() or {}
            account, room = account or context.get("account", ""), room or context.get("room", "")
            sender = sender or context.get("actor", "")
            if not account or not room:
                return False
            stamp = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() if at else __import__("time").time()
            payload = dict(payload if isinstance(payload, dict) else {"result": payload}, origins=[context["id"]] if context.get("id") else [])
            return app.store.record(account, room, sender, "tool", kind, body, payload, stamp, "legacy-tool:" + key)
        memory.record = record
        sys.modules["memory_store"] = memory
        loop = types.ModuleType("wx_chat_loop")
        loop.load_admins = lambda *args: app.settings.read()["admins"]
        loop.api_key = lambda: app.settings.read()["models"]["chat"]["key"]
        loop.load_settings = adapted
        loop.chat_json = lambda system, user, max_tokens=1600, temperature=0.1: app.models.json(system, json.loads(user) if isinstance(user, str) and user.startswith(("{", "[")) else user, max_tokens)
        loop._deepseek = lambda system, user, temperature=0.7, max_tokens=1600, **kwargs: app.models.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=max_tokens, temperature=temperature).get("content", "")
        loop.in_curfew = lambda *args: app.settings.quiet()
        loop.mute_left = lambda *args: 0
        loop.open_db = lambda account: self.modules["native_wechat"].NativeDB(account)
        loop.message_request_text = lambda msg: msg.get("message_content", "")
        loop.refresh_persona = lambda: None
        loop.MAX_LINES = 2
        loop.MAX_LINE = 200
        sys.modules["wx_chat_loop"] = loop
        task = types.ModuleType("task_store")
        task.record_receipt = lambda name, result: record("tool", hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                                                        json.dumps({"tool": name, "result": result}, ensure_ascii=False), result)
        sys.modules["task_store"] = task
        cloud = importlib.import_module("wx_cloud")
        cloud.search = app.models.search
        cloud._post = lambda path, body: app.models.request(
            "vision" if "image_url" in json.dumps(body) else "asr" if "input_audio" in json.dumps(body) else "chat",
            {key: value for key, value in body.items() if key in {"messages", "temperature", "max_tokens", "stream"}})
        cloud.enabled = lambda feature: bool(app.settings.read()["web"]["enabled"]) if feature == "web" else bool(app.settings.read()["models"]["asr"]["key"])
        self.modules["wx_cloud"] = cloud
        vision = importlib.import_module("vision_ovh")
        vision.describe = lambda path, prompt: app.models.vision([Path(path)], prompt)
        native = importlib.import_module("native_wechat")
        native.HERE = app.paths.path("temporary")
        # 继承原生客户端协议方法，临时媒体写入本次工具工作目录。
        original = native.NativeClient
        class Client(original):
            def media(self, message):
                folder = _CURRENT.get().get("temporary")
                if not folder:
                    raise RuntimeError("缺少受管理临时目录")
                return str(app.native.media({"id": message["native_id"], "type": message.get("local_type", 3),
                                            "_room": _CURRENT.get()["room"]}, Path(folder), 25 * 1024 * 1024))
        native.NativeClient = Client
        self.modules["native_wechat"] = native
        for name in ("sticker_lib", "sticker_store", "sticker_skill", "skill_manager"):
            self.modules[name] = importlib.import_module(name)
        library = self.modules["sticker_lib"]
        library.LIB, library.INDEX, library.HERE = str(app.paths.path("assets")), str(app.paths.path("assets/index.md")), str(app.paths.root)
        for key in ("USED", "SAVED", "HASHES"):
            setattr(library, key, str(app.paths.path({"USED": "sticker_used.json", "SAVED": "sticker_saved.json", "HASHES": "sticker_hashes.json"}[key])))
        stickers = self.modules["sticker_skill"]
        stickers.HERE = app.paths.root
        for key, name in (("JOBS", "sticker_skill_jobs.json"), ("CATALOG", "sticker_catalog.json"), ("AUTHOR_CATALOG", "sticker_author_catalog.json")):
            setattr(stickers, key, app.paths.path(name))
        self.modules["skill_manager"].ROOT = app.paths.path("installed_skills")
        # 内置技能说明属于程序资源，不能要求在用户数据目录再复制代码。
        resource = source / "skills/sticker-manager/SKILL.md"
        if resource.exists():
            destination = app.paths.path("skills/sticker-manager/SKILL.md")
            if not destination.exists():
                app.paths.atomic("skills/sticker-manager/SKILL.md", resource.read_bytes())
        self.ready = True

    @contextmanager
    def actor(self, context):
        self.initialize()
        with self.service.paths.temporary() as temp:
            context = dict(context, temporary=str(temp))
            token = _CURRENT.set(context)
            stickers = self.modules["sticker_skill"]
            previous = stickers._context.set(context)
            memory = sys.modules["memory_store"]
            mem = memory.CONTEXT.set({"account": context["account"], "room": context["room"], "sender": context["actor"]})
            try:
                yield
            finally:
                memory.CONTEXT.reset(mem)
                stickers._context.reset(previous)
                _CURRENT.reset(token)

    def tools(self, context):
        with self.actor(context):
            return copy.deepcopy(self.modules["skill_manager"].TOOLS + self.modules["sticker_skill"].model_tools())

    def execute(self, name, args, context):
        with self.actor(context), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            manager = self.modules["skill_manager"]
            if name in manager.TOOL_NAMES:
                return manager.execute(name, args)
            stickers = self.modules["sticker_skill"]
            if name in stickers.TOOL_NAMES:
                return stickers.execute(name, args)
            raise ValueError("不支持的已有工具")

    def stickers(self):
        self.initialize()
        return self.modules["sticker_lib"].entries()

    def instructions(self):
        self.initialize()
        return self.modules["skill_manager"].instructions()
