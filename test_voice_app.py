"""API/state regression checks; no model downloads or external chat requests."""
from contextlib import closing
import io
import asyncio
import sqlite3
import shutil
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4
import wave

import httpx
from fastapi.testclient import TestClient
from voice_app import Breeze, MAX_WAV, TaskControl, TaskStopped, create_app as make_app, CHAT_MAX_TOKENS


class Transcriber:
    def load(self):
        pass

    def transcribe(self, samples):
        return "这是识别结果。"

    def close(self):
        pass


def create_app(*args, **kwargs):
    kwargs.setdefault("transcriber", Transcriber())
    return make_app(*args, **kwargs)


def wav_bytes(seconds=3):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x01\x00\xff" * int(8000 * seconds / 2))
    return output.getvalue()


class Engine:
    sample_rate = 8000
    def __init__(self):
        self.fail = False
        self.warning = ""
        self.calls = []
        self.threads = []

    def load(self):
        self.threads.append(threading.get_ident())

    def close(self):
        self.threads.append(threading.get_ident())

    def synthesize(self, text, reference, transcript, target, check=lambda: None, on_pcm=None):
        self.threads.append(threading.get_ident())
        self.calls.append((text, reference, transcript))
        target.write_bytes(wav_bytes())
        if on_pcm:
            with wave.open(str(target)) as audio:
                on_pcm(audio.readframes(audio.getnframes()))
        if self.fail:
            raise RuntimeError("fake synthesis failure")
        return 3, self.warning


class VoiceAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "data"
        self.engine = Engine()
        self.reply_fail = False
        self.reply_started = threading.Event()
        self.reply_release = threading.Event()
        self.reply_release.set()
        self.histories = []
        self.app = create_app(self.directory, self.engine, self.reply)
        self.open_client()

    def open_client(self):
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait_for(lambda: self.client.get("/api/health").json()["model_status"] != "loading")

    def tearDown(self):
        self.reply_release.set()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def reply(self, history):
        self.histories.append(history)
        self.reply_started.set()
        if not self.reply_release.wait(5):
            raise TimeoutError
        if self.reply_fail:
            raise RuntimeError("secret-provider-payload")
        return "这是一条测试回复。", ""

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        self.fail("timed out waiting for task")

    def voice(self):
        response = self.upload(wav_bytes())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def upload(self, data, name="测试声音", transcript="这是参考录音。"):
        return self.client.post("/api/voices", data={"name": name, "transcript": transcript},
                                files={"recording": ("../../not-a-file.wav", data, "audio/wav")})

    def conversation(self, voice=None):
        voice = voice or self.voice()
        response = self.client.post("/api/conversations", json={"voice_id": voice["id"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def send(self, conversation, content="你好"):
        response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": content})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def finished(self, task):
        value = task["id"]
        self.wait_for(lambda: not self.client.get("/api/health").json()["busy"])
        return self.client.get(f"/api/messages/{value}").json()

    def stream_chat(self, body, content_type="text/event-stream", status=200):
        service = self.app.state.service
        service.streaming = True
        service.base_url, service.llm_model = "https://chat.example/v1", "test"
        response = httpx.Response(status, headers={"Content-Type": content_type}, content=body)
        transport = httpx.MockTransport(lambda request: response)
        client_class = httpx.AsyncClient
        return patch("voice_app.httpx.AsyncClient", side_effect=lambda **kwargs: client_class(transport=transport, **kwargs))

    def test_idempotent_submission(self):
        conversation = self.conversation()
        endpoint = f"/api/conversations/{conversation['id']}/messages"
        headers = {"Idempotency-Key": str(uuid4())}
        self.reply_release.clear()
        first = self.client.post(endpoint, json={"content": "你好"}, headers=headers)
        second = self.client.post(endpoint, json={"content": " 你好 "}, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.client.post(endpoint, json={"content": "不同"}, headers=headers).status_code, 409)
        self.assertEqual(self.client.post(endpoint, json={"content": "你好"}, headers={"Idempotency-Key": "bad"}).status_code, 422)
        self.reply_release.set()
        self.finished(first.json())
        self.assertEqual(len(self.histories), 1)
        self.client.__exit__(None, None, None)
        self.app = create_app(self.directory, Engine(), self.reply)
        self.open_client()
        repeated = self.client.post(endpoint, json={"content": "你好"}, headers=headers)
        self.assertEqual(repeated.json()["id"], first.json()["id"])
        self.assertEqual(len(self.client.get(endpoint).json()), 2)

    def test_concurrent_idempotency_and_readiness_bypass(self):
        conversation = self.conversation()
        url = f"/api/conversations/{conversation['id']}/messages"
        headers = {"Idempotency-Key": str(uuid4())}
        self.reply_release.clear()
        with ThreadPoolExecutor(2) as callers:
            results = list(callers.map(lambda _: self.client.post(url, json={"content": "你好"}, headers=headers), range(2)))
        self.assertEqual([r.status_code for r in results], [202, 202])
        self.assertEqual(results[0].json()["id"], results[1].json()["id"])
        service = self.app.state.service
        service.model_status, service.configured = "failed", False
        self.assertEqual(self.client.post(url, json={"content": "你好"}, headers=headers).status_code, 202)
        service.model_status, service.configured = "ready", True
        self.reply_release.set()
        self.finished(results[0].json())
        self.assertEqual(len(self.histories), 1)

    def test_cancel_waits_for_actual_worker_exit(self):
        conversation = self.conversation()
        self.reply_release.clear()
        task = self.send(conversation)
        self.assertTrue(self.reply_started.wait(3))
        url = f"/api/messages/{task['id']}/cancel"
        self.assertEqual(self.client.post(url).status_code, 202)
        self.assertEqual(self.client.post(url).status_code, 202)
        self.assertTrue(self.client.get("/api/health").json()["busy"])
        self.assertEqual(self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "不应运行"}).status_code, 409)
        self.reply_release.set()
        done = self.finished(task)
        self.assertEqual((done["status"], done["error_code"]), ("text_failed", "cancelled"))
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(f"/api/messages/{uuid4()}/cancel").status_code, 404)
        self.assertEqual(self.client.post(f"/api/messages/{task['id']}/retry").status_code, 202)
        self.assertEqual(self.finished(task)["status"], "complete")

    def test_cancel_audio_before_publish_and_retry_metrics(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.engine.synthesize
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            release.wait(3)
            return result
        with patch.object(self.engine, "synthesize", blocked):
            task = self.send(self.conversation())
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.client.post(f"/api/messages/{task['id']}/cancel").status_code, 202)
            self.assertTrue(self.client.get("/api/health").json()["busy"])
            release.set()
            done = self.finished(task)
        self.assertEqual((done["status"], done["error_code"]), ("audio_failed", "cancelled"))
        self.assertTrue(done["content"])
        self.assertFalse(list((self.directory / "audio").iterdir()))
        self.client.post(f"/api/messages/{task['id']}/retry")
        done = self.finished(task)
        self.assertEqual(done["attempt"], 2)
        self.assertIsNone(done["timings"]["llm_ms"])
        self.assertGreaterEqual(done["timings"]["tts_ms"], 0)
        self.assertGreaterEqual(done["timings"]["rtf"], 0)
        self.assertEqual(done["duration"], 3)
        self.assertTrue(done["started_at"] and done["finished_at"])
        self.assertEqual(len(self.histories), 1)
        self.assertEqual(self.client.post(f"/api/messages/{task['id']}/cancel").status_code, 200)

    def test_total_timeout_keeps_serial_ownership(self):
        service = self.app.state.service
        service.task_timeout = .03
        self.reply_release.clear()
        task = self.send(self.conversation())
        self.assertTrue(self.reply_started.wait(3))
        time.sleep(.05)
        self.assertTrue(service.gate.locked())
        self.reply_release.set()
        done = self.finished(task)
        self.assertEqual(done["error_code"], "timeout")
        self.assertGreaterEqual(done["timings"]["llm_ms"], 30)

    def test_async_chat_cancel_and_stage_deadline(self):
        service = self.app.state.service
        service.reply = service.complete
        service.base_url, service.llm_model = "https://chat.example/v1", "test"
        entered, cancelled = threading.Event(), threading.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        client_class = httpx.AsyncClient
        with patch("voice_app.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw)):
            task = self.send(self.conversation())
            self.assertTrue(entered.wait(3))
            self.client.post(f"/api/messages/{task['id']}/cancel")
            self.assertEqual(self.finished(task)["error_code"], "cancelled")
            self.assertTrue(cancelled.is_set())
            with patch("voice_app.CHAT_TIMEOUT_SECONDS", .03):
                task = self.send(self.conversation())
                self.assertEqual(self.finished(task)["error_code"], "timeout")

    def test_dispatch_failure_compensates_and_can_retry(self):
        conversation = self.conversation()
        service = self.app.state.service
        with patch.object(service.worker, "submit", side_effect=RuntimeError("secret-dispatch")):
            response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"})
        self.assertEqual(response.status_code, 503)
        task = self.client.get(f"/api/conversations/{conversation['id']}/messages").json()[-1]
        self.assertEqual((task["status"], task["error_code"]), ("text_failed", "dispatch_failed"))
        self.assertFalse(service.gate.locked())
        self.client.post(f"/api/messages/{task['id']}/retry")
        self.assertEqual(self.finished(task)["status"], "complete")

    def test_escaped_background_error_and_safe_diagnostics(self):
        service = self.app.state.service
        with self.assertLogs("voice_app", level="INFO") as captured:
            with patch.object(service, "run", side_effect=RuntimeError("SECRET-KEY PRIVATE-CONTENT")):
                task = self.send(self.conversation())
                done = self.finished(task)
        self.assertEqual(done["status"], "text_failed")
        self.assertNotIn("SECRET-KEY", "".join(captured.output) + json.dumps(done))
        self.assertNotIn("PRIVATE-CONTENT", "".join(captured.output) + json.dumps(done))
        record = json.loads(captured.records[-1].getMessage())
        self.assertEqual(record["task_id"], task["id"])
        self.assertTrue(record["locations"])
        self.assertFalse(service.gate.locked())

    def test_storage_write_failure_disables_admission(self):
        service = self.app.state.service
        conversation = self.conversation()
        original = service.update
        def failing(value, **fields):
            if fields.get("status") == "complete":
                raise sqlite3.OperationalError("secret database error")
            return original(value, **fields)
        with patch.object(service, "update", failing):
            task = self.finished(self.send(conversation))
        self.assertEqual(task["error_code"], "storage_error")
        health = self.client.get("/api/health").json()
        self.assertFalse(health["accepting_tasks"])
        self.assertFalse(health["busy"])
        self.assertFalse(list((self.directory / "audio").iterdir()))
        self.assertEqual(self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"}).status_code, 503)

    def test_final_state_failure_is_visible_in_health(self):
        service = self.app.state.service
        conversation = self.conversation()
        original = service.update
        def failing(value, **fields):
            if "finished_at" in fields and fields["finished_at"] is not None:
                raise sqlite3.OperationalError("cannot save")
            return original(value, **fields)
        with patch.object(service, "update", failing):
            task = self.finished(self.send(conversation))
        self.assertFalse(self.client.get("/api/health").json()["accepting_tasks"])
        self.assertFalse(service.gate.locked())

    def test_file_publish_failure_and_stop(self):
        service = self.app.state.service
        original = Path.replace
        def failing(path, target):
            if path.parent == service.audio:
                raise OSError(28, "disk full")
            return original(path, target)
        with patch.object(Path, "replace", failing):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["status"], "audio_failed")
        self.assertFalse(list(service.audio.iterdir()))
        self.assertFalse(service.accepting_tasks)

    def test_breeze_cancellation_closes_generator(self):
        import numpy as np
        closed = []
        control = TaskControl(str(uuid4()), 1, 10)
        engine = Breeze()
        engine.model, engine.interval = SimpleNamespace(sample_rate=24000), .32
        def chunks(*args, **kw):
            try:
                yield np.ones(10, dtype=np.float32), 4, False
                control.cancel.set()
                yield np.ones(10, dtype=np.float32), 4, False
            finally:
                closed.append(True)
        with patch("breezetts2_mac_fast.pcm_chunks", chunks):
            with self.assertRaises(TaskStopped):
                engine.synthesize("test", "unused", "test", Path(self.temp.name)/"test.wav", control.check)
        self.assertEqual(closed, [True])

    def test_invalid_task_timeout_configuration(self):
        for value in ("0", "-1", "nan", "inf", "bad"):
            with patch.dict("os.environ", {"TASK_TIMEOUT_SECONDS": value}):
                with self.assertRaises(ValueError):
                    create_app(self.directory, Engine())

    def test_old_database_migration_and_backup(self):
        voice = self.voice()
        task = self.finished(self.send(self.conversation(voice)))
        old = Path(self.temp.name)/"old"
        shutil.copytree(self.directory, old)
        db = sqlite3.connect(old/"chat.sqlite3")
        db.execute("DROP INDEX message_request_key")
        for column in ("reasoning", "request_key", "error_code", "attempt", "started_at", "finished_at", "timings"):
            db.execute(f"ALTER TABLE messages DROP COLUMN {column}")
        db.commit()
        db.close()
        for backup in old.glob("before-tasks-*.sqlite3"):
            backup.unlink()
        app = create_app(old, Engine(), self.reply)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get(f"/api/messages/{task['id']}").json()["content"], task["content"])
            self.assertEqual(client.get(f"/api/audio/{task['id']}").status_code, 200)
            self.assertEqual(client.get("/api/voices").json()[0]["id"], voice["id"])
        backups = list(old.glob("before-tasks-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            columns = {r[1] for r in backup.execute("PRAGMA table_info(messages)")}
            self.assertNotIn("attempt", columns)
            self.assertNotIn("reasoning", columns)
        with TestClient(create_app(old, Engine(), self.reply), base_url="http://127.0.0.1"):
            pass
        self.assertEqual(len(list(old.glob("before-tasks-*.sqlite3"))), 1)

    def test_shutdown_cancels_then_closes_on_worker(self):
        service = self.app.state.service
        self.reply_release.clear()
        task = self.send(self.conversation())
        self.assertTrue(self.reply_started.wait(3))
        stopped = threading.Event()
        thread = threading.Thread(target=lambda: (service.stop(), stopped.set()))
        thread.start()
        self.wait_for(lambda: not service.accepting_tasks)
        self.assertFalse(stopped.is_set())
        self.assertTrue(service.gate.locked())
        self.reply_release.set()
        thread.join(3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(service.one("messages", task["id"])["error_code"], "cancelled")
        self.assertEqual(len(set(self.engine.threads)), 1)
        # Already stopped explicitly; prevent a second lifespan shutdown.
        with patch.object(service, "stop"):
            self.client.__exit__(None, None, None)
        self.app = create_app(self.directory, Engine(), self.reply)
        self.open_client()

    def test_total_timeout_during_synthesis_retains_text(self):
        service = self.app.state.service
        service.task_timeout = .04
        original = self.engine.synthesize
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            time.sleep(.06)
            return result
        with patch.object(self.engine, "synthesize", slow):
            done = self.finished(self.send(self.conversation()))
        self.assertEqual((done["status"], done["error_code"]), ("audio_failed", "timeout"))
        self.assertTrue(done["content"])
        self.assertFalse(list(service.audio.iterdir()))

    def test_migration_failure_rolls_back_and_releases_lease(self):
        self.voice()
        old = Path(self.temp.name)/"rollback"
        shutil.copytree(self.directory, old)
        with closing(sqlite3.connect(old/"chat.sqlite3")) as db:
            for column in ("error_code", "attempt", "started_at", "finished_at", "timings"):
                db.execute(f"ALTER TABLE messages DROP COLUMN {column}")
            db.commit()
        app = create_app(old, Engine(), self.reply)
        service = app.state.service
        class FaultConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if "ADD COLUMN attempt" in sql:
                    raise sqlite3.OperationalError("migration fault")
                return super().execute(sql, *args)
        def connect():
            db = sqlite3.connect(service.db, factory=FaultConnection)
            db.row_factory = sqlite3.Row
            return db
        with patch.object(service, "connect", connect):
            with self.assertRaises(sqlite3.OperationalError):
                service.start()
        with closing(sqlite3.connect(service.db)) as db:
            self.assertNotIn("error_code", {r[1] for r in db.execute("PRAGMA table_info(messages)")})
        with TestClient(create_app(old, Engine(), self.reply), base_url="http://127.0.0.1") as client:
            self.assertEqual(len(client.get("/api/voices").json()), 1)

    def test_uuid_persistence_and_conversation_binding(self):
        first, second = self.voice(), self.voice()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(UUID(first["id"]).version, 4)
        one, two = self.conversation(first), self.conversation(second)
        self.assertEqual(one["voice_id"], first["id"])
        self.assertNotEqual(one["id"], two["id"])
        self.assertTrue((self.directory / "recordings" / f"{first['id']}.wav").is_file())
        self.client.__exit__(None, None, None)
        self.app = create_app(self.directory, Engine(), self.reply)
        self.open_client()
        self.assertEqual(len(self.client.get("/api/voices").json()), 2)
        self.assertEqual(len(self.client.get("/api/conversations").json()), 2)

    def test_invalid_uploads_and_text(self):
        for data in (b"not a wav", wav_bytes(2), wav_bytes(31), wav_bytes()[:-5]):
            self.assertEqual(self.upload(data).status_code, 422)
        silence = bytearray(wav_bytes())
        silence[44:] = b"\x00" * (len(silence) - 44)
        self.assertEqual(self.upload(bytes(silence)).status_code, 422)
        self.assertEqual(self.upload(wav_bytes(), name=" ").status_code, 422)
        self.assertEqual(self.upload(wav_bytes(), transcript=" ").status_code, 422)
        self.assertEqual(self.upload(wav_bytes(), transcript="x" * 2001).status_code, 422)
        self.assertEqual(self.client.get("/api/voices").json(), [])
        conversation = self.conversation()
        for content in (" ", "x" * 2001, "bad\x00value"):
            response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": content})
            self.assertEqual(response.status_code, 422)

    def test_upload_size_limit(self):
        self.assertEqual(self.upload(b"x" * (MAX_WAV + 1)).status_code, 413)
        self.assertEqual(self.upload(b"x" * (MAX_WAV + 100000)).status_code, 413)

    def test_unknown_ids_and_path_escape(self):
        for path in (f"/api/messages/{uuid4()}", f"/api/audio/{uuid4()}", f"/api/conversations/{uuid4()}/messages"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post("/api/conversations", json={"voice_id": str(uuid4())}).status_code, 404)
        for value in ("bad", "..%2F..%2Fetc%2Fpasswd", "00000000-0000-0000-0000-000000000000"):
            self.assertIn(self.client.get(f"/api/audio/{value}").status_code, (404, 422))
        for path in ("/.env", "/voice_app.py", "/data/chat.sqlite3"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_complete_audio_history_and_single_worker(self):
        conversation = self.conversation()
        task = self.finished(self.send(conversation))
        self.assertEqual(task["status"], "complete")
        audio = self.client.get(f"/api/audio/{task['id']}")
        self.assertEqual(audio.status_code, 200)
        with wave.open(io.BytesIO(audio.content)) as wav:
            self.assertEqual(wav.getnframes(), 24000)
        download = self.client.get(f"/api/audio/{task['id']}?download=true")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.content, audio.content)
        self.finished(self.send(conversation, "第二个问题"))
        self.assertEqual([m["role"] for m in self.histories[-1]], ["system", "user", "assistant", "user"])
        self.assertEqual(len(set(self.engine.threads)), 1)
        self.assertEqual(self.engine.calls[0][2], "这是参考录音。")
        messages = self.client.get(f"/api/conversations/{conversation['id']}/messages").json()
        self.assertEqual(len(messages), 4)

    def test_global_busy_rejected_without_extra_messages(self):
        first, second = self.conversation(), self.conversation()
        self.reply_release.clear()
        task = self.send(first)
        self.assertTrue(self.reply_started.wait(3))
        response = self.client.post(f"/api/conversations/{second['id']}/messages", json={"content": "并发"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get(f"/api/conversations/{second['id']}/messages").json(), [])
        self.reply_release.set()
        self.assertEqual(self.finished(task)["status"], "complete")

    def test_text_failure_retry_without_duplicate_user(self):
        self.reply_fail = True
        conversation = self.conversation()
        task = self.finished(self.send(conversation))
        self.assertEqual(task["status"], "text_failed")
        self.assertNotIn("secret", task["error"])
        self.assertEqual(self.client.get(f"/api/audio/{task['id']}").status_code, 404)
        response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "再次"})
        self.assertEqual(response.status_code, 409)
        self.reply_fail = False
        response = self.client.post(f"/api/messages/{task['id']}/retry")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.finished(task)["status"], "complete")
        self.assertEqual(len(self.client.get(f"/api/conversations/{conversation['id']}/messages").json()), 2)
        self.assertEqual(self.client.post(f"/api/messages/{task['id']}/retry").status_code, 409)

    def test_audio_failure_keeps_text_and_retries_only_audio(self):
        self.engine.fail = True
        task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["status"], "audio_failed")
        self.assertEqual(task["content"], "这是一条测试回复。")
        self.assertEqual(len(list((self.directory / "audio").iterdir())), 0)
        self.engine.fail = False
        self.engine.warning = "语音达到上限，可能被截断。"
        self.assertEqual(self.client.post(f"/api/messages/{task['id']}/retry").status_code, 202)
        done = self.finished(task)
        self.assertEqual(done["status"], "complete")
        self.assertIn("截断", done["warning"])
        self.assertEqual(len(self.histories), 1)

    def test_restart_recovers_unfinished_tasks(self):
        task = self.finished(self.send(self.conversation()))
        service = self.app.state.service
        service.update(task["id"], status="synthesizing")
        second = self.finished(self.send(self.conversation()))
        service.update(second["id"], status="thinking", content="")
        (service.audio / "interrupted.tmp").write_bytes(b"partial")
        self.client.__exit__(None, None, None)
        self.app = create_app(self.directory, Engine(), self.reply)
        self.open_client()
        self.assertEqual(self.client.get(f"/api/messages/{task['id']}").json()["status"], "audio_failed")
        self.assertEqual(self.client.get(f"/api/messages/{second['id']}").json()["status"], "text_failed")
        self.assertEqual(list(service.audio.glob("*.tmp")), [])

    def test_symlink_cannot_expose_other_files(self):
        task = self.finished(self.send(self.conversation()))
        target = self.directory / "audio" / f"{task['id']}.wav"
        target.unlink()
        secret = Path(self.temp.name) / "secret.wav"
        secret.write_bytes(b"secret")
        target.symlink_to(secret)
        response = self.client.get(f"/api/audio/{task['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.content, b"secret")

    def test_local_origin_host_and_security_headers(self):
        response = self.client.post("/api/conversations", json={"voice_id": str(uuid4())}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "evil.example"}).status_code, 400)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("https://fonts.googleapis.com", csp)
        self.assertIn("https://images.higgs.ai", csp)
        self.assertEqual(self.client.get("/api/health").headers["cache-control"], "no-store")

    def test_real_chat_http_contract_and_truncation(self):
        service = self.app.state.service
        service.base_url, service.llm_model, service.key = "https://chat.example/v1", "test-model", "test-secret"
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "真实协议测试"}, "finish_reason": "length"}]})

        client_class = httpx.AsyncClient
        with patch("voice_app.httpx.AsyncClient", side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs)):
            text, warning = service.complete([{"role": "user", "content": "你好"}])
        self.assertEqual(text, "真实协议测试")
        self.assertIn("截断", warning)
        self.assertEqual(str(requests[0].url), "https://chat.example/v1/chat/completions")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-secret")
        self.assertEqual(json.loads(requests[0].content)["model"], "test-model")
        self.assertNotIn("test-secret", self.client.get("/api/health").text)

    def test_streaming_does_not_hide_or_shorten_provider_reply(self):
        long_reply = "这是提供方忽略约束的完整测试回复。" * 30
        body = ("data: " + json.dumps({"choices": [{"delta": {"content": long_reply}}]}, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
        with self.stream_chat(body):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["content"], long_reply)
        self.assertEqual(self.engine.calls[-1][0], long_reply)
        self.assertNotIn("已缩短", task["warning"])

    def test_output_budget_is_shared_and_memory_budget_is_separate(self):
        service = self.app.state.service
        service.base_url, service.llm_model = "https://chat.example/v1", "test"
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "简短回答。"}, "finish_reason": "stop"}]})
        client_class = httpx.AsyncClient
        with patch("voice_app.httpx.AsyncClient", side_effect=lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw)):
            service.complete([{"role": "user", "content": "请详细解释，并分十层回答"}])
            service.streaming = True
            task = self.finished(self.send(self.conversation(), "详细说说"))
            asyncio.run(service.request_chat([], {}, TaskControl(str(uuid4()), 1, 10), max_tokens=2000))
        self.assertEqual([p["max_tokens"] for p in payloads], [CHAT_MAX_TOKENS, CHAT_MAX_TOKENS, 2000])
        self.assertEqual([p["stream"] for p in payloads], [False, True, False])

    def test_default_prompt_is_chat_and_old_content_is_not_rewritten(self):
        task = self.finished(self.send(self.conversation(), "你觉得爱情是什么？"))
        prompt = self.histories[-1][0]["content"]
        self.assertIn("不是写文章", prompt)
        self.assertIn("禁止分点、分层、编号列表", prompt)
        self.assertIn("不得超过100字", prompt)
        self.assertIn("不放宽篇幅", prompt)
        self.assertIn("1至3句", prompt)
        self.assertEqual(task["content"], "这是一条测试回复。")

    def test_streaming_reasoning_and_content(self):
        body = "\n\n".join([
            'data: {"choices":[{"delta":{"reasoning_content":"先确认一下语气。"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"再补一句。"}}]}',
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            'data: {"choices":[{"delta":{"content":"，世界。"},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]).encode() + b"\n\n"
        with self.stream_chat(body):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["status"], "complete")
        self.assertEqual(task["content"], "你好，世界。")
        self.assertEqual(task["reasoning"], "先确认一下语气。再补一句。")

    def test_streaming_without_reasoning(self):
        body = "\n\n".join([
            'data: {"choices":[{"delta":{"content":"只有回答。"},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]).encode() + b"\n\n"
        with self.stream_chat(body):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["content"], "只有回答。")
        self.assertEqual(task["reasoning"], "")

    def test_streaming_json_fallback(self):
        body = json.dumps({"choices": [{"message": {"reasoning_content": "内部推理", "content": "降级回答"},
                                        "finish_reason": "stop"}]}).encode()
        with self.stream_chat(body, content_type="application/json"):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["content"], "降级回答")
        self.assertEqual(task["reasoning"], "内部推理")

    def test_streaming_error_after_partial_content_marks_text_failed(self):
        body = "\n\n".join([
            'data: {"choices":[{"delta":{"content":"部分"}}]}',
            'data: {"error":{"message":"secret-provider-payload"}}',
        ]).encode() + b"\n\n"
        with self.stream_chat(body):
            task = self.finished(self.send(self.conversation()))
        self.assertEqual(task["status"], "text_failed")
        self.assertEqual(task["content"], "部分")
        self.assertNotIn("secret", task["error"])

    def test_sse_stream_replays_complete_message(self):
        task = self.finished(self.send(self.conversation()))
        with self.client.stream("GET", f"/api/messages/{task['id']}/events") as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], task["id"])
        self.assertEqual(events[0]["content"], task["content"])

    def test_sse_stream_pushes_updates(self):
        task = self.finished(self.send(self.conversation()))
        service = self.app.state.service
        service.update(task["id"], status="thinking", content="", reasoning="先想")

        def update_later():
            time.sleep(0.25)
            service.update(task["id"], status="replying", content="你", reasoning="先想")
            time.sleep(0.25)
            service.update(task["id"], status="complete", content="你好", duration=1.0, reasoning="先想")

        updater = threading.Thread(target=update_later)
        updater.start()
        with self.client.stream("GET", f"/api/messages/{task['id']}/events") as response:
            received = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
        updater.join(5)
        self.assertFalse(updater.is_alive())
        self.assertEqual(received[0]["status"], "thinking")
        self.assertTrue(any(event["status"] == "replying" for event in received))
        self.assertEqual(received[-1]["status"], "complete")
        self.assertEqual(received[-1]["content"], "你好")
        self.assertEqual(received[-1]["reasoning"], "先想")

    def test_model_loading_failure_and_unconfigured_chat(self):
        service = self.app.state.service
        conversation = self.conversation()
        service.model_status = "loading"
        response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"})
        self.assertEqual(response.status_code, 503)
        service.model_status = "failed"
        self.assertEqual(self.client.get("/api/health").json()["model_status"], "failed")
        service.model_status = "ready"
        service.configured = False
        response = self.client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"})
        self.assertEqual(response.status_code, 503)

    def test_history_limited_to_twenty_successful_messages(self):
        conversation = self.conversation()
        for index in range(12):
            self.finished(self.send(conversation, f"问题{index}"))
        self.assertEqual(len(self.histories[-1]), 22)  # System + 20 history messages + current question.
        self.assertEqual(self.histories[-1][1]["content"], "问题1")

    def test_breeze_writes_pcm_and_reports_frame_limit(self):
        import numpy as np
        engine = Breeze()
        engine.model = SimpleNamespace(sample_rate=24000)
        engine.interval = 0.32
        target = Path(self.temp.name) / "generated.tmp"
        reference = Path(self.temp.name) / "reference.wav"

        def chunks(model, args, ref_audio=None, ref_text=None):
            self.assertEqual(ref_audio, str(reference))
            self.assertEqual(ref_text, "参考原文")
            self.assertEqual(args.text, "合成文字")
            yield np.array([0.0, 0.25, -0.25], dtype=np.float32), 750, True

        with patch("breezetts2_mac_fast.pcm_chunks", chunks):
            duration, warning = engine.synthesize("合成文字", reference, "参考原文", target)
        self.assertAlmostEqual(duration, 3 / 24000)
        self.assertIn("截断", warning)
        with wave.open(str(target)) as audio:
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getnframes(), 3)

    def test_provider_errors_are_failed_tasks_without_payload_leak(self):
        service = self.app.state.service
        service.reply = service.complete
        service.base_url, service.llm_model = "https://chat.example/v1", "test"
        client_class = httpx.AsyncClient
        for response in (httpx.Response(429, text="secret-provider-payload"),
                         httpx.Response(200, json={"choices": []}),
                         httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})):
            transport = httpx.MockTransport(lambda request: response)
            with patch("voice_app.httpx.AsyncClient", side_effect=lambda **kwargs: client_class(transport=transport, **kwargs)):
                task = self.finished(self.send(self.conversation()))
            self.assertEqual(task["status"], "text_failed")
            self.assertNotIn("secret", task["error"])

    def test_model_load_failure_and_data_directory_lock(self):
        service = self.app.state.service
        with patch.object(self.engine, "load", side_effect=RuntimeError("test load error")):
            with self.assertLogs("voice_app", level="ERROR"):
                service.load()
        self.assertEqual(self.client.get("/api/health").json()["model_status"], "failed")
        second = create_app(self.directory, Engine(), self.reply)
        with self.assertRaisesRegex(RuntimeError, "已有应用"):
            with TestClient(second, base_url="http://127.0.0.1"):
                pass


class LLMSettingsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": ""})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.open_client()

    def open_client(self):
        self.app = create_app(self.directory, Engine())
        self.service = self.app.state.service
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.service.worker.submit(lambda: None).result(timeout=5)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()
        self.env.stop()

    def save(self, **changes):
        body = {"base_url": "https://chat.example/v1/", "model": "test-model", "api_key": "test-secret"}
        body.update(changes)
        return self.client.post("/api/settings/llm", json=body)

    def test_save_restore_permissions_and_secret_redaction(self):
        self.assertFalse(self.client.get("/api/health").json()["llm_configured"])
        self.assertEqual(self.client.get("/api/settings/llm").json(),
                         {"base_url": "", "model": "", "api_key_set": False})
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        expected = {"base_url": "https://chat.example/v1", "model": "test-model", "api_key_set": True}
        self.assertEqual(response.json(), expected)
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        get = self.client.get("/api/settings/llm")
        self.assertEqual(get.json(), expected)
        self.assertEqual(get.headers["cache-control"], "no-store")
        path = self.directory / "llm-settings.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["api_key"], "test-secret")
        self.assertTrue(self.client.get("/api/health").json()["llm_configured"])
        self.client.__exit__(None, None, None)
        with patch.dict("os.environ", {"LLM_BASE_URL": "https://env.example", "LLM_MODEL": "env-model", "LLM_API_KEY": "env-secret"}):
            self.open_client()
        self.assertEqual(self.client.get("/api/settings/llm").json(), expected)
        self.assertEqual(self.service.key, "test-secret")

    def test_environment_fallback_until_first_save(self):
        self.client.__exit__(None, None, None)
        with patch.dict("os.environ", {"LLM_BASE_URL": "http://localhost:9000/v1/", "LLM_MODEL": "local", "LLM_API_KEY": "env-secret"}):
            self.open_client()
        self.assertEqual(self.client.get("/api/settings/llm").json(),
                         {"base_url": "http://localhost:9000/v1", "model": "local", "api_key_set": True})
        self.assertFalse((self.directory / "llm-settings.json").exists())
        self.assertEqual(self.save(base_url="http://localhost:9000/v1", api_key="").status_code, 200)
        self.assertEqual(self.service.key, "env-secret")

    def test_blank_preserves_replace_and_explicit_clear(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(api_key="", model="other-model").status_code, 200)
        self.assertEqual(self.service.key, "test-secret")
        self.assertEqual(self.save(api_key="replacement").status_code, 200)
        self.assertEqual(self.service.key, "replacement")
        self.assertEqual(self.save(api_key="", clear_api_key=True).status_code, 200)
        self.assertEqual(self.service.key, "")
        self.assertFalse(self.client.get("/api/settings/llm").json()["api_key_set"])
        self.client.__exit__(None, None, None)
        with patch.dict("os.environ", {"LLM_API_KEY": "env-secret"}):
            self.open_client()
        self.assertEqual(self.service.key, "")

    def test_address_change_requires_key_confirmation(self):
        self.save()
        response = self.save(base_url="https://other.example/v1", api_key="")
        self.assertEqual(response.status_code, 422)
        self.assertIn("重新输入", response.json()["detail"])
        self.assertEqual(self.service.base_url, "https://chat.example/v1")
        self.assertEqual(self.save(base_url="https://other.example/v1", api_key="new-secret").status_code, 200)
        self.assertEqual(self.service.key, "new-secret")
        self.assertEqual(self.save(base_url="http://localhost:9000", api_key="", clear_api_key=True).status_code, 200)
        self.assertEqual(self.service.key, "")

    def test_invalid_urls_fields_and_json_never_echo_secrets(self):
        invalid_urls = ["", "chat.example/v1", "ftp://chat.example", "https://", "http://bad host/v1",
                        "https://user:secret@chat.example/v1", "https://@chat.example", "https://chat.example/v1?key=secret",
                        "https://chat.example/v1#fragment", "https://chat.example/?", "https://chat.example/#",
                        "http://[::1", "http://localhost:bad", "http://localhost:99999", "http://localhost:0",
                        "https://chat.example\\evil", "https://chat.example/\n"]
        for url in invalid_urls:
            with self.subTest(url=url):
                response = self.save(base_url=url)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("test-secret", response.text)
        for changes in ({"model": " "}, {"model": "x" * 201}, {"api_key": "test-secret" * 500},
                        {"api_key": {"value": "test-secret"}}, {"api_key": "test-secret\n"},
                        {"clear_api_key": "true"}, {"clear_api_key": True}):
            response = self.save(**changes)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("test-secret", response.text)
        for body in ([], None, "test-secret"):
            response = self.client.post("/api/settings/llm", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("test-secret", response.text)
        response = self.client.post("/api/settings/llm", content='{invalid test-secret', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("test-secret", response.text)
        self.assertFalse((self.directory / "llm-settings.json").exists())

    def test_busy_chat_transcription_and_memory_reject_changes(self):
        self.save()
        original = (self.directory / "llm-settings.json").read_bytes()
        for kind, gate in (("chat", self.service.gate), ("transcription", self.service.gate),
                           ("memory", self.service.characters.gate)):
            with self.subTest(kind=kind):
                gate.acquire()
                try:
                    response = self.save(model="changed")
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("重试", response.json()["detail"])
                    self.assertTrue(gate.locked())
                    self.assertEqual(self.service.llm_model, "test-model")
                    self.assertEqual((self.directory / "llm-settings.json").read_bytes(), original)
                finally:
                    gate.release()
        self.assertEqual(self.save(model="changed").status_code, 200)

    def test_disk_failures_keep_previous_file_and_runtime(self):
        self.save()
        path = self.directory / "llm-settings.json"
        original = path.read_bytes()
        for target in ("voice_app.os.fsync", "voice_app.Path.replace", "voice_app.tempfile.NamedTemporaryFile"):
            with self.subTest(target=target), patch(target, side_effect=OSError("test-secret")):
                response = self.save(model="changed", api_key="new-secret")
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("secret", response.text)
                self.assertIn("原配置未更改", response.json()["detail"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual((self.service.llm_model, self.service.key), ("test-model", "test-secret"))
            self.assertFalse(self.service.gate.locked())
            self.assertEqual(list(self.directory.glob(".llm-settings-*.tmp")), [])

    def test_cleanup_failure_keeps_actionable_save_error_and_old_config(self):
        self.save()
        path = self.directory / "llm-settings.json"
        original = path.read_bytes()
        with patch("voice_app.os.fsync", side_effect=OSError("test-secret")), patch("voice_app.Path.unlink", side_effect=PermissionError("test-secret")):
            response = self.save(model="changed")
        self.assertEqual(response.status_code, 500)
        self.assertIn("原配置未更改", response.json()["detail"])
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.service.llm_model, "test-model")
        self.assertFalse(self.service.gate.locked())
        for temp in self.directory.glob(".llm-settings-*.tmp"):
            self.assertEqual(temp.stat().st_mode & 0o777, 0o600)

    def test_corrupt_saved_config_fails_without_overwriting_or_leaking(self):
        for content in ('{test-secret', '{"api_key":"test-secret"}',
                        json.dumps({"base_url": "ftp://invalid", "model": "test", "api_key": "test-secret"})):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "llm-settings.json"
                path.write_text(content)
                for _ in range(2):
                    app = create_app(directory, Engine())
                    with self.assertRaisesRegex(RuntimeError, "配置损坏") as raised:
                        with TestClient(app, base_url="http://127.0.0.1"):
                            pass
                    self.assertNotIn("test-secret", str(raised.exception))
                    self.assertEqual(path.read_text(), content)

    def test_new_chat_request_uses_saved_configuration_without_restart(self):
        self.service.streaming = False
        client_class = httpx.AsyncClient
        seen = []
        def provider(request):
            seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)["model"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": "测试回复。"}}]})
        transport = httpx.MockTransport(provider)
        with patch("voice_app.httpx.AsyncClient", side_effect=lambda **kwargs: client_class(transport=transport, **kwargs)):
            self.assertEqual(self.save().status_code, 200)
            self.assertEqual(self.service.complete([{"role": "user", "content": "你好"}])[0], "测试回复。")
            self.assertEqual(self.save(base_url="http://localhost:9000/v1", model="new-model", api_key="", clear_api_key=True).status_code, 200)
            self.service.complete([{"role": "user", "content": "你好"}])
        self.assertEqual(seen, [("https://chat.example/v1/chat/completions", "Bearer test-secret", "test-model"),
                                ("http://localhost:9000/v1/chat/completions", None, "new-model")])

    def test_encoded_url_length_and_unicode_restart_round_trip(self):
        response = self.save(base_url="https://chat.example/" + "中" * 300)
        self.assertEqual(response.status_code, 422)
        self.assertIn("编码后", response.json()["detail"])
        self.assertFalse((self.directory / "llm-settings.json").exists())
        response = self.save(base_url="https://chat.example/中文/v1/")
        self.assertEqual(response.status_code, 200)
        expected = response.json()
        self.assertIn("%E4%B8%AD", expected["base_url"])
        self.client.__exit__(None, None, None)
        self.open_client()
        self.assertEqual(self.client.get("/api/settings/llm").json(), expected)

    def test_settings_require_local_host_and_same_origin(self):
        for endpoint in ("/api/settings/llm",):
            self.assertEqual(self.client.get(endpoint, headers={"Host": "evil.example"}).status_code, 400)
            response = self.client.post(endpoint, json={}, headers={"Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get(endpoint, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
