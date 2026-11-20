"""Dataset review, Ubuntu training exchange, and local A/B synthesis."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
from uuid import uuid4
import wave
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from starlette.background import BackgroundTask

from audio_runtime import UnreliableTranscript
from training_exchange import (MAX_ADAPTER, MAX_CLIP, MAX_MANIFEST, PACKAGE_VERSION, RECIPE_VERSION,
                               TrainingConfig, canonical, read_member, sha256)
from training_store import TrainingStore, select_reference

log = logging.getLogger(__name__)
MAX_SOURCE = 200 * 1024**2
PREVIEW_TEXTS = (
    "今天天气不错，我们周末去公园走走吧。",
    "请把桌上的水杯拿给我，顺便告诉我会议几点开始。",
    "如果明天下雨，我们就改在图书馆见面。",
    "刚才那段录音听起来很清楚，谢谢你的帮忙。",
)


class DatasetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    speaker_confirmed: bool = False


class SegmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript: str | None = Field(default=None, max_length=2000)
    review_status: str | None = None


class DatasetReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker_confirmed: bool


class ReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None returns to the automatic quality-based choice.
    segment_id: str | None = Field(default=None, max_length=64)


class StepResult(BaseModel):
    step: int = Field(ge=1, le=1000)
    loss: FiniteFloat
    gradient_norm: FiniteFloat = Field(ge=0)
    learning_rate: FiniteFloat
    epoch: int = Field(ge=1)


class ValidationTrend(BaseModel):
    step: int = Field(ge=1, le=1000)
    losses: list[FiniteFloat]
    mean: FiniteFloat


class BestCheckpoint(BaseModel):
    step: int = Field(ge=1, le=1000)
    validation_loss: FiniteFloat


class TrainingResult(BaseModel):
    base_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_adapter_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    adapter_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    steps: int = Field(ge=1, le=1000)
    step_log: list[StepResult] = Field(max_length=1000)
    validation_losses: list[FiniteFloat]
    validation_trend: list[ValidationTrend] = Field(max_length=1000)
    best_checkpoint: BestCheckpoint | None
    sampling: dict[str, int]


class VariantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: int = Field(ge=1, le=1000)


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume: bool = True


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    note: str = Field(default="", max_length=300)


class IncompatibleBase(ValueError):
    code = "base_mismatch"


class IncompatibleAdapter(ValueError):
    code = "adapter_mismatch"


class Cancelled(Exception):
    pass


def select_preview_texts(snapshot):
    used = ["".join(str(segment.get("transcript", "")).split())
            for segment in snapshot.get("segments", [])]
    selected = ["你好，好久不见"]
    for text in PREVIEW_TEXTS:
        normalized = "".join(text.split())
        if not any(normalized in transcript for transcript in used):
            selected.append(text)
            if len(selected) == 2:
                return selected
    raise HTTPException(422, "验收文本与训练原文重复，请修改素材后重新训练。")


def training_summary(job):
    snapshot = job.get("snapshot") or {}
    config = job.get("config") or {}
    indices = snapshot.get("train_indices") or []
    segments = snapshot.get("segments") or []
    training = [segments[i] for i in indices if 0 <= i < len(segments)]
    visits = int(config.get("steps", 0)) * int(config.get("accumulation_steps", 1))
    seconds = sum(max(0.0, segment.get("end", 0) - segment.get("start", 0)) for segment in training)
    full_epochs, remainder = divmod(visits, len(training)) if training else (0, 0)
    return {
        "training_segments": len(training),
        "training_seconds": round(seconds, 2),
        "steps": int(config.get("steps", 0)),
        "samples_per_step": int(config.get("accumulation_steps", 1)),
        "sample_visits": visits,
        "unique_segments_seen": min(visits, len(training)),
        "approx_passes": round(visits / len(training), 2) if training else 0,
        # Planned coverage: a short run only sees part of the shuffled material.
        "full_epochs": full_epochs,
        "partial_epoch_samples": remainder,
        "coverage_complete": bool(training) and remainder == 0,
        "independent_validation": bool(snapshot.get("validation_indices")),
        "validation_kind": snapshot.get("validation_kind"),
        "reference_segment_id": snapshot.get("reference_segment_id"),
        "reference_source": snapshot.get("reference_source"),
    }


EVALUATION_VERSION = "voice-metrics-2"
REFERENCE_LABELS = {
    "independent_source": "按来源留出验证",
    "same_recording": "同录音留出验证",
}


def reference_segment(job):
    """The synthesis reference recorded in the snapshot, with a legacy fallback.

    Old snapshots predate the explicit field and used the first array entry.
    """
    snapshot = job.get("snapshot") or {}
    segments = snapshot.get("segments") or []
    reference_id = snapshot.get("reference_segment_id")
    if reference_id:
        for segment in segments:
            if segment.get("id") == reference_id:
                return segment
        raise HTTPException(409, "训练快照的试听参考片段无效。")
    return segments[0] if segments else None


class TrainingService:
    def __init__(self, chat):
        self.chat = chat
        self.store = TrainingStore(chat)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-media")
        self.control = None
        self.lock = threading.RLock()
        self.stopping = False
        self.uploading = False
        self.metrics_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-metrics")
        self.metrics_pending = set()
        self.metrics_cancel = threading.Event()
        self.base_model = os.getenv("BREEZE_MODEL", "mlx-community/Breeze-TTS-2-mlx-4bit")

    def start(self):
        self.store.start()

    def stop(self):
        with self.lock:
            self.stopping = True
            if self.control:
                self.control[1].set()
            self.metrics_cancel.set()
        self.executor.shutdown(wait=True)
        self.metrics_executor.shutdown(wait=True)

    def check(self, cancel):
        if cancel.is_set() or self.stopping:
            raise Cancelled()

    def capabilities(self):
        import platform
        from importlib.metadata import version, PackageNotFoundError
        compatible = False
        try:
            compatible = (platform.machine() == "arm64" and platform.system() == "Darwin"
                          and version("mlx") == "0.32.2" and version("mlx-audio") == "0.5.1")
        except PackageNotFoundError:
            pass
        from breezetts2_mac_fast import hardware_info
        control = self.control
        return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
                "compatible": compatible, "available": (compatible and self.chat.accepting_tasks
                    and self.chat.model_status == "ready" and self.chat.stt_status == "ready"
                    and control is None and not self.uploading and bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))),
                "hardware": hardware_info(), "base_model": self.base_model,
                "active_job": control[0] if control else None,
                "max_source_bytes": MAX_SOURCE, "max_source_seconds": 3600,
                "max_dataset_seconds": 7200, "batch_size": 1,
                "warning": "技术链路已验证；音色相似度需用独立目标说话者素材和未训练文本试听验收。"}

    def metrics_path(self, kind, value):
        if kind == "dataset":
            record = self.store.one("training_datasets", value)
            directory = self.store.dataset_dir(value)
        elif kind == "job":
            record = self.store.one("training_jobs", value)
            self._require_completed_train(record)
            root = self.store.dataset_dir(record["dataset_id"])
            directory = root / "jobs" / record["id"]
            if not directory.resolve().is_relative_to(root):
                raise HTTPException(422, "分析目录不安全。")
        else:
            raise HTTPException(422, "分析类型无效。")
        target = directory / "voice-metrics.json"
        if target.resolve().parent != directory.resolve():
            raise HTTPException(422, "分析文件路径不安全。")
        return record, target

    @staticmethod
    def write_metrics(path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(canonical(data) + "\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def metrics_view(self, kind, value):
        record, path = self.metrics_path(kind, value)
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or result.get("status") not in {
                    "queued", "running", "completed", "failed", "interrupted"}:
                raise ValueError("invalid metrics record")
        except FileNotFoundError:
            result = {"status": "not_started", "result": None}
        except (OSError, ValueError):
            result = {"status": "failed", "message": "分析记录不可用，请重新计算。", "result": None}
        with self.lock:
            if result["status"] in ("queued", "running") and (kind, value) not in self.metrics_pending:
                result["status"] = "interrupted"
                result["message"] = "分析因应用退出而中断，请重新计算。"
        source = (result.get("result") or {}).get("source") or {}
        result["stale"] = bool(kind == "dataset" and source.get("dataset_version") is not None
                               and source["dataset_version"] != record["version"])
        return result

    def submit_metrics(self, kind, value):
        with self.lock:
            _, path = self.metrics_path(kind, value)
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            key = (kind, value)
            if key in self.metrics_pending:
                return self.metrics_view(kind, value)
            if len(self.metrics_pending) >= 16:
                raise HTTPException(429, "分析队列已满，请稍后重试。")
            previous = self.metrics_view(kind, value).get("result")
            self.metrics_pending.add(key)
            try:
                self.write_metrics(path, {"status": "queued", "message": "等待分析。", "result": previous})
                self.metrics_executor.submit(self.run_metrics, kind, value, path)
            except BaseException:
                self.metrics_pending.discard(key)
                raise HTTPException(503, "分析任务投递失败，请重试。") from None
            return self.metrics_view(kind, value)

    def metrics_history(self, dataset_id):
        self.store.one("training_datasets", dataset_id)
        jobs = self.store.rows("training_jobs", "dataset_id=?", (dataset_id,))
        jobs = sorted((job for job in jobs if job["kind"] == "train" and job["status"] == "completed"),
                      key=lambda job: (job["created_at"], job["id"]))
        return {"jobs": [{"id": job["id"], "created_at": job["created_at"], "config": job["config"],
                          "metrics": self.metrics_view("job", job["id"])} for job in jobs]}

    def metrics_inputs(self, kind, value):
        base_previews, inference, reference_label, source_reference = [], None, None, None
        if kind == "dataset":
            with self.lock:
                dataset = self.store.one("training_datasets", value)
                segments = self.store.rows("training_segments", "dataset_id=?", (value,))
            segments = sorted((segment for segment in segments if segment["review_status"] != "excluded"),
                              key=lambda segment: (segment["source_id"], segment["start"], segment["id"]))
            reference_segments, reference_kind = segments, "current"
            texts, previews, evidence = [], [], None
            version = dataset["version"]
            source_reference = dataset.get("preferred_reference_id")
        else:
            job = self.store.one("training_jobs", value)
            self._require_completed_train(job)
            self.verify_snapshot(job)
            validation = self._read_validation(job, required=True)
            evidence = self._preview_evidence(job, validation)
            snapshot = job["snapshot"]
            segments = snapshot["segments"]
            if any(segment["dataset_id"] != job["dataset_id"] for segment in segments):
                raise HTTPException(409, "训练快照中的素材不属于此素材集。")
            indices = snapshot.get("validation_indices") or snapshot.get("train_indices") or []
            if any(not isinstance(index, int) or index < 0 or index >= len(segments) for index in indices):
                raise HTTPException(409, "训练快照参考片段无效。")
            reference_segments = [segments[index] for index in indices]
            reference_kind = "validation" if snapshot.get("validation_indices") else "training"
            reference_label = REFERENCE_LABELS.get(snapshot.get("validation_kind"))
            reference = reference_segment(job)
            source_reference = reference["id"] if reference else None
            texts = validation["preview_texts"]
            previews = [self.preview_file(job, index) for index in (1, 2)]
            base_previews = [self.preview_file(job, index, variant="base") for index in (1, 2)
                             if (self.job_directory(job) / f"validation-base-{index:02d}.wav").is_file()]
            if validation.get("ab", 0) < 2:
                base_previews = []
            if isinstance(validation.get("inference_config"), dict):
                inference = dict(validation["inference_config"])
            version = snapshot["version"]
        entries = []
        for segment in segments:
            self.check(self.metrics_cancel)
            path = self.store.segment_path(segment)
            with path.open("rb") as audio:
                digest = hashlib.file_digest(audio, "sha256").hexdigest()
            expected = segment.get("audio_sha256") or segment.get("sha256")
            if expected and digest != expected:
                raise HTTPException(409, "训练音频快照已改变或不可用。")
            entries.append({"id": segment["id"], "path": path, "sha256": digest,
                            "transcript": segment["transcript"]})
        by_id = {entry["id"]: entry for entry in entries}
        references = [by_id[segment["id"]] for segment in reference_segments]
        material = [{"id": entry["id"], "sha256": entry["sha256"], "transcript": entry["transcript"]}
                    for entry in entries]
        source = {"dataset_version": version, "material_digest": hashlib.sha256(canonical(material).encode()).hexdigest(),
                  "reference_kind": reference_kind, "reference_label": reference_label,
                  "reference_ids": [], "synthesis_reference_id": source_reference,
                  "preview_texts": texts, "inference": inference,
                  "preview_audio_sha256": evidence["preview_audio_sha256"] if evidence else {}}
        return entries, references, previews, base_previews, source

    def verify_metrics_inputs(self, kind, value, entries):
        if kind == "job":
            job = self.store.one("training_jobs", value)
            self._require_completed_train(job)
            self.verify_snapshot(job)
            self._preview_evidence(job, self._read_validation(job, required=True))
        else:
            for entry in entries:
                self.check(self.metrics_cancel)
                with entry["path"].open("rb") as audio:
                    if hashlib.file_digest(audio, "sha256").hexdigest() != entry["sha256"]:
                        raise HTTPException(409, "素材音频已改变，请重新分析。")

    @staticmethod
    def quality_view(metrics):
        low = metrics.get("low_energy_fraction")
        clipping = metrics.get("clipping_fraction")
        invalid = metrics.get("file_count", 0) - metrics.get("valid_file_count", 0)
        return {"status": metrics["status"], "dbfs": metrics.get("rms_dbfs"),
                "low_energy_percent": low * 100 if low is not None else None,
                "clipping_percent": clipping * 100 if clipping is not None else None,
                "duration_seconds": metrics.get("duration_seconds", 0),
                "file_count": metrics.get("file_count", 0),
                "message": (f"{invalid} 个音频文件为空或无效。" if invalid else
                            "没有可用的非静音音频。" if metrics["status"] != "ok" else "")}

    def metrics_features_on_worker(self, model, references, previews):
        import gc
        import mlx.core as mx
        from voice_metrics import compute_speaker_similarity
        released = False
        try:
            self.check(self.metrics_cancel)
            released = True
            self.chat.engine.close()
            self.chat.transcriber.close()
            for name in ("model", "engine"):
                if hasattr(self.chat.engine, name):
                    delattr(self.chat.engine, name)
            gc.collect()
            mx.clear_cache()
            scores = compute_speaker_similarity(references, previews, model=model)
            self.check(self.metrics_cancel)
            return scores
        finally:
            model.close()
            gc.collect()
            mx.clear_cache()
            if released:
                try:
                    self.chat.engine.load()
                    self.chat.model_status, self.chat.model_error = "ready", None
                except BaseException:
                    self.chat.model_status = "failed"
                    self.chat.model_error = "音色分析后推理模型恢复失败，请重启应用。"
                try:
                    self.chat.transcriber.load()
                    self.chat.stt_status, self.chat.stt_error = "ready", None
                except BaseException:
                    self.chat.stt_status = "failed"
                    self.chat.stt_error = "音色分析后识别模型恢复失败，请重启应用。"
    INDEX_NOTE = "0–100 是声纹余弦相似度的缩放指数：不等于克隆准确率，也不衡量发音正确率。"

    @classmethod
    def features_view(cls, scores, trained_count, base_count=0):
        """Split speaker scores into independently meaningful parts.

        Reference consistency needs at least two references; generated
        similarity needs one. A single reference therefore limits one index
        without hiding the other, and a base/trained pair is only compared when
        both sides were fully scored on the same references.
        """
        from voice_metrics import (ALGORITHM, EVALUATION_VERSION, QUALITY_ALGORITHM,
                                   SPEAKER_MODEL_ID, reason_message)
        scores = scores if isinstance(scores, dict) else {}
        records = list(scores.get("generated") or [])
        trained = records[:trained_count]
        base = records[trained_count:trained_count + base_count]
        metadata = scores.get("model") if isinstance(scores.get("model"), dict) else {}
        model_error = scores.get("model_error")
        consistency = scores.get("reference_consistency") or {}
        reference_count = consistency.get("reference_count")
        if not isinstance(reference_count, int):
            reference_count = records[0].get("reference_count", 0) if records else 0

        def part(items, expected, empty_reason):
            # Records without a numeric score are failures, not zeros.
            scored = [item for item in items
                      if item.get("status") == "ok" and isinstance(item.get("score"), (int, float))]
            failed = [item for item in items if item not in scored]
            if scored and expected and len(scored) == expected:
                status, reason = "ok", None
            elif scored:
                status, reason = "partial", "partial_generated"
            else:
                status = "unavailable"
                reason = model_error and "model_load_failed" or (failed[0].get("reason") if failed else empty_reason)
            return {"status": status, "reason": reason,
                    "message": reason_message(reason) if reason else "",
                    "score": sum(item["score"] for item in scored) / len(scored) if scored else None,
                    "sample_count": len(scored), "expected_count": max(expected, len(items)),
                    "failed_count": len(failed), "reference_count": reference_count}

        reason = consistency.get("reason")
        consistency_part = {"status": consistency.get("status") or "unavailable", "reason": reason,
                            "message": reason_message(reason) if reason else "",
                            "score": consistency.get("score"),
                            "reference_count": reference_count,
                            "pair_count": consistency.get("pair_count", 0)}
        if trained_count:
            generated_part = part(trained, trained_count, "generated_audio_missing")
        else:
            # Dataset-level analysis has no generated audio to score.
            generated_part = {"status": "not_applicable", "reason": None, "message": "",
                              "score": None, "sample_count": 0, "expected_count": 0,
                              "failed_count": 0, "reference_count": reference_count}
        base_part = part(base, base_count, "generated_audio_missing") if base_count else None
        training_effect = None
        if base_part is not None:
            complete = base_part["status"] == "ok" and generated_part["status"] == "ok"
            training_effect = {
                "status": "ok" if complete else "partial" if (
                    base_part["score"] is not None and generated_part["score"] is not None) else "unavailable",
                "base_index": base_part["score"], "trained_index": generated_part["score"],
                "delta": (round(generated_part["score"] - base_part["score"], 3) if complete else None),
                "reason": None if complete else (generated_part["reason"] or base_part["reason"]
                                                 or "partial_generated"),
                "message": "" if complete else "基座与训练后没有在同一条参考、同一组文本上完整算出，暂不显示差值。",
            }
        if model_error:
            status, message = "failed", "声纹模型不可用，音色指标未能计算；音频质量仍可查看。"
        elif generated_part["status"] in ("ok", "not_applicable") and consistency_part["status"] == "ok":
            status, message = "completed", ""
        elif generated_part["score"] is not None or consistency_part["score"] is not None:
            status = "partial"
            message = " ".join(item["message"] for item in (consistency_part, generated_part) if item["message"])
        else:
            status = "insufficient"
            message = (" ".join(item["message"] for item in (consistency_part, generated_part) if item["message"])
                       or "没有可用于比较的参考片段。")
        previews = [{"index": index, "value": item.get("score"),
                     "reference_count": item.get("reference_count", reference_count),
                     "status": item.get("status"), "reason": item.get("reason"),
                     "message": reason_message(item.get("reason")) if item.get("reason") else ""}
                    for index, item in enumerate(trained, 1)]
        return {
            "status": status, "message": message, "index_note": cls.INDEX_NOTE,
            "model": SPEAKER_MODEL_ID,
            "revision": metadata.get("revision"),
            "requested_revision": metadata.get("requested_revision"),
            "algorithm": scores.get("algorithm") or ALGORITHM,
            "quality_algorithm": scores.get("quality_algorithm") or QUALITY_ALGORITHM,
            "evaluation_version": scores.get("evaluation_version") or EVALUATION_VERSION,
            "reference_consistency": consistency_part,
            "generated_similarity": generated_part,
            "base_similarity": base_part,
            "training_effect": training_effect,
            # Backward-compatible aliases keep old records and history charts readable.
            "original_index": consistency_part["score"],
            "generated_index": generated_part["score"],
            "reference_count": reference_count, "pair_count": consistency_part["pair_count"],
            "generated_count": generated_part["sample_count"],
            "previews": previews,
        }

    def run_metrics(self, kind, value, path):
        from voice_metrics import (ALGORITHM, EVALUATION_VERSION, MAX_REFERENCES, MIN_REFERENCE_SECONDS,
                                   QUALITY_ALGORITHM, SPEAKER_MODEL_ID, SpeakerEmbeddingModel,
                                   compute_dataset_metrics)
        result = None
        try:
            self.check(self.metrics_cancel)
            entries, references, previews, base_previews, source = self.metrics_inputs(kind, value)
            original = compute_dataset_metrics([entry["path"] for entry in entries])
            generated = compute_dataset_metrics(previews) if previews else None
            per_preview = [self.quality_view(compute_dataset_metrics([preview])) for preview in previews]
            result = {"analyzed_at": datetime.now(timezone.utc).isoformat(), "source": source,
                      "quality": {"original": self.quality_view(original),
                                  "generated": self.quality_view(generated) if generated else None,
                                  "previews": per_preview},
                      "features": {"status": "pending", "model": SPEAKER_MODEL_ID,
                                   "revision": None, "algorithm": ALGORITHM,
                                   "quality_algorithm": QUALITY_ALGORITHM,
                                   "evaluation_version": EVALUATION_VERSION,
                                   "reference_count": 0, "pair_count": 0, "original_index": None,
                                   "generated_index": None, "previews": [], "message": "",
                                   "index_note": self.INDEX_NOTE},
                      "comparison_key": None}
            self.write_metrics(path, {"status": "running", "message": "音频质量已计算，正在分析音色。", "result": result})
            self.check(self.metrics_cancel)
            file_quality = {item["path"]: item for item in original["files"]}
            eligible = [entry for entry in references if
                        file_quality[str(entry["path"])]["status"] == "ok" and
                        file_quality[str(entry["path"])]["duration_seconds"] >= MIN_REFERENCE_SECONDS]
            eligible = sorted(eligible, key=lambda entry: str(entry["path"]))[:MAX_REFERENCES]
            if eligible:
                model = SpeakerEmbeddingModel()
                try:
                    model.download()
                    self.check(self.metrics_cancel)
                    with self.model_slot(value, self.metrics_cancel):
                        scores = self.chat.worker.submit(
                            self.metrics_features_on_worker, model,
                            [entry["path"] for entry in eligible], previews + base_previews).result()
                    self.check(self.metrics_cancel)
                    by_path = {str(entry["path"]): entry["id"] for entry in references}
                    source["reference_ids"] = [by_path[item["path"]] for item in scores["references"]
                                               if item["status"] == "ok"]
                    self.check(self.metrics_cancel)
                    feature = self.features_view(scores, len(previews), len(base_previews))
                    result["features"] = feature
                    if feature["revision"] and feature["generated_index"] is not None:
                        comparison = {"material_digest": source["material_digest"],
                                      "reference_kind": source["reference_kind"],
                                      "reference_ids": source["reference_ids"],
                                      "preview_texts": source["preview_texts"],
                                      "synthesis_reference_id": source["synthesis_reference_id"],
                                      "inference": source["inference"],
                                      "model": feature["model"], "revision": feature["revision"],
                                      "algorithm": feature["algorithm"],
                                      "evaluation_version": feature["evaluation_version"],
                                      "quality_algorithm": feature["quality_algorithm"],
                                      # Partial scores must not share a key (or a
                                      # trend line) with fully scored runs.
                                      "generated_samples": [feature["generated_similarity"]["sample_count"],
                                                            feature["generated_similarity"]["expected_count"]]}
                        result["comparison_key"] = hashlib.sha256(canonical(comparison).encode()).hexdigest()
                except Cancelled:
                    raise
                except Exception as exc:
                    log.error(json.dumps({"task_id": value, "stage": "metrics_features", "error_type": type(exc).__name__}))
                    result["features"].update(status="failed",
                        message="音色模型下载或加载失败，请检查网络与本机缓存后重试。")
                finally:
                    model.close()
            else:
                result["features"].update(status="insufficient",
                    message="没有足够长且非静音的参考片段，无法比较音色；音频质量仍可查看。")
            self.verify_metrics_inputs(kind, value, entries)
            self.write_metrics(path, {"status": "completed", "message": "音频分析完成。", "result": result})
        except Cancelled:
            self.write_metrics(path, {"status": "interrupted", "message": "分析因应用停止而中断，请重新计算。", "result": result})
        except Exception as exc:
            log.error(json.dumps({"task_id": value, "stage": "metrics", "error_type": type(exc).__name__}))
            if isinstance(exc, FileNotFoundError):
                message = "素材或试听文件缺失，请恢复文件后重试。"
            elif isinstance(exc, HTTPException):
                message = exc.detail if isinstance(exc.detail, str) else "素材或试听校验失败，请检查文件后重试。"
            else:
                message = "分析失败，请检查素材和磁盘后重试。"
            self.write_metrics(path, {"status": "failed", "message": message, "result": None})
        finally:
            with self.lock:
                self.metrics_pending.discard((kind, value))

    @contextmanager
    def dataset_edit(self, dataset_id):
        with self.lock:
            self.store.one("training_datasets", dataset_id)
            if self.control:
                job = self.store.one("training_jobs", self.control[0])
                if job["dataset_id"] == dataset_id and job["kind"] == "prepare":
                    raise HTTPException(409, "数据集正在处理，请结束后编辑。")
            yield

    async def upload(self, dataset_id, upload):
        from media_processing import probe_media, MediaError
        from voice_app import identifier
        identifier(dataset_id)
        extension = Path(upload.filename or "").suffix.lower().lstrip(".")
        if extension not in ("mp3", "mp4", "wav"):
            await upload.close()
            raise HTTPException(422, "仅支持 MP3、MP4、WAV 素材。")
        source_id = str(uuid4())
        folder = self.store.dataset_dir(dataset_id) / "sources"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f"{source_id}.{extension}"
        temporary = target.with_suffix(".tmp")
        size = 0
        with self.lock:
            if self.control or self.uploading or self.stopping:
                raise HTTPException(409, "正在上传或处理素材，请稍后重试。")
            self.uploading = True
        try:
            self.store.one("training_datasets", dataset_id)
            with temporary.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SOURCE:
                        raise HTTPException(413, "每个素材不能超过 200 MiB。")
                    if shutil.disk_usage(folder).free < len(chunk) + 256 * 1024**2:
                        raise HTTPException(507, "磁盘剩余空间不足。")
                    stream.write(chunk)
            # Probe is CPU I/O only; do not block the event loop or model worker.
            import asyncio
            metadata = await asyncio.wrap_future(self.executor.submit(probe_media, temporary, extension))
            with self.dataset_edit(dataset_id):
                temporary.replace(target)
                result = self.store.add_source(dataset_id, source_id, extension, size, metadata)
            return result
        except MediaError as exc:
            raise HTTPException(422, {"code": exc.code, "message": "素材检查失败，请检查格式、音轨和时长。"}) from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            await upload.close()
            with self.lock:
                self.uploading = False

    def submit(self, dataset_id, kind, config=None, request_key=None):
        with self.lock, self.chat.admission:
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            if request_key:
                from voice_app import identifier
                identifier(request_key)
                existing = self.store.rows("training_jobs", "dataset_id=? AND request_key=?", (dataset_id, request_key))
                if existing:
                    if existing[0]["config"] != config:
                        raise HTTPException(409, "同一幂等键不能用于不同训练配置。")
                    return existing[0]
            if self.control or self.uploading:
                raise HTTPException(409, "已有训练或素材处理任务，请等待或取消。")
            if kind == "train" and any(source["status"] != "completed" for source in
                    self.store.rows("training_sources", "dataset_id=?", (dataset_id,))):
                raise HTTPException(409, "请先完成所有素材处理并审核，再启动训练。")
            job = self.store.create_job(dataset_id, kind, config or {}, request_key)
            if job["kind"] == "train":
                return self.await_result(job)
            self._dispatch(job)
            return job

    def submit_variant(self, value, steps, request_key=None):
        with self.lock, self.chat.admission:
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            if request_key:
                from voice_app import identifier
                identifier(request_key)
            if self.control or self.uploading:
                raise HTTPException(409, "已有训练或素材处理任务，请等待或取消。")
            parent = self.store.one("training_jobs", value)
            self._require_completed_train(parent)
            self.verify_snapshot(parent)
            job = self.store.create_variant_job(value, steps, request_key)
            return self.await_result(job)

    def await_result(self, job):
        if job["status"] == "queued":
            return self.store.update("training_jobs", job["id"], status="awaiting_result",
                                     stage="awaiting_result", steps_total=job["config"]["steps"])
        return job

    def task_identity(self, job):
        return {"format": PACKAGE_VERSION, "recipe_version": RECIPE_VERSION, "job_id": job["id"],
                "attempt": job["attempt"], "base_model": self.base_model,
                "snapshot_digest": job["digest"], "config": job["config"]}

    def export_task(self, value):
        with self.lock:
            job = self.store.one("training_jobs", value)
            if job["kind"] != "train" or job["status"] != "awaiting_result":
                raise HTTPException(409, "只有等待 Ubuntu 结果的训练任务可以下载。")
        payload = tempfile.TemporaryFile()
        try:
            manifest = canonical({"identity": self.task_identity(job), "snapshot": job["snapshot"]}).encode()
            if len(manifest) > MAX_MANIFEST or sha256(canonical(job["snapshot"]).encode()) != job["digest"]:
                raise HTTPException(409, "训练快照大小或摘要无效。")
            with ZipFile(payload, "w") as archive:
                archive.writestr("task.json", manifest)
                for index, segment in enumerate(job["snapshot"]["segments"]):
                    with self.store.segment_path(segment).open("rb") as source:
                        audio = source.read(MAX_CLIP + 1)
                    if len(audio) > MAX_CLIP or sha256(audio) != (segment.get("sha256") or segment.get("audio_sha256")):
                        raise HTTPException(409, "训练音频快照已改变或大小无效。")
                    archive.writestr(f"audio/{index}.wav", audio)
            size = payload.tell()
            payload.seek(0)
        except BaseException as exc:
            payload.close()
            if isinstance(exc, OSError):
                raise HTTPException(409, "读取训练音频或写入任务包失败。") from None
            raise
        return StreamingResponse(iter(lambda: payload.read(1024**2), b""), media_type="application/zip",
                                 headers={"Content-Length": str(size), "Cache-Control": "no-store",
                                          "Content-Disposition": f'attachment; filename="breeze-{value}-{job["attempt"]}.zip"'},
                                 background=BackgroundTask(payload.close))

    def import_result(self, value, package):
        with self.lock, self.chat.admission:
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            job = self.store.one("training_jobs", value)
            if self.control or self.uploading or job["kind"] != "train" or job["status"] != "awaiting_result":
                raise HTTPException(409, "任务未在等待 Ubuntu 结果，或本机正在处理其他任务。")
            try:
                with ZipFile(package) as archive:
                    if Counter(archive.namelist()) != {"result.json": 1, "adapter.safetensors": 1}:
                        raise ValueError
                    manifest = json.loads(read_member(archive, "result.json", MAX_MANIFEST))
                    adapter = read_member(archive, "adapter.safetensors", MAX_ADAPTER)
                if not isinstance(manifest, dict) or any(
                    manifest.get(key) != expected for key, expected in self.task_identity(job).items()
                ):
                    raise HTTPException(409, "结果包与当前任务、快照、基座或尝试次数不匹配。")
                result = TrainingResult.model_validate(manifest)
                if (result.steps != job["config"]["steps"] or result.adapter_sha256 != sha256(adapter)
                        or [entry.step for entry in result.step_log] != list(range(1, result.steps + 1))
                        or set(result.sampling) != {"epoch", "position"} or min(result.sampling.values()) < 0):
                    raise ValueError
            except (BadZipFile, KeyError, ValueError, OSError, RuntimeError, EOFError, RecursionError):
                raise HTTPException(422, "结果 ZIP、训练记录或 adapter 摘要无效。") from None
            self.verify_snapshot(job)
            root = self.job_directory(job)
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                path = Path(temporary) / "adapter.safetensors"
                path.write_bytes(adapter)
                path.replace(root / path.name)
            self.validation_path(job).unlink(missing_ok=True)
            job = self.store.update("training_jobs", value, status="queued", stage="queued", error_code=None)
            self._dispatch(job, result)
            return job

    def _dispatch(self, job, result=None):
        cancel = threading.Event()
        self.control = (job["id"], cancel)
        try:
            self.executor.submit(self.run, job["id"], cancel, result)
        except BaseException:
            self.control = None
            self.store.update("training_jobs", job["id"], status="failed", error_code="dispatch_failed")
            raise HTTPException(503, "任务投递失败。") from None

    def cancel(self, value):
        with self.lock:
            job = self.store.one("training_jobs", value)
            if job["status"] == "awaiting_result":
                return self.store.update("training_jobs", value, status="cancelled", stage="cancelled", error_code="cancelled")
            if (self.control and self.control[0] == value
                    and job["status"] not in ("completed", "review_required", "failed", "cancelled")):
                self.store.update("training_jobs", value, status="cancelling")
                self.control[1].set()
            return self.store.one("training_jobs", value)

    def retry(self, value):
        with self.lock, self.chat.admission:
            job = self.store.one("training_jobs", value)
            if self.control or self.uploading or job["status"] not in ("failed", "cancelled"):
                raise HTTPException(409, "只能重试已经退出的失败或取消任务。")
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            self.store.update("training_jobs", value, status="queued", stage="queued", error_code=None,
                              attempt=job["attempt"] + 1)
            job = self.store.one("training_jobs", value)
            if job["kind"] == "train":
                self.store.update("training_jobs", value, checkpoint=None, steps_done=0)
                return self.await_result(self.store.one("training_jobs", value))
            self._dispatch(job)
            return job

    def stage(self, value, status, **fields):
        with self.lock:
            if (status not in ("failed", "cancelled") and self.control
                    and self.control[0] == value and self.control[1].is_set()):
                raise Cancelled()
            self.store.update("training_jobs", value, status=status, stage=status, **fields)
        log.info(json.dumps({"task_id": value, "stage": status}))

    @contextmanager
    def model_slot(self, value, cancel):
        acquired = False
        try:
            while not acquired:
                self.check(cancel)
                with self.chat.admission:
                    if self.chat.accepting_tasks and self.chat.gate.acquire(blocking=False):
                        acquired = True
                        self.chat.active_kind = "training"
                if not acquired:
                    cancel.wait(0.05)
            yield
        finally:
            if acquired:
                with self.chat.admission:
                    self.chat.active_kind = None
                    self.chat.gate.release()

    def run(self, value, cancel, result):
        try:
            job = self.store.one("training_jobs", value)
            self.check(cancel)
            if job["kind"] == "prepare":
                self.prepare(job, cancel)
            else:
                if result is None:
                    raise RuntimeError("Missing Ubuntu training result")
                with self.model_slot(value, cancel):
                    self.chat.worker.submit(self.validate_import, job, cancel, result).result()
        except Cancelled:
            self.stage(value, "cancelled", error_code="cancelled")
        except BaseException as exc:
            code = getattr(exc, "code", "training_failed")
            with self.lock:
                if cancel.is_set():
                    self.stage(value, "cancelled", error_code="cancelled")
                else:
                    status = "awaiting_result" if result is not None else "failed"
                    self.stage(value, status, error_code=code)
            log.error(json.dumps({"task_id": value, "stage": "failed", "error_type": type(exc).__name__}))
        finally:
            with self.lock:
                if self.control and self.control[0] == value:
                    self.control = None
            if not self.stopping:
                try:
                    job = self.store.one("training_jobs", value)
                    if job["kind"] == "prepare" and job["status"] == "review_required":
                        self.submit_metrics("dataset", job["dataset_id"])
                    elif job["kind"] == "train" and job["status"] == "completed":
                        self.submit_metrics("job", value)
                except Exception as exc:
                    log.error(json.dumps({"task_id": value, "stage": "metrics_dispatch", "error_type": type(exc).__name__}))

    def prepare(self, job, cancel):
        from media_processing import decode_media, split_audio
        dataset_id, value = job["dataset_id"], job["id"]
        folder = self.store.dataset_dir(dataset_id)
        self.stage(value, "preparing")
        sources = self.store.rows("training_sources", "dataset_id=?", (dataset_id,))
        if not sources:
            raise HTTPException(422, "请先上传素材。")
        for source in sources:
            self.check(cancel)
            if source["status"] == "completed":
                continue
            self.store.update("training_sources", source["id"], status="processing")
            raw = folder / "sources" / f"{source['id']}.{source['extension']}"
            decoded = folder / f"{source['id']}.wav"
            segments_folder = folder / "segments"
            segments_folder.mkdir(exist_ok=True, mode=0o700)
            # Remove only unfinished source segments; completed sources are immutable here.
            for segment in self.store.rows("training_segments", "source_id=?", (source["id"],)):
                self.store.segment_path(segment).unlink(missing_ok=True)
            with closing(self.chat.connect()) as db, db:
                db.execute("DELETE FROM training_segments WHERE source_id=?", (source["id"],))
                # Reprocessing removes the old clips, so a chosen reference that
                # belonged to this source can no longer be used.
                db.execute("""UPDATE training_datasets SET preferred_reference_id=NULL
                    WHERE id=? AND preferred_reference_id IS NOT NULL
                    AND preferred_reference_id NOT IN (SELECT id FROM training_segments WHERE dataset_id=?)""",
                           (dataset_id, dataset_id))
                db.execute("""UPDATE training_datasets SET version=version+1, approved_duration=(
                    SELECT COALESCE(SUM(end-start),0) FROM training_segments
                    WHERE dataset_id=? AND review_status='approved') WHERE id=?""", (dataset_id, dataset_id))
            clips, pending_destination = [], None
            try:
                metadata = decode_media(raw, decoded, cancel)
                self.store.update("training_sources", source["id"], duration=metadata["decoded_duration"], metadata=metadata)
                clips = split_audio(decoded, segments_folder, cancel)
                self.store.update("training_jobs", value, segments_total=len(clips), segments_done=0)
                for index, clip in enumerate(clips):
                    self.check(cancel)
                    segment_id = str(uuid4())
                    destination = segments_folder / f"{segment_id}.wav"
                    Path(clip["path"]).replace(destination)
                    pending_destination = destination
                    text = ""
                    hints = list(clip.get("quality_hints", []))
                    self.stage(value, "transcribing")
                    with self.model_slot(value, cancel):
                        try:
                            text, reliable = self.chat.worker.submit(self.transcribe_clip, destination, cancel).result()
                            if not reliable:
                                hints.append("low_confidence_transcript")
                            if not text:
                                hints.append("no_speech")
                            elif len("".join(text.split())) <= 1:
                                # One character is usually a bad transcript or a
                                # fragment cut mid-sentence; never approve silently.
                                hints.append("one_character_text")
                        except Cancelled:
                            raise
                        except Exception:
                            hints.append("transcription_failed_edit_required")
                    self.store.add_segment(dataset_id, source["id"], segment_id, clip["start"], clip["end"], text,
                                           hints, clip.get("quality_stats"))
                    pending_destination = None
                    self.store.update("training_jobs", value, segments_done=index + 1)
                self.store.update("training_sources", source["id"], status="completed")
            except BaseException:
                self.store.update("training_sources", source["id"], status="failed")
                raise
            finally:
                decoded.unlink(missing_ok=True)
                for clip in clips:
                    Path(clip["path"]).unlink(missing_ok=True)
                if pending_destination is not None:
                    pending_destination.unlink(missing_ok=True)
        self.store.update("training_datasets", dataset_id, status="review_required")
        self.stage(value, "review_required")

    def transcribe_clip(self, path, cancel):
        import numpy as np
        self.check(cancel)
        with wave.open(str(path), "rb") as wav:
            raw = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
        # 24 kHz -> 16 kHz for Whisper; resampling is bounded to one <=12s clip.
        positions = np.arange(int(len(raw) * 2 / 3)) * 1.5
        samples = np.interp(positions, np.arange(len(raw)), raw).clip(-32768, 32767).astype("<i2").tobytes()
        reliable = True
        try:
            text = self.chat.transcriber.transcribe(samples)
        except UnreliableTranscript as error:
            text, reliable = error.text, False
        self.check(cancel)
        return (text.strip()[:2000] if isinstance(text, str) else ""), reliable

    def job_directory(self, job):
        from voice_app import identifier
        value = identifier(job["id"])
        directory = self.store.dataset_dir(job["dataset_id"]) / "jobs" / value
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.resolve().is_relative_to(self.chat.directory.resolve()):
            raise HTTPException(422, "任务路径无效。")
        return directory

    @staticmethod
    def _require_completed_train(job):
        if job.get("kind") != "train" or job.get("status") != "completed":
            raise HTTPException(409, "只有已完成的训练任务可以试听或发布。")

    def validation_path(self, job):
        return self.job_directory(job) / "validation.json"

    def _read_validation(self, job, required=False):
        path = self.validation_path(job)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            if required:
                raise HTTPException(409, "训练验证记录不存在。") from None
            return None
        except (OSError, ValueError):
            raise HTTPException(409, "训练验证记录不可用。") from None
        if not isinstance(data, dict):
            raise HTTPException(409, "训练验证记录格式无效。")
        return data

    def _write_validation(self, job, validation):
        path = self.validation_path(job)
        temporary = path.with_name("validation.json.tmp")
        temporary.write_text(canonical(validation) + "\n")
        os.replace(temporary, path)

    def preview_file(self, job, index, variant="trained"):
        if index not in (1, 2) or variant not in ("trained", "base"):
            raise HTTPException(422, "试听样本编号或变体无效。")
        directory = self.job_directory(job)
        if variant == "base":
            if not (directory / f"validation-base-{index:02d}.wav").is_file():
                raise HTTPException(404, "基座对照试听不可用。")
        name = "validation" if variant == "trained" else "validation-base"
        path = (directory / f"{name}-{index:02d}.wav").resolve()
        if path.parent != directory.resolve() or not path.is_file():
            raise HTTPException(404, "试听样本不可用。")
        return path

    def _preview_evidence(self, job, validation):
        texts = validation.get("preview_texts")
        audio_hashes = validation.get("preview_audio_sha256")
        adapter_hash = validation.get("adapter_sha256")
        if (not isinstance(texts, list) or len(texts) != 2
                or not isinstance(audio_hashes, dict) or not adapter_hash):
            raise HTTPException(409, "训练试听记录不完整。")
        if hashlib.sha256(canonical(texts).encode()).hexdigest() != validation.get("preview_texts_sha256"):
            raise HTTPException(409, "训练试听文本与记录不一致。")
        adapter = self.job_directory(job) / "adapter.safetensors"
        if not adapter.is_file() or hashlib.sha256(adapter.read_bytes()).hexdigest() != adapter_hash:
            raise HTTPException(409, "Adapter 与试听记录不一致。")
        checked = {}
        for index in (1, 2):
            key = f"{index:02d}"
            path = self.preview_file(job, index)
            if audio_hashes.get(key) != hashlib.sha256(path.read_bytes()).hexdigest():
                raise HTTPException(409, "试听音频与记录不一致。")
            checked[key] = audio_hashes[key]
        evidence = {"adapter_sha256": adapter_hash,
                    "preview_texts_sha256": validation["preview_texts_sha256"],
                    "preview_audio_sha256": checked}
        if validation.get("ab", 0) >= 2:
            base_hashes = validation.get("base_preview_audio_sha256")
            if (not isinstance(base_hashes, dict)
                    or validation.get("base_preview_texts_sha256") != validation["preview_texts_sha256"]
                    or not isinstance(validation.get("inference_config"), dict)
                    or validation.get("snapshot_digest") != job.get("digest")
                    or not validation.get("reference_digest")):
                raise HTTPException(409, "A/B 验收记录不完整。")
            base_checked = {}
            for index in (1, 2):
                key = f"{index:02d}"
                path = self.preview_file(job, index, variant="base")
                if base_hashes.get(key) != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise HTTPException(409, "基座对照音频与记录不一致。")
                base_checked[key] = base_hashes[key]
            evidence["base_preview_audio_sha256"] = base_checked
            evidence["inference_config_sha256"] = hashlib.sha256(canonical(validation["inference_config"]).encode()).hexdigest()
        return evidence

    def dataset_summary(self, dataset_id):
        """Pre-training review rollup so the workbench can show what will train."""
        dataset = self.store.one("training_datasets", dataset_id)
        # Same deterministic order create_job uses, so the review page cannot
        # promise a validation split the job would not build.
        segments = sorted(self.store.rows("training_segments", "dataset_id=?", (dataset_id,)),
                          key=lambda segment: (segment["source_id"], segment["start"], segment["id"]))
        sources = self.store.rows("training_sources", "dataset_id=?", (dataset_id,))
        approved = [s for s in segments if s["review_status"] == "approved" and s["transcript"].strip()]
        pending = [s for s in segments if s["review_status"] == "pending"]
        excluded = [s for s in segments if s["review_status"] == "excluded"]
        seconds = sum(max(0.0, float(s["end"]) - float(s["start"])) for s in approved)
        # Mirror create_job's split so the summary cannot promise a validation set
        # that training would not build.
        approved_sources = {s["source_id"] for s in approved}
        independent = False
        if len(approved_sources) > 1:
            independent = True
        elif len(approved) > 1 and any(
                approved[i]["end"] + 3 <= approved[-1]["start"] for i in range(len(approved) - 1)):
            independent = True
        # Mirror create_job's reference choice so the review page can show it.
        preferred = dataset.get("preferred_reference_id")
        reference_index, reference_source = select_reference(approved, list(range(len(approved))), preferred)
        reference = approved[reference_index] if reference_index is not None else None
        return {
            "dataset_version": dataset["version"],
            "speaker_confirmed": bool(dataset["speaker_confirmed"]),
            "sources": len(sources),
            "sources_pending": sum(1 for s in sources if s["status"] != "completed"),
            "approved_segments": len(approved),
            "approved_seconds": round(seconds, 2),
            "pending_segments": len(pending),
            "excluded_segments": len(excluded),
            "independent_validation": independent,
            "validation_warning": None if independent else "insufficient_independent_validation",
            "preferred_reference_id": preferred,
            "reference_segment_id": reference["id"] if reference else None,
            "reference_source": reference_source if reference else None,
            "reference_note": ("参考片段会同时决定试听与应用推理的提示条件。"
                               if reference else "至少确认一条带原文的片段后才能选择参考。"),
            "ready": bool(dataset["speaker_confirmed"] and approved),
        }

    def validation_view(self, job):
        validation = self._read_validation(job)
        if validation is None:
            return None
        confirmation = validation.get("confirmation") or {}
        return {"texts": validation.get("preview_texts", []),
                "confirmed": bool(confirmation.get("confirmed")),
                "note": confirmation.get("note", ""),
                "confirmed_at": confirmation.get("confirmed_at"),
                "summary": validation.get("training_summary"),
                "validation_losses": validation.get("validation_losses"),
                "validation_trend": validation.get("validation_trend"),
                "step_log": validation.get("step_log"),
                "best_checkpoint": validation.get("best_checkpoint"),
                "final_checkpoint_step": validation.get("final_checkpoint_step"),
                "recipe_version": validation.get("recipe_version"),
                "reference_segment_id": validation.get("reference_segment_id"),
                "inference_config": validation.get("inference_config"),
                "ab": validation.get("ab", 0),
                "preview_metrics": validation.get("preview_metrics"),
                "train_losses": validation.get("train_losses")}

    def validate_import(self, job, cancel, result):
        import gc
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model
        from breeze_training import attach_lora, frozen_digest, load_adapter
        model = None
        config = job["config"]
        value = job["id"]
        try:
            self.stage(value, "validating", steps_done=result.steps)
            self.chat.engine.close()
            self.chat.transcriber.close()
            if hasattr(self.chat.engine, "model"):
                del self.chat.engine.model
            if hasattr(self.chat.engine, "engine"):
                del self.chat.engine.engine
            gc.collect()
            mx.clear_cache()
            mx.set_memory_limit(mx.device_info()["max_recommended_working_set_size"])
            mx.set_cache_limit(256 * 1024**2)
            root = self.job_directory(job)
            snapshot = job["snapshot"]
            reference = reference_segment(job)
            if reference is None:
                raise HTTPException(422, "没有可用的试听参考片段。")
            # The same synthesis path and conditions as the running application;
            # validate_audio records the exact conditions it used.
            model = load_model(self.base_model)
            attach_lora(model, config["rank"])
            base_digest = frozen_digest(model)
            if base_digest != result.base_digest:
                raise IncompatibleBase()
            base_preview = self.validate_audio(model, job, cancel, variant="base")
            try:
                load_adapter(model, root / "adapter.safetensors")
            except (OSError, RuntimeError, ValueError) as exc:
                raise IncompatibleAdapter() from exc
            preview = self.validate_audio(model, job, cancel, variant="trained")
            inference_config = preview["inference"]
            reference_digest = hashlib.sha256(canonical({"id": reference["id"],
                "transcript": reference["transcript"], "audio_sha256": reference.get("audio_sha256", "")}).encode()).hexdigest()
            summary = training_summary(job)
            self._write_validation(job, {"ab": 2, "adapter_loaded": True,
                "validation_losses": result.validation_losses,
                "validation_trend": [entry.model_dump() for entry in result.validation_trend],
                "train_losses": [entry.loss for entry in result.step_log],
                "step_log": [entry.model_dump() for entry in result.step_log],
                "recipe_version": RECIPE_VERSION,
                "initial_adapter_digest": result.initial_adapter_digest,
                "sampling": {"seed": config["seed"], "epochs_completed": summary["full_epochs"],
                             **result.sampling, "order": "seeded per epoch", "recipe_version": RECIPE_VERSION},
                "best_checkpoint": result.best_checkpoint.model_dump() if result.best_checkpoint else None,
                "final_checkpoint_step": result.steps,
                "base_model": self.base_model,
                "adapter_sha256": hashlib.sha256((root / "adapter.safetensors").read_bytes()).hexdigest(),
                "warning": snapshot.get("validation_warning"), "base_digest": base_digest,
                "snapshot_digest": job["digest"], "reference_digest": reference_digest,
                "reference_segment_id": reference["id"],
                "inference_config": inference_config,
                "preview_texts": preview["texts"],
                "preview_texts_sha256": preview["texts_sha256"],
                "preview_audio_sha256": preview["audio_sha256"],
                "preview_metrics": {"base": base_preview["metrics"], "trained": preview["metrics"]},
                "base_preview_texts_sha256": base_preview["texts_sha256"],
                "base_preview_audio_sha256": base_preview["audio_sha256"],
                "training_summary": summary})
            self.check(cancel)
            self.stage(value, "completed")
        finally:
            model = None
            gc.collect()
            mx.clear_cache()
            try:
                self.chat.engine.load()
                self.chat.model_status, self.chat.model_error = "ready", None
            except BaseException:
                self.chat.model_status = "failed"
                self.chat.model_error = "试听验证后推理模型恢复失败，请重启应用。"
            try:
                self.chat.transcriber.load()
                self.chat.stt_status, self.chat.stt_error = "ready", None
            except BaseException:
                self.chat.stt_status = "failed"
                self.chat.stt_error = "试听验证后识别模型恢复失败，请重启应用。"

    def verify_snapshot(self, job):
        if sha256(canonical(job["snapshot"]).encode()) != job["digest"]:
            raise HTTPException(409, "训练数据快照摘要不匹配。")
        for segment in job["snapshot"]["segments"]:
            path = self.store.segment_path(segment)
            expected = segment.get("sha256") or segment.get("audio_sha256")
            try:
                with path.open("rb") as audio:
                    actual = hashlib.file_digest(audio, "sha256").hexdigest()
            except OSError:
                actual = None
            if not expected or actual != expected:
                raise HTTPException(409, "训练音频快照已改变或不可用。")
        snapshot = job["snapshot"]
        reference_id = snapshot.get("reference_segment_id")
        if reference_id is not None:
            # The synthesis reference must come from the training subset; a
            # held-out validation clip is an evaluation target, not a prompt.
            index = next((i for i, segment in enumerate(snapshot["segments"])
                          if segment.get("id") == reference_id), None)
            if index is None or index not in (snapshot.get("train_indices") or []):
                raise HTTPException(409, "训练快照的试听参考片段无效。")

    def validate_audio(self, model, job, cancel, variant="trained"):

        """Generate validation audio under the application's conditions.

        Same synthesis helper, frame limit, chunk size and seed as the chat
        engine, so a preview measures what the published voice will produce.
        The job's training seed does not change inference conditions.
        """
        import time
        import numpy as np
        from breezetts2_mac_fast import (STREAMING_FRAMES, SYNTHESIS_SEED, SYNTHESIS_MAX_TOKENS,
                                         inference_signature, pcm16_bytes, pcm_chunks,
                                         synthesis_args, synthesis_interval)
        from contextlib import closing
        from platform_runtime import streaming_frames
        if variant not in ("trained", "base"):
            raise HTTPException(422, "试听变体无效。")
        texts = select_preview_texts(job["snapshot"])
        reference = reference_segment(job)
        if reference is None:
            raise HTTPException(422, "没有可用的试听参考片段。")
        reference_audio = str(self.store.segment_path(reference))
        interval = synthesis_interval(model) * streaming_frames() / STREAMING_FRAMES
        seed = SYNTHESIS_SEED
        root = self.job_directory(job)
        prefix = "validation" if variant == "trained" else "validation-base"
        audio_hashes, metrics = {}, {}
        for index, text in enumerate(texts, 1):
            self.check(cancel)
            target = root / f"{prefix}-{index:02d}.wav"
            # Every text resets the seed, so one preview cannot condition the next.
            args = synthesis_args(text, max_tokens=SYNTHESIS_MAX_TOKENS, seed=seed,
                                  streaming_interval=interval)
            samples, frames, first_ms, started = 0, 0, None, time.monotonic()
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(model.sample_rate)
                with closing(pcm_chunks(model, args, reference_audio,
                                        reference["transcript"])) as chunks:
                    for pcm, chunk_frames, _ in chunks:
                        self.check(cancel)
                        if first_ms is None:
                            first_ms = round((time.monotonic() - started) * 1000, 2)
                        if not np.isfinite(pcm).all():
                            raise ValueError("invalid_audio")
                        frames += chunk_frames
                        samples += len(pcm)
                        wav.writeframes(pcm16_bytes(pcm))
            if not samples:
                raise ValueError("empty_audio")
            elapsed = (time.monotonic() - started) * 1000
            duration = samples / model.sample_rate
            hit_max = frames >= args.max_tokens
            audio_hashes[f"{index:02d}"] = hashlib.sha256(target.read_bytes()).hexdigest()
            metrics[f"{index:02d}"] = {"duration": round(duration, 3),
                                       "synthesis_ms": round(elapsed, 2),
                                       "first_audio_ms": first_ms,
                                       "codec_frames": frames,
                                       "hit_max_tokens": hit_max,
                                       "rtf": round(elapsed / 1000 / duration, 4) if duration > 0 else None}
        return {"texts": texts,
                "texts_sha256": hashlib.sha256(canonical(texts).encode()).hexdigest(),
                "audio_sha256": audio_hashes, "metrics": metrics, "variant": variant,
                "inference": inference_signature(args)}


    def confirm_preview(self, value, body):
        from voice_app import clean_text, timestamp
        if not body.confirmed:
            raise HTTPException(422, "请先勾选试听确认。")
        with self.lock:
            job = self.store.one("training_jobs", value)
            self._require_completed_train(job)
            validation = self._read_validation(job, required=True)
            evidence = self._preview_evidence(job, validation)
            note = body.note.strip()
            if note:
                note = clean_text(note, 300, "备注")
            confirmation = {"confirmed": True, "note": note, **evidence}
            existing = validation.get("confirmation")
            if existing and all(existing.get(key) == item for key, item in confirmation.items()):
                return self.validation_view(job)
            validation["confirmation"] = {**confirmation, "confirmed_at": timestamp()}
            self._write_validation(job, validation)
            return self.validation_view(job)

    def _check_preview_confirmation(self, job, validation):
        confirmation = validation.get("confirmation") or {}
        if not confirmation.get("confirmed"):
            raise HTTPException(409, "请先试听两条验收样本并确认后再发布。")
        evidence = self._preview_evidence(job, validation)
        if any(confirmation.get(key) != item for key, item in evidence.items()):
            raise HTTPException(409, "试听确认与当前产物不匹配，请重新试听并确认。")

    def publish(self, value, name):
        from voice_app import clean_text
        name = clean_text(name, 60, "名称")
        with self.lock, self.chat.admission:
            job = self.store.one("training_jobs", value)
            if self.control or job["kind"] != "train" or job["status"] != "completed":
                raise HTTPException(409, "仅能发布已完成并验证的训练任务。")
            if not self.chat.gate.acquire(blocking=False):
                raise HTTPException(409, "模型正在使用，请稍后发布。")
            self.chat.active_kind = "training_publish"
        try:
            return self.chat.worker.submit(self._publish, job, name).result()
        finally:
            with self.chat.admission:
                self.chat.active_kind = None
                self.chat.gate.release()

    def _publish(self, job, name):
        from voice_app import timestamp
        self.verify_snapshot(job)
        existing = self.store.rows("voice_models", "job_id=?", (job["id"],))
        if existing:
            return self.chat.rows("SELECT * FROM voices WHERE model_id=?", (existing[0]["id"],))[0]
        root = self.job_directory(job)
        validation = self._read_validation(job, required=True)
        if (not validation.get("adapter_loaded") or validation.get("base_model") != self.base_model
                or validation.get("adapter_sha256") != hashlib.sha256((root / "adapter.safetensors").read_bytes()).hexdigest()):
            raise HTTPException(409, "产物基座或 adapter 与验证记录不一致。")
        self._check_preview_confirmation(job, validation)
        model_id, voice_id = str(uuid4()), str(uuid4())
        folder = self.chat.directory / "voice_models" / model_id
        folder.mkdir(parents=True, mode=0o700)
        recording = self.chat.path(self.chat.recordings, voice_id)
        try:
            shutil.copyfile(root / "adapter.safetensors", folder / "adapter.safetensors")
            manifest = {"base_model": self.base_model, "rank": job["config"]["rank"],
                        "base_digest": validation["base_digest"], "sha256": validation["adapter_sha256"]}
            (folder / "manifest.json").write_text(json.dumps(manifest))
            try:
                self.chat.engine.select_adapter(folder)
            finally:
                try:
                    self.chat.engine.select_adapter(None)
                except BaseException:
                    self.chat.model_status = "failed"
                    self.chat.model_error = "发布后推理模型恢复失败，请重启应用。"
                    raise
            reference = reference_segment(job)
            if reference is None:
                raise HTTPException(409, "训练快照没有可用的参考片段。")
            shutil.copyfile(self.store.segment_path(reference), recording)
            with closing(self.chat.connect()) as db, db:
                db.execute("INSERT INTO voice_models(id,job_id,base_model,config,reference_segment_id,created_at) VALUES(?,?,?,?,?,?)",
                           (model_id, job["id"], self.base_model, json.dumps(job["config"]), reference["id"], timestamp()))
                db.execute("INSERT INTO voices(id,name,transcript,duration,model_id) VALUES(?,?,?,?,?)",
                           (voice_id, name, reference["transcript"], reference["end"] - reference["start"], model_id))
            return self.chat.one("voices", voice_id)
        except BaseException:
            recording.unlink(missing_ok=True)
            shutil.rmtree(folder, ignore_errors=True)
            raise


def training_router(service):
    router = APIRouter(prefix="/api/training")

    @router.get("/capabilities")
    def capabilities():
        return service.capabilities()

    @router.get("/datasets")
    def datasets():
        return service.store.datasets()

    @router.post("/datasets", status_code=201)
    def create(body: DatasetInput):
        with service.lock:
            return service.store.create_dataset(body.name, body.speaker_confirmed)

    @router.patch("/datasets/{value}")
    def confirm(value: str, body: DatasetReviewInput):
        with service.dataset_edit(value):
            return service.store.update("training_datasets", value, speaker_confirmed=body.speaker_confirmed)

    @router.post("/datasets/{value}/sources", status_code=201)
    async def upload(value: str, source: UploadFile = File()):
        return await service.upload(value, source)

    @router.post("/datasets/{value}/prepare", status_code=202)
    def prepare(value: str):
        return service.submit(value, "prepare")

    @router.get("/datasets/{value}/metrics")
    def dataset_metrics(value: str):
        return service.metrics_view("dataset", value)

    @router.post("/datasets/{value}/metrics", status_code=202)
    def analyze_dataset(value: str):
        return service.submit_metrics("dataset", value)

    @router.get("/datasets/{value}/metrics/history")
    def metrics_history(value: str):
        return service.metrics_history(value)

    @router.get("/jobs/{value}/metrics")
    def job_metrics(value: str):
        return service.metrics_view("job", value)

    @router.post("/jobs/{value}/metrics", status_code=202)
    def analyze_job(value: str):
        return service.submit_metrics("job", value)

    @router.get("/datasets/{value}/summary")
    def summary(value: str):
        return service.dataset_summary(value)

    @router.get("/datasets/{value}/segments")
    def segments(value: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        return service.store.segments(value, offset, limit)

    @router.get("/segments/{value}/audio")
    def audio(value: str):
        segment = service.store.one("training_segments", value)
        path = service.store.segment_path(segment)
        if not path.is_file():
            raise HTTPException(404, "切片音频不可用。")
        return FileResponse(path, media_type="audio/wav")

    @router.patch("/segments/{value}")
    def edit(value: str, body: SegmentInput):
        segment = service.store.one("training_segments", value)
        with service.dataset_edit(segment["dataset_id"]):
            return service.store.edit_segment(value, body.transcript, body.review_status)

    @router.patch("/datasets/{value}/reference")
    def reference(value: str, body: ReferenceInput):
        with service.dataset_edit(value):
            dataset = service.store.set_preferred_reference(value, body.segment_id)
        return service.dataset_summary(dataset["id"])

    @router.post("/datasets/{value}/jobs", status_code=202)
    def train(value: str, body: TrainingConfig, idempotency_key: str | None = Header(default=None)):
        return service.submit(value, "train", body.checked(), idempotency_key)

    @router.get("/jobs")
    def jobs(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        with closing(service.chat.connect()) as db:
            return [service.store._decode(row) for row in db.execute(
                "SELECT * FROM training_jobs ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset))]

    @router.get("/jobs/{value}")
    def job(value: str):
        record = service.store.one("training_jobs", value)
        if record["kind"] == "train":
            record["preview"] = service.validation_view(record)
        return record
    @router.get("/jobs/{value}/export")
    def export(value: str):
        return service.export_task(value)

    @router.post("/jobs/{value}/result", status_code=202)
    def import_result(value: str, result: UploadFile = File()):
        if result.size is None or result.size > MAX_MANIFEST + MAX_ADAPTER + 4096:
            raise HTTPException(413, "训练结果包过大。")
        return service.import_result(value, result.file)

    @router.get("/jobs/{value}/preview/{index}")
    def preview(value: str, index: int, variant: str = Query("trained")):
        job = service.store.one("training_jobs", value)
        service._require_completed_train(job)
        return FileResponse(service.preview_file(job, index, variant), media_type="audio/wav")

    @router.post("/jobs/{value}/variants", status_code=202)
    def variant(value: str, body: VariantInput, idempotency_key: str | None = Header(default=None)):
        return service.submit_variant(value, body.steps, idempotency_key)

    @router.post("/jobs/{value}/preview-confirm")
    def confirm_preview(value: str, body: PreviewInput):
        return service.confirm_preview(value, body)

    @router.post("/jobs/{value}/cancel")
    def cancel(value: str):
        return service.cancel(value)

    @router.post("/jobs/{value}/retry", status_code=202)
    def retry(value: str, body: RetryInput = RetryInput()):
        return service.retry(value)

    @router.post("/jobs/{value}/publish", status_code=201)
    def publish(value: str, body: PublishInput):
        return service.publish(value, body.name)

    return router
