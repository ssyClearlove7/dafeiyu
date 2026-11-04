"""管理员明确教导，原子保存实际配置与来源，不让模型口头答应冒充生效。"""
import copy
import json
import time
from .paths import LOCK

FIELDS = {"max_messages", "reference_chars", "mode", "reply_every", "unrelated_idle_seconds"}


class Teaching:
    def __init__(self, service):
        self.service = service

    def current(self):
        values = self.service.settings.read()["interaction"]
        state = self.service.paths.json("taught_rules.json", {"rules": {}, "history": []})
        result = []
        old_names = {"max_lines": "max_messages", "unrelated_idle_seconds": "unrelated_idle_seconds", "mode": "mode", "reply_every": "reply_every"}
        for key, item in state.get("rules", {}).items():
            parameter = old_names.get(key, key)
            result.append({"key": key, "value": values.get(parameter, item.get("value")), "source": item.get("source", ""),
                           "actor": item.get("actor", ""), "at": item.get("at"), "taught_value": item.get("value")})
        return result

    def prompt(self):
        return json.dumps([{"key": item["key"], "value": item["value"]} for item in self.current()], ensure_ascii=False)

    def apply(self, context, changes):
        app = self.service
        if context["actor"] not in app.settings.read()["admins"] or context["kind"] != "text":
            raise PermissionError("只有配置管理员当前直接文字可以教导全局规则")
        if not isinstance(changes, dict) or not changes or len(changes) > 12:
            raise ValueError("请明确要修改或撤销的规则")
        with LOCK:
            config = app.settings.read()
            state = app.paths.json("taught_rules.json", {"rules": {}, "history": []})
            state = copy.deepcopy(state)
            ident = str(context["id"])
            previous = next((item for item in state.get("history", []) if item.get("key") == ident), None)
            if previous:
                return {"ok": True, "receipt": previous["receipt"]}
            for key, value in changes.items():
                if key not in FIELDS and not key.startswith("behavior:"):
                    raise ValueError("该教导机制尚未支持，未修改规则")
                item = state["rules"].get(key)
                if value is None:
                    if not item:
                        raise ValueError("未找到要撤销的教导")
                    if key in FIELDS:
                        config["interaction"][key] = item["previous"]
                    state["rules"].pop(key)
                else:
                    if key.startswith("behavior:") and (not isinstance(value, str) or not 1 <= len(value) <= 2000):
                        raise ValueError("行为教导无效")
                    if key in FIELDS:
                        before = config["interaction"][key]
                        config["interaction"][key] = value
                    else:
                        before = None
                    state["rules"][key] = {"value": value, "actor": context["actor"], "source": context["body"],
                        "at": time.time(), "previous": item.get("previous") if item else before}
            app.settings.validate(config)
            receipt = "已保存并回读确认，全局行为规则已生效：" + "、".join(changes)
            state.setdefault("history", []).append({"key": ident, "actor": context["actor"], "source": context["body"],
                                                    "changes": changes, "at": time.time(), "receipt": receipt})
            original_settings = app.paths.path("settings.json").read_bytes()
            old_file = app.paths.path("taught_rules.json")
            original_rules = old_file.read_bytes() if old_file.exists() else None
            try:
                app.paths.save("settings.json", config)
                app.paths.save("taught_rules.json", state)
                if app.paths.json("taught_rules.json") != state or app.settings.read() != config:
                    raise RuntimeError("规则回读失败")
            except Exception:
                app.paths.atomic("settings.json", original_settings)
                if original_rules is None:
                    old_file.unlink(missing_ok=True)
                else:
                    app.paths.atomic("taught_rules.json", original_rules)
                raise
            return {"ok": True, "receipt": receipt, "effective": self.current()}
