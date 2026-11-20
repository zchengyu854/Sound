# VOICE˚ Voice Memory

[中文](README.md) · [English](README.en.md)

VOICE˚ is a local voice chat app. Breeze-TTS-2 generates speech, Whisper transcribes recordings locally, and the app stores voice profiles, characters, conversations, and memories on your device. Chat replies come from an OpenAI-compatible model you configure. To fine-tune a voice, train a LoRA adapter on Ubuntu and import it into the app.

Run the commands below from the project directory after [installing uv](https://docs.astral.sh/uv/getting-started/installation/). The first launch downloads the speech models if they are not already cached.

## Run on Mac

Apple Silicon Macs require macOS 14 or later and native arm64 Python 3.12.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

### Intel Mac

Intel Macs require macOS 14 or later, Python 3.12, `uv`, and Xcode Command Line Tools. MLX has no official Intel Mac wheel, so build its CPU backend from source:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python setuptools wheel cmake ninja nanobind numpy
PYPI_RELEASE=1 CMAKE_ARGS="-G Ninja -DMLX_ENABLE_X64_MAC=ON -DMLX_BUILD_METAL=OFF" uv pip install --python .venv/bin/python --no-deps --no-build-isolation "git+https://github.com/ml-explore/mlx.git@v0.32.2"
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

Open <http://127.0.0.1:8000/>. Intel Macs use the CPU, so synthesis and transcription may be slow and transcription may time out. This MLX build is not officially supported upstream.

## Run on Windows

Native Windows support is under validation. It requires x64 Windows, 64-bit Python 3.12.4 or a newer 3.12 release, `uv`, and an NVIDIA GPU and driver compatible with CUDA 12. MLX CUDA requires a GPU with compute capability 7.5 or later. Run in PowerShell:

```powershell
uv venv --python ">=3.12.4,<3.13" .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-windows.txt
.\start-windows.bat
```

You can also start the app by double-clicking `start-windows.bat`. The program runs natively on Windows and its interface is accessed in a browser; there is no standalone EXE installer yet.

For 64-bit Windows without an NVIDIA GPU, including ARM64, install the CPU backend in a fresh virtual environment:

```powershell
uv venv --python ">=3.12.4,<3.13" .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-web.txt mlx-cpu==0.32.2
.\start-windows.bat
```

Windows CPU performance has not been verified. Synthesis and transcription may be slow, and transcription may time out.

## Start a conversation

1. Once the app is running, open <http://127.0.0.1:8000/>. In **Settings → Chat Model** (「设置 → 大模型」), enter the API base URL, model name, and API key if required.
2. In **Voices** (「声音」), upload a recording of your own voice or one you have permission to use (3–30 seconds of PCM WAV, up to 10 MB) and enter its exact transcript. You can also import a trained voice. To use character settings and memories, create a character under **Characters** (「人物」) and assign it a voice.
3. Select a voice or character to start chatting. Standard chat supports typing, push-to-talk, audio playback, and WAV downloads.
4. Select **Real-time Voice** (「实时语音」) at the top of the home page to start a new session with the current character or voice. Allow microphone access for automatic replies; you can interrupt by speaking or type a message. Headphones are recommended. After refreshing, start listening again.

Open **Tone Preview** (「语气试听」) in the top bar to compare plain and sentence-by-sentence delivery with the same voice.

## Train on Ubuntu

The [`ubuntu` branch](https://github.com/zchengyu854/Sound/tree/ubuntu) provides a CLI that trains on an NVIDIA GPU from a prepared task ZIP. A separate web dashboard shows progress, loss, and logs and lets you download the result. Copy `result.zip` to the computer running the app when training finishes.

Open **Voices → Import Trained Voice** (「声音 → 导入训练声音」). The package should contain `result.json` and `adapter.safetensors` and match your local Breeze base weights. You can attach an existing reference recording and transcript or choose **No Reference Recording** (「无参考录音」). The import creates a separate voice profile. Under **Voices → Speaking Style** (「声音 → 说话语气」), choose the original or gentle style for future replies.

## Command-Line Synthesis

After installing the dependencies on an Apple Silicon Mac, generate a WAV without playing it:

```bash
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好，这是本地语音合成。'
```

## Data and Privacy

Reference recordings, generated WAV files, and conversations are stored in `data/` by default. Set `VOICE_DATA_DIR` to use another directory. Chat messages, character settings, and relevant memories are sent to your configured chat model. Reference recordings and speech synthesis stay on your device. On Windows, a newly created data directory is restricted to the current user and administrators. Check permissions if you use an existing directory. The web server binds to `127.0.0.1` by default and has no login authentication.

See the [web app guide in Chinese](docs/WEB_APP.md) for configuration, API details, and test commands.
