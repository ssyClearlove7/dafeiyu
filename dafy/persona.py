"""沿用人设，演化仅修改通用表达特征，不把私聊事实写入全局人格。"""
import hashlib
import json
import time
from .paths import CODE, LOCK

TRAITS = {"温柔": 3, "幽默": 3, "简洁": 3, "活泼": 3}
LABELS = {1: "很少", 2: "略少", 3: "适中", 4: "略多", 5: "较多"}


class Persona:
    def __init__(self, paths, store):
        self.paths, self.store = paths, store
        for name in ("persona.txt", "persona_evolved.txt"):
            if not paths.path(name).exists():
                candidate = CODE / "V1.2.4" / name
                paths.atomic(name, candidate.read_text(encoding="utf-8-sig") if candidate.exists() else "你是大肥鱼，住在微信里的AI陪伴者，中文自然口语，保持真实和温暖。")
        if not paths.path("persona_traits.json").exists():
            paths.save("persona_traits.json", TRAITS)

    def snapshot(self):
        with LOCK:
            value = {kind: self.paths.path(name).read_text(encoding="utf-8-sig") for kind, name in
                     (("base", "persona.txt"), ("evolved", "persona_evolved.txt"))}
            value["traits"] = self.paths.json("persona_traits.json", TRAITS)
            teaching = self.paths.path("taught_behavior.txt")
            value["teaching"] = teaching.read_text(encoding="utf-8-sig") if teaching.exists() else ""
            value["revision"] = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            return value

    def prompt(self):
        value = self.snapshot()
        traits = "；".join(key + "：" + LABELS[number] for key, number in value["traits"].items())
        return ("【基础人设，始终优先】\n" + value["base"] + "\n【已有演化人设】\n" + value["evolved"] +
                "\n【长期形成的表达习惯】" + traits + "\n【管理员教导人设】\n" + value["teaching"] +
                "\n基础身份不因对话改变，不复述私人资料作为公开人格。")

    def save(self, kind, body, source, expected=None):
        if kind not in {"base", "evolved", "teaching"} or not isinstance(body, str) or kind == "base" and not body.strip() or len(body) > 30000:
            raise ValueError("人设或教导正文无效")
        with LOCK:
            if expected and self.snapshot()["revision"] != expected:
                raise ValueError("人设已被修改，请重新读取后保存")
            with self.store.connect() as db:
                db.execute("INSERT INTO persona_versions(kind,body,source,created) VALUES (?,?,?,?)",
                           (kind, self.snapshot().get(kind, ""), str(source), time.time()))
            if kind == "teaching":
                self.paths.atomic("taught_behavior.txt", body)
            else:
                self.paths.atomic("persona.txt" if kind == "base" else "persona_evolved.txt", body)
        return self.snapshot()

    def evolve(self, traits, sources):
        if not isinstance(traits, dict) or set(traits) - set(TRAITS):
            raise ValueError("自动成长仅允许调整通用表达习惯")
        with LOCK:
            if time.time() - float(self.store.state("persona_last_change", 0)) < 86400:
                return {"changed": False, "reason": "今天已经成长过，保留长期稳定性"}
            current = self.paths.json("persona_traits.json", TRAITS)
            value = dict(current)
            for key, number in traits.items():
                if type(number) is not int or not 1 <= number <= 5 or abs(number - current[key]) > 1:
                    raise ValueError("每次仅可缓慢调整一个档位")
                value[key] = number
            if value == current:
                return {"changed": False}
            with self.store.connect() as db:
                db.execute("INSERT INTO persona_versions(kind,body,source,created) VALUES (?,?,?,?)",
                           ("traits", json.dumps(current, ensure_ascii=False), json.dumps(sources), time.time()))
            self.paths.save("persona_traits.json", value)
            self.store.state("persona_last_change", time.time(), write=True)
            return {"changed": True}

    def history(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM persona_versions ORDER BY id DESC LIMIT 100")]

    def rollback(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM persona_versions WHERE id=?", (int(ident),)).fetchone()
        if not row:
            raise ValueError("未找到人设历史")
        if row["kind"] == "traits":
            value = json.loads(row["body"])
            if set(value) != set(TRAITS) or any(type(x) is not int or not 1 <= x <= 5 for x in value.values()):
                raise ValueError("历史人设格式无效")
            self.paths.save("persona_traits.json", value)
        else:
            self.save(row["kind"], row["body"] or " ", "控制台回退")
        return self.snapshot()
