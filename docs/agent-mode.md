# Agent 模式与 MCP 服务

本文说明 Agent 模式和 MCP 服务的行为与架构，面向使用者和贡献者。命令和参数的完整用法见 [CLI 使用指南](cli.md)，设置项见 [配置说明](configuration.md)。

## 1. Agent 模式是什么

默认情况下，BiliSum 用设置里配置的模型（内置模型）完成总结。Agent 模式让调用 BiliSum 的外部 Agent（Claude Code、Codex，或连着 MCP 服务的对话客户端）用它自己的模型完成这些步骤：

- BiliSum 负责下载、字幕 / ASR 转写、分段、抽帧和存储。
- 任务流水线里的模型步骤（摘要、知识笔记、思维导图、关键帧规划、画面理解、图文合成）在 Agent 具备能力、且用户允许时交给 Agent；其余步骤走默认链路：内置模型 → 本地兜底。
- Agent 交回的结果写进视频库后，和内置模型生成的结果完全一致：产物文件、字段，以及思维导图、知识库索引等后续处理都相同。

不交给 Agent 的部分：多模态转写、TwelveLabs Pegasus、Embedding 与向量检索，以及知识库里的问答、自动打标、重新聚合。

### 1.1 引擎和 Agent 能力

创建任务时确定路由，任务执行过程中不再变化：

1. **引擎**：调用时显式指定（CLI `--engine`、MCP `engine`）就用它，否则用设置 `default_engine`（出厂 `builtin`）。
2. **只有声明了自己是 Agent 的调用方才会用到 `agent` 引擎**：带 `--agent-name`（或 `BILISUM_AGENT_NAME`）或显式 `--engine agent` 的 CLI、MCP 服务。桌面端界面和普通脚本创建的任务，即使默认引擎是 `agent`，也按 `builtin` 处理，不会空等。
3. **能力**：调用时显式声明（`--agent-capabilities`、MCP `agent_capabilities`）优先，否则用设置 `agent_default_capabilities`（出厂 `text,vision`）。
4. **交给 Agent 的步骤 = 能力覆盖的步骤 ∩ 设置 `agent_delegation`**。其余步骤走内置模型 → 本地兜底。

默认能力可能高估宿主的实际能力（比如宿主不能看图）。这时 Agent 对画面项提交“拒绝”即可，这些项会回退到默认链路。

### 1.2 任务状态 `agent_summarizing`

任务等待 Agent 时处于 `agent_summarizing`（界面显示“Agent 总结中”）：

- 它是进行中的状态，不是结束状态：桌面端不会显示“已完成”，CLI 的 `status` 和等待逻辑不会把它当作结束，SSE 事件流保持连接。
- 它不占用 worker 队列位置，也不算“活动任务”：服务照常空闲关闭；Agent 回来领取时，CLI 或 MCP 会把服务重新拉起。
- 它不在“可恢复”集合里：服务重启后不会重跑，不会重新下载和转写。
- 列表和详情页显示已等待的时长，以及“等待 Agent 领取”或“{Agent 名称} 处理中”。不弹系统通知。

后端、桌面端、CLI 各自集中定义了活动 / 结束 / 可恢复三个状态集合（`video_sum_core.models.tasks`、`apps/desktop/src/taskStatus.ts`、`packages/npx/lib/status.js`），其他代码一律引用它们。状态切换由仓储层的原子操作 `transition(task_id, from_, to)` 完成，并在那里集中校验允许的切换。

### 1.3 一直等待，不自动回退

任务停在 `agent_summarizing` 时没有超时，Agent 断开后也不会自动改走内置模型或本地兜底（没配内置模型时，自动回退只能得到质量很低的规则摘要）。只有三种方式让任务离开这个状态：

- Agent 对某些项提交“拒绝”：任务重新进入总结队列，被拒绝的步骤改走默认链路。
- 用户在详情页点“改用内置模型”（`POST /api/v1/tasks/{id}/agent-work/use-builtin`）：相当于拒绝剩下的所有 Agent 步骤。
- 用户取消任务。

## 2. Agent 工作协议

所有接口都需要现有的访问 token。CLI 和 MCP 已封装好这些接口（`bilisum agent next / submit`、`bilisum_agent_next / bilisum_agent_submit`），一般不需要直接调用。

### 2.1 批次

