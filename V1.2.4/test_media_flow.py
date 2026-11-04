# -*- coding: utf-8 -*-
"""图片使用普通回复判断，不再受活跃窗口限制；不联网、不发送真实消息。"""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import conversation as C
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wx_chat_loop as L
import wx_multi as M


def main():
    with tempfile.TemporaryDirectory() as folder:
        M.DONE_FILE = str(Path(folder) / "done.json")
        M.TUNE.apply = lambda *a: None
        M.SKILL.recover = lambda: None
        M.SKILL.completion_notices = lambda *a: []
        M.SKILL.is_command = lambda *a: False
        M.L.in_curfew = lambda *a: False
        M.L.mute_left = lambda *a: 0
        M.L.open_db = lambda *a: None
        M.L.my_name = lambda *a: "大肥鱼"
        M.load_rooms = lambda *a: [{"name":"群", "room":"group@chatroom", "account":"self"}]
        M.curfew_beat = lambda *a: None
        M.idle_topic_beat = lambda *a: None
        M.meal_beat = lambda *a: None
        M.mute_heads_up = lambda *a: None
        M.pat_beat = lambda *a: None
        M.time.sleep = lambda *a: None
        M.L.describe_media = lambda *a: "一只可爱小猫"
        M.L.media_is_self = lambda *a: False
        C.Coalescer.ready = lambda *a: True
        def generate(*a, **kw):
            C.RESULT.set({"relation": "new", "topic": "图片讨论", "pending": [],
                          "reply": related, "emotion": False, "self_related": related})
            return ["小猫真可爱"] if related else []
        M.L.gen = generate
        M.sticker_args = lambda *a: (None, False)
        sent = []
        M.send_lines = lambda room, acct, lines, *a, **kw: (sent.extend(lines) or len(lines))
        for related in (True, False):
            C.STATE_FILE = Path(folder) / ("topics_%s.json" % related)
            L.chat_json = lambda *a, **kw: {"relation": "new", "topic": "图片讨论", "pending": [],
                                           "reply": related, "emotion": False, "self_related": related}
            Path(M.DONE_FILE).unlink(missing_ok=True)
            sent.clear()
            now = datetime.datetime.now()
            rows = [("12:00", "成员", "<图片>", "person", now)]
            L.transcript = lambda *a, **kw: (rows, [{"create_time":int(now.timestamp())}])
            L.should_reply = lambda *a: (False, "无点名")
            L.relates_to_recent = lambda *a: (related, "模拟相关性")
            sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
            M.main()
            assert bool(sent) == related, sent
    print("PASS: relevant picture replies without active window; unrelated picture skipped")


if __name__ == "__main__":
    main()
