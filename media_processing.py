"""Local MP3/MP4 validation, bounded decoding, and silence-aware PCM slicing."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import threading
import time
from uuid import uuid4
import wave

import numpy as np

MAX_SOURCE_BYTES = 200 * 1024**2
MAX_DURATION = 3600
MAX_DECODE_BYTES = 180 * 1024**2
PROBE_TIMEOUT = 15
DECODE_TIMEOUT = 300
RATE = 24000
PCM_CODECS = {"pcm_u8", "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be",
              "pcm_s32le", "pcm_s32be"}
DEMUXERS = {"mp3": "mp3", "mp4": "mov", "wav": "wav"}


class MediaError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise MediaError("cancelled")


def run_tool(arguments, timeout, cancel=None):
    """Bound diagnostic output in memory; never expose stderr's private paths."""
    check_cancel(cancel)
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise MediaError("media_tool_unavailable") from None
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel(cancel)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError("media_timeout")
            try:
                output, _ = process.communicate(timeout=min(0.1, remaining))
                if process.returncode:
                    raise MediaError("invalid_media")
                if len(output) > 65536:
                    raise MediaError("invalid_media")
                return output
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        if process.stdout:
            process.stdout.close()


def probe_media(path, extension):
    path = Path(path)
    extension = extension.lower().lstrip(".")
    if extension not in DEMUXERS:
        raise MediaError("unsupported_format")
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise MediaError("source_size_limit")
        output = run_tool(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                           "-show_entries", "format=format_name,duration:stream=index,codec_type,codec_name,duration:stream_disposition=default",
                           "-of", "json", str(path.resolve())], PROBE_TIMEOUT)
        document = json.loads(output)
        container = document["format"]["format_name"]
        formats = set(container.split(","))
        if extension == "mp3" and formats != {"mp3"}:
            raise MediaError("container_mismatch")
        if extension == "mp4" and "mp4" not in formats:
            raise MediaError("container_mismatch")
        if extension == "wav" and "wav" not in formats:
            raise MediaError("container_mismatch")
        streams = [s for s in document.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise MediaError("no_audio_stream")
        selected = next((s for s in streams if s.get("disposition", {}).get("default")), streams[0])
        if extension == "wav" and selected.get("codec_name") not in PCM_CODECS:
            raise MediaError("unsupported_codec")
        duration = float(document["format"].get("duration", selected.get("duration", 0)))
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            raise MediaError("duration_limit")
        return {"duration": duration, "format": container, "audio_stream": int(selected["index"]),
                "audio_selection": "default_or_first"}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise MediaError("invalid_media") from None


def decode_media(source, destination, cancel):
    source, destination = Path(source), Path(destination)
    check_cancel(cancel)
    metadata = probe_media(source, source.suffix)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(destination.parent).free < MAX_DECODE_BYTES + 64 * 1024**2:
        raise MediaError("disk_space")
    temporary = destination.with_name(destination.stem + f"-{uuid4()}.tmp.wav")
    try:
        # Explicit demuxers plus a protocol allowlist prevent playlist/network interpretation.
        run_tool(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
                  "-protocol_whitelist", "file,pipe", "-f", DEMUXERS[source.suffix.lower().lstrip(".")],
                  "-i", str(source.resolve()), "-map", f"0:{metadata['audio_stream']}",
                  "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
                  "-fs", str(min(MAX_DECODE_BYTES, int(MAX_DURATION * RATE * 2) + 4096)),
                  "-f", "wav", str(temporary)], DECODE_TIMEOUT, cancel)
        check_cancel(cancel)
        with wave.open(str(temporary), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, RATE):
                raise MediaError("invalid_decoded_audio")
            if not 0 < duration <= MAX_DURATION or temporary.stat().st_size >= MAX_DECODE_BYTES:
                raise MediaError("decoded_size_limit")
            if abs(duration - metadata["duration"]) > 2:
                raise MediaError("duration_mismatch")
        temporary.replace(destination)
        return {**metadata, "decoded_duration": duration}
    except (wave.Error, EOFError, OSError):
        raise MediaError("invalid_decoded_audio") from None
    finally:
        temporary.unlink(missing_ok=True)


WINDOW = 480                 # 20 ms at 24 kHz
QUIET_RMS = 180              # about -45 dBFS: quiet, not necessarily silent
TRIM_MIN = int(0.3 * RATE)   # ignore shorter low-energy edges
TRIM_MARGIN = int(0.2 * RATE)  # keep 200 ms around the voiced span
TRIM_MAX = int(1.0 * RATE)   # never remove more than one second per edge
CLIP_LEVEL = 32760


def window_rms(values):
    """Complete 20 ms windows only; returns one RMS value per window."""
    count = values.size // WINDOW
    if not count:
        return np.zeros(0, dtype=np.float32)
    frames = values[:count * WINDOW].astype(np.float32).reshape(count, WINDOW)
    return np.sqrt(np.mean(frames * frames, axis=1))


