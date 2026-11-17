import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from media_processing import MediaError
import separate_vocals as separation


def _run_real_tool(arguments, timeout, cancel=None):
    return subprocess.run(arguments, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class SeparateVocalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "sample.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=220:duration=2", str(cls.source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.venv = self.folder / ".venv-separator"
        (self.venv / "bin").mkdir(parents=True)
        self.tool = self.venv / "bin" / "demucs-mlx"
        self.tool.write_text("")
        self.tool.chmod(0o700)
        self.out = self.folder / "out"
        self.cancel = threading.Event()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, create_stem=True):
        def side_effect(arguments, timeout, cancel=None):
            self.calls.append(list(arguments))
            if arguments[0] == str(self.tool):
                if create_stem:
                    stem_dir = Path(arguments[arguments.index("-o") + 1])
                    destination = stem_dir / "htdemucs_ft" / "sample" / "vocals.wav"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                                    "-i", "sine=frequency=330:duration=2", str(destination)], check=True)
                return b""
            return _run_real_tool(arguments, timeout, cancel)
        return side_effect

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(MediaError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_missing_separator_tool(self):
        self.assert_code("separator_unavailable", separation.separate, self.source, self.out,
                         self.folder / "absent")

    def test_unsupported_source_format(self):
        wav = self.folder / "clip.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=220:duration=1", str(wav)], check=True)
        with patch.object(separation, "run_tool", side_effect=self.fake_run()):
            self.assert_code("unsupported_format", separation.separate, wav, self.out, self.venv)

    def test_decode_separate_and_encode_arguments(self):
        with patch.object(separation, "run_tool", side_effect=self.fake_run()):
            report = separation.separate(self.source, self.out, self.venv, cancel=self.cancel)
        decode, separate, wav_encode, mp3_encode = self.calls
        self.assertEqual(Path(decode[decode.index("-f") + 1]), Path("mp3"))
        for flag in ("-vn", "-sn", "-dn", "-ac", "2", "-ar", "44100"):
            self.assertIn(flag, decode)
        self.assertIn("--stem", separate)
        self.assertEqual(separate[separate.index("--stem") + 1], "vocals")
        self.assertEqual(separate[separate.index("-n") + 1], "htdemucs_ft")
        for call in (wav_encode, mp3_encode):
            self.assertIn("-ar", call)
            self.assertEqual(call[call.index("-ar") + 1], "24000")
            self.assertEqual(call[call.index("-ac") + 1], "1")
        self.assertEqual(wav_encode[wav_encode.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(mp3_encode[mp3_encode.index("-c:a") + 1], "libmp3lame")
        self.assertEqual(report["source"], "sample.mp3")
        self.assertEqual(report["model"], "htdemucs_ft")
        self.assertIn("review_required", report["quality_hints"])

    def test_outputs_review_wav_and_uploadable_mp3(self):
        with patch.object(separation, "run_tool", side_effect=self.fake_run()):
            report = separation.separate(self.source, self.out, self.venv, cancel=self.cancel)
        wav_path, mp3_path = Path(report["wav"]), Path(report["mp3"])
        with wave.open(str(wav_path)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 2, delta=0.2)
        self.assertGreater(mp3_path.stat().st_size, 0)
        self.assertEqual(mp3_path.suffix, ".mp3")
        report_file = self.out / "sample.vocals.json"
        self.assertEqual(json.loads(report_file.read_text()), report)
        self.assertEqual(list(self.out.glob(".tmp-*")), [])

    def test_non_ft_model_omits_stem_flag(self):
        def side_effect(arguments, timeout, cancel=None):
            self.calls.append(list(arguments))
            if arguments[0] == str(self.tool):
                stem_dir = Path(arguments[arguments.index("-o") + 1])
                destination = stem_dir / "htdemucs" / "sample" / "vocals.wav"
                destination.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                                "-i", "sine=frequency=330:duration=2", str(destination)], check=True)
                return b""
            return _run_real_tool(arguments, timeout, cancel)

        with patch.object(separation, "run_tool", side_effect=side_effect):
            separation.separate(self.source, self.out, self.venv, model="htdemucs", cancel=self.cancel)
        self.assertNotIn("--stem", self.calls[1])

    def test_missing_stem_and_tool_failure_cleanup(self):
        with patch.object(separation, "run_tool", side_effect=self.fake_run(create_stem=False)):
            self.assert_code("separation_failed", separation.separate, self.source, self.out, self.venv)
        def failing(arguments, timeout, cancel=None):
            self.calls.append(list(arguments))
            if arguments[0] == str(self.tool):
                raise MediaError("media_timeout")
            return _run_real_tool(arguments, timeout, cancel)

        with patch.object(separation, "run_tool", side_effect=failing):
            self.assert_code("media_timeout", separation.separate, self.source, self.out, self.venv)
        self.assertEqual(list(self.out.glob(".tmp-*")), [])

    def test_invalid_output_and_cancel(self):
        with patch.object(separation, "run_tool", side_effect=self.fake_run()), \
             patch.object(separation, "_check_output", side_effect=MediaError("invalid_output_audio")):
            self.assert_code("invalid_output_audio", separation.separate, self.source, self.out,
                             self.venv, cancel=self.cancel)
        self.cancel.set()
        with patch.object(separation, "run_tool", side_effect=self.fake_run()):
            self.assert_code("cancelled", separation.separate, self.source, self.out,
                             self.venv, cancel=self.cancel)

    def test_main_success_and_error(self):
        argv = ["separate_vocals.py", str(self.source), "-o", str(self.out),
                "--separator-venv", str(self.venv)]
        with patch.object(sys, "argv", argv), \
             patch.object(separation, "run_tool", side_effect=self.fake_run()), \
             patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(separation.main(), 0)
        with patch.object(sys, "argv", argv), \
             patch.object(separation, "separate", side_effect=MediaError("media_timeout")), \
             patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(separation.main(), 1)


if __name__ == "__main__":
    unittest.main()
