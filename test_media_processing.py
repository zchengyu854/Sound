import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

import media_processing as media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.mp3 = cls.root / "sample.mp3"
        cls.mp4 = cls.root / "sample.mp4"
        cls.wav = cls.root / "sample.wav"
        cls.mute = cls.root / "mute.mp4"
        commands = [
            ["-f", "lavfi", "-i", "sine=frequency=220:duration=4", str(cls.mp3)],
            ["-f", "lavfi", "-i", "sine=frequency=220:duration=4", "-c:a", "pcm_s16le", str(cls.wav)],
            ["-f", "lavfi", "-i", "color=size=32x32:duration=4", "-f", "lavfi", "-i", "sine=frequency=220:duration=4", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(cls.mp4)],
            ["-f", "lavfi", "-i", "color=size=32x32:duration=1", "-c:v", "mpeg4", str(cls.mute)],
        ]
        for command in commands:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *command], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.cancel = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def assert_code(self, code, function, *args):
        with self.assertRaises(media.MediaError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def write_wav(self, path, samples):
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(samples.astype("<i2").tobytes())

    def test_real_containers_decode_slice_and_timing(self):
        for source in (self.mp3, self.mp4):
            metadata = media.probe_media(source, source.suffix)
            self.assertAlmostEqual(metadata["duration"], 4, delta=0.1)
            destination = self.folder / (source.stem + source.suffix + ".wav")
            media.decode_media(source, destination, self.cancel)
            clips = media.split_audio(destination, self.folder / source.suffix[1:], self.cancel)
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0]["start"], 0)
            self.assertAlmostEqual(clips[0]["end"], 4, delta=0.1)
            with wave.open(clips[0]["path"]) as wav:
                self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))

    def test_real_wav_probe_decode_and_codec_check(self):
        metadata = media.probe_media(self.wav, "wav")
        self.assertAlmostEqual(metadata["duration"], 4, delta=0.1)
        destination = self.folder / "wav-decoded.wav"
        media.decode_media(self.wav, destination, self.cancel)
        clips = media.split_audio(destination, self.folder / "wav-clips", self.cancel)
        self.assertEqual(len(clips), 1)
        with wave.open(clips[0]["path"]) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
        compressed = self.folder / "compressed.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=220:duration=2", "-c:a", "flac", str(compressed)], check=True)
        self.assert_code("unsupported_codec", media.probe_media, compressed, "wav")

    def test_corrupt_spoof_and_no_audio(self):
        corrupt = self.folder / "broken.mp3"
        corrupt.write_bytes(b"not media")
        self.assert_code("invalid_media", media.probe_media, corrupt, "mp3")
        self.assert_code("container_mismatch", media.probe_media, self.mp3, "mp4")
        self.assert_code("container_mismatch", media.probe_media, self.mp4, "mp3")
        self.assert_code("no_audio_stream", media.probe_media, self.mute, "mp4")
        self.assert_code("container_mismatch", media.probe_media, self.mp3, "wav")

    def test_source_duration_decoded_and_disk_limits(self):
        with patch.object(media, "MAX_SOURCE_BYTES", 10):
            self.assert_code("source_size_limit", media.probe_media, self.mp3, "mp3")
        with patch.object(media, "MAX_DURATION", 1):
            self.assert_code("duration_limit", media.probe_media, self.mp3, "mp3")
        with patch.object(media, "MAX_DECODE_BYTES", 10000):
            self.assert_code("decoded_size_limit", media.decode_media, self.mp3, self.folder / "limited.wav", self.cancel)
        self.assertFalse((self.folder / "limited.wav").exists())
        self.assertEqual(list(self.folder.glob("*.tmp.wav")), [])
        with patch.object(media.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            self.assert_code("disk_space", media.decode_media, self.mp3, self.folder / "disk.wav", self.cancel)

    def test_tool_timeout_and_cancel_reap_process(self):
        original = media.subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        command = [sys.executable, "-c", "import time; time.sleep(10)"]
        with patch.object(media.subprocess, "Popen", side_effect=capture):
            self.assert_code("media_timeout", media.run_tool, command, 0.05)
            timer = threading.Timer(0.05, self.cancel.set)
            timer.start()
            try:
                self.assert_code("cancelled", media.run_tool, command, 5, self.cancel)
            finally:
                timer.cancel()
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assert_code("cancelled", media.decode_media, self.mp3, self.folder / "cancel.wav", self.cancel)

    def test_silence_and_forced_boundaries(self):
        path = self.folder / "long.wav"
        samples = np.full(26 * 24000, 1000, dtype=np.int16)
        self.write_wav(path, samples)
        clips = media.split_audio(path, self.folder / "forced", self.cancel)
        self.assertEqual([clip["end"] - clip["start"] for clip in clips], [12, 12, 2])
        self.assertIn("forced_split_check_words", clips[0]["quality_hints"])
        self.assertIn("short_segment", clips[-1]["quality_hints"])
        self.assertEqual(clips[1]["start"], clips[0]["end"])
        samples[4 * 24000:5 * 24000] = 0
        self.write_wav(path, samples)
        clips = media.split_audio(path, self.folder / "silence", self.cancel)
        self.assertAlmostEqual(clips[0]["end"], 4.02, places=2)
        self.assertNotIn("forced_split_check_words", clips[0]["quality_hints"])
        self.cancel.set()
        self.assert_code("cancelled", media.split_audio, path, self.folder / "cancelled", self.cancel)
        self.assertEqual(list((self.folder / "cancelled").glob("*.wav")), [])


if __name__ == "__main__":
    unittest.main()
