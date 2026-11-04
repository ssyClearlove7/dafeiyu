# -*- coding: utf-8 -*-
"""离线验证「按时间先后回完欠账」的主循环逻辑（不打微信、不调 DeepSeek）。

跑法: python test_reply_flow.py
"""
import datetime
import os
import sys
import tempfile
import json
import conversation as C

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L
import wx_multi as M

ME = "wxid_SELF"
ROOM = "<群idB>@chatroom"
T0 = datetime.datetime.now() - datetime.timedelta(minutes=10)   # 全部落在 PENDING_AGE(3 分钟) 之内
_sent = []
_calls = []


def row(minute, who, text):
    sender = ME if who == "我" else who
    when = datetime.datetime.now() - datetime.timedelta(seconds=(4 - minute) * 40)  # 0~160 秒前
    return ("[18:%02d]" % minute, who, text, sender, when)


def scenario(name, rows, related=False, mode="活跃参与"):
    _sent.clear()
    _calls.clear()
    M.REPLY_MODE = mode
    C.STATE_FILE = os.path.join(tempfile.gettempdir(), "topics_test_%s.json" % name)
    try:
        os.remove(C.STATE_FILE)
    except FileNotFoundError:
        pass
    def fake_judge(system, user, **kwargs):
        payload = json.loads(user)
        if "draft" in payload:
            return {"send": True}
        text = payload["messages"][-1]["text"]
        called = payload["direct"]
        relation = "same" if payload["current"].get("topic") else "new"
        if "换题" in text: relation = "changed"
        if "定了" in text: relation = "finished"
        return {"relation": relation,
                "topic": "测试讨论", "pending": [],
                "reply": called or related or "好难受" in text or "开心死了" in text,
                "emotion": "好难受" in text or "开心死了" in text, "self_related": related}
    L.chat_json = fake_judge
    M.DONE_FILE = os.path.join(tempfile.gettempdir(), "pending_done_test_%s.json" % name)
    try:
        os.remove(M.DONE_FILE)
    except FileNotFoundError:
        pass
    def fake_send(room, acct, lines, bypass, tag, bypass_curfew=False, **kw):
        _sent.extend(lines)
        return len(lines)

    def fake_gen(r, is_group=True, todo=None, sticker_p=None, sticker_hot=False):
        assert is_group, "群聊出稿必须使用群聊人设"
        merged = C.BATCH.get()
        if merged is not None:
            # 模拟合并模型按顺序理解整批消息；正式流程不能再逐条调用chat_json。
            local = C.Topics()
            local.states = {local.key(ME, ROOM): dict(merged["current"])}
            local.save = lambda: None
            selected = []
            relation = "new"
            for item, meta in zip(todo, merged["messages"]):
                before = local.state(ME, ROOM).get("generation", 0)
                current_rows = [("", item[0], item[1], item[0], datetime.datetime.now())]
                ok, why, state = local.decide(ME, ROOM, current_rows, meta["direct"],
                                             merged["mode"], meta["legacy"] or related, fake_judge)
                relation = why.removeprefix("话题:") if why.startswith("话题:") else "same"
                if before != state.get("generation", 0):
                    selected = []
                if ok:
                    selected.append(item)
            state = local.state(ME, ROOM)
            C.RESULT.set({"relation": relation, "topic": state.get("topic", ""),
                          "pending": state.get("pending", []), "reply": bool(selected),
                          "emotion": any("好难受" in x[1] or "开心死了" in x[1] for x in selected),
                          "self_related": related})
            todo = selected
            if not todo:
                return []
        _calls.append(todo)
        if todo:
            # 每条欠账一次调用：欠账原文进回稿，方便断言「各回各的、不混」
            out = []
            for i, (_w, t, _k) in enumerate(todo):
                if "长回答" in t:          # 单条欠账拆两个气泡
                    out.append("回%d-1:%s" % (i + 1, t))
                    out.append("回%d-2:%s（接着说）" % (i + 1, t))
                else:
                    out.append("回%d:%s" % (i + 1, t))
            return "\n".join(out)
        return "回最后一条"

    M.TUNE.apply = lambda *a, **k: None   # 别让 tuning.json 覆盖测试里设的常量
    C.Coalescer.ready = lambda *a: True
    M.curfew_beat = M.idle_topic_beat = M.meal_beat = lambda *a: None
    M.send_lines = fake_send
    L.gen = fake_gen
    L.transcript = lambda db, room, me, n=50: (rows, [])
    L.relates_to_recent = lambda mine, body, max_n=5: (related, "stub-相关性")
    L.in_curfew = lambda now=None: False
    M.load_rooms = lambda p: [{"name": "测试群", "room": ROOM, "account": ME, "enabled": True}]
    L.open_db = lambda a: None
    L.my_name = lambda db, a: "鱼鱼"
    L.mute_left = lambda room=None: 0
    sys.argv = ["wx_multi.py", "--rounds", "1", "--gap", "0"]
    print("\n=== %s ===" % name)
    M.main()
    print("发出 %d 条:" % len(_sent))
    for s in _sent:
        print("   ", s)
    return list(_sent)


