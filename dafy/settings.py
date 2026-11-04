"""控制台与聊天教导共用的原子配置；不包含预算或自动模型切换。"""
import copy
import datetime as dt
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from .paths import LOCK

MODEL_DEFAULTS = {
    "chat": {"protocol": "chat_completions", "url": "https://api.deepseek.com/chat/completions", "model": "deepseek-flash", "key": "", "timeout": 60, "retries": 1},
    "vision": {"protocol": "chat_completions", "url": "https://api.deepseek.com/chat/completions", "model": "deepseek-flash", "key": "", "timeout": 90, "retries": 1},
    "asr": {"protocol": "chat_audio", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "model": "qwen3-asr-flash", "key": "", "timeout": 90, "retries": 1, "enabled": True},
    "tts": {"protocol": "dashscope_tts", "url": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer", "model": "cosyvoice-v3-flash", "key": "", "voice": "", "timeout": 90, "retries": 1, "enabled": True},
}
PROTOCOLS = {"chat": {"chat_completions"}, "vision": {"chat_completions"},
             "asr": {"chat_audio", "openai_transcription"}, "tts": {"dashscope_tts", "openai_speech"}}
DEFAULTS = {
    "schema": 1, "timezone": "Asia/Shanghai", "admins": [], "console_actor": "",
    "models": MODEL_DEFAULTS, "rooms": [],
    "quiet": {"start": "00:30", "end": "07:30"},
    "memory": {"time": "04:00"},
    "backup": {"time": "05:00", "keep": 7, "directory": ""},
    "interaction": {"max_messages": 2, "reference_chars": 200, "merge_seconds": 2, "max_wait_seconds": 5, "reply_gap": 1, "proactive": True,
                    "mode": "活跃参与", "reply_every": 1, "unrelated_idle_seconds": 0},
    "limits": {"file_mb": 20, "video_mb": 50, "video_seconds": 300, "document_chars": 60000, "video_frames": 12},
    "wechat": {"url": "http://127.0.0.1:6174", "account": "daughter", "version": "v1.3.3"},
    "updates": {"manifest": ""},
    "web": {"enabled": False, "url": "https://api.deepseek.com/v1/tools/search", "key": ""},
}


class Settings:
    def __init__(self, paths):
        self.paths = paths
        if not paths.path("settings.json").exists():
            paths.save("settings.json", copy.deepcopy(DEFAULTS))

    def read(self, public=False):
        with LOCK:
            raw = self.paths.json("settings.json", {})
        value = copy.deepcopy(DEFAULTS)
        self._merge(value, raw)
        self.validate(value)
        if public:
            for config in [*value["models"].values(), value["web"]]:
                config["key_set"] = bool(config.get("key"))
                config["key"] = ""
        return value

    @staticmethod
    def _merge(target, patch):
        for key, value in patch.items():
            if key not in target:
                raise ValueError("未知配置项：" + str(key))
            if isinstance(target[key], dict):
                if not isinstance(value, dict):
                    raise ValueError("配置应为对象：" + key)
                Settings._merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    def save(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("设置必须为对象")
        with LOCK:
            current = self.read()
            patch = copy.deepcopy(patch)
            for kind, config in patch.get("models", {}).items():
                if isinstance(config, dict):
                    config.pop("key_set", None)
                    if config.get("key") == "":
                        config.pop("key", None)  # 空白不覆盖已保存密钥。
            if isinstance(patch.get("web"), dict):
                patch["web"].pop("key_set", None)
                if patch["web"].get("key") == "":
                    patch["web"].pop("key", None)
            self._merge(current, patch)
            self.validate(current)
            self.paths.save("settings.json", current)
        return self.read(public=True)

    @staticmethod
    def validate(value):
        if value["schema"] != 1:
            raise ValueError("配置版本不受支持")
        ZoneInfo(value["timezone"])
        for group, fields in (("quiet", ("start", "end")), ("memory", ("time",)), ("backup", ("time",))):
            for key in fields:
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[group][key]):
                    raise ValueError("时间须为HH:MM")
        if type(value["backup"]["keep"]) is not int or not 1 <= value["backup"]["keep"] <= 1000:
            raise ValueError("自动备份保留份数须为1至1000")
        for kind, config in value["models"].items():
            if "enabled" in config and type(config["enabled"]) is not bool:
                raise ValueError("语音开关须为布尔值")
            if not isinstance(config["key"], str) or len(config["key"]) > 10000:
                raise ValueError("模型密钥格式无效")
            if config["protocol"] not in PROTOCOLS[kind]:
                raise ValueError(kind + "接口格式不支持")
            parsed = urlparse(config["url"])
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("服务地址须为不含凭据或查询参数的HTTP地址")
            if not isinstance(config["model"], str) or not config["model"].strip():
                raise ValueError("模型名称不能为空")
            if type(config["timeout"]) not in (float, int) or not 5 <= config["timeout"] <= 300:
                raise ValueError("模型超时须为5至300秒")
            if type(config["retries"]) is not int or not 0 <= config["retries"] <= 3:
                raise ValueError("重试次数须为0至3")
        if not isinstance(value["admins"], list) or any(not isinstance(x, str) or not x or len(x) > 128 for x in value["admins"]):
            raise ValueError("管理员须为真实微信ID列表")
        if value["console_actor"] and value["console_actor"] not in value["admins"]:
            raise ValueError("控制台绑定身份须在配置管理员名单中")
        seen = set()
        for room in value["rooms"]:
            if not isinstance(room, dict) or set(room) - {"id", "name", "enabled"} or not isinstance(room.get("id"), str) or not room["id"]:
                raise ValueError("会话须包含稳定ID、名称和启用状态")
            if room["id"] in seen or type(room.get("enabled")) is not bool:
                raise ValueError("会话重复或启用值无效")
            seen.add(room["id"])
        for key, (minimum, maximum) in {
            "file_mb": (1, 50), "video_mb": (1, 50), "video_seconds": (1, 1800),
            "document_chars": (1000, 200000), "video_frames": (1, 40)
        }.items():
            if type(value["limits"][key]) is not int or not minimum <= value["limits"][key] <= maximum:
                raise ValueError(key + "超出组件处理范围")
        for key in ("max_messages", "reference_chars", "merge_seconds", "max_wait_seconds", "reply_gap"):
            number = value["interaction"][key]
            if type(number) not in (int, float) or not 0 <= number <= 10000:
                raise ValueError("互动参数无效：" + key)
        if not 1 <= value["interaction"]["max_messages"] <= 8:
            raise ValueError("单次回应条数须为1至8")
        if type(value["interaction"]["max_messages"]) is not int or type(value["interaction"]["reference_chars"]) is not int or type(value["interaction"]["proactive"]) is not bool:
            raise ValueError("回应条数和参考字数须为整数，主动关心须为开关")
        if value["interaction"]["mode"] not in {"活跃参与", "情绪参与", "与我有关", "点名参与"}:
            raise ValueError("群聊参与方式无效")
        if type(value["interaction"]["reply_every"]) is not int or not 1 <= value["interaction"]["reply_every"] <= 1000:
            raise ValueError("群聊接话计数参数无效")
        if type(value["interaction"]["unrelated_idle_seconds"]) is not int or not 0 <= value["interaction"]["unrelated_idle_seconds"] <= 86400:
            raise ValueError("接无关话题前的安静时长无效")
        if value["interaction"]["max_wait_seconds"] < value["interaction"]["merge_seconds"]:
            raise ValueError("最长合并等待不能小于空闲等待")
        parsed = urlparse(value["wechat"]["url"])
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("微信接入服务必须为本机HTTP地址")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value["wechat"]["account"]):
            raise ValueError("微信实例名无效")

    def now(self):
        return dt.datetime.now(ZoneInfo(self.read()["timezone"]))

    def quiet(self, now=None):
        value = self.read()
        current = (now or self.now()).strftime("%H:%M")
        start, end = value["quiet"]["start"], value["quiet"]["end"]
        return start <= current < end if start < end else current >= start or current < end if start != end else False
