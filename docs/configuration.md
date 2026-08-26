# BiliSum 配置说明

BiliSum 的配置来源按优先级从高到低：

1. **持久化设置** `{数据根}\data\settings.json`（设置页保存的内容，**会覆盖环境变量**，含端口/数据目录等）
2. **环境变量**（前缀 `VIDEO_SUM_`，如 `VIDEO_SUM_LLM_API_KEY`）
3. **`.env` 文件**（服务启动目录下的 `.env`，仅服务端读取；CLI 不读 `.env`，用 `BILISUM_*` 环境变量）
4. 内置默认值

> ⚠️ 注意：`settings.json` 里持久化的字段（如 `port`、`data_dir`、`runtime_channel`）优先级最高，环境变量无法覆盖。这也是 CLI 在 `--port` 与持久化端口冲突时报错的原因。

## 最小配置（.env 示例）

复制 `.env.example` 为 `.env` 后填写：

```env
# 服务监听
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

## 变量全表

### 服务监听 / 数据

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_HOST` | `127.0.0.1`（Docker 为 `0.0.0.0`） | 监听地址 |
| `VIDEO_SUM_PORT` | `3838` | 监听端口 |
| `VIDEO_SUM_APP_DATA_ROOT` | 用户数据根（Windows `%LOCALAPPDATA%\bilisum`） | 数据根目录（推荐用这个统一控制） |
| `VIDEO_SUM_ACCESS_TOKEN` | 自动生成于 `{数据根}\data\auth.json` | API 访问令牌 |
| `VIDEO_SUM_RUNTIME_CHANNEL` | `base` | 运行时通道（`base` / `gpu-cu128` 等） |

> `data_dir` / `cache_dir` / `tasks_dir` / `database_url` 由 `VIDEO_SUM_APP_DATA_ROOT` 推导，也可单独覆盖。

### 转写（ASR）

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_TRANSCRIPTION_PROVIDER` | `siliconflow` | `siliconflow` / `multimodal` / `local`（Whisper）/ `funasr`（QwenASR） |
| `VIDEO_SUM_SILICONFLOW_ASR_BASE_URL` | `https://api.siliconflow.cn/v1` | SiliconFlow 端点 |
| `VIDEO_SUM_SILICONFLOW_ASR_MODEL` | `TeleAI/TeleSpeechASR` | SiliconFlow 模型 |
| `VIDEO_SUM_SILICONFLOW_ASR_API_KEY` | 空 | SiliconFlow Key（**必填**） |
| `VIDEO_SUM_SILICONFLOW_ASR_CHUNK_DURATION_SECONDS` | `1800` | 长音频切片时长 |
| `VIDEO_SUM_SILICONFLOW_ASR_CONCURRENCY` | `2` | 并发切片数 |
| `VIDEO_SUM_MULTIMODAL_ASR_BASE_URL` | 空 | 多模态 ASR 端点（OpenAI 兼容） |
| `VIDEO_SUM_MULTIMODAL_ASR_MODEL` | `mimo-v2-omni` | 多模态模型 |
| `VIDEO_SUM_MULTIMODAL_ASR_API_KEY` | 空 | 多模态 Key |
| `VIDEO_SUM_MULTIMODAL_ASR_CHUNK_DURATION_SECONDS` | `180` | 切片时长 |
| `VIDEO_SUM_MULTIMODAL_ASR_MAX_RETRIES` | `5` | 重试次数 |
| `VIDEO_SUM_WHISPER_MODEL` | `tiny` | 本地 Whisper 模型 |
| `VIDEO_SUM_WHISPER_DEVICE` | `cpu` | `cpu` / `cuda` |
| `VIDEO_SUM_WHISPER_COMPUTE_TYPE` | `int8` | 量化类型 |
| `VIDEO_SUM_DEVICE_PREFERENCE` | `cpu` | 设备偏好（`auto` / `cpu` / `cuda`） |
| `VIDEO_SUM_FUNASR_MODEL` | `paraformer-zh` | FunASR 模型 |
| `VIDEO_SUM_FUNASR_DEVICE` | `cpu` | FunASR 设备 |
| `VIDEO_SUM_FUNASR_HUB` | `ms` | 模型源（`ms` ModelScope / `hf` HuggingFace） |
| `VIDEO_SUM_FUNASR_VAD_MODEL` / `_PUNC_MODEL` / `_SPK_MODEL` / `_HOTWORD` | 见默认 | VAD / 标点 / 说话人 / 热词 |
| `VIDEO_SUM_PREFER_BILIBILI_SUBTITLE` | `true` | 优先 B 站字幕 |

