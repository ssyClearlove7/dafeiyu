"""记录经过限制的运维事件；不写聊天、密钥、请求体或供应商原始响应。"""
import time
from .paths import LOCK


def append(paths, summary):
    with LOCK:
        events = paths.json("logs/health.json", [])
        events.append({"at": time.time(), "summary": summary[:300]})
        paths.save("logs/health.json", events[-2000:])


def read(paths):
    return paths.json("logs/health.json", [])[-50:]