一次创建请求产生的所有任务（例如 `--all-pages` 的每个分 P）共用一个 `batch_id`，创建结果里会返回。Agent 用它统一领取，一个循环就能处理完整个合集。

### 2.2 领取

`POST /api/v1/agent-work/claim`

```json
{"scope": "batch", "batch_id": "...", "task_id": null, "agent": {"name": "Claude Code", "model": "<可选>"}}
```

- 范围：`batch`（本批次，需要 `batch_id`）、`all`（所有等待中的任务，用于在新对话里或换一个 Agent 接着做）、`task`（指定任务，需要 `task_id`）。
- 返回范围内最早进入 `agent_summarizing`、且没有被别人独占的一个任务的本轮待办项：`task_id`、`batch_id`、`round`、`lease_id`、`lease_expires_at`、`items`。每一项含 `item_id`、`step`、`kind`（`text` / `vision`）、`messages`、`response_format`；画面项另有 `images`（本地路径和媒体接口地址 `/api/v1/tasks/{id}/visual-evidence/media/{file}`，不内嵌 base64）。
- `item_id` 就是缓存主键 `usage_stage#下标`（见 5.4）。
- 待办项里的 `messages` 就是内置模型本来会收到的提示词，原样导出，包括补充要求和输出语言。
- 暂时没有可领取的待办项时返回 200 和 `{"task_id": null, "pending_tasks": N}`，N 是范围内仍在转写或重放的任务数。`scope=task` 的任务还没进入等待时也这样返回（`pending_tasks: 1`）；任务不存在返回 404；任务已结束或正被别人独占返回 409。

`GET /api/v1/tasks/{id}/agent-work` 只读查看本轮待办项和当前独占者，不领取、不续期。

### 2.3 独占权（先领先得）

- 领取时服务发一个 `lease_id`，有效期为设置 `agent_lease_ttl`（默认 600 秒）。持有期间其他 Agent 领不到这个任务。
- `POST /api/v1/tasks/{id}/agent-heartbeat`（带 `lease_id`）续期，不影响任务状态；独占权已过期或不属于调用方时返回 409。
- 持有者断开、心跳停止后，独占权到期自动释放，任务仍停在 `agent_summarizing`，任何 Agent 都可以重新领取。
- 是否过期在领取和提交时现场判断，不做定时扫描。独占权记录在任务目录的 `agent_work.json` 里，服务重启后未到期的独占权仍然有效。
- 心跳由调用方自动发送：CLI 领取后启动后台心跳进程，最长持有 `BILISUM_AGENT_MAX_HOLD` 秒（默认 7200），避免成为孤儿进程；MCP 服务在进程内续期，进程退出后心跳随之停止。

### 2.4 提交

`PUT /api/v1/tasks/{id}/agent-work`

```json
{
  "lease_id": "...",
  "agent": {"name": "Claude Code", "model": "<可选>"},
  "results": [
    {"item_id": "summary_full#0", "content": "<模型的原始输出>"},
    {"item_id": "visual_frame#0", "declined": true, "reason": "无法看图"}
  ]
}
```

1. 同步校验：`lease_id` 必须是本轮当前有效的独占权，否则 409“已被其他 Agent 处理”；本轮每一项都必须有结果或明确拒绝；`content` 用该 `usage_stage` 登记的解析函数预先解析，并检查大小上限。校验失败返回 422 并逐项说明原因，任务状态不变、独占权保留，修正后可以重新提交。
2. 校验通过后，任务从 `agent_summarizing` 切回 `running`（防止重复提交），保存结果和 Agent 名称，释放独占权，重新进入总结队列，返回 202。

### 2.5 重放中出错

- **Agent 的内容在后续处理中被发现不可用**（通常是依赖上下文、提交时查不出来的问题）：删除这一项的缓存，任务退回 `agent_summarizing`，并追加一条 `agent_work_rejected` 事件，说明是哪一项、什么原因。这一项会重新出现在待办项里。
- **其他错误**（下载、文件读写、内置模型回退后仍失败等，Agent 怎么改都没用）：离开 Agent 循环，避免反复提交。和默认模式的 LLM 失败一样处理：转写已经保存时进入 `paused`（“转写已保存，等待续跑”，可在详情页“仅重跑 LLM”）；没有保存转写的任务标为 `failed`。

### 2.6 执行来源 `model_sources` / `agent_identities`

