# BiliSum CLI

`bilisum` 是 BiliSum 的命令行控制面：**控制已安装的 BiliSum（桌面端 / 已运行的服务），或完全独立运行**（未安装桌面端也能用），让 Agent / 脚本做视频理解（转写 + 摘要）。

```bash
# 摘要一个视频（默认输出：元数据 + 总结内容，不含字幕）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx"

# 快速摘要卡片（跳过知识笔记 LLM 调用，更快）
bilisum brief "https://www.bilibili.com/video/BV1xxxx"

# 输出 JSON 给 Agent 解析（stdout 只含 JSON，进度在 stderr）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --format json

# 总结 + 追加转写全文 / 只要转写
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --with-transcript
bilisum transcribe ./demo.mp4 --output transcript.txt

# 多 P / 异步
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --page 2
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --all-pages --no-wait

# 任务管理
bilisum status <task-id> --json
bilisum tasks --limit 10

# 帮助（无参数执行也会显示 help，不会自动启动服务）
bilisum --help
bilisum summarize --help
```

## 环境（Environment）

四种环境决定连接哪个服务、用哪套数据：

| 环境 | 说明 | 地址 |
|------|------|------|
| `desktop` | 连接桌面端服务（控制现有程序，同库同知识库） | `127.0.0.1:3838` |
| `cli` | **独立环境**：自带运行时与配置，无需桌面端 | `127.0.0.1:3839` |
| `custom` | 任意 host/port/token（Docker、远程） | 自定 |
| `auto` | 默认：桌面端可达则 desktop，否则 cli | 自动 |

```bash
bilisum env                      # 查看环境状态
bilisum env use cli              # 切换到独立环境
bilisum desktop                  # 快捷切换到桌面端环境
bilisum auto                     # 恢复自动选择
bilisum env setup                # 初始化独立环境（内置运行时，需 Python 3.12）
bilisum --setting                # 打开当前环境的网页设置（cli 环境无需桌面端）
bilisum summarize <url> --environment cli   # 单次使用某环境
```

> 完整文档见 [BiliSum CLI 使用指南](https://github.com/lycohana/BiliSum/blob/master/docs/cli.md)。

## 输出格式

- **`markdown`（默认）**：YAML 元数据头（task_id / status / title / video_id / source）+ 总结内容（`summarize` 为知识笔记，`brief` 为摘要卡片），**不含字幕**；加 `--with-transcript` 会追加转写全文。
- **`json`**：完整任务对象（含 `result`，字幕在 `result.transcript_text`）。
- **`transcript`**：仅转写全文（`bilisum transcribe` 的默认输出）。

任务等待期间进度显示在 stderr（`[task_id]  12% [stage] 消息`），stdout 保持干净。

## 它如何"控制现有程序"

- `desktop` 环境连接桌面端服务 `http://127.0.0.1:3838`，任务、结果、知识库都与桌面端**同一个数据库**；默认 `auto` 会优先探测 desktop，不可达时使用 cli 独立环境。
- 访问令牌自动获取：`--token` → `VIDEO_SUM_ACCESS_TOKEN` → 桌面端自己的 token 文件（`%APPDATA%\BiliSum\access-token.json` 等）→ 服务侧 `{数据根}\data\auth.json`。桌面端正在运行时通常无需任何配置。
- desktop 数据根默认与桌面端一致（Windows `%LOCALAPPDATA%\bilisum`，macOS `~/Library/Application Support/bilisum`）；cli 使用独立的 `CLI_HOME`。
- 配置（LLM / ASR Key 等）跟随服务端设置，桌面端里配好了，CLI 直接用。

## 服务不在线时

`summarize / brief / transcribe / status / tasks` 会先探活当前环境。desktop 不可达时用桌面端数据目录拉起服务，cli 不可达时用 CLI 独立数据目录和 venv 拉起服务；custom 只连接指定地址，不会在本机拉起替代服务。CLI 拉起的服务进程记录在 `{数据根}\cli-runtime-<port>.json`：

```bash
bilisum start --no-open   # 前台启动（可选，一般不用）
bilisum stop              # 停止 CLI 拉起的服务
```

**无任务自动关闭**：CLI 后台拉起的服务默认空闲 10 分钟（且无排队/运行中任务）自动退出，可用 `--idle-timeout <sec>` 或 `BILISUM_CLI_IDLE_TIMEOUT` 环境变量调整。该机制只作用于 CLI 自己拉起的服务（通过 `VIDEO_SUM_CLI_MANAGED` 标记），**桌面端启动的服务绝不会被自动关闭**。

> 注意：桌面端自己运行的服务不受 `bilisum stop` 影响（没有 cli-runtime 记录时会提示）。

## 安装

```bash
npm install -g bilisum          # 或 npx bilisum ...
```

包内包含构建独立环境所需的 Python 源码；首次执行 `bilisum env setup` 仍要求系统已有 Python 3.12。

## Agent 视频理解 Skill

安装 CLI 后，可以把随包提供的 `bilisum-video-understanding` skill 安装给 Agent：

```bash
# 从 GitHub 安装到当前项目或指定 Agent
npx skills add https://github.com/lycohana/BiliSum --skill bilisum-video-understanding

# 使用 BiliSum CLI 自带的交互式安装器
bilisum skill install

# 非交互环境下显式指定目标
bilisum skill install --project
bilisum skill install --global
bilisum skill install --path ./agent-skills/bilisum-video-understanding
```

`bilisum skill install` 在终端中会让你选择当前项目、Codex 全局目录或自定义目录；被 Agent/CI 调用且未指定目标时只打印路径，不会写入文件。已有 skill 需要更新时加 `--force`。安装后，Agent 可以根据用户请求调用 `summarize`、`brief`、`transcribe` 和 `status` 理解视频。

## 常用选项

| 选项 | 说明 |
|------|------|
| `--host / --port` | 自定义服务地址（显式指定时进入 custom 环境） |
| `--data <path>` | 桌面端数据根（默认 `%LOCALAPPDATA%\bilisum`） |
| `--token <token>` | 访问令牌（默认自动读桌面端 token） |
| `--format json\|markdown\|transcript` | 输出格式，默认 markdown |
| `--output <path> / -o` | 结果写入文件 |
| `--with-transcript` | markdown 输出追加转写全文（默认不含字幕） |
| `--page <n> / --all-pages` | 多 P 视频分 P 选择 |
| `--visual-note <mode>` | `text` / `frame_insert` / `vlm_integrated` |
| `--no-wait` | 只创建任务，立即返回 task_id |
| `--idle-timeout <sec>` | CLI 后台服务空闲自动关闭秒数（默认 600） |
| `--timeout <sec>` | 等待完成超时（默认 3600） |
| `--quiet` | 抑制 stderr 进度输出 |

> 说明：任务转写与摘要在服务端一体执行；`transcribe` 输出转写全文，任务仍会顺带生成摘要。语言 / 摘要模式等参数跟随服务端设置。

> 完整文档见 [BiliSum CLI 使用指南](https://github.com/lycohana/BiliSum/blob/master/docs/cli.md)。

## 桌面端内置

桌面版 BiliSum 打包时会把 CLI 一并放进安装目录的 `resources/cli`，可用：

```bash
node "<安装目录>\resources\cli\bin\bilisum.js" summarize "https://..."
```

## 开发

```bash
npm test --prefix packages/npx   # node:test 单元测试
npm run npx:test                 # 版本 + 单测 + npm pack 校验（根目录）
node packages/npx/bin/bilisum.js --setting  # 可直接从仓库运行；自动使用仓库内 runtime/web 资源
```
