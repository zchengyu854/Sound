"""Repeatable local TTS benchmark: cold/warm RTF, first-chunk latency, medians.

Runs the real Breeze engine on this machine (no LLM calls, no network) and writes
a JSON record plus a Markdown table. Values are reported raw so a slow path cannot
be hidden behind an average; cold start and warm runs are separated on purpose.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

SHORT_TEXT = "你好，好久不见。"
MEDIUM_TEXT = "今天适合把拖了很久的小事做掉一件，心里会松快些。再留半小时出去走走，别老盯着屏幕。"


def summarize(values):
    """count/raw/median/max/mean for a numeric series (None-safe)."""
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return {"count": 0, "raw": [], "median": None, "max": None, "mean": None}
    return {"count": len(numbers),
            "raw": [round(v, 3) for v in numbers],
            "median": round(statistics.median(numbers), 3),
            "max": round(max(numbers), 3),
            "mean": round(statistics.fmean(numbers), 3)}


def rtf(wall_ms, audio_seconds):
    """Realtime factor: synthesis wall time divided by produced audio time."""
    if wall_ms is None or not audio_seconds or audio_seconds <= 0:
        return None
    return round(float(wall_ms) / 1000 / float(audio_seconds), 3)


def markdown_report(cases):
    """Render measured cases as a Markdown table plus per-case rollups."""
    lines = ["| 用例 | 轮次 | 音频秒 | 合成毫秒 | 首块毫秒 | RTF |",
             "|---|---|---|---|---|---|"]
    for case in cases:
        for index, run in enumerate(case["runs"], 1):
            lines.append(f"| {case['name']} | {index} | {run['audio_seconds']:.2f} | "
                         f"{run['synthesis_ms']:.0f} | "
                         f"{'-' if run['first_audio_ms'] is None else f'{run['first_audio_ms']:.0f}'} | "
                         f"{'-' if run['rtf'] is None else run['rtf']:.3f} |")
    lines += ["", "| 用例 | 样本数 | RTF 原始值 | 中位数 | 最大值 | 首块中位数 |",
              "|---|---|---|---|---|---|"]
    for case in cases:
        warm = case["warm"]
        lines.append(f"| {case['name']} | {warm['rtf']['count']} | "
                     f"{', '.join(str(v) for v in warm['rtf']['raw']) or '-'} | "
                     f"{'-' if warm['rtf']['median'] is None else f'{warm['rtf']['median']:.3f}'} | "
                     f"{'-' if warm['rtf']['max'] is None else f'{warm['rtf']['max']:.3f}'} | "
                     f"{'-' if warm['first_audio_ms']['median'] is None else f'{warm['first_audio_ms']['median']:.0f}'} |")
    return "\n".join(lines)


def load_voice(directory, voice_id):
    """Read a published voice's reference recording, transcript and adapter id."""
    database = Path(directory) / "chat.sqlite3"
    if not database.is_file():
        raise SystemExit(f"找不到数据目录中的 chat.sqlite3：{database}")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        row = db.execute("SELECT name, transcript, model_id FROM voices WHERE id=?",
                         (voice_id,)).fetchone()
    if row is None:
        raise SystemExit("找不到该声音，请确认 UUID。")
    name, transcript, model_id = row
    recording = Path(directory) / "recordings" / f"{voice_id}.wav"
    if not recording.is_file():
        raise SystemExit(f"找不到参考录音：{recording}")
    return str(recording), transcript, name, model_id


