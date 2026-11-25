# 声音 · 本地声音聊天

Apple Silicon Mac 上运行的单用户网页应用。添加参考声音 → 创建虚拟人物并绑定声音 → 新建会话 → AI 文字回复 → Breeze 本地合成 → 手动播放或下载 WAV。旧的仅声音会话仍然可用。

## 启动

在项目目录运行（原生 arm64 Python 3.12）：

```sh
uv venv --python 3.12 .venv  # 已有 .venv 时跳过此行
uv pip install --python .venv/bin/python -r requirements-web.txt

.venv/bin/python voice_app.py
```

打开 <http://127.0.0.1:8000>，点击右上角“设置”→“大模型”，填写接口地址（Base URL）、模型名与服务所需的 API Key，点击“保存配置”。接口地址填写前缀，应用会追加 `/chat/completions`。没有现有配置时预填 `https://api.lingsuan.si/v1` 与 `deepseek-v4.1-flash-1017`，点击保存前不会生效。未配置时仍可管理声音与历史。

保存后立即用于新请求，无需重启；刷新页面及重启应用后保留。保存成功仅表示配置已保存，不代表服务已连通、鉴权成功或模型可用。聊天、识别或人物记忆任务执行期间会拒绝保存，请任务结束后重试。

API Key 使用密码框，只显示“已设置 / 未设置”，不会从后端读取或回显密钥明文。留空保留原密钥，填写新值替换，勾选“清除已保存密钥”后保存可改用无需鉴权的服务。更换接口地址且已有密钥时，必须重新输入密钥或明确选择清除，防止将旧服务密钥误发到新地址。地址仅接受有效 HTTP/HTTPS URL，不允许内嵌用户名、密码、查询参数或片段；尾部斜杠会统一移除。

私有配置保存在实际 `VOICE_DATA_DIR` 下的 `llm-settings.json`（默认 `data/llm-settings.json`），权限为 `0600`，通过同目录临时文件与原子替换写入。写入失败保留旧文件和运行配置；文件损坏会明确报错并阻止启动，不会静默覆盖。密钥以明文保存在该私有文件中，备份时请一并保护；配置不存入浏览器 localStorage 或应用日志，勿将私有数据目录加入 Git。

从未保存网页配置时，继续兼容启动环境中的 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`（本地无鉴权服务可省略密钥）。保存过网页配置后，以文件为准，包括明确清除的空密钥；如需恢复环境变量配置，应先停服并移走私有配置文件，再重启。`.env.example` 只是配置参考，不会自动加载。

首次加载可能下载 Breeze 和 Whisper 权重；默认分别使用 `mlx-community/Breeze-TTS-2-mlx-4bit` 和 `mlx-community/whisper-small-mlx`，可通过 `BREEZE_MODEL` 和 `STT_MODEL` 指定本地权重目录。Whisper 模型目录缺少处理器文件时，还需缓存 `openai/whisper-small` 分词器；首次准备需要联网，缓存完整后可离线使用。两个模型在同一个工作线程加载和推理；识别模型加载失败不影响文字聊天和语音合成。

只运行一个进程，勿使用 `--workers` 或 `--reload`。默认仅监听 `127.0.0.1`，应用没有账号鉴权，不要将端口转发或代理到公网。

## 使用与数据

- 点击“添加声音”，上传本人或已获授权的录音，并逐字填写原文。支持未压缩 PCM WAV（8/16/24/32 位、单/双声道、8–96 kHz），实际时长 3–30 秒，文件不超过 10 MB。不支持压缩 WAV 或浮点 WAV；不能仅修改其他格式的扩展名。
- 录音应只有一位说话者、人声清晰、无背景音乐。程序会检查格式、实际帧数、时长和完全静音；**不会自动识别人声、授权情况或核对录音原文**。
- 普通声音 UUID 代表参考声音档案，默认使用原始 Breeze；经下述训练 API 发布的新声音还会关联独立、不可变的 adapter 版本。切换声音会创建新会话；相同声音可创建多段会话。
- 发送后先显示文字，再显示语音。文字失败可重试回复；语音失败会保留文字，可只重试合成。文字失败时需先重试或新建会话，避免聊天上下文错位。
- 页面通过 SSE 实时显示模型返回的思考与回复正文。思考内容只在聊天服务实际返回 `reasoning_content` / `reasoning` 时展示，生成中默认展开、完成后折叠，刷新后仍可展开；未返回思考时不会伪造。
- 语音不会自动播放；合成期间点击“试听生成中的语音”可从头边生成边听，停止试听不会取消合成。完成后原生音频控件支持暂停、拖动与重播，可下载 PCM16 WAV。
- 输入框为空时按住“按住说话”录音，松开后在本机识别中文；结果只填入输入框，检查并编辑后手动发送。需要浏览器麦克风权限，单次录音 0.5–30 秒，录音不会保存为声音档案或发送到聊天服务。
- 一条 AI 回复显示为一张卡片，段落之间用留白分隔；用户消息保持独立气泡。界面固定使用黑色主题，不随系统浅色/深色设置切换。
- 右上角独立的“设置”按钮打开大模型与外观设置，窄屏显示为设置图标。“界面显示”提供聊天面板透明度滑块（0%–85%，默认35%），数值越大越透明。仅改变聊天区域背景，顶部、侧栏和文字不跟随变透明；设置保存在当前浏览器，刷新后恢复。“本地”按钮仅展示服务、模型和任务状态。
- 背景图片与网格保持静态，不再显示跟随鼠标的光圈或位移效果。
- 生成过程中输入区显示“停止”；点击后请求取消，任务结束保存为失败状态并保留原有重试入口。窄屏下从会话列表进入聊天，聊天页左上角返回列表。
- 当前会话保留最近 10 组有效问答，长聊后另附有来源的会话摘要；摘要整理落后或失败时，近期问答最多补到 20 组。语音失败但正文完整的回复仍参与上下文，取消与打断跳过。普通回复最多请求 160 个输出 token；聊天服务报告截断或 Breeze 达到 750 声学帧上限时，消息中会明确提示。
- 参考录音、原文、声音名称、消息、状态、思考内容及生成 WAV 保存在项目 `data/`，可通过 `VOICE_DATA_DIR` 改为专用私有目录。目录已加入 `.gitignore`。刷新/重启保留历史，启动会将未完成任务标为失败，允许重试。备份或迁移时先停止应用，复制整个数据目录；首版不提供删除界面。
- 聊天文字会发送给你配置的大模型服务，密钥仅留在后端。参考录音不发送给聊天服务，Breeze 合成留在本机。首次模型下载需要联网，聊天服务有自己的隐私与计费规则。
- 页面字体来自 Google Fonts，背景图来自 Higgs 图片服务，首次加载需要联网；它们不接收聊天内容或录音。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -p "test_voice_app.py" -v
.venv/bin/python -m unittest discover -s tests -p "test_characters.py" -v
.venv/bin/python -m unittest discover -s tests -p "test_conversation_summary.py" -v
.venv/bin/python -m unittest discover -s tests -p "test_character_growth.py" -v
.venv/bin/python -m unittest discover -s tests -p "test_audio_runtime.py" -v
node --check web/app.js && node --check web/vad.js && node --check web/audio.js
node tests/test_audio_ui.cjs
node tests/test_reply_ui.cjs
node tests/test_evolution_ui.cjs
node tests/test_vad.cjs
node tests/test_realtime_ui.cjs
# 原命令行用法保持兼容：
.venv/bin/python breezetts2_mac_fast.py --no-play --runs 1 --text '你好，这是命令行兼容检查。' --output /tmp/breeze_cli_check
```

