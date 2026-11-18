"""Experimental Breeze LoRA feasibility gate; run separately from the web server.

This module deliberately does not publish voices or mark imported audio trained.
The full training service is gated on the real-model probe succeeding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten, tree_unflatten


class LoRALinear(nn.Module):
    """Keep the original (including quantized) linear frozen."""

    def __init__(self, linear, rank=4):
        super().__init__()
        if rank not in (1, 2, 4, 8):
            raise ValueError("LoRA rank 必须为 1、2、4 或 8。")
        self.linear = linear
        out_dim = linear.weight.shape[0]
        in_dim = (linear.weight.shape[1] * 32 // linear.bits
                  if isinstance(linear, nn.QuantizedLinear) else linear.weight.shape[1])
        self.lora_a = mx.random.normal((in_dim, rank)) * (1 / math.sqrt(in_dim))
        self.lora_b = mx.zeros((rank, out_dim))
        linear.freeze()

    def __call__(self, x):
        return self.linear(x) + ((x @ self.lora_a) @ self.lora_b).astype(x.dtype)


def attach_lora(model, rank=4):
    """Adapt q/v attention projections only, in both autoregressive stacks."""
    targets = []
    for name, module in model.named_modules():
        if (name.startswith(("backbone_model.layers.", "depth_decoder.model.layers."))
                and name.endswith((".self_attn.q_proj", ".self_attn.v_proj"))
                and isinstance(module, (nn.Linear, nn.QuantizedLinear))):
            targets.append((name, LoRALinear(module, rank)))
    if not targets or not any(n.startswith("depth_decoder.") for n, _ in targets):
        raise ValueError("Breeze 注意力结构不兼容。")
    model.freeze()
    model.update_modules(tree_unflatten(targets))
    for _, module in targets:
        module.unfreeze(keys=["lora_a", "lora_b"], recurse=False)
    return [name for name, _ in targets]


def validate_example(model, prompt, codes):
    if (prompt.ndim != 3 or prompt.shape[0] != 1 or prompt.shape[1] < 1
            or prompt.shape[2] != model.config.backbone_hidden_size):
        raise ValueError("文本提示形状无效。")
    if (codes.ndim != 3 or codes.shape[0] != 1 or codes.shape[1] < 1
            or codes.shape[2] != model.num_codebooks
            or codes.dtype not in (mx.int32, mx.int64, mx.uint32)):
        raise ValueError("音频 token 必须为单条非空整数帧。")
    if not bool(mx.all((codes >= 0) & (codes < model.config.codec_vocab_size)).item()):
        raise ValueError("音频 token 包含 padding、控制符或越界码。")


def teacher_forcing_terms(model, prompt, codes):
    """Return the main (first codebook and EOS) loss and the depth loss.

    Batch one, no padding: prompt tokens have no loss; EOS has no depth loss.
    Last prompt state predicts frame 0; state after frame t predicts t+1.
    Depth position 0 is the backbone state; position k predicts codebook k.
    """
    frames = codes.shape[1]
    embeddings = mx.concatenate((mx.stop_gradient(prompt),
                                 model.backbone_model.embed_tokens(codes)), axis=1)
    hidden = model.backbone_model(input_embeddings=embeddings)
    states = hidden[:, prompt.shape[1] - 1:, :]
    first_targets = mx.concatenate((codes[:, :, 0],
                                   mx.full((1, 1), model.vocab_size, mx.int32)), axis=1)
    first_logits = model._mask_reserved_codec_logits(model.lm_head(states))
    main_loss = nn.losses.cross_entropy(first_logits, first_targets, reduction="mean")
    tokens = codes[0]
    depth_input = mx.concatenate((mx.zeros((frames, 1), mx.int32), tokens[:, :-1]), axis=1)
    depth_hidden = model.depth_decoder.model(depth_input, states[0, :frames])
    losses = []
    for head in range(model.num_codebooks - 1):
        logits = depth_hidden[:, head + 1] @ model.depth_decoder.codebooks_head.weight[head]
        logits = model._mask_reserved_codec_logits(logits)
        losses.append(nn.losses.cross_entropy(logits, tokens[:, head + 1], reduction="mean"))
    # A single-codebook model has no depth heads, so its depth loss is absent.
    depth_loss = mx.stack(losses).mean() if losses else None
    return main_loss, depth_loss


def teacher_forcing_loss(model, prompt, codes):
    """Main plus depth loss, unchanged objective for existing recipes."""
    main_loss, depth_loss = teacher_forcing_terms(model, prompt, codes)
    return main_loss if depth_loss is None else main_loss + depth_loss


def parameter_digest(parameters):
    digest = hashlib.sha256()
    for name, value in sorted(tree_flatten(parameters)):
        mx.eval(value)
        digest.update(name.encode())
        digest.update(str((value.shape, value.dtype)).encode())
        digest.update(memoryview(__import__("numpy").array(value.reshape(-1).view(mx.uint8))).tobytes())
    return digest.hexdigest()


def frozen_digest(model):
    return parameter_digest(tree_unflatten([
        (n, v) for n, v in tree_flatten(model.parameters())
        if not n.endswith((".lora_a", ".lora_b"))]))


def train_step(model, optimizer, prompt, codes):
    validate_example(model, prompt, codes)
    loss, gradients = nn.value_and_grad(model, teacher_forcing_loss)(model, prompt, codes)
    mx.eval(loss, gradients)
    values = [v for _, v in tree_flatten(gradients)]
    if not math.isfinite(loss.item()) or not all(bool(mx.all(mx.isfinite(v)).item()) for v in values):
        raise ValueError("训练损失或梯度非有限值。")
    norm = math.sqrt(sum(float(mx.sum(v.astype(mx.float32) ** 2).item()) for v in values))
    if norm == 0:
        raise ValueError("LoRA 梯度为零。")
    optimizer.update(model, gradients)
    mx.eval(model.trainable_parameters(), optimizer.state)
    return {"loss": loss.item(), "gradient_norm": norm}


def save_adapter(model, destination):
    destination = Path(destination)
    temporary = destination.with_name(destination.stem + ".tmp.safetensors")
    mx.save_safetensors(str(temporary), dict(tree_flatten(model.trainable_parameters())))
    os.replace(temporary, destination)


def load_adapter(model, source):
    weights = mx.load(str(source))
    expected = dict(tree_flatten(model.trainable_parameters()))
    if set(weights) != set(expected) or any(weights[k].shape != v.shape for k, v in expected.items()):
        raise ValueError("Adapter 与基座或 LoRA 配置不兼容。")
    if not all(bool(mx.all(mx.isfinite(v)).item()) for v in weights.values()):
        raise ValueError("Adapter 含非有限值。")
    model.load_weights(list(weights.items()), strict=False)
    mx.eval(model.trainable_parameters())


def probe(args):
    """Real model gate. Report each completed stage, even on a native failure."""
    from mlx_audio.tts.utils import load_model
    from breezetts2_mac_fast import pcm16_bytes
    import wave

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {"status": "running", "stage": "loading", "model": args.model,
              "rank": args.rank, "device": mx.device_info(), "steps": 0}

    def record(stage, **fields):
        report.update(stage=stage, elapsed_seconds=time.perf_counter() - started,
                      peak_memory_bytes=mx.get_peak_memory(), **fields)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)

    # Keep allocations within this machine's recommended Metal working set.
    mx.set_memory_limit(mx.device_info()["max_recommended_working_set_size"])
    mx.set_cache_limit(256 * 1024**2)
    record("loading")
    try:
        model = load_model(args.model)
        targets = attach_lora(model, args.rank)
        record("encoding", targets=targets)
        import numpy as np
        with wave.open(str(args.audio), "rb") as wav:
            rate = wav.getframerate()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("验证素材必须为单声道 PCM16 WAV。")
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        if rate != model.sample_rate or not 0.1 <= len(audio) / rate <= 3:
            raise ValueError("验证素材必须为 0.1–3 秒、24 kHz 单声道 WAV。")
        codes = mx.stop_gradient(model._encode_reference(mx.array(audio)))
        prompt = mx.stop_gradient(model._prompt_embeddings(
            args.text, voice=None, instruct=None, ref_audio=None, ref_text=None))
        mx.eval(codes, prompt)
        record("checking_tokens", code_shape=list(codes.shape), code_dtype=str(codes.dtype))
        validate_example(model, prompt, codes)
        before_base = frozen_digest(model)
        before_adapter = parameter_digest(model.trainable_parameters())
        record("training", frames=codes.shape[1], frozen_before=before_base)
        result = train_step(model, optim.Adam(learning_rate=1e-4), prompt, codes)
        if frozen_digest(model) != before_base:
            raise RuntimeError("基座参数发生变化。")
        trained_digest = parameter_digest(model.trainable_parameters())
        if trained_digest == before_adapter:
            raise RuntimeError("Adapter 参数未更新。")
        save_adapter(model, output / "adapter.safetensors")
        record("reloading", steps=1, base_unchanged=True, adapter_changed=True, **result)
        # Reload into a fresh base, rather than merely assigning to the trained model.
        del model, codes, prompt
        import gc
        gc.collect()
        mx.clear_cache()
        model = load_model(args.model)
        attach_lora(model, args.rank)
        load_adapter(model, output / "adapter.safetensors")
        if parameter_digest(model.trainable_parameters()) != trained_digest:
            raise RuntimeError("Adapter 保存加载不一致。")
        record("synthesizing", adapter_roundtrip=True)
        chunks = [result.audio for result in model.generate(
            text=args.text, max_tokens=40, temperature=0.7)]
        audio = mx.concatenate(chunks)
        mx.eval(audio)
        if audio.size == 0 or not bool(mx.all(mx.isfinite(audio)).item()):
            raise RuntimeError("合成结果无效。")
        with wave.open(str(output / "probe.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(model.sample_rate)
            wav.writeframes(pcm16_bytes(audio))
        record("completed", status="passed", audio_samples=audio.size)
    except Exception as exc:
        record(report["stage"], status="failed", error_type=type(exc).__name__, error=str(exc))
        raise


def main():
    parser = argparse.ArgumentParser(description="独立 Breeze LoRA 最小训练可行性验证")
    parser.add_argument("--model", default="mlx-community/Breeze-TTS-2-mlx-4bit")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--rank", type=int, choices=(1, 2, 4, 8), default=4)
    parser.add_argument("--output", required=True)
    probe(parser.parse_args())


if __name__ == "__main__":
    main()