if __name__ == "__main__":
    os.chdir(HERE)
    # 1. 我最后发言 → 没欠账
    s1 = scenario("我最后一条", [row(0, "张三", "在吗"), row(1, "我", "在的")])
    assert s1 == [], s1

    # 2. 两条 @我 的欠账 → 各回各的（每条欠账独立出稿，允许 1~3 个气泡，但不许混在一起）
    s2 = scenario("两条欠账", [row(0, "张三", "在吗"),
                               row(1, "我", "在的"),
                               row(2, "张三", "@鱼鱼🐳 帮我看看这个"),
                               row(3, "李四", "今天好热"),
                               row(4, "李四", "@鱼鱼🐳 你也觉得热吧")])
    assert len(s2) == 2, s2
    assert "帮我看看这个" in s2[0] and "你也觉得热吧" not in s2[0], s2
    assert "你也觉得热吧" in s2[1] and "帮我看看这个" not in s2[1], s2
    assert len(_calls) == 1 and len(_calls[0]) == 2, "同话题多人问题应合并出稿"

    # 2b. 单条欠账拆成两个气泡 → 允许，且两个气泡都属于同一件事
    s2b = scenario("一条欠账拆两句", [row(0, "我", "在的"),
                                     row(1, "张三", "@鱼鱼🐳 给我长回答这个问题")])
    assert len(s2b) == 2, s2b
    assert all("长回答" in x for x in s2b), s2b

    # 3. 只有无关消息（没触发词且不相关）→ 不回
    s3 = scenario("无关消息", [row(0, "我", "我先说一句"),
                               row(1, "张三", "今天好热啊")])
    assert s3 == [], s3

    # 4. 被 @ 必回。
    s4 = scenario("被@不丢失", [row(0, "我", "在的"),
                               row(1, "张三", "@鱼鱼🐳 在吗")])
    assert len(s4) == 1 and "在吗" in s4[0], s4

    # 5. 没有活跃窗口限制，普通点名和连续对话都可以回复。
    for n in range(12):
        reply = scenario("连续点名%d" % n, [row(4, "张三", "鱼鱼，发一个新表情包出来瞅瞅")])
        assert len(reply) == 1, reply
    reply = scenario("普通连续请求", [row(0, "我", "刚更新好表情包"),
                       row(4, "张三", "发一个新表情包出来瞅瞅，看看你的表情包爱好")], related=True)
    assert len(reply) == 1, reply
    for mode in ("活跃参与", "情绪参与", "与我有关", "点名参与"):
        assert scenario("四档点名" + mode, [row(4, "甲", "@鱼鱼 帮我们看看")], mode=mode)
    assert not scenario("陈述旁听", [row(4, "甲", "今天查到成绩了")], mode="情绪参与")
    assert scenario("沮丧参与", [row(4, "甲", "准备很久还是没过，好难受")], mode="情绪参与")
    assert scenario("高兴参与", [row(4, "甲", "终于拿到offer，开心死了")], mode="情绪参与")
    assert not scenario("点名模式旁听情绪", [row(4, "甲", "好难受")], mode="点名参与")
    assert not scenario("完成取消旧回复", [row(2, "甲", "@鱼鱼 帮我们看看"),
                                         row(4, "乙", "不用了，就这样定了")], mode="点名参与")
    assert not scenario("换题取消旧回复", [row(2, "甲", "@鱼鱼 帮我们看看"),
                                         row(4, "乙", "换题，今晚玩什么")], mode="点名参与")
    print("\n全部通过")
