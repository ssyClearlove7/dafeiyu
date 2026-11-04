"""SQLite 持久层：原文与证据、按会话检索、调用计数及删除防复现。"""
from contextlib import contextmanager
from pathlib import Path
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo
from .paths import LOCK
from . import DATA_VERSION


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def terms(value):
    pieces = re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_@.-]+", str(value).lower())
    words = []
    for piece in pieces:
        if re.match(r"[\u3400-\u9fff]", piece):
            words.extend(piece)
            words.extend(piece[i:i + 2] for i in range(len(piece) - 1))
        else:
            words.append(piece)
    return " ".join(dict.fromkeys(words))


class Store:
    def __init__(self, paths):
        self.paths = paths
        self.file = paths.path("memory/state.sqlite3")
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > DATA_VERSION:
                raise ValueError("数据库版本比程序新，禁止降级强读")
            db.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
            db.execute("PRAGMA user_version=" + str(DATA_VERSION))
            ledger = self.paths.json("privacy-ledger.json", {"sources": []})
            recorded = {row[0] for row in db.execute("SELECT source FROM tombstones")}
            missing = set(ledger["sources"]) - recorded
            if missing:
                self.redact(db, missing)
                row = db.execute("SELECT value FROM state WHERE key='backup_purge'").fetchone()
                pending = json.loads(row[0]) if row else {"sources": []}
                db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("backup_purge", packed({"sources": sorted(missing | set(pending["sources"])), "state": "pending"})))
        paths.protect(self.file)

    @contextmanager
    def connect(self):
        with LOCK:
            db = sqlite3.connect(self.file, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA secure_delete=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def state(self, key, value=None, write=False):
        with self.connect() as db:
            if write:
                db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, packed(value)))
                return value
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else value

    @staticmethod
    def source(account, room, ident):
        return hashlib.sha256(packed([account, room, str(ident)]).encode()).hexdigest()

    def record(self, account, room, actor, direction, kind, body, payload, at, ident):
        source = self.source(account, room, ident)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM tombstones WHERE source=?", (source,)).fetchone():
                return None
            found = db.execute("SELECT id FROM events WHERE source=?", (source,)).fetchone()
            if found:
                return found[0]
            cur = db.execute("INSERT INTO events(source,account,room,actor,direction,kind,body,payload,at,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (source, account, room, actor, direction, str(kind), body or "", packed(payload), at, time.time()))
            db.execute("INSERT INTO lookup VALUES (?,?,?)", (terms(body), "event", cur.lastrowid))
            return cur.lastrowid

    def augment(self, event_id, text):
        with self.connect() as db:
            row = db.execute("SELECT body FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                return False
            body = row["body"] + "\n【已理解内容】" + text
            db.execute("UPDATE events SET body=?,done=0 WHERE id=?", (body, event_id))
            db.execute("DELETE FROM lookup WHERE kind='event' AND ref=?", (event_id,))
            db.execute("INSERT INTO lookup VALUES (?,?,?)", (terms(body), "event", event_id))
            return True

    def search(self, account, room, query="", limit=12, ids=None):
        limit = max(1, min(int(limit), 40))
        with self.connect() as db:
            if ids:
                selected = [int(x) for x in ids][:40]
                rows = db.execute("SELECT * FROM events WHERE account=? AND room=? AND id IN (" + ",".join("?" for _ in selected) + ") ORDER BY id",
                                  [account, room, *selected]).fetchall()
            elif query.strip() and terms(query):
                expression = " OR ".join('"' + x.replace('"', '""') + '"' for x in terms(query).split()[:60])
                rows = db.execute("""SELECT e.* FROM lookup l JOIN events e ON e.id=l.ref
                    WHERE lookup MATCH ? AND l.kind='event' AND e.account=? AND e.room=?
                    ORDER BY rank LIMIT ?""", (expression, account, room, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE account=? AND room=? ORDER BY id DESC LIMIT ?", (account, room, limit)).fetchall()
            return [dict(r) for r in rows]

    def context(self, account, room, query):
        events = self.search(account, room, query, 10)
        with self.connect() as db:
            notes = [dict(r) for r in db.execute("SELECT * FROM notes WHERE account=? AND room=? ORDER BY id DESC LIMIT 12", (account, room))]
            # 二次验证每条知识的全部来源；旧版本跨会话摘要不能越过来源边界。
            visible = []
            for note in notes:
                ids = json.loads(note["origin"])
                if not ids:
                    continue
                sources = db.execute("SELECT id,account,room FROM events WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
                if len(sources) == len(set(ids)) and all(r["account"] == account and r["room"] == room for r in sources):
                    visible.append({key: note[key] for key in ("id", "topic", "body", "origin")})
        return {"events": [{key: e[key] for key in ("id", "actor", "at", "body")} for e in events], "notes": visible}

    def correct(self, account, room, actor, event_id, body):
        if room.endswith("@chatroom"):
            raise PermissionError("本人记忆管理请在私聊操作")
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id=? AND account=? AND room=? AND (actor=? OR direction='outgoing')",
                             (int(event_id), account, room, actor)).fetchone()
            if not row or actor != room:
                raise PermissionError("只能修正自己的私聊记忆")
            if not isinstance(body, str) or not body.strip() or len(body) > 10000:
                raise ValueError("修正正文无效")
            db.execute("INSERT INTO corrections(event_id,body,created) VALUES (?,?,?)", (event_id, row["body"], time.time()))
            db.execute("UPDATE events SET body=?,done=0 WHERE id=?", (body, event_id))
            db.execute("DELETE FROM lookup WHERE kind='event' AND ref=?", (event_id,))
            db.execute("INSERT INTO lookup VALUES (?,?,?)", (terms(body), "event", event_id))
            notes = db.execute("SELECT id,origin,previous FROM notes WHERE account=? AND room=?", (account, room)).fetchall()
            affected = {n["id"] for n in notes if int(event_id) in json.loads(n["origin"])}
            while True:
                expanded = affected | {n["id"] for n in notes if n["previous"] in affected}
                if expanded == affected:
                    break
                affected = expanded
            for note in notes:
                if note["id"] in affected:
                    db.execute("DELETE FROM notes WHERE id=?", (note["id"],))
                    db.execute("DELETE FROM lookup WHERE kind IN ('note','knowledge') AND ref=?", (note["id"],))
                    db.executemany("UPDATE events SET done=0 WHERE id=?", [(x,) for x in json.loads(note["origin"])])
            return True

    @staticmethod
    def redact(db, sources):
        sources = set(sources)
        if not sources:
            return {"events": 0, "sources": []}
        rows = db.execute("SELECT id,source,payload FROM events").fetchall()
        removed = {row["id"] for row in rows if row["source"] in sources}
        changed = True
        while changed:
            before = len(removed)
            for row in rows:
                payload = json.loads(row["payload"])
                origins = payload.get("origins", []) if isinstance(payload, dict) else []
                if isinstance(origins, list) and removed & set(origins):
                    removed.add(row["id"])
                    sources.add(row["source"])
            changed = len(removed) != before
        notes = db.execute("SELECT id,origin,previous FROM notes").fetchall()
        removed_notes = {row["id"] for row in notes if removed & set(json.loads(row["origin"]))}
        changed = True
        while changed:
            before = len(removed_notes)
            removed_notes.update(row["id"] for row in notes if row["previous"] in removed_notes)
            changed = len(removed_notes) != before
        for row in notes:
            if row["id"] in removed_notes:
                # 受旧合并摘要影响的其他来源仍保留，重新整理，不保留含已删事实的后继正文。
                for event_id in set(json.loads(row["origin"])) - removed:
                    db.execute("UPDATE events SET done=0 WHERE id=?", (event_id,))
                db.execute("DELETE FROM notes WHERE id=?", (row["id"],))
                db.execute("DELETE FROM lookup WHERE kind IN ('note','knowledge') AND ref=?", (row["id"],))
        for row in db.execute("SELECT id,origin FROM shares").fetchall():
            if removed & set(json.loads(row["origin"])):
                db.execute("DELETE FROM deliveries WHERE id=?", (row["id"],))
                db.execute("DELETE FROM shares WHERE id=?", (row["id"],))
        for row in db.execute("SELECT id,origin FROM turns").fetchall():
            if removed & set(json.loads(row["origin"])):
                db.execute("DELETE FROM deliveries WHERE id LIKE ?", (row["id"] + ":%",))
                db.execute("DELETE FROM handled WHERE turn_id=?", (row["id"],))
                db.execute("DELETE FROM turns WHERE id=?", (row["id"],))
        for ident in removed:
            db.execute("DELETE FROM corrections WHERE event_id=?", (ident,))
            db.execute("DELETE FROM handled WHERE event_id=?", (ident,))
            db.execute("DELETE FROM lookup WHERE kind='event' AND ref=?", (ident,))
            db.execute("DELETE FROM events WHERE id=?", (ident,))
        for source in sources:
            db.execute("INSERT OR REPLACE INTO tombstones VALUES (?,?)", (source, time.time()))
        db.execute("INSERT INTO lookup(lookup) VALUES('optimize')")
        return {"events": len(removed), "sources": sorted(sources)}

    def delete(self, account, room, actor, ids):
        if room.endswith("@chatroom") or actor != room:
            raise PermissionError("只能在自己的私聊删除本人记忆")
        selected = list(dict.fromkeys(int(x) for x in ids))
        if not selected or len(selected) > 1000:
            raise ValueError("请明确指定要删除的记忆编号，每批最多1000条")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE id IN (" + ",".join("?" for _ in selected) + ")", selected).fetchall()
            if len(rows) != len(selected) or any(r["account"] != account or r["room"] != room for r in rows):
                raise PermissionError("请求包含不属于本人私聊的记忆")
            result = self.redact(db, [row["source"] for row in rows])
            prior = self.paths.json("privacy-ledger.json", {"sources": []})
            self.paths.save("privacy-ledger.json", {"sources": sorted(set(prior["sources"]) | set(result["sources"]))})
            previous_pending = db.execute("SELECT value FROM state WHERE key='backup_purge'").fetchone()
            pending = json.loads(previous_pending[0]) if previous_pending else {"sources": []}
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("backup_purge", packed({"sources": sorted(set(pending["sources"]) | set(result["sources"])), "state": "pending"})))
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
        # 仅保存消息身份摘要，供恢复旧版本时防复现，不保存被删正文。
        return result["sources"]

    def statistics(self, timezone, days=30):
        zone = ZoneInfo(timezone)
        since = (dt.datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=max(1, min(days, 366)) - 1)).timestamp()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM calls WHERE at>=? ORDER BY at", (since,)).fetchall()
        totals = {}
        for row in rows:
            day = dt.datetime.fromtimestamp(row["at"], zone).date().isoformat()
            key = (day, row["feature"], row["model"], row["endpoint"])
            item = totals.setdefault(key, {"day": day, "feature": row["feature"], "model": row["model"], "endpoint": row["endpoint"],
                                         "calls": 0, "success": 0, "failed": 0, "pending": 0, "tokens_in": 0, "tokens_out": 0, "cached": 0})
            item["calls"] += 1
            item["pending" if row["success"] is None else "success" if row["success"] else "failed"] += 1
            for field in ("tokens_in", "tokens_out", "cached"):
                item[field] += row[field] or 0
        return {"timezone": timezone, "rows": list(totals.values())[::-1], "estimated_cost": None}

    def call_start(self, feature, model, endpoint):
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO calls(id,at,feature,model,endpoint) VALUES (?,?,?,?,?)", (ident, time.time(), feature, model, endpoint))
        return ident

    def call_end(self, ident, success, elapsed, usage=None, error=""):
        usage = usage or {}
        cached = usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        with self.connect() as db:
            db.execute("UPDATE calls SET success=?,duration=?,tokens_in=?,tokens_out=?,cached=?,error=? WHERE id=?",
                       (int(success), elapsed, usage.get("prompt_tokens") or usage.get("input_tokens"),
                        usage.get("completion_tokens") or usage.get("output_tokens"), cached, error[:80], ident))

    def counts(self):
        with self.connect() as db:
            return {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in ("events", "notes", "calls", "reminders", "tombstones")}

    def integrity(self):
        with self.connect() as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError("数据库完整性检查失败")
        return self.counts()
