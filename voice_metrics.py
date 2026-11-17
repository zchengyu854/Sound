"""Streaming PCM quality metrics and caller-thread speaker similarity.

All input files must be uncompressed 24 kHz integer PCM WAV (8/16/24/32 bit,
any channel count). Energy is averaged across channels; clipping counts each
channel sample. Only complete, non-overlapping 20 ms windows count toward the
low-energy fraction. A trailing partial window still contributes to RMS and
clipping. Silent files have unavailable individual metrics but contribute their
samples and complete windows when a dataset is aggregated.

Reference selection is deterministic: sort and de-duplicate paths, retain the
first eight non-silent files of at least one second, then encode exactly that
fixed selection. Failed encodings are reported and are not replaced. Embeddings
use at most the first 12 seconds, downmixed to mono. These limits bound memory;
the complete files are still streamed for quality metrics and eligibility.

No model is loaded or downloaded on import or construction. Model loading and
inference run synchronously on the calling thread; callers own scheduling and
any inference gate. The Hugging Face downloader may use its own I/O worker.
"""

import math
from pathlib import Path
import re
import struct
import wave

import numpy as np


SAMPLE_RATE = 24000
WINDOW_SAMPLES = 480
CHUNK_SAMPLES = 24000
LOW_ENERGY_POWER = 10 ** (-40 / 10)
CLIP_LEVEL = 0.999
MAX_REFERENCES = 8
MIN_REFERENCE_SECONDS = 1.0
MAX_EMBEDDING_SECONDS = 12
# The installed extractor's spectrogram requires more than 384 samples.
MIN_EMBEDDING_SAMPLES = 385
SPEAKER_MODEL_ID = "marksverdhei/Qwen3-Voice-Embedding-12Hz-1.7B"
MODEL_FILES = ["config.json", "model.safetensors"]


def _open_pcm(path):
    source = wave.open(str(path), "rb")
    try:
        if (source.getframerate() != SAMPLE_RATE or source.getcomptype() != "NONE"
                or source.getnchannels() < 1 or source.getsampwidth() not in (1, 2, 3, 4)):
            raise ValueError("expected 24 kHz uncompressed 8/16/24/32-bit PCM WAV")
        return source
    except Exception:
        source.close()
        raise


def _decode_pcm(data, width):
    if width == 1:
        return (np.frombuffer(data, dtype=np.uint8).astype(np.float64) - 128) / 128
    if width == 2:
        return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768
    if width == 4:
        return np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648
    octets = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
    values = (values ^ 0x800000) - 0x800000
    return values.astype(np.float64) / 8388608


def _empty_scan(path, reason, detail=None):
    return {"path": str(path), "status": "unavailable", "reason": reason,
            "detail": detail, "channel_count": None, "audio_frame_count": 0,
            "sample_count": 0, "window_count": 0, "low_energy_window_count": 0,
            "clipped_sample_count": 0, "sum_squares": 0.0}


