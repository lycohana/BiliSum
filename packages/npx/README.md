# BiliSum CLI

`bilisum` 是 BiliSum 的命令行控制面：**直接控制已安装的 BiliSum（桌面端 / 已运行的服务）**，让 Agent / 脚本做视频理解（转写 + 摘要），不打开浏览器、不复用任何独立环境。

```bash
# 摘要一个视频（输出知识笔记 Markdown）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx"

# 输出 JSON 给 Agent 解析（stdout 只含 JSON，进度在 stderr）
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --format json

# 只拿转写全文 / 本地文件
bilisum transcribe ./demo.mp4 --output transcript.txt

# 多 P / 异步
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --page 2
bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --all-pages --no-wait

# 任务管理
bilisum status <task-id> --json
bilisum tasks --limit 10
```

## 它如何"控制现有程序"

- 默认连接桌面端服务 `http://127.0.0.1:3838`，任务、结果、知识库都与桌面端**同一个数据库**。
- 访问令牌自动获取：`--token` → `VIDEO_SUM_ACCESS_TOKEN` → 桌面端自己的 token 文件（`%APPDATA%\BiliSum\access-token.json` 等）→ 服务侧 `{数据根}\data\auth.json`。桌面端正在运行时通常无需任何配置。
- 数据根默认与桌面端一致（Windows `%LOCALAPPDATA%\bilisum`，macOS `~/Library/Application Support/bilisum`），可用 `--data` / `BILISUM_DATA_ROOT` 覆盖。
- 配置（LLM / ASR Key 等）跟随服务端设置，桌面端里配好了，CLI 直接用。

## 服务不在线时

`summarize / transcribe / status / tasks` 会先探活；如果 `127.0.0.1:3838` 没有服务，就**用桌面端数据目录自动后台拉起一个**（优先用桌面端自带的托管 Python 运行时，其次系统 Python 3.12），等 `/health` 就绪后继续。CLI 拉起的服务进程记录在 `{数据根}\cli-runtime.json`：

```bash
bilisum start --no-open   # 前台启动（可选，一般不用）
bilisum stop              # 停止 CLI 拉起的服务
```

> 注意：桌面端自己运行的服务不受 `bilisum stop` 影响（没有 cli-runtime 记录时会提示）。

## 安装

```bash
npm install -g bilisum          # 或 npx bilisum ...
```

包内**不包含 Python 运行时**（CLI 是纯客户端），首次使用要求机器上已有桌面版 BiliSum 或 Python 3.12。

## 常用选项

| 选项 | 说明 |
|------|------|
| `--host / --port` | 服务地址（默认 `127.0.0.1:3838`） |
| `--data <path>` | 桌面端数据根（默认 `%LOCALAPPDATA%\bilisum`） |
| `--token <token>` | 访问令牌（默认自动读桌面端 token） |
| `--format json\|markdown\|transcript` | 输出格式，默认 markdown |
| `--output <path> / -o` | 结果写入文件 |
| `--page <n> / --all-pages` | 多 P 视频分 P 选择 |
| `--visual-note <mode>` | `text` / `frame_insert` / `vlm_integrated` |
| `--no-wait` | 只创建任务，立即返回 task_id |
| `--timeout <sec>` | 等待完成超时（默认 3600） |
| `--quiet` | 抑制 stderr 进度输出 |

> 说明：任务转写与摘要在服务端一体执行；`transcribe` 输出转写全文，任务仍会顺带生成摘要。语言 / 摘要模式等参数跟随服务端设置。

## 桌面端内置

桌面版 BiliSum 打包时会把 CLI 一并放进安装目录的 `resources/cli`，可用：

```bash
node "<安装目录>\resources\cli\bin\bilisum.js" summarize "https://..."
```

## 开发

```bash
npm test --prefix packages/npx   # node:test 单元测试
npm run npx:test                 # 版本 + 单测 + npm pack 校验（根目录）
```
