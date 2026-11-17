"""Opt-in real Breeze integration. Never opens the application's private data.

RUN_BREEZE_TRAINING_HARDWARE=1 HF_HUB_OFFLINE=1 .venv/bin/python -m unittest -v test_training_hardware
Whisper is replaced with a deterministic transcript; transcription quality is not measured.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_voice_app import Transcriber
from voice_app import Breeze, create_app


@unittest.skipUnless(os.getenv("RUN_BREEZE_TRAINING_HARDWARE") == "1", "opt-in real Apple Silicon training")
class HardwareTrainingTests(unittest.TestCase):
    def test_prepare_cancel_resume_publish_and_restore(self):
        import mlx.core as mx
        started = time.perf_counter()
        artifacts = Path(__file__).resolve().parent / "web-test-artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="training-hardware-", dir=artifacts) as temporary:
            root = Path(temporary)
            subprocess.run(["say", "-o", str(root / "speech.aiff"), "Hello."], check=True)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(root / "speech.aiff"),
                            "-af", "apad=whole_dur=4", "-t", "4", str(root / "speech.mp3")], check=True)
            app = create_app(root / "data", Breeze(), lambda history: ("你好。", ""), Transcriber())
            service = app.state.service
            training = service.training

            def wait_for(predicate, seconds=240):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if result := predicate():
                        return result
                    time.sleep(0.05)
                self.fail("real training timed out")

            with TestClient(app, base_url="http://127.0.0.1") as client:
                wait_for(lambda: service.stt_status == "ready")
                dataset = client.post("/api/training/datasets", json={"name": "独立系统语音验收", "speaker_confirmed": True}).json()
                upload = client.post(f"/api/training/datasets/{dataset['id']}/sources", files={
                    "source": ("speech.mp3", (root / "speech.mp3").read_bytes(), "audio/mpeg")})
                self.assertEqual(upload.status_code, 201, upload.text)
                prepared = client.post(f"/api/training/datasets/{dataset['id']}/prepare")
                self.assertEqual(prepared.status_code, 202, prepared.text)
                wait_for(lambda: training.control is None)
                prepare = training.store.one("training_jobs", prepared.json()["id"])
                self.assertEqual(prepare["status"], "review_required", prepare)
                clips = client.get(f"/api/training/datasets/{dataset['id']}/segments").json()
                for clip in clips:
                    reviewed = client.patch(f"/api/training/segments/{clip['id']}", json={
                        "transcript": "Hello.", "review_status": "approved"})
                    self.assertEqual(reviewed.status_code, 200, reviewed.text)
                original = training.save_checkpoint
                interrupted = []

                def checkpoint(job, model, optimizer, step, base_digest):
                    original(job, model, optimizer, step, base_digest)
                    if not interrupted:
                        interrupted.append(step)
                        training.control[1].set()

                with patch.object(training, "save_checkpoint", side_effect=checkpoint):
                    submitted = client.post(f"/api/training/datasets/{dataset['id']}/jobs", json={
                        "steps": 2, "checkpoint_every": 1})
                    self.assertEqual(submitted.status_code, 202, submitted.text)
                    value = submitted.json()["id"]
                    wait_for(lambda: training.control is None)
                job = training.store.one("training_jobs", value)
                self.assertEqual(job["status"], "cancelled", job)
                self.assertEqual(job["steps_done"], 1)
                self.assertEqual(service.model_status, "ready")
                retried = client.post(f"/api/training/jobs/{value}/retry", json={"resume": True})
                self.assertEqual(retried.status_code, 202, retried.text)
                wait_for(lambda: training.control is None)
                job = training.store.one("training_jobs", value)
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual(job["steps_done"], 2)
                published = client.post(f"/api/training/jobs/{value}/publish", json={"name": "真实训练验收版本"})
                self.assertEqual(published.status_code, 201, published.text)
                voice = published.json()
                folder = service.directory / "voice_models" / voice["model_id"]
                conversation = client.post("/api/conversations", json={"voice_id": voice["id"]}).json()
                sent = client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"})
                self.assertEqual(sent.status_code, 202, sent.text)
                message_id = sent.json()["id"]
                wait_for(lambda: service.active is None)
                message = service.one("messages", message_id)
                self.assertEqual(message["status"], "complete", message)
                self.assertEqual(client.get(f"/api/audio/{message_id}").status_code, 200)
                self.assertEqual(service.engine.adapter_path, folder.resolve())
                service.worker.submit(service.engine.select_adapter, None).result()
                self.assertIsNone(service.engine.adapter_path)
                self.assertEqual(service.model_status, "ready")
                self.assertEqual(service.stt_status, "ready")
                self.assertFalse(service.gate.locked())
                report = {"status": "passed", "cancel_resume": True, "completed_steps": 2,
                          "publish": True, "trained_chat_audio": True,
                          "adapter_base_switch": True, "whisper": "test double",
                          "peak_mlx_bytes": mx.get_peak_memory(), "elapsed_seconds": time.perf_counter() - started}
                (artifacts / "training-service-hardware.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report), flush=True)


if __name__ == "__main__":
    unittest.main()