自动测试使用临时数据库、测试 WAV、替代聊天与合成实现，覆盖真实 HTTP 聊天协议、档案持久化、UUID 独立性、非法上传、未知标识、路径越界、并发拒绝、失败重试、截断提示和中断恢复。它们不证明真实模型音色相似度。

本机验证记录（2026-11-16，Apple M1 Pro / 16 GiB）：40 项自动测试通过；Chrome 验证了上传、新建会话、文字与语音展示、播放、WAV 下载、刷新恢复，以及 390 像素窄屏布局。流式聊天使用本机模拟的 OpenAI 兼容 SSE 服务，验证了思考实时展开、正文增量显示、完成后自动折叠、刷新后可再次展开；语音仍由真实 Breeze 合成。以现有合成 WAV 为参考，真实 Breeze 生成了 4.4 秒、24 kHz、单声道 PCM16 音频，文件有效且非静音；原命令行实测生成 2.48 秒音频、16 个 PCM 块。**尚未接入真实聊天服务，未验证真人音色相似度或主观听感。** 测试档案与生成音频保留在已忽略的 `web-test-artifacts/real-breeze-data/`，与正式 `data/` 分开。

真实验收：配置聊天服务，上传真人参考 WAV 和正确原文，发一条短消息；确认先出现 AI 文字，再出现有效 WAV；试听、暂停、重播、下载，刷新后确认历史仍在。窄屏下从会话列表进入聊天，左上角返回列表。已有 `deployment_test_*.wav` 是合成输出，不作为真人音色复刻验收依据。

