# -*- coding: utf-8 -*-
"""wx-chatbot 本地控制面板（只监听 127.0.0.1，无登录）。

用法:
    python panel.py            # 默认 http://127.0.0.1:8790
    python panel.py --port 8791

能做的事：
    概览   —— 机器人是否在跑 / hook 通不通 / 微信进程 / 管理员禁言与宵禁
    参数   —— tuning.json 里的旋钮，保存后主循环下一轮生效（不用重启）
    名单   —— rooms.json / whitelist.json / settings.json
    人设   —— 基础人设与演化人设在线编辑
    表情包 —— 看库、发一张、删除
    工具   —— 给某个会话发测试消息、设置/解除静音、启停机器人
    日志   —— 最近 N 行 run_bot.log
"""
from pathlib import Path
import argparse
import ctypes
import config as CFG
import io
import json
import os
import subprocess
import sys
import time
import persona_store
import admin_store

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tuning as TUNE  # noqa: E402

LOG_FILE = os.path.join(HERE, "run_bot.log")
HEART = os.path.join(HERE, "bot_heartbeat.json")


# ---------------------------------------------------------------- 小工具
def read_json(name, default):
    p = os.path.join(HERE, name)
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return default


def write_json(name, obj):
    p = os.path.join(HERE, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def read_text(name, default=""):
    try:
        return open(os.path.join(HERE, name), encoding="utf-8-sig").read()
    except (FileNotFoundError, OSError):
        return default


def write_text(name, text):
    p = os.path.join(HERE, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def tail(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except (FileNotFoundError, OSError):
        return []


def bot_state():
    """看心跳：主循环每轮写一次 bot_heartbeat.json。"""
    hb = read_json("bot_heartbeat.json", None)
    if not hb:
        return {"running": False, "age": None, "pid": None, "round": None}
    age = time.time() - float(hb.get("ts") or 0)
    from bot_running import running_pid
    live_pid = running_pid()
    return {"running": bool(live_pid and live_pid == hb.get("pid")), "age": round(age, 1), "pid": hb.get("pid"), "round": hb.get("round")}


def hook_state():
    try:
        import hook_send as HS
        ok = HS.available(timeout=2)
        prof = HS.self_profile(timeout=2) if ok else None
        return {"ok": bool(ok), "wxid": (prof or {}).get("wxid") if isinstance(prof, dict) else None}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def wechat_procs():
    if CFG.wechat_backend() == "native":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=0x08000000,
        ).stdout
        return max(0, out.count("Weixin.exe"))
    except Exception:                                          # noqa: BLE001
        return None


def mutes():
    raw = read_json("mute.json", {})
    scopes = raw.get("scopes") if isinstance(raw, dict) else None
    now = time.time()
    out = []
    for scope, info in (scopes or {}).items():
        try:
            until = float(info.get("until") or 0)
        except (TypeError, ValueError):
            continue
        if until > now:
            out.append({"scope": scope, "left": round(until - now), "by": info.get("by")})
    return out


def room_names():
    out = {}
    for r in read_json("rooms.json", []) or []:
        if isinstance(r, dict) and r.get("room"):
            out[r["room"]] = r.get("name") or r["room"]
    return out


def sticker_list():
    import sticker_lib as S
    rows = []
    for e in S.entries():
        p = os.path.join(S.LIB, e.get("file") or "")
        rows.append({
            "file": e.get("file"),
            "what": e.get("what"),
            "scene": e.get("scene"),
            "keys": e.get("keys"),
            "size": os.path.getsize(p) if os.path.exists(p) else 0,
        })
    return rows


# ---------------------------------------------------------------- API
KEYCAP_LOG = os.path.join(HERE, "key_capture.log")
KEYCAP_STATE = os.path.join(HERE, "key_capture.state")


def keycap_status():
    """重新抓库 key 的进度。"""
    st = {"running": False, "launched": 0, "log": ""}
    try:
        st["launched"] = int(io.open(KEYCAP_STATE, encoding="utf-8").read().strip() or 0)
    except Exception:
        pass
    try:
        txt = io.open(KEYCAP_LOG, encoding="utf-8", errors="replace").read()
    except Exception:
        txt = ""
    st["log"] = "\n".join(txt.strip().splitlines()[-8:])
    if st["launched"] and time.time() - st["launched"] < 180 and "[done]" not in txt:
        st["running"] = True
    return st


def keycap_start():
    """用管理员权限跑 wx4_key.py，结果写 keys.json。"""
    if CFG.wechat_backend() == "native":
        return {"ok": False, "error": "Ubuntu 原生接入无需抓数据库 Key，请使用 wx auth login 扫码登录"}
    py = os.path.join(HERE, "python", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    script = os.path.join(HERE, "wechat-mcp", "scripts", "wx4_key.py")
    if not os.path.isfile(script):
        return {"ok": False, "error": "找不到 wx4_key.py：%s" % script}
    io.open(KEYCAP_STATE, "w", encoding="utf-8").write(str(int(time.time())))
    io.open(KEYCAP_LOG, "w", encoding="utf-8").write(
        "启动抓取…\n请在 UAC 之后、切到微信点开任意聊天。\n")
    cmd = '"%s" "%s" --json --timeout 120 >> "%s" 2>&1 & echo [done] >> "%s"' % (
        py, script, KEYCAP_LOG, KEYCAP_LOG)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", "/c " + cmd, HERE, 0)
    if r <= 32:
        return {"ok": False, "error": "UAC 被取消或启动失败（代码 %d）" % r}
    return {"ok": True, "msg": "已启动，请看 UAC 弹窗和微信窗口"}


def api_overview():
    return {
        "backend": CFG.wechat_backend(),
        "bot": bot_state(),
        "hook": hook_state(),
        "wechat": wechat_procs(),
        "mutes": mutes(),
        "curfew": {"periods": TUNE.load()["CURFEW_PERIODS"], "active": TUNE.in_curfew()},
        "rooms": room_names(),
        "whitelist": read_json("whitelist.json", []),
        "admins": admin_store.load(),
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def api_send(body):
    room = (body.get("room") or "").strip()
    text = (body.get("text") or "").strip()
    if not room or not text:
        return {"ok": False, "error": "room 和 text 都要填"}
    import hook_send as HS
    ok, data = HS.send_text(room, text)
    return {"ok": bool(ok), "data": data}


def api_mute(body):
    room = (body.get("room") or "").strip()
    minutes = float(body.get("minutes") or 0)
    import wx_chat_loop as L
    if minutes <= 0:
        L.set_mute(0)
        return {"ok": True, "cleared": True, "mutes": mutes()}
    if not room:
        return {"ok": False, "error": "要选一个会话"}
    L.set_mute(minutes, scope=L.scope_for(room), by="panel")
    return {"ok": True, "mutes": mutes()}


def api_bot(body):
    action = (body.get("action") or "").strip()
    if os.name != "nt":
        if action == "stop":
            from stop_bot import stop
            stop()
        elif action == "start":
            p = subprocess.run([sys.executable, os.path.join(HERE, "bot_launcher.py")],
                               cwd=HERE, capture_output=True, text=True, timeout=15)
            if p.returncode not in (0, 4):
                return {"ok": False, "error": p.stdout[-500:] or p.stderr[-500:]}
        return {"ok": True, "bot": bot_state()}
    if action == "stop":
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", os.path.join(HERE, "stop_bot.ps1")],
                       capture_output=True, text=True, timeout=60)
        time.sleep(1)
        return {"ok": True, "bot": bot_state()}
    if action == "start":
        bat = os.path.join(HERE, "start_bot.bat")
        subprocess.Popen(["cmd", "/c", bat, "run_bot.log"], cwd=HERE,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        time.sleep(3)
        return {"ok": True, "bot": bot_state()}
    return {"ok": True, "bot": bot_state()}


def _settings_path():
    return os.path.join(HERE, "settings.json")


def allow_dsh_key():
    """是否允许读取本机 ~/.dsh 凭证（settings.json 的 panel.allowDshKey，默认 False）。"""
    try:
        st = json.load(open(_settings_path(), encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return ((((st or {}).get("panel") or {}).get("allowDshKey")) is True)


def apikey_state():
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return {"set": True, "source": "环境变量", "allowDsh": allow_dsh_key(), "masked": env[:7] + "…" + env[-4:] if len(env) > 12 else "***"}
    try:
        st = json.load(open(_settings_path(), encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        st = {}
    key = (((st or {}).get("deepseek") or {}).get("apiKey") or "").strip()
    if key:
        return {"set": True, "source": "settings.json", "allowDsh": allow_dsh_key(),
                "masked": key[:7] + "…" + key[-4:] if len(key) > 12 else "***"}
    if not allow_dsh_key():
        return {"set": False, "source": "", "masked": "", "allowDsh": False}
    cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    try:
        for line in open(cred, encoding="utf-8"):
            if line.strip().startswith("DEEPSEEK_API_KEY:"):
                k = line.split(":", 1)[1].strip()
                return {"set": True, "source": "DeepSeek Harness 凭证（本机才行）",
                        "allowDsh": True,
                        "masked": k[:7] + "…" + k[-4:] if len(k) > 12 else "***"}
    except (FileNotFoundError, OSError):
        pass
    return {"set": False, "source": "", "masked": "", "allowDsh": allow_dsh_key()}


def _save_allow_dsh(flag):
    p = _settings_path()
    try:
        st = json.load(open(p, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        st = {}
    st.setdefault("panel", {})["allowDshKey"] = bool(flag)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def api_apikey_set(body):
    if "allowDsh" in body:
        _save_allow_dsh(bool(body.get("allowDsh")))
        return {"ok": True, "state": apikey_state()}
    key = (body.get("key") or "").strip()
    if key and not key.startswith("sk-"):
        return {"ok": False, "error": "DeepSeek 的 Key 一般以 sk- 开头，检查一下？"}
    path = _settings_path()
    try:
        cfg = json.load(open(path, encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("deepseek", {})["apiKey"] = key
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return {"ok": True, "state": apikey_state()}


def api_apikey_test(body):
    import urllib.error
    import urllib.request
    key = (body.get("key") or "").strip() or apikey_state().get("masked")
    if not key or key.startswith("***") or "…" in key:
        # 用现有配置里的完整 key 再取一次
        key = ""
        try:
            st = json.load(open(_settings_path(), encoding="utf-8-sig"))
            key = (((st or {}).get("deepseek") or {}).get("apiKey") or "").strip()
        except (FileNotFoundError, OSError, ValueError):
            pass
        if not key:
            env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
            key = env
        if not key:   # 最后再试一次 DSH 的凭证文件
            cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
            try:
                for line in open(cred, encoding="utf-8"):
                    if line.strip().startswith("DEEPSEEK_API_KEY:"):
                        key = line.split(":", 1)[1].strip()
                        break
            except (FileNotFoundError, OSError):
                pass
    if not key:
        return {"ok": False, "error": "还没有 Key 可测"}
    try:
        req = urllib.request.Request("https://api.deepseek.com/models",
                                     headers={"Authorization": "Bearer " + key})
        import api_usage
        api_usage.record("DeepSeek", "密钥检查", key)
        with urllib.request.urlopen(req, timeout=20) as r:
            body_txt = r.read(300).decode("utf-8", "replace")
        return {"ok": True, "detail": body_txt}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s（%s）" % (e.code, "Key 不对 / 没余额" if e.code in (401, 402) else e.reason)}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def api_sticker_send(body):
    name = (body.get("file") or "").strip()
    room = (body.get("room") or "").strip()
    if not name or not room:
        return {"ok": False, "error": "file 和 room 都要填"}
    import hook_send as HS
    import sticker_lib as S
    path = os.path.join(S.LIB, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "库里没有这个文件"}
    ok, data = HS.send_image(room, path)
    if ok:
        try:
            S.mark_used(name)
        except Exception:                                      # noqa: BLE001
            pass
    return {"ok": bool(ok), "data": data}


def api_sticker_delete(body):
    name = (body.get("file") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return {"ok": False, "error": "文件名不合法"}
    import sticker_lib as S
    from sticker_store import library_lock
    with library_lock():
        path = os.path.join(S.LIB, name)
        if not os.path.exists(path):
            return {"ok": False, "error": "文件不存在"}
        os.remove(path)
        # 从 index.md 里删掉这一行
        idx = os.path.join(S.LIB, "index.md")
        try:
            lines = open(idx, encoding="utf-8").read().splitlines()
            keep = [ln for ln in lines if ("| %s |" % name) not in ln and not ln.strip().startswith("| %s " % name)]
            open(idx, "w", encoding="utf-8").write("\n".join(keep) + "\n")
        except OSError:
            pass
    return {"ok": True}

def _bad_msg(bad):
    parts = []
    for item in bad[:4]:
        cands = item.get("candidates") or []
        if cands:
            parts.append("「%s」有 %d 个候选：%s" % (
                item["input"], len(cands),
                "、".join((c["names"][0] if c.get("names") else c["wxid"]) for c in cands[:5])))
        else:
            parts.append("「%s」没找到" % item["input"])
    return "；".join(parts) + "（可以填 wxid，或换个更准确的名字）"


# ---------------------------------------------------------------- 名字 → wxid
def _fix_names(values):
    """把一串「名字或 id」解析成 wxid，返回 (结果, 认不出来的)。"""
    import names
    return names.resolve_many(values)


def _fix_rooms(rooms):
    import names
    out, bad = [], []
    for r in rooms or []:
        r = dict(r or {})
        raw = str(r.get("room") or "").strip()
        label = str(r.get("name") or "").strip()
        acct = str(r.get("account") or "").strip()
        if not raw:                      # 只填了「名字」列也认（m09094）
            raw = label
        if not raw:
            continue
        wxid, cands = names.resolve(raw, account=acct or None) if not names.looks_like_id(raw) else (raw, [])
        if not wxid:
            bad.append({"input": raw, "candidates": cands})
            continue
        r["room"] = wxid
        if not str(r.get("name") or "").strip():
            r["name"] = names.display(wxid, account=acct or None) or wxid
        if not acct:
            acct = names._account() or ""
            r["account"] = acct
        r.setdefault("enabled", True)
        out.append(r)
    return out, bad


def _fix_admins(rows):
    """逐行解析管理员，备注保留，微信名字转换为 ID。"""
    import names
    keep, bad = [], []
    for row in rows:
        label = str(row.get("name") or "").strip()
        who = str(row.get("wxid") or "").strip() or label
        if not who:
            continue
        wxid, cands = names.resolve(who) if not names.looks_like_id(who) else (who, [])
        if not wxid:
            bad.append({"input": who, "candidates": cands})
            continue
        keep.append({"wxid": wxid, "name": label})
    return keep, bad


# ---------------------------------------------------------------- HTTP
def make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "wxchatbot-panel/1.0"

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            raw = payload if isinstance(payload, bytes) else \
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):                                       # noqa: N802
            path = self.path.split("?")[0]
            q = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p) \
                if "?" in self.path else {}
            try:
                if path in ("/", "/index.html", "/login", "/cloud", "/memory"):
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/console/state":
                    return self._send(200, {"ok": True, "mode": "legacy", "csrf": ""})
                if path in ("/console/console.css", "/console/console.js", "/console/product.js"):
                    file = Path(HERE, "console-assets", path.rsplit("/", 1)[-1])
                    mime = "text/css" if file.suffix == ".css" else "application/javascript"
                    return self._send(200, file.read_bytes(), mime + "; charset=utf-8")
                if path == "/console/avatar.png":
                    with open(os.path.join(HERE, "console-assets", "avatar.png"), "rb") as asset:
                        return self._send(200, asset.read(), "image/png")
                if CFG.wechat_backend() == "native":
                    import native_wechat
                    if path == "/api/wechat-login":
                        return self._send(200, {"ok": True, **native_wechat.login_status(q.get("attempt"))})
                    if path == "/api/wechat-login/qr":
                        return self._send(200, native_wechat.login_qr(q.get("attempt")), "image/png")
                if path == "/api/memory/status":
                    import memory_store
                    return self._send(200, memory_store.status())
                if path == "/api/memory/search":
                    import memory_store
                    from urllib.parse import unquote_plus
                    return self._send(200, {"items": memory_store.search(unquote_plus(q.get("q", "")))})
                if path == "/api/cloud":
                    import wx_cloud
                    return self._send(200, {"ok": True, **wx_cloud.state()})
                if path == "/api/usage":
                    import api_usage
                    try:
                        return self._send(200, {"ok": True, **api_usage.summary(int(q.get("days", "14")))})
                    except ValueError as error:
                        return self._send(400, {"ok": False, "error": str(error)})
                if path == "/api/overview":
                    return self._send(200, {"ok": True, **api_overview()})
                if path == "/api/tuning":
                    return self._send(200, {"ok": True, **TUNE.describe()})
                if path == "/api/taught-rules":
                    import rule_teaching
                    return self._send(200, {"ok": True, **rule_teaching.describe()})
                if path == "/api/rooms":
                    return self._send(200, {"ok": True, "rooms": read_json("rooms.json", [])})
                if path == "/api/whitelist":      # 白名单已与 rooms 合并（m09032）：只返回启用的
                    rooms = read_json("rooms.json", [])
                    return self._send(200, {"ok": True,
                                            "whitelist": [r["room"] for r in rooms if r.get("enabled", True)]})
                if path == "/api/admins":
                    return self._send(200, {"ok": True, "admins": admin_store.load()})
                if path == "/api/persona":
                    return self._send(200, {"ok": True, **persona_store.snapshot()})
                if path == "/api/stickers":
                    return self._send(200, {"ok": True, "items": sticker_list()})
                if path == "/api/apikey":
                    return self._send(200, {"ok": True, **apikey_state()})
                if path == "/api/keycap":
                    return self._send(200, {"ok": True, **keycap_status()})
                if path == "/api/log":
                    n = int(q.get("n", "200") or 200)
                    return self._send(200, {"ok": True, "lines": tail(LOG_FILE, max(10, min(n, 2000)))})
                if path.startswith("/sticker/"):
                    return self._serve_sticker(path[len("/sticker/"):])
                return self._send(404, {"ok": False, "error": "没有这个接口"})
            except Exception as e:                              # noqa: BLE001
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        def _serve_sticker(self, name):
            import urllib.parse
            name = urllib.parse.unquote(name)
            import sticker_lib as S
            p = os.path.join(S.LIB, os.path.basename(name))
            if not os.path.exists(p):
                return self._send(404, {"ok": False, "error": "没有这个图"})
            ext = os.path.splitext(p)[1].lower()
            ctype = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)

        def do_POST(self):                                      # noqa: N802
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"ok": False, "error": "请求体不是 JSON"})
            try:
                if path == "/api/cloud/save":
                    import wx_cloud
                    return self._send(200, {"ok": True, **wx_cloud.save(body)})
                if path == "/api/cloud/test":
                    import wx_cloud
                    result = wx_cloud.search("今天的北京时间日期是什么？请核实日期。")
                    return self._send(200, {"ok": True, "sourceCount": len(result["sources"])})
                if path == "/api/wechat-login/start" and CFG.wechat_backend() == "native":
                    import native_wechat
                    return self._send(200, {"ok": True, **native_wechat.start_login()})
                if path == "/api/tuning":
                    try:
                        changed = TUNE.save(body.get("patch") or {})
                    except ValueError as e:
                        return self._send(400, {"ok": False, "error": str(e)})
                    return self._send(200, {"ok": True, "changed": changed, **TUNE.describe()})
                if path == "/api/rooms":
                    rooms = body.get("rooms")
                    if not isinstance(rooms, list):
                        return self._send(400, {"ok": False, "error": "rooms 要是数组"})
                    rooms, bad = _fix_rooms(rooms)
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    write_json("rooms.json", rooms)
                    write_json("whitelist.json", [r["room"] for r in rooms if r.get("enabled", True)])
                    return self._send(200, {"ok": True, "rooms": rooms})
                if path == "/api/whitelist":
                    wl = body.get("whitelist")
                    if not isinstance(wl, list):
                        return self._send(400, {"ok": False, "error": "whitelist 要是数组"})
                    wl, bad = _fix_names(wl)
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    # 兼容老接口：把名单里的会话在 rooms.json 里置 enabled，其余置 false
                    rooms = read_json("rooms.json", [])
                    have = {r.get("room") for r in rooms}
                    for rid in wl:
                        if rid not in have:
                            rooms.append({"name": names.display(rid) or rid, "room": rid,
                                          "account": names._account() or "", "enabled": True})
                    for r in rooms:
                        r["enabled"] = r.get("room") in set(wl)
                    write_json("rooms.json", rooms)
                    write_json("whitelist.json", [r["room"] for r in rooms if r.get("enabled", True)])
                    return self._send(200, {"ok": True, "whitelist": wl})
                if path == "/api/admins/save":
                    rows = body.get("admins")
                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                        return self._send(400, {"ok": False, "error": "admins 要是对象数组"})
                    rows, bad = _fix_admins(rows)
                    if bad:
                        return self._send(400, {"ok": False, "error": _bad_msg(bad), "bad": bad})
                    admin_store.save(rows)
                    return self._send(200, {"ok": True, "admins": rows})
                if path == "/api/taught-rules/save":
                    import rule_teaching
                    try:
                        result = rule_teaching.save(body.get("text"), body.get("revision"))
                    except ValueError as error:
                        return self._send(400, {"ok": False, "error": str(error)})
                    return self._send(200, {"ok": True, **result})
                if path == "/api/persona/save":
                    kind, text = body.get("kind"), body.get("text")
                    if kind not in persona_store.FILES or not isinstance(text, str):
                        return self._send(400, {"ok": False, "error": "人设类型或文本无效"})
                    persona_store.save(kind, text)
                    return self._send(200, {"ok": True, "note": "下一轮自动生效"})
                if path == "/api/send":
                    return self._send(200, api_send(body))
                if path == "/api/mute":
                    return self._send(200, api_mute(body))
                if path == "/api/bot":
                    return self._send(200, api_bot(body))
                if path == "/api/stickers/send":
                    return self._send(200, api_sticker_send(body))
                if path == "/api/stickers/delete":
                    return self._send(200, api_sticker_delete(body))
                if path == "/api/apikey":
                    return self._send(200, api_apikey_set(body))
                if path == "/api/keycap":
                    return self._send(200, keycap_start())
                if path == "/api/apikey/test":
                    return self._send(200, api_apikey_test(body))
                return self._send(404, {"ok": False, "error": "没有这个接口"})
            except Exception as e:                              # noqa: BLE001
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        def log_message(self, fmt, *args):                      # 静音访问日志
            pass

    return Handler


PAGE = Path(HERE, "console-assets", "index.html").read_text(encoding="utf-8").replace("__RUNTIME__", "legacy")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer((args.host, args.port), make_handler())
    url = "http://%s:%d" % (args.host, args.port)
    print("面板已启动：%s" % url, flush=True)
    print("（只监听本机，不需要登录；Ctrl+C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
