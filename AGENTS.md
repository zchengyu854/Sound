# Repository Guidelines

## Project Structure & Module Organization

- `voice_app.py`: FastAPI endpoints, SQLite persistence, task scheduling, cancellation, and Breeze integration. Standard (`mode=standard`) and realtime (`mode=realtime`) replies share one model thread; realtime streams LLM text on a separate I/O executor and synthesizes per sentence.
- `realtime_runtime.py`: incremental Chinese sentence splitting, bounded segment queue, and turn/round control for realtime replies. Pure logic; keep it free of model and I/O dependencies.
- `breezetts2_mac_fast.py`: streaming inference CLI and shared helpers such as `FastDepth`, `pcm_chunks`, and `pcm16_bytes`. Preserve existing CLI behavior when changing shared code.
- `character_store.py`: character profiles, immutable conversation snapshots, and bounded memory extraction.
- `media_processing.py`: bounded MP3/MP4/WAV probe, decode to 24 kHz mono PCM16, and silence-aware slicing.
- `training_store.py`, `training_service.py`, `breeze_training.py`: dataset/snapshot review gates, LoRA training jobs, checkpoints, versioned A/B validation records, and publishing immutable voice versions.
- `separate_vocals.py` + `requirements-separator.txt`: standalone vocal separation CLI that runs in its own `.venv-separator` and calls `demucs-mlx`; outputs a review WAV plus an uploadable MP3. Not part of the web service.
- `web/`: chat frontend (native HTML/CSS/JS, no build step). `web/vad.js` and `web/realtime-worklet.js` are shared with the realtime page.
- `training_web/`: standalone training workbench served at `/training/`. `realtime_web/`: standalone realtime voice page served at `/realtime/`.
- `test_voice_app.py`: backend regression tests and fake inference/provider implementations. `test_realtime_runtime.py` covers splitting/queue/turn logic; `test_vad.cjs` and `test_realtime_ui.cjs` cover the realtime browser logic with DOM/API doubles.
- `README.md` describes streaming inference; `WEB_APP.md` covers web configuration, APIs, training/realtime behavior, and the manual acceptance checklist. `MANUAL_TESTS.md` is the human acceptance checklist (audio listening, microphone, barge-in, latency) with recording tables. `fish_s2_mac_stream.py.py` is a separate Fish streaming script.
- Ignored `data/` holds private recordings, generated audio, SQLite state, and migration backups. Keep verification artifacts in ignored `web-test-artifacts/`.

## Build, Test, and Development Commands

Run from the repository root using native arm64 Python 3.12 on Apple Silicon:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
.venv/bin/python -m unittest -v test_voice_app test_characters test_audio_runtime \
  test_breeze_training test_media_processing test_training_store \
  test_training_service test_training_web test_separate_vocals test_realtime_runtime
node --check web/app.js && node --check training_web/app.js
node --check web/vad.js && node --check web/realtime-worklet.js && node --check realtime_web/app.js
node test_training_ui.cjs && node test_audio_ui.cjs && node test_reply_ui.cjs
node test_vad.cjs && node test_realtime_ui.cjs
.venv/bin/python -m py_compile voice_app.py breezetts2_mac_fast.py realtime_runtime.py \
  media_processing.py training_store.py training_service.py separate_vocals.py
```

These commands install dependencies, serve localhost port 8000, run the 156 backend
tests plus five Node suites, and check syntax. No build step is required. Optional
vocal separation uses a separate environment (`.venv-separator`, git-ignored) so the
pinned `mlx==0.32.2` / `mlx-audio==0.5.1` of `.venv` stays untouched:

```sh
uv venv --python 3.12 .venv-separator
uv pip install --python .venv-separator/bin/python -r requirements-separator.txt
.venv/bin/python separate_vocals.py /path/to/song.mp3 -o web-test-artifacts/sep/
```

For real CLI verification:

```sh
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好' --output /tmp/breeze_check
```

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Match existing two-space JavaScript/CSS indentation and JavaScript `camelCase`. No formatter or lint configuration is established. Reuse existing helpers; avoid new dependencies or abstractions for small changes. Keep user-facing errors actionable and preserve Chinese interface wording.

## Testing Guidelines

Use `unittest`, FastAPI `TestClient`, temporary databases, and mocked HTTP transports. Name tests `test_<behavior>`. Cover changed validation, task ownership, cancellation, retries, idempotency, and migration behavior. No coverage threshold is configured. Hardware checks supplement automated tests; generated audio alone does not establish voice similarity.

## Commit & Pull Request Guidelines

History uses short descriptions such as `Update README.md`; no enforced commit convention exists. Prefer specific imperative summaries. PRs should describe behavior, validation performed, linked issues when applicable, and compatibility or migration risks. Include screenshots for visual changes and measured results for performance claims.

## Security & Architecture Constraints

Run one application process without multiple workers or reload. Keep model operations on the single inference thread; release task ownership only after execution exits. Export `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`; `.env.example` is documentation, not automatically loaded configuration. Never commit credentials, private audio, or chat history. Preserve localhost binding, UUID/path checks, migration backups, and diagnostic redaction.
