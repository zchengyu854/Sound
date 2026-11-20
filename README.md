# VOICE˚ 声音记忆

[中文](README.md) · [English](README.en.md)

面向 Apple Silicon 的语音对话应用。用 Breeze-TTS-2 在 Mac 上合成语音、用 Whisper 在本机识别录音，并将声音档案、人物、会话和记忆保存在本机。聊天回复由你配置的 OpenAI 兼容模型生成。需要微调声音时，在 Ubuntu 上训练 LoRA，再把结果导入 Mac。

## 在 Mac 上运行

需要 Apple Silicon、原生 arm64 Python 3.12 和 `uv`。首次启动可能需要下载语音模型。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

1. 打开 <http://127.0.0.1:8000/>，在「设置 → 大模型」填写接口地址、模型名和所需的 API Key。
2. 在「声音」中上传本人或已获授权的参考录音（3–30 秒的 PCM WAV，不超过 10 MB），填写录音的逐字稿。也可以导入训练声音。
3. 选择声音直接开始对话，或在「人物」中创建人物并绑定声音。普通聊天可以输入文字、按住说话、试听和下载 WAV。免按键的实时语音对话使用 <http://127.0.0.1:8000/realtime/>。

顶部「语气试听」可以用同一个声音对比原始朗读与逐句调整语气后的效果。

## 在 Ubuntu 上训练

[`ubuntu` 分支](https://github.com/zchengyu854/Sound/tree/ubuntu) 的 CLI 接收已准备好的训练任务 ZIP，在 NVIDIA GPU 上训练。独立的网页监控展示进度、损失和日志，并提供结果下载。训练完成后，把 `result.zip` 带回 Mac。

在 Mac 页面打开「声音 → 导入训练声音」。结果包应包含 `result.json` 和 `adapter.safetensors`，并匹配本机的 Breeze 基座权重。可以绑定已有参考录音及其逐字稿，也可以选「无参考录音」。导入后会创建一个可独立选择的声音档案。在「声音 → 说话语气」可选「原始语气」或「轻柔克制」，设置用于之后生成的语音。

## 命令行合成

在已安装依赖的 Mac 上生成一条 WAV，跳过播放：

```bash
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好，这是本地语音合成。'
```

## 数据与隐私

参考录音、生成的 WAV 和会话保存在 `data/`，也可用 `VOICE_DATA_DIR` 指定目录。聊天文字、人物设定和相关记忆会发给你配置的聊天模型，参考录音和语音合成留在 Mac。网页服务默认仅监听 `127.0.0.1`，没有登录鉴权。

更多配置、接口和测试命令见 [Web 应用说明](WEB_APP.md)。
