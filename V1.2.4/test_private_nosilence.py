# -*- coding: utf-8 -*-
"""私聊不做随机静默（用户 m09368）：python test_private_nosilence.py"""
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L
import wx_multi as M

ME = "wxid_SELF"
PRIV = "wxid_OWNER"
_sent = []
fails = []


def row(who, text, seconds_ago=5):
    sender = ME if who == "我" else who
    when = datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)
    return ("[12:00]", who, text, sender, when)


def run():
    _sent.clear()
    M.DONE_FILE = os.path.join(tempfile.gettempdir(), "done_priv.json")
    for f in (M.DONE_FILE,):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    def fake_send(room, acct, lines, bypass, tag, bypass_curfew=False, **kw):
        _sent.extend(lines)
        return len(lines)

    M.TUNE.apply = lambda *a, **k: None
    M.send_lines = fake_send
    L.gen = lambda r, is_group=True, todo=None, sticker_p=None, sticker_hot=False: "私聊回一句"
    L.transcript = lambda db, room, me, n=50: ([row("威", "在吗")], [])
    L.should_reply = lambda r, name, is_private=False: (True, "私聊")
    L.relates_to_recent = lambda mine, body, max_n=5: (True, "stub")
    L.in_curfew = lambda now=None: False
    M.load_rooms = lambda p: [{"name": "威", "room": PRIV, "account": ME, "enabled": True}]
    L.open_db = lambda a: None
    L.my_name = lambda db, a: "鱼鱼"
    L.mute_left = lambda room=None: 0
    sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
    M.main()
    return list(_sent)


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def main():
    for i in range(3):
        check("私聊正常回复%d" % i, bool(run()))

    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
