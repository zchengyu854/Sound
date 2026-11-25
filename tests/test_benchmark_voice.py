"""Benchmark report helpers and case runner tests; no model or audio hardware."""
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import benchmark_voice as bench


class FakeEngine:
    sample_rate = 24000

    def __init__(self, durations, with_pcm=True):
        self.durations = list(durations)
        self.calls = 0
        self.with_pcm = with_pcm

    def synthesize(self, text, reference, transcript, target, check=lambda: None, on_pcm=None):
        duration = self.durations[self.calls % len(self.durations)]
        self.calls += 1
        if on_pcm and self.with_pcm:
            on_pcm(b"\x00\x00")
        return duration, ""


class BenchmarkTests(unittest.TestCase):
    def test_summarize_ignores_missing_values(self):
        result = bench.summarize([2.0, None, 4.0, 3.0])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["raw"], [2.0, 4.0, 3.0])
        self.assertEqual(result["median"], 3.0)
        self.assertEqual(result["max"], 4.0)
        self.assertEqual(bench.summarize([])["count"], 0)
        self.assertIsNone(bench.summarize([None])["median"])

    def test_rtf_guards_invalid_input(self):
        self.assertEqual(bench.rtf(3000, 3), 1.0)
        self.assertEqual(bench.rtf(1500, 3), 0.5)
        self.assertIsNone(bench.rtf(None, 3))
        self.assertIsNone(bench.rtf(3000, 0))
        self.assertIsNone(bench.rtf(3000, None))

    def test_run_cases_records_cold_and_warm(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = FakeEngine([2.0, 2.0, 4.0])
            cases, cold = bench.run_cases(engine, "ref.wav", "原文",
                                          [("短句", "你好。"), ("中句", "你好。")], 3, directory)
            self.assertEqual(len(cases), 2)
            self.assertEqual(len(cases[0]["runs"]), 3)
            self.assertEqual(cold["case"], "短句")
            self.assertIsNotNone(cold["rtf"])
            self.assertTrue(all(run["rtf"] is not None for run in cases[0]["runs"]))
            self.assertTrue(all(run["audio_seconds"] == 2.0 or run["audio_seconds"] == 4.0
                                for run in cases[0]["runs"]))
            self.assertEqual(cases[0]["warm"]["rtf"]["raw"], [run["rtf"] for run in cases[0]["runs"][1:]])
            self.assertEqual(cases[0]["warm"]["rtf"]["count"], 2)
            self.assertEqual(cases[1]["warm"]["rtf"]["count"], 3)
            self.assertIsNotNone(cases[0]["warm"]["first_audio_ms"]["median"])

    def test_missing_first_chunk_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = FakeEngine([2.0], with_pcm=False)
            cases, cold = bench.run_cases(engine, "ref.wav", "原文", [("短句", "你好。")], 1, directory)
            self.assertIsNone(cases[0]["runs"][0]["first_audio_ms"])
            self.assertEqual(cases[0]["warm"]["first_audio_ms"]["count"], 0)

    def test_markdown_report_has_both_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            cases, _ = bench.run_cases(FakeEngine([2.0]), "ref.wav", "原文",
                                       [("短句", "你好。")], 1, directory)
        table = bench.markdown_report(cases)
        self.assertIn("| 用例 | 轮次 | 音频秒 | 合成毫秒 | 首块毫秒 | RTF |", table)
        self.assertIn("| 用例 | 样本数 | RTF 原始值 | 中位数 | 最大值 | 首块中位数 |", table)
        self.assertIn("| 短句 | 1 | 2.00 |", table)

    def test_single_run_does_not_claim_realtime_without_warm_short_sample(self):
        engine = FakeEngine([2.0])
        engine.load = lambda: None
        engine.close = lambda: None
        with tempfile.TemporaryDirectory() as directory, patch("voice_app.Breeze", return_value=engine):
            output = StringIO()
            with redirect_stderr(output):
                bench.main(["--reference", "ref.wav", "--transcript", "原文", "--runs", "1", "--out", directory])
            self.assertIn("热态样本不足", output.getvalue())

    def test_load_voice_reads_transcript_recording_and_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recordings").mkdir()
            with sqlite3.connect(root / "chat.sqlite3") as db:
                db.execute("CREATE TABLE voices(id TEXT PRIMARY KEY, name TEXT, transcript TEXT, model_id TEXT)")
                db.execute("INSERT INTO voices VALUES('v1','我的声音','这是原文。','m1')")
                db.execute("INSERT INTO voices VALUES('v2','普通声音','没有训练。',NULL)")
            (root / "recordings" / "v1.wav").write_bytes(b"RIFF")
            (root / "recordings" / "v2.wav").write_bytes(b"RIFF")
            reference, transcript, name, model_id = bench.load_voice(directory, "v1")
            self.assertEqual(Path(reference).name, "v1.wav")
            self.assertEqual(transcript, "这是原文。")
            self.assertEqual(name, "我的声音")
            self.assertEqual(model_id, "m1")
            self.assertIsNone(bench.load_voice(directory, "v2")[3])
            with self.assertRaises(SystemExit):
                bench.load_voice(directory, "missing")
            with self.assertRaises(SystemExit):
                bench.load_voice(str(root / "nowhere"), "v1")


if __name__ == "__main__":
    unittest.main()
