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
- 最近 20 条有成功文字回复的历史消息会发给聊天服务，另附系统提示与当前问题。语音失败但文字成功的消息仍参与上下文。默认请求简短中文、最多 256 个输出 token；聊天服务报告截断或 Breeze 达到 750 声学帧上限时，消息中会明确提示。
- 参考录音、原文、声音名称、消息、状态、思考内容及生成 WAV 保存在项目 `data/`，可通过 `VOICE_DATA_DIR` 改为专用私有目录。目录已加入 `.gitignore`。刷新/重启保留历史，启动会将未完成任务标为失败，允许重试。备份或迁移时先停止应用，复制整个数据目录；首版不提供删除界面。
- 聊天文字会发送给你配置的大模型服务，密钥仅留在后端。参考录音不发送给聊天服务，Breeze 合成留在本机。首次模型下载需要联网，聊天服务有自己的隐私与计费规则。
- 页面字体来自 Google Fonts，背景图来自 Higgs 图片服务，首次加载需要联网；它们不接收聊天内容或录音。

## 验证

```sh
.venv/bin/python -m unittest -v test_voice_app test_characters test_audio_runtime
node --check web/app.js
node test_audio_ui.cjs
node test_reply_ui.cjs
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

- 同一人物共享长期记忆，不同人物即使绑定同一声音也互不共享。人物记忆修改对旧会话的后续回复也生效。
- 每新增3轮成功文字回复触发一次提炼。语音失败仍计数；重试语音或同一文字任务不会重复计数。未完成或失败的文字不参与提炼。
- 使用相同的 LLM 地址、模型和密钥，额外发送非流式请求；独立单线程只处理网络与数据库，不调用 Breeze，不加载新本地模型。最多一个提炼运行，无无限任务队列；其他待处理内容在后续聊天或手动更新时处理。
- 每次最多20轮未处理对话，超时10秒。仅发送用户原文及已有记忆，不发送AI回复或推理。提炼仅应保留明确陈述的事实、偏好、约定与纠正；结果关联来源用户消息，JSON与来源标识校验不通过则不保存。
- 记忆面板可查看自动/手动来源、更新时间及来源消息UUID，并可添加、修改、删除。每个人物最多30条，每条内容200字，主题60字；主题在同一人物内唯一。不静默淘汰；达到上限会提示整理，超出的本批候选不再自动回补，必要时手动补录。
- 自动结果可修正同主题的自动记忆；与手动记忆冲突时保留手动内容。冲突识别依赖提炼模型复用已有主题，因此不能保证识别所有语义冲突或杜绝错误事实，需人工检查。
- 手动修改或删除会递增版本；若提炼期间版本改变，丢弃整个在途批次并推进该批游标，避免旧结果恢复刚删内容。该批其他候选也不会回补，可手动录入。已处理历史不回扫；将来重新明确陈述的事实仍可形成新记忆。
- 提炼失败不影响当前文字和音频，面板显示错误。少于3轮或失败后可点“更新记忆”；没有待处理内容时不会调用模型。游标、事实和状态持久化，重启不重复处理已完成批次；中断提炼会显示失败，允许手动重试。

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

人物JSON字段：`name`、`voice_id`、`background`、`personality`、`relationship`、`speech_style`、`examples`（`[{"user":"...","assistant":"..."}]`）。人物和声音使用不同UUID。会话列表保留旧字段，新增 `character_id` 和JSON字符串 `character_snapshot`；旧会话两者为空。

迁移前使用SQLite备份接口生成 `before-characters-<uuid>.sqlite3`，事务增加人物、记忆、已处理轮次及会话快照。迁移失败回滚，保留历史和音频；不会自动生成任何人物或虚构记忆。停服时取消提炼并等待其退出，再释放项目资源。

本次验收：54项自动测试覆盖旧接口、SSE、取消、迁移与回滚，以及人物快照、记忆隔离、提炼频率、版本竞争、无效来源、容量、超时和停服。Chrome验证人物创建、录音绑定、消息与语音展示、记忆提炼与修正、刷新恢复、人物编辑及旧快照不变、400像素窄屏。浏览器使用明确的测试回复与替代合成，正式LLM的人物一致性、自然程度与语义记忆准确性仍待验收。所有测试使用独立目录，不修改正式历史，结束后停止服务。

本次额外用真实 Breeze 对两个人物分别合成，各生成4.72秒有效24kHz音频，确认人物上下文与声音绑定可完成链路。文字为固定测试回复，不能据此证明真实LLM的人物风格；验收进程已退出释放模型。

## LLM 简短口语约束

所有人物和仅声音会话都使用统一的系统规则：每次1–3句，通常30–80个汉字，不得超过100字，只写一个自然段；禁止分点、分层、编号、小标题及总结式长文。即使用户要求详细解释，也只讲最重要的一点，通过后续多轮继续交流，不放宽篇幅。旧会话后续回复同样生效，不修改既有人物设定或历史记录。

流式和非流式聊天统一使用 `CHAT_MAX_TOKENS=160` 输出预算；记忆提炼仍单独使用2000 token预算。约束在LLM请求中生效，应用不事后删句、截短或折叠回答，也不提供放宽长度的“展开聊聊”入口。模型返回的文字完整保存、显示并交给语音合成；思考面板独立保留。

提示词的字数要求不是服务端硬保证，token也不等于汉字数或音频秒数。若提供方忽略参数或提示，仍可能返回长回复；应用不伪装为短回答。提供方报告输出上限或Breeze达到750帧（约60秒）时，沿用原有明确提示。真实服务是否遵守约束需要实测，不保证任何语速下都在60秒内。

消息气泡、紧凑语音卡片和安全的历史加粗显示保留；一条回复渲染为单张卡片，所有段落完整显示。界面使用统一设计令牌，桌面与窄屏列表/聊天视图共用黑色主题。检查命令：`node --check web/app.js`、`node test_reply_ui.cjs`、`.venv/bin/python -m unittest -q test_voice_app test_characters`。

## 角色声音训练工作台与 Ubuntu 训练

训练后端 API 另有独立可视化工作台：启动或重启应用后直接访问 <http://127.0.0.1:8000/training/>。它不嵌入聊天首页，也不添加首页导航入口；原有黑色聊天页面和 CLI 用法不变。Mac 需要本机 `ffmpeg`、`ffprobe`（可用 `brew install ffmpeg` 安装）及固定版本 MLX 依赖。Ubuntu 使用独立的 CUDA 命令行训练。只处理本人或已获授权的单一目标说话者素材；不含自动说话人分离、背景音乐分离或多人混合训练。

工作台导航分为“导入素材”“试听与校对”“训练与任务”“声音对照”。选择素材集后，可在各页之间切换；未保存的片段原文会保留，切换素材集或刷新前仍需保存。“声音对照”页可直接选择已完成的训练任务。

### 声音对照

素材处理完成后，工作台会排队分析当前素材；被排除的片段不参与，尚待审核的片段会参与。审核状态或原文变化后，当前结果会标为过期，可点击重新分析。训练完成后会分析该任务的冻结素材和两条原有试听；在任务记录中选择历史完成任务，可以手动补算或重试。分析结果单独保存在 `data/training/` 的素材集及任务目录，不修改训练快照、试听确认或发布记录。

- 指标分成独立条目返回和展示，各自带状态、数值、原因、参考数量和有效样本数：**参考片段一致性**（至少两条有效参考，只有一条时显示“需要至少两条参考片段”，不是系统故障）、**生成音色相似度**（一条参考即可，逐条显示，部分成功会写“已计算 1/2”）、**音频质量**（响度、低能量帧、削波，声纹模型失败时仍可查看）以及**训练前后声纹对照**（仅对具备 A/B 试听、且两侧都在同一参考集合上完整算出的任务显示差值）。
- 固定选择最多 8 条足够长、非静音的参考：训练任务优先使用冻结快照的留出验证片段（按来源划分标为“按来源留出验证”，同一录音内按时间划分标为“同录音留出验证”）；没有独立验证片段时使用训练素材，页面会明确标注。不会为了凑够两条而复制同一片段或混入训练参考冒充验证。
- 指数是声纹余弦相似度限制在 0–1 后乘以 100 的特征指数，不是真人相似概率、克隆准确率或发音评分。页面固定显示这条说明；请逐条试听，判断音色、发音、情绪、自然度及失真。
- 每条评估记录保存评估版本、声纹模型 revision、参考音频摘要、试听文本、推理参数（帧上限、随机种子、采样参数、流式间隔）和样本数；缺少这些字段的旧记录标为“旧算法结果（未记录评估版本）”。比较条件标识只在素材、参考、文本、模型、算法版本、合成参考与推理参数一致时才相同，历史曲线只连接相同条件的记录。
- 质量对照对 24 kHz PCM 音频计算均方根整体响度（dBFS）、20 毫秒低于 −40 dBFS 的低能量帧占比，以及绝对幅度达到满量程 99.9% 的采样占比。多段音频按时长加权；低能量不等于噪声或静音，这些数值不代表主观音质。
- 失败原因只返回白名单码与可操作的中文提示（模型下载失败、缓存不完整、没有合格参考、特征提取失败、生成音频缺失、部分成功等），不暴露私有路径或原始异常。首次分析需要联网下载 `marksverdhei/Qwen3-Voice-Embedding-12Hz-1.7B`，之后复用本机缓存；不自动删除或重下模型，失败后可显式重试。音色分析与训练、语音合成共享模型线程与资源锁。

### 试听参考与片段质量

- 训练、试听、发布和应用推理共用同一个合成参考：新任务创建快照时，从**训练子集**的已确认片段中按质量自动选择（无低音量/削波/短片段提示、有效语音更长者优先），并把 `reference_segment_id` 与选择来源写入快照。留出验证片段只作为评估目标，不会被选为合成参考。
- 可在“试听与校对”页把任一已确认片段设为试听参考（`PATCH /datasets/{id}/reference`）。排除该片段会自动清除选择；没有人工选择时回到质量自动选择。旧任务沿用原快照的参考（历史上是数组第一项），要更换请新建训练/验收版本，已发布声音不变。
- 切片按有声区间与持续静音边界划分，并在 300 毫秒以上时修剪首尾低能量，保留约 200 毫秒缓冲、单边最多 1 秒，不拼接删除句中停顿，源文件时间坐标随之调整。每片保存有声窗口占比、首尾低能量时长、有效语音时长、峰值与削波计数，供审核查看。
- 审核提示新增“只有 1 个字”“有声占比偏低”“已修剪首尾停顿”等条目。这些只是提醒，不自动通过；低音量不直接判为无语音。


### 调用顺序

所有接口仍受 localhost 和同源检查保护；从本地客户端调用，例如：

```sh
curl http://127.0.0.1:8000/api/training/capabilities
curl -X POST http://127.0.0.1:8000/api/training/datasets \
  -H 'Content-Type: application/json' \
  -d '{"name":"角色一","speaker_confirmed":true}'
