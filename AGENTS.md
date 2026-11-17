# Repository Guidelines

## Project Structure & Module Organization

- `voice_app.py`: FastAPI endpoints, SQLite persistence, task scheduling, cancellation, and Breeze integration.
- `breezetts2_mac_fast.py`: streaming inference CLI and shared helpers such as `FastDepth`, `pcm_chunks`, and `pcm16_bytes`. Preserve existing CLI behavior when changing shared code.
- `character_store.py`: character profiles, immutable conversation snapshots, and bounded memory extraction.
- `web/`: native HTML, CSS, and JavaScript; no frontend build pipeline.
- `test_voice_app.py`: backend regression tests and fake inference/provider implementations.
- `README.md` describes streaming inference; `WEB_APP.md` covers web configuration and APIs. `fish_s2_mac_stream.py.py` is a separate Fish streaming script.
- Ignored `data/` holds private recordings, generated audio, SQLite state, and migration backups. Keep verification artifacts in ignored `web-test-artifacts/`.

## Build, Test, and Development Commands

Run from the repository root using native arm64 Python 3.12 on Apple Silicon:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
.venv/bin/python -m unittest -v test_voice_app test_characters
node --check web/app.js
.venv/bin/python -m py_compile voice_app.py breezetts2_mac_fast.py
```

These commands install dependencies, serve localhost port 8000, run tests, and check syntax. No build step is required. For real CLI verification:

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