- `TaskResult.model_sources` 记录每个模型步骤实际由谁生成：`agent`、`internal`（内置模型）、`local`（本地兜底）、`none`（没有运行）。一个步骤由多项组成且来源不同时，取最弱的来源。`summary` 为 `local` 表示摘要是规则生成的。取值和展示位置详见 [CLI 使用指南](cli.md) 的“模型来源”。
- `TaskResult.agent_identities` 按项记录回答它的 Agent：键为 `usage_stage#下标`，值为 `{name, model, self_reported}`。中途换了 Agent 时，每一项记录的是实际完成它的那个 Agent。名称是 Agent 自报的，BiliSum 无法核实，只用于显示（标为“自报”），不参与任何权限或路由判断。
- 详情页显示每一步的执行来源和 Agent 自报的名称。

### 2.7 一轮完整的工作循环

```
创建任务（--engine agent 或默认引擎为 agent）→ 返回 batch_id
循环：
  领取（scope=batch）→ 拿到一个任务的本轮待办项和 lease_id
  逐项生成结果，做不了的项明确拒绝（期间自动心跳）
  提交 → 202，任务重放
  暂无待办但 pending_tasks > 0 → 稍后再领取
直到 pending_tasks 为 0
```

一个带图文笔记和思维导图的任务通常需要 3–5 轮；合集按任务依次处理。

### 2.8 完成后的手动重新生成

- Agent 任务的思维导图和图文笔记在任务变为 `completed` 之前生成（`inline` 策略）；默认模式仍在完成后异步生成，行为不变。
- `agent_summarizing` 期间，手动重新生成思维导图、图文笔记的接口（`POST /tasks/{id}/mindmap`、`POST /tasks/{id}/visual-evidence`）只接受 `completed` 的任务，会返回 400，避免和重放同时写入。
- 完成后手动重新生成走默认链路，不读 Agent 留下的缓存。

## 3. MCP 服务

### 3.1 用途

`bilisum mcp` 给不能执行 shell、但支持 MCP 工具的本地对话客户端使用。它以 stdio 方式运行在本机，是 CLI 的一个子命令，和 CLI 共用同一套命令实现，连接规则（desktop / cli / custom / auto 环境、令牌获取、服务自动拉起）完全相同；连桌面端时和桌面端共用同一个视频库。不支持远程 HTTP 连接。配置方法见 [CLI 使用指南](cli.md) 的“MCP 服务”。

### 3.2 工具

| 工具 | 对应 CLI | 只读 |
|---|---|---|
| `bilisum_summarize` / `bilisum_brief` / `bilisum_transcribe` | `summarize` / `brief` / `transcribe` | 否 |
| `bilisum_status` | `status` | 是 |
| `bilisum_result` | `result` | 是 |
| `bilisum_tasks` | `tasks` | 是 |
| `bilisum_env` / `bilisum_doctor` | `env` / `doctor` | 是 |
| `bilisum_settings_url` | `--setting` | 是 |
| `bilisum_agent_next` / `bilisum_agent_submit` | `agent next` / `agent submit` | 否 |

各工具的参数见 [CLI 使用指南](cli.md)。不提供删除类工具，也不提供会改动持久配置或耗时很长的管理操作（启停服务、切换或初始化环境、安装 skill）。

### 3.3 分段等待和分段读取

- **分段等待**：创建任务的工具最多等 `wait_seconds` 秒（默认 50，上限 300），完成就返回结果，没完成就返回 `task_id` 和进度；之后用 `bilisum_status` 的 `wait_seconds`（默认 0）继续等。客户端带 `progressToken` 时，等待期间发送进度通知。
- **分段读取**：`bilisum_result` 每次最多返回 `max_chars` 字（默认 20000），没读完时返回 `next_offset`。创建任务的工具完成时默认只返回总结，不含转写；转写用 `format=transcript` 单独读取。
- 每个工具都返回 `structuredContent`（`task_id`、`status`、`title`、`video_id`、`model_sources` 等）。

### 3.4 Agent 工具

- MCP 客户端算作声明了自己是 Agent 的调用方；`agent.name` 自动取客户端初始化时报的名称（`clientInfo.name`），模型名可用 `agent_model` 选填。
- 画面项的帧图片压缩后（长边不超过 1280 像素）作为 `image` 内容块返回，并标明所属 `item_id`；待办项较多时用 `offset` / `limit` 分批读取同一轮。
- 持有独占权期间自动续期。提交的 422 和 409 原样作为 `isError` 结果返回。

