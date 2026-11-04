# -*- coding: utf-8 -*-
"""多段宵禁的边界、校验及播报回归；不联网、不发送真实消息。"""
import datetime
import json
import os
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tuning as T
import wx_chat_loop as L
import wx_multi as M


def at(hour, minute):
    return datetime.datetime(2026, 10, 30, hour, minute)


def main():
    with tempfile.TemporaryDirectory() as folder:
        T.TUNING_FILE = str(Path(folder) / "tuning.json")
        T._cache = {"mtime": 0, "data": None}
        M.CURFEW_STATE = str(Path(folder) / "curfew-state.json")
        assert not any(L.in_curfew(at(h, 0)) for h in range(24))
        periods = [{"start": "23:00", "end": "07:00"}, {"start": "12:00", "end": "14:00"}]
        T.save({"CURFEW_PERIODS": periods})
        for hour in range(24):
            for minute in range(60):
                hm = hour * 60 + minute
                expected = hm >= 1380 or hm < 420 or 720 <= hm < 840
                assert L.in_curfew(at(hour, minute)) == expected, (hour, minute)
        before = Path(T.TUNING_FILE).read_bytes()
        for invalid in [None, {}, [{"start":"24:00","end":"07:00"}],
                        [{"start":"7:00","end":"08:00"}], [{"start":"07:60","end":"08:00"}],
                        [{"start":"08:00","end":"08:00"}], [{"start":"08:00"}],
                        [{"start":"08:00","end":"09:00","extra":True}]]:
            try:
                T.save({"CURFEW_PERIODS": invalid, "MAX_LINES": 8})
            except ValueError:
                pass
            else:
                raise AssertionError(invalid)
            assert Path(T.TUNING_FILE).read_bytes() == before
        sent = []
        M.send_lines = lambda room, account, lines, *a, **kw: (sent.append(room) or len(lines))
        M.L.transcript = lambda *a, **kw: ([("12:00", "成员", "聊两句", "person", at(12, 0))], [])
        M.L.gen_curfew = lambda *a, **kw: "到点了，休息一下"
        M.L.mute_left = lambda *a: 0
        M.time.sleep = lambda *a: None
        rooms = [{"room":"group@chatroom", "account":"self", "name":"群"},
                 {"room":"private", "account":"self", "name":"私聊"}]
        def beat(hour, minute, state):
            return M.curfew_beat(rooms, {"self":None}, state, 0, now=at(hour, minute))
        state = {}
        assert beat(0, 10, state) == 1  # 前一天23点开始，跨过午夜仍属于同一边界。
        assert beat(0, 15, state) == 0
        assert beat(7, 10, state) == 1
        assert beat(12, 10, state) == 1
        assert beat(14, 10, state) == 1
        T.save({"CURFEW_PERIODS":[{"start":"10:00","end":"12:00"},{"start":"11:00","end":"13:00"}]})
        state = {}
        assert beat(10, 10, state) == 1
        assert beat(11, 10, state) == 0  # 重叠内边界不重复播报。
        assert beat(12, 10, state) == 0
        assert beat(13, 10, state) == 1
        T.save({"CURFEW_PERIODS":[]})
        assert not L.in_curfew(at(0, 30)) and beat(0, 40, {}) == 0
        assert all(room.endswith("@chatroom") for room in sent)
        assert not any(k in T.describe()["values"] for k in ["ACTIVE_SECS","REPLY_LOSS_P","ATTENTION_FROM"])
    print("PASS: no defaults, multi periods, all1440minutes, midnight, exact boundaries, overlapping union, invalid save rollback, clear and broadcasts")


if __name__ == "__main__":
    main()
