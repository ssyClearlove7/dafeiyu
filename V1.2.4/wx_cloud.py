"""Bailian speech recognition and DeepSeek Search; credentials stay local."""
import base64
import ctypes
import hashlib
import io
import json
import os
import threading
import time
import wave
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "cloud_settings.json"
CACHE = HERE / "voice_transcripts.json"
_lock = threading.Lock()
_retry_after = {}


def settings():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def api_key():
    return os.environ.get("DASHSCOPE_API_KEY", "").strip() or settings().get("apiKey", "").strip()


def enabled(feature):
    if settings().get(feature) is not True:
        return False
    if feature == "web":
        import wx_chat_loop as L
        try:
            return bool(L.api_key())
        except SystemExit:
            return False
    return bool(api_key())


def state():
    s = settings()
    return {"configured": bool(api_key()), "webConfigured": enabled("web"),
            "voice": s.get("voice", False), "voiceReply": s.get("voiceReply", False),
            "web": s.get("web", False)}


def save(body):
    with _lock:
        s = settings()
        if body.get("apiKey", "").strip():
            s["apiKey"] = body["apiKey"].strip()
        for feature in ("voice", "web", "voiceReply"):
            if feature in body:
                s[feature] = body.get(feature) is True
        temp = CONFIG.with_suffix(".tmp")
        temp.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(CONFIG)
    return state()


def _post(path, body):
    key = api_key()
    if not key:
        raise RuntimeError("请先在“联网与语音”页填写北京地域百炼 API Key")
    import api_usage
    api_usage.record("百炼", body.get("model", "未知模型"), key)
    r = requests.post("https://dashscope.aliyuncs.com/" + path, json=body,
                      headers={"Authorization": "Bearer " + key}, timeout=(10, 65))
    if not r.ok:
        raise RuntimeError("百炼请求失败，HTTP %d；请检查地域、模型权限和余额" % r.status_code)
    return r.json()


def search(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError("搜索词不能为空，且不能超过128字")
    import wx_chat_loop as L
    key = L.api_key()
    import api_usage
    api_usage.record("DeepSeek", "联网搜索", key)
    r = requests.post("https://api.deepseek.com/v1/tools/search",
                      headers={"Authorization": "Bearer " + key},
                      json={"query": query, "count": 5, "need_content": False}, timeout=(10, 40))
    if not r.ok:
        raise RuntimeError("DeepSeek搜索失败，HTTP %d；请检查余额或稍后重试" % r.status_code)
    rows = r.json().get("results") or []
    if not rows:
        raise RuntimeError("搜索未返回可核验来源，不能当作已查到事实")
    result = {"summary": "\n\n".join("%s（%s）：%s" % (x.get("title", ""), x.get("date") or "日期未知",
                       (x.get("summary") or x.get("snippet") or "")[:1500]) for x in rows[:5]),
            "sources": [{"title": x.get("title"), "url": x.get("url")} for x in rows[:5]]}
    import memory_store
    memory_store.remember_web(query, result)
    return result


def voice_wav(db, message, room):
    """Read the exact message's SILK blob using the existing WCDB API."""
    if "native_id" in message:
        import native_wechat
        path = native_wechat.media_path(message, db.client.profile()["ownUsername"])
        raw = Path(path).read_bytes()
        if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            if len(raw) > 7 * 1024 * 1024:
                raise RuntimeError("语音超过当前识别大小限制")
            return raw
        return _silk_wav(raw)
    f = db.lib.wcdb_get_voice_data
    f.argtypes = [ctypes.c_int64, ctypes.c_char_p, ctypes.c_int32, ctypes.c_int32,
                  ctypes.c_int64, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    f.restype = ctypes.c_int32
    out = ctypes.c_void_p()
    rc = f(db.handle, room.encode(), int(message.get("create_time") or 0),
           int(message.get("local_id") or 0), int(message.get("server_id") or 0),
           json.dumps([message.get("sender_username") or "", room]).encode(), ctypes.byref(out))
    try:
        if rc or not out.value:
            raise RuntimeError("微信语音尚未下载或读库失败")
        raw = bytes.fromhex(ctypes.string_at(out).decode("ascii"))
    finally:
        if out.value:
            db.lib.wcdb_free_string(out)
    return _silk_wav(raw)


def _silk_wav(raw):
    import pysilk
    pcm = io.BytesIO()
    pysilk.decode(io.BytesIO(raw), pcm, 24000)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm.getvalue())
    if len(wav.getvalue()) > 7 * 1024 * 1024:
        raise RuntimeError("语音超过当前识别大小限制")
    return wav.getvalue()


def transcribe(db, message, room, account):
    ident = "%s|%s|%s|%s|%s" % (account, room, message.get("server_id"), message.get("local_id"), message.get("create_time"))
    key = hashlib.sha256(ident.encode()).hexdigest()
    with _lock:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            cache = {}
        if key in cache:
            return cache[key]
    if not enabled("voice") or time.time() - int(message.get("create_time") or 0) > 300:
        return None
    if _retry_after.get(key, 0) > time.time():
        return None
    try:
        audio = voice_wav(db, message, room)
        data = _post("compatible-mode/v1/chat/completions", {
            "model": "qwen3-asr-flash", "stream": False,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
                "data": "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii")}}]}],
            "asr_options": {"enable_itn": True}})
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise RuntimeError("未识别到可用语音内容")
        with _lock:
            cache[key] = text
            temp = CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            temp.replace(CACHE)
        print("语音转写成功（%d字）" % len(text), flush=True)
        return text
    except Exception as e:
        _retry_after[key] = time.time() + 60
        print("语音转写失败: %s" % str(e)[:180], flush=True)
        return None


