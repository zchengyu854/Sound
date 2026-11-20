from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import mlx.core as mx
from mlx_audio.tts.models.breeze_tts import breeze_tts
from mlx_audio.tts import utils as tts_utils

import platform_runtime
import voice_app


class Model:
    sample_rate = 24000
    audio_tokenizer = SimpleNamespace(decode_upsample_rate=1920)


class PlatformSupportTests(unittest.TestCase):
    def load_on(self, system, machine, *, cuda=False, gpu=None, cpu=False, metal=False):
        engine = voice_app.Breeze()
        gpu = cuda if gpu is None else gpu
        with patch("voice_app.platform.system", return_value=system), \
             patch("voice_app.platform.machine", return_value=machine), \
             patch.object(mx.cuda, "is_available", return_value=cuda), \
             patch.object(mx.metal, "is_available", return_value=metal), \
             patch.object(mx, "is_available", side_effect=lambda device: gpu if device == mx.gpu else cpu), \
             patch.object(mx, "set_default_device") as set_device, \
             patch.object(mx, "synchronize"), \
             patch.object(tts_utils, "load_model", return_value=Model()), \
             patch.object(breeze_tts, "Model", Model), \
             patch.object(engine, "attach_depth", return_value=Mock()):
            engine.load()
            return set_device.call_args

    def test_windows_prefers_cuda_and_falls_back_to_cpu(self):
        self.assertEqual(self.load_on("Windows", "AMD64", cuda=True).args, (mx.gpu,))
        self.assertEqual(self.load_on("Windows", "AMD64", cpu=True).args, (mx.cpu,))
        self.assertEqual(self.load_on("Windows", "AMD64", cuda=True, gpu=False, cpu=True).args, (mx.cpu,))
        self.assertEqual(self.load_on("Windows", "ARM64", cpu=True).args, (mx.cpu,))
        with self.assertRaisesRegex(RuntimeError, "CUDA"):
            self.load_on("Windows", "AMD64")

    def test_mac_uses_metal_on_apple_silicon_and_cpu_on_intel(self):
        self.assertIsNone(self.load_on("Darwin", "arm64", metal=True))
        with self.assertRaisesRegex(RuntimeError, "Metal"):
            self.load_on("Darwin", "arm64")
        self.assertEqual(self.load_on("Darwin", "x86_64", cpu=True).args, (mx.cpu,))
        with self.assertRaisesRegex(RuntimeError, "CPU"):
            self.load_on("Darwin", "x86_64")

    def test_windows_registers_installed_cuda_dll_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_bin = Path(temporary) / "nvidia" / "cublas" / "bin"
            toolkit_bin = Path(temporary) / "toolkit" / "bin"
            package_bin.mkdir(parents=True)
            toolkit_bin.mkdir(parents=True)
            add = Mock(side_effect=lambda directory: directory)
            windows_os = SimpleNamespace(name="nt", getenv=lambda key: str(toolkit_bin.parent),
                                         add_dll_directory=add)
            with patch.object(platform_runtime, "os", windows_os), \
                 patch.object(platform_runtime.sysconfig, "get_path", return_value=temporary), \
                 patch.object(platform_runtime, "_cuda_dll_directories", []):
                platform_runtime.prepare_windows_cuda()
                platform_runtime.prepare_windows_cuda()
            self.assertEqual({call.args[0] for call in add.call_args_list},
                             {str(package_bin), str(toolkit_bin)})
            self.assertEqual(add.call_count, 2)

    def test_windows_locks_first_byte_without_truncating(self):
        lease = Mock()
        lease.fileno.return_value = 42
        windows_lock = SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with patch.object(platform_runtime, "os", SimpleNamespace(name="nt")), \
             patch.object(platform_runtime, "msvcrt", windows_lock, create=True):
            platform_runtime.lock_data_directory(lease)
        lease.seek.assert_called_once_with(0)
        windows_lock.locking.assert_called_once_with(42, 2, 1)

    def test_directory_lock_blocks_other_process_until_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.lock"
            path.write_text("keep", encoding="ascii")
            script = (
                "import sys\n"
                "from platform_runtime import lock_data_directory\n"
                "with open(sys.argv[1], 'a') as lease:\n"
                "    try:\n"
                "        lock_data_directory(lease)\n"
                "    except OSError:\n"
                "        sys.exit(17)\n"
            )
            def child():
                return subprocess.run([sys.executable, "-c", script, str(path)],
                                      capture_output=True, timeout=15).returncode
            with path.open("a") as lease:
                platform_runtime.lock_data_directory(lease)
                self.assertEqual(child(), 17)
            self.assertEqual(child(), 0)
            self.assertEqual(path.read_text(encoding="ascii"), "keep")


if __name__ == "__main__":
    unittest.main()
