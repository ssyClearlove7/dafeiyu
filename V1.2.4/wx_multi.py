# -*- coding: utf-8 -*-
"""多会话自动回复：同时盯若干个私聊/群，各自判断要不要接、各自出稿发出。

回复语义：私聊按未回复消息处理；群聊短时合并，统一判断参与、更新话题并出稿，
只回应最终仍需处理的问题，不补发已取消、完成或换题的旧请求。

用法:
  python wx_multi.py --rooms rooms.json --rounds 999999 --gap 2
"""
import argparse
import datetime
import json
import os
import random
import subprocess
import sys
import time

# 控制台可能是 GBK（Start-Process 拉起来时尤其容易），打不出 🐳 会直接崩，先统一成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wx_chat_loop as L  # noqa: E402
import hook_send as HS  # noqa: E402   hook 直发通道（含发图）
import sticker_skill as SKILL
import self_repair as REPAIR
import sticker_lib as STICKER  # noqa: E402  表情包库
import tuning as TUNE  # noqa: E402  面板可调参数（tuning.json）
import conversation as C
import rule_teaching as TEACH
import task_store as TASK
from native_wechat import NativeNotReadyError

SEND = os.path.join(HERE, "send_guard.py")

# 非管理员想让我闭嘴时的固定回击（不含换行，一条消息发完）
MUTE_TAUNT = "诶诶杂鱼，就你还想让我闭嘴(｀∀´)Ψ ざぁこ♡"

MAX_LINES = 4          # 一次回答最多发几条（用户 m04375：一次回应最多 4 句）
LINE_GAP_MIN, LINE_GAP_MAX = 2.0, 4.0   # 同一轮里两条消息之间的间隔（用户 m04417：2~4 秒随机）
PENDING_AGE = 180       # 只追最近 3 分钟还没回的，更早的算翻篇（用户 m03356）
REPLY_MODE = "活跃参与"
DONE_FILE = os.path.join(HERE, "pending_done.json")

# ---- 主动起话题 / 饭点报菜单（用户 m06545）----
IDLE_TOPIC_AFTER = 3600      # 群里静 1 小时后才开始考虑（用户 m06842）
IDLE_TOPIC_EVERY = 1800      # 之后每 30 分钟掷一次（用户 m07464）
IDLE_TOPIC_P = 0.10          # 掷中的概率 10%
IDLE_FILE = os.path.join(HERE, "idle_topic.json")
MEAL_WINDOWS = (("11:30", "12:30"), ("17:00", "19:00"))   # 两个饭点
MEAL_RECENT = 1200           # 饭点前 20 分钟内有消息才发（用户 m07464）
MEAL_FILE = os.path.join(HERE, "meal_beat.json")

# 表情包由模型按语境判断要不要配一张。
STICKER_CONTEXT = True

# 图/表情里画的是我自己时，可按配置概率回一张表情包。
SELF_STICKER_P = 0.40
SELF_STICKER_HINT = "大肥鱼 鲸鱼 女仆 本鱼 自己 撒娇 反驳"

# 宵禁开始/结束各发一次播报（用户 m04548）：{"night": "2026-10-22", "morning": "2026-10-22"}
CURFEW_STATE = os.path.join(HERE, "curfew_state.json")
CURFEW_WINDOW = 90    # 只在宵禁开始/结束后 90 分钟内发（单位：分钟），深夜/白天重启不会突兀地补播