def trim_bounds(rms, total):
    """Return (start, end) sample offsets to keep, with bounded edge trimming.

    Silence inside the clip is never removed. Edges are trimmed only when more
    than TRIM_MIN of low energy is present, at most TRIM_MAX per side, keeping
    TRIM_MARGIN so soft onsets and trailing breaths survive. `total` keeps any
    trailing partial window.
    """
    voiced = np.flatnonzero(rms >= QUIET_RMS)
    if voiced.size == 0:
        return 0, total
    voiced_start = int(voiced[0]) * WINDOW
    voiced_end = (int(voiced[-1]) + 1) * WINDOW
    start = 0
    if voiced_start >= TRIM_MIN:
        start = max(voiced_start - TRIM_MARGIN, voiced_start - TRIM_MAX)
        start -= start % WINDOW
    end = total
    if total - voiced_end >= TRIM_MIN:
        end = min(total, voiced_end + TRIM_MARGIN, voiced_end + TRIM_MAX)
        end -= end % WINDOW
    return start, end


def clip_stats(values, peak, start, end):
    """Reviewable quality facts; low volume is not treated as no speech."""
    rms = window_rms(values)
    voiced = np.flatnonzero(rms >= QUIET_RMS)
    windows = int(rms.size)
    if voiced.size:
        voiced_start = int(voiced[0]) * WINDOW
        voiced_end = (int(voiced[-1]) + 1) * WINDOW
    else:
        # No voiced window: report all low energy, never a negative duration.
        voiced_start, voiced_end = 0, 0
    return {
        "window_count": windows,
        "voiced_window_count": int(voiced.size),
        "voiced_fraction": round(float(voiced.size) / windows, 4) if windows else 0.0,
        "leading_low_energy_ms": (round(voiced_start / RATE * 1000, 1) if voiced.size
                                  else round(values.size / RATE * 1000, 1)),
        "trailing_low_energy_ms": (round((values.size - voiced_end) / RATE * 1000, 1) if voiced.size
                                   else 0.0),
        "trimmed_start_ms": round(start / RATE * 1000, 1),
        "trimmed_end_ms": round((values.size - end) / RATE * 1000, 1),
        "effective_seconds": round((voiced_end - voiced_start) / RATE, 3),
        "peak_dbfs": round(20 * math.log10(peak / 32768), 2) if peak > 0 else None,
        "clipped_samples": int(np.count_nonzero(np.abs(values.astype(np.int32)) >= CLIP_LEVEL)),
    }


def split_audio(wav_path, output_dir, cancel):
    """Use quiet 20ms boundaries after 3s; force a cut at 12s if needed.

    Each clip keeps its source time coordinates, trims only the low-energy
    edges within bounds, and carries reviewable quality facts and hints.
    Nothing is approved automatically.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    created = []
    try:
        with wave.open(str(wav_path), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (RATE, 1, 2):
                raise MediaError("invalid_decoded_audio")
            total = source.getnframes()
            if total <= 0 or total > MAX_DURATION * RATE:
                raise MediaError("duration_limit")
            start = 0
            while start < total:
                check_cancel(cancel)
                maximum = min(start + 12 * RATE, total)
                end, quiet = maximum, False
                if maximum < total:
                    source.setpos(start + 3 * RATE)
                    position = start + 3 * RATE
                    while position + 480 <= maximum:
                        check_cancel(cancel)
                        frame = np.frombuffer(source.readframes(480), dtype="<i2").astype(np.float32)
                        if frame.size and float(np.sqrt(np.mean(frame * frame))) < QUIET_RMS:
                            end, quiet = position + 480, True
                            break
                        position += 480
                source.setpos(start)
                raw = source.readframes(end - start)
                if len(raw) != (end - start) * 2:
                    raise MediaError("truncated_audio")
                values = np.frombuffer(raw, dtype="<i2")
                keep_start, keep_end = trim_bounds(window_rms(values), values.size)
                # int32 avoids int16 overflow at -32768 when measuring the peak.
                peak = int(np.max(np.abs(values.astype(np.int32)))) if values.size else 0
                stats = clip_stats(values, peak, keep_start, keep_end)
                path = output_dir / f"{uuid4()}.wav"
                created.append(path)
                with wave.open(str(path), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(RATE)
                    target.writeframes(values[keep_start:keep_end].tobytes())
                hints = []
                if not quiet and maximum < total:
                    hints.append("forced_split_check_words")
                if (keep_end - keep_start) < 3 * RATE:
                    hints.append("short_segment")
                if peak < QUIET_RMS:
                    hints.append("low_volume_or_silence")
                elif stats["voiced_fraction"] < 0.3:
                    hints.append("low_voiced_ratio")
                if peak >= CLIP_LEVEL:
                    hints.append("possible_clipping")
                if keep_start or keep_end < values.size:
                    hints.append("trimmed_edges")
                result.append({"start": (start + keep_start) / RATE,
                               "end": (start + keep_end) / RATE,
                               "path": str(path), "quality_hints": hints,
                               "quality_stats": stats})
                start = end
        return result
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
