"""Bounded live PCM and local speech recognition."""
import io
import os
import threading
import wave

from fastapi import HTTPException

MAX_PCM = 10 * 1024 * 1024
MAX_TRANSCRIPTION = 1024 * 1024
TRANSCRIPTION_TIMEOUT = 60


class PCMBuffer:
    def __init__(self, attempt, sample_rate):
        self.attempt = attempt
        self.sample_rate = sample_rate
        self.data = bytearray()
        self.lock = threading.Lock()
        self.closed = False

    def append(self, data):
        with self.lock:
            if self.closed:
                raise RuntimeError("音频流已经结束")
            if len(data) % 2 or len(self.data) + len(data) > MAX_PCM:
                raise ValueError("音频流超出大小限制或采样无效")
            self.data.extend(data)

    def read(self, offset):
        with self.lock:
            return bytes(self.data[offset:offset + 65536]), self.closed

    def close(self):
        with self.lock:
            self.closed = True


def transcription_audio(data):
    if len(data) > MAX_TRANSCRIPTION:
        raise HTTPException(413, "转写录音不能超过 1 MiB。")
    try:
        with wave.open(io.BytesIO(data)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError
            frames = audio.getnframes()
            if not 8000 <= frames <= 480000:
                raise HTTPException(422, "转写录音时长需为 0.5–30 秒。")
            samples = audio.readframes(frames)
            if len(samples) != frames * 2:
                raise ValueError
            if not any(samples):
                raise HTTPException(422, "录音是静音，请重新录制。")
            return samples, frames / 16000
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(422, "请上传完整的 16kHz、单声道、PCM16 WAV。") from None


class Whisper:
    def load(self):
        from mlx_audio.stt.utils import load_model
        from transformers import WhisperProcessor
        self.model = load_model(os.getenv("STT_MODEL", "mlx-community/whisper-small-mlx"))
        if not self.model.is_multilingual:
            raise ValueError("需要支持中文的 Whisper 模型")
        if getattr(self.model, "_processor", None) is None:
            self.model._processor = WhisperProcessor.from_pretrained("openai/whisper-small")
        self.model.get_tokenizer(language="zh", task="transcribe")

    def transcribe(self, samples):
        import numpy as np
        result = self.model.generate(np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768,
                                     language="zh", task="transcribe", verbose=None,
                                     temperature=0.0,
                                     return_timestamps=True, condition_on_previous_text=False)
        segments = result.segments or []
        if segments and all(segment.get("no_speech_prob", 0) > 0.6 for segment in segments):
            return ""
        if any(segment.get("compression_ratio", 0) > 2.4 or segment.get("avg_logprob", 0) < -1 for segment in segments):
            raise HTTPException(422, "识别结果不可靠，请靠近麦克风、在安静环境重新录制。")
        return result.text

    def close(self):
        if hasattr(self, "model"):
            del self.model
