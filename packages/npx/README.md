# BiliSum CLI

`bilisum` 是 BiliSum 的命令行入口：既可以把本地服务跑起来，也可以让 Agent / 脚本直接做视频理解（转写 + 摘要），不需要打开浏览器。

```bash
npx bilisum
npx bilisum start --port 3839
npx bilisum doctor
```

Python 3.12 是必需的。首次运行会创建一个托管 Python 虚拟环境（位于用户本地应用数据目录）。

## Agent 用法（推荐）

一条命令完成「确保服务运行 → 提交任务 → 等待完成 → 输出结果」：

```bash
# 输出知识笔记（Markdown）到 stdout
npx bilisum summarize "https://www.bilibili.com/video/BV1xxxx"

# 输出 JSON 给 Agent 解析（stdout 只含 JSON，进度在 stderr）
npx bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --format json

# 只拿转写全文
npx bilisum transcribe "https://www.bilibili.com/video/BV1xxxx"

# 本地视频文件
npx bilisum summarize ./demo.mp4 --format json --output result.json

# 多 P 视频：指定 P 或全部
npx bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --page 2
npx bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --all-pages

# 不等待完成，先拿 task_id 异步处理
npx bilisum summarize "https://www.bilibili.com/video/BV1xxxx" --no-wait

# 查询任务
npx bilisum status <task-id> --json
npx bilisum tasks --limit 10
```

### 服务生命周期

- `summarize / transcribe / status / tasks` 会自动检测目标地址（默认 `127.0.0.1:3838`）是否有 BiliSum 服务；没有就**后台静默拉起一个**，等 `/health` 就绪后继续。
- 后台服务的进程号记录在数据目录的 `runtime.json`，用 `bilisum stop` 停止。
- 也可以自己先 `npx bilisum start --no-open` 常驻，Agent 再直接跑任务命令。

### 访问令牌

CLI 按以下顺序解析令牌：`--token` → `VIDEO_SUM_ACCESS_TOKEN` 环境变量 → 数据目录下的 `auth.json`（服务首次启动自动生成）。使用 CLI 默认数据目录时通常无需手动配置。

### 常用选项

| 选项 | 说明 |
|------|------|
| `--host / --port` | 服务地址（默认 `127.0.0.1:3838`） |
| `--data <path>` | 数据目录（任务库、缓存、auth.json） |
| `--token <token>` | 访问令牌 |
| `--format json\|markdown\|transcript` | 输出格式，默认 markdown |
| `--output <path> / -o` | 结果写入文件 |
| `--page <n> / --all-pages` | 多 P 视频分 P 选择 |
| `--visual-note <mode>` | `text` / `frame_insert` / `vlm_integrated` |
| `--no-wait` | 只创建任务，立即返回 task_id |
| `--timeout <sec>` | 等待完成超时（默认 3600） |
| `--quiet` | 抑制 stderr 进度输出 |

> 说明：任务转写与摘要在服务端一体执行；`transcribe` 输出转写全文，任务仍会顺带生成摘要。语言 / 摘要模式等参数跟随服务端设置。

## 开发

```bash
npm test --prefix packages/npx   # node:test 单元测试
npm run npx:test                 # 版本 + 单测 + npm pack 校验（根目录）
```