def _scan_audio(path):
    try:
        with _open_pcm(path) as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            total_frames = source.getnframes()
            if total_frames == 0:
                result = _empty_scan(path, "empty")
                result["channel_count"] = channels
                return result
            remaining = total_frames
            sum_squares = 0.0
            clipped = 0
            low_windows = 0
            window_frames = 0
            window_power = 0.0
            # Limit each read by scalar samples, even for multichannel inputs.
            chunk_frames = max(1, CHUNK_SAMPLES // channels)
            while remaining:
                count = min(chunk_frames, remaining)
                data = source.readframes(count)
                if len(data) != count * channels * width:
                    raise ValueError("truncated PCM WAV data")
                values = _decode_pcm(data, width)
                clipped += int(np.count_nonzero(np.abs(values) >= CLIP_LEVEL))
                powers = np.sum((values * values).reshape(-1, channels), axis=1)
                sum_squares += float(np.sum(powers))
                position = 0
                while position < count:
                    part = min(WINDOW_SAMPLES - window_frames, count - position)
                    window_power += float(np.sum(powers[position:position + part]))
                    window_frames += part
                    position += part
                    if window_frames == WINDOW_SAMPLES:
                        if window_power < LOW_ENERGY_POWER * WINDOW_SAMPLES * channels:
                            low_windows += 1
                        window_frames = 0
                        window_power = 0.0
                remaining -= count
            silent = sum_squares == 0.0
            return {"path": str(path), "status": "unavailable" if silent else "ok",
                    "reason": "silent" if silent else None, "detail": None,
                    "channel_count": channels, "audio_frame_count": total_frames,
                    "sample_count": total_frames * channels,
                    "window_count": total_frames // WINDOW_SAMPLES,
                    "low_energy_window_count": low_windows,
                    "clipped_sample_count": clipped, "sum_squares": sum_squares}
    except (OSError, EOFError, wave.Error, ValueError, OverflowError, struct.error) as exc:
        return _empty_scan(path, "invalid_audio", f"{type(exc).__name__}: {exc}")


def _audio_result(scan):
    result = {key: value for key, value in scan.items() if key != "sum_squares"}
    result["duration_seconds"] = scan["audio_frame_count"] / SAMPLE_RATE
    available = scan["status"] == "ok"
    result["rms_dbfs"] = (10 * math.log10(scan["sum_squares"] / scan["sample_count"])
                          if available else None)
    result["low_energy_fraction"] = (scan["low_energy_window_count"] / scan["window_count"]
                                     if available and scan["window_count"] else None)
    result["clipping_fraction"] = (scan["clipped_sample_count"] / scan["sample_count"]
                                   if available else None)
    return result


def compute_audio_metrics(path):
    """Return quality metrics and counts for one WAV; unavailable values are None.

    A non-silent file shorter than 20 ms has no complete window, so only its
    low_energy_fraction is None. Empty, silent, and invalid files have status
    'unavailable' and reasons 'empty', 'silent', and 'invalid_audio'.
    """
    return _audio_result(_scan_audio(path))


def compute_dataset_metrics(paths):
    """Aggregate WAV quality, weighting RMS by time and clipping by samples.

    Returns an aggregate with metric/count keys, a 'files' list, and file
    counts. Low-energy fractions are weighted by analyzed
    duration (complete 20 ms windows). Silent files contribute zeros; empty and
    invalid files do not contribute. No valid non-silent audio means unavailable.
    """
    scans = [_scan_audio(path) for path in paths]
    included = [item for item in scans if item["status"] == "ok" or item["reason"] == "silent"]
    total_frames = sum(item["audio_frame_count"] for item in included)
    total_samples = sum(item["sample_count"] for item in included)
    windows = sum(item["window_count"] for item in included)
    low_windows = sum(item["low_energy_window_count"] for item in included)
    clipped = sum(item["clipped_sample_count"] for item in included)
    # Divide out the channel count first, so stereo and mono have equal weight
    # per unit of time rather than weighting stereo files twice as heavily.
    power_sum = sum(item["sum_squares"] / item["channel_count"] for item in included)
    available = power_sum > 0
    reason = None if available else ("silent" if included else "no_valid_audio")
    return {
        "status": "ok" if available else "unavailable", "reason": reason,
        "duration_seconds": total_frames / SAMPLE_RATE, "audio_frame_count": total_frames,
        "sample_count": total_samples, "window_count": windows,
        "low_energy_window_count": low_windows, "clipped_sample_count": clipped,
        "rms_dbfs": 10 * math.log10(power_sum / total_frames) if available else None,
        "low_energy_fraction": low_windows / windows if available and windows else None,
        "clipping_fraction": clipped / total_samples if available else None,
        "file_count": len(scans), "valid_file_count": len(included),
        "non_silent_file_count": sum(item["status"] == "ok" for item in scans),
        "unavailable_file_count": sum(item["status"] != "ok" for item in scans),
        "files": [_audio_result(item) for item in scans],
    }


class SpeakerModelError(RuntimeError):
    """Downloading or initializing the speaker model failed."""


class SpeakerEmbeddingModel:
    """Lazy local Zonos2 speaker encoder, reusable by a caller-owned gate.

    metadata identifies the model and records a commit SHA when the downloader
    returns its standard snapshots/<commit> directory. Requested revisions can
    be branches or tags, and are never reported as an actual immutable SHA.
    """

    def __init__(self, revision=None):
        self.metadata = {"id": SPEAKER_MODEL_ID, "requested_revision": revision, "revision": None}
        self._extractor = None
        self._directory = None

    def download(self):
        if self._directory is not None:
            return self
        try:
            from huggingface_hub import snapshot_download

            directory = Path(snapshot_download(
                repo_id=SPEAKER_MODEL_ID, revision=self.metadata["requested_revision"],
                allow_patterns=MODEL_FILES, max_workers=1,
            ))
            if directory.parent.name == "snapshots" and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", directory.name):
                self.metadata["revision"] = directory.name.lower()
            for name in MODEL_FILES:
                if not (directory / name).is_file():
                    raise FileNotFoundError(f"speaker model file missing: {name}")
            self._directory = directory
        except Exception as exc:
            raise SpeakerModelError(f"{type(exc).__name__}: {exc}") from exc
        return self

    def load(self):
        if self._extractor is not None:
            return self
        self.download()
        try:
            from mlx_audio.tts.models.zonos2.speaker_encoder import Zonos2SpeakerEmbeddingExtractor
            self._extractor = Zonos2SpeakerEmbeddingExtractor(self._directory, sample_rate=SAMPLE_RATE)
        except Exception as exc:
            raise SpeakerModelError(f"{type(exc).__name__}: {exc}") from exc
        return self

    def encode(self, audio, *, sample_rate=SAMPLE_RATE):
        self.load()
        return _normalize_embedding(self._extractor.encode(audio, sample_rate=sample_rate))

    def close(self):
        self._extractor = None


def _normalize_embedding(embedding):
    values = np.asarray(embedding, dtype=np.float64)
    if values.ndim == 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 1 or not values.size or not np.all(np.isfinite(values)):
        raise ValueError("speaker embedding must be a nonempty finite vector")
    norm = float(np.linalg.norm(values))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("speaker embedding has invalid or zero norm")
    return values / norm


def _embedding_audio(path):
    with _open_pcm(path) as source:
        channels, width = source.getnchannels(), source.getsampwidth()
        frames = min(source.getnframes(), MAX_EMBEDDING_SECONDS * SAMPLE_RATE)
        if frames < MIN_EMBEDDING_SAMPLES:
            raise ValueError(f"speaker audio requires at least {MIN_EMBEDDING_SAMPLES} samples")
        result = np.empty(frames, dtype=np.float32)
        position = 0
        chunk_frames = max(1, CHUNK_SAMPLES // channels)
        while position < frames:
            count = min(chunk_frames, frames - position)
            data = source.readframes(count)
            if len(data) != count * channels * width:
                raise ValueError("truncated PCM WAV data")
            values = _decode_pcm(data, width).reshape(-1, channels)
            result[position:position + count] = np.mean(values, axis=1)
            position += count
        if not np.any(result):
            raise ValueError("embedding window is silent after downmix")
        return result


def _similarity(left, right):
    return 100.0 * float(np.clip(np.dot(left, right), 0.0, 1.0))


def compute_speaker_similarity(reference_paths, generated_paths, *, model=None):
    """Score references and generated WAVs against the same selected references.

    Returns sorted reference records, generated records in input order, a
    distinct-pair reference_consistency score, and an equally per-generated-file
    generated_aggregate score. Each score is mean clipped cosine times 100;
    counts accompany scores. The optional model supplies encode(audio,
    sample_rate=24000), and can be reused or mocked. Failures are explicit.
    """
    model = model if model is not None else SpeakerEmbeddingModel()
    reference_records = []
    selected = []
    eligible_count = 0
    reference_paths = [str(Path(path)) for path in reference_paths]
    for path in sorted(set(reference_paths)):
        scan = _scan_audio(path)
        record = {"path": path, "status": "excluded", "reason": scan["reason"],
                  "detail": scan["detail"], "duration_seconds": scan["audio_frame_count"] / SAMPLE_RATE,
                  "embedding_duration_seconds": None}
        if scan["status"] == "ok":
            if scan["audio_frame_count"] < MIN_REFERENCE_SECONDS * SAMPLE_RATE:
                record["reason"] = "too_short"
            else:
                eligible_count += 1
                if len(selected) >= MAX_REFERENCES:
                    record["reason"] = "reference_limit"
                else:
                    record["status"] = "selected"
                    record["reason"] = None
                    selected.append(record)
        reference_records.append(record)

    vectors = []
    model_error = None
    for record in selected:
        if model_error is not None:
            record.update(status="unavailable", reason="model_load_failed", detail=model_error)
            continue
        try:
            audio = _embedding_audio(record["path"])
            record["embedding_duration_seconds"] = len(audio) / SAMPLE_RATE
            vector = _normalize_embedding(model.encode(audio, sample_rate=SAMPLE_RATE))
            if vectors and vector.shape != vectors[0].shape:
                raise ValueError("speaker embedding dimensions do not match")
            vectors.append(vector)
            record.update(status="ok", reason=None, detail=None)
        except SpeakerModelError as exc:
            model_error = str(exc)
            record.update(status="unavailable", reason="model_load_failed", detail=model_error)
        except Exception as exc:
            record.update(status="unavailable", reason="embedding_failed",
                          detail=f"{type(exc).__name__}: {exc}")

    pair_scores = [_similarity(vectors[i], vectors[j])
                   for i in range(len(vectors)) for j in range(i + 1, len(vectors))]
    reference_consistency = {
        "status": "ok" if pair_scores else "unavailable",
        "reason": None if pair_scores else ("model_load_failed" if model_error else "insufficient_references"),
        "score": sum(pair_scores) / len(pair_scores) if pair_scores else None,
        "reference_count": len(vectors), "pair_count": len(pair_scores),
    }

    generated_records = []
    for path in generated_paths:
        path = str(Path(path))
        scan = _scan_audio(path)
        record = {"path": path, "status": "unavailable", "reason": scan["reason"],
                  "detail": scan["detail"], "duration_seconds": scan["audio_frame_count"] / SAMPLE_RATE,
                  "embedding_duration_seconds": None, "score": None,
                  "reference_count": len(vectors), "comparison_count": 0}
        if scan["status"] == "ok":
            if not vectors:
                record.update(reason="model_load_failed" if model_error else "no_usable_references",
                              detail=model_error)
            elif scan["audio_frame_count"] < MIN_EMBEDDING_SAMPLES:
                record.update(reason="too_short", detail=None)
            else:
                try:
                    audio = _embedding_audio(path)
                    record["embedding_duration_seconds"] = len(audio) / SAMPLE_RATE
                    vector = _normalize_embedding(model.encode(audio, sample_rate=SAMPLE_RATE))
                    if vector.shape != vectors[0].shape:
                        raise ValueError("speaker embedding dimensions do not match")
                    scores = [_similarity(vector, reference) for reference in vectors]
                    record.update(status="ok", reason=None, detail=None,
                                  score=sum(scores) / len(scores), comparison_count=len(scores))
                except Exception as exc:
                    record.update(reason="embedding_failed", detail=f"{type(exc).__name__}: {exc}")
        generated_records.append(record)

    scores = [item["score"] for item in generated_records if item["status"] == "ok"]
    generated_aggregate = {
        "status": "ok" if scores else "unavailable",
        "reason": None if scores else ("model_load_failed" if model_error else "no_usable_generated_audio"),
        "score": sum(scores) / len(scores) if scores else None,
        "sample_count": len(scores),
        "comparison_count": sum(item["comparison_count"] for item in generated_records),
        "reference_count": len(vectors),
    }
    metadata = getattr(model, "metadata", None)
    return {"model": dict(metadata) if isinstance(metadata, dict) else None,
            "model_error": model_error, "reference_input_count": len(reference_paths),
            "reference_unique_count": len(reference_records),
            "eligible_reference_count": eligible_count, "selected_reference_count": len(selected),
            "references": reference_records, "reference_consistency": reference_consistency,
            "generated": generated_records, "generated_aggregate": generated_aggregate}