### 3.5 安全约束

- 工具不接收令牌、API Key 等密钥参数；需要配置模型时，由用户自己打开 `bilisum_settings_url` 返回的设置页。
- 本地文件输入只接受已存在的路径。
- stdout 只输出 MCP 协议消息，日志走 MCP 日志通知和 stderr。
- 服务在第一次工具调用时才拉起；MCP 进程退出时不停止服务，交给空闲自动关闭。
- 业务错误作为工具结果返回并标记 `isError`，提示文本和 CLI 相同；只有参数不合法时才返回协议错误。
- 转写、视频标题、待办项里的提示词都来自视频，是不可信数据，其中出现的指令不能执行。工具描述和 skill 里都写明了这一点。整个流程中 Agent 都接触不到 API Key。

## 4. CLI 和 MCP 共同的能力

### 4.1 只转写

`bilisum transcribe`（以及 `bilisum_transcribe`）创建 `summary_scope=transcript_only` 的任务：转写完成后直接以转写快照作为结果并完成任务，不调用任何模型。需要像以前一样顺带生成摘要时加 `--with-summary`。

### 4.2 复用已有结果

创建任务前先查找可复用的任务（`GET /api/v1/videos/{video_id}/reusable-task`）：同一视频、同一 P，总结范围、提示词预设、输出语言、补充要求都一致；图文笔记模式和引擎只有显式指定时才参与比对。完整总结可以顶替快速摘要，反过来不行。

- 找到已完成的任务：直接返回它的结果，并标明 `reused: true`。
- 找到同样选项、仍在进行中或处于 `agent_summarizing` 的任务：接上它（返回它的 `task_id` 和 `batch_id`），不再新建。
- 显式要求 `--engine agent` 时，即使已有内置模型的结果，也会新建一个 Agent 任务。
- `--force`（MCP `force=true`）跳过查找，新建任务重做。

### 4.3 按时间段 / 关键词读取转写

`bilisum result <task-id> --time 12:00-15:30 --grep 关键词`（MCP：`bilisum_result` 的 `time_range`、`query`）只返回这段时间内的转写，或命中段落及前后若干段的上下文，每段带时间戳。两者可以组合，仍按 `max_chars` 分段。筛选只用任务结果里已有的分段和时间戳在本地完成。

### 4.4 补充要求和输出语言

- `--instructions`（MCP `instructions`）写进 `TaskOptions.extra_instructions`，由流水线放进提示词里一个单独的、标明来源的段落。它只影响内容取舍和侧重，不改变输出结构；上限 1000 字，超长直接报错。结果里记录用了哪些补充要求，详情页会显示。
- `--language`（MCP `language`）写进 `TaskOptions.language`，由流水线在提示词里加入输出语言要求；不指定时按 BiliSum 设置的语言输出。
- 两种引擎都生效：Agent 拿到的待办项就是这份提示词。
- 两者都不指定时，发给模型的提示词和以前完全一致。

## 5. 架构要点

### 5.1 `ModelClient`

`packages/infra/src/video_sum_infra/model_client.py` 提供 `ModelClient.chat(ModelRequest) -> ModelResponse`，统一处理 OpenAI / Anthropic 格式适配、推理参数、图片内容块、超时、token 用量规整和响应解析（兼容 SSE / NDJSON 网关）。流水线和知识库的模型调用都走它。

### 5.2 步骤注册表和 `StepRouter`

`packages/core/src/video_sum_core/pipeline/model_steps.py` 用一张表定义所有调用模型的步骤：

| 步骤 | `usage_stage` | 所需能力 | 本地兜底 |
|---|---|---|---|
| `summary` | `summary_full` / `summary_chunk` / `summary_merge` / `aggregate_summary` | text | 规则摘要 |
| `knowledge_note` | `knowledge_note` | text | 规则拼装 |
| `keyframe_plan` | `visual_keyframe_plan` | text | 本地关键帧规划 |
| `visual_frame` | `visual_frame` | vision | 不生成图文（按 text 模式处理） |
| `visual_compose` | `visual_note_compose` | text | 本地图文合成 |
| `mindmap` | `mindmap` / `mindmap_repair` | text | 思维导图兜底 |