### LLM 摘要

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_LLM_ENABLED` | `false` | 启用 LLM 摘要 |
| `VIDEO_SUM_LLM_PROVIDER` | `openai-compatible` | `openai-compatible` / `anthropic` / 自定义 |
| `VIDEO_SUM_LLM_BASE_URL` | 空 | 端点 |
| `VIDEO_SUM_LLM_MODEL` | 空 | 模型名 |
| `VIDEO_SUM_LLM_API_KEY` | 空 | API Key |
| `VIDEO_SUM_SUMMARY_MODE` | `llm` | `llm` / `rule` / `auto` |
| `VIDEO_SUM_SUMMARY_CONTEXT_MODE` | `auto` | `auto` / `full` / `chunked`；自动模式对短字幕整段发送，长字幕回退分块 |
| `VIDEO_SUM_SUMMARY_FULL_CONTEXT_MAX_CHARS` | `18000` | 自动模式的整段字幕字符上限 |
| `VIDEO_SUM_LANGUAGE` | `zh` | 输出语言 |
| `VIDEO_SUM_SUMMARY_CHUNK_TARGET_CHARS` | `2200` | 摘要分块目标字数 |
| `VIDEO_SUM_SUMMARY_CHUNK_OVERLAP_SEGMENTS` | `2` | 分块重叠段数 |
| `VIDEO_SUM_SUMMARY_CHUNK_CONCURRENCY` | `2` | 分块并发 |
| `VIDEO_SUM_SUMMARY_CHUNK_RETRY_COUNT` | `2` | 分块重试 |

Gemini 可通过 Google 的 OpenAI-compatible 端点使用：将 Provider 设为 `openai-compatible`，Base URL 填写 `https://generativelanguage.googleapis.com/v1beta/openai`，模型名填写实际 Gemini 模型（例如 `gemini-2.5-pro`）。发送到 Gemini 时不会携带本地模型专用的 `enable_thinking` 和 `chat_template_kwargs` 字段。

### 视觉模型 / 图文笔记

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_VISUAL_NOTE_MODE` | `text` | `text` / `frame_insert` / `vlm_integrated` |
| `VIDEO_SUM_VISUAL_EVIDENCE_ENABLED` | `false` | 启用图文笔记 |
| `VIDEO_SUM_VISUAL_VLM_PROVIDER` | `openai-compatible` | 视觉模型提供商 |
| `VIDEO_SUM_VISUAL_EVIDENCE_BASE_URL` / `_MODEL` / `_API_KEY` | 空 | 视觉模型端点 / 模型 / Key |
| `VIDEO_SUM_VISUAL_EVIDENCE_MAX_FRAMES` | `12` | 最多截图数 |
| `VIDEO_SUM_VISUAL_EVIDENCE_FRAME_INTERVAL_SECONDS` | `10` | 截图最小间隔 |
| `VIDEO_SUM_VISUAL_DOWNLOAD_RESOLUTION` | `720p` | 截图清晰度 |
| `VIDEO_SUM_TWELVELABS_SUMMARY_ENABLED` | `false` | Twelve Labs Pegasus 视频理解 |
| `VIDEO_SUM_TWELVELABS_API_KEY` / `_MODEL` | 空 / `pegasus1.5` | Pegasus Key / 模型 |

### 知识库（RAG）

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_KNOWLEDGE_ENABLED` | `false` | 启用知识库索引 |
| `VIDEO_SUM_KNOWLEDGE_EMBEDDING_PROVIDER` | `local_huggingface` | `local_huggingface` / `local_modelscope` / `siliconflow` |
| `VIDEO_SUM_KNOWLEDGE_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | Embedding 模型 |
| `VIDEO_SUM_SILICONFLOW_EMBEDDING_API_KEY` / `_BASE_URL` / `_MODEL` | 空 / `https://api.siliconflow.cn/v1` / `BAAI/bge-large-zh-v1.5` | SiliconFlow Embedding |
| `VIDEO_SUM_KNOWLEDGE_LLM_MODE` | `same_as_main` | `same_as_main` / `custom` |
| `VIDEO_SUM_KNOWLEDGE_LLM_ENABLED` / `_PROVIDER` / `_BASE_URL` / `_MODEL` / `_API_KEY` | 关 / 同主 LLM | 知识问答 LLM |
| `VIDEO_SUM_KNOWLEDGE_INDEX_AUTO_REBUILD` | `disabled` | 任务完成自动重建索引 |

