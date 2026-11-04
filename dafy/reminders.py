"""按稳定身份授权的持久提醒；明确提醒豁免免打扰，恢复后按原会话汇总。"""
import datetime as dt
import hashlib
import json
import math
import re
import time
import threading
from zoneinfo import ZoneInfo
from .store import packed


class Reminders:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.resume_before = time.time()
        self.offline = True
        self.lock = threading.RLock()

    def allowed(self, room, actor):
        if room.endswith("@chatroom"):
            return actor in self.settings.read()["admins"]
        return actor == room

    def require(self, room, actor):
        if not self.allowed(room, actor):
            raise PermissionError("只能管理本人的私聊提醒，群提醒由配置管理员管理")

    def repeat(self, value):
        if not isinstance(value, dict) or set(value) - {"kind", "seconds", "time", "weekday", "timezone"}:
            raise ValueError("提醒周期不明确")
        value = dict(value)
        kind = value.get("kind", "once")
        if kind not in {"once", "interval", "daily", "weekly"}:
            raise ValueError("当前支持单次、固定间隔、每天和每周提醒，请明确周期")
        value["kind"] = kind
        value.setdefault("timezone", self.settings.read()["timezone"])
        ZoneInfo(value["timezone"])
        if kind == "interval":
            seconds = value.get("seconds")
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 60:
                raise ValueError("重复间隔须至少一分钟")
        if kind in {"daily", "weekly"}:
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value.get("time", ""))):
                raise ValueError("日历提醒须有明确时刻")
        if kind == "weekly" and (type(value.get("weekday")) is not int or not 0 <= value["weekday"] <= 6):
            raise ValueError("每周提醒须明确星期，周一为0")
        return value

    @staticmethod
    def advance(due, recurrence, after):
        kind = recurrence["kind"]
        if kind == "once":
            return None
        if kind == "interval":
            step = recurrence["seconds"]
            return due + (math.floor(max(0, after - due) / step) + 1) * step
        zone = ZoneInfo(recurrence["timezone"])
        base = dt.datetime.fromtimestamp(after, zone)
        hour, minute = map(int, recurrence["time"].split(":"))
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == "weekly":
            candidate += dt.timedelta(days=(recurrence["weekday"] - candidate.weekday()) % 7)
        if candidate.timestamp() <= after:
            candidate += dt.timedelta(days=7 if kind == "weekly" else 1)
        return candidate.timestamp()

    def create(self, account, room, actor, request, content, due_at, recurrence):
        self.require(room, actor)
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
            raise ValueError("提醒内容须为1至2000字")
        try:
            due = dt.datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            if due.tzinfo is None:
                raise ValueError()
            stamp = due.timestamp()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("请明确未来时间和时区") from None
        repeat = self.repeat(recurrence)
        ident = hashlib.sha256(packed([account, room, actor, request]).encode()).hexdigest()[:24]
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM reminders WHERE request=?", (str(request),)).fetchone()
            if existing:
                return dict(existing)
            if stamp <= time.time():
                raise ValueError("提醒时间已经过去，请指定未来时间")
            db.execute("INSERT INTO reminders(id,request,account,room,actor,content,due,recurrence,status,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (ident, str(request), account, room, actor, content.strip(), stamp, packed(repeat), "pending", time.time(), time.time()))
            return dict(db.execute("SELECT * FROM reminders WHERE id=?", (ident,)).fetchone())

    def list(self, account, room, actor):
        self.require(room, actor)
        with self.store.connect() as db:
            if room.endswith("@chatroom"):
                rows = db.execute("SELECT * FROM reminders WHERE account=? AND room=? ORDER BY due", (account, room)).fetchall()
            else:
                rows = db.execute("SELECT * FROM reminders WHERE account=? AND room=? AND actor=? ORDER BY due", (account, room, actor)).fetchall()
        return [dict(row) for row in rows]

    def change(self, account, room, actor, ident, patch):
        with self.lock:
            return self._change(account, room, actor, ident, patch)

    def _change(self, account, room, actor, ident, patch):
        self.require(room, actor)
        visible = {x["id"]: x for x in self.list(account, room, actor)}
        if ident not in visible:
            raise PermissionError("没有找到当前会话有权管理的提醒")
        if not isinstance(patch, dict) or set(patch) - {"content", "due_at", "recurrence", "cancel"}:
            raise ValueError("提醒修改项无效")
        row = visible[ident]
        if patch.get("cancel"):
            status = "cancelled"
        else:
            if row["status"] != "pending":
                raise ValueError("已结束或待核对的提醒不能直接修改，请重新设置")
            status = "pending"
        content, due, repeat = row["content"], row["due"], json.loads(row["recurrence"])
        if "content" in patch:
            content = patch["content"]
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
                raise ValueError("提醒内容无效")
        if "due_at" in patch:
            date = dt.datetime.fromisoformat(patch["due_at"].replace("Z", "+00:00"))
            if date.tzinfo is None or date.timestamp() <= time.time():
                raise ValueError("提醒时间须为带时区的未来时间")
            due = date.timestamp()
        if "recurrence" in patch:
            repeat = self.repeat(patch["recurrence"])
        with self.store.connect() as db:
            db.execute("UPDATE reminders SET content=?,due=?,recurrence=?,status=?,revision=revision+1,updated=? WHERE id=?",
                       (content, due, packed(repeat), status, time.time(), ident))
            return dict(db.execute("SELECT * FROM reminders WHERE id=?", (ident,)).fetchone())

    def describe(self, row):
        zone = ZoneInfo(self.settings.read()["timezone"])
        date = dt.datetime.fromtimestamp(row["due"], zone).strftime("%Y-%m-%d %H:%M")
        return {"id": row["id"], "content": row["content"], "next_time": date, "timezone": str(zone),
                "recurrence": json.loads(row["recurrence"]), "status": row["status"]}

    def tick(self, account, enabled_rooms, deliver, online=True):
        with self.lock:
            return self._tick(account, enabled_rooms, deliver, online)

    def _tick(self, account, enabled_rooms, deliver, online=True):
        now = time.time()
        if not online:
            self.offline = True
            return
        if self.offline:
            self.resume_before = now
            self.offline = False
        with self.store.connect() as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM reminders WHERE account=? AND status='pending' AND due<=? ORDER BY due", (account, now))]
            batches = [json.loads(row[0]) for row in db.execute("SELECT value FROM state WHERE key LIKE 'reminder_batch:%'")]
        batches = [b for b in batches if b["account"] == account]
        claimed = {j["id"] for b in batches for j in b["jobs"]}
        groups = {}
        for row in jobs:
            if row["id"] in claimed:
                continue
            if row["room"] not in enabled_rooms:
                continue
            if not self.allowed(row["room"], row["actor"]):
                with self.store.connect() as db:
                    db.execute("UPDATE reminders SET status='cancelled',updated=? WHERE id=?", (now, row["id"]))
                continue
            missed = row["due"] < self.resume_before or now - row["due"] > 60
            key = (row["room"], "missed" if missed else row["id"])
            groups.setdefault(key, []).append(row)
        for (room, group_kind), batch in groups.items():
            # 首次准备时固化批次；恢复不会新增一条不同key重复发送。
            parts = [[j["id"], j["revision"], j["due"]] for j in batch]
            key = "reminder-" + hashlib.sha256(packed(parts).encode()).hexdigest()
            if group_kind == "missed":
                zone = ZoneInfo(self.settings.read()["timezone"])
                content = "刚恢复运行，以下提醒错过了原定时间：\n" + "\n".join(
                    dt.datetime.fromtimestamp(j["due"], zone).strftime("%m-%d %H:%M") + "：" + j["content"] for j in batch)
            else:
                content = batch[0]["content"]
            with self.store.connect() as db:
                current = [db.execute("SELECT revision,status FROM reminders WHERE id=?", (j["id"],)).fetchone() for j in batch]
                if any(not c or c["revision"] != j["revision"] or c["status"] != "pending" for c, j in zip(current, batch)):
                    continue
            frozen = {"id": key, "account": account, "room": room, "content": content, "jobs": batch}
            self.store.state("reminder_batch:" + key, frozen, write=True)
            batches.append(frozen)
        for frozen in batches:
            batch, room, key = frozen["jobs"], frozen["room"], frozen["id"]
            if room not in enabled_rooms:
                continue
            with self.store.connect() as db:
                current = [db.execute("SELECT revision,status FROM reminders WHERE id=?", (j["id"],)).fetchone() for j in batch]
                valid = all(c and c["revision"] == j["revision"] and c["status"] == "pending" and self.allowed(room, j["actor"]) for c, j in zip(current, batch))
                if not valid:
                    db.execute("DELETE FROM state WHERE key=?", ("reminder_batch:" + key,))
                    continue
            result = deliver(key, room, frozen["content"], allow_quiet=True)
            if result != "confirmed":
                continue
            with self.store.connect() as db:
                for job in batch:
                    next_due = self.advance(job["due"], json.loads(job["recurrence"]), now)
                    db.execute("UPDATE reminders SET status=?,due=?,updated=? WHERE id=? AND revision=? AND status='pending'",
                               ("completed" if next_due is None else "pending", job["due"] if next_due is None else next_due,
                                now, job["id"], job["revision"]))
                db.execute("DELETE FROM state WHERE key=?", ("reminder_batch:" + key,))
