# -*- coding: utf-8 -*-
"""wx-chatbot 的可调参数（面板改这里，主循环下一轮就生效，不用重启）。

优先级：tuning.json > 代码里的默认值。
只放「随时可以改、改了立刻见效」的旋钮；会改结构的（比如换账号、换库）不放这。
"""
import datetime
import json
import os
import re
import time
from conversation import MODES

HERE = os.path.dirname(os.path.abspath(__file__))
TUNING_FILE = os.path.join(HERE, "tuning.json")

# 字段 → (默认值, 类型, 说明, 最小, 最大)
SPEC = {
    "CURFEW_PERIODS": ([], list, "宵禁时间段（24小时制 HH:MM；空数组表示无宵禁）", None, None),
    "PENDING_AGE": (180, float, "只追最近多少秒还没回的欠账", 30, 86400),
    # ---- 主动说话 ----
    "IDLE_TOPIC_AFTER": (3600, float, "群静多少秒后才考虑主动抛话题", 60, 86400),
    "IDLE_TOPIC_EVERY": (1800, float, "之后每隔多少秒掷一次", 60, 86400),
    "IDLE_TOPIC_P": (0.10, float, "抛话题概率", 0, 1),
    "MEAL_RECENT": (1200, float, "饭点前多少秒内有消息才报菜单", 0, 86400),
    "CURFEW_WINDOW": (90, float, "宵禁开始/结束后多少分钟内播报（分钟）", 0, 1440),
    # ---- 回复形态 ----
    "REPLY_MODE": ("活跃参与", str, "群聊参与方式（仅控制加入；同一话题持续参与）", None, None),
    "MAX_LINES": (4, int, "一次回应最多几条消息", 1, 10),
    "LINE_GAP_MIN": (2.0, float, "同一轮里两条消息的最小间隔（秒）", 0, 60),
    "LINE_GAP_MAX": (4.0, float, "最大间隔（秒）", 0, 120),
    "MAX_LINE": (30, int, "单条消息参考字数（完整语义允许超长）", 5, 200),
    # ---- 图片 / 表情包 ----
    "MEDIA_MAX_AGE": (300, float, "只识别多少秒内的新图", 0, 86400),
    "DESCRIBE_BUDGET": (1, int, "每轮最多现场识别几张", 0, 5),
    "SELF_STICKER_P": (0.40, float, "「图里是我」→ 回表情包的概率", 0, 1),
    "PAT_REPLY_P": (1.0, float, "被拍一拍回一句的概率", 0, 1),
}

GROUPS = [
    ("消息处理", ["PENDING_AGE"]),
    ("回复形态", ["REPLY_MODE", "MAX_LINES", "LINE_GAP_MIN", "LINE_GAP_MAX", "MAX_LINE", "PAT_REPLY_P"]),
    ("主动说话", ["IDLE_TOPIC_AFTER", "IDLE_TOPIC_EVERY", "IDLE_TOPIC_P", "MEAL_RECENT", "CURFEW_WINDOW"]),
    ("图片 / 表情包", ["MEDIA_MAX_AGE", "DESCRIBE_BUDGET", "SELF_STICKER_P"]),
]

_cache = {"mtime": 0, "data": None}