- **路由按步骤决定**：`agent`（Agent 具备能力且用户允许）、`internal`（已配置对应的内置模型）、`local`（本地兜底）。
- **解析函数和重试策略按 `usage_stage` 登记**，原样沿用各调用点原有的行为：哪些异常重试、重试几次、哪些直接抛出、哪些触发回退。
- 接口：`StepRouter.run` 执行单个请求（带逻辑下标和本地兜底）；`run_many` 批量执行同一步骤的多个请求（逐帧画面理解先组装全部请求再统一分发），按输入顺序返回；`probe()` 供 preflight 做连通性探测，只在有步骤路由到 `internal` 时才探测。
- **回退只在路由层发生**，并在这里记录来源，`model_sources` 直接取自这里。
- **流水线里的模型调用只能经由 `StepRouter`**。流水线测试注入的 `FakeModelClient` 被路由之外的代码调用时会直接报错，绕过路由的新步骤会让测试失败。新增模型步骤时，在注册表里登记它的 `usage_stage`、解析函数、重试策略和本地兜底。

### 5.3 `AgentSignal`

`AgentSignal` 是两个异常的共同基类：

- `AgentWorkRequired`：遇到缺少结果的 Agent 步骤时由路由抛出，带着本轮的待办项。
- `AgentContentRejected`：Agent 来源的结果在路由层解析、校验失败时抛出，路由同时删除这一项的缓存。Agent 来源的结果不做解析重试（每次读到的都是同一份缓存）。

`AgentSignal` 永远向上抛，不触发回退。规则：凡是会包住模型步骤的 `except Exception`，前面都必须有 `except AgentSignal: raise`，否则 Agent 步骤会被悄悄变成本地兜底。防吞异常测试会在每个模型步骤抛出信号，断言它能穿透摘要、内联图文和思维导图。

### 5.4 重放和全步骤缓存键

Agent 任务不保存执行到一半的状态，而是**重放**：

1. 缺少 Agent 结果时，路由登记待办项（`run_many` 一次收齐整批）并抛出 `AgentWorkRequired`；worker 保存待办项，把任务从 `running` 切到 `agent_summarizing`，释放队列位置。
2. Agent 提交后，任务重新进入总结队列，**总结单元**（摘要，其中含内联图文笔记；以及 Agent 模式下的思维导图）从头再跑一遍：已有结果的步骤直接读取，遇到下一个缺少结果的 Agent 步骤就再交给 Agent，直到全部完成。

让重放可靠的几点：

- **全步骤缓存**：Agent 任务里，`agent`、`internal`、`local` 三种来源的每次模型调用结果都缓存在任务目录的 `agent_results/` 里，失败的结果也缓存（记下原因和回退路径）。混合路由下内置模型每一步只调用一次，多轮重放的结果是确定的。代价是：第一轮因偶发超时被跳过的分块，在后续轮次里会一直被跳过；想补回可以重新总结。
- **缓存键是 `usage_stage#下标`**（如 `summary_chunk#2`、`mindmap_repair#0`、`visual_frame#3`）。不用步骤名，因为同一步骤下的多个 `usage_stage`（如 `mindmap` 和 `mindmap_repair`）不能互相覆盖。下标是逻辑下标而不是调用顺序：分块用块号，帧用帧号，只调用一次的为 0；并发分块的完成顺序每轮不同，重试也沿用同一个键。请求内容的哈希只用于检测提示词变化并告警。
- **转写落盘**：转写结果写入 `task_dir/transcription.json`，每轮重放从这里加载。服务重启时，已有转写的任务直接进入总结队列，不重新下载和转写（默认模式同样受益）。
- **视觉素材复用**：`visual_evidence/assets.json` 记录已下载的视频和已抽取的帧，重放时不再重复下载和抽帧；关键帧规划变化时只抽新增的时间点。重新生成图文证据（`force`）只清理派生产物，保留源视频。
- **进度只增不减**：重放时已完成的步骤会再发一遍进度事件，事件处理器丢弃不超过上次记录值的进度。

## 6. 相关文档

- [CLI 使用指南](cli.md)：`bilisum agent`、`bilisum result`、MCP 服务的配置与工具参数
- [配置说明](configuration.md)：Agent 模式的设置项和环境变量
- [贡献指南](contributing.md)：开发环境、测试与 PR 流程
- [Skills 安装说明](skills/README.md)：`bilisum-video-understanding` skill