# 将返回的 id 赋值为 DATASET；每个素材调用一次，可反复添加。
curl -X POST "http://127.0.0.1:8000/api/training/datasets/$DATASET/sources" \
  -F 'source=@/本地素材/voice.mp3'
curl -X POST "http://127.0.0.1:8000/api/training/datasets/$DATASET/prepare"
curl "http://127.0.0.1:8000/api/training/datasets/$DATASET/segments?offset=0&limit=50"
# 逐片试听、核对转写；SEGMENT 为返回的片段 id。
curl "http://127.0.0.1:8000/api/training/segments/$SEGMENT/audio" -o segment.wav
curl -X PATCH "http://127.0.0.1:8000/api/training/segments/$SEGMENT" \
  -H 'Content-Type: application/json' \
  -d '{"transcript":"逐字核对后的原文。","review_status":"approved"}'
# 创建时未确认单一说话者，可在审核后确认。
curl -X PATCH "http://127.0.0.1:8000/api/training/datasets/$DATASET" \
  -H 'Content-Type: application/json' -d '{"speaker_confirmed":true}'
curl -X POST "http://127.0.0.1:8000/api/training/datasets/$DATASET/jobs" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen | tr A-Z a-z)" \
  -d '{"rank":4,"steps":20,"learning_rate":0.0001,"accumulation_steps":1,"checkpoint_every":5,"seed":42}'