def validate_periods(value):
    if not isinstance(value, list):
        raise ValueError("宵禁时间段必须是数组")
    periods = []
    for i, period in enumerate(value, 1):
        if not isinstance(period, dict) or set(period) != {"start", "end"}:
            raise ValueError("第%d段宵禁必须提供开始和结束时间" % i)
        for key in ("start", "end"):
            if not isinstance(period[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", period[key], flags=re.ASCII):
                raise ValueError("第%d段时间须为24小时制 HH:MM（00:00—23:59）" % i)
        if period["start"] == period["end"]:
            raise ValueError("第%d段宵禁的开始和结束时间不能相同" % i)
        periods.append(dict(period))
    return periods


def load():
    """读 tuning.json；只保留认识的字段，类型按 SPEC 校验。"""
    try:
        st = os.stat(TUNING_FILE)
        if st.st_mtime == _cache["mtime"] and _cache["data"] is not None:
            return _cache["data"]
        raw = json.load(open(TUNING_FILE, encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raw = {}
        out = {}
        for k, (default, typ, *_rest) in SPEC.items():
            if k in raw:
                try:
                    out[k] = validate_periods(raw[k]) if k == "CURFEW_PERIODS" else typ(raw[k])
                    if k == "REPLY_MODE" and out[k] not in MODES:
                        out[k] = default
                except (TypeError, ValueError):
                    out[k] = default
            else:
                out[k] = default
        _cache["mtime"] = st.st_mtime
        _cache["data"] = out
        return out
    except FileNotFoundError:
        _cache["mtime"] = 0
        _cache["data"] = {k: ([] if v[1] is list else v[0]) for k, v in SPEC.items()}
        return _cache["data"]
    except (OSError, ValueError):
        if _cache["data"] is None:
            _cache["data"] = {k: v[0] for k, v in SPEC.items()}
        return _cache["data"]


def in_curfew(now=None):
    """时段按每天重复，开始包含、结束不含，重叠时取并集。"""
    now = now or datetime.datetime.now()
    hm = now.hour * 60 + now.minute
    for period in load()["CURFEW_PERIODS"]:
        sh, sm = map(int, period["start"].split(":"))
        eh, em = map(int, period["end"].split(":"))
        start, end = sh * 60 + sm, eh * 60 + em
        if (start <= hm < end) if start < end else (hm >= start or hm < end):
            return True
    return False


def save(patch):
    # 面板与聊天教导跨进程共用锁，避免同时读改写丢失其他参数。
    import persona_store
    with persona_store.locked():
        return _save(patch)


def _save(patch):
    """把 patch 合并进 tuning.json，返回实际写入的字段。"""
    if not isinstance(patch, dict):
        raise ValueError("参数必须是对象")
    periods = validate_periods(patch["CURFEW_PERIODS"]) if "CURFEW_PERIODS" in patch else None
    if "REPLY_MODE" in patch and patch["REPLY_MODE"] not in MODES:
        raise ValueError("参与方式必须为：" + "、".join(MODES))
    cur = {}
    try:
        cur = json.load(open(TUNING_FILE, encoding="utf-8-sig"))
        if not isinstance(cur, dict):
            cur = {}
    except (FileNotFoundError, OSError, ValueError):
        cur = {}
    cur = {k: v for k, v in cur.items() if k in SPEC}
    changed = []
    for k, v in (patch or {}).items():
        if k not in SPEC:
            continue
        if k == "CURFEW_PERIODS":
            cur[k] = periods
            changed.append(k)
            continue
        if k == "REPLY_MODE":
            cur[k] = v
            changed.append(k)
            continue
        default, typ, _desc, lo, hi = SPEC[k]
        try:
            val = typ(v)
        except (TypeError, ValueError):
            continue
        if val < lo:
            val = lo
        if val > hi:
            val = hi
        cur[k] = val
        changed.append(k)
    tmp = TUNING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, TUNING_FILE)
    _cache["mtime"] = 0
    return changed


def apply(module):
    """把 tuning 的值贴到 wx_multi / wx_chat_loop 这些模块的同名全局上（每轮调一次，带 mtime 缓存）。"""
    vals = load()
    touched = []
    for k, v in vals.items():
        if hasattr(module, k) and getattr(module, k) != v:
            setattr(module, k, v)
            touched.append(k)
    return touched


def describe():
    """给面板用的元信息。"""
    vals = load()
    return {
        "values": vals,
        "spec": {k: {"default": v[0], "type": v[1].__name__, "desc": v[2], "min": v[3], "max": v[4],
                     **({"options": list(MODES)} if k == "REPLY_MODE" else {})}
                 for k, v in SPEC.items()},
        "groups": [{"title": t, "keys": ks} for t, ks in GROUPS],
        "file": TUNING_FILE,
        "now": time.time(),
    }
