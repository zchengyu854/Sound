import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

import voice_metrics as metrics


class VoiceMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def wav(self, name, samples, *, rate=24000, channels=1, width=2):
        path = self.root / name
        with wave.open(str(path), "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(width)
            target.setframerate(rate)
            target.writeframes(samples if isinstance(samples, bytes)
                               else np.asarray(samples, dtype="<i2").tobytes())
        return path

    def constant(self, name, value, seconds=1):
        return self.wav(name, np.full(int(24000 * seconds), value, dtype=np.int16))

    def test_rms_clipping_and_complete_twenty_ms_frames(self):
        phase = np.arange(480) * 2 * np.pi / 40
        sine = np.rint(16384 * np.sin(phase)).astype(np.int16)
        tail = np.tile(np.array([32735, 32736, -32735, -32736], dtype=np.int16), 60)
        values = np.concatenate([sine, np.zeros(480, dtype=np.int16), tail])
        result = metrics.compute_audio_metrics(self.wav("signal.wav", values))
        expected_power = np.mean((values.astype(np.float64) / 32768) ** 2)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["rms_dbfs"], 10 * math.log10(expected_power), places=10)
        self.assertEqual(result["sample_count"], 1200)
        self.assertEqual(result["window_count"], 2)
        self.assertEqual(result["low_energy_window_count"], 1)
        self.assertEqual(result["low_energy_fraction"], 0.5)
        self.assertEqual(result["clipped_sample_count"], 120)
        self.assertEqual(result["clipping_fraction"], 0.1)

    def test_energy_threshold_and_windows_across_chunk_boundaries(self):
        # Seven channels makes the 24,000-sample read bound fall inside a window.
        frame_values = np.concatenate([np.full(480, 327), np.full(480, 328)] * 6)
        samples = np.repeat(frame_values[:, None], 7, axis=1).astype(np.int16)
        result = metrics.compute_audio_metrics(self.wav("seven.wav", samples, channels=7))
        self.assertEqual(result["window_count"], 12)
        self.assertEqual(result["low_energy_window_count"], 6)
        self.assertEqual(result["low_energy_fraction"], 0.5)
        self.assertEqual(result["sample_count"], 5760 * 7)

    def test_unsigned_8_bit_and_signed_24_and_32_bit_pcm(self):
        # Each two-sample pattern is [-1, 0.5], in the relevant PCM encoding.
        packed_24 = bytes([0, 0, 128, 0, 0, 64]) * 240
        packed_32 = np.tile(np.array([-2147483648, 1073741824], dtype="<i4"), 240).tobytes()
        for name, data, width in [("8.wav", bytes([0, 192]) * 240, 1),
                                  ("24.wav", packed_24, 3), ("32.wav", packed_32, 4)]:
            with self.subTest(width=width):
                result = metrics.compute_audio_metrics(self.wav(name, data, width=width))
                self.assertEqual(result["status"], "ok")
                self.assertAlmostEqual(result["rms_dbfs"], 10 * math.log10((1 + 0.25) / 2))
                self.assertEqual(result["clipping_fraction"], 0.5)
                self.assertEqual(result["low_energy_fraction"], 0)

    def test_silent_empty_invalid_truncated_wrong_rate_and_partial_window(self):
        silence = self.constant("silent.wav", 0)
        empty = self.wav("empty.wav", [])
        corrupt = self.root / "corrupt.wav"
        corrupt.write_bytes(b"not a wav")
        wrong_rate = self.wav("wrong.wav", [123] * 480, rate=16000)
        truncated = self.constant("truncated.wav", 123)
        truncated.write_bytes(truncated.read_bytes()[:-17])
        for path, reason in [(silence, "silent"), (empty, "empty"),
                             (corrupt, "invalid_audio"), (wrong_rate, "invalid_audio"),
                             (truncated, "invalid_audio"), (self.root / "missing.wav", "invalid_audio")]:
            with self.subTest(path=path):
                result = metrics.compute_audio_metrics(path)
                self.assertEqual((result["status"], result["reason"]), ("unavailable", reason))
                self.assertIsNone(result["rms_dbfs"])
                self.assertIsNone(result["low_energy_fraction"])
                self.assertIsNone(result["clipping_fraction"])
        short = metrics.compute_audio_metrics(self.wav("short.wav", [1000] * 479))
        self.assertEqual(short["status"], "ok")
        self.assertIsNone(short["low_energy_fraction"])
        self.assertIsNotNone(short["rms_dbfs"])
        self.assertEqual(metrics.compute_dataset_metrics([silence])["reason"], "silent")
        self.assertEqual(metrics.compute_dataset_metrics([empty, corrupt])["reason"], "no_valid_audio")
        self.assertEqual(metrics.compute_dataset_metrics([])["status"], "unavailable")

    def test_dataset_weights_power_frames_and_clipping_by_duration(self):
        mono = self.constant("mono.wav", -32768)
        stereo = self.wav("stereo.wav", np.full((24000, 2), 16384, dtype=np.int16), channels=2)
        silence = self.constant("silent.wav", 0, seconds=2)
        empty = self.wav("empty.wav", [])
        result = metrics.compute_dataset_metrics([mono, stereo, silence, empty])
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["rms_dbfs"], 10 * math.log10((1 + 0.25) / 4))
        self.assertEqual(result["low_energy_fraction"], 0.5)
        self.assertEqual(result["clipping_fraction"], 0.25)
        self.assertEqual(result["duration_seconds"], 4)
        self.assertEqual((result["file_count"], result["valid_file_count"],
                          result["non_silent_file_count"], result["unavailable_file_count"]), (4, 3, 2, 2))
        self.assertEqual([item["reason"] for item in result["files"]], [None, None, "silent", "empty"])

    def test_single_reference_can_score_generated_audio(self):
        ref = self.constant("ref.wav", 1000)
        generated = self.constant("generated.wav", 2000)
        model = Mock()
        model.encode.side_effect = [np.array([[4, 0]]), np.array([1, 0])]
        result = metrics.compute_speaker_similarity([ref], [generated], model=model)
        self.assertEqual(result["reference_consistency"]["reason"], "insufficient_references")
        self.assertEqual(result["reference_consistency"]["pair_count"], 0)
        self.assertIsNone(result["reference_consistency"]["score"])
        self.assertEqual(result["generated"][0]["score"], 100)
        self.assertEqual(result["generated"][0]["comparison_count"], 1)
        self.assertEqual(result["generated_aggregate"]["sample_count"], 1)
        self.assertEqual(result["generated_aggregate"]["score"], 100)

    def test_similarity_uses_distinct_pairs_clips_and_averages_generated_scores(self):
        refs = [self.constant(f"ref{i}.wav", i * 1000) for i in (1, 2, 3)]
        gen1 = self.constant("gen1.wav", 4000)
        gen2 = self.constant("gen2.wav", 5000)
        silent = self.constant("silent.wav", 0)
        model = Mock()
        model.encode.side_effect = [np.array([[1, 0]]), np.array([2, 0]), np.array([0, 5]),
                                    np.array([1, 1]), np.array([-1, 0])]
        result = metrics.compute_speaker_similarity(list(reversed(refs)), [gen1, gen2, silent], model=model)
        consistency = result["reference_consistency"]
        self.assertEqual((consistency["reference_count"], consistency["pair_count"]), (3, 3))
        self.assertAlmostEqual(consistency["score"], 100 / 3)
        self.assertAlmostEqual(result["generated"][0]["score"], 100 / math.sqrt(2))
        self.assertEqual(result["generated"][1]["score"], 0)
        self.assertEqual(result["generated"][2]["reason"], "silent")
        aggregate = result["generated_aggregate"]
        self.assertAlmostEqual(aggregate["score"], 50 / math.sqrt(2))
        self.assertEqual((aggregate["sample_count"], aggregate["comparison_count"], aggregate["reference_count"]),
                         (2, 6, 3))
        self.assertEqual(model.encode.call_count, 5)

    def test_selection_is_sorted_deduplicated_capped_and_does_not_backfill_failures(self):
        refs = [self.constant(f"ref{i:02d}.wav", 1000 + i) for i in range(10)]
        silence = self.constant("00silent.wav", 0)
        short = self.constant("01short.wav", 1000, 0.5)
        invalid = self.root / "00missing.wav"
        model = Mock()
        model.encode.side_effect = [ValueError("bad embedding")] + [np.array([1, 0])] * 7
        result = metrics.compute_speaker_similarity(list(reversed(refs)) + [refs[0], silence, short, invalid], [], model=model)
        self.assertEqual(result["reference_input_count"], 14)
        self.assertEqual(result["reference_unique_count"], 13)
        self.assertEqual(result["eligible_reference_count"], 10)
        self.assertEqual(result["selected_reference_count"], 8)
        self.assertEqual(result["reference_consistency"]["reference_count"], 7)
        self.assertEqual(result["reference_consistency"]["pair_count"], 21)
        records = {Path(item["path"]).name: item for item in result["references"]}
        self.assertEqual(records["00missing.wav"]["reason"], "invalid_audio")
        self.assertEqual(records["00silent.wav"]["reason"], "silent")
        self.assertEqual(records["01short.wav"]["reason"], "too_short")
        self.assertEqual(records["ref00.wav"]["reason"], "embedding_failed")
        self.assertEqual(records["ref08.wav"]["reason"], "reference_limit")
        self.assertEqual(records["ref09.wav"]["reason"], "reference_limit")
        self.assertEqual(model.encode.call_count, 8)

    def test_embedding_window_is_bounded_and_invalid_vectors_are_unavailable(self):
        long_ref = self.constant("long.wav", 1000, 13)
        generated = self.constant("generated.wav", 2000)
        model = Mock()
        model.encode.side_effect = [np.array([3, 4]), np.array([0, 0])]
        result = metrics.compute_speaker_similarity([long_ref], [generated], model=model)
        self.assertEqual(len(model.encode.call_args_list[0].args[0]), 12 * 24000)
        self.assertEqual(model.encode.call_args_list[0].kwargs["sample_rate"], 24000)
        self.assertEqual(result["references"][0]["duration_seconds"], 13)
        self.assertEqual(result["references"][0]["embedding_duration_seconds"], 12)
        self.assertEqual(result["generated"][0]["reason"], "embedding_failed")
        self.assertIn("zero norm", result["generated"][0]["detail"])
        self.assertEqual(result["generated_aggregate"]["status"], "unavailable")

    def test_no_usable_reference_does_not_load_model(self):
        silent = self.constant("silent.wav", 0)
        generated = self.constant("generated.wav", 1000)
        model = Mock()
        result = metrics.compute_speaker_similarity([silent], [generated], model=model)
        self.assertEqual(result["generated"][0]["reason"], "no_usable_references")
        model.encode.assert_not_called()

    def fake_model_modules(self, download, extractor):
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = download
        module = types.ModuleType("mlx_audio.tts.models.zonos2.speaker_encoder")
        module.Zonos2SpeakerEmbeddingExtractor = extractor
        return {"huggingface_hub": hub, "mlx_audio.tts.models.zonos2.speaker_encoder": module}

    def test_model_download_failure_is_reported_and_not_retried_per_file(self):
        refs = [self.constant("ref1.wav", 1000), self.constant("ref2.wav", 2000)]
        generated = self.constant("generated.wav", 3000)
        download = Mock(side_effect=OSError("network unavailable"))
        model = metrics.SpeakerEmbeddingModel()
        with patch.dict(sys.modules, self.fake_model_modules(download, Mock())):
            result = metrics.compute_speaker_similarity(refs, [generated], model=model)
        download.assert_called_once_with(repo_id=metrics.SPEAKER_MODEL_ID, revision=None,
                                         allow_patterns=["config.json", "model.safetensors"], max_workers=1)
        self.assertIn("OSError: network unavailable", result["model_error"])
        self.assertEqual([record["reason"] for record in result["references"]],
                         ["model_load_failed", "model_load_failed"])
        self.assertEqual(result["reference_consistency"]["reason"], "model_load_failed")
        self.assertEqual(result["generated"][0]["reason"], "model_load_failed")
        self.assertEqual(result["generated_aggregate"]["reason"], "model_load_failed")

    def test_lazy_load_uses_local_extractor_and_records_actual_snapshot_revision(self):
        revision = "a" * 40
        directory = self.root / "snapshots" / revision
        directory.mkdir(parents=True)
        (directory / "config.json").write_text("{}")
        (directory / "model.safetensors").write_bytes(b"mock")
        download = Mock(return_value=str(directory))
        extractor = Mock()
        extractor.return_value.encode.return_value = np.array([[3, 4]])
        with patch.dict(sys.modules, self.fake_model_modules(download, extractor)):
            model = metrics.SpeakerEmbeddingModel(revision="main")
            download.assert_not_called()
            self.assertEqual(model.metadata["revision"], None)
            np.testing.assert_allclose(model.encode(np.ones(480), sample_rate=24000), [0.6, 0.8])
            model.load()
            model.encode(np.ones(480), sample_rate=24000)
        download.assert_called_once_with(repo_id=metrics.SPEAKER_MODEL_ID, revision="main",
                                         allow_patterns=["config.json", "model.safetensors"], max_workers=1)
        extractor.assert_called_once_with(directory, sample_rate=24000)
        self.assertEqual(model.metadata, {"id": metrics.SPEAKER_MODEL_ID,
                                          "requested_revision": "main", "revision": revision})
        self.assertEqual(extractor.return_value.encode.call_count, 2)
        model.close()


if __name__ == "__main__":
    unittest.main()
