"""固定服务的实际请求统一计数；本地缓存不计，失败和重试分别计。"""
import base64
import io
import json
import mimetypes
import socket
import ipaddress
import time
from urllib.parse import urlparse
import requests


class ModelError(RuntimeError):
    pass


class Models:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def request(self, feature, payload=None, files=None, form=None, binary=False):
        config = self.settings.read()["models"][feature]
        if not config["key"]:
            raise ModelError("请先在控制台配置" + feature + "服务密钥")
        payload = dict(payload or {}, model=config["model"]) if files is None else None
        form = dict(form or {}, model=config["model"]) if files is not None else None
        for attempt in range(config["retries"] + 1):
            # 构造失败属于本地未发出，不计次数。
            with requests.Session() as session:
                request = requests.Request("POST", config["url"],
                    headers={"Authorization": "Bearer " + config["key"]},
                    json=payload, files=files, data=form)
                prepared = session.prepare_request(request)
                ident = self.store.call_start(feature, config["model"], config["url"])
                started = time.monotonic()
                try:
                    with session.send(prepared, timeout=(10, config["timeout"]), allow_redirects=False, stream=True) as response:
                        if not 200 <= response.status_code < 300:
                            error = "服务HTTP " + str(response.status_code)
                            if response.status_code in (408, 429) or response.status_code >= 500:
                                raise requests.exceptions.RetryError(error)
                            raise ModelError(error)
                        chunks, total = [], 0
                        for chunk in response.iter_content(65536):
                            total += len(chunk)
                            if total > (25 if binary else 16) * 1024 * 1024:
                                raise ModelError("服务响应超过处理限制")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        value = raw if binary else json.loads(raw)
                        if not binary and value.get("error"):
                            raise ModelError("服务返回模型错误")
                        self.store.call_end(ident, True, time.monotonic() - started, value.get("usage") if isinstance(value, dict) else None)
                        return value
                except Exception as exc:
                    # 不能存储可能含密钥、媒体或提示词的供应商正文。
                    self.store.call_end(ident, False, time.monotonic() - started, error=type(exc).__name__)
                    if isinstance(exc, (requests.RequestException, json.JSONDecodeError)) and attempt < config["retries"]:
                        if files:
                            for file in files.values():
                                source = file[1] if isinstance(file, tuple) else file
                                if hasattr(source, "seek"):
                                    source.seek(0)
                        time.sleep(min(2 ** attempt, 4))
                        continue
                    if isinstance(exc, ModelError):
                        raise
                    raise ModelError("模型请求未完成：" + type(exc).__name__) from None

    def chat(self, messages, tools=None, max_tokens=1600, temperature=0.7, feature="chat"):
        body = {"messages": messages, "stream": False, "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
        value = self.request(feature, body)
        try:
            message = value["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError()
            return message
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型响应缺少有效消息") from None

    def json(self, system, value, max_tokens=1600):
        message = self.chat([{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(value, ensure_ascii=False)}],
                            max_tokens=max_tokens, temperature=0.1)
        text = message.get("content") or ""
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            result = json.loads(text)
        except ValueError:
            raise ModelError("模型未返回约定结构，操作未执行") from None
        if not isinstance(result, dict):
            raise ModelError("模型返回结构无效")
        return result

    def vision(self, images, prompt):
        content = [{"type": "text", "text": prompt}]
        for path in images:
            mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            content.append({"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode()}})
        return (self.chat([{"role": "user", "content": content}], max_tokens=2500, feature="vision").get("content") or "").strip()

    def transcribe(self, path):
        config = self.settings.read()["models"]["asr"]
        if config["protocol"] == "openai_transcription":
            with path.open("rb") as handle:
                result = self.request("asr", files={"file": (path.name, handle, "audio/wav")})
            text = result.get("text")
        else:
            content = [{"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()}}]
            result = self.request("asr", {"messages": [{"role": "user", "content": content}], "stream": False, "asr_options": {"enable_itn": True}})
            text = result.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(text, str) or not text.strip():
            raise ModelError("语音未返回可用转写")
        return text.strip()

    def speech(self, text, target, instruction=""):
        config = self.settings.read()["models"]["tts"]
        if not config.get("voice"):
            raise ModelError("请先配置语音合成音色")
        if config["protocol"] == "openai_speech":
            data = self.request("tts", {"input": text, "voice": config["voice"], "response_format": "wav"}, binary=True)
        else:
            source = {"text": text, "voice": config["voice"], "language_type": "Chinese"}
            if instruction:
                source["instruction"] = instruction
            value = self.request("tts", {"input": source})
            address = (value.get("output", {}).get("audio") or {}).get("url")
            parsed = urlparse(address or "")
            if parsed.scheme != "https" or not parsed.hostname:
                raise ModelError("语音服务未返回安全音频地址")
            # 服务返回的下载地址也不能访问本机或局域网资源。
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise ModelError("音频下载地址不可用")
            chunks, size = [], 0
            with requests.get(address, timeout=(10, 60), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise ModelError("语音结果超过限制")
                    chunks.append(chunk)
            data = b"".join(chunks)
        if not data:
            raise ModelError("语音服务返回空音频")
        target.write_bytes(data)
        return target

    def search(self, query):
        config = self.settings.read()["web"]
        if not config["enabled"] or not config["key"]:
            raise ModelError("联网搜索尚未配置")
        with requests.post(config["url"], json={"query": query}, headers={"Authorization": "Bearer " + config["key"]}, timeout=(10, 30), allow_redirects=False) as response:
            if response.status_code != 200:
                raise ModelError("联网搜索未完成")
            value = response.json()
        rows = (value.get("data") or {}).get("web") or value.get("results") or []
        return [{"title": str(x.get("title") or ""), "url": str(x.get("url") or ""), "snippet": str(x.get("snippet") or x.get("summary") or "")} for x in rows[:8]]
