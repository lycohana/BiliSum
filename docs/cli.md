# BiliSum CLI 使用指南

`bilisum` 是 BiliSum 的命令行控制面：**直接控制已安装的 BiliSum（桌面端 / 已运行的服务）**，让 Agent / 脚本做视频理解（转写 + 摘要），不打开浏览器、不复用任何独立环境。

## 安装

```bash
npm install -g bilisum      # 全局安装（推荐）
npx bilisum --help          # 或临时使用
```

包内**不包含 Python 运行时**（CLI 是纯客户端）。首次使用要求机器上已有桌面版 BiliSum（自带运行时）或系统 Python 3.12。

> 桌面版 BiliSum 安装包内也内置了 CLI（`resources/cli`）：`node "<安装目录>\resources\cli\bin\bilisum.js" ...`

## 快速开始

```bash
# 摘要一个视频（默认输出：元数据 + 知识笔记，不含字幕）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx"

# 快速摘要卡片（跳过知识笔记的 LLM 调用，更快）
bilisum brief "https://www.bilibili.com/video/BV1xxxx"

# 输出 JSON 给 Agent 解析（stdout 只含 JSON，进度在 stderr）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --format json

# 总结 + 追加转写全文 / 只要转写
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --with-transcript
bilisum transcribe ./demo.mp4 --output transcript.txt

# 任务管理
bilisum status <task-id> --json
bilisum tasks --limit 10

# 帮助（无参数执行也会显示 help，不会隐式启动服务）
bilisum --help
bilisum summarize --help
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `bilisum summarize <url\|file>` | 完整摘要：转写 → 摘要 → 知识笔记（默认输出笔记） |
| `bilisum brief <url\|file>` | 快速摘要卡片：只生成摘要（overview + 要点 + 章节），**跳过知识笔记 LLM 调用**，速度更快 |
| `bilisum transcribe <url\|file>` | 输出转写全文（任务仍会顺带生成摘要） |
| `bilisum status <task-id> [--json]` | 查询任务状态 / 进度 |
| `bilisum tasks [--limit N] [--json]` | 列出任务 |
| `bilisum start [--no-open]` | 前台启动服务（桌面端数据目录） |
| `bilisum stop` | 停止 CLI 后台拉起的服务 |
| `bilisum doctor` | 检查数据根 / Python / 服务 / 令牌状态 |
| `bilisum --version` | 打印版本 |
| `bilisum release` | 打开最新 GitHub Release |

## 常用选项

| 选项 | 说明 |
|------|------|
| `--host <host>` / `--port <port>` | 服务地址，默认 `127.0.0.1:3838` |
| `--data <path>` | 数据根目录（默认与桌面端一致，见下文） |
| `--token <token>` | 访问令牌（默认自动获取，见下文） |
| `--env KEY=VALUE` | 为 CLI 拉起的服务附加环境变量 |
| `--page <n>` / `--all-pages` | 多 P 视频分 P 选择 |
| `--visual-note <mode>` | `text` / `frame_insert` / `vlm_integrated` |
| `--prompt-preset <id>` | 使用摘要提示词预设 |
| `--format <fmt>` | `json` / `markdown` / `transcript`（默认 markdown） |
| `--output <path> / -o <path>` | 结果写入文件 |
| `--with-transcript` | markdown 输出追加转写全文 |
| `--no-wait` | 只创建任务，立即返回 task_id |
| `--idle-timeout <sec>` | CLI 后台服务无任务自动关闭秒数（默认 600，最小 30） |
| `--timeout <sec>` | 等待任务完成超时（默认 3600） |
| `--startup-timeout <sec>` | 等待服务启动超时（默认 300） |
| `--quiet / -q` | 抑制 stderr 进度输出 |

## 输出格式

- **`markdown`（默认）**：YAML 元数据头（`task_id` / `status` / `title` / `video_id` / `source`）+ 总结内容（`summarize` 为知识笔记，`brief` 为摘要卡片），**不含字幕**；加 `--with-transcript` 追加转写全文。
- **`json`**：完整任务对象（含 `result`，字幕在 `result.transcript_text`）。
- **`transcript`**：仅转写全文（`transcribe` 的默认输出）。

任务等待期间，进度显示在 **stderr**（`[task_id]  12% [stage] 消息`），stdout 保持干净，方便 Agent 直接解析 `--format json`。

## 它如何"控制现有程序"

- 默认连接桌面端服务 `http://127.0.0.1:3838`，任务、结果、知识库都与桌面端**同一个数据库**。
- 访问令牌自动获取：`--token` → `VIDEO_SUM_ACCESS_TOKEN` → 桌面端 token 文件（`%APPDATA%\BiliSum\access-token.json` 等）→ 服务侧 `{数据根}\data\auth.json`。
- 数据根默认与桌面端一致：Windows `%LOCALAPPDATA%\bilisum`，macOS `~/Library/Application Support/bilisum`。
- 配置（LLM / ASR Key 等）跟随服务端设置，桌面端里配好了，CLI 直接用。