TOOL = {"type": "function", "function": {"name": "web_search", "description":
        "搜索网页获取最新新闻、天气、价格或需要核实的事实。用户要求搜索或问题有时效性时使用。普通闲聊不用。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 128}}, "required": ["query"]}}}

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>联网与语音</title>
<style>body{font:16px system-ui;background:#101720;color:#e7edf6;max-width:680px;margin:40px auto;padding:20px}input[type=password]{width:95%;padding:12px}label{display:block;margin:20px 0}button{padding:10px 20px;margin-right:12px}a{color:#8ec8ff}#status{white-space:pre-wrap}</style>
<a href="/">← 返回控制面板</a><h1>联网与语音</h1>
<p>联网搜索直接使用已配置的 DeepSeek Key，无需新增 Key。语音识别与合成使用阿里云百炼北京地域 Key。分别按服务商规则计费，对话仍由 DeepSeek 回复。</p>
<p>启用后，需要识别的微信语音和需要合成的回复文字会发给百炼；需要联网的问题会提交给 DeepSeek 搜索接口。</p>
<label>百炼语音 API Key（留空保留原值；仅联网无需填写）<input id="key" type="password" autocomplete="off" placeholder="sk-..."></label>
<label><input id="voice" type="checkbox"> 识别新收到的微信语音</label>
<label><input id="voiceReply" type="checkbox"> 允许发送语音回复（Ubuntu 原生微信）</label>
<p>大肥鱼使用已选定的第二版定制音色：软绵、懒洋洋的小女孩，带鼻音，咬字松弛，尾音轻拖。被要求用语音说时发语音，也可以自主选择用语音表达吃东西的拟声、轻声哼唱等。平时可继续发文字；收到语音不强制回语音。短哼唱会尝试有旋律、无伴奏的表达，效果需试听确认；暂不支持指定歌曲、完整演唱或伴奏。</p>
<label><input id="web" type="checkbox"> 允许根据问题搜索网页</label>
<button onclick="save()">保存</button><button onclick="check()">测试联网（会产生一次调用）</button><p id="status"></p>
<p>语音识别开启后，可发一条新语音验证；群聊仍按现有的称呼和触发规则回复。</p>
<a href="https://help.aliyun.com/zh/model-studio/get-api-key" target="_blank" rel="noreferrer">如何获取百炼 API Key</a>
<script>
async function load(){const x=await(await fetch('/api/cloud')).json();voice.checked=x.voice;voiceReply.checked=x.voiceReply;web.checked=x.web;statusText((x.webConfigured?'联网：已使用 DeepSeek Key':'联网：未启用或未配置 DeepSeek Key')+'；'+(x.configured?'语音：已配置百炼 Key':'语音：待填写百炼 Key'))}
function statusText(s){document.getElementById('status').textContent=s}
async function save(){try{let r=await(await fetch('/api/cloud/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apiKey:document.getElementById('key').value,voice:voice.checked,voiceReply:voiceReply.checked,web:web.checked})})).json();document.getElementById('key').value='';statusText(r.ok?'已保存，下一条新消息生效':r.error)}catch(e){statusText('保存失败，请检查面板是否运行')}}
async function check(){statusText('正在搜索验证…');try{let r=await(await fetch('/api/cloud/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();statusText(r.ok?'联网调用成功，返回 '+r.sourceCount+' 个来源':r.error)}catch(e){statusText('调用失败，请稍后重试')}}
load();</script></html>"""