# JOB 为 prepare 或 train 返回的任务 id。
curl "http://127.0.0.1:8000/api/training/jobs/$JOB"
curl -X POST "http://127.0.0.1:8000/api/training/jobs/$JOB/cancel"
curl -X POST "http://127.0.0.1:8000/api/training/jobs/$JOB/retry" \
  -H 'Content-Type: application/json' -d '{"resume":true}'
# 训练完成后先下载并试听两条固定验收样本；JOB 为训练任务 id。
curl "http://127.0.0.1:8000/api/training/jobs/$JOB/preview/1" -o preview-1.wav
curl "http://127.0.0.1:8000/api/training/jobs/$JOB/preview/2" -o preview-2.wav
# 逐条确认试听后才允许发布；note 可省略。
curl -X POST "http://127.0.0.1:8000/api/training/jobs/$JOB/preview-confirm" \
  -H 'Content-Type: application/json' -d '{"confirmed":true,"note":"音色接近"}'
# 只有已确认试听的 completed 训练产物能发布；返回普通声音 API 可用的新声音 UUID。
curl -X POST "http://127.0.0.1:8000/api/training/jobs/$JOB/publish" \
  -H 'Content-Type: application/json' -d '{"name":"角色一 · 训练版本1"}'
```

接口总览：

| 方法 | `/api/training` 后路径 | 说明 |
|---|---|---|
| GET | `/capabilities` | 工具、固定版本兼容性、硬件内存、当前可用性和任务 |
| GET / POST | `/datasets` | 列表 / 创建数据集 |
| PATCH | `/datasets/{id}` | 更新单一目标说话者确认 |
| GET / POST | `/datasets/{id}/metrics` | 只读当前分析结果 / 排队重算；审核变化后标记过期 |
| GET | `/datasets/{id}/metrics/history` | 已完成训练任务及保存的历史分析；只读取、不重复计算 |
| GET / POST | `/jobs/{id}/metrics` | 只读冻结任务分析 / 手动补算或重试 |
| POST | `/datasets/{id}/sources` | 流式接收单个 MP3 / MP4，multipart 字段 `source` |
| POST | `/datasets/{id}/prepare` | 有界后台解码、切片和逐片转写 |
| GET | `/datasets/{id}/summary` | 训练前摘要：已确认片段数与有效时长、待审核/已排除数量、来源与未处理来源、是否具备独立验证、当前/人工选择的试听参考片段 |
| GET | `/datasets/{id}/segments` | `offset>=0`、`limit=1..200`，默认50 |
| GET | `/segments/{id}/audio` | WAV 试听，仅接受 UUID，不接受文件路径 |
| PATCH | `/segments/{id}` | `transcript`、`review_status`；审核为 `pending/approved/excluded` |
| PATCH | `/datasets/{id}/reference` | `{"segment_id": "<uuid>"}` 指定试听参考；`{}` 回到质量自动选择；只接受该素材集已确认片段 |
| POST | `/datasets/{id}/jobs` | 冻结审核数据快照，创建等待 Ubuntu 结果的任务，接受 UUID v4 幂等键 |
| GET | `/jobs/{id}/export` | 下载任务 ZIP，包含配置、冻结快照和已审核 WAV |
| POST | `/jobs/{id}/result` | multipart 字段 `result`，校验 Ubuntu 结果 ZIP 并排队生成本机 A/B 试听 |
| GET | `/jobs`、`/jobs/{id}` | 分页列表及状态、阶段、进度、检查点和错误码 |
| GET | `/jobs/{id}/preview/1`、`/preview/2` | 两条私有 WAV 试听样本；新训练任务第一条为“你好，好久不见” |
| POST | `/jobs/{id}/preview-confirm` | 提交 `confirmed:true` 与可选 `note`，确认后发布表单才可用 |
| POST | `/jobs/{id}/cancel`、`/retry` | 取消本机操作或撤销待导入任务 / 从原快照创建新尝试 |
| POST | `/jobs/{id}/publish` | 再次加载验证 adapter，创建独立声音版本；重复发布返回已有声音 |

### 约束、资源与恢复

- 每文件最多 200 MiB、实际媒体时长最多60分钟、每数据集累计最多2小时。FFprobe 检查实际容器、音轨和时长；MP4 选择默认音轨，没有默认时用第一条，并记录选择。解码固定为24 kHz、单声道 PCM16，网络协议禁用，最多180 MiB输出，最多300秒；FFprobe最多15秒。上传和解码检查剩余磁盘空间。参考 WAV 与识别接口仍分别保持10 MiB、1 MiB限制。
- 静音边界切成约3–12秒片段；持续语音强制12秒切分并提示检查断词，尾部不足3秒、低音量或可能削波会保留提示。逐片使用现有 Whisper，本地重采样到16 kHz。转写失败仍保留片段和可手动修正的提示，不自动确认。只有非空原文且审核通过的片段能训练，并要求明确确认单一说话者。
- Mac 的原素材、切片、导入的 adapter 和试听保存在 `data/training/<dataset_uuid>/`；发布版本在 `data/voice_models/<model_uuid>/`。这些目录不暴露为静态文件。Ubuntu 在结果 ZIP 旁的隐藏工作目录保存 token 和检查点。
- 从创建任务到 Ubuntu 训练结束，Mac 的任务状态为 `awaiting_result`，可以继续聊天合成。导入后，Mac A/B 试听与本地转写、聊天合成共用模型线程和 gate。试听等待正在执行的模型操作退出，结束后恢复 Breeze 和 Whisper。
- Mac 取消待导入任务后拒收其结果。停止正在运行的 Ubuntu 训练进程请在 Ubuntu 用 Ctrl+C。用同一个任务 ZIP 和输出目录再次运行命令，会从最后一个完整检查点继续。Mac 的“重新创建训练任务包”会增加尝试次数，旧结果无法导入。
- 改动原文只影响之后创建的任务。导出会重新校验快照和 WAV 摘要；导入会校验任务 ID、尝试次数、快照、配置和 adapter 摘要。Mac 生成 A/B 时还会校验实际基座权重摘要，并用对应 rank 加载 adapter。
- 默认 batch size固定1、rank4、学习率1e-4、20步、每5步保存、累积1次。rank可为1/2/4/8；步数1–1000、学习率1e-6–1e-3、累积1–8、保存间隔1–100。冻结基座、文本编码器和codec，只为主干及深度解码器的注意力q/v投影加LoRA。loss使用可微teacher forcing，提示无损失，首码本含EOS，深度码本不含EOS/padding；不是调用采样循环当作训练。
- 训练/验证按来源分离；只有单来源时保留至少3秒间隔，邻接片段不交叉泄漏。不足时快照明确返回 `validation_warning=insufficient_independent_validation`，不伪装有独立验证集。新训练任务完成还会记录数据覆盖摘要（完整轮数、未满一轮的样本数、合成参考片段），并使用训练后的 adapter 与快照记录的合成参考生成两条试听；`preview/1` 固定为“你好，好久不见”，`preview/2` 从未参与训练的固定文本中选择。已有任务保留原有试听样本。有效音频只证明技术链路，不代表音色相似。
- 训练配方版本化为 `breeze-lora-recipe-2`：随机种子在 LoRA 初始化之前设置，并记录初始 adapter 摘要；每个 epoch 用“种子+轮次”生成可复现洗牌，按轮覆盖全部训练片段，采样随机数与模型随机数分开；检查点记录轮次与位置，恢复后取样顺序与未中断训练一致。
- 训练日志按优化步记录步号、微批次平均损失、累积梯度范数、学习率和所在轮次，最多保留 1000 条；不再只保存前 200 个无步号微步值。每个检查点在同一留出验证片段上计算验证损失并记录趋势，同时标出验证损失最低的步与最终步；最终步更大不自动代表声音更好，发布仍以未训练文本盲听为准。
- 验收试听与聊天合成共用同一套合成参数（同一 `synthesis_args`、帧上限 750、种子、CFG、流式间隔），每条文本独立重置种子，并记录是否达到帧上限。此前验收使用 `max_tokens=120` 且未重置种子，条件与真实使用不一致。
  - 排查效果时先用相同素材逐字校对，再以相同参数对照 20、50、100 步；网页默认20步只是短跑。数据覆盖不足两轮时界面会提示补清晰、单人素材。步数和时长不是效果保证。
- 发布除基座、adapter、数据快照校验外，还要求验证记录中存在与当前 adapter、两条试听音频及文本哈希匹配的确认；未确认返回409“请先试听两条验收样本并确认后再发布”，确认记录重复提交保持幂等。发布创建新的声音 UUID，关联独立 adapter 和已审核参考片段原文；可通过原人物 `voice_id` 绑定。旧会话保留原声音，修改人物仅影响新会话；切回普通声音重新加载原基座，避免串用adapter。加载 adapter 和有效 WAV 均不代表音色已达要求。

### 拆分前的 Mac 本地训练实测与回归

Apple M1 Pro / 16 GiB、`mlx==0.32.2`、`mlx-audio==0.5.1`、本地缓存的4bit Breeze：独立系统语音11帧最小验证完成1步，loss约6.32186、梯度范数约0.48095；LoRA更新、基座原始字节摘要不变、保存后新基座重新加载及有效WAV均通过。全程约16.37秒，MLX峰值分配约3.47 GiB。

服务级另用4秒独立MP3验收：上传、处理、审核、首步检查点后取消、显式恢复至第2步、合成验证、发布声音、adapter与基座切换均通过；约36.56秒、MLX峰值分配约5.75 GiB。此服务级测试以确定性替身代替Whisper，不验证真实识别准确度。峰值是MLX分配量，不是整个系统或进程RSS，也不代表长片段/长文本训练上限；初次训练建议短片段、rank4、累积1，从少量步数验证。

```sh
.venv/bin/python -m unittest -q test_voice_app test_characters test_audio_runtime \
  test_breeze_training test_media_processing test_training_store test_training_service test_training_hardware
