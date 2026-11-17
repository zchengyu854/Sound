"""Training API/lifecycle checks, using isolated databases and generated audio."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4
import wave

from fastapi import HTTPException
from fastapi.testclient import TestClient

from test_voice_app import Engine, Transcriber, wav_bytes
from training_service import TrainingConfig, PreviewInput, Cancelled, canonical, select_preview_texts, training_summary
from voice_app import create_app


def metrics_wav_bytes():
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes((b"\x00\x20\x00\xe0") * 36000)
    return result.getvalue()


class AdapterEngine(Engine):
    def __init__(self):
        super().__init__()
        self.selected = []
        self.fail_adapter = False

    def select_adapter(self, folder):
        self.threads.append(threading.get_ident())
        self.selected.append(folder)
        if self.fail_adapter:
            raise RuntimeError("private-adapter-path")


class TrainingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = AdapterEngine()
        self.app = create_app(Path(self.temp.name) / "data", self.engine,
                              lambda history: ("测试回复。", ""), Transcriber())
        self.chat = self.app.state.service
        self.service = self.chat.training
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait(lambda: self.chat.stt_status == "ready")
        self.model_download = patch("voice_metrics.SpeakerEmbeddingModel.download", side_effect=OSError("offline"))
        self.model_download.start()
        self.addCleanup(self.model_download.stop)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if result := condition():
                return result
            time.sleep(0.01)
        self.fail("training timed out")

    def dataset(self, confirmed=True):
        result = self.client.post("/api/training/datasets", json={"name": "独立素材", "speaker_confirmed": confirmed})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def approved(self, confirmed=True):
        dataset = self.dataset(confirmed)
        store = self.service.store
        source_id, segment_id = str(uuid4()), str(uuid4())
        source = store.add_source(dataset["id"], source_id, "mp3", 100,
                                  {"duration": 3, "format": "mp3", "audio_stream": 0})
        store.update("training_sources", source_id, status="completed")
        folder = store.dataset_dir(dataset["id"]) / "segments"
        folder.mkdir(parents=True)
        (folder / f"{segment_id}.wav").write_bytes(wav_bytes())
        store.add_segment(dataset["id"], source_id, segment_id, 0, 3, "训练原文。")
        store.edit_segment(segment_id, review_status="approved")
        return dataset, segment_id

    def job(self, dataset):
        return self.service.store.create_job(dataset["id"], "train", TrainingConfig(steps=1).checked())
    def completed_job(self, note="", confirmed=False):
        dataset, _ = self.approved()
        job = self.job(dataset)
        root = self.service.job_directory(job)
        texts = select_preview_texts(job["snapshot"])
        audio = wav_bytes()
        hashes = {}
        for index in (1, 2):
            (root / f"validation-{index:02d}.wav").write_bytes(audio)
            hashes[f"{index:02d}"] = hashlib.sha256(audio).hexdigest()
        (root / "adapter.safetensors").write_bytes(b"adapter")
        adapter_hash = hashlib.sha256(b"adapter").hexdigest()
        validation = {"adapter_loaded": True, "base_model": self.service.base_model,
            "base_digest": "base-hash", "adapter_sha256": adapter_hash,
            "preview_texts": texts,
            "preview_texts_sha256": hashlib.sha256(canonical(texts).encode()).hexdigest(),
            "preview_audio_sha256": hashes, "validation_losses": [1.0],
            "training_summary": training_summary(job)}
        if confirmed:
            validation["confirmation"] = {"confirmed": True, "note": note,
                "adapter_sha256": adapter_hash,
                "preview_texts_sha256": validation["preview_texts_sha256"],
                "preview_audio_sha256": hashes,
                "confirmed_at": "2026-01-01T00:00:00+00:00"}
        self.service._write_validation(job, validation)
        self.service.store.update("training_jobs", job["id"], status="completed")
        return self.service.store.one("training_jobs", job["id"]), validation, texts

    def metrics_completed_job(self):
        dataset, segment_id = self.approved()
        store = self.service.store
        segment = store.one("training_segments", segment_id)
        audio = metrics_wav_bytes()
        store.segment_path(segment).write_bytes(audio)
        job = self.job(dataset)
        root = self.service.job_directory(job)
        texts = select_preview_texts(job["snapshot"])
        hashes = {}
        for index in (1, 2):
            (root / f"validation-{index:02d}.wav").write_bytes(audio)
            hashes[f"{index:02d}"] = hashlib.sha256(audio).hexdigest()
        (root / "adapter.safetensors").write_bytes(b"adapter")
        validation = {"adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
                      "preview_texts": texts,
                      "preview_texts_sha256": hashlib.sha256(canonical(texts).encode()).hexdigest(),
                      "preview_audio_sha256": hashes}
        self.service._write_validation(job, validation)
        store.update("training_jobs", job["id"], status="completed")
        return store.one("training_jobs", job["id"]), segment_id

    def test_metrics_preserve_quality_on_download_failure_and_stale_on_review(self):
        job, segment_id = self.metrics_completed_job()
        url = f"/api/training/datasets/{job['dataset_id']}/metrics"
        self.assertEqual(self.client.get(url).json()["status"], "not_started")
        self.assertEqual(self.client.post(url).status_code, 202)
        self.wait(lambda: not self.service.metrics_pending)
        result = self.client.get(url).json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["quality"]["original"]["status"], "ok")
        self.assertAlmostEqual(result["result"]["quality"]["original"]["dbfs"], -12.0412, places=3)
        self.assertEqual(result["result"]["features"]["status"], "failed")
        self.assertFalse(result["stale"])
        self.service.store.edit_segment(segment_id, transcript="修改后的原文。")
        self.assertTrue(self.client.get(url).json()["stale"])
        self.assertEqual(self.client.post(url).status_code, 202)
        self.wait(lambda: not self.service.metrics_pending)
        self.assertFalse(self.client.get(url).json()["stale"])

    def test_old_job_metrics_use_snapshot_and_reject_changed_preview(self):
        job, segment_id = self.metrics_completed_job()
        url = f"/api/training/jobs/{job['id']}/metrics"
        self.service.store.edit_segment(segment_id, transcript="之后修改的原文。")
        self.assertEqual(self.client.post(url).status_code, 202)
        self.wait(lambda: not self.service.metrics_pending)
        saved = self.client.get(url).json()
        segment = job["snapshot"]["segments"][0]
        material = [{"id": segment["id"], "sha256": segment["audio_sha256"], "transcript": segment["transcript"]}]
        self.assertEqual(saved["result"]["source"]["material_digest"], hashlib.sha256(canonical(material).encode()).hexdigest())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["result"]["source"]["reference_kind"], "training")
        self.assertEqual(saved["result"]["quality"]["generated"]["file_count"], 2)
        history = self.client.get(f"/api/training/datasets/{job['dataset_id']}/metrics/history").json()
        self.assertEqual(history["jobs"][0]["id"], job["id"])
        self.service.preview_file(job, 1).write_bytes(b"changed")
        self.assertEqual(self.client.post(url).status_code, 202)
        self.wait(lambda: not self.service.metrics_pending)
        changed = self.client.get(url).json()
        self.assertEqual(changed["status"], "failed")
        self.assertIn("音频", changed["message"])
        self.assertEqual(self.service.store.one("training_jobs", job["id"])["status"], "completed")

    def test_metrics_comparability_uses_snapshot_reference_text_and_revision(self):
        first, _ = self.metrics_completed_job()
        dataset = self.service.store.one("training_datasets", first["dataset_id"])
        validation = self.service._read_validation(first, required=True)

        def clone_job(text_change=False):
            job = self.job(dataset)
            source = self.service.job_directory(first)
            target = self.service.job_directory(job)
            for name in ("adapter.safetensors", "validation-01.wav", "validation-02.wav"):
                shutil.copyfile(source / name, target / name)
            copy = dict(validation)
            if text_change:
                copy["preview_texts"] = ["不同的试听句。", copy["preview_texts"][1]]
                copy["preview_texts_sha256"] = hashlib.sha256(canonical(copy["preview_texts"]).encode()).hexdigest()
            self.service._write_validation(job, copy)
            self.service.store.update("training_jobs", job["id"], status="completed")
            return job

        second, third = clone_job(), clone_job(text_change=True)

        def fake_features(model, references, previews):
            return {"model": {"revision": "a" * 40}, "model_error": None,
                    "references": [{"path": str(references[0]), "status": "ok"}],
                    "reference_consistency": {"reference_count": 1, "pair_count": 0, "score": None},
                    "generated": [{"score": score, "reference_count": 1, "status": "ok", "reason": None}
                                  for score in (80.0, 90.0)],
                    "generated_aggregate": {"sample_count": 2, "score": 85.0}}

        with patch("voice_metrics.SpeakerEmbeddingModel.download"), \
                patch.object(self.service, "metrics_features_on_worker", side_effect=fake_features):
            for job in (first, second, third):
                self.assertEqual(self.client.post(f"/api/training/jobs/{job['id']}/metrics").status_code, 202)
                self.wait(lambda: not self.service.metrics_pending)
        results = [self.client.get(f"/api/training/jobs/{job['id']}/metrics").json()["result"]
                   for job in (first, second, third)]
        self.assertEqual(results[0]["features"]["original_index"], None)
        self.assertEqual(results[0]["features"]["generated_index"], 85.0)
        self.assertEqual(results[0]["features"]["reference_count"], 1)
        self.assertTrue(results[0]["comparison_key"])
        self.assertEqual(results[0]["comparison_key"], results[1]["comparison_key"])
        self.assertNotEqual(results[0]["comparison_key"], results[2]["comparison_key"])

    def test_metrics_wait_for_model_and_keep_gate_until_worker_returns(self):
        job, _ = self.metrics_completed_job()
        entered, release, training_entered = threading.Event(), threading.Event(), threading.Event()
        thread_ids = []

        def features(model, references, previews):
            thread_ids.append(threading.get_ident())
            self.assertTrue(self.chat.gate.locked())
            entered.set()
            release.wait(5)
            raise ValueError("mock failure")

        def train(job, cancel, resume):
            thread_ids.append(threading.get_ident())
            training_entered.set()
            self.service.stage(job["id"], "cancelled")

        with patch("voice_metrics.SpeakerEmbeddingModel.download"), \
                patch.object(self.service, "metrics_features_on_worker", side_effect=features), \
                patch.object(self.service, "train", side_effect=train):
            try:
                self.client.post(f"/api/training/jobs/{job['id']}/metrics")
                self.assertTrue(entered.wait(5))
                pending = self.service.submit(job["dataset_id"], "train", TrainingConfig(steps=1).checked())
                self.assertFalse(training_entered.wait(0.1))
                self.assertTrue(self.chat.gate.locked())
                release.set()
                self.assertTrue(training_entered.wait(5))
                self.wait(lambda: self.service.control is None and not self.service.metrics_pending)
                self.assertEqual(self.service.store.one("training_jobs", pending["id"])["status"], "cancelled")
                self.assertEqual(len(set(thread_ids)), 1)
                self.assertFalse(self.chat.gate.locked())
            finally:
                release.set()

    def test_api_validation_and_local_origin(self):
        capabilities = self.client.get("/api/training/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(capabilities.json()["batch_size"], 1)
        self.assertEqual(self.client.post("/api/training/datasets", json={"name": " "}).status_code, 422)
        self.assertEqual(self.client.get("/api/training/datasets/not-uuid/segments").status_code, 422)
        self.assertEqual(self.client.get("/api/training/jobs?limit=201").status_code, 422)
        self.assertEqual(self.client.post("/api/training/datasets", json={"name": "test"},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
        dataset = self.dataset()
        for body in ({"rank": 3}, {"steps": 0}, {"learning_rate": 1}, {"command": "rm"}):
            self.assertEqual(self.client.post(f"/api/training/datasets/{dataset['id']}/jobs", json=body).status_code, 422)
        self.assertEqual(self.client.post(f"/api/training/datasets/{dataset['id']}/sources",
                         files={"source": ("fake.wav", b"fake", "audio/wav")}).status_code, 422)

    def test_review_gate_idempotency_and_snapshot(self):
        dataset, segment_id = self.approved(False)
        response = self.client.post(f"/api/training/datasets/{dataset['id']}/jobs", json={"steps": 1})
        self.assertEqual(response.status_code, 422)
        confirmed = self.client.patch(f"/api/training/datasets/{dataset['id']}", json={"speaker_confirmed": True})
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.json()["speaker_confirmed"])
        key = str(uuid4())
        with patch.object(self.service, "_dispatch"):
            first = self.client.post(f"/api/training/datasets/{dataset['id']}/jobs", json={"steps": 1}, headers={"Idempotency-Key": key})
            second = self.client.post(f"/api/training/datasets/{dataset['id']}/jobs", json={"steps": 1}, headers={"Idempotency-Key": key})
            conflict = self.client.post(f"/api/training/datasets/{dataset['id']}/jobs", json={"steps": 2}, headers={"Idempotency-Key": key})
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(conflict.status_code, 409)
        edited = self.client.patch(f"/api/training/segments/{segment_id}", json={"transcript": "新的原文。"})
        self.assertEqual(edited.status_code, 200)
        job = self.client.get(f"/api/training/jobs/{first.json()['id']}").json()
        self.assertEqual(job["snapshot"]["segments"][0]["transcript"], "训练原文。")
        self.assertEqual(len(self.client.get("/api/training/jobs?limit=1").json()), 1)
        self.service.verify_snapshot(job)
        self.service.store.segment_path(job["snapshot"]["segments"][0]).write_bytes(b"tampered")
        with self.assertRaises(HTTPException):
            self.service.verify_snapshot(job)

    def test_wait_cancel_and_resource_ownership(self):
        dataset, _ = self.approved()
        self.chat.gate.acquire()
        job = self.service.submit(dataset["id"], "train", TrainingConfig(steps=1).checked())
        with self.assertRaises(HTTPException) as busy:
            self.chat.reserve(False)
        self.assertEqual(busy.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            self.chat.start_transcription(b"\0" * 16000, 0.5)
        self.service.cancel(job["id"])
        self.wait(lambda: self.service.control is None)
        self.assertEqual(self.service.store.one("training_jobs", job["id"])["status"], "cancelled")
        self.assertTrue(self.chat.gate.locked())  # The previous owner still owns it.
        self.chat.gate.release()

    def test_cancel_does_not_release_running_model_early(self):
        dataset, _ = self.approved()
        entered, release = threading.Event(), threading.Event()

        def running(job, cancel, resume):
            self.service.stage(job["id"], "training")
            entered.set()
            release.wait(5)
            self.service.check(cancel)

        with patch.object(self.service, "train", side_effect=running):
            job = self.service.submit(dataset["id"], "train", TrainingConfig(steps=1).checked())
            self.assertTrue(entered.wait(5))
            self.service.cancel(job["id"])
            self.assertTrue(self.chat.gate.locked())
            self.assertEqual(self.service.store.one("training_jobs", job["id"])["status"], "cancelling")
            release.set()
            self.wait(lambda: self.service.control is None)
        self.assertFalse(self.chat.gate.locked())
        self.assertEqual(self.service.store.one("training_jobs", job["id"])["status"], "cancelled")

    def test_checkpoint_roundtrip_and_incompatible_resume(self):
        import mlx.core as mx
        import mlx.optimizers as optim
        from mlx.utils import tree_unflatten
        from breeze_training import attach_lora, train_step, load_adapter, parameter_digest
        from test_breeze_training import TinyBreeze
        dataset, _ = self.approved()
        job = self.job(dataset)
        model = TinyBreeze()
        attach_lora(model)
        optimizer = optim.Adam(1e-4)
        train_step(model, optimizer, mx.ones((1, 2, 64)), mx.array([[[1, 2, 3]]]))
        self.service.save_checkpoint(job, model, optimizer, 1, "base-hash")
        job = self.service.store.one("training_jobs", job["id"])
        metadata = self.service.validate_checkpoint(job)
        self.assertEqual(metadata["step"], 1)
        folder = self.service.checkpoint_directory(job)
        restored = TinyBreeze()
        attach_lora(restored)
        load_adapter(restored, folder / "adapter.safetensors")
        restored_optimizer = optim.Adam(1e-4)
        restored_optimizer.state = tree_unflatten(list(mx.load(str(folder / "optimizer.safetensors")).items()))
        self.assertEqual(parameter_digest(optimizer.state), parameter_digest(restored_optimizer.state))
        self.assertEqual(parameter_digest(model.trainable_parameters()), parameter_digest(restored.trainable_parameters()))
        metadata["digest"] = "wrong"
        (folder / "metadata.json").write_text(json.dumps(metadata))
        self.service.store.update("training_jobs", job["id"], status="failed")
        response = self.client.post(f"/api/training/jobs/{job['id']}/retry", json={"resume": True})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.service.control)
    def test_publish_requires_confirmed_preview(self):
        job, _, texts = self.completed_job()
        detail = self.client.get(f"/api/training/jobs/{job['id']}").json()
        self.assertEqual(detail["preview"]["texts"], texts)
        self.assertFalse(detail["preview"]["confirmed"])
        blocked = self.client.post(f"/api/training/jobs/{job['id']}/publish", json={"name": "未试听"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("试听", blocked.json()["detail"])
        self.assertEqual(self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm",
                                          json={"confirmed": False}).status_code, 422)
        first = self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm",
                                 json={"confirmed": True, "note": "音色接近"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["confirmed"])
        self.assertEqual(first.json()["note"], "音色接近")
        repeat = self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm",
                                  json={"confirmed": True, "note": "音色接近"})
        self.assertEqual(repeat.json()["confirmed_at"], first.json()["confirmed_at"])
        published = self.client.post(f"/api/training/jobs/{job['id']}/publish", json={"name": "已试听"})
        self.assertEqual(published.status_code, 201, published.text)

    def test_training_preview_generates_greeting_even_when_in_training_data(self):
        dataset, segment_id = self.approved()
        self.service.store.edit_segment(segment_id, transcript="你好，好久不见。今天天气不错，我们周末去公园走走吧。", review_status="approved")
        job = self.job(dataset)

        class PreviewModel:
            sample_rate = 24000

            def __init__(self):
                self.texts = []
                self.references = []

            def generate(self, text, max_tokens, stream, ref_audio, ref_text):
                self.texts.append(text)
                self.references.append((ref_audio, ref_text))
                yield SimpleNamespace(audio=[0.2, -0.2])

        model = PreviewModel()
        preview = self.service.validate_audio(model, job, threading.Event())
        self.assertEqual(preview["texts"][0], "你好，好久不见")
        self.assertEqual(model.texts, preview["texts"])
        self.assertNotIn(preview["texts"][1], job["snapshot"]["segments"][0]["transcript"])
        reference = job["snapshot"]["segments"][0]
        self.assertEqual(model.references, [(str(self.service.store.segment_path(reference)), reference["transcript"])] * 2)

        root = self.service.job_directory(job)
        (root / "adapter.safetensors").write_bytes(b"adapter")
        self.service._write_validation(job, {
            "adapter_sha256": hashlib.sha256(b"adapter").hexdigest(),
            "preview_texts": preview["texts"], "preview_texts_sha256": preview["texts_sha256"],
            "preview_audio_sha256": preview["audio_sha256"],
        })
        self.service.store.update("training_jobs", job["id"], status="completed")
        detail = self.client.get(f"/api/training/jobs/{job['id']}").json()
        self.assertEqual(detail["preview"]["texts"], preview["texts"])
        response = self.client.get(f"/api/training/jobs/{job['id']}/preview/1")
        self.assertEqual(response.status_code, 200)
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            self.assertEqual(wav.getframerate(), model.sample_rate)
            self.assertGreater(wav.getnframes(), 0)
        confirmed = self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm", json={"confirmed": True})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_existing_preview_keeps_its_original_text_and_audio(self):
        job, validation, _ = self.completed_job()
        validation["preview_texts"] = ["旧试听一。", "旧试听二。"]
        validation["preview_texts_sha256"] = hashlib.sha256(canonical(validation["preview_texts"]).encode()).hexdigest()
        self.service._write_validation(job, validation)
        detail = self.client.get(f"/api/training/jobs/{job['id']}").json()
        self.assertEqual(detail["preview"]["texts"], validation["preview_texts"])
        self.assertEqual(self.client.get(f"/api/training/jobs/{job['id']}/preview/1").status_code, 200)
        confirmed = self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm", json={"confirmed": True})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_preview_access_and_tamper_detection(self):
        job, _, _ = self.completed_job()
        preview = self.client.get(f"/api/training/jobs/{job['id']}/preview/1")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["content-type"], "audio/wav")
        self.assertEqual(self.client.get(f"/api/training/jobs/{job['id']}/preview/3").status_code, 422)
        self.assertEqual(self.client.get("/api/training/jobs/not-uuid/preview/1").status_code, 422)
        dataset, _ = self.approved()
        prepare = self.service.store.create_job(dataset["id"], "prepare", {})
        self.assertEqual(self.client.get(f"/api/training/jobs/{prepare['id']}/preview/1").status_code, 409)

        forged, validation, _ = self.completed_job()
        validation["confirmation"] = {"confirmed": True, "note": "",
            "adapter_sha256": "wrong", "preview_texts_sha256": validation["preview_texts_sha256"],
            "preview_audio_sha256": validation["preview_audio_sha256"],
            "confirmed_at": "2026-01-01T00:00:00+00:00"}
        self.service._write_validation(forged, validation)
        forged_publish = self.client.post(f"/api/training/jobs/{forged['id']}/publish", json={"name": "伪造确认"})
        self.assertEqual(forged_publish.status_code, 409)
        self.assertIn("不匹配", forged_publish.json()["detail"])

        root = self.service.job_directory(job)
        (root / "validation-01.wav").write_bytes(wav_bytes() + b"tampered")
        blocked = self.client.post(f"/api/training/jobs/{job['id']}/preview-confirm", json={"confirmed": True})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("音频", blocked.json()["detail"])

    def test_training_summary_reports_data_coverage(self):
        dataset, _ = self.approved()
        store = self.service.store
        source = store.rows("training_sources", "dataset_id=?", (dataset["id"],))[0]
        folder = store.dataset_dir(dataset["id"]) / "segments"
        for index in range(1, 6):
            segment_id = str(uuid4())
            (folder / f"{segment_id}.wav").write_bytes(wav_bytes())
            store.add_segment(dataset["id"], source["id"], segment_id,
                              index * 3, (index + 1) * 3, f"文本{index}。")
            store.edit_segment(segment_id, review_status="approved")
        job = store.create_job(dataset["id"], "train", TrainingConfig(steps=2).checked())
        summary = training_summary(job)
        self.assertEqual(summary["training_segments"], 4)
        self.assertEqual(summary["training_seconds"], 12)
        self.assertEqual(summary["steps"], 2)
        self.assertEqual(summary["sample_visits"], 2)
        self.assertEqual(summary["unique_segments_seen"], 2)
        self.assertEqual(summary["approx_passes"], 0.5)
        self.assertTrue(summary["independent_validation"])
        texts = select_preview_texts(job["snapshot"])
        self.assertEqual(len(texts), 2)
        used = "".join(segment["transcript"] for segment in job["snapshot"]["segments"])
        self.assertTrue(all(text not in used for text in texts))


    def test_publish_failure_rolls_back_and_success_uses_new_voice(self):
        job, _, _ = self.completed_job(confirmed=True)
        self.engine.fail_adapter = True
        with self.assertRaises(RuntimeError):
            self.service.publish(job["id"], "失败版本")
        self.assertEqual(self.client.get("/api/voices").json(), [])
        self.assertEqual(list((self.chat.directory / "voice_models").iterdir()), [])
        self.assertFalse(self.chat.gate.locked())
        self.engine.fail_adapter = False
        voice = self.service.publish(job["id"], "训练版本")
        repeated = self.service.publish(job["id"], "重复发布")
        self.assertEqual(voice["id"], repeated["id"])
        self.assertIsNotNone(voice["model_id"])
        self.assertTrue(all(t == self.engine.threads[0] for t in self.engine.threads))
        conversation = self.client.post("/api/conversations", json={"voice_id": voice["id"]}).json()
        with closing(self.chat.connect()) as db:
            self.assertEqual(db.execute("SELECT voice_id FROM conversations WHERE id=?", (conversation["id"],)).fetchone()[0], voice["id"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_upload_prepare_review_audio(self):
        path = Path(self.temp.name) / "source.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=240:duration=4", str(path)], check=True)
        dataset = self.dataset()
        upload = self.client.post(f"/api/training/datasets/{dataset['id']}/sources",
                                 files={"source": ("source.mp3", path.read_bytes(), "audio/mpeg")})
        self.assertEqual(upload.status_code, 201, upload.text)
        result = self.client.post(f"/api/training/datasets/{dataset['id']}/prepare")
        self.assertEqual(result.status_code, 202, result.text)
        self.wait(lambda: self.service.control is None)
        job = self.client.get(f"/api/training/jobs/{result.json()['id']}").json()
        self.assertEqual(job["status"], "review_required", job)
        segments = self.client.get(f"/api/training/datasets/{dataset['id']}/segments").json()
        self.assertGreater(len(segments), 0)
        segment = segments[0]
        self.assertEqual(segment["review_status"], "pending")
        audio = self.client.get(f"/api/training/segments/{segment['id']}/audio")
        self.assertEqual(audio.status_code, 200)
        with wave.open(io.BytesIO(audio.content)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))


    def test_busy_check_reads_control_once_when_job_finishes(self):
        dataset, _ = self.approved()
        job = self.job(dataset)
        store = self.service.store

        class Finishing:
            reads = 0

            @property
            def control(inner):
                inner.reads += 1
                return (job["id"], threading.Event()) if inner.reads == 1 else None

        finishing = Finishing()
        finishing.store = store
        with patch.object(self.chat, "training", finishing):
            with self.assertRaises(HTTPException) as caught:
                self.chat.check_training_busy()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(finishing.reads, 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_prepare_cancel_removes_unregistered_clips_before_retry(self):
        path = Path(self.temp.name) / "long.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=240:duration=26", str(path)], check=True)
        dataset = self.dataset()
        upload = self.client.post(f"/api/training/datasets/{dataset['id']}/sources",
                                 files={"source": ("long.mp3", path.read_bytes(), "audio/mpeg")})
        self.assertEqual(upload.status_code, 201, upload.text)
        with patch.object(self.service, "transcribe_clip", side_effect=Cancelled):
            submitted = self.client.post(f"/api/training/datasets/{dataset['id']}/prepare")
            self.wait(lambda: self.service.control is None)
        value = submitted.json()["id"]
        self.assertEqual(self.service.store.one("training_jobs", value)["status"], "cancelled")
        folder = self.service.store.dataset_dir(dataset["id"]) / "segments"
        self.assertEqual(list(folder.glob("*.wav")), [])
        response = self.client.post(f"/api/training/jobs/{value}/retry", json={"resume": False})
        self.assertEqual(response.status_code, 202, response.text)
        self.wait(lambda: self.service.control is None)
        self.assertEqual(self.service.store.one("training_jobs", value)["status"], "review_required")
        self.assertEqual(len(list(folder.glob("*.wav"))), len(self.service.store.segments(dataset["id"])))

    def test_upload_admission_blocks_additional_jobs(self):
        dataset, _ = self.approved()
        self.service.uploading = True
        try:
            with self.assertRaises(HTTPException) as caught:
                self.service.submit(dataset["id"], "prepare")
            self.assertEqual(caught.exception.status_code, 409)
            self.assertFalse(self.service.capabilities()["available"])
        finally:
            self.service.uploading = False

    def _ab_job(self, confirmed=False):
        dataset, _ = self.approved()
        job = self.job(dataset)
        root = self.service.job_directory(job)

        class PreviewModel:
            sample_rate = 24000

            def __init__(self, offset):
                self.offset = offset

            def generate(self, text, max_tokens, stream, ref_audio, ref_text):
                sample = 0.1 + self.offset
                yield SimpleNamespace(audio=[sample, -sample])

        config = {"max_tokens": 120, "seed": 7}
        base = self.service.validate_audio(PreviewModel(0.0), job, threading.Event(), variant="base")
        trained = self.service.validate_audio(PreviewModel(0.2), job, threading.Event(), variant="trained")
        adapter_hash = hashlib.sha256(b"adapter").hexdigest()
        (root / "adapter.safetensors").write_bytes(b"adapter")
        validation = {"ab": 2, "adapter_loaded": True, "base_model": self.service.base_model,
            "base_digest": "base-hash", "adapter_sha256": adapter_hash,
            "snapshot_digest": job["digest"], "reference_digest": "ref-hash",
            "inference_config": config, "preview_texts": trained["texts"],
            "preview_texts_sha256": trained["texts_sha256"],
            "preview_audio_sha256": trained["audio_sha256"],
            "base_preview_texts_sha256": base["texts_sha256"],
            "base_preview_audio_sha256": base["audio_sha256"],
            "preview_metrics": {"base": base["metrics"], "trained": trained["metrics"]},
            "train_losses": [6.1, 5.9], "validation_losses": [1.0],
            "training_summary": training_summary(job)}
        self.service._write_validation(job, validation)
        self.service.store.update("training_jobs", job["id"], status="completed")
        return self.service.store.one("training_jobs", job["id"]), validation

    def test_ab_uses_same_text_reference_and_config(self):
        job, validation = self._ab_job()
        root = self.service.job_directory(job)
        base_files = sorted(p.name for p in root.glob("validation-base-*.wav"))
        trained_files = sorted(p.name for p in root.glob("validation-[0-9]*.wav"))
        self.assertEqual(base_files, ["validation-base-01.wav", "validation-base-02.wav"])
        self.assertEqual(trained_files, ["validation-01.wav", "validation-02.wav"])
        self.assertNotEqual(validation["preview_audio_sha256"], validation["base_preview_audio_sha256"])
        self.assertEqual(validation["base_preview_texts_sha256"], validation["preview_texts_sha256"])
        self.assertEqual(validation["inference_config"], {"max_tokens": 120, "seed": 7})
        detail = self.client.get(f"/api/training/jobs/{job['id']}").json()
        self.assertEqual(detail["preview"]["ab"], 2)
        self.assertIn("base", detail["preview"]["preview_metrics"])
        self.assertEqual(detail["preview"]["train_losses"], [6.1, 5.9])
        base_response = self.client.get(f"/api/training/jobs/{job['id']}/preview/1?variant=base")
        self.assertEqual(base_response.status_code, 200)
        self.assertNotEqual(base_response.content,
                            self.client.get(f"/api/training/jobs/{job['id']}/preview/1").content)

    def test_ab_confirmation_binds_both_groups_and_invalidates_on_change(self):
        job, validation = self._ab_job()
        value = job["id"]
        self.assertEqual(self.client.post(f"/api/training/jobs/{value}/publish",
                         json={"name": "x"}).status_code, 409)
        confirmed = self.client.post(f"/api/training/jobs/{value}/preview-confirm", json={"confirmed": True})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        record = self.client.get(f"/api/training/jobs/{value}").json()
        self.assertTrue(record["preview"]["confirmed"])
        root = self.service.job_directory(job)
        (root / "validation-base-01.wav").write_bytes(b"tampered")
        published = self.client.post(f"/api/training/jobs/{value}/publish", json={"name": "x"})
        self.assertEqual(published.status_code, 409)
        self.assertIn("基座对照音频", published.json()["detail"])

    def test_ab_config_change_invalidates_confirmation(self):
        job, validation = self._ab_job()
        value = job["id"]
        self.client.post(f"/api/training/jobs/{value}/preview-confirm", json={"confirmed": True})
        validation["inference_config"] = {"max_tokens": 130, "seed": 7}
        self.service._write_validation(job, validation)
        published = self.client.post(f"/api/training/jobs/{value}/publish", json={"name": "x"})
        self.assertEqual(published.status_code, 409)


if __name__ == "__main__":
    unittest.main()
