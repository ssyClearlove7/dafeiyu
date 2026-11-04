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


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>大肥鱼 · 控制台</title><link rel="icon" type="image/png" href="/console/avatar.png">
<style>
:root{--bg:#0f1419;--panel:#161c24;--line:#243040;--fg:#dbe4ee;--dim:#8797a8;--acc:#4aa8ff;--ok:#4cd18a;--warn:#ffb454;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:600}
header .sp{flex:1}
.badge{padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:#1e4a34;background:#10241a}
.badge.bad{color:var(--bad);border-color:#4a1e1e;background:#241010}
.badge.warn{color:var(--warn);border-color:#4a3a1e;background:#241d10}
main{display:flex;min-height:calc(100vh - 104px)}
.foot{padding:9px 16px;border-top:1px solid var(--line);background:var(--panel);color:var(--dim);font-size:12px;text-align:center;line-height:1.6}
nav{width:150px;border-right:1px solid var(--line);padding:10px 8px;background:var(--panel)}
nav button{display:block;width:100%;text-align:left;padding:9px 12px;margin:2px 0;border:0;border-radius:8px;background:transparent;color:var(--dim);cursor:pointer;font-size:14px}
nav button:hover{background:#1d2632;color:var(--fg)}
nav button.on{background:#1b2dur;background:#1b2b3d;color:#fff}
section{flex:1;padding:18px 22px;max-width:1080px}
h2{font-size:15px;margin:0 0 14px;color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px dashed #1e2836}
.kv:last-child{border:0}
.kv b{font-weight:500;color:var(--dim)}
label{display:block;margin:10px 0 4px;color:var(--dim);font-size:13px}
input[type=text],input[type=number],textarea,select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#0d1218;color:var(--fg);font:inherit}
textarea{min-height:120px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
button.act{padding:8px 16px;border-radius:8px;border:1px solid #2b4b6d;background:#17324d;color:#cfe6ff;cursor:pointer;font:inherit}
button.act:hover{background:#1d3f61}
button.ghost{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--dim);cursor:pointer}
button.danger{border-color:#5a2626;background:#2a1414;color:#ffb3b3}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
.stickers{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.sticker{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#0d1218}
.sticker img{width:100%;height:120px;object-fit:contain;background:#0a0e13;display:block}
.sticker .cap{padding:8px;font-size:12px;color:var(--dim);height:96px;overflow:auto}
.sticker .ops{display:flex;gap:6px;padding:0 8px 8px}
.sticker .ops button{flex:1;padding:5px 0;font-size:12px}
pre{background:#0a0e13;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;max-height:60vh;font-size:12.5px;line-height:1.5}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;background:#1b2430;color:var(--dim);font-size:12px;margin:0 6px 6px 0}
.hint{color:var(--dim);font-size:12.5px;margin:6px 0 0}
.toast{position:fixed;right:18px;bottom:18px;padding:10px 16px;border-radius:10px;background:#1d3f61;color:#fff;opacity:0;transition:.2s;pointer-events:none}
.toast.on{opacity:1}

/* Console navigation, adapted to the existing Ubuntu runtime. */
:root{--bg:#f1f6f5;--panel:#fff;--line:#dce7e4;--fg:#173339;--dim:#6e8385;--acc:#137b79;--ok:#27765d;--warn:#946c2e;--bad:#a0433d}
body{font:14px/1.7 "Segoe UI","Microsoft YaHei",sans-serif}
header{margin-left:244px;background:transparent;border:0;padding:30px 40px 18px;flex-wrap:wrap;gap:10px}
header h1{font-size:18px;color:var(--fg);margin-right:auto}header .sp{display:none}
.badge{background:#fff;font-size:11px;padding:4px 10px}.badge.ok{background:#edf5f1;border-color:#d4e8df;color:var(--ok)}.badge.bad{background:#f9eae5;border-color:#efd7d3;color:var(--bad)}.badge.warn{background:#fbefdc;border-color:#efdfc4;color:var(--warn)}
main{display:block;min-height:calc(100vh - 130px);margin-left:244px}
nav{position:fixed;left:0;top:0;bottom:0;width:244px;padding:28px 18px;overflow-y:auto;scrollbar-width:thin;z-index:10}
.brand{display:flex;gap:12px;align-items:center;text-decoration:none;color:var(--fg);padding:0 8px 26px;font-size:23px;font-weight:650}.brand .fish{display:grid;place-items:center;width:49px;height:49px;background:#dff1ed;border-radius:15px;font-size:29px}.brand small{display:block;font-size:11px;letter-spacing:3px;font-weight:400;color:var(--dim)}
.nav-caption{font-size:10px;color:#8a9b9a;letter-spacing:2px;padding:0 13px;margin:0 0 12px}.nav-group{margin-bottom:14px}
nav .nav-heading{display:flex;gap:10px;align-items:center;padding:9px 12px;color:#385657;font-size:12px;font-weight:600;background:transparent;margin:0}
.nav-icon{width:17px;text-align:center;font-size:16px;color:#608983}.chevron{margin-left:auto;color:#8a9e99;transition:transform .15s}.nav-heading[aria-expanded=false] .chevron{transform:rotate(-90deg)}
.nav-items{display:grid;gap:3px;margin:4px 0 0 20px;padding-left:10px;border-left:1px solid #e0ebe7}.nav-items[hidden]{display:none}
nav .nav-items button,nav .nav-items a{display:block;width:100%;position:relative;margin:0;text-align:left;color:var(--dim);padding:10px 14px;border-radius:8px;text-decoration:none;line-height:1.5;font-size:13px;background:transparent}
nav .nav-items button:hover,nav .nav-items a:hover{background:#f1f7f4;color:#265f58}nav .nav-items button.on{background:#e5f2ed;color:#126b65;font-weight:600}nav .nav-items button.on:before{content:'';position:absolute;left:-13px;top:18px;width:5px;height:5px;border-radius:50%;background:#18847a}
.aside-note{border-top:1px solid var(--line);padding:18px 8px 0;font-size:10px;color:var(--dim);margin-top:24px}
section#view{width:100%;max-width:1280px;padding:4px 40px 24px;margin:auto;min-width:0}
.page-heading{padding:0 0 24px}.page-heading .eyebrow{font-size:11px;color:var(--dim);margin:0 0 7px}.page-heading h1{font-size:27px;line-height:1.4;margin:0 0 8px}.page-heading p{font-size:13px;color:var(--dim);margin:0}
h2{color:var(--fg);font-size:16px}.card{border-radius:16px;padding:22px 24px;margin-bottom:18px;min-width:0}.grid{gap:20px}.kv{border-color:#edf2ef;padding:7px 0;overflow-wrap:anywhere}
input[type=text],input[type=number],textarea,select{background:#fcfefd;color:var(--fg);padding:10px 12px;border-color:#d5e1de;max-width:100%}input:focus,textarea:focus,select:focus{outline:2px solid #81cbc6;outline-offset:1px}
button,a,input,select,textarea{font-family:inherit}button{cursor:pointer}button.act{background:var(--acc);border-color:var(--acc);color:#fff;padding:10px 16px}button.act:hover{background:#0e6664}button.ghost{background:#fff;color:#385657;padding:10px 14px}button.ghost:hover{background:#e5f0ed}button.danger{background:#fff5f3;color:var(--bad);border-color:#efd7d3}button:disabled{opacity:.5;cursor:default}button:focus-visible,a:focus-visible{outline:3px solid #81cbc6;outline-offset:3px}
a{color:var(--acc)}.hint{color:var(--dim);overflow-wrap:anywhere}.tag{background:#edf5f1;color:#47756a;padding:4px 10px}.sticker,.sticker img{background:#f7faf8}.sticker .cap{color:var(--dim)}
pre{background:#f7faf8;color:var(--fg);border-color:var(--line);white-space:pre-wrap;overflow-wrap:anywhere}.table-scroll{width:100%;overflow-x:auto}table{font-size:13px}th{font-size:11px;color:var(--dim);background:#f6faf8;padding:12px 8px}td{padding:10px 8px;border-bottom:1px solid #edf2ef}table input[type=text]{min-width:150px}
.toast{background:#244940;box-shadow:0 10px 40px #0002;max-width:calc(100vw - 36px);z-index:20}.foot{margin-left:244px;background:transparent;border:0;color:#9aadab;font-size:11px;padding-bottom:20px}.loading{padding:48px;text-align:center;color:var(--dim)}
@media(max-width:1000px){header{padding-left:24px;padding-right:24px}section#view{padding-left:24px;padding-right:24px}}
@media(max-width:760px){header,main,.foot{margin-left:0}body>header{padding:20px 16px 12px}nav{position:relative;width:100%;padding:18px 15px;border-right:0;border-bottom:1px solid var(--line)}.brand{padding:0 5px 16px;font-size:21px}.brand .fish{width:38px;height:38px;font-size:24px}.brand small,.nav-caption,.aside-note{display:none}.nav-groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-items:start}.nav-group{margin:0;background:#f7faf8;border:1px solid #e6eeea;border-radius:10px;padding:4px}nav .nav-heading{font-size:12px;padding:7px 8px}.nav-items{margin:2px 0 4px 12px;padding-left:8px}nav .nav-items button,nav .nav-items a{font-size:12px;padding:8px 10px}nav .nav-items button.on:before{left:-11px;top:14px}section#view{padding:8px 16px 20px}.page-heading h1{font-size:23px}.card{padding:18px;border-radius:13px}.row>div{min-width:0;flex-basis:100%}.row{align-items:stretch}.grid{grid-template-columns:minmax(0,1fr)}.stickers{grid-template-columns:repeat(2,minmax(0,1fr))}header h1{width:100%;font-size:16px}#badgeTime{font-size:10px}.page-heading{padding-bottom:18px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}


.brand-avatar{object-fit:contain;background:white!important;border:1px solid var(--line)}
input[type=password]{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #d5e1de;background:#fcfefd;color:var(--fg);font:inherit}
.check-label{display:flex;align-items:center;gap:10px;margin:18px 0}.check-label input{width:18px;height:18px;accent-color:var(--acc)}
.wechat-qr{display:block;width:260px;max-width:100%;margin:22px auto;border:1px solid var(--line);border-radius:12px;padding:12px;background:#fff}.wechat-qr[hidden]{display:none}
.service-state{color:var(--ok);font-size:13px}.memory-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.memory-stats strong{display:block;font-size:28px;margin-top:8px;font-variant-numeric:tabular-nums}
.memory-entry{padding-top:16px;margin-top:16px;border-top:1px solid var(--line)}.memory-entry h3{font-size:14px;font-weight:600;margin:0 0 10px}.memory-entry pre{max-height:none}.card summary{cursor:pointer;color:var(--dim)}
@media(max-width:760px){.memory-stats{gap:8px}.memory-stats .card{padding:14px 10px}.memory-stats strong{font-size:23px}.memory-stats .hint{font-size:11px}}

/* Shared black theme, including legacy controls and embedded SVG charts. */
:root{color-scheme:dark;--bg:#0b0e11;--panel:#14191e;--line:#293139;--fg:#e2e9ed;--dim:#94a3ac;--acc:#79c6b1;--ok:#8cd2b5;--warn:#e3be7c;--bad:#ef9b9b}
body{background:var(--bg);color:var(--fg)}
nav{background:#101519;border-color:var(--line)}
.brand,.page-heading h1,header h1,h2{color:var(--fg)}
.brand .brand-avatar{background:#fff!important;border-color:#3a454d}
.nav-caption{color:#74878f}nav .nav-heading{color:#c3d0d5}.nav-icon{color:#83a99e}.chevron{color:#83968e}
.nav-items{border-color:#303d3c}nav .nav-items button,nav .nav-items a{color:#a3b2b9}
nav .nav-items button:hover,nav .nav-items a:hover{background:#1c262a;color:#e5efeb}
nav .nav-items button.on{background:#223831;color:#a1dfcc}nav .nav-items button.on:before{background:#96d7c1}
.card{background:var(--panel);border-color:var(--line)}.kv{border-color:#283138}.kv b,label,.hint,.page-heading p,.page-heading .eyebrow{color:var(--dim)}
.badge{background:#182127;color:#b4c1c8;border-color:#33434a}.badge.ok{background:#192c25;border-color:#30483c;color:#a6d2bc}.badge.bad{background:#312123;border-color:#57373b;color:#efacac}.badge.warn{background:#30291d;border-color:#51452c;color:#e3c78e}
input[type=text],input[type=number],input[type=password],textarea,select{background:#0e1418;color:var(--fg);border-color:#344149}input::placeholder,textarea::placeholder{color:#677b85}input[type=checkbox]{accent-color:#79c6b1}
button.act{background:#83cbb7;border-color:#83cbb7;color:#10251e}button.act:hover{background:#a0dfcd}button.ghost{background:#182127;border-color:#34434c;color:#c2d0d7}button.ghost:hover{background:#26343c}button.danger{background:#302023;border-color:#59373c;color:#eeacac}
button:focus-visible,a:focus-visible{outline-color:#a2d8ca}input:focus,textarea:focus,select:focus{outline-color:#79c6b1}
.tag{background:#20332c;color:#a3cbb9}pre{background:#0d1317;color:#c2cfd7;border-color:#2d3940}.sticker,.sticker img{background:#0d1317}.sticker{border-color:#2d3940}
th{background:#10171b;color:#aabac1}td{border-color:#29343b}a{color:#9dd5c4}.toast{background:#29483e;box-shadow:0 10px 40px #0006}.foot{color:#71848b}.service-state{color:#a5d6bd}.loading{color:#9aacb4}
.list-column-hint{display:block;margin-top:6px;max-width:280px;font-size:11px;line-height:1.6;font-weight:400;color:var(--dim)}
.chart-heading{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}.chart-heading h2{margin-bottom:6px}.chart-heading p{margin:0}
.usage-chart{overflow-x:auto;margin:18px -3px 8px}.usage-chart svg{display:block;min-width:620px;width:100%;font:11px "Segoe UI","Microsoft YaHei",sans-serif}.usage-chart text{fill:#94a9b1}.chart-grid{stroke:#2a353d;stroke-width:1;stroke-dasharray:3 5}.usage-chart g:focus{outline:1px solid #9bd6c1;outline-offset:2px}
.usage-legend{display:flex;gap:10px 20px;flex-wrap:wrap;margin-top:22px;font-size:11px;color:#c2ced3}.usage-legend span{display:inline-flex;align-items:center;gap:7px;overflow-wrap:anywhere}.usage-legend i{display:inline-block;width:9px;height:9px;border-radius:3px;flex-shrink:0}.usage-legend b{font-weight:500;color:#8da39f;font-variant-numeric:tabular-nums}
@media(max-width:760px){.nav-group{background:#151c20;border-color:#2c383f}.nav-items{border-color:#304039}}
/* Overview: service board, compact policies, and a bounded chart. */
.overview-layout{display:grid;gap:14px}
.overview-layout>.card,.overview-policies>.card{margin:0}
.overview-status{padding:22px 24px}
.overview-section-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
.overview-section-head h2{font-size:14px;font-weight:600;line-height:1.5;margin:0;color:#d6e2e7}
.overview-refresh{display:inline-flex;align-items:center;gap:6px;font-size:10px;color:#7f949e;white-space:nowrap}
.overview-refresh i{height:5px;width:5px;border-radius:50%;background:#79b9a5}
.overview-services{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin:20px 0}
.overview-service{display:flex;gap:12px;align-items:center;min-width:0}
.overview-service+ .overview-service{border-left:1px solid #2b363d;padding-left:28px}
.overview-service-icon{display:grid;place-items:center;width:38px;height:38px;flex-shrink:0;border:1px solid #30463f;border-radius:12px;color:#8cc4b2;background:#1b2a25;font-size:18px}
.overview-label{font-size:11px;color:#91a3ab;margin:0 0 3px;line-height:1.5}
.overview-service-value{display:flex;align-items:center;gap:7px;font-size:19px;font-weight:550;line-height:1.5;color:#e4eeeb;margin:0;white-space:nowrap}
.status-light{height:6px;width:6px;border-radius:50%;background:#d8a46c;flex-shrink:0}.status-light.is-on{background:#82c9ae}
.overview-account{margin-left:auto;color:#91a6ad;font-family:Consolas,monospace;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.overview-metadata{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:18px;margin:0;padding:14px 0 0;border-top:1px solid #29343b}
.overview-metadata dt{font-size:10px;color:#82969f;margin:0 0 5px;line-height:1.5}
.overview-metadata dd{font-size:13px;line-height:1.5;font-variant-numeric:tabular-nums;color:#c9d6dc;margin:0;overflow-wrap:anywhere}
.overview-metadata small{font-size:10px;color:#82969f;margin-left:5px}
.overview-policies{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.overview-policy{padding:17px 22px}
.overview-policy-state{font-size:10px;line-height:1.6;color:#91b3a5;background:#1d2b25;border:1px solid #304036;border-radius:6px;padding:2px 8px;white-space:nowrap}
.overview-policy-state.is-resting{color:#d9be91;background:#302a1f;border-color:#4c402c}
.overview-policy-content{font-size:12px;margin-top:12px;color:#94a5ad;line-height:1.7}
.overview-policy-content .hint{font-size:12px}.overview-policy-content .tag{font-size:11px;padding:3px 7px;margin:0 6px 4px 0}
.overview-layout .usage-card{padding:21px 24px 14px}
.overview-chart-subtitle{font-size:11px;color:#82969f;line-height:1.6;margin:4px 0 0}
.overview-chart-range{color:#9cb4bc;background:#10181d;border:1px solid #2b3b42;border-radius:7px;padding:5px 9px;font-size:10px;white-space:nowrap}
.overview-layout .usage-legend{margin-top:18px;gap:8px 18px;font-size:10px}
.overview-layout .usage-chart{margin:12px 0 0}
.overview-layout .usage-chart svg{min-width:700px;height:auto;max-width:none}
.overview-chart-note{border-top:1px solid #29343b;margin-top:0;padding-top:11px;color:#8197a1;font-size:10px}
.overview-chart-note summary{display:flex;gap:10px;justify-content:space-between;align-items:center;cursor:pointer;list-style:none}
.overview-chart-note summary:before{content:'+';color:#97b8ad;font-size:13px}.overview-chart-note[open] summary:before{content:'−'}
.overview-chart-note summary span{margin-left:auto;font-size:10px;color:#718790}
.overview-chart-note p{font-size:11px;line-height:1.8;margin:10px 0 0}
@media(max-width:1050px){.overview-account{max-width:155px;font-size:10px}.overview-services{gap:18px}.overview-service+ .overview-service{padding-left:18px}.overview-service-icon{width:32px;height:32px;border-radius:10px}.overview-service-value{font-size:17px}}
@media(max-width:760px){
 .overview-layout{gap:12px}.overview-status{padding:18px}
 .overview-services{grid-template-columns:1fr;gap:14px;margin:17px 0}
 .overview-service+ .overview-service{padding:14px 0 0;border-left:0;border-top:1px solid #29343b}
 .overview-service-value{font-size:17px}.overview-service-icon{width:36px;height:36px}
 .overview-account{max-width:160px;font-size:10px}.overview-metadata{grid-template-columns:1fr 1fr;gap:13px 20px;padding-top:14px}.overview-metadata>div:last-child{grid-column:1/-1}
 .overview-policies{gap:10px}.overview-policy{padding:14px}.overview-section-head h2{font-size:13px}.overview-policy-state{font-size:9px;padding:2px 5px}.overview-policy-content{margin-top:10px}
 .overview-layout .usage-card{padding:18px 16px 13px}.overview-chart-range{font-size:9px;padding:5px 7px}.overview-chart-subtitle{font-size:10px}.overview-layout .usage-legend{font-size:10px;gap:7px 12px}
 .overview-chart-note summary{flex-wrap:wrap;gap:4px}.overview-chart-note summary span{font-size:9px}
}


.persona-tabs{display:flex;gap:6px;background:#10171b;border:1px solid var(--line);padding:5px;border-radius:12px;margin-bottom:18px;width:fit-content;max-width:100%}
.persona-tabs button{background:transparent;border:0;color:var(--dim);padding:10px 18px;border-radius:8px;font:inherit;font-size:13px;white-space:nowrap}
.persona-tabs button[aria-selected=true]{background:#253c32;color:#b5e2ce}.persona-tabs button:hover{color:#def0e7}
.persona-editor[hidden]{display:none}.persona-editor textarea{line-height:1.8;min-height:200px}.teaching-field{margin:20px 0;gap:8px;display:grid}.teaching-field>span{font-size:13px;color:#c3d2d9}.teaching-field textarea{min-height:120px;font-family:inherit}.teaching-field input,.teaching-field select{max-width:420px}.teaching-empty{padding:25px 0;color:var(--dim);font-size:13px}
@media(max-width:760px){.persona-tabs{width:100%;gap:3px}.persona-tabs button{flex:1;padding:10px 8px;font-size:12px}}

</style></head>
<body>
<nav aria-label="主要功能"><a class="brand" href="#overview"><img class="fish brand-avatar" src="/console/avatar.png" alt="大肥鱼形象"><span>大肥鱼<small>陪伴控制台</small></span></a><p class="nav-caption">工作空间</p><div class="nav-groups" id="navigation"></div><p class="aside-note">Ubuntu · 控制台直接访问</p></nav>
<header>
  <h1>大肥鱼 · 陪伴控制台</h1>
  <span class="badge" id="badgeBot">机器人：查询中</span>
  <span class="badge" id="badgeHook">hook：查询中</span>
  <span class="sp"></span>
  <span class="badge" id="badgeTime"></span>
</header>
<main>

<section id="view"></section>
</main>
<div class="toast" id="toast"></div>
<script>
const $ = (s) => document.querySelector(s);
let TAB = "overview", OVERVIEW = null, TUNING = null, ROOMS = [], WHITELIST = [],
    ADMINS = [];
let BOT_BUSY = false;

function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.style.background = bad ? "#5a2626" : "#1d3f61";
  t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 2200);
}
async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ok: false, error: "返回不是 JSON"}));
  if (!r.ok || j.ok === false) toast(j.error || "失败", true);
  return j;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }
function roomOptions(sel) {
  return ROOMS.map((r) => `<option value="${esc(r.room)}"${r.room === sel ? " selected" : ""}>${esc(r.name || r.room)}</option>`).join("");
}

async function loadOverview() {
  const [j,usage] = await Promise.all([api("/api/overview"), TAB === "overview" ? api("/api/usage?days="+USAGE_DAYS) : Promise.resolve(null)]);
  if (usage?.ok && usage.days.length===USAGE_DAYS) USAGE_DATA=usage;
  if (!j.ok) return;
  OVERVIEW = j;
  const b = $("#badgeBot"), h = $("#badgeHook");
  b.className = "badge " + (j.bot.running ? "ok" : "bad");
  b.textContent = j.bot.running ? (j.bot.round == null ? `机器人：等待配置 (#${j.bot.pid})` : `机器人：运行中 (#${j.bot.pid})`) : "机器人：没在跑";
  h.className = "badge " + (j.hook.ok ? "ok" : "bad");
  h.textContent = "微信：" + (j.hook.ok ? "正常" : "不通");
  $("#badgeTime").textContent = j.now;
  $("#badgeTime").className = "badge ok";
  if ((TAB === "overview" || TAB === "bot") && !BOT_BUSY) render();
}

async function setBotRunning(action) {
  if (BOT_BUSY) return;
  BOT_BUSY = true;
  const controls = ["#botStart", "#botStop"];
  controls.forEach(s => { if ($(s)) $(s).disabled = true; });
  try {
    const r = await api("/api/bot", {action});
    if (r.ok) {
      const expected = action === "start";
      const matched = !!r.bot?.running === expected;
      toast(matched ? (expected ? "机器人已启动" : "机器人已停止") : "操作后状态未确认，请查看日志", !matched);
    }
    await loadOverview();
  } catch (e) {
    toast("操作失败：" + e.message, true);
  } finally {
    BOT_BUSY = false;
    if (TAB === "bot") renderBot();
    else controls.forEach(s => { if ($(s)) $(s).disabled = false; });
  }
}

function render() {
  if (TAB === "overview") return renderOverview();
  if (TAB === "bot") return renderBot();
  if (TAB === "model") return renderModel();
  if (TAB === "wechat") return renderWechat();
  if (TAB === "voice") return renderVoice();
  if (TAB === "memory") return renderMemory();
  if (TAB === "tuning") return renderTuning();
  if (TAB === "lists") return renderLists();
  if (TAB === "persona") return renderPersona();
  if (TAB === "stickers") return renderStickers();
  if (TAB === "tools") return renderTools();
  if (TAB === "log") return renderLog();
}

function renderOverview() {
  const o = OVERVIEW || {},bot=o.bot||{},connected=!!o.hook?.ok;
  const running=bot.running?(bot.round==null?(connected?'等待配置会话':'等待微信就绪'):'运行中'):'已停止';
  const mutes=(o.mutes||[]).map(m=>`<span class="tag">${esc(o.rooms?.[m.scope]||m.scope)} · 剩余 ${m.left} 秒</span>`).join('');
  const curfew=(o.curfew?.periods||[]).map(period=>`<span class="tag">${esc(period.start)} — ${esc(period.end)}</span>`).join('');
  $('#view').innerHTML=`<div class="overview-layout">
    <div class="card overview-status">
      <div class="overview-section-head"><h2>服务状态</h2><span class="overview-refresh"><i></i>每 5 秒更新</span></div>
      <div class="overview-services">
        <div class="overview-service"><span class="overview-service-icon" aria-hidden="true">◉</span><div><p class="overview-label">自动回复服务</p><p class="overview-service-value"><span class="status-light ${bot.running?'is-on':''}"></span>${running}</p></div></div>
        <div class="overview-service"><span class="overview-service-icon" aria-hidden="true">◇</span><div><p class="overview-label">微信连接</p><p class="overview-service-value"><span class="status-light ${connected?'is-on':''}"></span>${connected?'已连接':'未连接'}</p></div><span class="overview-account" title="${esc(o.hook?.wxid||'未登录')}">${esc(o.hook?.wxid||'未登录')}</span></div>
      </div>
      <dl class="overview-metadata"><div><dt>进程 PID</dt><dd>${bot.pid??'—'}</dd></div><div><dt>最近心跳</dt><dd>${bot.age!=null?bot.age+' 秒前':'—'}</dd></div><div><dt>运行轮次</dt><dd>${bot.round??'—'}</dd></div><div><dt>会话数量</dt><dd>${Object.keys(o.rooms||{}).length}<small>个</small></dd></div><div><dt>接入方式</dt><dd>${o.backend==='native'?'Ubuntu 原生':'Windows Hook'}</dd></div></dl>
    </div>
    <div class="overview-policies">
      <div class="card overview-policy"><div class="overview-section-head"><h2>静音</h2><span class="overview-policy-state ${(o.mutes||[]).length?'is-resting':''}">${(o.mutes||[]).length?'静音中':'未静音'}</span></div><div class="overview-policy-content">${mutes||'<span class="hint">当前没有静音会话</span>'}</div></div>
      <div class="card overview-policy"><div class="overview-section-head"><h2>宵禁</h2><span class="overview-policy-state ${o.curfew?.active?'is-resting':''}">${o.curfew?.active?'休息中':'可聊天'}</span></div><div class="overview-policy-content">${curfew||'<span class="hint">未设置，全天可聊</span>'}</div></div>
    </div>
    ${usageMarkup()}
  </div>`;
  decorateView();
  const note=$('.overview-chart-note'),chart=$('.overview-layout .usage-chart');
  if(note)note.ontoggle=()=>{OVERVIEW_NOTE_OPEN=note.open;};
  if(chart){chart.scrollLeft=OVERVIEW_CHART_SCROLL;chart.onscroll=()=>{OVERVIEW_CHART_SCROLL=chart.scrollLeft;};}
}

async function renderModel(){
  $("#view").innerHTML = `    <div class="card" id="keyCard"><h2 style="margin-bottom:6px">聊天与联网 · DeepSeek</h2>
      <div class="hint" id="keyState">读取中…</div>
      <div class="row" style="margin-top:8px"><div><label>API Key（聊天与联网共用）</label>
        <input type="password" autocomplete="off" id="keyInput" placeholder="sk-..."></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="keySave">保存</button>
      <button class="ghost" id="keyTest">检查聊天连接</button></div>
<div class="row" style="margin-top:10px"><button class="act" id="keyCap">重新抓数据库 key</button></div>
<p class="hint" id="keyCapMsg">换过微信号、重新装过微信、或 keys.json 被清空时点这个。点了会弹 UAC，然后切到微信点开任意聊天。</p>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:8px">
        <input type="checkbox" id="keyAllowDsh"> 允许读取本机 DeepSeek Harness 凭证（默认关；关掉后只认这里填的 Key 或环境变量）</label>
      <p class="hint" id="keyMsg"></p></div>`;
  $('#view').insertAdjacentHTML('beforeend', `<div class="card"><h2>联网搜索</h2><p class="hint">聊天和联网共用上方的 DeepSeek Key，无需单独配置模型或密钥。</p><label class="check-label"><input type="checkbox" id="webEnabled">允许根据问题搜索网页</label><p id="webState" class="hint">正在读取…</p><div class="row"><button class="act" id="webSave">保存联网设置</button><button class="ghost" id="webTest">检查联网（会产生一次调用）</button></div></div>`);
  decorateView();
  await Promise.all([bindKey(),bindWeb()]);
}

async function bindKey() {
  if (!$("#keyCard")) return;
  if (OVERVIEW?.backend === "native") {
    $("#keyCap").style.display = "none";
    $("#keyCapMsg").hidden = true;
  }
  const ks = await api("/api/apikey");
  if (!ks.ok || !$("#keyState")) return;
  $("#keyState").innerHTML = ks.set
      ? `当前：<b>${esc(ks.masked)}</b>　来源：${esc(ks.source)}`
      : "当前：<b>没有 Key</b>　（聊天会直接报错，先填一个）";
  const cb = $("#keyAllowDsh");
  if (cb) {
    cb.checked = !!ks.allowDsh;
    cb.onchange = async () => {
      const r = await api("/api/apikey", {allowDsh: cb.checked});
      if (r.ok) { toast(cb.checked ? "已允许读取本机 DSH 凭证" : "已关闭：只认这里填的 Key / 环境变量"); bindKey(); }
    };
  }
  $("#keySave").onclick = async () => {
    const r = await api("/api/apikey", {key: $("#keyInput").value});
    if (r.ok) { toast("已保存到 settings.json，下一轮就能用"); bindKey(); }
  };
  $("#keyTest").onclick = async () => {
    $("#keyMsg").textContent = "测试中…";
    const r = await api("/api/apikey/test", {key: $("#keyInput").value});
    if (!$("#keyMsg")) return;
    $("#keyMsg").textContent = r.ok ? "✅ 能用：" + (r.detail || "").slice(0, 120) : "❌ " + (r.error || "失败");
  };
}

async function renderTuning() {
  const j = await api("/api/tuning");
  if (TAB !== "tuning") return;
  if (!j.ok) return;
  TUNING = j;
  const groups = j.groups.map((g) => `
    <div class="card"><h2 style="margin-bottom:6px">${esc(g.title)}</h2>
      ${g.keys.includes("IDLE_TOPIC_P") ? `<p class="hint">主动抛话题和饭点报菜单仅在「活跃参与」模式下对已启用的群聊生效；「情绪参与」「与我有关」「点名参与」模式均不触发，私聊不触发。宵禁或群聊被禁言时也不发送。</p>
      <p class="hint">群里连续无消息达到 IDLE_TOPIC_AFTER 后，才按 IDLE_TOPIC_P 的概率尝试抛话题（0.9 表示每次尝试有 90% 的概率，并非发言频率）。未抽中时，间隔 IDLE_TOPIC_EVERY 秒再尝试；群里出现新消息（含她自己的发言）会重新计算静默时间。</p>` : ""}
      ${g.keys.map((k) => {
        const s = j.spec[k], v = j.values[k];
        if (s.options) return `<label>群聊参与方式</label>
          <select data-k="${k}">${s.options.map((name) => `<option value="${esc(name)}" ${v === name ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>
          <p class="hint">活跃参与：保持当前接话程度；情绪参与：主动回应明确的喜怒哀乐，普通陈述不接；与我有关：谈论或回应我时加入；点名参与：被 @ 或叫到别名才加入。被叫到都会发言，加入后无需反复点名，完成或换题后退出。除活跃参与外，不主动抛话题或报菜单。</p>`;
        return `<label title="${esc(s.desc)}">${k} <span class="hint">— ${esc(s.desc)}</span></label>
          <input type="number" step="${s.type === "int" ? "1" : "0.01"}" min="${s.min}" max="${s.max}"
                 data-k="${k}" value="${v}">`;
      }).join("")}
    </div>`).join("");
  $("#view").innerHTML = `<h2>参数 <span class="hint">保存后主循环下一轮生效，不用重启</span></h2>
    <div class="card"><h2>宵禁时间段</h2>
      <p class="hint">每天按北京时间执行。使用24小时制 HH:MM（00:00–23:59），例如 23:00–07:00。结束早于开始表示跨午夜；开始时刻进入宵禁，结束时刻恢复。未设置任何时段时，全天可聊。</p>
      <div id="curfewPeriods"></div>
      <button class="ghost" id="addCurfew" style="margin-top:10px">添加时段</button>
    </div>${groups}
    <div class="row"><button class="act" id="saveTuning">保存参数</button>
    <button class="ghost" id="resetTuning">恢复默认</button></div>
    <p class="hint">文件：${esc(j.file)}</p>`;
  function addPeriod(period = {start: "", end: ""}) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.curfew = "1";
    row.style.marginTop = "8px";
    row.innerHTML = `<div><label>开始时间（24小时制）</label><input type="text" data-time="start" inputmode="numeric" placeholder="23:00" maxlength="5" value="${esc(period.start)}"></div>
      <div><label>结束时间（24小时制）</label><input type="text" data-time="end" inputmode="numeric" placeholder="07:00" maxlength="5" value="${esc(period.end)}"></div>
      <button class="ghost danger" type="button">删除时段</button>`;
    row.querySelector("button").onclick = () => row.remove();
    $("#curfewPeriods").appendChild(row);
  }
  (j.values.CURFEW_PERIODS || []).forEach(addPeriod);
  $("#addCurfew").onclick = () => addPeriod();
  $("#saveTuning").onclick = async () => {
    const patch = {};
    patch.CURFEW_PERIODS = [...document.querySelectorAll("[data-curfew]")].map((row) => ({
      start: row.querySelector('[data-time="start"]').value,
      end: row.querySelector('[data-time="end"]').value,
    }));
    if (patch.CURFEW_PERIODS.some((p) => !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(p.start) || !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(p.end) || p.start === p.end)) {
      toast("每段须填写有效的24小时制时间 HH:MM，开始和结束不能相同", true);
      return;
    }
    document.querySelectorAll("[data-k]").forEach((el) => { patch[el.dataset.k] = el.value; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已保存：" + r.changed.join("、")); renderTuning(); }
  };
  $("#resetTuning").onclick = async () => {
    const patch = {};
    Object.keys(j.spec).forEach((k) => { patch[k] = j.spec[k].default; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已恢复默认"); renderTuning(); }
  };
}

async function renderLists(refresh = true) {
  if (refresh) {
    const [rj, wj, aj] = await Promise.all([api("/api/rooms"), api("/api/whitelist"), api("/api/admins")]);
    ROOMS = rj.rooms || []; WHITELIST = wj.whitelist || []; ADMINS = aj.admins || [];
  }
  const roomRows = ROOMS.map((r, i) => `<tr>
      <td><input type="text" data-r="${i}" data-f="name" value="${esc(r.name || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="room" value="${esc(r.room || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="account" value="${esc(r.account || "")}"></td>
      <td style="text-align:center"><input type="checkbox" data-r="${i}" data-f="enabled" ${r.enabled ? "checked" : ""}></td>
      <td><button class="ghost danger" data-del="${i}">删</button></td></tr>`).join("");
  const adminRows = ADMINS.map((r, i) => `<tr>
      <td><input type="text" aria-label="管理员备注名称" data-a="${i}" data-f="name" value="${esc(r.name || "")}"></td>
      <td><input type="text" aria-label="管理员微信 ID 或名字" data-a="${i}" data-f="wxid" value="${esc(r.wxid || "")}"></td>
      <td><button class="ghost danger" data-admin-del="${i}">删</button></td></tr>`).join("");
  $("#view").innerHTML = `
    <h2>白名单</h2>
    <p class="hint">白名单已经和会话合并：这张表就是白名单，<b>勾了「启用」才会听、才会发</b>；取消勾选等于移除。</p>
    <div class="card"><h2 style="margin-bottom:6px">会话 / 白名单</h2>
      <table style="width:100%;border-collapse:collapse"><thead><tr>
        <th align="left">名字<small class="list-column-hint">会话备注；room 留空时用此名称识别</small></th>
        <th align="left">会话 ID / 名字（room）<small class="list-column-hint">群 ID 或好友 wxid，也可填微信名称；保存时转为 ID</small></th>
        <th align="left">机器人账号（account）<small class="list-column-hint">接收和发送消息的机器人 wxid；留空自动使用当前账号</small></th>
        <th>启用<small class="list-column-hint">勾选后允许该会话收发消息</small></th><th></th>
      </tr></thead><tbody>${roomRows}</tbody></table>
      <div class="row" style="margin-top:10px"><button class="ghost" id="addRoom">+ 加一行</button>
      <button class="act" id="saveRooms">保存会话</button></div></div>
    <div class="card"><h2 style="margin-bottom:6px">管理员</h2>
      <p class="hint">每行一位管理员，只有名单中的人能使用 /闭嘴 等管理员指令；保存后下一轮生效。</p>
      <table style="width:100%;border-collapse:collapse"><thead><tr>
        <th align="left">备注名称<small class="list-column-hint">方便辨认；微信 ID 留空时用此名称识别</small></th>
        <th align="left">微信 ID / 名字<small class="list-column-hint">填写管理员的 wxid 或微信名称；保存时转为 ID</small></th><th></th>
      </tr></thead><tbody>${adminRows}</tbody></table>
      <div class="row" style="margin-top:10px"><button class="ghost" id="addAdmin">+ 加一行</button>
      <button class="act" id="saveAdmins">保存管理员</button></div></div>`;
  // 输入即保留到本地草稿，加行、删除或保存另一张表时不会丢失编辑。
  document.querySelectorAll('[data-r], [data-a]').forEach(el => el.oninput = () => {
    const rows = el.hasAttribute('data-r') ? ROOMS : ADMINS;
    const i = +(el.dataset.r ?? el.dataset.a);
    rows[i][el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
  });
  // 只重绘本地数据，不要重新拉服务端（否则刚加的空行会被覆盖掉）
  $("#addRoom").onclick = () => { ROOMS.push({name: "", room: "", account: "", enabled: true}); renderLists(false); };
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => { ROOMS.splice(+b.dataset.del, 1); renderLists(false); });
  $('#addAdmin').onclick = () => { ADMINS.push({name: '', wxid: ''}); renderLists(false); };
  document.querySelectorAll('[data-admin-del]').forEach(b => b.onclick = () => { ADMINS.splice(+b.dataset.adminDel, 1); renderLists(false); });
  $("#saveRooms").onclick = async () => {
    document.querySelectorAll("[data-r]").forEach((el) => {
      const i = +el.dataset.r, f = el.dataset.f;
      ROOMS[i][f] = f === "enabled" ? el.checked : el.value.trim();
    });
    ROOMS = ROOMS.filter((r) => r.room || r.name);   // 只填了名字也别丢（m09094）
    const r = await api("/api/rooms", {rooms: ROOMS});
    if (r.ok) { ROOMS = r.rooms; toast("会话已保存"); renderLists(false); }
  };
  $("#saveAdmins").onclick = async () => {
    const r = await api("/api/admins/save", {admins: ADMINS});
    if (r.ok) { ADMINS = r.admins; toast("管理员已保存（下一轮生效）"); renderLists(false); }
  };
}

let PERSONA_PANE = 'base';
async function renderPersona() {
  const [persona, result] = await Promise.all([api('/api/persona'),api('/api/taught-rules')]);
  if(TAB!=='persona'||!persona.ok||!result.ok)return;
  let teaching=result;
  $('#view').innerHTML=`<div class="persona-tabs" role="tablist" aria-label="人设类型">${[['base','基础人设'],['evolved','演化人设'],['taught','教导人设']].map(([kind,title])=>`<button type="button" role="tab" id="persona-tab-${kind}" aria-controls="persona-pane-${kind}" data-persona-pane="${kind}">${title}</button>`).join('')}</div>
    ${[['base','基础人设','固定身份、形象和基本规则，机器人不会自行修改。'],['evolved','演化人设','记录逐渐形成的性格、偏好与表达习惯，也可以在这里人工调整。'],['taught','教导人设','通过完整文字描述性格、语气、表达和做事习惯，可以在此直接编辑。']].map(([kind,title,hint])=>`<div class="card persona-editor" id="persona-pane-${kind}" role="tabpanel" aria-labelledby="persona-tab-${kind}" data-persona-editor="${kind}"><h2>${title}</h2><p class="hint">${hint}</p><textarea id="persona-${kind}" rows="16">${esc(kind==='taught'?teaching.text:persona[kind]||'')}</textarea>${kind==='base'?'<p class="hint">点名别名写在“# 别名”段，用顿号、逗号或换行分隔。</p>':kind==='taught'?'<p class="hint">回复条数、频率等运行参数在“互动参数”中调整。</p>':''}<div class="row" style="margin-top:16px"><button class="act" data-save-persona="${kind}">保存${title}</button></div></div>`).join('')}`;
  const selectPane=kind=>{
    PERSONA_PANE=kind;
    document.querySelectorAll('[data-persona-pane]').forEach(button=>{const active=button.dataset.personaPane===kind;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
    document.querySelectorAll('[data-persona-editor]').forEach(panel=>panel.hidden=panel.dataset.personaEditor!==kind);
  };
  const tabButtons=[...document.querySelectorAll('[data-persona-pane]')];
  tabButtons.forEach((button,index)=>{
    button.onclick=()=>selectPane(button.dataset.personaPane);
    button.onkeydown=event=>{let next;if(event.key==='ArrowRight')next=(index+1)%tabButtons.length;else if(event.key==='ArrowLeft')next=(index+tabButtons.length-1)%tabButtons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabButtons.length-1;else return;event.preventDefault();tabButtons[next].focus();selectPane(tabButtons[next].dataset.personaPane);};
  });
  selectPane(PERSONA_PANE);
  document.querySelectorAll('[data-save-persona]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try{
      const kind=button.dataset.savePersona,text=$('#persona-'+kind).value;
      const saved=await api(kind==='taught'?'/api/taught-rules/save':'/api/persona/save',kind==='taught'?{text,revision:teaching.revision}:{kind,text});
      if(saved.ok){if(kind==='taught')teaching=saved;toast('已保存，下一轮自动生效');}
    }catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  });
}

async function renderStickers() {
  const [j, rj] = await Promise.all([api("/api/stickers"), api("/api/rooms")]);
  if (!j.ok || !rj.ok || TAB !== "stickers") return;
  ROOMS = rj.rooms || [];
  const items = j.items || [];
  $("#view").innerHTML = `<h2>表情包（${items.length} 张）</h2>
    <div class="row" style="margin-bottom:12px"><div><label>发到哪个会话</label>
      <select id="stRoom">${roomOptions()}</select></div></div>
    <div class="stickers">${items.map((it) => `
      <div class="sticker"><img src="/sticker/${encodeURIComponent(it.file)}" loading="lazy">
        <div class="cap">${esc(it.what || "")}<br><span class="hint">${esc(it.scene || "")}</span></div>
        <div class="ops"><button class="ghost" data-send="${esc(it.file)}">发这张</button>
        <button class="ghost danger" data-del="${esc(it.file)}">删</button></div></div>`).join("") || "<p class='hint'>库是空的</p>"}
    </div>`;
  document.querySelectorAll("[data-send]").forEach((b) => b.onclick = async () => {
    const r = await api("/api/stickers/send", {file: b.dataset.send, room: $("#stRoom").value});
    if (r.ok) toast("发了：" + b.dataset.send);
  });
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("删掉 " + b.dataset.del + " ？")) return;
    const r = await api("/api/stickers/delete", {file: b.dataset.del});
    if (r.ok) { toast("已删"); renderStickers(); }
  });
}

async function renderTools() {
  const j = await api("/api/overview");
  if (TAB !== "tools") return;
  ROOMS = Object.entries(j.rooms || {}).map(([room, name]) => ({room, name}));
  $("#view").innerHTML = `<h2>消息与静音</h2>
    <div class="card"><h2 style="margin-bottom:6px">发一条消息</h2>
      <div class="row"><div><label>会话</label><select id="tRoom">${roomOptions()}</select></div></div>
      <label>内容</label><input type="text" id="tText" placeholder="测一下通不通">
      <div class="row" style="margin-top:10px"><button class="act" id="tSend">发送</button></div>
      <p class="hint">通过当前微信连接向所选会话发送消息。</p></div>
    <div class="card"><h2 style="margin-bottom:6px">静音</h2>
      <div class="row"><div><label>会话</label><select id="mRoom">${roomOptions()}</select></div>
      <div style="max-width:140px"><label>分钟</label><input type="number" id="mMin" value="10" min="1" max="1440"></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="mSet">设静音</button>
      <button class="ghost" id="mClear">全部解除</button></div>
      <p class="hint">群聊静音只停该群；私聊（管理员）静音是全体停、留管理员私聊。</p></div>
`;
  $("#tSend").onclick = async () => {
    const r = await api("/api/send", {room: $("#tRoom").value, text: $("#tText").value});
    if (r.ok) toast("已发送");
  };
  $("#mSet").onclick = async () => {
    const r = await api("/api/mute", {room: $("#mRoom").value, minutes: +$("#mMin").value});
    if (r.ok) toast("已设静音");
  };
  $("#mClear").onclick = async () => {
    const r = await api("/api/mute", {minutes: 0});
    if (r.ok) toast("已全部解除");
  };

}

async function renderLog() {
  const j = await api("/api/log?n=400");
  if (TAB !== "log") return;
  const lines = (j.lines || []).join("\n");
  $("#view").innerHTML = `<h2>日志（最后 400 行）</h2>
    <div class="card"><pre id="logBox">${esc(lines || "（还没有日志）")}</pre>
    <div class="row"><button class="ghost" id="reLog">刷新</button></div></div>`;
  const box = $("#logBox"); box.scrollTop = box.scrollHeight;
  $("#reLog").onclick = renderLog;
}

const USAGE_DAYS = 14;
let USAGE_DATA = null;
let OVERVIEW_NOTE_OPEN = false, OVERVIEW_CHART_SCROLL = 0;
const CHART_COLORS = ['#7ac7b3','#8ba9f0','#d8b77d','#c89de0','#e3919b','#7dc8df','#b9c67e','#a4adbb'];
function usageMarkup(){
  const data=USAGE_DATA;
  if(!data)return '<div class="card"><h2>每日 API 调用</h2><p class="hint">统计暂时不可用，请稍后刷新。</p></div>';
  const grouped=new Map();
  for(const row of data.rows){
    const id=JSON.stringify([row.provider,row.key_id]);
    const label=row.provider+' · '+row.key_label;
    if(!grouped.has(id))grouped.set(id,{id,label,values:{},total:0});
    const item=grouped.get(id);item.values[row.day]=(item.values[row.day]||0)+row.calls;item.total+=row.calls;
  }
  const series=[...grouped.values()].sort((a,b)=>b.total-a.total),daily=data.days.map(day=>series.reduce((sum,item)=>sum+(item.values[day]||0),0));
  const max=Math.max(...daily,1),step=Math.max(1,Math.ceil(max/4)),ceiling=step*4;
  const viewStyle=getComputedStyle($('#view'));
  const width=Math.max(700,Math.round($('#view').clientWidth-parseFloat(viewStyle.paddingLeft)-parseFloat(viewStyle.paddingRight)-48)),height=238,left=40,top=15,plotHeight=175,plotWidth=width-left-18,stride=plotWidth/data.days.length,barWidth=Math.min(26,stride*.5);
  let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="北京时间每日 API 调用次数柱状图">`;
  for(let tick=0;tick<=4;tick++){const value=step*tick,y=top+plotHeight-plotHeight*value/ceiling;svg+=`<line x1="${left}" x2="${width-18}" y1="${y}" y2="${y}" class="chart-grid"/><text x="${left-9}" y="${y+4}" text-anchor="end">${value}</text>`;}
  data.days.forEach((day,index)=>{
    const x=left+stride*index+(stride-barWidth)/2;let used=0;
    const description=day+'：'+daily[index]+' 次'+series.filter(item=>item.values[day]).map(item=>'；'+item.label+' '+item.values[day]+' 次').join('');
    svg+=`<g tabindex="0" aria-label="${esc(description)}"><title>${esc(description)}</title><rect x="${left+index*stride}" y="${top}" width="${stride}" height="${plotHeight}" fill="transparent"/>`;
    series.forEach((item,seriesIndex)=>{const count=item.values[day]||0;if(!count)return;const barHeight=count/ceiling*plotHeight;svg+=`<rect x="${x}" y="${top+plotHeight-used-barHeight}" width="${barWidth}" height="${barHeight}" fill="${CHART_COLORS[seriesIndex%CHART_COLORS.length]}"/>`;used+=barHeight;});
    svg+='</g>';
    if(index%Math.max(1,Math.ceil(data.days.length/14))===0||index===data.days.length-1)svg+=`<text x="${x+barWidth/2}" y="${top+plotHeight+25}" text-anchor="middle">${day.slice(5)}</text>`;
  });
  svg+='</svg>';
  const start=new Date(data.started_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  return `<div class="card usage-card"><div class="overview-section-head"><div><h2>每日 API 调用</h2><p class="overview-chart-subtitle">查看各密钥的每日调用分布</p></div><span class="overview-chart-range">近 14 天 · 按 API Key</span></div><div class="usage-legend">${series.map((item,index)=>`<span><i style="background:${CHART_COLORS[index%CHART_COLORS.length]}"></i>${esc(item.label)}<b>${item.total}</b></span>`).join('')||'<span class="hint">尚无调用记录</span>'}</div><div class="usage-chart">${svg}</div><details class="overview-chart-note" ${OVERVIEW_NOTE_OPEN?'open':''}><summary>统计说明<span>自 ${esc(start)} 起采集</span></summary><p class="hint">按北京时间自然日汇总。采集前的历史未计入；每次请求尝试计一次，重试与追加请求分别计数。页面读取和本地过滤不计数，密钥仅展示尾号与指纹。</p></details></div>`;
}
function renderBot(){
  const bot=OVERVIEW?.bot||{};
  $('#view').innerHTML=`<div class="card"><h2>自动回复服务</h2><p><span class="badge ${bot.running?'ok':'bad'}">${bot.running?'运行中':'已停止'}</span></p><p class="hint">启动或停止大肥鱼的自动回复。微信账号连接在“微信接入”中管理。</p><div class="row"><button class="act" id="botStart" ${bot.running?'disabled':''}>启动机器人</button><button class="ghost danger" id="botStop" ${!bot.running?'disabled':''}>停止机器人</button></div><p class="hint">${bot.age!=null?'最近心跳：'+bot.age+' 秒前':'尚未取得心跳'}</p></div>`;
  $('#botStart').onclick=()=>setBotRunning('start');$('#botStop').onclick=()=>setBotRunning('stop');
  decorateView();
}

let VIEW_TIMER = null;
let WECHAT_ATTEMPT = null;

async function renderWechat(){
  $('#view').innerHTML = `<div class="card"><h2>微信扫码连接</h2><p class="hint">使用大肥鱼的微信账号扫码，并在手机确认。连接后到“会话与管理员”管理参与会话。</p><div class="row"><button class="act" id="wechatStart">开始扫码</button><button class="ghost" id="wechatRefresh">刷新状态</button></div><p id="wechatState" role="status">正在读取连接状态…</p><img id="wechatQr" class="wechat-qr" alt="微信登录二维码" hidden><p class="hint">已有登录状态会自动恢复；新登录不会自动导入电脑微信历史。</p></div>`;
  const status = async()=>{
    const result = await api('/api/wechat-login'+(WECHAT_ATTEMPT?'?attempt='+encodeURIComponent(WECHAT_ATTEMPT):''));
    if(TAB!=='wechat'||!result.ok)return;
    $('#wechatStart').disabled=!!result.connected;
    $('#wechatState').textContent=result.connected?'已连接：'+result.wxid:WECHAT_ATTEMPT?'请扫码并在手机确认，状态：'+(result.operation?.state||'准备中'):'尚未连接，点击“开始扫码”生成二维码。';
    const qr=$('#wechatQr');qr.hidden=!!result.connected||!WECHAT_ATTEMPT;
    if(!qr.hidden)qr.src='/api/wechat-login/qr?attempt='+encodeURIComponent(WECHAT_ATTEMPT)+'&t='+Date.now();
  };
  $('#wechatQr').onerror=()=>{if($('#wechatQr'))$('#wechatQr').hidden=true;};
  $('#wechatStart').onclick=async()=>{
    const button=$('#wechatStart');button.disabled=true;
    try{const result=await api('/api/wechat-login/start',{});if(!result.ok)return;WECHAT_ATTEMPT=result.operation?.id||null;await status();}catch(error){toast(error.message,true);}finally{if(button.isConnected&& !WECHAT_ATTEMPT)button.disabled=false;}
  };
  $('#wechatRefresh').onclick=()=>status().catch(error=>toast(error.message,true));
  await status();
  if(TAB==='wechat')VIEW_TIMER=setInterval(()=>status().catch(()=>{if($('#wechatState'))$('#wechatState').textContent='连接状态读取失败，请刷新重试。';}),4000);
}

async function bindWeb(){
  const cloud=await api('/api/cloud');if(TAB!=='model'||!cloud.ok||!$('#webEnabled'))return;
  $('#webEnabled').checked=!!cloud.web;
  $('#webState').textContent=cloud.webConfigured?'已启用，使用上方配置的 DeepSeek Key':'未启用，或聊天密钥尚未配置';
  $('#webSave').onclick=async()=>{const result=await api('/api/cloud/save',{web:$('#webEnabled').checked});if(result.ok){toast('联网设置已保存');await bindWeb();}};
  $('#webTest').onclick=async()=>{
    const button=$('#webTest');button.disabled=true;$('#webState').textContent='正在检查联网…';
    try{const result=await api('/api/cloud/test',{});if(TAB==='model'&&$('#webState'))$('#webState').textContent=result.ok?'联网成功，返回 '+result.sourceCount+' 个来源':result.error;}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
}

async function renderVoice(){
  const cloud=await api('/api/cloud');if(TAB!=='voice'||!cloud.ok)return;
  $('#view').innerHTML=`<form id="voiceForm" class="card"><h2>语音服务</h2><p class="hint">语音识别和合成使用阿里云百炼北京地域服务；对话内容仍由 DeepSeek 生成。</p><p class="service-state">${cloud.configured?'百炼密钥已配置':'百炼密钥待配置'}</p><label for="voiceKey">百炼 API Key（留空保留原值）</label><input id="voiceKey" type="password" autocomplete="off" placeholder="填写新的 API Key"><label class="check-label"><input id="voiceInput" type="checkbox" ${cloud.voice?'checked':''}>识别新收到的微信语音</label><label class="check-label"><input id="voiceOutput" type="checkbox" ${cloud.voiceReply?'checked':''}>允许发送语音回复</label><p class="hint">启用后，需要识别的微信语音和需要合成的回复文字会提交给百炼服务。</p><div class="row"><button class="act" type="submit">保存语音设置</button><a href="https://help.aliyun.com/zh/model-studio/get-api-key" target="_blank" rel="noreferrer">获取百炼 API Key ↗</a></div></form><div class="card"><h2>大肥鱼的声音</h2><p>沿用已选定的第二版定制音色：软绵、懒洋洋，带鼻音，咬字松弛，尾音轻拖。</p><p class="hint">被要求用语音时发送语音，也可以自主选择语音表达。收到语音不强制回复语音。短哼唱会尝试无伴奏的旋律表达；暂不支持指定歌曲、完整演唱或伴奏。</p></div>`;
  $('#voiceForm').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;
    try{const result=await api('/api/cloud/save',{apiKey:form.querySelector('#voiceKey').value,voice:form.querySelector('#voiceInput').checked,voiceReply:form.querySelector('#voiceOutput').checked});if(result.ok){toast('语音设置已保存');if(TAB==='voice')await renderVoice();}}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
}

async function renderMemory(){
  const result=await api('/api/memory/status');if(TAB!=='memory')return;
  $('#view').innerHTML=`<div class="memory-stats"><div class="card"><span class="hint">经历记录</span><strong id="memoryEvents"></strong></div><div class="card"><span class="hint">知识主题</span><strong id="memoryTopics"></strong></div><div class="card"><span class="hint">待整理经历</span><strong id="memoryPending"></strong></div></div><div class="card"><h2>记忆检索</h2><p class="hint">群聊与私聊共享经历。原始记录及时保存，每天北京时间 04:00 集中整理；未整理内容仍可检索。</p><form id="memorySearch" class="row"><div><label for="memoryQuery">关键词</label><input id="memoryQuery" type="text" placeholder="输入人物、事件、知识或任务关键词"></div><button class="act" type="submit">查找记忆</button></form><div id="memoryResults" aria-live="polite"></div></div><div class="card"><h2>管理员任务摘要</h2><p class="hint">摘要来自记忆整理，任务实际进展以执行记录为准。</p><div id="memoryTasks"></div></div><details class="card"><summary>采集与整理状态</summary><pre id="memoryState"></pre></details>`;
  const update=data=>{
    if(TAB!=='memory'||!$('#memoryEvents'))return;
    $('#memoryEvents').textContent=data.events;$('#memoryTopics').textContent=data.topics;$('#memoryPending').textContent=data.pending;
    $('#memoryState').textContent=JSON.stringify(data.state,null,2);
    $('#memoryTasks').innerHTML=data.tasks?.length?data.tasks.map(task=>`<article class="memory-entry"><h3>${esc(task.topic)}</h3><pre>${esc(task.body)}</pre></article>`).join(''):'<p class="hint">暂未提取到管理员任务</p>';
  };
  update(result);
  $('#memorySearch').onsubmit=async event=>{
    event.preventDefault();const query=$('#memoryQuery').value,button=event.currentTarget.querySelector('button');button.disabled=true;
    try{const data=await api('/api/memory/search?q='+encodeURIComponent(query));if(TAB!=='memory')return;$('#memoryResults').innerHTML=data.items?.length?data.items.map(item=>`<article class="memory-entry"><h3>${esc(item.topic||'原始经历')}</h3><pre>${esc(item.body||'')}</pre><details><summary>来源与完整记录</summary><pre>${esc(JSON.stringify(item,null,2))}</pre></details></article>`).join(''):'<p class="hint">没有找到相关记录</p>';}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
  VIEW_TIMER=setInterval(()=>api('/api/memory/status').then(update).catch(()=>toast('记忆状态读取失败，请稍后重试',true)),30000);
}

const NAV_GROUPS = [
 {name:'运行中心',icon:'◉',items:[['overview','运行概览','查看微信连接、机器人状态与静音时段']]},
 {name:'陪伴管理',icon:'♡',items:[['lists','会话与管理员','设置参与会话、白名单与管理员'],['tuning','互动参数','管理群聊参与、回复方式与宵禁时段'],['persona','人格与成长','管理基础人设、演化人设和教导人设'],['memory','记忆与知识库','检索经历、知识和管理员任务摘要'],['stickers','表情包','浏览、发送和管理现有表情包']]},
 {name:'接入与服务',icon:'◇',items:[['bot','机器人控制','启动、停止与查看自动回复服务'],['wechat','微信接入','管理账号连接与扫码登录'],['model','聊天与联网','统一配置聊天密钥和联网搜索'],['voice','语音服务','管理语音识别、语音回复与服务密钥']]},
 {name:'系统管理',icon:'⚙',items:[['tools','消息与静音','向指定会话发送消息，管理静音状态'],['log','运行日志','查看最近的机器人运行记录']]}
];
const NAV_ENTRIES = Object.fromEntries(NAV_GROUPS.flatMap(group=>group.items.filter(([id])=>!id.startsWith('/')).map(([id,title,description])=>[id,{title,description,group:group.name}])));
const MOBILE_NAV = window.matchMedia('(max-width:760px)');
$('#navigation').innerHTML=NAV_GROUPS.map((group,index)=>`<div class="nav-group"><button class="nav-heading" aria-expanded="true" aria-controls="nav-${index}"><span class="nav-icon" aria-hidden="true">${group.icon}</span><span>${group.name}</span><span class="chevron" aria-hidden="true">⌄</span></button><div class="nav-items" id="nav-${index}">${group.items.map(([id,title])=>id.startsWith('/')?`<a href="${id}">${title} ↗</a>`:`<button data-tab="${id}">${title}</button>`).join('')}</div></div>`).join('');
function syncNavigation(){
 document.querySelectorAll('.nav-items').forEach(group=>{const expanded=!MOBILE_NAV.matches||!!group.querySelector(`[data-tab="${TAB}"]`);group.hidden=!expanded;group.previousElementSibling.setAttribute('aria-expanded',String(expanded));});
 document.querySelectorAll('nav [data-tab]').forEach(button=>{const active=button.dataset.tab===TAB;button.classList.toggle('on',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
}
function decorateView(){
 const view=$('#view');if(!view||view.querySelector('.page-heading'))return;
 const entry=NAV_ENTRIES[TAB];if(!entry)return;
 view.insertAdjacentHTML('afterbegin',`<div class="page-heading"><p class="eyebrow">控制台 / ${entry.group}</p><h1>${entry.title}</h1><p>${entry.description}</p></div>`);
 view.querySelectorAll('table').forEach(table=>{if(table.parentElement.classList.contains('table-scroll'))return;const wrap=document.createElement('div');wrap.className='table-scroll';table.replaceWith(wrap);wrap.appendChild(table);});
}
new MutationObserver(decorateView).observe($('#view'),{childList:true});
document.querySelectorAll('.nav-heading').forEach(button=>button.onclick=()=>{const expanded=button.getAttribute('aria-expanded')==='true';if(MOBILE_NAV.matches&&!expanded)document.querySelectorAll('.nav-items').forEach(group=>{group.hidden=true;group.previousElementSibling.setAttribute('aria-expanded','false');});button.setAttribute('aria-expanded',String(!expanded));document.getElementById(button.getAttribute('aria-controls')).hidden=expanded;});
document.querySelectorAll('nav [data-tab]').forEach(button=>button.onclick=()=>{if(location.hash.slice(1)===button.dataset.tab)openTab();else location.hash=button.dataset.tab;});
async function openTab(){
 clearInterval(VIEW_TIMER);VIEW_TIMER=null;
 const aliases={'/login':'wechat','/cloud':'model','/memory':'memory'};
 const id=location.hash.slice(1)||aliases[location.pathname];if(id==='teaching')PERSONA_PANE='taught';TAB=id==='teaching'?'persona':NAV_ENTRIES[id]?id:'overview';syncNavigation();
 $('#view').innerHTML='<div class="loading">正在读取…</div>';
 try{if(!OVERVIEW||TAB==='overview')await loadOverview();if(TAB!=='overview')await render();}catch(error){$('#view').innerHTML='<div class="card">'+esc(error.message)+'</div>';}
}
window.addEventListener('hashchange',openTab);MOBILE_NAV.addEventListener('change',syncNavigation);
openTab();
setInterval(()=>loadOverview().catch(()=>{$('#badgeBot').textContent='状态读取失败，请稍后重试';}),5000);
</script>

<script>
(function () {
  const cap = document.getElementById('keyCap');
  if (!cap) return;
  async function poll() {
    const r = await fetch('/api/keycap').then(x => x.json()).catch(() => null);
    const box = document.getElementById('keyCapMsg');
    if (!r || !box) return;
    if (r.running) {
      box.textContent = '抓取中…（' + (r.log || '').split('\n').slice(-2).join(' ') + '）';
      setTimeout(poll, 2000);
    } else if (r.log) {
      box.textContent = '上次抓取：' + (r.log || '').split('\n').slice(-3).join(' ');
    }
  }
  cap.onclick = async () => {
    const box = document.getElementById('keyCapMsg');
    box.textContent = '正在启动（同意 UAC 后切到微信点开任意聊天）…';
    const r = await fetch('/api/keycap', {method: 'POST', body: '{}'}).then(x => x.json()).catch(e => null);
    if (!r || !r.ok) { box.textContent = '启动失败：' + ((r && r.error) || '请求出错'); return; }
    poll();
  };
  poll();
})();
</script>
</body></html>
"""


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
