# -*- coding: utf-8 -*-
"""离线验证：别人拍别人不应回复；拍机器人正常回复（用户 m09722）。

跑法: python test_pat_target.py
"""
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L
import wx_multi as M

ME = "wxid_SELF_TEST"
OTHER = "wxid_example_2fdb9d41"
THIRD = "wxid_THIRD_TEST"
ROOM = "example_6c02fd38@chatroom"

_sent = []
fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def pat_xml(patted):
    return ('<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
            '<title>"%s" 拍了拍 "%s"</title><type>62</type>'
            '<patinfo><fromusername>%s</fromusername><chatusername>%s</chatusername>'
            '<pattedusername>%s</pattedusername><patsuffixversion>0</patsuffixversion>'
            '<template>"${%s}" 拍了拍 "${%s}"</template></patinfo></appmsg></msg>'
            % (OTHER, patted, OTHER, ROOM, patted, OTHER, patted))


def run(name, patted):
    _sent.clear()
    M.DONE_FILE = os.path.join(tempfile.gettempdir(), "done_pat_%s.json" % name)
    for f in (M.DONE_FILE,):
        try:
            os.remove(f)
        except (FileNotFoundError, OSError):
            pass
    now = datetime.datetime.now()
    body = pat_xml(patted)
    rows = [("[12:00]", "群成员", "<拍一拍>", "", now - datetime.timedelta(seconds=10))]
    raw = [{"create_time": int((now - datetime.timedelta(seconds=10)).timestamp()),
            "message_content": body, "local_type": "266287972401",
            "sender_username": ""}]

    def fake_send(room, acct, lines, bypass, tag, bypass_curfew=False, **kw):
        _sent.extend(lines)
        return len(lines)

    M.TUNE.apply = lambda *a, **k: None
    M.send_lines = fake_send
    M.PAT_REPLY_P = 1.0
    M.PAT_OPEN.clear()
    M.PAT_STREAK.clear()
    L.gen = lambda r, is_group=True, todo=None, sticker_p=None, sticker_hot=False: "回一句"
    L.transcript = lambda db, room, me, n=50: (rows, raw)
    L.relates_to_recent = lambda mine, body, max_n=5: (True, "stub")
    L.should_reply = lambda r, name, is_private=False: (True, "被@")
    L.in_curfew = lambda now=None: False
    L.mute_left = lambda room=None: 0
    L.my_name = lambda db, a: "鱼鱼"
    L.open_db = lambda a: None
    M.load_rooms = lambda p: [{"name": "测试群", "room": ROOM, "account": ME, "enabled": True}]
    sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
    print("\n=== %s ===" % name)
    M.main()
    return list(_sent)


def main():
    sent = run("拍别人", THIRD)
    check("① 别人拍别人 → 不回", not sent)
    sent = run("拍我", ME)
    check("② 别人拍我 → 回一句", bool(sent))
    print("\n%s（失败 %d 项）" % ("全部通过" if not fails else "有失败", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
