"""Live PCM and local transcription regressions without downloading models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

from audio_runtime import PCMBuffer, Whisper, MAX_TRANSCRIPTION, transcription_audio
from test_voice_app import Engine, Transcriber, create_app, wav_bytes


def recording(seconds=1, rate=16000, channels=1, width=2, silent=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes((b"\0" if silent else b"\x01") * int(seconds * rate * channels * width))
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "data"
        self.engine, self.transcriber = Engine(), Transcriber()
        self.app = create_app(self.directory, self.engine, lambda history: ("你好。", ""), transcriber=self.transcriber)
        self.service = self.app.state.service
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait(lambda: self.service.stt_status != "loading")
        self.voice = self.client.post("/api/voices", data={"name": "声音", "transcript": "你好"},
                                      files={"recording": ("reference.wav", wav_bytes(), "audio/wav")}).json()
        self.conversation = self.client.post("/api/conversations", json={"voice_id": self.voice["id"]}).json()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("operation timed out")

    def send(self):
        response = self.client.post(f"/api/conversations/{self.conversation['id']}/messages", json={"content": "你好"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def transcribe(self, data=None):
        return self.client.post("/api/transcriptions", files={"recording": ("input.wav", data if data is not None else recording(), "audio/wav")})

    def endpoint(self, path):
        return next(route.endpoint for route in self.app.routes if getattr(route, "path", None) == path)

    def test_completed_stream_matches_wav_and_checks_attempt(self):
        task = self.send()
        self.wait(lambda: not self.service.gate.locked())
        url = f"/api/audio/{task['id']}/stream?attempt={task['attempt']}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-audio-sample-rate"], "8000")
        self.assertEqual(response.headers["x-audio-channels"], "1")
        self.assertEqual(response.headers["x-audio-format"], "pcm_s16le")
        downloaded = self.client.get(f"/api/audio/{task['id']}?download=true")
        with wave.open(io.BytesIO(downloaded.content)) as audio:
            self.assertEqual(response.content, audio.readframes(audio.getnframes()))
        self.assertEqual(self.client.get(url.replace("attempt=1", "attempt=2")).status_code, 409)
        self.assertEqual(self.client.get(f"/api/audio/{uuid4()}/stream?attempt=1").status_code, 404)
        self.assertEqual(self.client.get("/api/audio/not-a-uuid/stream?attempt=1").status_code, 422)
        done = self.service.one("messages", task["id"])
        self.assertIsNotNone(done["timings"]["first_audio_ms"])

    def test_live_stream_before_finish_multiple_readers_and_disconnect(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.synthesize

        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            release.wait(5)
            return result

        async def connected():
            return False

        try:
            with patch.object(self.engine, "synthesize", blocked):
                task = self.send()
                self.assertTrue(entered.wait(2))
                async def consume():
                    endpoint = self.endpoint("/api/audio/{value}/stream")
                    request = SimpleNamespace(is_disconnected=connected)
                    first = endpoint(task["id"], request, task["attempt"])
                    second = endpoint(task["id"], request, task["attempt"])
                    left, right = await asyncio.gather(anext(first.body_iterator), anext(second.body_iterator))
                    self.assertEqual(left, right)
                    self.assertTrue(left)
                    self.assertFalse(release.is_set())
                    self.assertEqual(self.service.one("messages", task["id"])["status"], "synthesizing")
                    await first.body_iterator.aclose()
                    self.assertTrue(self.service.gate.locked())
                    release.set()
                    remaining = b"".join([chunk async for chunk in second.body_iterator])
                    self.assertEqual(remaining, b"")
                asyncio.run(consume())
        finally:
            release.set()
        self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(len(self.engine.calls), 1)

    def test_cancel_closes_stream_and_retry_isolated(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.synthesize
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set(); release.wait(5)
            return result
        async def connected():
            return False
        try:
            with patch.object(self.engine, "synthesize", blocked):
                task = self.send()
                self.assertTrue(entered.wait(2))
                async def consume():
                    response = self.endpoint("/api/audio/{value}/stream")(task["id"], SimpleNamespace(is_disconnected=connected), 1)
                    self.assertTrue(await anext(response.body_iterator))
                    self.service.cancel(task["id"])
                    self.assertEqual([chunk async for chunk in response.body_iterator], [])
                asyncio.run(consume())
                self.assertTrue(self.service.gate.locked())
                release.set()
        finally:
            release.set()
        self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(self.service.one("messages", task["id"])["status"], "audio_failed")
        self.assertEqual(self.client.post(f"/api/messages/{task['id']}/retry").status_code, 202)
        self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(self.client.get(f"/api/audio/{task['id']}/stream?attempt=1").status_code, 409)
        self.assertEqual(self.client.get(f"/api/audio/{task['id']}/stream?attempt=2").status_code, 200)

    def test_pcm_bound_and_failure_retains_text(self):
        buffer = PCMBuffer(1, 24000)
        with patch("audio_runtime.MAX_PCM", 4):
            buffer.append(b"1234")
            with self.assertRaises(ValueError): buffer.append(b"56")
        self.assertEqual(buffer.read(0), (b"1234", False))
        buffer.close()
        self.assertEqual(buffer.read(0), (b"1234", True))
        with patch("audio_runtime.MAX_PCM", 2):
            task = self.send()
            self.wait(lambda: not self.service.gate.locked())
        done = self.service.one("messages", task["id"])
        self.assertEqual(done["status"], "audio_failed")
        self.assertTrue(done["content"])
        self.assertFalse(list(self.service.audio.iterdir()))

    def test_transcription_valid_no_persistence(self):
        before = self.service.rows("SELECT * FROM messages")
        response = self.transcribe()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["text"], result["language"], result["duration"]), ("这是识别结果。", "zh", 1))
        self.assertGreaterEqual(result["timings"]["stt_ms"], 0)
        self.assertEqual(self.service.rows("SELECT * FROM messages"), before)
        self.assertEqual(len(list(self.service.recordings.iterdir())), 1)
        self.assertFalse(self.service.gate.locked())

    def test_transcription_validation_and_boundaries(self):
        for data in (b"invalid", recording(0.49), recording(30.01), recording(rate=8000),
                     recording(channels=2), recording(width=1), recording(silent=True), recording()[:-10]):
            with self.subTest(size=len(data)):
                self.assertEqual(self.transcribe(data).status_code, 422)
        self.assertEqual(self.transcribe(b"a" * (MAX_TRANSCRIPTION + 1)).status_code, 413)
        for seconds in (0.5, 30):
            self.assertEqual(self.transcribe(recording(seconds)).status_code, 200)
        self.assertEqual(self.client.post("/api/voices", data={"name": "声音", "transcript": "你好"},
                         files={"recording": ("test.wav", recording(1), "audio/wav")}).status_code, 422)

    def test_empty_long_and_provider_error(self):
        for text in ("", " " * 3, "字" * 2001, None):
            with patch.object(self.transcriber, "transcribe", return_value=text):
                self.assertEqual(self.transcribe().status_code, 422)
        with patch.object(self.transcriber, "transcribe", side_effect=RuntimeError("private payload")):
            response = self.transcribe()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private payload", response.text)
        self.assertFalse(self.service.gate.locked())

    def test_whisper_rejects_unreliable_output(self):
        engine = Whisper()
        engine.model = SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(
            text="字幕字幕字幕", segments=[{"no_speech_prob": 0.1, "compression_ratio": 4.4, "avg_logprob": -0.2}]))
        with self.assertRaises(HTTPException) as error:
            engine.transcribe(b"\x01\x00" * 16000)
        self.assertEqual(error.exception.status_code, 422)
        engine.model = SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(
            text="你好", segments=[{"no_speech_prob": 0.1, "compression_ratio": 1.0, "avg_logprob": -0.2}]))
        self.assertEqual(engine.transcribe(b"\x01\x00" * 16000), "你好")

    def test_transcription_timeout_keeps_ownership_until_exit(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(samples):
            entered.set(); release.wait(5)
            return "迟到的结果"
        try:
            with patch.object(self.transcriber, "transcribe", blocked), patch("voice_app.TRANSCRIPTION_TIMEOUT", 0.1):
                with ThreadPoolExecutor(1) as pool:
                    response = pool.submit(self.transcribe)
                    self.assertTrue(entered.wait(2))
                    health = self.client.get("/api/health").json()
                    self.assertEqual(health["active_kind"], "transcription")
                    self.assertTrue(health["busy"])
                    self.assertEqual(self.transcribe().status_code, 409)
                    self.assertEqual(self.client.post(f"/api/conversations/{self.conversation['id']}/messages", json={"content": "不并行"}).status_code, 409)
                    self.assertEqual(response.result(2).status_code, 504)
                    self.assertTrue(self.service.gate.locked())
                    release.set()
        finally:
            release.set()
        self.wait(lambda: not self.service.gate.locked())

    def test_disconnect_discards_transcription_but_retains_gate(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(samples):
            entered.set(); release.wait(5)
            return "迟到的结果"
        async def disconnected():
            await asyncio.to_thread(entered.wait, 2)
            return True
        async def invoke():
            with self.assertRaises(HTTPException) as error:
                await self.endpoint("/api/transcriptions")(SimpleNamespace(is_disconnected=disconnected),
                    UploadFile(filename="test.wav", file=io.BytesIO(recording())))
            self.assertEqual(error.exception.status_code, 499)
        try:
            with patch.object(self.transcriber, "transcribe", blocked):
                asyncio.run(invoke())
                self.assertTrue(self.service.gate.locked())
        finally:
            release.set()
        self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(self.service.rows("SELECT * FROM messages"), [])

    def test_failed_stt_does_not_disable_chat(self):
        self.service.stt_status = "failed"
        self.service.stt_error = "识别模型失败"
        self.assertEqual(self.transcribe().status_code, 503)
        task = self.send()
        self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(self.service.one("messages", task["id"])["status"], "complete")

    def test_model_load_failure_isolated_and_models_share_worker(self):
        calls = []
        class FailedTranscriber(Transcriber):
            def load(self):
                calls.append(threading.get_ident())
                raise RuntimeError("private loading failure")
            def close(self):
                calls.append(threading.get_ident())
        engine = Engine()
        app = create_app(self.directory / "other", engine, lambda history: ("你好", ""), transcriber=FailedTranscriber())
        with TestClient(app, base_url="http://127.0.0.1") as client:
            service = app.state.service
            self.wait(lambda: service.stt_status == "failed")
            health = client.get("/api/health").json()
            self.assertEqual(health["model_status"], "ready")
            self.assertNotIn("private loading failure", health["stt_error"])
            self.assertFalse(health["busy"])
        self.assertEqual(len(set(engine.threads + calls)), 1)


if __name__ == "__main__":
    unittest.main()
