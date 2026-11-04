"""大肥鱼的语音表达：模型选择表达方式，发送器负责合成和发送。"""
import io
import re
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlparse

import requests

import config
import wx_cloud

TAG = re.compile(r"^\s*[\[【]语音(?:[：:]\s*([^\]】\r\n]*))?[\]】]\s*")
STYLES = frozenset(("说话", "吃东西", "哼唱"))
# 用户选定的第二版定制音色，绑定创建时的百炼账号与合成模型。
VOICE_MODEL = "cosyvoice-v3-flash"
VOICE_ID = "cosyvoice-v3-flash-vd-dafydrowsy-383c2a7def4149b1803f122742983180"
# 定制音色支持请求级指令；短哼唱仍需试听确认，不代表支持指定歌曲。
HUM_INSTRUCTION = "用轻柔慵懒的童声随口哼唱一小段旋律，音符有高低和长短变化，无伴奏，不要朗读或说话。"


def enabled():
    return config.wechat_backend() == "native" and wx_cloud.enabled("voiceReply")


def prompt():
    if not enabled():
        return ""
    return (
        "\n【语音表达】你可以发送微信语音。对方明确要求你用语音说、发语音、"
        "念给他听时，本次必须使用语音；对方没要求时，你也可以按自己的意愿和语境"
        "选择语音，例如吃东西的拟声、轻声哼唱、想用声音表达情绪。通常仍用文字，"
        "不因对方发了语音就必须回语音，不设随机概率。对方要求文字或不要语音时用文字。"
        "选择语音时第一行单独写[语音:说话]、[语音:吃东西]或[语音:哼唱]之一，"
        "随后只写真正要发声的内容；不要写动作旁白或把标记当成对白。"
        "音色是软绵、懒洋洋的小女孩，带鼻音，咬字松弛。吃东西可写‘啊呜、嚼嚼、唔、好好吃’等拟声词；"
        "对方明确要求随口哼几句、哼个小调或无伴奏短哼唱时，本次使用[语音:哼唱]。"
        "哼唱时随后只写一小段‘嗯、哼、啦’等哼唱音节，不混入对白、动作旁白、歌词或歌名；"
        "合成器会尝试有高低和长短变化的无伴奏旋律，效果尚需试听，不能声称已准确演唱。"
        "对方只说‘唱两句’且没有指定歌曲时，可以用短哼唱回应。"
        "当前没有指定歌曲演唱、翻唱或伴奏能力。对方要求演唱某首歌或完整歌曲时，"
        "只用文字简短说明暂时只能试着哼个小调，还不能唱指定歌曲；整个回复不得包含语音标记，"
        "不要改成朗读歌词，不要声称唱完了。"
        "真实咀嚼音效同样尚未接入。"
        "本次回复会合成一条语音，不要同时重复发相同文字。"
    )


def extract(lines):
    style, kept = None, []
    for line in lines:
        match = TAG.match(line)
        if match:
            requested = (match.group(1) or "").strip()
            style = style or (requested if requested in STYLES else "说话")
            if line[match.end():].strip():
                kept.append(line[match.end():].strip())
        else:
            kept.append(line)
    return style, kept


def synthesize(text, style="说话"):
    """返回临时 WAV 路径；调用方负责删除。不会发送微信消息。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise ValueError("语音内容须为1—600字")
    if style not in STYLES:
        raise ValueError("不支持的语音表达方式")
    voice_input = {"text": text.strip(), "voice": VOICE_ID,
                   "format": "wav", "sample_rate": 24000}
    if style == "哼唱":
        voice_input["instruction"] = HUM_INSTRUCTION
    data = wx_cloud._post("api/v1/services/audio/tts/SpeechSynthesizer", {
        "model": VOICE_MODEL, "input": voice_input})
    url = ((data.get("output") or {}).get("audio") or {}).get("url")
    parsed = urlparse(url or "")
    if parsed.scheme not in ("https", "http") or not (parsed.hostname or "").endswith(".aliyuncs.com"):
        raise RuntimeError("语音合成没有返回有效音频")
    audio = bytearray()
    with requests.get(url, timeout=(10, 60), stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(65536):
            audio.extend(chunk)
            if len(audio) > 7 * 1024 * 1024:
                raise RuntimeError("合成音频超过大小限制")
    with wave.open(io.BytesIO(audio), "rb") as wav:
        params = wav.getparams()
        pcm = wav.readframes(wav.getnframes())
        duration = len(pcm) / (params.framerate * params.nchannels * params.sampwidth)
        if not 0 < duration <= 60:
            raise RuntimeError("合成语音须在60秒内")
    # 百炼返回的 WAV 可能保留流式长度占位，按实际 PCM 重写文件头。
    normalized = io.BytesIO()
    with wave.open(normalized, "wb") as wav:
        wav.setparams(params)
        wav.writeframes(pcm)
    with tempfile.NamedTemporaryFile(prefix="dafy-voice-", suffix=".wav", delete=False) as output:
        path = Path(output.name)
        try:
            output.write(normalized.getvalue())
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return path
