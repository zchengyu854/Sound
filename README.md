# VOICE˚ 声音记忆

[中文](README.md) · [English](README.en.md)

本地语音对话应用。用 Breeze-TTS-2 合成语音、用 Whisper 在本机识别录音，并将声音档案、人物、会话和记忆保存在本机。聊天回复由你配置的 OpenAI 兼容模型生成。需要微调声音时，在 Ubuntu 上训练 LoRA，再导入应用。

以下命令在项目目录执行，启动前请先[安装 uv](https://docs.astral.sh/uv/getting-started/installation/)。如果本机没有模型缓存，首次启动会下载语音模型。

## 在 Mac 上运行

Apple Silicon Mac 需要 macOS 14 或更高版本和原生 arm64 Python 3.12。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

### Intel Mac

Intel Mac 需要 macOS 14 或更高版本、Python 3.12、`uv` 和 Xcode 命令行工具。MLX 没有官方 Intel Mac 安装包，需要从源码构建 CPU 版本：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python setuptools wheel cmake ninja nanobind numpy
PYPI_RELEASE=1 CMAKE_ARGS="-G Ninja -DMLX_ENABLE_X64_MAC=ON -DMLX_BUILD_METAL=OFF" uv pip install --python .venv/bin/python --no-deps --no-build-isolation "git+https://github.com/ml-explore/mlx.git@v0.32.2"
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

打开 <http://127.0.0.1:8000/>。Intel 版使用 CPU，合成和识别可能很慢，识别也可能超时；该 MLX 构建方式不受上游官方支持。

## 在 Windows 上运行

原生 Windows 支持正在验证中。需要 x64 Windows、64 位 Python 3.12.4 或更新的 3.12 版本、`uv` 和支持 CUDA 12 的 NVIDIA 显卡及驱动。MLX CUDA 需要计算能力 7.5 或以上的显卡。在 PowerShell 中运行：

```powershell
uv venv --python ">=3.12.4,<3.13" .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-windows.txt
.\start-windows.bat
```

最后一条命令也可以通过双击 `start-windows.bat` 运行。程序在 Windows 原生运行，界面通过浏览器访问；目前没有独立的 EXE 安装包。

没有 NVIDIA 显卡的 64 位 Windows（包括 ARM64）可在新的虚拟环境中使用 CPU：

```powershell
uv venv --python ">=3.12.4,<3.13" .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-web.txt mlx-cpu==0.32.2
.\start-windows.bat
```

Windows 的 CPU 速度未经验证，合成和识别可能很慢，识别也可能超时。

## 开始对话

1. 程序启动后，打开 <http://127.0.0.1:8000/>，在「设置 → 大模型」填写接口地址、模型名和所需的 API Key。
2. 在「声音」中上传本人或已获授权的参考录音（3–30 秒的 PCM WAV，不超过 10 MB），填写录音的逐字稿；也可以导入训练声音。需要人物设定和记忆时，在「人物」中创建人物并绑定声音。
3. 选择声音或人物开始聊天。普通对话支持文字、按住说话、语音播放和 WAV 下载。
4. 点击首页顶部的「实时语音」，会带入当前人物或声音并新建会话。允许浏览器使用麦克风后，说完会自动回复，也可以开口打断或输入文字；建议佩戴耳机。刷新页面后需要重新开始监听。

## 在 Ubuntu 上训练

[`ubuntu` 分支](https://github.com/zchengyu854/Sound/tree/ubuntu) 的 CLI 接收已准备好的训练任务 ZIP，在 NVIDIA GPU 上训练。独立的网页监控展示进度、损失和日志，并提供结果下载。训练完成后，把 `result.zip` 带回使用应用的电脑。

在页面打开「声音 → 导入训练声音」。结果包应包含 `result.json` 和 `adapter.safetensors`，并匹配本机的 Breeze 基座权重。可以绑定已有参考录音及其逐字稿，也可以选「无参考录音」。导入后会创建一个可独立选择的声音档案。在「声音 → 说话语气」可选「原始语气」或「轻柔克制」，设置用于之后生成的语音。

## 命令行合成

在已安装依赖的 Apple Silicon Mac 上生成一条 WAV，跳过播放：

```bash
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好，这是本地语音合成。'
```

## 数据与隐私

参考录音、生成的 WAV 和会话保存在 `data/`，也可用 `VOICE_DATA_DIR` 指定目录。聊天文字、人物设定和相关记忆会发给你配置的聊天模型，参考录音和语音合成留在本机。Windows 新建的数据目录限制为当前用户和管理员访问；如果使用已有目录，请自行检查其访问权限。网页服务默认仅监听 `127.0.0.1`，没有登录鉴权。

更多配置、接口和测试命令见 [Web 应用说明](WEB_APP.md)。