## 服务生命周期

### 自动拉起

`summarize / brief / transcribe / status / tasks` 会先探活；`127.0.0.1:3838` 没有服务时，**用桌面端数据目录自动后台拉起一个**（优先桌面端托管 Python 运行时，其次系统 Python 3.12），等 `/health` 就绪后继续。CLI 拉起的服务进程记录在 `{数据根}\cli-runtime-<port>.json`，日志在 `{数据根}\cli-service-<port>.log`。

### 无任务自动关闭

CLI 后台拉起的服务默认在 **10 分钟（600 秒）无活动且无排队/运行中任务**后自动退出，避免长期占用资源。可配置：

```bash
# 命令行
bilisum summarize "https://..." --idle-timeout 120

# 环境变量（shell 级，所有命令生效）
set BILISUM_CLI_IDLE_TIMEOUT=120     # Windows
export BILISUM_CLI_IDLE_TIMEOUT=120  # macOS / Linux
```

> **安全边界**：该机制只作用于 CLI 自己拉起的后台服务（通过 `VIDEO_SUM_CLI_MANAGED=1` 环境变量标记）。**桌面端启动的服务永不设置该标记，绝不会被自动关闭**；前台 `bilisum start` 也不启用。
> 有任务在跑时服务不会关闭（先等任务队列清空，再计空闲时间）。

### 停止

```bash
bilisum stop     # 只停 CLI 拉起的服务（按端口匹配 cli-runtime 记录）
```

桌面端自己运行的服务不受 `bilisum stop` 影响（没有 cli-runtime 记录时会明确提示）。

## 环境变量

CLI 侧（shell 环境变量）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `BILISUM_HOST` / `BILISUM_PORT` | 服务地址 | `127.0.0.1` / `3838` |
| `BILISUM_DATA_ROOT` / `BILISUM_DATA` | 数据根目录 | 桌面端数据根 |
| `BILISUM_TOKEN` | 访问令牌 | 自动获取 |
| `BILISUM_PYTHON` | 指定 Python 可执行文件 | 自动查找 |
| `BILISUM_CLI_IDLE_TIMEOUT` | 后台服务空闲自动关闭秒数 | `600` |

服务侧（CLI 自动拉起时注入，一般无需手动设置）：`VIDEO_SUM_CLI_MANAGED=1`、`VIDEO_SUM_CLI_IDLE_TIMEOUT_SECONDS`。

完整的服务端配置变量见 [配置说明](configuration.md)。

## 常见问题

**Q: 提示"没有找到可用的 Python 3.12"**
未检测到桌面端托管运行时，且系统 Python 版本不足。安装桌面版 BiliSum 或 Python 3.12。

**Q: 提示系统 Python 缺少 BiliSum 服务依赖**
`No module named video_sum_service`：系统 Python 未安装服务依赖。请安装桌面版（自带运行时），或给该 Python 安装依赖后重试。

**Q: `--port` 与数据根的 settings.json 端口冲突**
服务端持久化设置（`{数据根}\data\settings.json`）会覆盖环境变量。冲突时 CLI 会明确报错：去掉 `--port`，或用 `--data` 指定不含该设置的数据根。

**Q: 桌面端在运行，但 CLI 连不上**
确认桌面端服务确实监听 `127.0.0.1:3838`（桌面端设置页可查看）；端口被改过时用 `--port` 对齐。

**Q: Docker 场景怎么用 CLI？**
CLI 连接的是 HTTP 服务，`docker run -p 3838:3838 ...` 后：

```bash
bilisum summarize "https://..." --host 127.0.0.1 --port 3838 --token <容器内设置的 VIDEO_SUM_ACCESS_TOKEN>
```
