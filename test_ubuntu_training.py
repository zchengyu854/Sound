"""Tests for resumable Ubuntu training and seed-dependent sample order."""
import json
from pathlib import Path
import tempfile
import unittest

from training_exchange import deterministic_order


class UbuntuTrainingTests(unittest.TestCase):
    def test_ubuntu_checkpoint_roundtrip(self):
        import mlx.core as mx
        import mlx.optimizers as optim
        from mlx.utils import tree_unflatten
        from breeze_training import attach_lora, train_step, load_adapter, parameter_digest
        from test_breeze_training import TinyBreeze
        from ubuntu_training import save_checkpoint
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        model = TinyBreeze()
        attach_lora(model)
        optimizer = optim.Adam(1e-4)
        train_step(model, optimizer, mx.ones((1, 2, 64)), mx.array([[[1, 2, 3]]]))
        save_checkpoint(work, model, optimizer, 1, {"step": 1})
        folder = work / "checkpoint-0001"
        self.assertEqual(json.loads((folder / "metadata.json").read_text()), {"step": 1})
        restored = TinyBreeze()
        attach_lora(restored)
        load_adapter(restored, folder / "adapter.safetensors")
        restored_optimizer = optim.Adam(1e-4)
        restored_optimizer.state = tree_unflatten(list(mx.load(str(folder / "optimizer.safetensors")).items()))
        self.assertEqual(parameter_digest(optimizer.state), parameter_digest(restored_optimizer.state))
        self.assertEqual(parameter_digest(model.trainable_parameters()), parameter_digest(restored.trainable_parameters()))

    def test_deterministic_order_covers_material_and_ignores_model_randomness(self):
        first = deterministic_order(5, 7, 1)
        self.assertEqual(sorted(first), [0, 1, 2, 3, 4])
        self.assertEqual(deterministic_order(5, 7, 1), first)
        self.assertNotEqual(deterministic_order(5, 7, 2), first)
        self.assertNotEqual(deterministic_order(5, 8, 1), first)

    def test_progress_file_is_atomic_json(self):
        from ubuntu_training import write_progress
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            write_progress(path, {"status": "running", "step": 3, "loss": 1.25})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"status": "running", "step": 3, "loss": 1.25})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_failure_progress_keeps_existing_step_log(self):
        from ubuntu_training import fail_progress, write_progress
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            write_progress(path, {"status": "running", "step": 3, "step_log": [{"step": 1, "loss": 2.0}]})
            fail_progress(path, "task.zip", "result.zip", "CUDA out of memory")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"], "CUDA out of memory")
            self.assertEqual(data["step_log"], [{"step": 1, "loss": 2.0}])
