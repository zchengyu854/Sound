"""Bounded local training jobs; all MLX work uses the chat inference thread."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import threading
from uuid import uuid4
import wave

from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from training_store import TrainingStore, canonical

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


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(default=4)
    steps: int = Field(default=20, ge=1, le=1000)
    learning_rate: float = Field(default=0.0001, ge=0.000001, le=0.001, allow_inf_nan=False)
    accumulation_steps: int = Field(default=1, ge=1, le=8)
    checkpoint_every: int = Field(default=5, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)

    def checked(self):
        if self.rank not in (1, 2, 4, 8):
            raise HTTPException(422, "rank 必须为 1、2、4 或 8。")
        return self.model_dump()


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
    return {
        "training_segments": len(training),
        "training_seconds": round(seconds, 2),
        "steps": int(config.get("steps", 0)),
        "samples_per_step": int(config.get("accumulation_steps", 1)),
        "sample_visits": visits,
        "unique_segments_seen": min(visits, len(training)),
        "approx_passes": round(visits / len(training), 2) if training else 0,
        "independent_validation": bool(snapshot.get("validation_indices")),
    }


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
        if kind == "dataset":
            with self.lock:
                dataset = self.store.one("training_datasets", value)
                segments = self.store.rows("training_segments", "dataset_id=?", (value,))
            segments = sorted((segment for segment in segments if segment["review_status"] != "excluded"),
                              key=lambda segment: (segment["source_id"], segment["start"], segment["id"]))
            reference_segments, reference_kind = segments, "current"
            texts, previews, evidence = [], [], None
            version = dataset["version"]
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
            texts = validation["preview_texts"]
            previews = [self.preview_file(job, index) for index in (1, 2)]
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
                  "reference_kind": reference_kind, "reference_ids": [],
                  "preview_texts": texts, "preview_audio_sha256": evidence["preview_audio_sha256"] if evidence else {}}
        return entries, references, previews, source

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
    def run_metrics(self, kind, value, path):
        from voice_metrics import (SPEAKER_MODEL_ID, MIN_REFERENCE_SECONDS, MAX_REFERENCES,
                                   SpeakerEmbeddingModel, compute_dataset_metrics)
        result = None
        try:
            self.check(self.metrics_cancel)
            entries, references, previews, source = self.metrics_inputs(kind, value)
            original = compute_dataset_metrics([entry["path"] for entry in entries])
            generated = compute_dataset_metrics(previews) if previews else None
            per_preview = [self.quality_view(compute_dataset_metrics([preview])) for preview in previews]
            result = {"analyzed_at": datetime.now(timezone.utc).isoformat(), "source": source,
                      "quality": {"original": self.quality_view(original),
                                  "generated": self.quality_view(generated) if generated else None,
                                  "previews": per_preview},
                      "features": {"status": "pending", "model": SPEAKER_MODEL_ID,
                                   "revision": None, "algorithm": "speaker-cosine-v1",
                                   "reference_count": 0, "pair_count": 0, "original_index": None,
                                   "generated_index": None, "previews": [], "message": ""},
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
                            [entry["path"] for entry in eligible], previews).result()
                    self.check(self.metrics_cancel)
                    consistency = scores["reference_consistency"]
                    aggregate = scores["generated_aggregate"]
                    by_path = {str(entry["path"]): entry["id"] for entry in references}
                    source["reference_ids"] = [by_path[item["path"]] for item in scores["references"]
                                               if item["status"] == "ok"]
                    count = consistency["reference_count"]
                    feature = result["features"]
                    feature.update({"status": "completed" if count >= 2 else "insufficient",
                        "message": "" if count >= 2 else "有效原声片段不足两条，无法计算原声内部一致性。",
                        "revision": (scores.get("model") or {}).get("revision"),
                        "reference_count": count, "pair_count": consistency["pair_count"],
                        "original_index": consistency["score"],
                        "generated_index": aggregate["score"] if aggregate["sample_count"] == 2 else None,
                        "generated_count": aggregate["sample_count"],
                        "previews": [{"index": index, "value": item["score"],
                                      "reference_count": item["reference_count"], "status": item["status"],
                                      "reason": item["reason"]}
                                     for index, item in enumerate(scores["generated"], 1)]})
                    if scores.get("model_error"):
                        feature.update(status="failed", message="音色模型加载失败，请检查模型缓存后重试。")
                    elif count == 0:
                        feature.update(status="failed", message="参考片段特征提取失败，请检查音频后重试。")
                    elif previews and aggregate["sample_count"] != 2:
                        feature["message"] = (feature["message"] + " 两条试听未能全部提取特征，请检查音频并重试。 ").strip()
                    if feature["revision"] and feature["generated_index"] is not None:
                        comparison = {"material_digest": source["material_digest"],
                                      "reference_kind": source["reference_kind"],
                                      "reference_ids": source["reference_ids"], "preview_texts": source["preview_texts"],
                                      "model": feature["model"], "revision": feature["revision"],
                                      "algorithm": feature["algorithm"], "quality_algorithm": "pcm-20ms-v1"}
                        result["comparison_key"] = hashlib.sha256(canonical(comparison).encode()).hexdigest()
                except Cancelled:
                    raise
                except Exception as exc:
                    log.error(json.dumps({"task_id": value, "stage": "metrics_features", "error_type": type(exc).__name__}))
                    result["features"].update(status="failed", message="音色模型下载或加载失败，请检查网络与本机缓存后重试。")
                finally:
                    model.close()
            else:
                result["features"].update(status="insufficient", message="没有足够长且非静音的参考片段。")
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
            self._dispatch(job)
            return job

    def _dispatch(self, job, resume=False):
        cancel = threading.Event()
        self.control = (job["id"], cancel)
        try:
            self.executor.submit(self.run, job["id"], cancel, resume)
        except BaseException:
            self.control = None
            self.store.update("training_jobs", job["id"], status="failed", error_code="dispatch_failed")
            raise HTTPException(503, "任务投递失败。") from None

    def cancel(self, value):
        with self.lock:
            job = self.store.one("training_jobs", value)
            if (self.control and self.control[0] == value
                    and job["status"] not in ("completed", "review_required", "failed", "cancelled")):
                self.store.update("training_jobs", value, status="cancelling")
                self.control[1].set()
            return self.store.one("training_jobs", value)

    def retry(self, value, resume):
        with self.lock, self.chat.admission:
            job = self.store.one("training_jobs", value)
            if self.control or self.uploading or job["status"] not in ("failed", "cancelled"):
                raise HTTPException(409, "只能重试已经退出的失败或取消任务。")
            if self.stopping or not self.chat.accepting_tasks:
                raise HTTPException(503, "应用正在停止。")
            if resume and job["kind"] == "train" and job.get("checkpoint"):
                self.validate_checkpoint(job)
            self.store.update("training_jobs", value, status="queued", stage="queued", error_code=None,
                              attempt=job["attempt"] + 1)
            self._dispatch(self.store.one("training_jobs", value), resume)
            return self.store.one("training_jobs", value)

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

    def run(self, value, cancel, resume):
        try:
            job = self.store.one("training_jobs", value)
            self.check(cancel)
            if job["kind"] == "prepare":
                self.prepare(job, cancel)
            else:
                with self.model_slot(value, cancel):
                    self.chat.worker.submit(self.train, job, cancel, resume).result()
        except Cancelled:
            self.stage(value, "cancelled", error_code="cancelled")
        except BaseException as exc:
            code = getattr(exc, "code", "training_failed")
            if cancel.is_set():
                self.stage(value, "cancelled", error_code="cancelled")
            else:
                self.stage(value, "failed", error_code=code)
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
                            text = self.chat.worker.submit(self.transcribe_clip, destination, cancel).result()
                            if not text:
                                hints.append("no_speech")
                        except Cancelled:
                            raise
                        except Exception:
                            hints.append("transcription_failed_edit_required")
                    self.store.add_segment(dataset_id, source["id"], segment_id, clip["start"], clip["end"], text, hints)
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
        text = self.chat.transcriber.transcribe(samples)
        self.check(cancel)
        return text.strip()[:2000] if isinstance(text, str) else ""

    def job_directory(self, job):
        from voice_app import identifier
        value = identifier(job["id"])
        directory = self.store.dataset_dir(job["dataset_id"]) / "jobs" / value
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.resolve().is_relative_to(self.chat.directory.resolve()):
            raise HTTPException(422, "任务路径无效。")
        return directory

    def checkpoint_directory(self, job):
        name = job.get("checkpoint")
        if not name or Path(name).name != name:
            raise HTTPException(409, "没有可恢复检查点。")
        directory = self.job_directory(job)
        path = (directory / name).resolve()
        if path.parent != directory.resolve():
            raise HTTPException(409, "检查点路径无效。")
        return path

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
                "ab": validation.get("ab", 0),
                "preview_metrics": validation.get("preview_metrics"),
                "train_losses": validation.get("train_losses")}

    def validate_checkpoint(self, job):
        try:
            path = self.checkpoint_directory(job)
            metadata = json.loads((path / "metadata.json").read_text())
            if any(metadata[key] != expected for key, expected in (
                ("base_model", self.base_model), ("digest", job["digest"]), ("config", job["config"]))):
                raise ValueError
            if not all((path / name).is_file() for name in ("adapter.safetensors", "optimizer.safetensors", "random.safetensors")):
                raise ValueError
            return metadata
        except (OSError, KeyError, ValueError):
            raise HTTPException(409, "检查点与基座、数据或配置不兼容，不能恢复。") from None

    def save_checkpoint(self, job, model, optimizer, step, base_digest):
        import mlx.core as mx
        from mlx.utils import tree_flatten
        from breeze_training import save_adapter
        root = self.job_directory(job)
        name = f"checkpoint-{step}-{uuid4()}"
        temporary = root / (name + ".tmp")
        temporary.mkdir(mode=0o700)
        try:
            save_adapter(model, temporary / "adapter.safetensors")
            mx.save_safetensors(str(temporary / "optimizer.safetensors"), dict(tree_flatten(optimizer.state)))
            mx.save_safetensors(str(temporary / "random.safetensors"), {str(i): a for i, a in enumerate(mx.random.state)})
            metadata = {"base_model": self.base_model, "base_digest": base_digest, "digest": job["digest"],
                        "config": job["config"], "step": step}
            (temporary / "metadata.json").write_text(json.dumps(metadata, sort_keys=True))
            temporary.replace(root / name)
            self.store.update("training_jobs", job["id"], checkpoint=name, steps_done=step)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def train(self, job, cancel, resume):
        import gc
        import math
        import numpy as np
        import mlx.core as mx
        import mlx.nn as nn
        import mlx.optimizers as optim
        from mlx.utils import tree_flatten, tree_map, tree_unflatten
        from mlx_audio.tts.utils import load_model
        from breeze_training import (attach_lora, frozen_digest, load_adapter, save_adapter,
                                     teacher_forcing_loss, validate_example)
        model = gradient_fn = optimizer = None
        config = job["config"]
        value = job["id"]
        try:
            self.stage(value, "preparing", steps_total=config["steps"])
            self.check(cancel)
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
            model = load_model(self.base_model)
            attach_lora(model, config["rank"])
            base_digest = frozen_digest(model)
            optimizer = optim.Adam(config["learning_rate"])
            mx.random.seed(config["seed"])
            step = 0
            if resume and job.get("checkpoint"):
                metadata = self.validate_checkpoint(job)
                if metadata["base_digest"] != base_digest:
                    raise HTTPException(409, "基座权重与检查点不一致。")
                directory = self.checkpoint_directory(job)
                load_adapter(model, directory / "adapter.safetensors")
                optimizer.state = tree_unflatten(list(mx.load(str(directory / "optimizer.safetensors")).items()))
                random = mx.load(str(directory / "random.safetensors"))
                mx.random.state = [random[str(i)] for i in range(len(random))]
                step = metadata["step"]
            snapshot = job["snapshot"]
            examples = snapshot["segments"]
            training = [examples[i] for i in snapshot["train_indices"]]
            if not training:
                raise HTTPException(422, "没有可训练片段。")
            self.verify_snapshot(job)
            cache = self.job_directory(job) / "tokens"
            cache.mkdir(exist_ok=True, mode=0o700)

            def example(segment):
                self.check(cancel)
                target = cache / f"{segment['id']}.safetensors"
                if target.is_file():
                    arrays = mx.load(str(target))
                    prompt, codes = arrays["prompt"], arrays["codes"]
                else:
                    path = self.store.segment_path(segment)
                    with wave.open(str(path), "rb") as wav:
                        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
                    codes = mx.stop_gradient(model._encode_reference(mx.array(audio)))
                    prompt = mx.stop_gradient(model._prompt_embeddings(segment["transcript"], voice=None,
                                                    instruct=None, ref_audio=None, ref_text=None))
                    mx.eval(prompt, codes)
                    validate_example(model, prompt, codes)
                    temp = target.with_suffix(".tmp.safetensors")
                    mx.save_safetensors(str(temp), {"prompt": prompt, "codes": codes})
                    temp.replace(target)
                validate_example(model, prompt, codes)
                return prompt, codes

            gradient_fn = nn.value_and_grad(model, teacher_forcing_loss)
            self.stage(value, "training", steps_done=step)
            loss_log = []
            while step < config["steps"]:
                self.check(cancel)
                accumulated = None
                for micro in range(config["accumulation_steps"]):
                    index = (step * config["accumulation_steps"] + micro) % len(training)
                    prompt, codes = example(training[index])
                    loss, gradients = gradient_fn(model, prompt, codes)
                    mx.eval(loss, gradients)
                    if len(loss_log) < 200:
                        loss_log.append(round(loss.item(), 6))
                    if not math.isfinite(loss.item()) or not all(bool(mx.all(mx.isfinite(g)).item()) for _, g in tree_flatten(gradients)):
                        raise ValueError("nonfinite_gradient")
                    scaled = tree_map(lambda g: g / config["accumulation_steps"], gradients)
                    accumulated = scaled if accumulated is None else tree_map(lambda a, b: a + b, accumulated, scaled)
                    self.check(cancel)
                optimizer.update(model, accumulated)
                mx.eval(model.trainable_parameters(), optimizer.state)
                step += 1
                self.store.update("training_jobs", value, steps_done=step)
                if step % config["checkpoint_every"] == 0 or step == config["steps"]:
                    self.save_checkpoint(job, model, optimizer, step, base_digest)
            self.check(cancel)
            self.stage(value, "validating")
            if frozen_digest(model) != base_digest:
                raise ValueError("base_weights_changed")
            validation = [examples[i] for i in snapshot["validation_indices"]]
            validation_losses = []
            for segment in validation:
                prompt, codes = example(segment)
                score = teacher_forcing_loss(model, prompt, codes)
                mx.eval(score)
                if not math.isfinite(score.item()):
                    raise ValueError("validation_nonfinite")
                validation_losses.append(score.item())
            root = self.job_directory(job)
            save_adapter(model, root / "adapter.safetensors")
            gradient_fn = optimizer = model = None
            gc.collect()
            mx.clear_cache()
            # Versioned A/B evidence: same text, same reference, same inference
            # config and reset seed; base and trained audio generated in order
            # so only one model is resident at a time.
            inference_config = {"max_tokens": 120, "seed": config["seed"]}
            reference = snapshot["segments"][0]
            model = load_model(self.base_model)
            attach_lora(model, config["rank"])
            mx.random.seed(config["seed"])
            base_preview = self.validate_audio(model, job, cancel, variant="base")
            model = None
            gc.collect()
            mx.clear_cache()
            model = load_model(self.base_model)
            attach_lora(model, config["rank"])
            load_adapter(model, root / "adapter.safetensors")
            mx.random.seed(config["seed"])
            preview = self.validate_audio(model, job, cancel, variant="trained")
            reference_digest = hashlib.sha256(canonical({"id": reference["id"],
                "transcript": reference["transcript"], "audio_sha256": reference.get("audio_sha256", "")}).encode()).hexdigest()
            self._write_validation(job, {"ab": 2, "adapter_loaded": True,
                "validation_losses": validation_losses, "train_losses": loss_log,
                "base_model": self.base_model,
                "adapter_sha256": hashlib.sha256((root / "adapter.safetensors").read_bytes()).hexdigest(),
                "warning": snapshot.get("validation_warning"), "base_digest": base_digest,
                "snapshot_digest": job["digest"], "reference_digest": reference_digest,
                "inference_config": inference_config,
                "preview_texts": preview["texts"],
                "preview_texts_sha256": preview["texts_sha256"],
                "preview_audio_sha256": preview["audio_sha256"],
                "preview_metrics": {"base": base_preview["metrics"], "trained": preview["metrics"]},
                "base_preview_texts_sha256": base_preview["texts_sha256"],
                "base_preview_audio_sha256": base_preview["audio_sha256"],
                "training_summary": training_summary(job)})
            self.check(cancel)
            self.stage(value, "completed")
        finally:
            gradient_fn = optimizer = model = None
            gc.collect()
            mx.clear_cache()
            try:
                self.chat.engine.load()
                self.chat.model_status, self.chat.model_error = "ready", None
            except BaseException:
                self.chat.model_status = "failed"
                self.chat.model_error = "训练后推理模型恢复失败，请重启应用。"
            try:
                self.chat.transcriber.load()
                self.chat.stt_status, self.chat.stt_error = "ready", None
            except BaseException:
                self.chat.stt_status = "failed"
                self.chat.stt_error = "训练后识别模型恢复失败，请重启应用。"

    def verify_snapshot(self, job):
        canonical = json.dumps(job["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(canonical.encode()).hexdigest() != job["digest"]:
            raise HTTPException(409, "训练数据快照摘要不匹配。")
        for segment in job["snapshot"]["segments"]:
            path = self.store.segment_path(segment)
            expected = segment.get("sha256") or segment.get("audio_sha256")
            if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise HTTPException(409, "训练音频快照已改变或不可用。")

    def validate_audio(self, model, job, cancel, variant="trained"):
        import time
        import numpy as np
        from breezetts2_mac_fast import pcm16_bytes
        from contextlib import closing
        if variant not in ("trained", "base"):
            raise HTTPException(422, "试听变体无效。")
        texts = select_preview_texts(job["snapshot"])
        reference = job["snapshot"]["segments"][0]
        reference_audio = str(self.store.segment_path(reference))
        root = self.job_directory(job)
        prefix = "validation" if variant == "trained" else "validation-base"
        audio_hashes, metrics = {}, {}
        for index, text in enumerate(texts, 1):
            self.check(cancel)
            target = root / f"{prefix}-{index:02d}.wav"
            samples, first_ms, started = 0, None, time.monotonic()
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(model.sample_rate)
                with closing(model.generate(text=text, max_tokens=120, stream=True,
                                            ref_audio=reference_audio, ref_text=reference["transcript"])) as results:
                    for result in results:
                        self.check(cancel)
                        if first_ms is None:
                            first_ms = round((time.monotonic() - started) * 1000, 2)
                        audio = np.array(result.audio, dtype=np.float32).reshape(-1)
                        if not np.isfinite(audio).all():
                            raise ValueError("invalid_audio")
                        samples += audio.size
                        wav.writeframes(pcm16_bytes(audio))
            if not samples:
                raise ValueError("empty_audio")
            elapsed = (time.monotonic() - started) * 1000
            duration = samples / model.sample_rate
            audio_hashes[f"{index:02d}"] = hashlib.sha256(target.read_bytes()).hexdigest()
            metrics[f"{index:02d}"] = {"duration": round(duration, 3),
                                       "synthesis_ms": round(elapsed, 2),
                                       "first_audio_ms": first_ms,
                                       "rtf": round(elapsed / 1000 / duration, 4) if duration > 0 else None}
        return {"texts": texts,
                "texts_sha256": hashlib.sha256(canonical(texts).encode()).hexdigest(),
                "audio_sha256": audio_hashes, "metrics": metrics, "variant": variant}

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
            reference = job["snapshot"]["segments"][0]
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
        return service.retry(value, body.resume)

    @router.post("/jobs/{value}/publish", status_code=201)
    def publish(value: str, body: PublishInput):
        return service.publish(value, body.name)

    return router