## API

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/health` | 模型状态、全局忙碌状态、聊天配置是否存在 |
| GET / POST | `/api/settings/llm` | 读取 / 保存大模型配置；POST：`base_url`、`model`、`api_key`（留空保留）、`clear_api_key`（布尔，默认 false）；响应仅含 `base_url`、`model`、`api_key_set`，不含密钥 |
| POST | `/api/transcriptions` | multipart `recording`：16kHz 单声道 PCM16 WAV，0.5–30 秒、最大 1 MiB；返回 `text`、`language`、`duration` 和 `timings.stt_ms` |
| GET / POST | `/api/voices` | 列表 / multipart：`name`、`transcript`、`recording` |
| GET | `/api/voices/{uuid}` | 单个声音；供实时页服务端校验 URL 中的 `voice_id`，未知 404、非 UUID 422 |
| GET / POST | `/api/conversations` | 列表 / JSON：`voice_id` |
| GET / POST | `/api/conversations/{uuid}/messages` | 消息列表 / JSON：`content`；立即返回 assistant 消息作为任务 |
| GET | `/api/messages/{uuid}` | 轮询任务状态，文字成功后即返回文字 |
| GET | `/api/messages/{uuid}/events` | SSE 推送思考、正文与状态变化，任务结束后关闭 |
| POST | `/api/messages/{uuid}/retry` | 按失败阶段重试，复用任务 UUID |
| POST | `/api/messages/{uuid}/cancel` | 请求取消活动回复；模型退出前仍保持忙碌 |
| GET | `/api/audio/{uuid}/stream?attempt={attempt}` | 从头流式读取当前尝试的 PCM16 小端单声道音频；响应头 `X-Audio-Sample-Rate`、`X-Audio-Channels`、`X-Audio-Format` 描述格式 |
| GET | `/api/audio/{uuid}` | 完整生成的 WAV；`?download=true` 下载 |

消息状态为 `queued`、`thinking`、`replying`、`synthesizing`、`complete`、`text_failed` 或 `audio_failed`。消息行的 `reasoning` 字段保存模型思考，`content` 在流式生成中逐步增长。音频通过消息 UUID 定位，不接收文件路径。声音 UUID 和任务 UUID 不通用。

`/api/health` 的 `stt_status`、`stt_error` 和 `active_kind` 报告识别状态与占用类型。转写和聊天共享单个推理线程及任务锁，忙碌时返回 409；转写超时为 60 秒，底层模型退出前不会释放锁。`busy=false` 表示模型占用已释放（gate 不再锁定），不表示单线程 LLM I/O worker 已空闲；打断后旧 LLM 请求若仍未退出，新一轮 LLM 调用仍可能在该 worker 排队。`timings.first_audio_ms` 表示任务开始到首块音频的毫秒数。

尚未实现微调、按句并行合成、自动打断、账号或安装包。音色相似度需要真人录音试听验证，不承诺复刻效果。

## 后端任务控制（2026-11-16）

保持单模型、单推理线程、全局单任务。现有网页和命令行调用方式不变，新能力首先供 API 调用方使用。

### 幂等发送

发送消息可选携带 `Idempotency-Key: <UUID v4>`。键在同一会话内唯一，与规范化后的消息文字一起识别请求；同键同文字始终返回原任务当前状态（HTTP 202），包括任务正在执行、已失败、已完成或模型暂不可用时。同键不同文字返回 409，非法键返回 422。不同会话可复用同一键。幂等记录随消息保存，重启后有效；重复原请求不会重新执行失败任务，需调用原 `/retry` 接口。

不带请求头保持原行为。**当前网页不发送幂等键，因此暂时没有网络重发保护。** 不自动重试聊天请求；即使有幂等键，外部服务在断线或进程崩溃情况下仍可能计费，不能承诺外部调用恰好一次。

### 超时与取消

- 可设置 `TASK_TIMEOUT_SECONDS`，默认 180 秒；非正数、NaN、无穷或非数字会阻止启动。总预算从每次任务投递开始计算，包括聊天、合成和发布，使用单调时钟。
- 聊天阶段上限 120 秒，受剩余总预算限制；连接超时 10 秒。请求支持取消，不自动重发。
- `POST /api/messages/{uuid}/cancel` 无需请求体。活动回复接受取消返回 202 和 `cancel_requested: true`；重复取消不额外执行。已结束回复返回 200，不撤回已经完成的音频；未知 UUID 返回 404，用户消息返回 409。
- 接受取消不等于已经停止：在模型开始前、PCM 块之间和发布前检查。工作线程实际退出及清理之前，`busy` 仍为 true，继续拒绝新任务。**无法强制打断卡死的 GPU 调用，必要时需重启。**
- 没有已确认文字时，取消或超时保存为 `text_failed`；有已确认文字时为 `audio_failed`，保留文字并清理未发布音频。调用原 `/retry` 可再次执行，使用相同消息 UUID、新的尝试次数和预算；已确认正文的音频重试不会再次调用聊天服务，实时回复若正文尚未确认则从文字阶段重试。
- 应用停服时拒绝新任务并请求取消，等待后台实际退出后关闭模型、释放目录锁；GPU 卡住仍可能导致等待。

### 查询、诊断和故障恢复

消息查询和列表保留原字段，新增：

| 字段 | 含义 |
|---|---|
| `request_key` | 可选幂等键，历史消息为 null |
| `attempt` | 执行尝试次数，未执行的历史消息为 0 |
| `started_at` / `finished_at` | 最近一次尝试的 UTC 起止时间 |
| `error_code` | 成功时 null；可为 `cancelled`、`timeout`、`dispatch_failed`、`interrupted`、`storage_error`、`provider_timeout`、`provider_http`、`text_generation_failed`、`audio_generation_failed` |
| `timings` | 最近一次尝试的 `llm_ms`、`tts_ms`、`publish_ms`、`total_ms` 和 `rtf`；未执行阶段为 null，历史记录为 `{}` |
| `text_finalized` | 正文是否已确认完整；历史 `complete`、标准模式的 `audio_failed` 和正文已确认的实时 `audio_failed` 为 1，实时未完成/中断/未确认正文为 0；只有确认正文进入后续上下文 |

`duration` 仍然是音频秒数；RTF 为合成耗时除以音频时长，不含聊天与发布。失败也记录已执行阶段耗时。日志使用现有 logging，包含 UUID、尝试次数、阶段、结果、错误码、耗时及白名单诊断（异常类型、HTTP 状态码、系统错误号和调用位置），不包含原始异常字符串、正文、录音原文、密钥或响应正文。数据库只保存最近一次尝试的指标，逐次诊断由日志保留；使用本文启动命令时会输出成功和失败的结构化记录；嵌入其他启动器时需由启动方开启 `voice_app` 的 INFO 日志。

`GET /api/health` 增加 `accepting_tasks` 和 `service_error`。线程投递失败会补偿保存失败状态，不额外新增用户消息；后台 Future 的异常会被观察。存储写入或任务状态保存失败时停止接收新任务，任务接口返回 503；检查磁盘/数据库后重启，勿直接循环重试。状态本身写不进去时，以 health 的故障提示和日志为准，不保证数据库已能显示失败。

升级时通过 SQLite 备份接口在数据目录生成 `before-tasks-<uuid>.sqlite3`，再事务添加字段与唯一索引。失败回滚并拒绝启动；已有历史与音频不删除，重复启动不重复迁移。不要删除旧备份，恢复前停服。启动将未完成任务标记为失败并设置 `interrupted`，不自动重放聊天请求。

后端验收：34 项自动测试通过，包括迁移回滚、重启与并发幂等、聊天阶段和总任务超时、取消与发布边界、阻塞推理不提前释放占用、线程投递/后台异常/磁盘与数据库故障、停服和日志脱敏。在本机用固定测试聊天回复和已有合成参考 WAV，真实 Breeze 正常生成 4.16 秒音频，首个 PCM 块后取消，再单独重试语音成功（未额外调用聊天服务）。测试数据在 `web-test-artifacts/backend-upgrade-*/`，不混入正式档案；不代表真人音色相似度验收。

本次原命令行实测：生成 3.04 秒 WAV、19 个 PCM 块，未修改命令行脚本行为。已有正式进程需要重启才会加载新代码及迁移；本次验收使用独立目录，没有改动正式历史。

## 自定义人物与跨会话记忆

界面顶部“人物”打开人物库；侧栏“创建人物”填写档案。没有声音时可在人物表单中添加参考录音，保存后返回表单继续填写。声音库仍通过 VOICES 进入。人物创建后自动建立会话，也可从人物库或会话选择框开始新的对话。

人物名称（最多60字）和声音必填；背景最多1500字，性格1000字，与你的关系500字，说话习惯1000字，示例对话最多3组，每条用户/人物发言最多500字。示例需要成对填写。人物是AI虚拟角色，参考声音不代表模型训练或专属权重。

每个新会话保存人物和声音的快照。聊天页“人物设定”显示当前会话快照；编辑后仅新会话使用新设定，旧会话与已经生成的音频不变。顶部选择框显示人物库最新名称，聊天标题及快照仍使用本会话创建时的名字。

### 自动记忆

- 同一人物的普通聊天和实时语音共享已确认的长期记忆。两个绑定同一声音的人物各自拥有独立记忆。人物记忆修改对旧会话的后续回复也生效。
- 每新增3轮符合条件的回复触发提炼。回复正常完成，或文字完整但语音失败时计数。取消、打断、转写失败和未完成的文字均跳过，重试音频最多计一次。
- 使用相同的 LLM 地址、模型和密钥，额外发送非流式请求。记忆、摘要和人物建议共用一个后台线程，只处理网络与数据库，不调用 Breeze，不加载新本地模型。待处理状态保存在 SQLite，每个实体最多一份，各类工作轮流执行；聊天不等待整理完成。
- 每批最多20轮未处理对话，超时10秒。提炼请求只包含本批用户原文（语音使用最终转写文本）和已确认记忆。模型结果需要通过格式及批内来源校验，候选与游标在同一事务保存。
- 「待确认」显示来源、时间及同主题旧值和拟更新值。可以直接确认、编辑后确认或驳回。若旧值在审阅期间发生变化，需先重新审阅。候选仅供面板审阅，聊天请求使用「已记住」中的事实。用户明确纠正旧事实时，聊天提示词要求本轮采用新说法。
- 「已记住」可手动添加、修改或删除。每个人物最多30条，主题最多60字，内容最多200字。删除后，下一轮不再从长期记忆读取该条，原始聊天仍可在会话里查看。同主题待确认更新随事实删除。若提炼期间已确认记忆发生变更，该批候选丢弃并推进游标，防止恢复刚删除的内容。
- 提炼失败保留游标，面板显示错误，聊天继续运行。少于3轮或失败后可手动提炼。没有待处理内容时跳过模型调用。已完成批次在重启后不会重复处理。

**隐私与费用：聊天文字、人物设定、示例和相关记忆会发送到配置的 LLM 服务。** 录音、Breeze语音合成及SQLite数据留在本机。自动提炼会增加LLM费用；无需人物功能时，旧声音会话不会进行记忆提炼。

### 人物 API

| 方法 | 路径 | 请求 / 行为 |
|---|---|---|
| GET / POST | `/api/characters` | 人物列表 / 创建档案 |
| GET / PUT | `/api/characters/{uuid}` | 详情 / 完整替换档案，旧会话快照不变 |
| POST | `/api/conversations` | JSON中 `character_id` 与旧 `voice_id` 二选一 |
| GET | `/api/characters/{uuid}/memories` | `items`、`status`、`error`、`version`、全局记忆任务 `busy` |
| POST | `/api/characters/{uuid}/memories` | JSON：`key`、`content`，添加手动记忆 |
| PUT / DELETE | `/api/characters/{uuid}/memories/{memory_uuid}` | 修改（同上JSON）/ 删除，限本人物的记忆 |
| POST | `/api/characters/{uuid}/memories/refresh` | 手动提炼，返回202；其他提炼占用时409，未配置时503 |
| GET | `/api/characters/{uuid}/memory-candidates` | 待确认候选、旧值、来源及审阅令牌 |
| POST | `/api/characters/{uuid}/memory-candidates/{candidate_uuid}` | `action` 可为 `approve`、`reject` 或 `review`。确认需提交 `review_token`，可附 `content`，审阅过期返回409 |

人物JSON字段：`name`、`voice_id`、`background`、`personality`、`relationship`、`speech_style`、`examples`（`[{"user":"...","assistant":"..."}]`）。人物和声音使用不同UUID。会话列表保留旧字段，新增 `character_id` 和JSON字符串 `character_snapshot`；旧会话两者为空。

SQLite 迁移分别在增加人物和候选表前生成 `before-characters-<uuid>.sqlite3` 和 `before-memory-review-<uuid>.sqlite3`。已有记忆作为已确认事实继续生效，保留来源消息。停服时取消提炼并等待其退出，再释放项目资源。

## 会话摘要与人物成长

聊天顶部「会话摘要」适用于文字、实时和纯声音会话。首次累计 20 组有效问答时，后台整理最早 10 组，此后每多出 10 组更新。摘要区分用户陈述、人物建议和待继续讨论的问题，最多 12 条、每条 160 字、总计 1200 字。每次最多整理 20 组，输入上限 12000 字符，按完整问答分批追赶。单组超出预算会显示失败并保留上一版本。

摘要面板可查看原文及时间、编辑条目、删除条目、清空和恢复历史版本。保存带版本基线，冲突时保留草稿并要求重新读取。清空后暂停自动重建，点击「重新整理历史」可恢复；清空摘要保留原始聊天。来源正文、回复终态或尝试次数变更后，旧摘要停止注入。删除带来源的长期记忆时，相关摘要条目和未审批人物建议也会失效，排除集阻止摘要后台任务或回退重新带入该来源。原始消息仍在聊天记录中。

人物库或当前会话的「交流风格与版本」显示待审批建议和历史。新增至少 20 轮有效互动、覆盖至少两个会话后，尝试整理用户反复明确表达的交流方式反馈。每次最多一条建议，只能修改「性格」或「说话习惯」，至少引用两个会话中的不同用户消息；一次抱怨、引用台词和人物自述不应作为依据。模型是否正确识别语义仍需用户审阅。首个会话很长时，选取跨入下一会话边界的最近 20 轮作为有界样本，原始聊天保留。

建议只在确认后生效，可编辑后确认、驳回或在版本冲突后重新审阅。确认、手动编辑和回退均追加完整人物版本，审批依据继续出现在版本历史。新会话记录 `character_revision_id` 并使用新设定；已有会话保留原快照。人物设定面板可用最新版本另开会话。此功能改变后续提示词中的人物档案，不在线更新模型权重。

| 方法 | 路径 | 请求 / 行为 |
| --- | --- | --- |
| GET | `/api/conversations/{id}/summary` | 当前摘要、来源、覆盖数量、待整理数量及后台状态 |
| GET | `/api/conversations/{id}/summary/revisions?offset=0` | 每页 20 个版本，`next_offset` 为下一页或 null |
| POST | `/api/conversations/{id}/summary/refresh` | 手动排队，返回 202；重复请求合并 |
| PUT | `/api/conversations/{id}/summary` | `base_revision_id`、`items`；仅修改已有来源条目 |
| POST | `/api/conversations/{id}/summary/rollback` | `base_revision_id`、`target_revision_id` |
| DELETE | `/api/conversations/{id}/summary` | JSON：`base_revision_id`，首次可以为 null |
| GET | `/api/characters/{id}/profile-candidates` | 待审建议、原值/新值、原因、来源、审阅令牌及状态 |
| POST | `/api/characters/{id}/profile-candidates/refresh` | 达到轮次条件后排队，返回 202；待审建议处理前不再生成 |
| POST | `/api/characters/{id}/profile-candidates/{candidate_id}` | `action`：approve/reject/review；确认带 `review_token`、`base_revision_id`，可附 `new_value` |
| GET | `/api/characters/{id}/profile-revisions?offset=0` | 完整人物版本、变更原因及审批依据，每页 20 条 |
| POST | `/api/characters/{id}/profile-rollback` | 当前 `base_revision_id` 与历史 `target_revision_id`，追加回退版本 |

现有 `PUT /api/characters/{id}` 接受可选 `base_revision_id`；网页始终提交以检测并发，旧调用方省略时保持完整替换语义。跨人物/会话资源返回 404，格式校验返回 422，版本或来源冲突返回 409。后台失败保留有效资料和游标，手动重试；失败不影响聊天。

升级生成 `before-profile-history-*.sqlite3`、`before-conversation-summary-*.sqlite3`，事务回填当前人物版本。历史会话的 `character_revision_id` 保持 null，显示历史快照，避免误标为新档案。未完成后台任务重启后保留为中断状态，手动恢复。

2026-11-23 验证：历史库只读副本迁移保留 152 条消息、5 条记忆及来源、23 份会话快照，完整性与外键检查通过。桌面与 390px 浏览器完成摘要编辑/清空/回退、审批冲突保留草稿、成长确认、新旧快照及人物回退，无脚本错误和抽屉横向溢出。验收使用临时 SQLite 和替代推理，未改正式数据库；真实 LLM 的摘要准确性、风格建议质量和长时间使用效果仍需验收。同日自动化：248 项后端测试通过（1 项跨机硬件测试按需跳过），5 套 Node 测试通过，覆盖摘要版本与来源校验、预算追赶、迁移恢复、人物建议与版本、VAD 校准、播放暂停续播和插话状态；真实硬件听感与延迟按 [人工验收](MANUAL_TESTS.md) 记录。

### 实时语音的停顿与插话

实时页增加「正常停顿」（700 ms）和「慢速表达」（1100 ms），参数在下一段录音开始时应用。开始监听会做约 1 秒噪声校准，检测到用户起声就提前停止校准。播放时疑似起声先暂停播放器，保留 SSE 和流式音频；短噪声结束后从原位置恢复。持续约 350 ms 人声或 2 秒无法裁决时，才停止播放、取消对应 `attempt` 并提交新发言。独立录音上下文继续保留用户声音。

取消可带 `/api/messages/{id}/cancel?attempt=N`，过期尝试返回 409。播放器暂停期间缓冲受背压限制，流结束也等待恢复后播完才结束。页面结束、声音切换和过期回调均不能恢复已废弃实例。

当前为能量与时长启发式，**未启用中文语义轮次模型**。短促的「停」和无接管意图的「嗯」仍无法可靠区分，短声音可能恢复播放；可用「停止播放」按钮立即停播。`window.__realtime.state.metrics` 保留最近 100 个浏览器控制事件，用于定位暂停、恢复及端点时刻，不等同于扬声器出声或端到端首音延迟。中文录音标注、回声/外放场景及 P95 回环测量按 [人工验收](MANUAL_TESTS.md) 执行后，才能判断是否接入语义模型。

上一轮人物与记忆功能验收：54项自动测试覆盖旧接口、SSE、取消、迁移与回滚，以及人物快照、记忆隔离、提炼频率、版本竞争、无效来源、容量、超时和停服。Chrome验证人物创建、录音绑定、消息与语音展示、记忆提炼与修正、刷新恢复、人物编辑及旧快照不变、400像素窄屏。浏览器使用明确的测试回复与替代合成，正式LLM的人物一致性、自然程度与语义记忆准确性仍待验收。所有测试使用独立目录，不修改正式历史，结束后停止服务。

同一轮额外用真实 Breeze 对两个人物分别合成，各生成4.72秒有效24kHz音频，确认人物上下文与声音绑定可完成链路。文字为固定测试回复，不能据此证明真实LLM的人物风格；验收进程已退出释放模型。

## LLM 简短口语约束

所有人物和仅声音会话都使用统一的系统规则：每次1–3句，通常30–80个汉字，不得超过100字，只写一个自然段；禁止分点、分层、编号、小标题及总结式长文。即使用户要求详细解释，也只讲最重要的一点，通过后续多轮继续交流，不放宽篇幅。旧会话后续回复同样生效，不修改既有人物设定或历史记录。

流式和非流式聊天统一使用 `CHAT_MAX_TOKENS=160` 输出预算；记忆提炼仍单独使用2000 token预算。约束在LLM请求中生效，应用不事后删句、截短或折叠回答，也不提供放宽长度的“展开聊聊”入口。模型返回的文字完整保存、显示并交给语音合成；思考面板独立保留。

提示词的字数要求不是服务端硬保证，token也不等于汉字数或音频秒数。若提供方忽略参数或提示，仍可能返回长回复；应用不伪装为短回答。提供方报告输出上限或Breeze达到750帧（约60秒）时，沿用原有明确提示。真实服务是否遵守约束需要实测，不保证任何语速下都在60秒内。

消息气泡、紧凑语音卡片和安全的历史加粗显示保留；一条回复渲染为单张卡片，所有段落完整显示。界面使用统一设计令牌，桌面与窄屏列表/聊天视图共用黑色主题。检查命令：`node --check web/app.js`、`node tests/test_reply_ui.cjs`、`.venv/bin/python -m unittest discover -s tests -p "test_voice_app.py" -q`、`.venv/bin/python -m unittest discover -s tests -p "test_characters.py" -q`。

## Mac 与 Ubuntu 的训练边界

Mac `main` 只提供聊天、声音、人物、录音播放和实时语音这些交互能力。`voice_app.py` 不注册训练 API，也不挂载训练页面，因此 `/training`、`/training/` 和 `/api/training/*` 均返回 404。Mac 仍保留已发布声音加载 adapter 所需的后端代码，但不在本机启动 LoRA 训练。

训练控制和 CUDA 执行属于 [`ubuntu` 分支](https://github.com/zchengyu854/Sound/tree/ubuntu)。该分支提供独立控制台、串行任务队列、训练进度与日志、取消、重试、重启后的中断恢复以及结果 ZIP 下载。默认从 `127.0.0.1:8766` 启动：

```sh
uv run --python 3.12 --with-requirements requirements-ubuntu.txt \
  python ubuntu_web.py
```

控制台上传 multipart 字段 `task` 指向的冻结任务 ZIP，完成后从对应任务下载结果 ZIP。`TRAINING_WEB_HOST`、`TRAINING_WEB_PORT`、`TRAINING_DATA_DIR`、`BREEZE_BASE_DIR` 和 `TRAINING_MAX_UPLOAD_BYTES` 用于配置监听、任务数据、基座缓存和上传上限。

Mac 不提交或控制 Ubuntu 训练任务。Ubuntu 完成训练后，下载仅含 `result.json` 和 `adapter.safetensors` 的 `result.zip`，在 Mac 主页面的「声音」中导入训练声音。可绑定已有参考声音，也可选「无参考录音」。两种模式都需要兼容的 Breeze 基座，都会校验包结构、配方版本、基座型号、adapter 摘要和本地基座权重摘要。绑定模式会复制参考 WAV 与原文，无参考模式直接使用基座加 LoRA 按文本合成。两种模式各自创建独立声音档案，可分别用于普通和实时对话。

在首页「声音」→「说话语气」可为已有声音选择「轻柔克制」或「原始语气」。设置适用于该声音之后生成的普通和实时语音，已有音频保持原样。

当前没有跨机任务调度、远端训练控制和 A/B 工作台，也不需要这些能力来完成“Ubuntu 训练、Mac 使用产物”。现有旧训练声音仍按原 `model_id` 兼容加载。导入只证明训练产物已通过格式校验并能被本机模型加载，不代替主观音色验收。

## 带背景音乐素材：本地人声抽离（可选预处理）

Mac 主页面**不识别也不剥离背景音乐**。`separate_vocals.py` 是独立的本地 CLI：在现有 `.venv` 中运行，调用**另一个独立虚拟环境**里的 `demucs-mlx`，把带伴奏的 MP3 / MP4 抽成人声并留在本机。它不启动服务、不提供训练上传接口，也不改动主页面或数据库。

一次安装（只装到 `.venv-separator`，主环境 `.venv` 的 `mlx==0.32.2` / `mlx-audio==0.5.1` 保持不变）：

```sh
uv venv --python 3.12 .venv-separator
uv pip install --python .venv-separator/bin/python -r requirements-separator.txt
```

首次分离会从 Hugging Face 下载权重（约 160 MiB）到 `~/.cache/demucs-mlx`；需要离线时先在有网环境下载，再设置 `HF_HUB_OFFLINE=1`。`demucs-mlx` 要求 MLX 0.32.3 以上，因此不能装进主 `.venv`。

使用（在项目根目录运行；相对 `--separator-venv` 基于当前目录解析）：

```sh
.venv/bin/python separate_vocals.py /本地素材/带伴奏.mp3 -o web-test-artifacts/separated/
# 输出：带伴奏.vocals.wav（24 kHz 单声道 PCM16，试听用）
#      带伴奏.vocals.mp3（24 kHz 单声道 192 kbps，训练上传用）
#      带伴奏.vocals.json（时长、模型、质量提示）
```

默认模型 `htdemucs_ft`，只对 vocals 进行计算；`--model htdemucs` 会计算全部 stem 再取 vocals。`ffmpeg` / `ffprobe` 仍需本机可用；输入仍受训练素材的 200 MiB、60 分钟限制，输出 MP3 也受同一上传上限。工具缺失、媒体非法、超时、分离无结果或输出无效时返回错误码（例如 `separator_unavailable`、`separation_failed`），临时目录会清理。

**必须人工试听**：人声分离不保证干净，残留伴奏、混响、多人声或断词都会污染训练。脚本只标记 `review_required` 和 `single_speaker_unverified`，不会判断授权、说话人数或转写是否正确。确认只有一位说话者且人声清晰后，再交给训练侧的素材整理流程。当前 Mac 主页面不负责上传或审核，分离也不能替代真人音色相似度验收。

本部分验证命令：`.venv/bin/python -m py_compile separate_vocals.py`、`.venv/bin/python -m unittest discover -s tests -p "test_separate_vocals.py" -q`。测试用真实 ffmpeg 转码、替身模拟 demucs，不下载模型或触发真实分离。

## 实时语音（两阶段）

自动接话与开口打断仍保持 Mac 单进程、单模型执行线程与统一 gate；LLM 网络读取可与合成重叠，本地模型运算仍串行。

### 自动接话
首页顶部「实时语音」进入 **`/realtime/`**（`/realtime` 跳转），并带入当前会话的人物或声音。也可在实时页选择人物或声音；人物会话沿用人物设定和记忆。点击开始后请求麦克风并开始持续监听，刷新后需重新开始。开始监听会做约 1 秒噪声校准，检测到用户起声即提前结束校准。URL 中的人物或声音由服务端校验，无效时提示并改用可用选项。浏览器端 `web/vad.js` 用约 20 ms 帧、自适应阈值、约 150 ms 起始、约 200 ms 前置缓冲、最长 30 秒、短于 0.5 秒不提交；静音结束提供「正常停顿」700 ms 与「慢速表达」1100 ms 两档，参数在下一段录音开始时生效。音频采集经 `web/realtime-worklet.js` 以约 20 ms 帧送入。说完自动识别，空文本/低可信不发送；自动发送用 `Idempotency-Key`，网络失败时用同一键重试并核对历史。识别失败可用文字输入入口；点「结束对话」会停止麦克风并取消在途回复。

### 分句合成与打断
实时页发送的消息带 `mode: realtime`（默认 `standard`，持久化并用于重试；升级备份回滚）。`realtime_runtime.py` 提供增量断句、有界队列（≤4 段，满时反压等待、不丢句）与轮次控制：LLM 网络读取在独立 I/O 执行器，仅正文入分句器（思考不进语音）；中文句末标点/换行优先，无标点约 80 字在合理边界切分。模型线程逐句合成，PCM 顺序写入同一 live buffer 与临时 WAV，成功后原子发布完整 WAV；每段失败不重试。SSE 在文字仍增长时即可报告音频开始，实时页收到 `synthesizing` 便开始播放；音频流按当前任务/attempt/live buffer/mode 判断可读。分别记录 LLM 首正文、首句就绪、首 PCM、分句与总耗时。

播放时保留麦克风检测（建议耳机验收，浏览器回声消除开启）：疑似起声先暂停播放器，短噪声或附和结束后从实际位置恢复；持续约 350 ms 人声或 2 秒无法裁决才停播、带目标 `attempt` 请求取消、停 LLM 读取并清队列。页面随后轮询 `/api/health`，等旧任务真正释放 gate 再识别新一轮；`busy=false` 只代表 gate 释放，旧 LLM I/O 请求仍可能让下一轮 LLM 调用排队。播放中录音的最短长度为 150 ms，普通提问仍为 500 ms；只允许一段待处理录音、等待有界（约 12 秒后放弃并提示）。异步回调核对会话/消息/attempt/轮次，过期数据与过期恢复不进当前界面。实时主动打断产生新终态 `interrupted`：历史与数据库保留并标注，但下一轮上下文排除被打断的整轮配对；标准聊天的取消与重试规则保持兼容。逐句合成耗时写入 `timings.sentence_ms`（最多 20 条）与 `timings.sentences`；`timings.tts_ms` 只统计模型合成，不含等待下一句的网络时间，RTF 因此可比。

### 限制与验收
- 人声分离仍是独立工具，独立环境**不是**资源隔离；先完成分离再启动模型服务，避免与训练/推理争用 GPU。
- 训练效果、未训练文本试听与真人音色相似度在 Ubuntu 训练侧验收，不由有效 WAV、有限 loss 或 Mac 页面自动化测试代替。真实麦克风多轮对话、端到端首音、缓冲耗尽与停顿仍是 Mac 人工验收项。
- 性能记录要求：固定声音与配置，冷启动与预热后分开；短、中两档中文回复，每档预热后至少 5 次，报告原始值/中位数/最大值/样本量；端到端首音以浏览器实测为准，不用服务端首 PCM 代替。预热后 RTF < 1（争取约 20% 余量）是持续播放目标，非未测承诺；若训练后声音持续 RTF ≥ 1，保留自动接话与完整生成播放并如实记录，不靠加大缓冲冒充实时输出。

验证：`tests/test_realtime_runtime.py`（断句/队列/轮次）、`tests/test_vad.cjs`（校准、两档停顿、短噪声与最长录音）、`tests/test_audio_ui.cjs`（播放暂停/续播、缓冲背压、实例过期）、`tests/test_realtime_ui.cjs`（自动发送、幂等、SSE 早播、旧轮次隔离、结束回收、疑似/确认/忽略插话、过期 `attempt`）、`tests/test_voice_adapters.py`（Ubuntu 产物校验、导入回滚、原声音隔离、导入声音实际合成）、`tests/test_voice_app.py` 与 `tests/test_training_service.py`（分句合成、interrupted 终态、标准取消兼容、幂等模式校验、合成失败分类、训练产物校验和单声音校验）以及现有 Node 回归。

人工验收步骤、记录表与判定标准见 **`docs/MANUAL_TESTS.md`**；自动化覆盖范围也在其中列出，避免重复劳动。

## 性能测量与推进条件

`benchmark_voice.py` 在同一台机器上用**真实引擎**测合成速度，不调用聊天服务、不联网。冷启动与热态分开记录，输出原始值、中位数、最大值与样本量，并写入 `web-test-artifacts/benchmark-<时间>/`（`benchmark.json` + `benchmark.md`，含每次生成的 WAV，便于复查音频是否正常）：

```sh
# 用已发布声音（读取其参考录音与原文）
.venv/bin/python benchmark_voice.py --voice <声音UUID> --runs 6

# 指定深度模式并做数值校验
.venv/bin/python benchmark_voice.py --voice <声音UUID> --runs 6 --depth-mode compiled --verify

# 或用外部参考素材
.venv/bin/python benchmark_voice.py --reference /path/ref.wav --transcript '逐字原文' --runs 6
```

运行前应停止应用，避免两个进程同时占用 GPU；**测量期间不要跑训练或其它 GPU 任务**。本机出现过同一份代码两次测量相差约 2 倍，并发负载会直接污染结论（见下文“测量条件”）。`RTF = 合成耗时 / 音频时长`，热态中位数 ≥ 1 表示无法持续实时输出。

三个可调项：`--depth-mode`（`cached` 默认 / `compiled` / `native` 对照）、`--chunk-frames`（流式每块帧数）、`--adapter`（`auto` 默认：声音带训练版本时加载 adapter；`off` 只用基座）。`--verify` 用 `verify_depth()` 做不采样的 logits 相对误差校验。应用侧对应 `BREEZE_DEPTH_MODE`（默认 `cached`）与 `BREEZE_STREAMING_FRAMES`（默认库默认 4），非法值都回退默认。

**测量条件**：同一台机、同一声音、同一文本，安静环境下两次独立测量结果一致（短句 0.875 / 0.878，中句 0.826 / 0.824）。更早一次测得 1.57–1.63，当时 `pcm_chunks` 与当前一致（仅新增参数助手），最可能是同时段并发 GPU 负载所致，未能逐项证实；因此本节数字都标注条件，复测请在安静环境进行。以下历史报告的短句热态统计包含首轮冷启动，不能与修正后的热态统计直接比较。

本机实测（Apple M1 Pro / 16 GiB，`mlx==0.32.2`、`mlx-audio==0.5.1`，`cached` + 4 帧）：

| 配置 | 用例 | RTF 原始值 | 中位数 | 最大值 | 首块中位数 |
|---|---|---|---|---|---|
| 基座（5 次） | 短句 2.96 s | 0.936, 0.889, 0.875, 0.872, 0.870 | **0.875** | 0.936 | 451 ms |
| 基座（5 次） | 中句 9.52 s | 0.828, 0.829, 0.826, 0.826, 0.823 | **0.826** | 0.829 | 456 ms |
| 训练后 adapter（5 次） | 短句 2.96 s | 1.158, 0.965, 0.965, 0.966, 0.972 | **0.966** | 1.158 | 469 ms |
| 训练后 adapter（5 次） | 中句 9.52 s | 0.915, 0.916, 0.915, 0.912, 0.915 | **0.915** | 0.916 | 484 ms |

调参对比（基座，各 3 次，其余条件相同）：

| 变量 | 短句中位 | 中句中位 | 短句首块 | 中句首块 |
|---|---|---|---|---|
| `compiled` 深度 | 0.860 | 0.795 | 467 ms | 441 ms |
| `native`（关闭优化，对照） | 2.265 | 2.222 | 913 ms | 894 ms |
| 2 帧/块 | 1.644（抖动 1.135–1.924） | 0.919 | 682 ms | 551 ms |
| 8 帧/块 | 0.877 | 0.807 | 720 ms | 697 ms |

**结论（如实记录）**：

1. 当前热态中位 RTF **已 < 1**：基座 0.83–0.88，训练后 0.92–0.97，具备持续播放余量。但**不承诺严格实时**：训练后短句最大值 1.158，真实实时消息实测 `rtf` 1.006（两句、含固定开销），首个请求或极短回复仍可能欠载。
2. `FastDepth` 仍是主要收益来源：`native` 2.27/2.22 → `cached` 0.88/0.83，约 **2.6 倍**，首块从约 0.9 秒降到 0.45 秒。
3. `compiled` 仍只快 2–4%（0.860/0.795 对 0.875/0.826），属噪声级且首调多付编译开销；**默认保持 `cached`**。
4. 块大小：4 帧是平衡点（中句最快 0.824，短句首块 465 ms）；2 帧短句抖动大；8 帧吞吐略好但首块晚约 250 ms。默认 4 帧不变，需要更早出声可设 `BREEZE_STREAMING_FRAMES=2`。
5. 音频有效性：抽查 WAV 时长 2.96 / 9.52 秒、均非静音；`cached` 与 `compiled`、4 帧与 8 帧的平均/峰值音量完全一致（-20.8/-4.0 dB、-18.7/-1.2 dB），调参没有牺牲音频。`--verify` 最差相对误差 2.8e-2，接近 3% 门槛（BF16/4bit 矩阵-向量与前缀 GEMM 的固有差异），换量化或换机后若失败应停止使用该模式。

推进条件：实时性在中位意义上已达标，继续调参收益耗尽。若要让短句与首个请求也稳定 < 1，只能降低固定开销（更小模型或更高算力），而不是继续加缓冲。任何改动都要在同等条件下重测并保留 `benchmark.json`。产物：`web-test-artifacts/chunk-2|4|8/`、`chunk-4-r5/`、`final-adapter/`、`final-compiled/`、`final-native/`；更早一次的对照记录在 `bench-cached|bench-compiled|bench-native/`。

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js && node --check web/vad.js && node --check web/realtime-worklet.js \
  && node --check realtime_web/app.js
node tests/test_audio_ui.cjs && node tests/test_reply_ui.cjs && node tests/test_vad.cjs && node tests/test_realtime_ui.cjs
.venv/bin/python -m py_compile voice_app.py voice_adapters.py breezetts2_mac_fast.py \
  breeze_training.py media_processing.py training_store.py training_service.py \
  realtime_runtime.py separate_vocals.py
git diff --check
```
