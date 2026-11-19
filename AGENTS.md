# Ubuntu CUDA Training

- `ubuntu_training.py` reads a Mac workbench task ZIP, trains LoRA on CUDA, resumes from checkpoints, and writes a result ZIP.
- `training_exchange.py` and `breeze_training.py` are shared with `main`. Keep package versions, adapter layout, and base digests compatible across branches.
- Run `uv run --python 3.12 --with-requirements requirements-ubuntu.txt python -m unittest -v`. Run the opt-in hardware test from `main` for a real cross-machine exchange.
- Keep task and result ZIPs, training audio, checkpoints, and model weights outside the source tree or in ignored directories. Use generated audio for verification.
