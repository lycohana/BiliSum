# 贡献指南

感谢你愿意参与 BiliSum！本指南覆盖开发环境、代码结构、代码风格、测试与发布流程。

## 开发环境

要求：Python **3.12**、Node.js **20+**（推荐 22）、Windows / macOS。

```powershell
uv sync --python 3.12 --all-packages   # Python 工作区（packages/core, packages/infra, apps/service）
npm install --prefix .\apps\desktop    # 桌面端
Copy-Item .env.example .env            # 可选：本地配置
```

## 仓库结构

```
BiliSum/
├── apps/
│   ├── desktop/          # Electron + React + TypeScript 桌面端
│   ├── web/              # 浏览器版静态产物（构建输出）
│   └── service/          # FastAPI 后端（video_sum_service）
│       └── src/video_sum_service/
│           ├── app.py              # FastAPI 入口 / lifespan / 中间件
│           ├── worker.py           # 后台任务调度（摘要 / 导图 / 图文笔记队列）
│           ├── repository.py       # SQLite 持久化
│           ├── settings_manager.py # 配置管理（settings.json）
│           ├── cli_idle.py         # CLI 后台服务的空闲自动关闭
│           ├── knowledge/          # 知识库（索引 / RAG / 标签）
│           └── routers/            # API 路由（tasks / videos / knowledge / system）
├── packages/
│   ├── core/             # 下载 / 转写 / 摘要 / 图文笔记核心逻辑（video_sum_core）
│   └── infra/            # 配置 / 运行时 / LLM 工具（video_sum_infra）
├── packages/npx/         # bilisum CLI（node，零依赖；bin/ + lib/ + test/）
├── docs/                 # 文档（cli / configuration / contributing）
└── scripts/              # 版本管理 / 发布脚本（bump_version / detect_bump / release notes）
```

## 代码风格

- **Python**：PEP 8 + 类型注解（`ruff` 校验：`E/F/I/B/UP`，行宽 100）
- **TypeScript**：严格模式 + 函数式组件
- **CLI（Node）**：CommonJS + JSDoc 注释，零依赖（Node ≥ 18）
- **提交信息**：Conventional Commits（`feat:` / `fix:` / `chore:` / `docs:` / `refactor:` ...）

```bash
.\.venv\Scripts\python -m ruff check packages apps tests scripts   # Python lint
npm run typecheck --prefix apps/desktop                            # 桌面端类型检查
```

## 测试

```powershell
.\.venv\Scripts\python -m pytest          # Python 测试（tests/unit）
npm test --prefix packages/npx            # CLI 单元测试（node:test）
npm test --prefix apps/desktop            # 桌面端测试
npm run npx:test                          # CLI 全链路（版本 + 单测 + pack 校验）
```

- Python 测试在 `tests/unit/`，用内存 SQLite / 假 Worker 隔离外部依赖。
- CLI 测试在 `packages/npx/test/`，覆盖参数解析 / token / 输出格式化 / 服务运行时文件，**必须跨平台**（CI 跑 Linux，勿假设 `%APPDATA%` 等 Windows 语义）。

## PR 流程

1. 从最新 `master` 开分支（`feat/`、`fix/`、`docs/` 前缀）。
2. 提交并推送，开 PR 到 `master`（描述里关联 Issue 编号）。
3. CI 会自动跑：Python 测试、CLI 测试、桌面端测试/类型检查/打包验证（含 Windows / macOS / Docker）。
4. 合并方式：仓库默认 **Squash merge**。

## 版本发布流程

BiliSum 采用「Release PR + tag」驱动（参考 MAA 模式）：

1. 向 `master` 推送带 `*` 标记（或 `BREAKING CHANGE`）的提交 → 自动 bump 版本并创建 **Release vX.Y.Z PR**（`prepare-release.yml`）。
2. Release PR 上可加 label `release:beta` / `release:rc` / `release:alpha` 切换预发布版本号（`release-pr-updater.yml`）。
3. 合并 Release PR → 自动打 tag 并触发构建 → 发布 GitHub Release（桌面端安装包 + 升级通道）（`create-release-tag.yml` / `release.yml`）。
4. GitHub Release 发布成功后自动触发 npm 与 Docker 发布（`publish-npx-package.yml` / `publish-docker-image.yml`）。

版本同步由 `scripts/bump_version.py` 统一管理（VERSION + 全部 pyproject.toml + package.json + package-lock.json）。

## 文档

- 用户文档在 `docs/`，入口见 [docs/README.md](README.md)。
- 修改 CLI 行为时同步更新 `docs/cli.md`；修改配置项时同步更新 `docs/configuration.md`。
- npm 包自带 README（`packages/npx/README.md`）保留核心用法。

## 常见约定

- 涉及服务端新能力（如 CLI 自动拉起的服务）时，务必用环境变量标记与桌面端行为隔离，禁止影响桌面端默认行为。
- CLI 新增命令 / 选项必须配套单元测试与 `docs/cli.md` 更新。
- 发布相关改动（workflow / 版本脚本）必须本地跑 `python scripts/bump_version.py --check` 验证版本同步。
