# BreezeTTS2 Mac Streaming

面向 Apple Silicon 的本地语音项目：用 Breeze-TTS-2 做流式语音合成，并提供网页聊天与本地声音训练工作台。

- `breezetts2_mac_fast.py`：重写 Breeze-TTS-2 流式生成循环，减少重复计算并输出 RTF / underflow 诊断。
- `voice_app.py` + `web/`：FastAPI + 原生网页 + SQLite 的本地语音聊天，支持参考声音、独立会话和 AI 文字回复。
- `training_service.py` + `training_web/`：本地声音训练工作台，支持 MP3 / MP4 预处理、切片转写、试听对照与发布。

## 快速开始

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

打开 <http://127.0.0.1:8000/>。

命令行直接生成语音：

```bash
uv run --python 3.12 breezetts2_mac_fast.py
```

训练功能还需要本机安装 `ffmpeg` 和 `ffprobe`。更多配置与 API 见 [WEB_APP.md](WEB_APP.md)。