def load_curfew_state():
    try:
        return json.load(open(CURFEW_STATE, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_curfew_state(state):
    try:
        with open(CURFEW_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass






def load_done():
    try:
        data = json.load(open(DONE_FILE, encoding="utf-8-sig"))
    except Exception:
        return {}
    out = {}
    for room, items in data.items():
        s = set()
        for it in items or []:
            try:
                s.add((int(it[0]), str(it[1])))
            except Exception:
                continue
        out[room] = s
    return out


def save_done(done):
    data = {}
    for room, s in done.items():
        keep = sorted(s)[-200:]
        data[room] = [[t, who] for (t, who) in keep]
    try:
        with open(DONE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def recent_has_sticker(rows, me):
    """最近媒体上下文里，别人是否发过表情包。"""
    now = time.time()
    span = L.MEDIA_MAX_AGE
    for r in rows:
        try:
            ts = r[4].timestamp()
        except Exception:
            continue
        if now - ts > span or r[3] == me:
            continue
        body = str(r[2])
        if body.startswith("<表情>") or body.startswith("（表情）"):
            return True
    return False


def load_rooms(path):
    """rooms.json 的 enabled=true 就是白名单（2026-10-25 m09032 合并）。

    以前是 rooms.json ∩ whitelist.json 两份都要，用户嫌麻烦，现在只认 rooms.json；
    whitelist.json 退化成自动生成的镜像（给 send_guard / broadcast / doctor 兜底用）。
    """
    data = json.load(open(path, encoding="utf-8-sig"))
    out = []
    for r in data:
        if not r.get("enabled", True):
            continue
        out.append(r)
    try:                                   # 顺手刷新镜像，保证三个脚本看到的一致
        with open(os.path.join(HERE, "whitelist.json"), "w", encoding="utf-8") as f:
            json.dump([r["room"] for r in out], f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        pass
    return out


def mute_heads_up(db, room, acct, done, scan=10):
    """每轮都扫最近 scan 条里的 `/闭嘴`（m04751）。

    为什么不是只看最后一条：我先回别人的话、管理员那条 `/闭嘴` 落在我的回复之前，
    下一轮它就不再是「我最后一条消息之后」的欠账，永远吃不到（19:20 群里实测）。
    这里同时处理两件事：
      - 管理员写了分钟数 → 立刻 set_mute（群里只停本群、私聊按 scope_for）
      - 路人提到 `/闭嘴` → 回固定台词（静音期间也要能回，用户 m04067）
    返回 True 表示这一轮改了静音状态。
    """
    try:
        rows, _ = L.transcript(db, room, acct, scan)
    except Exception as e:
        print("  扫 /闭嘴 读失败 %s: %s" % (room[:12], str(e)[:40]), flush=True)
        return False
    if not rows:
        return False
    dset = done.setdefault(room, set())
    changed = False
    now = time.time()
    for row in rows:
        if row[3] == acct or not L.has_mute_cmd(row[2]):
            continue
        tkey = (int(row[4].timestamp()), row[3])
        if tkey in dset:
            continue
        if now - row[4].timestamp() > 120:     # 两分钟前的命令不再补执行（防止重启后翻旧账）
            dset.add(tkey)
            continue
        mins = L.find_mute(row[2])
        is_admin = row[3] in L.load_admins()
        if is_admin and mins is not None:
            scope = L.scope_for(room)
            if mins <= 0:
                prev = mute_window(room)          # `/闭嘴 0` 会把窗口清掉，先记着
                if prev:                          # 手动解除：窗口就地结在这一刻
                    MUTE_LAST[room] = (prev[0], min(prev[1], time.time() - 1))
            L.set_mute(mins, scope=scope, by=row[3])
            if mins <= 0:
                print("  %s 管理员 /闭嘴 0，解除静音（作用域 %s）" % (room[:12], scope), flush=True)
            else:
                print("  %s 管理员 /闭嘴 %d，静音刷新（作用域 %s）"
                      % (room[:12], mins, scope), flush=True)
            dset.add(tkey)
            save_done(done)
            changed = True
        elif not is_admin:
            if L.mute_left(room) > 0:
                # 静音期间不回杂鱼台词（用户 m07608）；只记一笔，免得解除后又补发
                dset.add(tkey)
                continue
            print("  %s 非管理员提到 /闭嘴 → 回固定台词" % room[:12], flush=True)
            try:
                send_lines(room, acct, [MUTE_TAUNT], True, "taunt")
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            dset.add(tkey)
            save_done(done)
    return changed


MUTE_PREV = {}          # room -> 上一轮是否在静音（用来判断「刚解除」）
MUTE_FIRED = set()      # (room, until) 已经冒过话的静音段
MUTE_LAST = {}          # room -> 上一段静音窗口（`/闭嘴 0` 会把 mute.json 清空，得先记着）

PAT_REPLY_P = 1.0      # 被拍就回（用户 m06588）        # 被拍一拍之后，只有 20% 概率回一句（用户 m05541）
PAT_LAST = {}
PAT_STREAK = {}
PAT_OPEN = {}            # room -> 已经放回正常流程、还没回完的那一拍（防重复）          # room -> 最近 10 分钟被拍了几下（拍多了会急眼，m06588）           # room -> 上次回拍的时间（只用于日志）


def pat_beat(db, room, acct, done, scan=8):
    """别人拍我一拍：被拍就回一句（交给正常出稿流程），其余直接标记已处理。

    不主动发「（拍了拍X）」：Windows 微信 4.x 客户端没有拍一拍入口，hook 也没有这个接口，
    发不出真正的拍一拍，所以干脆只按概率回话（用户 m05541）。
    """
    if L.mute_left(room) > 0 or L.in_curfew():
        return False
    try:
        msgs, _ = db.get_messages(room, limit=scan, offset=0)
    except Exception:
        return False
    dset = done.setdefault(room, set())
    now = time.time()
    fresh = []          # [(ts, 谁, tkey)] 还没处理过的「拍我」
    for m in msgs:
        body = L.decode_content(m.get("message_content") or "")
        info = L.pat_info(body, acct)
        if not info:
            continue
        ts = int(m.get("create_time") or 0)
        tkey = (ts, info["from"])
        if tkey in dset:
            continue
        fresh.append((ts, info["from"], tkey))
    if not fresh:
        return False
    fresh.sort()
    ts, frm, tkey = fresh[-1]          # 一次只处理最新那一拍，旧的直接作废
    if PAT_OPEN.get(room) == tkey:     # 这条已经放回去了，等正常流程回完，别重复放
        return False
    for _ts, _frm, _k in fresh:
        dset.add(_k)
    save_done(done)
    if now - ts > 600:                 # 十几分钟前的拍别补
        return False
    if random.random() >= PAT_REPLY_P:
        return False
    try:
        PAT_STREAK[room] = len([1 for m in msgs
                                 if now - int(m.get("create_time") or 0) <= 600
                                 and L.pat_info(L.decode_content(m.get("message_content") or ""), acct)])
    except Exception:
        PAT_STREAK[room] = 1
    dset.discard(tkey)                 # 抽中 → 留给下面的正常流程回一句
    PAT_OPEN[room] = tkey
    save_done(done)
    PAT_LAST[room] = now
    print("  %s 被 %s 拍了 → 回一句" % (room, frm), flush=True)
    return True


def mute_window(room):
    """这次静音的开始/结束时间；读原始 mute.json（过期条目也还留在里面）。"""
    try:
        d = json.load(open(L.MUTE_FILE, encoding="utf-8-sig"))
    except Exception:
        return None
    scopes = d.get("scopes") or {}
    until = 0.0
    for k, v in scopes.items():
        if not (k == "all" or k == room or k.startswith("all_except:")):
            continue
        try:
            u = float(v.get("until", 0) if isinstance(v, dict) else v)
        except Exception:
            continue
        until = max(until, u)
    if until <= 0:
        return None
    try:
        mins = float(d.get("minutes") or 0)
    except Exception:
        mins = 0
    start = until - mins * 60 if mins > 0 else until - 300
    return start, until


def mute_end_beat(db, room, acct, name):
    """静音刚解除：这段时间里有人说话 → 冒一句「憋死我了」（用户 m04866）。"""
    now = time.time()
    win = mute_window(room)
    if win:
        start, until = win
        if now < until + 1:
            return                              # 其实还在静音
    else:
        # `/闭嘴 0` 会把 mute.json 清空，窗口只能靠静音期间记下的那份
        win = MUTE_LAST.get(room)
        if not win:
            return
        start, until = win
        until = min(until, now - 1)              # 被手动解除：就当刚刚结束
    MUTE_LAST.pop(room, None)
    if now - until > 120:
        return                                  # 过期太久（重启/睡过去了）不补
    if (room, int(until)) in MUTE_FIRED:
        return                                  # 这一段已经冒过
    try:
        rows, _ = L.transcript(db, room, acct, 20)
    except Exception:
        return
    if not rows:
        return
    spoke = [r for r in rows if r[3] != acct and r[4].timestamp() > start]
    if not spoke:
        MUTE_FIRED.add((room, int(until)))       # 期间没人说话就不吭声
        print("  %s 静音解除，期间没人说话 → 不冒" % name, flush=True)
        return
    lines = L.gen_mute_end(rows, not room.endswith("@chatroom"))
    if not lines:
        return
    MUTE_FIRED.add((room, int(until)))
    print("  %s 静音解除（期间 %d 条新消息）→ 冒一句：%s"
          % (name, len(spoke), " / ".join(lines)), flush=True)
    send_lines(room, acct, lines[:1], False, "mute_end")


def admin_private_bypass(db, room, acct):
    """静音期间，管理员在**自己的私聊**里说话照样处理。"""
    if room.endswith("@chatroom"):
        return False
    try:
        rows, _ = L.transcript(db, room, acct, 3)
    except Exception:
        return False
    if not rows:
        return False
    last = rows[-1]
    if last[3] == acct:
        return False
    return last[3] == room and last[3] in L.load_admins()


def curfew_beat(rooms, dbs, state, i, now=None):
    """每天多段宵禁的联合开始/结束边界各播报一次；未设置时不播报。

    - 只发群聊（`@chatroom`），私聊不打扰
    - 内容由各群最近 14 条上下文生成（`L.gen_curfew`）
    - 发送要穿过 send_guard 的宵禁 + 静音闸（bypass_curfew / bypass_mute）
    """
    now = now or datetime.datetime.now()
    boundaries = set()
    for period in L.curfew_periods():
        for offset in (-1, 0):
            day = now.date() + datetime.timedelta(days=offset)
            start = datetime.datetime.combine(day, datetime.time.fromisoformat(period["start"]))
            end = datetime.datetime.combine(day, datetime.time.fromisoformat(period["end"]))
            if end <= start:
                end += datetime.timedelta(days=1)
            boundaries.update((start, end))
    night_now = L.in_curfew(now)
    candidates = []
    for boundary in boundaries:
        if not 0 <= (now - boundary).total_seconds() < CURFEW_WINDOW * 60:
            continue
        before = L.in_curfew(boundary - datetime.timedelta(microseconds=1))
        after = L.in_curfew(boundary)
        if before != after and after == night_now:
            candidates.append(boundary)
    if not candidates:
        return 0
    event = max(candidates)
    event_id = event.isoformat(timespec="minutes")
    if state.get("event") == event_id:
        return 0
    kind = "night" if night_now else "morning"

    groups = [r for r in rooms if r["room"].endswith("@chatroom")]
    ok_all = 0
    for r in groups:
        room, acct = r["room"], r["account"]
        if L.mute_left(room) > 0:
            # 群被 /闭嘴 了就不播报晚安/早安（用户 m07608）
            print("宵禁播报跳过 %s（群在静音中）" % r["name"], flush=True)
            continue
        try:
            rows, _raw = L.transcript(dbs[acct], room, acct, n=14)
        except Exception as e:
            print("宵禁播报读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
            continue
        if not rows:
            continue
        draft = L.gen_curfew(rows, night=(kind == "night"))
        lines = [x.strip() for x in (draft or "").splitlines() if x.strip()][:2]
        if not lines:
            print("宵禁播报出稿失败 %s，跳过" % r["name"], flush=True)
            continue
        print("[%d] %s %s: %s" % (i, r["name"], "开始休息" if kind == "night" else "恢复聊天",
                                  " / ".join(lines)), flush=True)
        try:
            n_ok = send_lines(room, acct, lines, True, "curfew", bypass_curfew=True)
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            raise
        if n_ok:
            ok_all += 1
        time.sleep(random.uniform(LINE_GAP_MIN, LINE_GAP_MAX))
    state["event"] = event_id
    save_curfew_state(state)
    print("宵禁播报（%s）完成 %d/%d 个群" % ("开始" if kind == "night" else "结束", ok_all, len(groups)),
          flush=True)
    return ok_all


def load_kv(path):
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return {}


def save_kv(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _as_lines(draft, max_n=2):
    if isinstance(draft, (list, tuple)):
        out = [str(x).strip() for x in draft if str(x).strip()]
    else:
        out = [x.strip() for x in (draft or "").splitlines() if x.strip()]
    return out[:max_n]


def last_msg_ts(rows):
    """最近一条消息（含自己）的时间戳。"""
    best = 0.0
    for r in rows:
        try:
            best = max(best, r[4].timestamp())
        except Exception:
            pass
    return best


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def idle_topic_beat(rooms, dbs, state, i, now=None):
    """群聊静 1 小时后，每 10 分钟掷 10% 主动抛个话题（用户 m06545 / m06842，只群聊）。"""
    if REPLY_MODE != "活跃参与":
        return 0
    now = now or datetime.datetime.now()
    if L.in_curfew(now):
        return 0
    now_ts = now.timestamp()
    sent = 0
    for r in rooms:
        room, acct = r["room"], r["account"]
        if not room.endswith("@chatroom"):
            continue
        if L.mute_left(room) > 0:
            continue        # 群被 /闭嘴 了就不主动抛话题（用户 m07583）
        try:
            rows, _raw = L.transcript(dbs[acct], room, acct, n=6)
        except Exception:
            continue
        last = last_msg_ts(rows)
        if not last:
            continue
        if now_ts - last < IDLE_TOPIC_AFTER:
            if state.pop(room, None) is not None:
                save_kv(IDLE_FILE, state)
            continue
        if now_ts - float(state.get(room, 0) or 0) < IDLE_TOPIC_EVERY:
            continue
        state[room] = now_ts
        save_kv(IDLE_FILE, state)
        if random.random() >= IDLE_TOPIC_P:
            print("[%d] %s 静了 %d 分钟，没抽中 10%%"
                  % (i, r["name"], int((now_ts - last) // 60)), flush=True)
            continue
        try:
            draft = L.gen_topic(rows)
        except Exception as e:
            print("起话题出稿失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
            continue
        lines = _as_lines(draft)
        if not lines:
            continue
        print("[%d] %s 静了 %d 分钟，抽中 10%% → 抛话题: %s"
              % (i, r["name"], int((now_ts - last) // 60), " / ".join(lines)), flush=True)
        try:
            n_ok = send_lines(room, acct, lines, False, "idle")
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            raise
        if n_ok:
            sent += 1
    return sent


def meal_beat(rooms, dbs, state, i, now=None):
    """饭点（11:30-12:30 / 17:00-19:00）随机挑一个时间点说今天吃了什么（用户 m06545）。

    到点了再看这个群最近 20 分钟有没有消息，没有就不发（用户 m07464）。
    """
    if REPLY_MODE != "活跃参与":
        return 0
    now = now or datetime.datetime.now()
    if L.in_curfew(now):
        return 0
    mins = now.hour * 60 + now.minute
    day = now.strftime("%Y-%m-%d")
    now_ts = now.timestamp()
    sent = 0
    for idx, (a, b) in enumerate(MEAL_WINDOWS):
        s, e = _mins(a), _mins(b)
        if not (s <= mins < e):
            continue
        for r in rooms:
            room, acct = r["room"], r["account"]
            if not room.endswith("@chatroom"):
                continue
            if L.mute_left(room) > 0:
                continue    # 群被 /闭嘴 了就不报菜单（用户 m07583）
            key = "%s|%d|%s" % (room, idx, day)
            rec = state.get(key)
            if rec is None:
                base = now.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0).timestamp()
                pick = base + random.randint(0, (e - s) * 60 - 1)
                rec = {"at": day, "pick": pick, "sent": False}
                state[key] = rec
                save_kv(MEAL_FILE, state)
                print("[%d] %s 饭点 %s-%s 挑了 %s 发"
                      % (i, r["name"], a, b, time.strftime("%H:%M", time.localtime(pick))), flush=True)
            if rec.get("sent") or now_ts < float(rec.get("pick") or 0):
                continue
            try:
                rows, _raw = L.transcript(dbs[acct], room, acct, n=20)
            except Exception:
                continue
            if now_ts - last_msg_ts(rows) > MEAL_RECENT:
                rec["sent"] = True
                save_kv(MEAL_FILE, state)
                print("[%d] %s 到饭点了，但最近半小时没消息 → 不发" % (i, r["name"]), flush=True)
                continue
            try:
                draft = L.gen_meal(rows)
            except Exception as e:
                print("饭点出稿失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
                continue
            lines = _as_lines(draft)
            if not lines:
                continue
            print("[%d] %s 饭点报菜单: %s" % (i, r["name"], " / ".join(lines)), flush=True)
            try:
                n_ok = send_lines(room, acct, lines, False, "meal")
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                raise
            if n_ok:
                rec["sent"] = True
                save_kv(MEAL_FILE, state)
                sent += 1
    return sent


def send_sticker(room, hint):
    """按当前话题/关键词从库里挑一张表情包发出去（只走 hook，不动界面）。"""
    path = STICKER.pick(hint or "")
    if not path:
        print("    表情包：库里没挑到（%s）" % (hint or "").strip()[:24], flush=True)
        return False
    time.sleep(random.uniform(1.0, 2.0))     # 文字发完停一下再补图，像人
    ok, info = HS.send_image(room, path)
    if ok:
        STICKER.mark_used(os.path.basename(path))
        print("    表情包: %s 已发（hook）" % os.path.basename(path), flush=True)
        return True
    print("    表情包发送失败: %s" % (info,), flush=True)
    return False


def send_lines(room, acct, lines, bypass_mute, tag, bypass_curfew=False, voice_style=None):
    """逐条发；返回发成功的条数。rc=9 直接抛出去（消息发错会话，必须停机）。"""
    env = dict(os.environ)
    if voice_style:
        env["WX_VOICE_STYLE"] = voice_style
        lines = ["，".join(lines)]
    else:
        env.pop("WX_VOICE_STYLE", None)
    if bypass_mute:
        env["WX_BYPASS_MUTE"] = "1"     # 管理员私聊的豁免要一路传到发送器，否则被 rc=8 挡住
    if bypass_curfew:
        env["WX_BYPASS_CURFEW"] = "1"   # 宵禁播报要能穿过 send_guard 的宵禁检查
    ok_any = 0
    for idx, one in enumerate(lines):
        if idx > 0:
            env["WX_TRUST_ROOM"] = room
            env["WX_TRUST_UNTIL"] = str(time.time() + 25)
        p = subprocess.run([sys.executable, SEND, room, acct, one],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", env=env)
        out = (p.stdout or "").strip().replace("\n", " ")[:60]
        print("    %s[%d] rc=%s %s" % (tag, idx, p.returncode, out), flush=True)
        if p.returncode not in (0, 4):
            # 非正常返回时把 stderr 也打出来（rc=1 常见是 send_guard 自己崩了）
            err = (p.stderr or "").strip().replace("\n", " ")[:300]
            if err:
                print("      stderr: %s" % err, flush=True)
        if p.returncode == 9:
            raise RuntimeError("发到别的会话了")
        if p.returncode in (0, 4):
            ok_any += 1
            env["WX_TRUST_ROOM"] = room
            env["WX_TRUST_UNTIL"] = str(time.time() + 25)
        elif p.returncode == 3:
            print("    跳过了：证明不了当前会话就是它", flush=True)
            break
        if idx < len(lines) - 1:
            time.sleep(random.uniform(LINE_GAP_MIN, LINE_GAP_MAX))   # 2~4 秒随机（m04417）
    return ok_any


def sticker_args(rows_ctx, acct, txt):
    """这一条回复要不要配表情包 → (sticker_p, sticker_hot)。

    拍一拍一律不配（用户 m06805）。其余交给模型按语境判断（用户 m07954）：
    sticker_p=None 表示「允许，但由模型决定」；sticker_hot=True 表示近期上下文里
    别人刚发过表情包，可以顺着接一张。
    """
    if "拍了拍" in str(txt):
        return 0.0, False
    return None, recent_has_sticker(rows_ctx, acct)


def write_heartbeat(round_number=None):
    try:
        with open(os.path.join(HERE, "bot_heartbeat.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "pid": os.getpid(), "round": round_number}, f)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rooms", default=os.path.join(HERE, "rooms.json"))
    ap.add_argument("--rounds", type=int, default=999999)
    ap.add_argument("--gap", type=float, default=2.0)
    args = ap.parse_args()

    import memory_store
    memory_store.start()
    SKILL.recover()
    rooms = load_rooms(args.rooms)
    if not rooms:
        print("还没有启用任何会话（rooms.json 是空的）。", flush=True)
        print("→ 打开面板 http://127.0.0.1:8790 的「名单」页添加，或直接编辑 rooms.json", flush=True)
        print("→ 这里每 10 秒重读一次名单，配置好会自动开始；按 Ctrl+C 退出", flush=True)
        while not rooms:
            write_heartbeat()
            time.sleep(10)
            rooms = load_rooms(args.rooms)
        print("名单读到了：%d 个会话，开始运行" % len(rooms), flush=True)
    dbs = {}
    names = {}
    for r in rooms:
        if r["account"] not in dbs:
            while True:
                try:
                    dbs[r["account"]] = L.open_db(r["account"])
                    break
                except NativeNotReadyError as e:
                    print("等待微信就绪：%s" % e, flush=True)
                    write_heartbeat()
                    time.sleep(3)
            names[r["account"]] = L.my_name(dbs[r["account"]], r["account"])
            print("本号昵称:", names[r["account"]] or "(未知)", flush=True)
        print("盯:", r["name"], r["room"], flush=True)

    done = load_done()      # room -> {(ts, 发送者)} 已处理（回过 / 判过不回 / 太老）
    topics = C.Topics()
    coalescer = C.Coalescer()
    _bypass_log = {}        # room -> 上次打「管理员私聊豁免」日志的时间（防刷屏）
    curfew_state = load_curfew_state()   # 最近一次已播报的联合时段边界
    idle_state = load_kv(IDLE_FILE)      # room -> 上次掷话题的时间（m06545）
    meal_state = load_kv(MEAL_FILE)      # 饭点挑的时间点 / 是否已发（m06545）

    for i in range(args.rounds):
        L.refresh_persona()
        # 面板改的参数：写进 tuning.json，这里每轮贴一次（mtime 没变就是空操作）
        TUNE.apply(sys.modules[__name__])
        TUNE.apply(L)
        TEACH.apply(sys.modules[__name__])
        TEACH.apply(L)
        write_heartbeat(i)
        # 面板改名单后不用重启：每轮重读一次 rooms.json（m09094）
        try:
            _rooms = load_rooms(args.rooms)
            if _rooms:
                if [r["room"] for r in _rooms] != [r["room"] for r in rooms]:
                    print("名单有变化 → 现在盯 %d 个会话" % len(_rooms), flush=True)
                rooms = _rooms
                for _r in rooms:
                    if _r["account"] not in dbs:
                        dbs[_r["account"]] = L.open_db(_r["account"])
                        names[_r["account"]] = L.my_name(dbs[_r["account"]], _r["account"])
        except Exception as _e:
            print("重读名单失败（沿用上一份）:", _e, flush=True)

        enabled_keys = {(r["account"], r["room"]) for r in rooms}
        coalescer.waiting = {key: value for key, value in coalescer.waiting.items() if key in enabled_keys}

        try:
            try:
                TASK.beat(load_rooms(args.rooms), send_lines)
            except RuntimeError:
                raise
            except Exception as exc:
                print("提醒调度待重试：%s" % type(exc).__name__, flush=True)
            for job, notice in SKILL.completion_notices(rooms):
                if send_lines(job["room"], job["account"], [notice], False, "skill"):
                    SKILL.mark_notice_sent(job["id"])
            for job, notice in REPAIR.completion_notices(rooms):
                if send_lines(job["room"], job["account"], [notice], False, "repair"):
                    REPAIR.mark_notice_sent(job["id"])
            curfew_beat(rooms, dbs, curfew_state, i)   # 宵禁时段开始/结束播报
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            return 9
        try:
            idle_topic_beat(rooms, dbs, idle_state, i)   # 群里静太久 → 抛话题（m06545）
            meal_beat(rooms, dbs, meal_state, i)         # 饭点报菜单（m06545）
        except RuntimeError:
            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
            return 9
        if L.in_curfew():
            if i % 30 == 0:
                print("宵禁时间段内，不读不回，等待时段结束", flush=True)
            time.sleep(max(args.gap, 0.3))
            continue
        for r in rooms:
            room, acct = r["room"], r["account"]
            try:
                mute_heads_up(dbs[acct], room, acct, done)   # 每轮都扫最近的 /闭嘴（m04751）
                pat_beat(dbs[acct], room, acct, done)        # 被拍一拍 → 拍回去（m05350）
            except RuntimeError:
                print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                return 9
            left = L.mute_left(room)
            bypass_mute = False
            was_muted = MUTE_PREV.get(room)
            MUTE_PREV[room] = left > 0
            if left > 0:
                # 静音期间把窗口记下来：`/闭嘴 0` 会把 mute.json 清空，到解除时就没地方查了
                win = mute_window(room)
                if win:
                    MUTE_LAST[room] = win
            if was_muted and left <= 0:
                # 静音刚解除：这段时间里有人说话的话，冒一句「憋死我了」（用户 m04866）
                try:
                    mute_end_beat(dbs[acct], room, acct, r["name"])
                except RuntimeError:
                    print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                    return 9
                except Exception as e:
                    print("  解禁提示失败 %s: %s" % (r["name"], str(e)[:60]), flush=True)
            if left > 0:
                if left > 0 and not admin_private_bypass(dbs[acct], room, acct):
                    if i % 30 == 0:
                        print("%s 静音中，还剩 %d 秒" % (r["name"], int(left)), flush=True)
                    continue
                if left > 0:
                    bypass_mute = True
                    if time.time() - _bypass_log.get(room, 0) > 60:
                        _bypass_log[room] = time.time()
                        print("%s 静音中，但管理员私聊 → 照常回" % r["name"], flush=True)
                else:
                    print("%s 静音已解除，继续" % r["name"], flush=True)
            db = dbs[acct]
            try:
                rows, raw = L.transcript(db, room, acct)
            except Exception as e:
                print("  读失败 %s: %s" % (r["name"], str(e)[:40]), flush=True)
                continue
            if not rows:
                coalescer.clear(acct, room)
                continue
            raw_by_ts = {}
            for m in (raw or []):
                try:
                    raw_by_ts[int(m.get("create_time") or 0)] = m
                except Exception:
                    pass

            pend = L.pending_after_me(rows, acct)
            if not pend:
                coalescer.clear(acct, room)
                continue                     # 我已经是最后说话的人，没欠账
            rows_ctx = list(rows)
            base = len(rows) - len(pend)
            dset = done.setdefault(room, set())
            todo = []                        # [(tkey, 谁, 内容)]
            teaching_receipts = {}
            topic_meta = {}
            acted = False
            is_group = room.endswith("@chatroom")
            if is_group:
                fresh = [row for row in pend if row[3] != acct and
                         (int(row[4].timestamp()), row[3]) not in dset and
                         time.time() - row[4].timestamp() <= PENDING_AGE]
                # 管理指令、拍一拍和点名不必等待合并；普通连续发言按会话独立收集。
                immediate = False
                for entry in fresh:
                    source = raw_by_ts.get(int(entry[4].timestamp())) or {}
                    body = L.message_request_text(source) if source else entry[2]
                    trigger = (entry[0], entry[1], body, entry[3], entry[4])
                    _, reason = L.should_reply([trigger], names.get(acct, ""))
                    if (reason in ("被@", "点名开头", "被点名") or source.get("native_mentions_self")
                            or L.has_mute_cmd(body) or "拍了拍" in entry[2]
                            or TEACH.candidate(entry[3], body)
                            or REPAIR.is_command(entry[3], body)
                            or SKILL.is_command(entry[3], body, rows, source)):
                        immediate = True
                        break
                if not immediate and not coalescer.ready(acct, room,
                        [(int(x[4].timestamp()), x[3], x[2]) for x in fresh], time.monotonic()):
                    continue
                coalescer.clear(acct, room)

            for off, row in enumerate(pend):
                j = base + off
                original_body = row[2]
                tkey = (int(row[4].timestamp()), row[3])
                if tkey in dset:
                    continue
                if time.time() - row[4].timestamp() > PENDING_AGE:
                    dset.add(tkey)           # 超过 3 分钟的翻篇，不补
                    save_done(done)
                    continue
                if row[3] == acct:
                    dset.add(tkey)
                    continue

                # 未转写成功的语音不交给模型猜测，留待下一轮或超时。
                if row[2] == "<语音>":
                    continue

                # ---- 拍一拍：只有抽中的才会走到这里（没抽中的已被 pat_beat 标记处理掉）----
                if str(row[2]).startswith("<拍"):
                    # 拍一拍消息的 sender 是空的，名字要从 patinfo 里捞（不然日志里全是 wxid）
                    raw_pat = raw_by_ts.get(int(row[4].timestamp())) or {}
                    _raw_pat_body = str(raw_pat.get("message_content") or "")
                    # 只在「拍我」时才算（用户 m09722：别人拍别人不该开窗、也不该回）
                    if not L.pat_info(L.decode_content(_raw_pat_body), acct):
                        dset.add(tkey)
                        save_done(done)
                        print("[%d] %s 这拍一拍不是拍我 → 跳过" % (i, r["name"]), flush=True)
                        continue
                    pat_wxid = L.pat_from(_raw_pat_body)
                    who = (L.contact_name(dbs[acct], pat_wxid) if pat_wxid else "") or row[1] or "有人"
                    rows_ctx[j] = (row[0], row[1], "（%s 拍了拍我）" % who, row[3], row[4])
                    streak = PAT_STREAK.get(room, 1)
                    if streak >= 3:
                        pat_txt = "拍了拍我（连着被拍 %d 下，已经有点急了）" % streak
                    elif streak == 2:
                        pat_txt = "拍了拍我（第二下）"
                    else:
                        pat_txt = "拍了拍我"
                    todo.append((tkey, who, pat_txt))
                    dset.add(tkey)           # 这里就记 done：免得下一轮又被 pat_beat 放回来
                    PAT_OPEN.pop(room, None)
                    save_done(done)
                    print("[%d] %s 被 %s 拍了，回一句" % (i, r["name"], who), flush=True)
                    continue

                # ---- 图片 / 表情：识别后按普通消息判断 ----
                if L.media_pending(row):
                    budget_before = L._budget[0]
                    desc = L.describe_media(raw_by_ts.get(int(row[4].timestamp())), room, acct)
                    if not desc:
                        if budget_before <= 0:
                            continue  # 本轮额度用尽，尚未尝试，不属于获取失败。
                        print("[%d] %s %s 获取或识别失败，停止处理本条消息"
                              % (i, r["name"], L.media_tag(row)), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        continue
                    tag = L.media_tag(row)
                    row = (row[0], row[1], "（%s）%s" % (tag, desc), row[3], row[4])
                    rows_ctx[j] = row
                    print("[%d] %s %s 识别: %s" % (i, r["name"], tag, desc[:40]), flush=True)
                    is_me = L.media_is_self(desc)
                    if is_me and not is_group:
                        if random.random() < SELF_STICKER_P:
                            if send_sticker(room, SELF_STICKER_HINT + " " + desc[:24]):
                                print("[%d] %s 图里是我 → 回一张表情包" % (i, r["name"]), flush=True)
                                acted = True
                        dset.add(tkey)
                        save_done(done)
                        continue
                    # 其他图片与文字使用相同的相关性判断。

                # ---- /闭嘴 ----
                if L.has_mute_cmd(row[2]):
                    mins = L.find_mute(row[2])
                    is_admin = row[3] in L.load_admins()
                    if is_admin and mins is not None:
                        scope = L.scope_for(room)
                        L.set_mute(mins, scope=scope, by=row[3])
                        if scope == "all":
                            where = "所有会话"
                        elif scope.startswith("all_except:"):
                            where = "除管理员私聊以外的所有会话"
                        else:
                            where = "本群"
                        print("[%d] %s 管理员 /闭嘴 %d，静音%s %d 分钟"
                              % (i, r["name"], mins, where, mins), flush=True)
                        dset.add(tkey)
                        save_done(done)
                        todo = []       # 指令一下，这一轮剩下的欠账也不再回（m04751）
                        break
                    if not is_admin:
                        print("[%d] %s 非管理员提到 /闭嘴 → 回固定台词" % (i, r["name"]), flush=True)
                        try:
                            if send_lines(room, acct, [MUTE_TAUNT], False, "taunt"):
                                acted = True
                                dset.add(tkey)
                                save_done(done)
                        except RuntimeError:
                            print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                            return 9
                        continue
                    print("[%d] %s 管理员提到 /闭嘴 但没写分钟数，当普通消息"
                          % (i, r["name"]), flush=True)

                # ---- 普通消息 ----
                message = raw_by_ts.get(tkey[0])
                request_text = L.message_request_text(message) if message else REPAIR.request_text(row[2])
                skill_text = row[2] if not message or str(message.get("local_type")) == "34" else request_text
                task_command = TASK.is_command(row[3], skill_text)
                teaching = None if task_command else TEACH.teach(row[3], room, acct, tkey[0], SKILL._request_text(skill_text), L.chat_json)
                if teaching is not None:
                    teaching_receipts[tkey] = teaching
                    todo.append((tkey, row[1], row[2]))
                    continue
                quoted_media = L.quote_media_message(message, raw) if message else None
                if quoted_media and not SKILL.quoted_delete_requested(skill_text, message):
                    budget_before = L._budget[0]
                    desc = L.describe_media(quoted_media, room, acct)
                    if not desc and budget_before <= 0:
                        continue
                    if desc:
                        ref = message["native_quote"]
                        sender = ref.get("senderId") or ""
                        ref_name = ("我" if sender == acct else L.contact_name(db, sender)) if sender else ""
                        body = L.quoted_text(ref, L.message_request_text(message), ref_name, desc)
                        row = (row[0], row[1], body, row[3], row[4])
                        rows_ctx[j] = row
                if task_command:
                    ok, why = True, "管理员任务"
                elif REPAIR.is_command(row[3], request_text):
                    ok, why = True, "管理员故障修复"
                elif SKILL.is_command(row[3], skill_text, rows_ctx[:j + 1], message):
                    ok, why = True, "管理员表情包技能"
                elif (raw_by_ts.get(tkey[0]) or {}).get("native_mentions_self"):
                    ok, why = True, "被@"
                else:
                    trigger_rows = rows_ctx[:j + 1]
                    # 图片描述或引用原文出现名字，不等于发消息的人点名叫我。
                    trigger_body = request_text if message and str(message.get("local_type")) != "34" else original_body
                    trigger_rows[-1] = (row[0], row[1], trigger_body, row[3], row[4])
                    ok, why = L.should_reply(trigger_rows, names.get(acct, ""), not is_group)
                targeted = why in ("被@", "点名开头", "被点名", "私聊", "管理员表情包技能", "管理员故障修复", "管理员任务")
                special = why in ("管理员表情包技能", "管理员故障修复", "管理员任务")
                if is_group and not special:
                    if (REPLY_MODE == "点名参与" and not targeted and
                            not topics.state(acct, room).get("topic") and
                            not any(x["direct"] for x in topic_meta.values())):
                        ok, why = False, "等待点名"
                    else:
                        topic_meta[tkey] = {"direct": targeted, "legacy": ok}
                        ok, why = True, "合并理解与出稿"
                elif why == "聊吃的":
                    # 聊吃的不走"跟前面搭不搭"那道闸：检测到好吃的直接回（用户 m04448）
                    why = "聊吃的（直接回）"
                elif not targeted:
                    mine = [x[2] for x in rows_ctx[:j + 1] if x[3] == acct][-5:]
                    rel, rwhy = L.relates_to_recent(mine, row[2])
                    if rel and ok:
                        why = "%s+接着我说的（%s）" % (why, rwhy)
                    elif rel:
                        ok, why = True, "接着我说的（%s）" % rwhy
                    else:
                        ok, why = False, "跟前面不搭，不接（%s）" % rwhy
                if not ok:
                    print("[%d] %s 跳过（%s）: %s" % (i, r["name"], why, row[2][:26]), flush=True)
                    dset.add(tkey)
                    save_done(done)
                    continue
                todo.append((tkey, row[1], row[2]))
                print("[%d] %s 待回（%s）: %s" % (i, r["name"], why, row[2][:26]), flush=True)

            if not todo:
                continue
            batches = []
            for item in todo:
                meta = topic_meta.get(item[0])
                if meta is not None and batches and batches[-1][1] is not None:
                    batches[-1][0].append(item)
                    batches[-1] = (batches[-1][0], meta)
                else:
                    batches.append(([item], meta))
            save_done(done)
            for n, (batch, topic) in enumerate(batches[-MAX_LINES:], 1):
                verdict = None
                tkey, who, txt = batch[-1]
                requests = [(item[1], item[2], item[0][0]) for item in batch]
                # 普通群聊合并理解；私聊、拍一拍和管理指令各自处理。
                sp, sh = sticker_args(rows_ctx, acct, txt)
                message = raw_by_ts.get(tkey[0])
                request_text = L.message_request_text(message) if message else REPAIR.request_text(txt)
                skill_text = txt if not message or str(message.get("local_type")) == "34" else request_text
                if tkey in teaching_receipts:
                    draft = [teaching_receipts[tkey]]
                elif REPAIR.is_command(tkey[1], request_text) and not TASK.is_command(tkey[1], skill_text):
                    draft = [REPAIR.start(tkey[1], room, acct, tkey[0], request_text)]
                else:
                    with SKILL.request_context(tkey[1], room, acct, tkey[0], skill_text, messages=raw, message=message):
                        receipt = TASK.command_reply(L.chat_json)
                        if receipt is None:
                            receipt = REPAIR.status_reply(tkey[1], room, acct, request_text, rows_ctx)
                        if receipt is None:
                            receipt = SKILL.command_reply(rows_ctx)
                        if receipt is not None:
                            draft = receipt.splitlines()
                        else:
                            combined = topic is not None
                            previous = dict(topics.state(acct, room)) if combined else None
                            merged = ({"current": previous, "mode": REPLY_MODE,
                                       "messages": [{"speaker": item[1], "sender": item[0][1],
                                                     "text": item[2], **topic_meta[item[0]]}
                                                    for item in batch]} if combined else None)
                            token = C.CONTEXT.set(previous if combined else topic)
                            batch_token = C.BATCH.set(merged)
                            result_token = C.RESULT.set(None)
                            try:
                                draft = L.gen(rows_ctx, is_group, todo=requests,
                                              sticker_p=sp, sticker_hot=sh)
                                verdict = C.RESULT.get()
                            finally:
                                C.CONTEXT.reset(token)
                                C.BATCH.reset(batch_token)
                                C.RESULT.reset(result_token)
                            if combined:
                                if verdict is None:
                                    # 模型失败或协议无效，不把消息误记为已处理。
                                    continue
                                verdict = C.review_batch(merged, verdict,
                                    [{"speaker": row[1], "text": row[2], "self": row[3] == acct}
                                     for row in rows_ctx[-18:]], L.chat_json)
                                print("    参与校验：mode=%s relation=%s self_related=%s emotion=%s reply=%s" %
                                      (REPLY_MODE, verdict["relation"], verdict["self_related"],
                                       verdict["emotion"], verdict["reply"]), flush=True)
                                topic = topics.accept(acct, room, verdict)
                                if not verdict["reply"]:
                                    for item in batch:
                                        dset.add(item[0])
                                    save_done(done)
                                    print("    合并判定：旁听，不发送", flush=True)
                                    continue
                # gen() 现在返回 list（可能最后一行是 [表情:关键词]）；老版本返回字符串，两种都认
                if isinstance(draft, (list, tuple)):
                    lines = [str(x).strip(" -·") for x in draft if str(x).strip()]
                else:
                    draft = (draft or "").strip()
                    lines = [x.strip(" -·") for x in draft.splitlines() if x.strip()]
                import wx_voice
                voice_style, lines = wx_voice.extract(lines)
                lines = [x for x in lines if x]
                if not lines:
                    continue
                # [表情:关键词] 那一行不是文字：抽出来，文字发完再补一张表情包（m05769）
                sticker_kw = None
                kept = []
                for one in lines:
                    m = L.STICKER_TAG_RE.match(one)
                    if m:
                        if sticker_kw is None:
                            sticker_kw = (m.group(1) or "").strip()
                    else:
                        kept.append(one)
                lines = kept
                if topic is not None and tkey not in teaching_receipts:
                    related = bool((verdict or {}).get("self_related")) or any(
                        topic_meta.get(item[0], {}).get("direct") for item in batch)
                    rule_rows = rows_ctx
                    if any(k in TEACH.current() for k in ("reply_every", "unrelated_idle_seconds")):
                        try:
                            rule_rows, _ = L.transcript(db, room, acct, n=max(50, TEACH.current().get("reply_every", 1) + 1))
                        except Exception as exc:
                            print("    教导限制检查失败，暂不发送：%s" % str(exc)[:100], flush=True)
                            continue
                    if not TEACH.allowed(rule_rows, acct, related,
                                         judge=None if verdict is not None else L.chat_json):
                        for item in batch:
                            dset.add(item[0])
                        save_done(done)
                        print("    admin教导限制：本轮旁听", flush=True)
                        continue
                bubble_limit = TEACH.current().get("max_lines", MAX_LINES) if tkey not in teaching_receipts else MAX_LINES
                lines = L.semantic_lines("\n".join(lines), max_lines=bubble_limit, limit=L.MAX_LINE)
                if not lines and sticker_kw is None:
                    continue
                if topic is not None:
                    try:
                        latest, _ = L.transcript(db, room, acct)
                        seen = {(r[3], r[4]) for r in rows_ctx}
                        new_rows = [r for r in latest if r[3] != acct and
                                    (r[3], r[4]) not in seen and r[4] >= rows_ctx[-1][4]]
                        needed = topics.still_needed(topic, new_rows, "\n".join(lines), L.chat_json)
                    except Exception:
                        needed = False
                    if not needed:
                        for item in batch:
                            dset.add(item[0])
                        save_done(done)
                        print("    群聊已变化，丢弃过时草稿", flush=True)
                        continue
                print("[%d] %s 回欠账 %d/%d（%s）%d 条: %s"
                      % (i, r["name"], n, len(todo), txt[:20], len(lines),
                         " / ".join(lines)), flush=True)
                n_ok = 0
                if lines:
                    try:
                        if voice_style:
                            n_ok = send_lines(room, acct, lines, bypass_mute, "send", voice_style=voice_style)
                        else:
                            n_ok = send_lines(room, acct, lines, bypass_mute, "send")
                    except RuntimeError:
                        print("!!! 消息发到了别的会话，立刻停机 !!!", flush=True)
                        return 9
                if sticker_kw is not None and (n_ok or not lines):
                    try:
                        send_sticker(room, sticker_kw + " " + txt)
                    except Exception as e:
                        print("    表情包发送失败: %s" % e, flush=True)
                if n_ok:
                    for item in batch:
                        dset.add(item[0])
                    if topic is not None:
                        topics.sent(acct, room, "\n".join(lines))
                    save_done(done)
                    acted = True
                time.sleep(0.5)
            if acted:
                continue
        time.sleep(coalescer.delay(time.monotonic(), max(args.gap, 0.3)))


if __name__ == "__main__":
    main()