.venv/bin/python -m py_compile voice_app.py breeze_training.py media_processing.py training_store.py training_service.py
git diff --check
# opt-in：真实模型、独立临时目录，默认回归跳过；不启动正式服务。
RUN_BREEZE_TRAINING_HARDWARE=1 HF_HUB_OFFLINE=1 .venv/bin/python -m unittest -v test_training_hardware
# 仅最小训练验证：输入0.1–3秒24kHz单声道PCM16 WAV。
.venv/bin/python breeze_training.py --audio /你的独立素材.wav --text '对应原文' --output web-test-artifacts/my-training-probe
```

原始最小验证在 `web-test-artifacts/training-probe/report.json`，服务级结果在 `web-test-artifacts/training-service-hardware.json`，均被Git忽略。没有前端改动，未增加UI测试。真实目标说话者的音色相似度、长数据集训练速度与不同参数内存占用仍需实际素材验收；有效WAV与有限损失本身不能证明音色训练效果。

最终加固后复测：服务级4秒MP3链路增加了已发布声音在现有聊天接口中的真实合成验证，取消/恢复、基座原始字节摘要校验、adapter哈希校验、发布、聊天音频获取与切回基座全部通过。该次约30.79秒，MLX峰值分配6,172,736,168字节（约5.75 GiB）；LLM回复与Whisper为测试替身，Breeze/codec/训练/adapter/聊天合成为真实执行。最终常规回归100项中99项通过、1项硬件测试默认跳过；该硬件测试已单独显式运行通过。语法与 `git diff --check` 通过。

## 独立训练工作台

直接访问 **<http://127.0.0.1:8000/training/>**（`/training` 会跳转到此地址）。它是独立页面，静态文件放在 `training_web/`，不修改 `web/` 中的聊天前端，不添加聊天首页入口。仍复用同一进程的本机 API、数据库与模型资源，不需要额外端口、CORS 设置、前端构建或新增依赖；请勿通过双击 HTML 或另开静态服务器使用。

操作顺序：

1. 点击“新建”，为一位说话者创建素材集；可在左侧切换已有素材集。
2. 拖入或选择多个 MP3 / MP4，页面逐个上传并显示各文件结果。上传记录仅代表本次页面会话；重新打开页面后，已上传素材仍保存在后端。点击“处理素材并转写”，等待右侧任务变为“等待人工审核”。
3. 在中间区域按多行多列卡片试听片段、修改原文，然后点击“确认片段”或“排除此片段”。卡片列数随窗口宽度自动变化（约 3 列，窄屏 1 列），原文框获得焦点时会临时展开。“保存原文”会将该片段设回待审核，需再次确认。片段每页20条，切换页面或素材集前会提醒未保存修改。确认素材属于同一位说话者且已获授权。
4. 选择训练步数和参数，点击“创建 Ubuntu 训练任务”。在任务详情点击“下载训练任务包”，将 ZIP 传到 Ubuntu。任务包包含已经审核的录音及原文。
5. 在 Ubuntu 运行下面的训练命令。完成后把 `result.zip` 传回 Mac，在同一个任务里选择“导入 Ubuntu 结果包”。进度和显存峰值在 Ubuntu 终端显示，Mac 导入后生成试听。
6. 任务 `completed` 后先进入“试听验收”，逐条播放两条样本。新训练任务的第一条固定为“你好，好久不见”，第二条用于未训练文本对照；旧任务保留原有样本。查看数据覆盖提示，勾选检查项并可填写备注，点击“我已试听并确认”。未确认时发布表单保持隐藏，刷新后确认状态仍由服务端保留。
7. 确认后才显示发布表单。填写声音名称，点击“验证并发布声音”，获取新声音 UUID。此声音可用于聊天项目的人物设置；创建新会话再用其他句子试听，旧会话不受影响。

### Ubuntu CUDA 训练命令

把 `ubuntu_training.py`、`training_exchange.py`、`breeze_training.py` 与任务包放在 Ubuntu 的同一目录。已配置支持 CUDA 13 的 NVIDIA 驱动及 `uv` 后执行：

```sh
uv run --python 3.12 --with 'mlx[cuda13]==0.32.2' --with mlx-audio==0.5.1 \
  --with pydantic python ubuntu_training.py task.zip result.zip
