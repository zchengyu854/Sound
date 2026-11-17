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
    if extension not in ("mp3", "mp4"):
        raise MediaError("unsupported_format")
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise MediaError("source_size_limit")
        output = run_tool(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                           "-show_entries", "format=format_name,duration:stream=index,codec_type,duration:stream_disposition=default",
                           "-of", "json", str(path.resolve())], PROBE_TIMEOUT)
        document = json.loads(output)
        container = document["format"]["format_name"]
        formats = set(container.split(","))
        if (extension == "mp3" and formats != {"mp3"}) or (extension == "mp4" and "mp4" not in formats):
            raise MediaError("container_mismatch")
        streams = [s for s in document.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise MediaError("no_audio_stream")
        selected = next((s for s in streams if s.get("disposition", {}).get("default")), streams[0])
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
                  "-protocol_whitelist", "file,pipe", "-f", "mp3" if source.suffix.lower() == ".mp3" else "mov",
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


def split_audio(wav_path, output_dir, cancel):
    """Use quiet 20ms boundaries after 3s; force a cut at 12s if needed.

    Short trailing clips remain reviewable and carry a quality hint, not silent approval.
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
                        if frame.size and float(np.sqrt(np.mean(frame * frame))) < 180:
                            end, quiet = position + 480, True
                            break
                        position += 480
                source.setpos(start)
                path = output_dir / f"{uuid4()}.wav"
                created.append(path)
                peak = 0
                with wave.open(str(path), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(RATE)
                    remaining = end - start
                    while remaining:
                        check_cancel(cancel)
                        data = source.readframes(min(remaining, RATE))
                        if not data:
                            raise MediaError("truncated_audio")
                        values = np.frombuffer(data, dtype="<i2").astype(np.int32)
                        peak = max(peak, int(np.max(np.abs(values))))
                        target.writeframes(data)
                        remaining -= len(data) // 2
                hints = []
                if not quiet and maximum < total:
                    hints.append("forced_split_check_words")
                if end - start < 3 * RATE:
                    hints.append("short_segment")
                if peak < 180:
                    hints.append("low_volume_or_silence")
                if peak >= 32760:
                    hints.append("possible_clipping")
                result.append({"start": start / RATE, "end": end / RATE,
                               "path": str(path), "quality_hints": hints})
                start = end
        return result
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
