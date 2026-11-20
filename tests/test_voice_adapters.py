"""Standalone adapter import checks for the Mac interaction app."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import UUID, uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient

from test_voice_app import Engine, create_app, wav_bytes
from training_exchange import PACKAGE_VERSION, RECIPE_VERSION, TrainingConfig
from voice_adapters import MAX_ARTIFACT
from voice_app import VOICE_TONE_INSTRUCTIONS


class AdapterEngine(Engine):
    def __init__(self):
        super().__init__()
        self.selected = []
        self.select_threads = []
        self.fail_adapter = False

    def select_adapter(self, folder):
        self.selected.append(folder)
        self.select_threads.append(threading.current_thread().name)
        if self.fail_adapter:
            raise RuntimeError("adapter load failed")


def adapter_package(*, adapter=b"adapter bytes", **overrides):
    config = TrainingConfig(steps=1, checkpoint_every=1).checked()
    result = {
        "format": PACKAGE_VERSION,
        "recipe_version": RECIPE_VERSION,
        "job_id": str(uuid4()),
        "attempt": 1,
        "base_model": os.getenv("BREEZE_MODEL", "mlx-community/Breeze-TTS-2-mlx-4bit"),
        "config": config,
        "base_digest": "a" * 64,
        "initial_adapter_digest": "b" * 64,
        "adapter_sha256": hashlib.sha256(adapter).hexdigest(),
        "steps": 1,
    }
    result.update(overrides)
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("result.json", json.dumps(result))
        archive.writestr("adapter.safetensors", adapter)
    return output.getvalue()


class VoiceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "data"
        self.engine = AdapterEngine()
        self.app = create_app(
            self.directory,
            self.engine,
            lambda history: ("这是一条测试回复。", ""),
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait_for_ready()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait_for_ready(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.client.get("/api/health").json()["model_status"] != "loading":
                return
            time.sleep(0.01)
        self.fail("timed out waiting for the model")

    def reference_voice(self):
        response = self.client.post(
            "/api/voices",
            data={"name": "参考声音", "transcript": "这是参考录音原文。"},
            files={"recording": ("reference.wav", wav_bytes(), "audio/wav")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def import_adapter(self, reference, package):
        return self.client.post(
            "/api/voice-adapters",
            data={"name": "训练声音", "reference_voice_id": reference["id"]},
            files={"artifact": ("result.zip", package, "application/zip")},
        )

    def wait_for_message(self, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.client.get(f"/api/messages/{message['id']}").json()
            if value["status"] in ("complete", "text_failed", "audio_failed"):
                return value
            time.sleep(0.01)
        self.fail("timed out waiting for the message")

    def test_import_creates_independent_voice_and_chat_uses_adapter(self):
        reference = self.reference_voice()
        source_path = self.directory / "recordings" / f"{reference['id']}.wav"
        original_audio = source_path.read_bytes()

        response = self.import_adapter(reference, adapter_package())
        self.assertEqual(response.status_code, 201, response.text)
        imported = response.json()
        self.assertNotEqual(imported["id"], reference["id"])
        self.assertEqual(imported["transcript"], reference["transcript"])
        self.assertEqual(UUID(imported["adapter_id"]).version, 4)
        self.assertIsNone(imported["model_id"])

        adapter_folder = self.directory / "voice_adapters" / imported["adapter_id"]
        self.assertEqual(self.engine.selected[-1], adapter_folder.resolve())
        self.assertTrue(self.engine.select_threads[-1].startswith("voice-inference"))
        self.assertEqual(
            (adapter_folder / "adapter.safetensors").read_bytes(),
            b"adapter bytes",
        )
        self.assertEqual((self.directory / "recordings" / f"{reference['id']}.wav").read_bytes(), original_audio)
        self.assertEqual(
            (self.directory / "recordings" / f"{imported['id']}.wav").read_bytes(),
            original_audio,
        )

        conversation = self.client.post("/api/conversations", json={"voice_id": imported["id"]})
        self.assertEqual(conversation.status_code, 201, conversation.text)
        message = self.client.post(
            f"/api/conversations/{conversation.json()['id']}/messages",
            json={"content": "你好"},
        )
        self.assertEqual(message.status_code, 202, message.text)
        finished = self.wait_for_message(message.json())
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(
            self.engine.calls[-1][1],
            (self.directory / "recordings" / f"{imported['id']}.wav").resolve(),
        )
        self.assertEqual(self.engine.selected[-1], adapter_folder.resolve())

    def test_invalid_or_incompatible_package_is_rejected_without_partial_data(self):
        reference = self.reference_voice()
        rejected = [
            b"not a zip",
            adapter_package(base_model="wrong/model"),
            adapter_package(adapter_sha256="c" * 64),
        ]
        for package in rejected:
            response = self.import_adapter(reference, package)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.client.get("/api/voices").json()), 1)
        self.assertFalse(list((self.directory / "voice_adapters").iterdir()))

    def test_adapter_without_reference_works_in_both_chat_modes(self):
        response = self.client.post(
            "/api/voice-adapters",
            data={"name": "无参考声音", "reference_voice_id": ""},
            files={"artifact": ("result.zip", adapter_package(), "application/zip")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        voice = response.json()
        self.assertEqual(voice["duration"], 0)
        self.assertEqual(voice["transcript"], "")
        self.assertFalse(list((self.directory / "recordings").iterdir()))
        adapter_folder = (self.directory / "voice_adapters" / voice["adapter_id"]).resolve()
        conversation = self.client.post("/api/conversations", json={"voice_id": voice["id"]})
        self.assertEqual(conversation.status_code, 201, conversation.text)
        for mode in ("standard", "realtime"):
            with self.subTest(mode=mode):
                call_count = len(self.engine.calls)
                reply = self.client.post(
                    f"/api/conversations/{conversation.json()['id']}/messages",
                    json={"content": "你好", "mode": mode},
                )
                self.assertEqual(reply.status_code, 202, reply.text)
                self.assertEqual(self.wait_for_message(reply.json())["status"], "complete")
                self.assertGreater(len(self.engine.calls), call_count)
                self.assertTrue(all(call[1:] == (None, None) for call in self.engine.calls[call_count:]))
                self.assertEqual(self.engine.selected[-1], adapter_folder)

        rejected = self.import_adapter(voice, adapter_package())
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(len(self.client.get("/api/voices").json()), 1)

    def test_tone_is_saved_per_voice_and_used_by_both_chat_modes(self):
        reference = self.reference_voice()
        imported = self.import_adapter(reference, adapter_package())
        self.assertEqual(imported.status_code, 201, imported.text)
        voice_id = imported.json()["id"]
        endpoint = f"/api/voices/{voice_id}/tone"
        self.assertEqual(self.client.patch(endpoint, json={"tone_preset": "unknown"}).status_code, 422)
        update = self.client.patch(endpoint, json={"tone_preset": "gentle"})
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["tone_preset"], "gentle")
        self.assertEqual(self.client.get(f"/api/voices/{reference['id']}").json()["tone_preset"], "default")
        conversation = self.client.post("/api/conversations", json={"voice_id": voice_id})
        self.assertEqual(conversation.status_code, 201, conversation.text)
        for mode in ("standard", "realtime"):
            with self.subTest(mode=mode):
                start = len(self.engine.instructions)
                message = self.client.post(
                    f"/api/conversations/{conversation.json()['id']}/messages",
                    json={"content": "你好", "mode": mode},
                )
                self.assertEqual(message.status_code, 202, message.text)
                self.assertEqual(self.wait_for_message(message.json())["status"], "complete")
                self.assertGreater(len(self.engine.instructions), start)
                self.assertEqual(set(self.engine.instructions[start:]), {VOICE_TONE_INSTRUCTIONS["gentle"]})

        self.assertEqual(self.client.patch(endpoint, json={"tone_preset": "default"}).json()["tone_preset"], "default")

    def test_adapter_load_failure_rolls_back_files_and_rows(self):
        reference = self.reference_voice()
        self.engine.fail_adapter = True
        response = self.import_adapter(reference, adapter_package())
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.client.get("/api/voices").json()), 1)
        self.assertFalse(list((self.directory / "voice_adapters").iterdir()))
        with closing(self.app.state.service.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_adapters").fetchone()[0], 0)
        self.assertEqual(len(list((self.directory / "recordings").glob("*.wav"))), 1)

    def test_oversized_package_is_rejected(self):
        reference = self.reference_voice()
        response = self.client.post(
            "/api/voice-adapters",
            data={"name": "训练声音", "reference_voice_id": reference["id"]},
            files={"artifact": ("result.zip", b"x" * (MAX_ARTIFACT + 1), "application/zip")},
        )
        self.assertEqual(response.status_code, 413, response.text)


if __name__ == "__main__":
    unittest.main()
