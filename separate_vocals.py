"""Local vocal separation for MP3/MP4 sources with background music.

Runs in the project's existing .venv and shells out to demucs-mlx installed in
a separate virtualenv. Outputs a 24 kHz mono PCM16 WAV for review and a mono
MP3 that the training upload endpoint accepts. No training backend changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4
import wave

from media_processing import DECODE_TIMEOUT, MAX_DURATION, MAX_SOURCE_BYTES
from media_processing import MediaError, check_cancel, probe_media, run_tool

SEPARATION_TIMEOUT = 900
DEFAULT_MODEL = "htdemucs_ft"
DEFAULT_VENV = ".venv-separator"
MP3_BITRATE = "192k"


def _tool_path(separator_venv):
    root = Path(separator_venv)
    if root.is_absolute():
        return root / "bin" / "demucs-mlx"
    return Path.cwd() / root / "bin" / "demucs-mlx"


def _locate_vocals(stem_dir):
    candidates = sorted(stem_dir.rglob("vocals.*"))
    if len(candidates) != 1:
        raise MediaError("separation_failed")
    return candidates[0]


def _check_output(wav_path, mp3_path, duration):
    try:
        with wave.open(str(wav_path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
                raise MediaError("invalid_output_audio")
            frames = audio.getnframes() / audio.getframerate()
            if not 0 < frames <= MAX_DURATION or abs(frames - duration) > 2:
                raise MediaError("invalid_output_audio")
    except (wave.Error, EOFError, OSError):
        raise MediaError("invalid_output_audio") from None
    try:
        if not 0 < mp3_path.stat().st_size <= MAX_SOURCE_BYTES:
            raise MediaError("separated_size_limit")
    except OSError:
        raise MediaError("invalid_output_audio") from None


def separate(source, output_dir, separator_venv, model=DEFAULT_MODEL, cancel=None):
    """Return a report dict after writing review WAV and uploadable MP3."""
    source = Path(source)
    output_dir = Path(output_dir)
    tool = _tool_path(separator_venv)
    if not tool.is_file():
        raise MediaError("separator_unavailable")
    if not shutil.which("ffmpeg"):
        raise MediaError("media_tool_unavailable")
    metadata = probe_media(source, source.suffix)
    check_cancel(cancel)

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = output_dir / f".tmp-{uuid4()}"
    work.mkdir(mode=0o700)
    try:
        decoded = work / "source.wav"
        run_tool(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
                  "-protocol_whitelist", "file,pipe",
                  "-f", "mp3" if source.suffix.lower() == ".mp3" else "mov",
                  "-i", str(source.resolve()), "-map", f"0:{metadata['audio_stream']}",
                  "-vn", "-sn", "-dn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                  "-f", "wav", str(decoded)], DECODE_TIMEOUT, cancel)

        stem_dir = work / "stems"
        stem_dir.mkdir(mode=0o700)
        arguments = [str(tool), "-n", model, "-o", str(stem_dir)]
        if model == "htdemucs_ft":
            arguments += ["--stem", "vocals"]
        arguments.append(str(decoded))
        run_tool(arguments, SEPARATION_TIMEOUT, cancel)
        check_cancel(cancel)

        vocals = _locate_vocals(stem_dir)
        wav_path = output_dir / f"{source.stem}.vocals.wav"
        mp3_path = output_dir / f"{source.stem}.vocals.mp3"
        run_tool(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
                  "-protocol_whitelist", "file,pipe", "-i", str(vocals),
                  "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
                  "-f", "wav", str(wav_path)], DECODE_TIMEOUT, cancel)
        run_tool(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
                  "-protocol_whitelist", "file,pipe", "-i", str(vocals),
                  "-vn", "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame",
                  "-b:a", MP3_BITRATE, "-f", "mp3", str(mp3_path)], DECODE_TIMEOUT, cancel)
        check_cancel(cancel)
        _check_output(wav_path, mp3_path, metadata["duration"])

        report = {"source": source.name, "duration": metadata["duration"], "model": model,
                  "wav": str(wav_path), "mp3": str(mp3_path),
                  "wav_bytes": wav_path.stat().st_size, "mp3_bytes": mp3_path.stat().st_size,
                  "audio_selection": metadata["audio_selection"],
                  "quality_hints": ["review_required", "single_speaker_unverified"]}
        (output_dir / f"{source.stem}.vocals.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="从带背景音乐的 MP3/MP4 中抽取人声")
    parser.add_argument("source", help="输入 MP3 或 MP4 文件")
    parser.add_argument("-o", "--out", default="separated", help="输出目录（默认 separated）")
    parser.add_argument("--separator-venv", default=DEFAULT_VENV,
                        help=f"含 demucs-mlx 的独立虚拟环境（默认 {DEFAULT_VENV}）")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"分离模型（默认 {DEFAULT_MODEL}；--stem 仅对该模型有效）")
    arguments = parser.parse_args()
    try:
        report = separate(arguments.source, arguments.out, arguments.separator_venv, arguments.model)
    except MediaError as error:
        print(f"separation error: {error.code}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
