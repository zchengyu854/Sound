# BreezeTTS2 Mac Streaming

面向 Apple Silicon 的本地语音项目：Mac 负责网页聊天、录音和流式语音调用，Ubuntu 负责训练控制台与 CUDA LoRA 训练。

- `breezetts2_mac_fast.py`：重写 Breeze-TTS-2 流式生成循环，减少重复计算并输出 RTF / underflow 诊断。
- `voice_app.py` + `web/`：FastAPI + 原生网页 + SQLite 的本地语音聊天，支持参考声音、独立会话和 AI 文字回复。
- `voice_adapters.py`：校验 Ubuntu 导出的 `result.zip`，Mac 可将 adapter 绑定到已有参考声音并生成本机可用的训练声音。
- `training_service.py`：Mac 侧只保留声音产物加载和交互运行需要的后端能力，不提供训练页面或 `/api/training` 路由。
- [`ubuntu` 分支](https://github.com/zchengyu854/Sound/tree/ubuntu) 提供独立训练控制台、CUDA LoRA worker、任务状态和结果包下载。

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

训练页面不运行在 Mac。切换到 `ubuntu` 分支并在训练节点完成训练、下载结果 ZIP，再回到 Mac 主页面点击“已有训练产物？绑定到参考声音”导入。Mac 不向 Ubuntu 提交任务、不控制训练，也不提供 A/B 工作台。结果包只包含 `result.json` 与 `adapter.safetensors`，导入时必须选择一个已有声音作为参考 WAV 和原文；导入会生成独立声音，原参考声音保持不变。