### 其他

| 变量 | 默认 | 说明 |
|------|------|------|
| `VIDEO_SUM_YTDLP_COOKIES_FILE` | 空 | B 站 Cookies 文件（风控时用） |
| `VIDEO_SUM_YTDLP_COOKIES_BROWSER` | 空 | 从浏览器读取 Cookies |
| `VIDEO_SUM_TASK_CONCURRENCY` | `2` | 任务并发数 |
| `VIDEO_SUM_OUTPUT_DIR` | 空 | 导出目录 |
| `VIDEO_SUM_ENABLE_CACHE` | `true` | 缓存 |
| `VIDEO_SUM_PRESERVE_TEMP_AUDIO` | `false` | 保留临时音频 |

## CLI 相关配置

CLI（`bilisum`）不读 `.env`，使用 `BILISUM_*` 环境变量，详见 [CLI 使用指南](cli.md)：

| 变量 | 说明 |
|------|------|
| `BILISUM_HOST` / `BILISUM_PORT` | 服务地址 |
| `BILISUM_DATA_ROOT` / `BILISUM_DATA` | 桌面端数据根覆盖 |
| `BILISUM_CLI_HOME` | **CLI 独立环境（cli 模式）的数据根**：config.json + venv + 服务数据 |
| `BILISUM_TOKEN` | 访问令牌 |
| `BILISUM_PYTHON` | Python 可执行文件 |
| `BILISUM_CLI_IDLE_TIMEOUT` | CLI 后台服务空闲自动关闭秒数（默认 600） |

### CLI 环境与 config.json

CLI 支持四种环境（`bilisum env` 查看 / `bilisum env use <name>` 切换，也可直接执行 `bilisum <name>`）：

- `desktop`：连接桌面端服务（127.0.0.1:3838，桌面端数据根与 token）
- `cli`：独立环境（默认 127.0.0.1:3839，数据根 = `BILISUM_CLI_HOME`，自带 venv 与设置页，无需桌面端）
- `custom`：任意 host/port/token（Docker、远程）
- `auto`（默认）：桌面端可达则 desktop，否则 cli

`bilisum env use auto`（或 `bilisum auto`）可清除固定环境的效果、恢复自动选择；单次使用 `--environment auto` 时也会忽略持久化选择并重新探测。

选择持久化在 `CLI_HOME/config.json`：

```json
{
  "env": "auto",
  "custom": { "host": "127.0.0.1", "port": "3838", "token": "" }
}
```

CLI 自动拉起服务时会注入 `VIDEO_SUM_CLI_MANAGED=1` 与 `VIDEO_SUM_CLI_IDLE_TIMEOUT_SECONDS`，服务端据此启用空闲自动退出；**桌面端启动的服务不受影响**。

## Docker

```bash
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

容器内服务监听 `0.0.0.0:3838`，数据目录 `/data`（即 `VIDEO_SUM_APP_DATA_ROOT=/data`）。
