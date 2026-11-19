# Ubuntu CUDA Training

- `ubuntu_training.py` reads a frozen task ZIP, trains LoRA on CUDA, resumes from checkpoints, and writes a result ZIP plus `progress.json` / `training.log` for the monitor.
- `training_dashboard.py` serves the read-only `/training` monitor from `training_web/`; it reads `training-runs/*/progress.json` and never starts or cancels training.
- `training_exchange.py` and `breeze_training.py` are shared with `main`. Keep package versions, adapter layout, and base digests compatible across branches.
- Run `uv run --python 3.12 --with-requirements requirements-ubuntu.txt python -m unittest -v`. Run the opt-in hardware test from `main` for a real cross-machine exchange.
- Keep task and result ZIPs, training audio, checkpoints, and model weights outside the source tree or in ignored directories. `training-runs/` is ignored; use generated audio for verification.
