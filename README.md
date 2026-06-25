<div align="center">

![BiliSum Banner](docs/pic/banner.svg)

# BiliSum

**AI 视频总结与知识库工具**

把 B 站、YouTube 和本地视频，沉淀成可检索、可追问、可导出的知识笔记。

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Windows | macOS](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)](#)

[快速开始](#-快速开始) · [产品特性](#-产品特性) · [技术栈](#️-技术栈) · [贡献指南](#-贡献指南)

</div>
 
---

> 深度优化 B 站体验，同时支持 YouTube 与本地视频。自动转写、总结、图文笔记、思维导图、知识库 RAG 问答，数据全部落本地。

<div align="center">
  <img src="docs/pic/mainpage.png" alt="BiliSum 首页" width="800"/>
</div>

## 产品特性

```
B 站 / YouTube / 本地视频  →  转写  →  文本笔记  →  图文笔记  →  思维导图  →  知识库
         ↓                          ↓                          ↓
    B 站扫码登录              可回溯的任务历史             AI 检索问答
```

### 图文笔记（VLM 理解型） `新增`

VLM 理解型图文笔记是一版从零设计的笔记生成方式：VLM 阅读原始笔记和所有截图的客观信息，以画面为线索重新组织文章结构。每张图跟在对应知识段落后面，不是原文里插几张图，也不是把所有截图堆在末尾。

- 支持 OpenAI / Anthropic / 兼容接口 / 自定义端点作为视觉模型
- 帧描述→客观事实列表，从源头杜绝流水账描述
- 精选配图（3-6 张），段落与图片交替呈现
- 合成超时 300s，预过滤低质量帧

在设置页 → 图文笔记形式中选择「VLM 理解型图文笔记」启用。

> VLM 模式会调用多模态模型处理截图，API 费用比纯文本高。想控制成本可以把「最多截图数」调小或「截图最小间隔」调大。超长视频的笔记可能偏简略，截图逻辑还在优化中——设置页可以自定义截图规划的提示词。

<div align="center">
  <img src="docs/pic/visual-note.png" alt="VLM 理解型图文笔记" width="800"/>
  <p><i>VLM 理解型图文笔记：段落与截图交替排列，禁止图片堆积末尾</i></p>
</div>

### 视觉模型独立配置 `新增`

画面理解和图文笔记合成可以独立配置视觉模型，不再跟随主 LLM。支持多提供商自动适配——选中 OpenAI / Anthropic / 兼容接口后，图片格式、端点、认证全部自动切换。第三方端点图片请求不兼容时也会自动回退。

<!-- TODO: 待补充视觉模型设置页截图 visual-settings.png -->

### 文本笔记 / 思维导图

- LLM 摘要：不只是压缩，而是识别论点、案例、结论，生成结构化知识卡片
- 思维导图：线性视频转放射状知识网络，支持缩放、拖拽、节点高亮
- 转写全文：章节时间轴 + 关键句定位，数学公式和代码块自动格式化
- 重跑机制：换套模型重生成摘要，不满意就再来

<div align="center">
  <img src="docs/pic/mindmappage.png" alt="BiliSum 思维导图" width="800"/>
  <p><i>线性视频转放射状知识网络，一眼看清内容结构和逻辑脉络</i></p>
</div>

### 知识库

- 跨视频 RAG 检索问答，语义搜索 + 关键词搜索
- 自动/手动标签，标签关系网络可视化
- 支持本地 LLM，断网也能用

<div align="center">
  <img src="docs/pic/knowledge.png" alt="BiliSum 知识库" width="800"/>
  <p><i>跨视频 AI 检索与问答，标签管理，可生长的知识体系</i></p>
</div>

### Twelve Labs Pegasus 视频理解（可选） `新增`

针对本地视频，可选开启 [Twelve Labs](https://twelvelabs.io/) 的 Pegasus 视频理解模型。Pegasus 直接“看”视频画面（演示步骤、界面操作、图表、代码、实验结果等），生成一段视频理解摘要，并自动并入知识笔记、随之进入知识库。它与基于转写文本的 LLM 摘要互补——当画面信息无法从字幕/转写获知时尤其有用。

- 完全可选、不改默认行为：默认关闭，未配置或调用失败都会安静跳过，不影响原有摘要与知识笔记。
- 仅对本地视频文件生效（直接读取磁盘上的原始视频）。
- 在设置页 → Twelve Labs Pegasus 视频理解中填入 API Key 并开启。
- 复用现有 `httpx` 直连 REST API（`/v1.3/analyze`），不引入额外依赖。

```env
VIDEO_SUM_TWELVELABS_SUMMARY_ENABLED=true
VIDEO_SUM_TWELVELABS_API_KEY=tlk-your-key
VIDEO_SUM_TWELVELABS_MODEL=pegasus1.5
```

> 可在 https://twelvelabs.io 免费申请 API Key，有较慷慨的免费额度。

### 多 P 视频与全集总结

自动检测分 P 视频，支持选择单个分 P 或批量创建任务。全集总结模式可聚合所有分 P 内容生成一篇总笔记。

### ASR 转写

- SiliconFlow ASR：长音频自动切片 + 并发识别，突破 60 分钟限制
- **FunASR（QwenASR）**：阿里本地语音识别引擎，中文效果超越 Whisper，CPU 速度约 34 倍，GPU 约 13 倍。支持 VAD / 自动标点 / 说话人识别
- 多模态 ASR：支持 OpenAI 兼容的音频模型（如 mimo-v2-omni），切片时长和重试次数可调
- 本地 Whisper：CPU / CUDA 可选

### 导入导出

- 导入：B 站链接、YouTube 链接、本地视频文件（mp4 / mkv / mov / webm）
- 导出：Markdown、Obsidian 格式，一键打包笔记和截图

### B 站风控

桌面端内置扫码登录，自动保存 Cookies。也支持手动导入 cookies.txt。

### 桌面端 `macOS 新增`

- Windows / macOS 双平台，自绘窗口栏，统一 UI 风格
- 动画启动画面
- 应用内自动更新，设置页一键检查新版本

## 技术栈

| 模块 | 技术选型 |
|------|----------|
| 桌面端 | Electron + React + TypeScript + Vite |
| 后端服务 | FastAPI + SQLite |
| 视频下载 | yt-dlp |
| 语音转写 | SiliconFlow ASR / 多模态 ASR / 本地 Whisper / FunASR（QwenASR） |
| 摘要生成 | OpenAI-compatible / Anthropic Claude / 本地规则降级 / Twelve Labs Pegasus 视频理解（可选） |
| 视觉模型 | OpenAI / Anthropic / 兼容接口（自动格式适配） |
| 知识库 RAG | Embedding 向量检索 + LLM Agent |
| 思维导图 | ReactFlow |
| 知识网络 | D3 Force Graph |
| 打包分发 | PyInstaller onedir + electron-builder + Docker |

## 快速开始

### 环境要求

- Python **3.12**
- Node.js **20+**
- Windows / macOS
- 可选：`ffmpeg`、CUDA

### 安装依赖

```powershell
uv sync --python 3.12 --all-packages
npm install --prefix .\apps\desktop
```

### 环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```env
VIDEO_SUM_HOST=127.0.0.1
VIDEO_SUM_PORT=3838
VIDEO_SUM_ACCESS_TOKEN=replace-with-a-long-random-token

# 转写（SiliconFlow）
VIDEO_SUM_TRANSCRIPTION_PROVIDER=siliconflow
VIDEO_SUM_SILICONFLOW_ASR_BASE_URL=https://api.siliconflow.cn/v1
VIDEO_SUM_SILICONFLOW_ASR_MODEL=TeleAI/TeleSpeechASR
VIDEO_SUM_SILICONFLOW_ASR_API_KEY=your-key

# LLM 摘要
VIDEO_SUM_LLM_ENABLED=true
VIDEO_SUM_LLM_PROVIDER=openai-compatible
VIDEO_SUM_LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
VIDEO_SUM_LLM_MODEL=qwen3.5-plus
VIDEO_SUM_LLM_API_KEY=your-key

# B 站 Cookies（遇到风控时配置）
VIDEO_SUM_YTDLP_COOKIES_FILE=
```

桌面端遇到 B 站风控优先用内置扫码登录。

### 启动开发环境

```powershell
npm run dev
```

同时拉起 Vite 渲染层、Electron 桌面壳、Python 后端。

### 桌面端打包

```powershell
npm run package:win     # Windows
npm run package:mac     # macOS
```

### Docker 浏览器版

```powershell
# 构建
npm run docker:build

# 运行
docker run --rm -p 3838:3838 \
  -v bilisum-data:/data \
  -e VIDEO_SUM_ACCESS_TOKEN=your-token \
  -e VIDEO_SUM_LLM_ENABLED=true \
  -e VIDEO_SUM_LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1 \
  -e VIDEO_SUM_LLM_MODEL=qwen3.5-plus \
  -e VIDEO_SUM_LLM_API_KEY=your-key \
  -e VIDEO_SUM_SILICONFLOW_ASR_API_KEY=your-key \
  lycohana/bilisum:latest
```

访问 `http://127.0.0.1:3838`。容器内服务监听 `0.0.0.0:3838`，数据目录 `/data`。

```powershell
docker pull lycohana/bilisum:latest
```

### 从旧版迁移

首次启动自动从 BriefVid 目录迁移数据到 BiliSum 目录，只复制缺失文件，不覆盖已有数据，不删除旧目录。

## 项目结构

```
BiliSum/
├── apps/
│   ├── desktop/          # Electron + React 桌面端
│   │   └── src/
│   │       ├── pages/         # 首页/视频库/知识库/详情页/设置
│   │       ├── components/    # 通用 UI 组件
│   │       ├── api.ts         # API 客户端
│   │       └── appModel.ts    # 状态管理
│   ├── web/              # 浏览器版静态产物
│   └── service/          # FastAPI 本地服务
│       └── src/video_sum_service/
│           ├── app.py              # FastAPI 入口
│           ├── worker.py           # 后台任务调度
│           ├── repository.py       # SQLite 持久化
│           ├── settings_manager.py # 配置管理
│           ├── knowledge/          # 知识库（索引/RAG/标签/本地LLM）
│           └── routers/            # API 路由
├── packages/
│   ├── core/             # 下载/转写/摘要/图文笔记核心逻辑
│   └── infra/            # 配置/运行时/LLM 工具函数
├── docs/pic/             # 文档截图
├── tests/                # 测试
└── .env.example
```

## 贡献指南

- 遇到 Bug 或有想法 → 直接开 Issue
- 修复 Bug、加功能、优化体验 → 提 PR
- 完善文档、补充示例 → 非常欢迎
- 在你的工作流里用起来了 → 分享经验，帮助后来者

代码风格：Python PEP 8 + 类型注解，TypeScript 严格模式 + 函数式组件，Commit 遵循 Conventional Commits。

## 开发流程

### 初始化

```powershell
uv sync --python 3.12 --all-packages
npm install --prefix .\apps\desktop
```

### 启动后端

```powershell
uv run --package video-sum-service python -m video_sum_service
```

### 启动桌面端

```powershell
npm run dev
```

### 测试

```powershell
.\.venv\Scripts\python -m pytest          # Python
npm test --prefix .\apps\desktop           # 桌面端
npm run typecheck --prefix .\apps\desktop  # 类型检查
```

### macOS / Linux

```bash
uv sync --python 3.12 --all-packages
uv run --package video-sum-service python -m video_sum_service
npm run dev
```

### 代码改了但运行时还是旧逻辑？

```powershell
uv run --package video-sum-service python -c "import video_sum_core, video_sum_service; print(video_sum_core.__file__); print(video_sum_service.__file__)"
```

如果输出不是仓库内的源码路径，重新执行 `uv sync --python 3.12 --all-packages`。

## 路线图

- [x] 思维导图视图
- [x] 本地视频导入与处理
- [x] 知识笔记 Markdown / Obsidian 导出
- [x] 知识库系统（RAG / 标签 / 知识网络）
- [x] B 站风控处理（扫码登录 / Cookies）
- [x] 多 P 视频批量处理与全集总结
- [x] GPU 运行时一键安装
- [x] macOS 桌面端
- [x] Anthropic / Claude 原生 API
- [x] 桌面端自动更新
- [x] VLM 理解型图文笔记
- [x] 视觉模型多提供商独立配置
- [x] 多模态 ASR
- [x] FunASR（QwenASR）本地语音识别
- [ ] 更多平台支持
- [ ] Notion 等第三方工具集成

## Star History

<a href="https://github.com/lycohana/bilisum">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=lycohana/bilisum&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=lycohana/bilisum&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=lycohana/bilisum&type=date&legend=top-left" />
 </picture>
</a>

## License

MIT License © 2026 Lycohana

特别致谢：[Linux Do](https://linux.do)

<div align="center">
  <sub>Built with ❤️ by Lycohana</sub>
</div>
