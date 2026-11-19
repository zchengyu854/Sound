"""Run a frozen Breeze training task on Ubuntu CUDA, then write a result ZIP."""
import argparse
import fcntl
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from uuid import UUID
import wave
from zipfile import ZipFile, ZIP_STORED

from training_exchange import (MAX_CLIP, MAX_MANIFEST, PACKAGE_VERSION, RECIPE_VERSION,
                               TrainingConfig, canonical, deterministic_order, read_member, sha256)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_progress(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical(data), encoding="utf-8")
    temporary.replace(path)


def append_log(path, message):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{message}\n")


def fail_progress(path, task_name, output_name, message, status="failed"):
    data = {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            data = value
    except (OSError, ValueError):
        pass
    data.update({"status": status, "task": task_name, "output": output_name,
                 "error": message, "updated_at": utc_now()})
    write_progress(path, data)


def save_checkpoint(work, model, optimizer, step, metadata):
    import mlx.core as mx
    from mlx.utils import tree_flatten
    from breeze_training import save_adapter

    with tempfile.TemporaryDirectory(dir=work) as temporary:
        directory = Path(temporary) / "checkpoint"
        directory.mkdir()
        save_adapter(model, directory / "adapter.safetensors")
        mx.save_safetensors(str(directory / "optimizer.safetensors"), dict(tree_flatten(optimizer.state)))
        mx.save_safetensors(str(directory / "random.safetensors"),
                            {str(i): value for i, value in enumerate(mx.random.state)})
        (directory / "metadata.json").write_text(canonical(metadata))
        directory.replace(work / f"checkpoint-{step:04d}")


def train(identity, snapshot, audio_by_id, work, base_dir, progress_path, log_path, task_name, output_name):
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten, tree_map, tree_unflatten
    import numpy as np
    from mlx_audio.tts.utils import load_model
    from breeze_training import (attach_lora, frozen_digest, load_adapter, parameter_digest,
                                 teacher_forcing_loss, validate_example)

    mx.set_default_device(mx.gpu)
    mx.set_cache_limit(256 * 1024**2)
    config = identity["config"]
    started_at = utc_now()
    session_started = time.monotonic()
    checkpoint = max(work.glob("checkpoint-[0-9][0-9][0-9][0-9]"), default=None)
    model = load_model(base_dir or identity["base_model"])
    # Seed before LoRA initialization: the adapter matrices are part of
    # the experiment, so the same seed must reproduce the same start.
    seed = config["seed"]
    mx.random.seed(seed)
    attach_lora(model, config["rank"])
    initial_adapter = parameter_digest(model.trainable_parameters())
    base_digest = frozen_digest(model)
    optimizer = optim.Adam(config["learning_rate"])
    step = 0
    sampling = {"epoch": 0, "position": 0}
    if checkpoint is not None:
        metadata = json.loads((checkpoint / "metadata.json").read_text())
        if metadata["base_digest"] != base_digest or metadata["identity"] != identity:
            raise ValueError("基座或任务与检查点不一致。")
        directory = checkpoint
        load_adapter(model, directory / "adapter.safetensors")
        optimizer.state = tree_unflatten(list(mx.load(str(directory / "optimizer.safetensors")).items()))
        random_state = mx.load(str(directory / "random.safetensors"))
        mx.random.state = [random_state[str(i)] for i in range(len(random_state))]
        step = metadata["step"]
        sampling = metadata["sampling"]
        initial_adapter = metadata["initial_adapter_digest"]
    session_start_step = step
    examples = snapshot["segments"]
    training = [examples[i] for i in snapshot["train_indices"]]
    cache = work / f"tokens-{base_digest}"
    cache.mkdir(exist_ok=True, mode=0o700)

    def example(segment):
        target = cache / f"{segment['id']}.safetensors"
        if target.is_file():
            arrays = mx.load(str(target))
            prompt, codes = arrays["prompt"], arrays["codes"]
        else:
            with wave.open(io.BytesIO(audio_by_id(segment)), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (24000, 1, 2):
                    raise ValueError("训练音频必须为 24 kHz 单声道 PCM16 WAV。")
                audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
            with mx.stream(mx.cpu):
                codes = mx.stop_gradient(model._encode_reference(mx.array(audio)))
                mx.eval(codes)
            prompt = mx.stop_gradient(model._prompt_embeddings(segment["transcript"], voice=None,
                                            instruct=None, ref_audio=None, ref_text=None))
            mx.eval(prompt, codes)
            validate_example(model, prompt, codes)
            temp = target.with_suffix(".tmp.safetensors")
            mx.save_safetensors(str(temp), {"prompt": prompt, "codes": codes})
            temp.replace(target)
        validate_example(model, prompt, codes)
        return prompt, codes

    order = {"epoch": sampling["epoch"], "position": sampling["position"], "order": []}
    if order["epoch"] or order["position"]:
        order["order"] = deterministic_order(len(training), seed, order["epoch"])

    def next_index():
        # Every epoch visits all training segments once, in a seeded order.
        if order["position"] >= len(order["order"]):
            order["epoch"] += 1
            order["order"] = deterministic_order(len(training), seed, order["epoch"])
            order["position"] = 0
        index = order["order"][order["position"]]
        order["position"] += 1
        return index

    validation_segments = [examples[i] for i in snapshot["validation_indices"]]
    step_log = metadata["step_log"] if checkpoint else []
    validation_trend = metadata["validation_trend"] if checkpoint else []

    def emit(message):
        print(message, flush=True)
        append_log(log_path, message)

    def report(status="running", **extra):
        done = step - session_start_step
        elapsed = time.monotonic() - session_started
        eta = round(elapsed / done * (config["steps"] - step), 1) if status == "running" and done > 0 else None
        data = {
            "status": status,
            "task": task_name,
            "output": output_name,
            "started_at": started_at,
            "updated_at": utc_now(),
            "step": step,
            "total_steps": config["steps"],
            "epoch": order["epoch"],
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta,
            "step_log": step_log,
            "validation_trend": validation_trend,
        }
        data.update(extra)
        write_progress(progress_path, data)

    def validation_losses():
        values = []
        for segment in validation_segments:
            prompt, codes = example(segment)
            score = teacher_forcing_loss(model, prompt, codes)
            mx.eval(score)
            if not math.isfinite(score.item()):
                raise ValueError("validation_nonfinite")
            values.append(round(score.item(), 6))
        return values

    def record_trend(step_number):
        if not validation_segments:
            return None
        values = validation_losses()
        entry = {"step": step_number, "losses": values,
                 "mean": round(sum(values) / len(values), 6)}
        validation_trend.append(entry)
        return entry

    gradient_fn = nn.value_and_grad(model, teacher_forcing_loss)
    emit(f"训练开始: {task_name} -> {output_name}")
    report()
    while step < config["steps"]:
        accumulated = None
        micro_losses = []
        for _ in range(config["accumulation_steps"]):
            index = next_index()
            prompt, codes = example(training[index])
            loss, gradients = gradient_fn(model, prompt, codes)
            mx.eval(loss, gradients)
            micro_losses.append(loss.item())
            values = [g for _, g in tree_flatten(gradients)]
            if not math.isfinite(loss.item()) or not all(bool(mx.all(mx.isfinite(g)).item()) for g in values):
                raise ValueError("nonfinite_gradient")
            scaled = tree_map(lambda g: g / config["accumulation_steps"], gradients)
            accumulated = scaled if accumulated is None else tree_map(lambda a, b: a + b, accumulated, scaled)
        gradient_norm = math.sqrt(sum(
            float(mx.sum(g.astype(mx.float32) ** 2).item())
            for _, g in tree_flatten(accumulated)))
        if not math.isfinite(gradient_norm) or gradient_norm == 0:
            raise ValueError("LoRA 梯度无效。")
        optimizer.update(model, accumulated)
        mx.eval(model.trainable_parameters(), optimizer.state)
        step += 1
        step_log.append({"step": step,
                         "loss": round(sum(micro_losses) / len(micro_losses), 6),
                         "gradient_norm": round(gradient_norm, 6),
                         "learning_rate": config["learning_rate"],
                         "epoch": order["epoch"]})
        if step % config["checkpoint_every"] == 0 or step == config["steps"]:
            record_trend(step)
            save_checkpoint(work, model, optimizer, step, {
                "identity": identity, "base_digest": base_digest, "step": step,
                "sampling": {key: order[key] for key in ("epoch", "position")},
                "step_log": step_log, "validation_trend": validation_trend,
                "initial_adapter_digest": initial_adapter})
        peak = mx.get_peak_memory() / 1024**3
        emit(f"{step}/{config['steps']}  loss={step_log[-1]['loss']:.6f}  peak={peak:.2f} GiB")
        report(loss=step_log[-1]["loss"], gradient_norm=step_log[-1]["gradient_norm"],
               learning_rate=config["learning_rate"], peak_memory_gib=round(peak, 2))
    if frozen_digest(model) != base_digest:
        raise ValueError("base_weights_changed")
    validation_losses_final = validation_trend[-1]["losses"] if validation_trend else []
    best = min(validation_trend, key=lambda entry: entry["mean"]) if validation_trend else None
    if parameter_digest(model.trainable_parameters()) == initial_adapter:
        raise ValueError("LoRA 参数未更新。")
    adapter = (work / f"checkpoint-{step:04d}" / "adapter.safetensors").read_bytes()
    result = {**identity, "base_digest": base_digest, "steps": step,
              "adapter_sha256": sha256(adapter), "validation_losses": validation_losses_final,
              "validation_trend": validation_trend, "step_log": step_log,
              "initial_adapter_digest": initial_adapter,
              "sampling": {key: order[key] for key in ("epoch", "position")},
              "best_checkpoint": ({"step": best["step"], "validation_loss": best["mean"]} if best else None)}
    last = step_log[-1] if step_log else {}
    report("completed", loss=last.get("loss"), gradient_norm=last.get("gradient_norm"),
           learning_rate=config["learning_rate"],
           best_checkpoint=result["best_checkpoint"], validation_losses=validation_losses_final,
           final_checkpoint_step=step, adapter_sha256=result["adapter_sha256"])
    emit(f"训练完成: {step} 步")
    return result, adapter


def run(task, output, base_dir):
    output.parent.mkdir(parents=True, exist_ok=True)
    progress_path = output.parent / "progress.json"
    log_path = output.parent / "training.log"
    with ZipFile(task) as package:
        manifest = json.loads(read_member(package, "task.json", MAX_MANIFEST))
        identity, snapshot = manifest["identity"], manifest["snapshot"]
        if (identity["format"] != PACKAGE_VERSION or identity["recipe_version"] != RECIPE_VERSION
                or sha256(canonical(snapshot).encode()) != identity["snapshot_digest"]):
            raise ValueError("训练任务版本或快照摘要不匹配。")
        if TrainingConfig.model_validate(identity["config"]).checked() != identity["config"]:
            raise ValueError("训练配置不完整。")
        segments = snapshot["segments"]
        ids = [segment["id"] for segment in segments]
        indices = snapshot["train_indices"] + snapshot["validation_indices"]
        if (not snapshot["train_indices"] or sorted(indices) != list(range(len(segments)))
                or len(set(ids)) != len(ids) or any(str(UUID(value)) != value for value in ids)):
            raise ValueError("训练快照片段或划分无效。")
        files = {value: f"audio/{index}.wav" for index, value in enumerate(ids)}

        def audio_by_id(segment):
            audio = read_member(package, files[segment["id"]], MAX_CLIP)
            if sha256(audio) != (segment.get("sha256") or segment.get("audio_sha256")):
                raise ValueError(f"音频摘要不匹配：{segment['id']}")
            return audio

        for segment in segments:
            audio_by_id(segment)
        work = output.parent / (".breeze-" + sha256(canonical(identity).encode()))
        work.mkdir(mode=0o700, exist_ok=True)
        write_progress(progress_path, {"status": "running", "task": task.name, "output": output.name,
                                       "started_at": utc_now(), "updated_at": utc_now(), "step": 0,
                                       "total_steps": identity["config"]["steps"],
                                       "step_log": [], "validation_trend": []})
        with (work / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result, adapter = train(identity, snapshot, audio_by_id, work, base_dir,
                                        progress_path, log_path, task.name, output.name)
                with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
                    target = Path(temporary) / "result.zip"
                    with ZipFile(target, "w", ZIP_STORED) as archive:
                        archive.writestr("result.json", canonical(result))
                        archive.writestr("adapter.safetensors", adapter)
                    os.replace(target, output)
            except KeyboardInterrupt:
                fail_progress(progress_path, task.name, output.name, "训练被中断。", "interrupted")
                append_log(log_path, "训练被中断。")
                raise
            except Exception as exc:
                fail_progress(progress_path, task.name, output.name, str(exc))
                append_log(log_path, f"训练失败: {exc}")
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base-dir", type=Path, help="同一基座的本地模型目录")
    args = parser.parse_args()
    if args.task.resolve() == args.output.resolve():
        parser.error("输入和输出必须是不同文件。")
    run(args.task, args.output, args.base_dir)


if __name__ == "__main__":
    main()
