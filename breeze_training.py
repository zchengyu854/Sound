"""Shared Breeze LoRA math and adapter compatibility helpers."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
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