```

把 `task.zip` 换成工作台下载的 ZIP 文件名。已有相同基座缓存时可加 `--base-dir /path/to/base`。结果包旁的任务工作目录保留训练检查点，可重新运行命令恢复。不同训练任务使用不同的输出文件名，避免覆盖已完成结果包。8 GB 显存可先用短片段、rank 4、1 步检查链路。

使用生成的系统语音实测通过 Mac 导出、RTX 5060 单步训练、Mac A/B 合成、确认发布和聊天合成。运行环境与结果记录在忽略目录的 `web-test-artifacts/training-exchange-hardware.json`。真实转写准确度、长片段显存上限和音色相似度需要单独验收。

输入中的转写不会被状态轮询覆盖；离开页面、切换素材集或片段分页时会提醒未保存的修改。训练提交在本页会话中保留幂等键，网络响应丢失后可先查看任务状态；请勿通过改配置或重复上传来代替检查。素材和任务的真实验证仍由服务端执行。

检查命令：`.venv/bin/python -m unittest -v test_voice_app test_characters test_audio_runtime test_breeze_training test_media_processing test_training_store test_training_service test_training_web test_training_hardware`、`node --check training_web/app.js`、`node test_training_ui.cjs`、`.venv/bin/python -m py_compile voice_app.py breeze_training.py media_processing.py training_store.py training_service.py`、`git diff --check`。发布门禁、试听哈希、训练覆盖摘要与前端确认状态分别由 `test_training_service`、`test_training_ui.cjs`、`test_training_web` 覆盖；前端测试使用隔离 DOM/API 替身，路由测试使用临时数据库与替身模型，不操作正式数据或触发真实训练。本轮未执行真实浏览器截图和真人音色相似度验收。

## 带背景音乐素材：本地人声抽离（可选预处理）

训练工作台本身**不识别也不剥离背景音乐**。`separate_vocals.py` 是训练之外的独立 CLI：在现有 `.venv` 中运行，调用**另一个独立虚拟环境**里的 `demucs-mlx`，把带伴奏的 MP3 / MP4 抽成人声后，再由你手动上传到训练工作台。它不启动服务、不占用训练单线程、不改上传接口或数据库。

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

**必须人工试听**：人声分离不保证干净，残留伴奏、混响、多人声或断词都会污染训练；脚本只标记 `review_required` 和 `single_speaker_unverified`，不会判断授权、说话人数或转写是否正确。确认只有一位说话者、人声清晰后，把 `.vocals.mp3` 当作普通素材上传到训练工作台，继续“处理素材并转写 → 逐片审核 → 训练 → 试听验收”流程。分离不能替代真人音色相似度验收。

本部分验证命令：`.venv/bin/python -m py_compile separate_vocals.py`、`.venv/bin/python -m unittest -q test_separate_vocals`。测试用真实 ffmpeg 转码、替身模拟 demucs，不下载模型或触发真实分离。

## 训练验收与实时语音（三阶段）

按“素材准备 → 审核 → Ubuntu 训练 → A/B 对照 → 发布 → 自动语音对话 → 开口打断”的主线补齐能力。仍保持单进程、单模型执行线程与统一 gate；LLM 网络读取可与合成重叠，本地模型运算仍串行。

### 阶段 1 · 训练闭环
- **WAV 导入**：训练素材现接受未压缩 PCM WAV（与 MP3/MP4 同样校验真实容器、编码、音轨、时长、大小与磁盘上限，统一解码为 24 kHz 单声道 PCM16）。带伴奏素材先运行 `separate_vocals.py` 抽成人声，试听后直接上传 `.vocals.wav`，无需再压 MP3。工作台导入区给出该指引。
- **对照训练**：`POST /api/training/jobs/{id}/variants`（UUID 幂等键，请求体 `{"steps": N}`）从已完成训练任务复制**同一不可变数据快照、划分与摘要**，仅改步数，从相同基座与种子重新训练；与“从检查点恢复”明确区分。`training_jobs` 新增可空 `parent_job_id`，升级时 SQLite 备份、失败回滚，旧任务为空。
- **A/B 验收**：每条新训练任务按相同文本、相同参考片段、相同推理配置与重置种子，顺序生成“基座 / 训练后”两组音频（不同时加载两套模型）。验收记录版本化保存基座/adapter/快照/参考摘要、文本、配置、种子、两组音频哈希、时长、合成耗时、首块耗时、RTF 与有界 loss 记录。工作台按文本成对展示两组播放控件与 RTF，每次只播一个；固定试听“你好，好久不见”，另一条从预置文本中选未出现在训练/参考原文的句子，无法选择时明确报错。确认后发布，确认绑定两组音频、文本、配置与 adapter 哈希，任一变化即失效；旧任务保持原试听与确认规则。
- **发布入口**：发布成功后显示“用此声音开始语音对话”，直达 `/realtime/?voice_id=...`；新声音为独立不可变 adapter 版本，切回普通声音不残留 adapter。
- **训练前摘要**：工作台在启动训练前显示已确认片段数与有效时长、待审核/已排除数量、来源与未处理来源，以及是否具备独立验证（口径与训练时划分一致，不承诺实际不存在的验证集）。

### 阶段 2 · 自动接话
新增独立页面 **`/realtime/`**（`/realtime` 跳转）。选择已发布声音或普通参考声音，点击开始后才请求麦克风并解锁 AudioContext，创建仅绑定 `voice_id` 的会话；切换声音新建会话，刷新不自动恢复。URL 中的 `voice_id` 由服务端 `GET /api/voices/{id}` 校验，无效时提示并改用可用声音。浏览器端 `web/vad.js` 用约 20 ms 帧、自适应阈值、约 150 ms 起始、约 700 ms 静音结束、约 200 ms 前置缓冲、最长 30 秒、短于 0.5 秒不提交；音频采集经 `web/realtime-worklet.js` 以约 20 ms 帧送入。说完自动识别，空文本/低可信不发送；自动发送用 `Idempotency-Key`，失败先查原任务不重复调用聊天服务。识别失败可用文字输入入口；点「结束对话」会取消在途回复，确保模型真正释放。

### 阶段 3 · 分句合成与打断
实时页发送的消息带 `mode: realtime`（默认 `standard`，持久化并用于重试；升级备份回滚）。`realtime_runtime.py` 提供增量断句、有界队列（≤4 段，满时反压等待、不丢句）与轮次控制：LLM 网络读取在独立 I/O 执行器，仅正文入分句器（思考不进语音）；中文句末标点/换行优先，无标点约 80 字在合理边界切分。模型线程逐句合成，PCM 顺序写入同一 live buffer 与临时 WAV，成功后原子发布完整 WAV；每段失败不重试。SSE 在文字仍增长时即可报告音频开始，实时页收到 `synthesizing` 便开始播放；音频流按当前任务/attempt/live buffer/mode 判断可读。分别记录 LLM 首正文、首句就绪、首 PCM、分句与总耗时。

播放时保留麦克风检测（建议耳机验收，浏览器回声消除开启）：检测到用户讲话即停播放器、请求取消、停 LLM 读取并清队列，页面随后轮询 `/api/health`，等旧任务真正释放 gate 再识别新一轮；`busy=false` 只代表 gate 释放，旧 LLM I/O 请求仍可能让下一轮 LLM 调用排队。只允许一段待处理录音、等待有界（约 12 秒后放弃并提示）。异步回调核对会话/消息/attempt/轮次，过期数据不进当前界面。实时主动打断产生新终态 `interrupted`：历史与数据库保留并标注，但下一轮上下文排除被打断的整轮配对；标准聊天的取消与重试规则保持兼容。逐句合成耗时写入 `timings.sentence_ms`（最多 20 条）与 `timings.sentences`；`timings.tts_ms` 只统计模型合成，不含等待下一句的网络时间，RTF 因此可比。

### 限制与验收
- 人声分离仍是独立工具，独立环境**不是**资源隔离；先完成分离再启动模型服务，避免与训练/推理争用 GPU。
- 未训练文本试听、真人音色相似度、发音/断句/失真、真实麦克风多轮对话、说话结束到首次实际播放的端到端延迟、缓冲耗尽与停顿，均为**人工/实测验收项**，不由有效 WAV、有限 loss 或自动化测试代替。
- 性能记录要求：固定声音与配置，冷启动与预热后分开；短、中两档中文回复，每档预热后至少 5 次，报告原始值/中位数/最大值/样本量；端到端首音以浏览器实测为准，不用服务端首 PCM 代替。预热后 RTF < 1（争取约 20% 余量）是持续播放目标，非未测承诺；若训练后声音持续 RTF ≥ 1，保留自动接话与完整生成播放并如实记录，不靠加大缓冲冒充实时输出。

本阶段验证：`test_realtime_runtime.py`（断句/队列/轮次）、`test_realtime_ui.cjs`（自动发送、幂等、SSE 早播、旧轮次隔离、结束回收、打断中止、播放中开口打断、URL 声音服务端校验）、`test_voice_app.py` 与 `test_training_service.py`（分句合成、interrupted 终态、标准取消兼容、幂等模式校验、合成失败分类、训练前摘要、单声音校验），以及原有全量回归均通过（156 项后端 + 5 套前端）。

人工验收步骤、记录表与判定标准见 **`MANUAL_TESTS.md`**；自动化覆盖范围也在其中列出，避免重复劳动。

## 性能测量与推进条件

`benchmark_voice.py` 在同一台机器上用**真实引擎**测合成速度，不调用聊天服务、不联网。冷启动与热态分开记录，输出原始值、中位数、最大值与样本量，并写入 `web-test-artifacts/benchmark-<时间>/`（`benchmark.json` + `benchmark.md`，含每次生成的 WAV，便于复查音频是否正常）：

```sh
# 用已发布声音（读取其参考录音与原文）
.venv/bin/python benchmark_voice.py --voice <声音UUID> --runs 5

