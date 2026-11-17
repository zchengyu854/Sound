import math
from pathlib import Path
import tempfile
import unittest

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten
from mlx_audio.tts.models.breeze_tts.breeze_tts import Model, _Backbone, _DepthDecoder
from mlx_audio.tts.models.breeze_tts.config import ModelConfig

from breeze_training import (LoRALinear, attach_lora, frozen_digest, load_adapter,
                             parameter_digest, save_adapter, teacher_forcing_loss,
                             train_step, validate_example)


class TinyBreeze(nn.Module):
    _mask_reserved_codec_logits = Model._mask_reserved_codec_logits

    def __init__(self):
        super().__init__()
        self.config = ModelConfig(
            hidden_size=64, intermediate_size=128, num_hidden_layers=1,
            num_attention_heads=4, num_key_value_heads=2, head_dim=16,
            num_codebooks=3, vocab_size=11, audio_embed_size=64,
            codec_config={"codebook_size": 8},
            depth_decoder_config={"hidden_size": 64, "intermediate_size": 128,
                                  "num_hidden_layers": 1, "num_attention_heads": 4,
                                  "num_key_value_heads": 2, "head_dim": 16})
        self.num_codebooks = 3
        self.vocab_size = 11
        self.backbone_model = _Backbone(self.config)
        self.depth_decoder = _DepthDecoder(self.config)
        self.depth_decoder.codebooks_head.weight = mx.random.normal((2, 64, 11)) * 0.05
        self.lm_head = nn.Linear(64, 12, bias=False)


class TrainingTests(unittest.TestCase):
    def setUp(self):
        mx.random.seed(5)
        self.model = TinyBreeze()
        self.prompt = mx.random.normal((1, 3, 64))
        self.codes = mx.array([[[1, 2, 3], [4, 5, 6]]], mx.int32)

    def test_teacher_forcing_matches_independent_prefix_predictions(self):
        m = self.model
        first, depth = [], []
        for frame in range(3):
            inputs = self.prompt
            if frame:
                inputs = mx.concatenate((inputs, m.backbone_model.embed_tokens(self.codes[:, :frame])), axis=1)
            state = m.backbone_model(input_embeddings=inputs)[:, -1]
            target = self.codes[:, frame, 0] if frame < 2 else mx.array([m.vocab_size])
            first.append(nn.losses.cross_entropy(m._mask_reserved_codec_logits(m.lm_head(state)), target))
            if frame < 2:
                for book in range(1, 3):
                    ids = mx.concatenate((mx.zeros((1, 1), mx.int32), self.codes[:, frame, :book]), axis=1)
                    scores = m.depth_decoder.next_logits(ids, state)
                    depth.append(nn.losses.cross_entropy(m._mask_reserved_codec_logits(scores), self.codes[:, frame, book]))
        expected = mx.stack(first).mean() + mx.stack(depth).mean()
        actual = teacher_forcing_loss(m, self.prompt, self.codes)
        self.assertAlmostEqual(actual.item(), expected.item(), places=5)

    def test_only_adapters_update_with_dense_and_quantized_base(self):
        for quantized in (False, True):
            with self.subTest(quantized=quantized):
                model = TinyBreeze()
                if quantized:
                    nn.quantize(model, group_size=64, bits=4,
                                class_predicate=lambda name, mod: isinstance(mod, nn.Linear))
                attach_lora(model)
                self.assertTrue(all(n.endswith((".lora_a", ".lora_b")) for n, _ in tree_flatten(model.trainable_parameters())))
                before = frozen_digest(model)
                adapter = parameter_digest(model.trainable_parameters())
                result = train_step(model, optim.Adam(1e-3), self.prompt, self.codes)
                self.assertTrue(math.isfinite(result["loss"]))
                self.assertGreater(result["gradient_norm"], 0)
                self.assertEqual(before, frozen_digest(model))
                self.assertNotEqual(adapter, parameter_digest(model.trainable_parameters()))
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "adapter.safetensors"
                    save_adapter(model, path)
                    saved = parameter_digest(model.trainable_parameters())
                    fresh = TinyBreeze()
                    attach_lora(fresh)
                    load_adapter(fresh, path)
                    self.assertEqual(saved, parameter_digest(fresh.trainable_parameters()))
                    fresh = TinyBreeze()
                    attach_lora(fresh, rank=2)
                    with self.assertRaises(ValueError):
                        load_adapter(fresh, path)

    def test_zero_adapter_preserves_original_linear(self):
        for linear in (nn.Linear(64, 32), nn.QuantizedLinear(64, 32, group_size=64)):
            x = mx.random.normal((2, 64))
            adapted = LoRALinear(linear)
            self.assertTrue(bool(mx.all(linear(x) == adapted(x)).item()))
        with self.assertRaises(ValueError):
            LoRALinear(nn.Linear(64, 32), rank=3)

    def test_bfloat16_digest_is_stable_and_sensitive_to_changes(self):
        first = {"weight": mx.array([1, 2], mx.bfloat16)}
        second = {"weight": mx.array([1, 3], mx.bfloat16)}
        self.assertEqual(parameter_digest(first), parameter_digest(first))
        self.assertNotEqual(parameter_digest(first), parameter_digest(second))

    def test_padding_and_invalid_examples_rejected(self):
        validate_example(self.model, self.prompt, self.codes.astype(mx.uint32))
        validate_example(self.model, self.prompt, self.codes)
        for codes in (mx.zeros((1, 0, 3), mx.int32), mx.ones((2, 1, 3), mx.int32),
                      mx.ones((1, 1, 3)), mx.full((1, 1, 3), 8, mx.int32),
                      mx.full((1, 1, 3), -1, mx.int32)):
            with self.assertRaises(ValueError):
                validate_example(self.model, self.prompt, codes)


if __name__ == "__main__":
    unittest.main()
