# VOICE˚ Voice Memory

[中文](README.md) · [English](README.en.md)

VOICE˚ is a voice chat app for Apple Silicon. Breeze-TTS-2 generates speech on your Mac, Whisper transcribes recordings locally, and the app stores voice profiles, characters, conversations, and memories on your device. Chat replies come from an OpenAI-compatible model you configure. To fine-tune a voice, train a LoRA adapter on Ubuntu and import it on your Mac.

## Run on Mac

Requires Apple Silicon, native arm64 Python 3.12, and `uv`. The first launch may download the speech models.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
.venv/bin/python voice_app.py
```

1. Open <http://127.0.0.1:8000/>. In **Settings → Chat Model** (「设置 → 大模型」), enter the API base URL, model name, and API key if required.
2. In **Voices** (「声音」), upload a recording of your own voice or one you have permission to use (3–30 seconds of PCM WAV, up to 10 MB) and enter its exact transcript. You can also import a trained voice.
3. Start a conversation with a voice, or create a character and assign it a voice under **Characters** (「人物」). Standard chat supports typing, push-to-talk, audio playback, and WAV downloads. For hands-free voice chat, open <http://127.0.0.1:8000/realtime/>.

## Train on Ubuntu

The [`ubuntu` branch](https://github.com/zchengyu854/Sound/tree/ubuntu) provides a CLI that trains on an NVIDIA GPU from a prepared task ZIP. A separate web dashboard shows progress, loss, and logs and lets you download the result. Copy `result.zip` to your Mac when training finishes.

On your Mac, open **Voices → Import Trained Voice** (「声音 → 导入训练声音」). The package should contain `result.json` and `adapter.safetensors` and match your local Breeze base weights. You can attach an existing reference recording and transcript or choose **No Reference Recording** (「无参考录音」). The import creates a separate voice profile. Under **Voices → Speaking Style** (「声音 → 说话语气」), choose the original or gentle style for future replies.

## Command-Line Synthesis

After installing the dependencies on your Mac, generate a WAV without playing it:

```bash
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好，这是本地语音合成。'
```

## Data and Privacy

Reference recordings, generated WAV files, and conversations are stored in `data/` by default. Set `VOICE_DATA_DIR` to use another directory. Chat messages, character settings, and relevant memories are sent to your configured chat model. Reference recordings and speech synthesis stay on your Mac. The web server binds to `127.0.0.1` by default and has no login authentication.

See the [web app guide in Chinese](WEB_APP.md) for configuration, API details, and test commands.