# 指定深度模式并做数值校验
.venv/bin/python benchmark_voice.py --voice <声音UUID> --runs 5 --depth-mode compiled --verify

# 或用外部参考素材
.venv/bin/python benchmark_voice.py --reference /path/ref.wav --transcript '逐字原文' --runs 5
```

运行前应停止应用，避免两个进程同时占用 GPU；**测量期间不要跑训练或其它 GPU 任务**。本机出现过同一份代码两次测量相差约 2 倍，并发负载会直接污染结论（见下文“测量条件”）。`RTF = 合成耗时 / 音频时长`，热态中位数 ≥ 1 表示无法持续实时输出。

三个可调项：`--depth-mode`（`cached` 默认 / `compiled` / `native` 对照）、`--chunk-frames`（流式每块帧数）、`--adapter`（`auto` 默认：声音带训练版本时加载 adapter；`off` 只用基座）。`--verify` 用 `verify_depth()` 做不采样的 logits 相对误差校验。应用侧对应 `BREEZE_DEPTH_MODE`（默认 `cached`）与 `BREEZE_STREAMING_FRAMES`（默认库默认 4），非法值都回退默认。

**测量条件**：同一台机、同一声音、同一文本，安静环境下两次独立测量结果一致（短句 0.875 / 0.878，中句 0.826 / 0.824）。更早一次测得 1.57–1.63，当时 `pcm_chunks` 与当前一致（仅新增参数助手），最可能是同时段并发 GPU 负载所致，未能逐项证实；因此本节数字都标注条件，复测请在安静环境进行。

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
.venv/bin/python -m unittest -v test_voice_app test_characters test_audio_runtime \
  test_breeze_training test_media_processing test_training_store \
  test_training_service test_training_web test_separate_vocals test_realtime_runtime
node --check training_web/app.js && node --check web/app.js
node --check web/vad.js && node --check web/realtime-worklet.js && node --check realtime_web/app.js
node test_training_ui.cjs && node test_audio_ui.cjs && node test_reply_ui.cjs
node test_vad.cjs && node test_realtime_ui.cjs
.venv/bin/python -m py_compile voice_app.py breezetts2_mac_fast.py breeze_training.py \
  media_processing.py training_store.py training_service.py realtime_runtime.py separate_vocals.py
git diff --check
```