def run_cases(engine, reference, transcript, texts, runs, out_dir, check=lambda: None):
    """Benchmark each text `runs` times; the first run overall is reported as cold."""
    cases = []
    cold = None
    for name, text in texts:
        case = {"name": name, "text": text, "runs": []}
        has_cold = cold is None
        for index in range(runs):
            check()
            first = {"ms": None}
            target = Path(out_dir) / f"{name}-{index + 1}.wav"

            def on_pcm(_data, _first=first):
                if _first["ms"] is None:
                    _first["ms"] = (time.monotonic() - started) * 1000

            started = time.monotonic()
            duration, _warning = engine.synthesize(text, reference, transcript, target,
                                                   check=check, on_pcm=on_pcm)
            wall = (time.monotonic() - started) * 1000
            record = {"audio_seconds": round(float(duration), 3),
                      "synthesis_ms": round(wall, 1),
                      "first_audio_ms": None if first["ms"] is None else round(first["ms"], 1),
                      "rtf": rtf(wall, duration)}
            case["runs"].append(record)
            if cold is None:
                cold = {"case": name, **record}
        warm_runs = case["runs"][1:] if has_cold else case["runs"]
        case["warm"] = {
            "rtf": summarize([r["rtf"] for r in warm_runs]),
            "first_audio_ms": summarize([r["first_audio_ms"] for r in warm_runs]),
            "synthesis_ms": summarize([r["synthesis_ms"] for r in warm_runs]),
        }
        cases.append(case)
    return cases, cold


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地 TTS 性能基准（冷/热分离，输出原始值与中位数）")
    parser.add_argument("--voice", help="已发布声音 UUID；从 VOICE_DATA_DIR 读取参考录音与原文")
    parser.add_argument("--reference", help="参考录音 WAV 路径（与 --transcript 同用）")
    parser.add_argument("--transcript", help="参考录音逐字原文")
    parser.add_argument("--runs", type=int, default=5, help="每个文本的运行次数（默认 5，至少 1；首轮为冷启动）")
    parser.add_argument("--out", default=None, help="输出目录（默认 web-test-artifacts/benchmark-<时间>）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 VOICE_DATA_DIR 或 data）")
    parser.add_argument("--depth-mode", choices=["cached", "compiled", "native"], default="cached",
                        help="深度解码模式：cached（默认）、compiled（整帧编译）或 native（关闭优化，作对照）")
    parser.add_argument("--verify", action="store_true",
                        help="训练前先做数值一致性校验（不采样，比较 logits 相对误差）")
    parser.add_argument("--chunk-frames", type=int, default=None,
                        help="流式合成每块帧数（默认沿用库默认；用于对比首块延迟与吞吐）")
    parser.add_argument("--adapter", choices=["auto", "off"], default="auto",
                        help="auto：声音带训练版本时加载其 adapter（默认）；off：只用基座")
    arguments = parser.parse_args(argv)
    if arguments.runs < 1:
        parser.error("--runs 必须 >= 1")

    import os
    data_dir = arguments.data_dir or os.getenv("VOICE_DATA_DIR", "data")
    model_id = None
    if arguments.voice:
        reference, transcript, label, model_id = load_voice(data_dir, arguments.voice)
    elif arguments.reference and arguments.transcript:
        reference, transcript, label = arguments.reference, arguments.transcript, Path(arguments.reference).stem
    else:
        parser.error("需要 --voice，或同时提供 --reference 与 --transcript")

    out_dir = Path(arguments.out or f"web-test-artifacts/benchmark-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # The engine reads BREEZE_DEPTH_MODE at load time; native means "do not patch".
    os.environ["BREEZE_DEPTH_MODE"] = "cached" if arguments.depth_mode == "native" else arguments.depth_mode
    if arguments.chunk_frames is not None:
        os.environ["BREEZE_STREAMING_FRAMES"] = str(arguments.chunk_frames)
    from voice_app import Breeze
    engine = Breeze()
    engine.load()
    verification = None
    adapter = {"applied": False}
    try:
        if arguments.adapter == "auto" and model_id:
            folder = (Path(data_dir) / "voice_models" / model_id).resolve()
            engine.select_adapter(folder)
            adapter = {"applied": True, "model_id": model_id}
            print(f"已加载训练 adapter：{model_id}", flush=True)
        if arguments.verify:
            from breezetts2_mac_fast import verify_depth
            worst = verify_depth(engine.model, engine.engine)
            verification = {"worst_relative_error": worst}
            print(f"数值校验：最差相对误差 {worst:.2e}", flush=True)
        if arguments.depth_mode == "native":
            # Restore the upstream decoder so this run measures the unoptimized path.
            engine.engine.close()
        cases, cold = run_cases(engine, reference, transcript,
                                [("短句", SHORT_TEXT), ("中句", MEDIUM_TEXT)],
                                arguments.runs, out_dir)
        sample_rate = engine.sample_rate
    finally:
        engine.close()

    document = {"created_at": datetime.now(timezone.utc).isoformat(), "voice": label,
                "reference": str(reference), "transcript": transcript,
                "sample_rate": sample_rate, "runs_per_case": arguments.runs,
                "depth_mode": arguments.depth_mode, "verification": verification,
                "adapter": adapter,
                "chunk_frames": getattr(engine, "streaming_frames", None),
                "cold_start": cold, "cases": cases,
                "note": "RTF 为合成耗时除以音频时长；冷启动含首次预热，不计入热态中位数。"}
    (out_dir / "benchmark.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    table = markdown_report(cases)
    (out_dir / "benchmark.md").write_text(
        f"# 本地 TTS 基准\n\n- 声音：{label}\n- 深度模式：{arguments.depth_mode}\n"
        f"- 训练 adapter：{'已加载' if adapter['applied'] else '未加载（基座）'}\n"
        f"- 每块帧数：{getattr(engine, 'streaming_frames', '')}\n"
        f"- 每档 {arguments.runs} 次，采样率 {sample_rate}\n"
        + (f"- 数值校验最差相对误差：{verification['worst_relative_error']:.2e}\n" if verification else "")
        + f"- 冷启动：{cold['audio_seconds']:.2f} 秒音频 / {cold['synthesis_ms']:.0f} 毫秒 / RTF "
        f"{'-' if cold['rtf'] is None else f'{cold['rtf']:.3f}'}\n\n{table}\n", encoding="utf-8")
    print(table)
    print(f"\n结果已写入 {out_dir}/benchmark.json 与 benchmark.md")
    medians = [case["warm"]["rtf"]["median"] for case in cases]
    if any(value is None for value in medians):
        print("结论：热态样本不足，无法判断持续播放能力。", file=sys.stderr)
    elif (worst := max(medians)) >= 1:
        print(f"结论：热态 RTF 中位数最高 {worst:.3f} ≥ 1，当前硬件无法持续实时输出；"
              "请如实记录，不要靠加大缓冲掩盖。", file=sys.stderr)
    else:
        print(f"结论：热态 RTF 中位数最高 {worst:.3f} < 1，具备持续播放余量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
