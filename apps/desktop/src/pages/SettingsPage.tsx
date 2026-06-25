import { type FocusEvent, type FormEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import QRCode from "qrcode";

import {
  DesktopState,
  Snapshot,
  UpdateState,
  devicePreferenceLabel,
  formatShortDate,
  getUpdateStatusLabel,
  getUpdateStatusTone,
  getUpdateSummary,
  getConfigHealth,
  isUpdateUnsupported,
  normalizeDevicePreference,
  taskStatusClass,
} from "../appModel";
import { api } from "../api";
import { SearchIcon } from "../components/AppIcons";
import { FloatingNoticeStack } from "../components/FloatingNoticeStack";
import { DependencyTree } from "../components/DependencyTree";
import type { EnvironmentInfo, PromptPreset, PromptPresetCreateRequest, RuntimeStatus, ServiceSettings, StorageLocationKind, StorageDirectoryStat, StorageOverview, TaskSummary } from "../types";

import { formatDateTime, taskStatusLabel } from "../utils";
import { buildKnowledgeDependencyTree, buildAsrDependencyTree } from "../utils/dependencyTreeBuilder";
import { settingsCategories, type SettingsCategory } from "./settingsConfig";

const HIDDEN_PROMPT_PRESETS_STORAGE_KEY = "bilisum.hiddenPromptPresetIds";

function SiliconFlowApiKeyHelp({ purpose = "asr" }: { purpose?: "asr" | "embedding" }) {
  return (
    <div className="settings-input-help">
      <span className="settings-input-caption">
        {purpose === "embedding" ? "调用硅基流动在线向量模型必须提供 API Key。" : "调用云端语音识别必须提供 API Key。"}
      </span>
      <div className="settings-help-popover">
        <span className="settings-help-link" role="button" tabIndex={0}>
          如何获得 API？
        </span>
        <div className="settings-help-popover-card" id="siliconflow-api-help">
          <strong>获取步骤</strong>
          <ol>
            <li>
              注册 SiliconFlow 账号：
              {" "}
              <a href="https://cloud.siliconflow.cn/i/d8SF8w5Z" target="_blank" rel="noreferrer">
                点此注册
              </a>
            </li>
            <li>
              新建 API Key：
              {" "}
              <a href="https://cloud.siliconflow.cn/me/account/ak" target="_blank" rel="noreferrer">
                前往创建
              </a>
            </li>
          </ol>
        </div>
      </div>
    </div>
  );
}

function loadHiddenPromptPresetIds() {
  if (typeof window === "undefined") {
    return new Set<string>();
  }
  try {
    const parsed = JSON.parse(window.localStorage.getItem(HIDDEN_PROMPT_PRESETS_STORAGE_KEY) || "[]") as unknown;
    return new Set(Array.isArray(parsed) ? parsed.map((item) => String(item)).filter(Boolean) : []);
  } catch {
    return new Set<string>();
  }
}

function persistHiddenPromptPresetIds(ids: Set<string>) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(HIDDEN_PROMPT_PRESETS_STORAGE_KEY, JSON.stringify(Array.from(ids)));
  window.dispatchEvent(new Event("bilisum:prompt-presets-visibility-changed"));
}
type SettingsPageProps = {
  snapshot: Snapshot;
  desktop: DesktopState;
  focusIssueRequest?: { issueKey: string; nonce: number } | null;
  promptPresetRequest?: { presetId: string; nonce: number } | null;
  onRefresh(): void;
  onSettingsSaved(settings: ServiceSettings, environment: EnvironmentInfo | null): void;
  updateInfo: UpdateState;
  canCheckUpdate: boolean;
  canInstallUpdate: boolean;
  onCheckUpdate(): Promise<unknown>;
  onDownloadUpdate(): Promise<unknown>;
  onInstallUpdate(): Promise<void>;
  onOpenUpdateDialog(): void;
  onOpenSetupAssistant(): void;
};

const TASK_LIST_LIMIT = 60;
type GenerationModelDialog = "main" | "visual" | null;
type GenerationModelScope = Exclude<GenerationModelDialog, null>;
type ModelAvailabilityStatus = "unknown" | "checking" | "available" | "unavailable";

type ModelAvailabilityState = {
  status: ModelAvailabilityStatus;
  message: string;
};

type SettingsSearchItem = {
  category: SettingsCategory;
  targetKey: string;
  title: string;
  description: string;
  keywords: string[];
};

type BilibiliCookieCaptureResult = {
  cookiesFile: string;
  cookieCount: number;
  browser?: string;
};

const MASKED_API_KEY = "******";
const DEFAULT_SILICONFLOW_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1";
const DEFAULT_SILICONFLOW_EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5";

function isMaskedApiKey(value: string | undefined | null) {
  return String(value || "").trim() === MASKED_API_KEY;
}

function hasUsableApiKey(value: string | undefined | null, configured: boolean | undefined) {
  const trimmed = String(value || "").trim();
  return Boolean(configured || (trimmed && !isMaskedApiKey(trimmed)));
}

function maskConfiguredApiKeys(settings: ServiceSettings | null): ServiceSettings | null {
  if (!settings) {
    return settings;
  }
  return {
    ...settings,
    siliconflow_asr_api_key: settings.siliconflow_asr_api_key_configured ? MASKED_API_KEY : settings.siliconflow_asr_api_key,
    multimodal_asr_api_key: settings.multimodal_asr_api_key_configured ? MASKED_API_KEY : settings.multimodal_asr_api_key,
    llm_api_key: settings.llm_api_key_configured ? MASKED_API_KEY : settings.llm_api_key,
    knowledge_llm_api_key: settings.knowledge_llm_api_key_configured ? MASKED_API_KEY : settings.knowledge_llm_api_key,
    siliconflow_embedding_api_key: settings.siliconflow_embedding_api_key_configured ? MASKED_API_KEY : settings.siliconflow_embedding_api_key,
    visual_evidence_api_key: settings.visual_evidence_api_key_configured ? MASKED_API_KEY : settings.visual_evidence_api_key,
    twelvelabs_api_key: settings.twelvelabs_api_key_configured ? MASKED_API_KEY : settings.twelvelabs_api_key,
  };
}

function selectMaskedApiKey(event: FocusEvent<HTMLInputElement>) {
  if (isMaskedApiKey(event.currentTarget.value)) {
    event.currentTarget.select();
  }
}

const SETTINGS_SEARCH_ITEMS: SettingsSearchItem[] = [
  { category: "logs", targetKey: "host", title: "监听地址", description: "服务绑定的 IP 地址。", keywords: ["host", "ip", "地址", "服务入口"] },
  { category: "logs", targetKey: "port", title: "监听端口", description: "后端服务端口号。", keywords: ["port", "端口", "3838"] },
  { category: "files", targetKey: "data_dir", title: "数据目录", description: "视频摘要和元数据保存位置。", keywords: ["data", "目录", "存储", "数据库"] },
  { category: "files", targetKey: "cache_dir", title: "缓存目录", description: "临时缓存文件保存位置。", keywords: ["cache", "缓存", "临时文件"] },
  { category: "files", targetKey: "tasks_dir", title: "任务目录", description: "任务历史和结果文件位置。", keywords: ["task", "tasks", "任务", "历史"] },
  { category: "files", targetKey: "output_dir", title: "输出目录", description: "Markdown / Obsidian 导出目录。", keywords: ["output", "导出", "obsidian", "markdown", "笔记"] },
  { category: "files", targetKey: "storage_cleanup", title: "空间清理", description: "查看占用并清理缓存和孤儿任务。", keywords: ["清理", "空间", "缓存", "孤儿", "storage"] },
  { category: "transcription", targetKey: "transcription_provider", title: "转写方式", description: "选择云端 ASR 或本地 ASR。", keywords: ["asr", "转写", "语音识别", "whisper", "本地"] },
  { category: "transcription", targetKey: "siliconflow_asr_base_url", title: "SiliconFlow Base URL", description: "云端转写 API 地址。", keywords: ["siliconflow", "base url", "api", "硅基流动"] },
  { category: "transcription", targetKey: "siliconflow_asr_api_key", title: "SiliconFlow API Key", description: "云端转写 API 密钥。", keywords: ["key", "apikey", "api key", "密钥", "硅基流动"] },
  { category: "transcription", targetKey: "siliconflow_asr_model", title: "ASR 模型", description: "云端转写模型名称。", keywords: ["model", "模型", "teleai", "telespeechasr"] },
  { category: "transcription", targetKey: "siliconflow_asr_chunk_duration_seconds", title: "硅基 ASR 切片时长（秒）", description: "长音频自动切片的每段秒数，默认 1800（30 分钟）。", keywords: ["siliconflow", "切片", "chunk", "分段", "秒"] },
  { category: "transcription", targetKey: "siliconflow_asr_concurrency", title: "硅基 ASR 并发数", description: "同时发送的转写请求数。", keywords: ["siliconflow", "并发", "concurrency"] },
  { category: "transcription", targetKey: "multimodal_asr_base_url", title: "多模态 ASR Base URL", description: "多模态转写 API 地址。", keywords: ["multimodal", "多模态", "base url", "api"] },
  { category: "transcription", targetKey: "multimodal_asr_api_key", title: "多模态 ASR API Key", description: "多模态转写 API 密钥。", keywords: ["multimodal", "多模态", "key", "apikey", "api key", "密钥"] },
  { category: "transcription", targetKey: "multimodal_asr_model", title: "多模态 ASR 模型", description: "多模态转写模型名称。", keywords: ["multimodal", "多模态", "model", "模型", "mimo"] },
  { category: "transcription", targetKey: "multimodal_asr_chunk_duration_seconds", title: "多模态切片时长（秒）", description: "长音频自动切片的每段秒数。", keywords: ["multimodal", "切片", "chunk", "分段", "秒"] },
  { category: "transcription", targetKey: "multimodal_asr_max_retries", title: "多模态切片重试次数", description: "每段切片返回空时的重试上限。", keywords: ["multimodal", "重试", "retry", "次数"] },
  { category: "transcription", targetKey: "device_preference", title: "推理设备", description: "本地 ASR 使用 CPU 或 CUDA。", keywords: ["cuda", "gpu", "cpu", "设备"] },
  { category: "transcription", targetKey: "fixed_model", title: "Whisper 固定模型", description: "本地 Whisper 模型大小。", keywords: ["whisper", "tiny", "base", "small", "medium", "large"] },
  { category: "generation", targetKey: "llm_enabled", title: "启用 LLM 摘要", description: "打开或关闭大模型摘要。", keywords: ["llm", "摘要", "开启", "关闭"] },
  { category: "generation", targetKey: "auto_generate_mindmap", title: "自动生成思维导图", description: "摘要完成后自动生成导图。", keywords: ["导图", "mindmap", "自动", "思维导图"] },
  { category: "generation", targetKey: "prompt_router_mode", title: "Prompt 路由模式", description: "选择自动套用推荐 Prompt，或每次确认后使用。", keywords: ["prompt", "提示词", "路由", "自动", "确认"] },
  { category: "generation", targetKey: "visual_note_mode", title: "图文笔记形式", description: "选择纯文本、插图笔记或 VLM 理解型图文笔记。", keywords: ["视觉", "图片", "截图", "图文笔记", "vlm"] },
  { category: "generation", targetKey: "visual_download_resolution", title: "图文视频分辨率", description: "图文笔记专用视频下载清晰度。", keywords: ["图文", "分辨率", "下载", "截图"] },
  { category: "generation", targetKey: "visual_multimodal_enabled", title: "多模态理解", description: "是否调用 VLM 理解截图。", keywords: ["vlm", "多模态", "视觉", "图片理解"] },
  { category: "generation", targetKey: "llm_base_url", title: "LLM API Base URL", description: "主摘要 LLM API 地址。", keywords: ["base url", "openai", "compatible", "api", "地址"] },
  { category: "generation", targetKey: "llm_api_key", title: "LLM API Key", description: "主摘要 LLM API 密钥。", keywords: ["key", "apikey", "api key", "密钥"] },
  { category: "generation", targetKey: "llm_model", title: "LLM 模型名称", description: "主摘要使用的模型名。", keywords: ["model", "模型", "gpt", "qwen", "mimo", "claude"] },
  { category: "knowledge", targetKey: "knowledge_enabled", title: "启用知识库", description: "开启知识库索引和问答能力。", keywords: ["知识库", "knowledge", "rag", "索引", "问答"] },
  { category: "knowledge", targetKey: "knowledge_dependencies", title: "知识库依赖", description: "安装和检查知识库扩展依赖。", keywords: ["依赖", "安装", "runtime", "faiss", "向量"] },
  { category: "knowledge", targetKey: "knowledge_embedding_provider", title: "Embedding 模型来源", description: "选择向量模型的下载源：本地 HuggingFace（支持镜像）、本地 ModelScope 或硅基流动在线 API。", keywords: ["embedding", "bge", "向量", "huggingface", "modelscope", "siliconflow", "硅基流动"] },
  { category: "knowledge", targetKey: "siliconflow_embedding_api_key", title: "硅基流动 Embedding API Key", description: "硅基流动在线向量模型 API 密钥。", keywords: ["siliconflow", "硅基流动", "embedding", "api key", "密钥"] },
  { category: "knowledge", targetKey: "knowledge_llm_mode", title: "知识库 LLM 来源", description: "跟随主 LLM 或使用独立配置。", keywords: ["知识库", "llm", "来源", "独立配置"] },
  { category: "knowledge", targetKey: "knowledge_llm_provider", title: "知识库 LLM 提供商", description: "独立知识库 LLM 服务类型。", keywords: ["知识库", "provider", "openai", "anthropic", "提供商"] },
  { category: "knowledge", targetKey: "knowledge_llm_base_url", title: "知识库 API Base URL", description: "独立知识库 LLM API 地址。", keywords: ["知识库", "base url", "api", "openai"] },
  { category: "knowledge", targetKey: "knowledge_llm_api_key", title: "知识库 API Key", description: "独立知识库 LLM API 密钥。", keywords: ["知识库", "key", "apikey", "密钥"] },
  { category: "knowledge", targetKey: "knowledge_llm_model", title: "知识库模型名称", description: "独立知识库 LLM 模型名。", keywords: ["知识库", "model", "模型", "问答"] },
  { category: "generation", targetKey: "summary_mode", title: "摘要模式", description: "LLM 智能摘要或抽取式摘要。", keywords: ["摘要", "summary", "抽取式", "llm"] },
  { category: "generation", targetKey: "language", title: "语言", description: "摘要输出语言。", keywords: ["语言", "中文", "english", "日本語"] },
  { category: "generation", targetKey: "summary_chunk_target_chars", title: "分块目标字符数", description: "LLM 分块处理的目标长度。", keywords: ["分块", "chunk", "字符", "长度"] },
  { category: "generation", targetKey: "summary_chunk_overlap_segments", title: "分块重叠段数", description: "摘要分块之间保留的重叠段落。", keywords: ["重叠", "overlap", "分块"] },
  { category: "generation", targetKey: "summary_chunk_retry_count", title: "重试次数", description: "摘要 API 失败后的重试次数。", keywords: ["重试", "retry", "失败"] },
  { category: "generation", targetKey: "twelvelabs_summary_enabled", title: "Pegasus 视频理解", description: "Twelve Labs Pegasus 直接观看画面生成摘要。仅本地视频生效。", keywords: ["twelvelabs", "pegasus", "视频理解", "画面", "笔记"] },
  { category: "prompts", targetKey: "summary_system_prompt", title: "摘要 System Prompt", description: "控制视频摘要生成的角色、风格和整体约束。", keywords: ["摘要", "prompt", "system", "提示词", "风格"] },
  { category: "prompts", targetKey: "summary_user_prompt_template", title: "摘要 User Template", description: "控制摘要变量、结构和输出格式。", keywords: ["摘要", "template", "模板", "格式", "transcript"] },
  { category: "prompts", targetKey: "knowledge_note_system_prompt", title: "知识笔记 System Prompt", description: "控制知识笔记角色、风格和整体约束。", keywords: ["知识笔记", "prompt", "system", "提示词", "风格"] },
  { category: "prompts", targetKey: "knowledge_note_user_prompt_template", title: "知识笔记 User Template", description: "控制知识笔记变量、结构和 Markdown 格式。", keywords: ["知识笔记", "template", "模板", "格式", "summary_json", "transcript"] },
  { category: "prompts", targetKey: "visual_note_system_prompt", title: "图文笔记 System Prompt", description: "控制 VLM 图文笔记整合风格。", keywords: ["图文笔记", "prompt", "vlm", "图片"] },
  { category: "prompts", targetKey: "visual_note_user_prompt_template", title: "图文笔记 User Template", description: "控制图文笔记变量、结构和格式。", keywords: ["图文笔记", "template", "模板", "格式", "prompt"] },
  { category: "prompts", targetKey: "visual_frame_planning_prompt", title: "捕获帧规划 Prompt", description: "控制如何判断哪些时间点值得截图。", keywords: ["截图", "规划", "关键帧", "prompt"] },
  { category: "prompts", targetKey: "visual_vlm_prompt", title: "画面理解 Prompt", description: "控制 VLM 如何解析截图。", keywords: ["vlm", "画面理解", "ocr", "prompt"] },
  { category: "performance", targetKey: "task_concurrency", title: "任务并发数", description: "控制整体任务吞吐。", keywords: ["并发", "concurrency", "任务", "性能"] },
  { category: "performance", targetKey: "mindmap_concurrency", title: "导图并发数", description: "控制导图生成并发。", keywords: ["导图", "并发", "mindmap"] },
  { category: "performance", targetKey: "summary_chunk_concurrency", title: "摘要分块并发数", description: "控制单任务内部摘要请求并发。", keywords: ["摘要", "分块", "并发", "chunk"] },
  { category: "performance", targetKey: "cuda_variant", title: "CUDA 变体", description: "选择 PyTorch CUDA 版本。", keywords: ["cuda", "cu128", "cu126", "cu124", "gpu"] },
  { category: "performance", targetKey: "runtime_channel", title: "运行环境通道", description: "选择基础版或 GPU 运行环境。", keywords: ["runtime", "运行环境", "gpu", "base"] },
  { category: "video", targetKey: "preserve_temp_audio", title: "保留临时音频", description: "控制是否保留转写中间音频。", keywords: ["音频", "临时", "preserve", "temp"] },
  { category: "video", targetKey: "enable_cache", title: "启用缓存", description: "控制任务缓存行为。", keywords: ["缓存", "cache"] },
  { category: "video", targetKey: "ytdlp_cookies_file", title: "yt-dlp Cookies 文件", description: "配置 B 站登录态 cookies.txt。", keywords: ["cookie", "cookies", "b站", "登录", "风控", "412"] },
  { category: "runtime", targetKey: "runtime_status", title: "运行环境状态", description: "检查 Python、Torch、CUDA 与扩展依赖。", keywords: ["运行环境", "环境", "torch", "python", "cuda"] },
  { category: "runtime", targetKey: "local_asr_runtime", title: "本地 ASR 运行环境", description: "安装或检查本地 ASR 依赖。", keywords: ["本地", "asr", "whisper", "安装"] },
  { category: "runtime", targetKey: "funasr_runtime", title: "FunASR 运行环境", description: "安装或检查 FunASR 依赖（中文效果优于 Whisper）。", keywords: ["funasr", "qwen", "asr", "安装", "中文"] },
  { category: "runtime", targetKey: "knowledge_runtime", title: "知识库运行环境", description: "安装或检查知识库依赖（chromadb、sentence-transformers）。", keywords: ["知识库", "依赖", "chromadb", "sentence", "transformers", "向量"] },
  { category: "logs", targetKey: "service_logs", title: "服务日志", description: "查看后端服务日志。", keywords: ["日志", "log", "报错", "服务"] },
  { category: "updates", targetKey: "app_updates", title: "应用更新", description: "检查桌面应用新版本。", keywords: ["更新", "版本", "update", "release"] },
];

function formatStorageSize(sizeBytes: number) {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(sizeBytes) / Math.log(1024)), units.length - 1);
  const value = sizeBytes / (1024 ** exponent);
  return `${value >= 100 || exponent === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[exponent]}`;
}

function formatStorageCount(value: number, noun: string) {
  return `${new Intl.NumberFormat("zh-CN").format(Math.max(0, value))} ${noun}`;
}

function parseMinOneInt(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, parsed);
}

type RuntimeSectionTone = "installed" | "missing" | "error";

type RuntimeSectionProps = {
  title: string;
  status: string;
  tone: RuntimeSectionTone;
  summary: string;
  open: boolean;
  onToggle(): void;
  focusKey?: string;
  highlighted?: boolean;
  sectionRef?: (node: HTMLElement | null) => void;
  children: ReactNode;
};

function RuntimeSection({
  title,
  status,
  tone,
  summary,
  open,
  onToggle,
  focusKey,
  highlighted,
  sectionRef,
  children,
}: RuntimeSectionProps) {
  return (
    <section
      className={`runtime-subsection settings-focus-target ${highlighted ? "is-highlighted" : ""}`}
      data-focus-key={focusKey}
      ref={sectionRef}
    >
      <button
        className="runtime-subsection-header"
        type="button"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span className="runtime-subsection-chevron" aria-hidden="true">▶</span>
        <span className="runtime-subsection-heading">
          <span className="runtime-subsection-title">{title}</span>
          <span className="runtime-subsection-summary">{summary}</span>
        </span>
        <span className={`runtime-subsection-badge status-${tone}`}>{status}</span>
      </button>
      {open && <div className="runtime-subsection-content">{children}</div>}
    </section>
  );
}

export function SettingsPage({
  snapshot,
  desktop,
  focusIssueRequest,
  promptPresetRequest,
  onRefresh,
  onSettingsSaved,
  updateInfo,
  canCheckUpdate,
  canInstallUpdate,
  onCheckUpdate,
  onDownloadUpdate,
  onInstallUpdate,
  onOpenUpdateDialog,
  onOpenSetupAssistant,
}: SettingsPageProps) {
  const [form, setForm] = useState<ServiceSettings | null>(() => maskConfiguredApiKeys(snapshot.settings));
  const [environment, setEnvironment] = useState<EnvironmentInfo | null>(snapshot.environment);
  const [isDirty, setIsDirty] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState("");
  const [cudaStatus, setCudaStatus] = useState("");
  const [cudaOutput, setCudaOutput] = useState("");
  const [cudaInstalling, setCudaInstalling] = useState(false);
  const [cudaProgress, setCudaProgress] = useState(0);
  const [closeBehavior, setCloseBehavior] = useState<"tray" | "exit">("tray");
  const [autoLaunchEnabled, setAutoLaunchEnabled] = useState(false);
  const [silentStartEnabled, setSilentStartEnabled] = useState(false);
  const [crashAutoRestart, setCrashAutoRestart] = useState(false);
  const [developerModeEnabled, setDeveloperModeEnabled] = useState(false);
  const [logAutoRefresh, setLogAutoRefresh] = useState(false);
  const [logLevelFilter, setLogLevelFilter] = useState<"all" | "ERROR" | "WARNING" | "INFO">("all");
  const logTextareaRef = useRef<HTMLTextAreaElement>(null);

  // Load desktop preferences once
  useEffect(() => {
    void (async () => {
      const [cb, al, ss, cr, dm] = await Promise.all([
        window.desktop?.preferences?.getCloseBehavior(),
        window.desktop?.app?.getAutoLaunch(),
        window.desktop?.preferences?.getSilentStart(),
        window.desktop?.preferences?.getCrashAutoRestart(),
        window.desktop?.preferences?.getDeveloperMode(),
      ]);
      if (cb) setCloseBehavior(cb as "tray" | "exit");
      if (typeof al === "boolean") setAutoLaunchEnabled(al);
      if (typeof ss === "boolean") setSilentStartEnabled(ss);
      if (typeof cr === "boolean") setCrashAutoRestart(cr);
      if (typeof dm === "boolean") setDeveloperModeEnabled(dm);
    })();
  }, []);

  useEffect(() => {
    void fetchKnowledgeRequirements();
    setEmbeddingVerified(false);
    setEmbeddingStatus("");
    setEmbeddingOutput("");
  }, [form?.knowledge_embedding_provider]);

  useEffect(() => {
    setEmbeddingVerified(false);
    setEmbeddingStatus("");
    setEmbeddingOutput("");
  }, [
    form?.knowledge_embedding_model,
    form?.hf_endpoint,
    form?.siliconflow_embedding_api_key,
    form?.siliconflow_embedding_base_url,
    form?.siliconflow_embedding_model,
  ]);

  const [localAsrStatus, setLocalAsrStatus] = useState("");
  const [localAsrOutput, setLocalAsrOutput] = useState("");
  const [localAsrInstalling, setLocalAsrInstalling] = useState(false);
  const [funasrStatus, setFunasrStatus] = useState("");
  const [funasrOutput, setFunasrOutput] = useState("");
  const [funasrInstalling, setFunasrInstalling] = useState(false);
  const [bilibiliCookieCapturing, setBilibiliCookieCapturing] = useState(false);
  const [bilibiliCookieStatus, setBilibiliCookieStatus] = useState("");
  const [bilibiliQrcodeKey, setBilibiliQrcodeKey] = useState("");
  const [bilibiliQrcodeImage, setBilibiliQrcodeImage] = useState("");
  const [knowledgeDepsStatus, setKnowledgeDepsStatus] = useState("");
  const [knowledgeDepsOutput, setKnowledgeDepsOutput] = useState("");
  const [knowledgeDepsInstalling, setKnowledgeDepsInstalling] = useState(false);
  const [knowledgeRequirements, setKnowledgeRequirements] = useState<{ required: string[]; optional: string[]; preinstalled: string[] } | null>(null);
  const [embeddingDownloading, setEmbeddingDownloading] = useState(false);
  const [embeddingTesting, setEmbeddingTesting] = useState(false);
  const [embeddingStatus, setEmbeddingStatus] = useState("");
  const [embeddingOutput, setEmbeddingOutput] = useState("");
  const [embeddingVerified, setEmbeddingVerified] = useState(false);
  const [embeddingPresets, setEmbeddingPresets] = useState<Record<string, string>>({});
  const [embeddingCustomModel, setEmbeddingCustomModel] = useState("");
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);
  const [runtimeStatusMessage, setRuntimeStatusMessage] = useState("");
  const [runtimeStatusLoading, setRuntimeStatusLoading] = useState(false);
  const [runtimeSyncing, setRuntimeSyncing] = useState(false);
  const [logOutput, setLogOutput] = useState("");
  const [logPath, setLogPath] = useState(snapshot.systemInfo?.service?.log_file || desktop.logPath || "");
  const [serviceStatus, setServiceStatus] = useState("");
  const [asrTestStatus, setAsrTestStatus] = useState("");
  const [asrTestBusy, setAsrTestBusy] = useState(false);
  const [llmTestStatus, setLlmTestStatus] = useState("");
  const [llmTestNoticeVersion, setLlmTestNoticeVersion] = useState(0);
  const [llmTestBusy, setLlmTestBusy] = useState(false);
  const [modelAvailability, setModelAvailability] = useState<Record<GenerationModelScope, ModelAvailabilityState>>({
    main: { status: "unknown", message: "" },
    visual: { status: "unknown", message: "" },
  });
  const [storageOverview, setStorageOverview] = useState<StorageOverview | null>(null);
  const [storageLoading, setStorageLoading] = useState(false);
  const [storageCleaning, setStorageCleaning] = useState(false);
  const [storageStatus, setStorageStatus] = useState("");
  const [activeCategory, setActiveCategory] = useState<SettingsCategory>("overview");
  const [pendingFocusTarget, setPendingFocusTarget] = useState<string | null>(null);
  const [activeFocusTarget, setActiveFocusTarget] = useState<string | null>(null);
  const [knowledgePromptGuideOpen, setKnowledgePromptGuideOpen] = useState(false);
  const [generationModelDialog, setGenerationModelDialog] = useState<GenerationModelDialog>(null);
  const [settingsSearchQuery, setSettingsSearchQuery] = useState("");
  const [taskListOpen, setTaskListOpen] = useState(false);
  const [taskListLoading, setTaskListLoading] = useState(false);
  const [taskListError, setTaskListError] = useState("");
  const [taskList, setTaskList] = useState<TaskSummary[]>([]);
  const settingsNavRef = useRef<HTMLElement | null>(null);
  const settingsContentScrollRef = useRef<HTMLDivElement | null>(null);
  const promptDetailsRefs = useRef<Record<string, HTMLDetailsElement | null>>({});
  const focusTargetRefs = useRef<Record<string, HTMLElement | null>>({});
  const lastHandledExternalFocusNonce = useRef<number | null>(null);
  const lastHandledPromptPresetNonce = useRef<number | null>(null);
  const silentModelCheckRunId = useRef(0);
  const [promptPresets, setPromptPresets] = useState<PromptPreset[]>([]);
  const [hiddenPromptPresetIds, setHiddenPromptPresetIds] = useState<Set<string>>(() => loadHiddenPromptPresetIds());
  const [promptPresetsLoading, setPromptPresetsLoading] = useState(false);
  const [expandedPresetIds, setExpandedPresetIds] = useState<Set<string>>(new Set());
  const [showNewPresetForm, setShowNewPresetForm] = useState(false);
  const [presetForm, setPresetForm] = useState<PromptPresetCreateRequest>(() => emptyPresetForm());
  const [presetSaveBusy, setPresetSaveBusy] = useState(false);
  const [presetStatus, setPresetStatus] = useState("");
  const [presetDeleteConfirm, setPresetDeleteConfirm] = useState<string | null>(null);
  const [presetsSectionOpen, setPresetsSectionOpen] = useState(false);
  const [undoPromptValues, setUndoPromptValues] = useState<Record<string, string> | null>(null);
  const [localAsrSectionOpen, setLocalAsrSectionOpen] = useState(false);
  const [funasrSectionOpen, setFunasrSectionOpen] = useState(false);
  const [knowledgeSectionOpen, setKnowledgeSectionOpen] = useState(false);

  function emptyPresetForm(): PromptPresetCreateRequest {
    return { name: "", system_prompt: "", user_prompt_template: "", description: "", category: "", auto_match_keywords: [] };
  }

  function loadPromptPresets() {
    setPromptPresetsLoading(true);
    api.listPromptPresets().then((list) => setPromptPresets(list)).catch(() => {}).finally(() => setPromptPresetsLoading(false));
  }

  function togglePreset(presetId: string, preset: PromptPreset) {
    setExpandedPresetIds((prev) => {
      const next = new Set(prev);
      if (next.has(presetId)) {
        next.delete(presetId);
      } else {
        next.add(presetId);
        setPresetForm({
          name: preset.name,
          system_prompt: preset.system_prompt,
          user_prompt_template: preset.user_prompt_template,
          description: preset.description || "",
          category: preset.category || "",
          auto_match_keywords: preset.auto_match_keywords || [],
        });
        setShowNewPresetForm(false);
        setPresetStatus("");
        setPresetDeleteConfirm(null);
      }
      return next;
    });
  }

  function startNewPreset() {
    setPresetForm(emptyPresetForm());
    setShowNewPresetForm(true);
    setPresetStatus("");
  }

  function cancelPresetEdit() {
    setExpandedPresetIds(new Set());
    setShowNewPresetForm(false);
    setPresetForm(emptyPresetForm());
    setPresetStatus("");
    setPresetDeleteConfirm(null);
  }

  function closeOnePreset(presetId: string) {
    setExpandedPresetIds((prev) => {
      const next = new Set(prev);
      next.delete(presetId);
      return next;
    });
  }

  function openPromptPresetInSettings(presetId: string) {
    const targetPreset = promptPresets.find((preset) => preset.id === presetId);
    setActiveCategory("prompts");
    setPresetsSectionOpen(true);
    setShowNewPresetForm(false);
    setPresetDeleteConfirm(null);
    setSettingsSearchQuery("");
    setPresetStatus(targetPreset ? `已定位到「${targetPreset.name}」` : "已打开 Prompt 预设库");
    if (targetPreset) {
      setExpandedPresetIds(new Set([targetPreset.id]));
      setPresetForm({
        name: targetPreset.name,
        system_prompt: targetPreset.system_prompt,
        user_prompt_template: targetPreset.user_prompt_template,
        description: targetPreset.description || "",
        category: targetPreset.category || "",
        auto_match_keywords: targetPreset.auto_match_keywords || [],
      });
    }
    window.setTimeout(() => {
      focusTargetRefs.current.prompt_presets_library?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }

  function savePreset() {
    if (!presetForm.name.trim() || !presetForm.system_prompt.trim() || !presetForm.user_prompt_template.trim()) return;
    setPresetSaveBusy(true);
    setPresetStatus("保存中...");
    api.createPromptPreset(presetForm).then(() => {
      setPresetStatus("保存成功");
      setShowNewPresetForm(false);
      setExpandedPresetIds(new Set());
      loadPromptPresets();
    }).catch((err) => {
      setPresetStatus("保存失败: " + (err?.message || String(err)));
    }).finally(() => setPresetSaveBusy(false));
  }

  function deletePreset(presetId: string) {
    setPresetSaveBusy(true);
    setPresetStatus("删除中...");
    api.deletePromptPreset(presetId).then(() => {
      setPresetStatus("删除成功");
      setPresetDeleteConfirm(null);
      setExpandedPresetIds(new Set());
      setShowNewPresetForm(false);
      loadPromptPresets();
    }).catch((err) => {
      setPresetStatus("删除失败: " + (err?.message || String(err)));
    }).finally(() => setPresetSaveBusy(false));
  }

  function collapseAllPrompts() {
    setExpandedPresetIds(new Set());
    setShowNewPresetForm(false);
    setPresetDeleteConfirm(null);
  }

  function collapseAllAndCloseSection() {
    collapseAllPrompts();
    setPresetsSectionOpen(false);
  }

  const builtinPresetCount = promptPresets.filter((p) => p.is_builtin).length;
  const hiddenBuiltinPresetCount = promptPresets.filter((p) => p.is_builtin && hiddenPromptPresetIds.has(p.id)).length;
  const customPresetCount = promptPresets.length - builtinPresetCount;
  const visiblePresets = promptPresets.filter((p) => !p.is_builtin || !hiddenPromptPresetIds.has(p.id));
  const hiddenBuiltinPresets = promptPresets.filter((p) => p.is_builtin && hiddenPromptPresetIds.has(p.id));


  function setBuiltinPresetHidden(presetId: string, hidden: boolean) {
    setHiddenPromptPresetIds((prev) => {
      const next = new Set(prev);
      if (hidden) {
        next.add(presetId);
        closeOnePreset(presetId);
      } else {
        next.delete(presetId);
      }
      persistHiddenPromptPresetIds(next);
      return next;
    });
  }

  function renderPresetCard(preset: PromptPreset) {
    const isExpanded = expandedPresetIds.has(preset.id);
    return (
      <div key={preset.id} className={`settings-preset-card${preset.is_builtin ? " builtin" : ""}`}>
        <div className="settings-preset-card-header" onClick={() => togglePreset(preset.id, preset)}>
          <span className="settings-preset-name">
            {preset.name}
            {preset.is_builtin && <span className="settings-preset-badge builtin">内置</span>}
          </span>
          <span className="settings-preset-meta">
            {preset.category && <span className="settings-preset-category">{preset.category}</span>}
            <span className="settings-preset-keywords">{preset.auto_match_keywords?.join("、") || "无匹配关键词"}</span>
          </span>
          <span className="settings-preset-expand-hint">{isExpanded ? "收起" : preset.is_builtin ? "查看内容" : "编辑"}</span>
          {preset.is_builtin && (
            <button
              className="settings-preset-hide-button"
              type="button"
              onClick={(event) => { event.stopPropagation(); setBuiltinPresetHidden(preset.id, true); }}
            >
              隐藏
            </button>
          )}
        </div>
        {(isExpanded && !preset.is_builtin) && (
          <div className="settings-preset-edit-body">
            {presetDeleteConfirm === preset.id ? (
              <div className="settings-preset-delete-confirm">
                <strong>确定删除「{preset.name}」？</strong>
                <button className="secondary-button" type="button" onClick={() => setPresetDeleteConfirm(null)}>取消</button>
                <button className="primary-button danger" type="button" disabled={presetSaveBusy} onClick={() => deletePreset(preset.id)}>
                  {presetSaveBusy ? "删除中..." : "确认删除"}
                </button>
              </div>
            ) : (
              <>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">名称</span>
                  <input className="settings-input-field" value={presetForm.name} onChange={(e) => setPresetForm({ ...presetForm, name: e.target.value })} />
                </label>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">描述</span>
                  <input className="settings-input-field" value={presetForm.description || ""} onChange={(e) => setPresetForm({ ...presetForm, description: e.target.value })} />
                </label>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">分类</span>
                  <input className="settings-input-field" value={presetForm.category || ""} onChange={(e) => setPresetForm({ ...presetForm, category: e.target.value })} placeholder="例如: 教程、会议、娱乐" />
                </label>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">自动匹配关键词（逗号分隔）</span>
                  <input className="settings-input-field" value={presetForm.auto_match_keywords?.join("、") || ""} onChange={(e) => setPresetForm({ ...presetForm, auto_match_keywords: e.target.value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean) })} placeholder="例如: 教程、教学、入门、实操" />
                </label>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">System Prompt</span>
                  <textarea className="textarea-field" rows={4} value={presetForm.system_prompt} onChange={(e) => setPresetForm({ ...presetForm, system_prompt: e.target.value })} />
                </label>
                <label className="settings-input-group settings-preset-field">
                  <span className="settings-input-label">User Template</span>
                  <textarea className="textarea-field" rows={10} value={presetForm.user_prompt_template} onChange={(e) => setPresetForm({ ...presetForm, user_prompt_template: e.target.value })} />
                  <span className="settings-input-caption">可用变量：{"{title}"}、{"{transcript}"}、{"{segments_json}"}。</span>
                </label>
                <div className="settings-preset-actions">
                  <button className="primary-button" type="button" disabled={presetSaveBusy} onClick={savePreset}>
                    {presetSaveBusy ? "保存中..." : "保存修改"}
                  </button>
                  <button className="secondary-button" type="button" onClick={() => closeOnePreset(preset.id)}>收起</button>
                  <button className="secondary-button danger" type="button" onClick={() => setPresetDeleteConfirm(preset.id)}>删除</button>
                </div>
              </>
            )}
          </div>
        )}
        {(isExpanded && preset.is_builtin) && (
          <div className="settings-preset-view-body">
            <p className="settings-preset-desc">{preset.description || "（无描述）"}</p>
            <pre className="settings-preset-preview"><strong>System:</strong>{"\n"}{preset.system_prompt}{"\n\n"}<strong>User Template:</strong>{"\n"}{preset.user_prompt_template}</pre>
            <button className="secondary-button" type="button" onClick={() => closeOnePreset(preset.id)}>收起</button>
          </div>
        )}
      </div>
    );
  }

  // Track outermost <details> open state for sticky header
  const [outerSectionsOpen, setOuterSectionsOpen] = useState<Set<string>>(new Set());
  const hasOuterSectionsOpen = outerSectionsOpen.size > 0;

  function collapseAllOuter() {
    Object.values(promptDetailsRefs.current).forEach((node) => {
      if (node) {
        node.open = false;
      }
    });
    setOuterSectionsOpen(new Set());
    setPresetsSectionOpen(false);
    collapseAllPrompts();
  }

  function scrollSettingsToTop() {
    const target = settingsContentScrollRef.current;
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.scrollTo({ top: 0, behavior: "smooth" });
    target?.closest(".settings-content")?.scrollTo({ top: 0, behavior: "smooth" });
    document.querySelector(".app-main")?.scrollTo({ top: 0, behavior: "smooth" });
    document.querySelector(".app-content")?.scrollTo({ top: 0, behavior: "smooth" });
    document.scrollingElement?.scrollTo({ top: 0, behavior: "smooth" });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function handleOuterToggle(name: string, e: React.SyntheticEvent<HTMLDetailsElement>) {
    const open = (e.target as HTMLDetailsElement).open;
    setOuterSectionsOpen((prev) => {
      const next = new Set(prev);
      if (open) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  function handlePresetsSectionToggle() {
    setPresetsSectionOpen((v) => {
      const next = !v;
      setOuterSectionsOpen((prev) => {
        const n = new Set(prev);
        if (next) n.add("presets");
        else n.delete("presets");
        return n;
      });
      return next;
    });
  }

  useLayoutEffect(() => {
    const node = settingsNavRef.current;
    if (!node) {
      return;
    }

    const updateStickyTop = () => {
      const bottomGap = 24;
      const topGap = 24;
      const stickyTop = Math.min(topGap, window.innerHeight - node.offsetHeight - bottomGap);
      node.style.setProperty("--settings-nav-sticky-top", `${Math.round(stickyTop)}px`);
    };

    updateStickyTop();
    const observer = new ResizeObserver(updateStickyTop);
    observer.observe(node);
    window.addEventListener("resize", updateStickyTop);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateStickyTop);
    };
  }, []);

  useEffect(() => {
    if (isDirty) {
      return;
    }
    setForm(maskConfiguredApiKeys(snapshot.settings));
  }, [isDirty, snapshot.settings]);

  useEffect(() => {
    setEnvironment(snapshot.environment);
  }, [snapshot.environment]);

  useEffect(() => {
    setLogPath(snapshot.systemInfo?.service?.log_file || desktop.logPath || "");
  }, [desktop.logPath, snapshot.systemInfo?.service?.log_file]);

  useEffect(() => {
    void refreshLogs();
    void refreshRuntimeStatus({ silent: true });
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshRuntimeStatus({ silent: true });
    }, 30 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (activeCategory === "prompts") {
      loadPromptPresets();
    }
  }, [activeCategory]);

  useEffect(() => {
    if (!bilibiliQrcodeKey) {
      return;
    }
    let cancelled = false;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const result = await api.pollBilibiliCookieQrcode(bilibiliQrcodeKey);
          if (cancelled) {
            return;
          }
          if (result.status === "pending") {
            setBilibiliCookieStatus("等待手机 B 站扫码。");
            return;
          }
          if (result.status === "scanned") {
            setBilibiliCookieStatus("已扫码，请在手机 B 站上确认登录。");
            return;
          }
          if (result.status === "expired") {
            setBilibiliCookieStatus(result.message || "二维码已过期，请重新获取。");
            setBilibiliQrcodeKey("");
            setBilibiliQrcodeImage("");
            return;
          }
          if (result.status === "confirmed" && result.cookiesFile) {
            const response = await api.updateSettings({
              ytdlp_cookies_file: result.cookiesFile,
              ytdlp_cookies_browser: "",
            });
            if (cancelled) {
              return;
            }
            setForm(maskConfiguredApiKeys(response.settings));
            setIsDirty(false);
            setSaveStatus(response.message || "设置已保存");
            setBilibiliCookieStatus(`B 站登录态已保存，捕获 ${result.cookieCount || 0} 条 cookies。`);
            setBilibiliQrcodeKey("");
            setBilibiliQrcodeImage("");
            onSettingsSaved(response.settings, environment);
          }
        } catch (error) {
          if (!cancelled) {
            setBilibiliCookieStatus(error instanceof Error ? error.message : "二维码登录状态检查失败。");
          }
        }
      })();
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [bilibiliQrcodeKey, environment, onSettingsSaved]);

  async function refreshLogs() {
    try {
      const response = await api.getSystemLogs();
      setLogOutput(response.content || "日志文件存在，但当前还没有内容。");
      setLogPath(response.path || snapshot.systemInfo?.service?.log_file || desktop.logPath || "");
    } catch (error) {
      try {
        const fallback = await window.desktop?.logs.readServiceLogTail(200);
        if (fallback) {
          setLogOutput(fallback.content || "本地日志文件存在，但当前还没有内容。");
          setLogPath(fallback.path || snapshot.systemInfo?.service?.log_file || desktop.logPath || "");
          setServiceStatus("服务接口不可用，已切换为本地日志读取。");
          return;
        }
      } catch {}
      setLogOutput(error instanceof Error ? error.message : "读取日志失败");
    }
  }

  // Auto-refresh logs every 5s
  useEffect(() => {
    if (!logAutoRefresh) return;
    const timer = setInterval(() => { void refreshLogs(); }, 5000);
    return () => clearInterval(timer);
  }, [logAutoRefresh]);

  // Auto-scroll log textarea to bottom on update
  useEffect(() => {
    const el = logTextareaRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logOutput]);

  const filteredLogOutput = logLevelFilter === "all"
    ? logOutput
    : logOutput.split("\n").filter((line) => line.includes(logLevelFilter)).join("\n");

  // Auto-scroll log textarea to bottom on update
  useEffect(() => {
    const el = logTextareaRef.current;
    if (!el) return;
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
  }, [filteredLogOutput]);

  async function refreshRuntimeStatus(options: { silent?: boolean } = {}) {
    try {
      setRuntimeStatusLoading(true);
      if (!options.silent) {
        setRuntimeStatusMessage("正在检查所有运行环境...");
      }
      const status = await api.getRuntimeStatus();
      setRuntimeStatus(status);
      if (form?.runtime_channel) {
        const nextEnvironment = await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true });
        setEnvironment(nextEnvironment);
        onSettingsSaved(form, nextEnvironment);
      }
      const outdatedCount = status.channels.filter((channel) => channel.needsUpdate).length;
      if (!options.silent) {
        setRuntimeStatusMessage(outdatedCount > 0 ? `${outdatedCount} 个运行环境需要同步基础版本。` : "所有已安装运行环境均为最新基础版本。");
      }
    } catch (error) {
      if (!options.silent) {
        setRuntimeStatusMessage(error instanceof Error ? error.message : "运行环境检查失败");
      }
    } finally {
      setRuntimeStatusLoading(false);
    }
  }

  async function syncRuntimeChannels() {
    if (!form || runtimeSyncing) {
      return;
    }
    try {
      setRuntimeSyncing(true);
      setRuntimeStatusMessage("正在同步需要更新的运行环境...");
      const response = await api.syncRuntime();
      if (response.runtimeStatus) {
        setRuntimeStatus(response.runtimeStatus);
      } else {
        await refreshRuntimeStatus({ silent: true });
      }
      const nextEnvironment = response.environment || (await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true }));
      setEnvironment(nextEnvironment);
      onSettingsSaved(form, nextEnvironment);
      const syncedCount = response.channels?.filter((channel) => channel.synced).length ?? 0;
      setRuntimeStatusMessage(syncedCount > 0 ? `已同步 ${syncedCount} 个运行环境，保留 CUDA / ASR / 知识库扩展包。` : "运行环境已检查，无需同步。");
      onRefresh();
    } catch (error) {
      setRuntimeStatusMessage(error instanceof Error ? error.message : "运行环境同步失败");
    } finally {
      setRuntimeSyncing(false);
    }
  }

  const refreshStorageOverview = useCallback(async () => {
    if (!form || !window.desktop?.fileManager) {
      setStorageStatus("当前环境不支持文件管理。");
      return;
    }
    try {
      setStorageLoading(true);
      setStorageStatus("");
      let taskIds: string[] | undefined;
      if (snapshot.serviceOnline) {
        try {
          taskIds = (await api.listTasks()).map((task) => task.task_id);
        } catch {
          taskIds = undefined;
        }
      }
      const overview = await window.desktop.fileManager.getStorageOverview({ taskIds });
      setStorageOverview(overview);
      if (!taskIds) {
        setStorageStatus("服务离线：已展示本地占用情况，清理操作需要在服务在线时确认引用关系。");
      }
    } catch (error) {
      setStorageStatus(error instanceof Error ? error.message : "读取文件占用失败");
    } finally {
      setStorageLoading(false);
    }
  }, [form, snapshot.serviceOnline]);

  async function openManagedDirectory(kind: StorageLocationKind) {
    if (!form || !window.desktop?.fileManager) {
      return;
    }
    await window.desktop.fileManager.openDirectory(kind);
  }

  async function cleanupManagedFiles() {
    if (!form || !window.desktop?.fileManager || !serviceOnline) {
      return;
    }
    try {
      setStorageCleaning(true);
      setStorageStatus("");
      const taskIds = (await api.listTasks()).map((task) => task.task_id);
      const preview = await window.desktop.fileManager.getStorageOverview({ taskIds });
      const targetCount = preview.cleanup.orphanTaskCount + preview.cleanup.cacheCandidateCount;
      const targetBytes = preview.cleanup.orphanTaskBytes + preview.cleanup.cacheCandidateBytes;
      if (targetCount <= 0) {
        setStorageOverview(preview);
        setStorageStatus("当前没有可安全清理的缓存或孤儿文件。");
        return;
      }
      const confirmed = window.confirm(
        `将删除 ${targetCount} 项可回收内容，预计释放 ${formatStorageSize(targetBytes)}。\n\n包含：${preview.cleanup.orphanTaskCount} 个孤儿任务目录、${preview.cleanup.cacheCandidateCount} 个缓存项。\n此操作不会删除仍被引用的任务结果。`,
      );
      if (!confirmed) {
        setStorageOverview(preview);
        setStorageStatus("已取消清理。");
        return;
      }
      const result = await window.desktop.fileManager.cleanupOrphans({ taskIds });
      await refreshStorageOverview();
      setStorageStatus(`已清理 ${result.deletedCount} 项内容，释放约 ${formatStorageSize(result.reclaimedBytes)}。`);
    } catch (error) {
      setStorageStatus(error instanceof Error ? error.message : "清理文件失败");
    } finally {
      setStorageCleaning(false);
    }
  }

  async function refreshTaskList() {
    try {
      setTaskListLoading(true);
      setTaskListError("");
      const tasks = await api.listTasks();
      setTaskList(tasks.slice(0, TASK_LIST_LIMIT));
    } catch (error) {
      setTaskListError(error instanceof Error ? error.message : "读取任务列表失败");
    } finally {
      setTaskListLoading(false);
    }
  }

  const backendRunning = Boolean(desktop.backend?.running);
  const backendReady = Boolean(desktop.backend?.ready);
  const serviceOnline = snapshot.serviceOnline;
  const effectiveLogPath = logPath || snapshot.systemInfo?.service?.log_file || desktop.logPath || "-";
  const targetRuntimeChannel = `gpu-${form?.cuda_variant || "cu128"}`;
  const activeCategoryMeta = settingsCategories.find((category) => category.id === activeCategory) || settingsCategories[0];
  const workflowCategories = settingsCategories.filter((category) => category.group === "workflow");
  const systemCategories = settingsCategories.filter((category) => category.group === "system");
  const normalizedSettingsSearchQuery = settingsSearchQuery.trim().toLowerCase();
  const settingsSearchTokens = normalizedSettingsSearchQuery.split(/\s+/).filter(Boolean);
  const settingsSearchResults = normalizedSettingsSearchQuery
    ? SETTINGS_SEARCH_ITEMS
      .map((item) => {
        const category = settingsCategories.find((entry) => entry.id === item.category);
        const haystack = [
          item.title,
          item.description,
          category?.label || "",
          category?.description || "",
          ...item.keywords,
        ].join(" ").toLowerCase();
        return settingsSearchTokens.every((token) => haystack.includes(token)) ? { ...item, categoryLabel: category?.label || item.category } : null;
      })
      .filter((item): item is SettingsSearchItem & { categoryLabel: string } => Boolean(item))
      .slice(0, 8)
    : [];
  const llmApiKeyReady = hasUsableApiKey(form?.llm_api_key, form?.llm_api_key_configured);
  const visualApiKeyReady = hasUsableApiKey(form?.visual_evidence_api_key, form?.visual_evidence_api_key_configured) || llmApiKeyReady;
  const knowledgeLlmApiKeyReady = hasUsableApiKey(form?.knowledge_llm_api_key, form?.knowledge_llm_api_key_configured);
  const siliconflowEmbeddingApiKeyReady = hasUsableApiKey(form?.siliconflow_embedding_api_key, form?.siliconflow_embedding_api_key_configured);
  const knowledgeEmbeddingProvider = String(form?.knowledge_embedding_provider || "local_huggingface").trim().toLowerCase();
  const knowledgeEmbeddingUsesSiliconFlow = knowledgeEmbeddingProvider === "siliconflow";
  const llmEnabled = Boolean(form?.llm_enabled);
  const visualMultimodalEnabled = Boolean(form?.visual_multimodal_enabled);
  const llmReady = Boolean(form?.llm_enabled && llmApiKeyReady && String(form?.llm_base_url || "").trim() && String(form?.llm_model || "").trim());
  const visualLlmReady = Boolean(
    form?.visual_multimodal_enabled
    && visualApiKeyReady
    && String(form?.visual_evidence_base_url || form?.llm_base_url || "").trim()
    && String(form?.visual_evidence_model || form?.llm_model || "").trim(),
  );
  const mainModelAvailability = modelAvailability.main;
  const visualModelAvailability = modelAvailability.visual;
  const mainModelStatusLabel = mainModelAvailability.status === "available"
    ? "可用"
    : mainModelAvailability.status === "unavailable"
      ? "不可用"
      : mainModelAvailability.status === "checking"
        ? "检查中"
        : llmReady
          ? "可用"
          : llmEnabled
            ? "待配置"
            : "关闭";
  const mainModelStatusClass = mainModelAvailability.status === "available" || (mainModelAvailability.status === "unknown" && llmReady)
    ? "success"
    : mainModelAvailability.status === "unavailable"
      ? "danger"
      : mainModelAvailability.status === "checking" || llmEnabled
        ? "warning"
        : "";
  const mainModelSummary = llmEnabled
    ? mainModelAvailability.status === "unavailable"
      ? "不可用"
      : llmReady
        ? "已配置"
        : "待补全"
    : "未启用";
  const visualModelStatusLabel = visualModelAvailability.status === "available"
    ? "可用"
    : visualModelAvailability.status === "unavailable"
      ? "不可用"
      : visualModelAvailability.status === "checking"
        ? "检查中"
        : visualLlmReady
          ? "可用"
          : visualMultimodalEnabled
            ? "待确认"
            : "关闭";
  const visualModelStatusClass = visualModelAvailability.status === "available" || (visualModelAvailability.status === "unknown" && visualLlmReady)
    ? "success"
    : visualModelAvailability.status === "unavailable"
      ? "danger"
      : visualModelAvailability.status === "checking" || visualMultimodalEnabled
        ? "warning"
        : "";
  const visualModelSummary = visualMultimodalEnabled
    ? visualModelAvailability.status === "unavailable"
      ? "不可用"
      : visualLlmReady
        ? "已配置"
        : "跟随或待补全"
    : "未启用";
  const visualNotePreset = (() => {
    const mode = form?.visual_note_mode || "text";
    if (mode === "vlm_integrated") {
      return "multimodal";
    }
    if (mode === "frame_insert") {
      return "visual";
    }
    return "text";
  })();
  const knowledgeLlmUsesCustom = String(form?.knowledge_llm_mode || "same_as_main").trim().toLowerCase() === "custom";
  const knowledgeLlmReady = knowledgeLlmUsesCustom
    ? Boolean(form?.knowledge_llm_enabled && knowledgeLlmApiKeyReady && String(form?.knowledge_llm_base_url || "").trim() && String(form?.knowledge_llm_model || "").trim())
    : Boolean(form?.llm_enabled && llmApiKeyReady && String(form?.llm_base_url || "").trim() && String(form?.llm_model || "").trim());
  const autoMindMapReady = Boolean(form?.auto_generate_mindmap);
  const currentVersion = desktop.version || snapshot.systemInfo?.application?.version || "-";
  const asrReady =
    form?.transcription_provider === "local"
      ? Boolean(environment?.localAsrAvailable)
      : form?.transcription_provider === "funasr"
        ? Boolean(environment?.funasrAvailable)
        : form?.transcription_provider === "multimodal"
          ? Boolean(form?.multimodal_asr_api_key_configured && String(form?.multimodal_asr_base_url || "").trim() && String(form?.multimodal_asr_model || "").trim())
          : Boolean(form?.siliconflow_asr_api_key_configured);
  const updateUnsupported = isUpdateUnsupported(updateInfo);
  const updateStatusLabel = getUpdateStatusLabel(updateInfo);
  const updateStatusTone = getUpdateStatusTone(updateInfo);
  const updateSummary = getUpdateSummary(updateInfo, currentVersion);
  const updateActionBusy = updateInfo.status === "checking" || updateInfo.status === "downloading" || updateInfo.status === "installing";

  useEffect(() => {
    if (!form || activeCategory !== "files") {
      return;
    }
    // 使用 setTimeout 延迟执行，避免阻塞 UI 渲染
    const timer = window.setTimeout(() => {
      void refreshStorageOverview();
    }, 100);
    return () => window.clearTimeout(timer);
  }, [activeCategory, refreshStorageOverview]);

  useEffect(() => {
    if (!focusIssueRequest || !form) {
      return;
    }
    if (lastHandledExternalFocusNonce.current === focusIssueRequest.nonce) {
      return;
    }
    lastHandledExternalFocusNonce.current = focusIssueRequest.nonce;
    const target = resolveIssueTarget(focusIssueRequest.issueKey);
    if (target) {
      setActiveCategory(target.category);
      setPendingFocusTarget(target.targetKey);
    }
  }, [focusIssueRequest, form]);

  useEffect(() => {
    if (!promptPresetRequest || !form) {
      return;
    }
    if (lastHandledPromptPresetNonce.current === promptPresetRequest.nonce) {
      return;
    }
    lastHandledPromptPresetNonce.current = promptPresetRequest.nonce;
    openPromptPresetInSettings(promptPresetRequest.presetId);
  }, [promptPresetRequest, form, promptPresets]);

  useEffect(() => {
    if (!pendingFocusTarget) {
      return;
    }
    const targetNode = focusTargetRefs.current[pendingFocusTarget];
    if (!targetNode) {
      return;
    }
    const timer = window.setTimeout(() => {
      targetNode.scrollIntoView({ behavior: "smooth", block: "center" });
      const focusable = targetNode.matches("input, select, textarea, button")
        ? targetNode
        : targetNode.querySelector("input, select, textarea, button");
      if (focusable instanceof HTMLElement) {
        focusable.focus({ preventScroll: true });
      }
      setActiveFocusTarget(pendingFocusTarget);
      setPendingFocusTarget(null);
    }, 80);
    return () => window.clearTimeout(timer);
  }, [activeCategory, pendingFocusTarget]);

  useEffect(() => {
    if (!activeFocusTarget) {
      return;
    }
    const timer = window.setTimeout(() => setActiveFocusTarget((current) => (current === activeFocusTarget ? null : current)), 2200);
    return () => window.clearTimeout(timer);
  }, [activeFocusTarget]);

  useEffect(() => {
    if (!taskListOpen) {
      return;
    }
    void refreshTaskList();
  }, [taskListOpen]);

  // Must be called before any conditional return (React hooks rules).
  const funasrInstalled = Boolean(environment?.funasrInstalled);
  useEffect(() => {
    if (!funasrInstalled && form?.transcription_provider === "funasr") {
      setForm({ ...form, transcription_provider: "siliconflow" });
    }
  }, [funasrInstalled]);

  // Load embedding presets on mount
  useEffect(() => {
    api.getEmbeddingPresets().then(r => setEmbeddingPresets(r.presets || {})).catch(() => {});
  }, []);

  if (!form) return <section className="grid-card empty-state-card">正在加载设置...</section>;

  const usesSiliconFlowAsr = form.transcription_provider === "siliconflow";
  const usesMultimodalAsr = form.transcription_provider === "multimodal";
  const usesFunAsr = form.transcription_provider === "funasr";
  const recommendedTaskConcurrency = form.transcription_provider === "local" ? 1 : 2;
  const performanceRecommendation = recommendedTaskConcurrency === 1
    ? "当前建议：本地 ASR / CPU 场景任务并发数设为 1，导图并发数设为 1。"
    : "当前建议：云 ASR 或 GPU 场景任务并发数设为 2，导图并发数设为 1。";
  const queuedTaskCount = taskList.filter((task) => task.status === "queued").length;
  const runningTaskCount = taskList.filter((task) => task.status === "running").length;
  const localAsrInstalled = Boolean(environment?.localAsrInstalled);

  const knowledgeDepsReady = Boolean(environment?.knowledgeDependenciesReady);
  const effectiveSiliconFlowEmbeddingBaseUrl = String(form?.siliconflow_embedding_base_url || DEFAULT_SILICONFLOW_EMBEDDING_BASE_URL).trim();
  const effectiveSiliconFlowEmbeddingModel = String(form?.siliconflow_embedding_model || DEFAULT_SILICONFLOW_EMBEDDING_MODEL).trim();
  const knowledgeEmbeddingReady = knowledgeEmbeddingUsesSiliconFlow
    ? Boolean(
        siliconflowEmbeddingApiKeyReady &&
        effectiveSiliconFlowEmbeddingBaseUrl &&
        effectiveSiliconFlowEmbeddingModel
      )
    : knowledgeDepsReady;
  const hasKnowledgeBrokenDeps = Boolean(
    environment?.chromadbBroken ||
    environment?.sentenceTransformersBroken ||
    environment?.modelscopeBroken
  );
  const requiredKnowledgePackages = knowledgeRequirements?.required?.length
    ? knowledgeRequirements.required
    : knowledgeEmbeddingUsesSiliconFlow
      ? ["chromadb"]
      : ["chromadb", "sentence-transformers"];
  const requiredKnowledgePackageText = requiredKnowledgePackages.join(" / ");
  const missingKnowledgeDeps = requiredKnowledgePackages.filter((packageName) => {
    if (packageName === "chromadb") {
      return !environment?.chromadbInstalled && !environment?.chromadbBroken;
    }
    if (packageName === "sentence-transformers") {
      return !environment?.sentenceTransformersInstalled && !environment?.sentenceTransformersBroken;
    }
    if (packageName === "modelscope") {
      return !environment?.modelscopeInstalled && !environment?.modelscopeBroken;
    }
    return false;
  });
  const knowledgeRuntimeStatus = knowledgeDepsReady ? "已安装" : hasKnowledgeBrokenDeps ? "部分损坏" : "未安装";
  const knowledgeRuntimeTone = knowledgeDepsReady ? "installed" : hasKnowledgeBrokenDeps ? "error" : "missing";
  const knowledgeRuntimeSummary = knowledgeDepsReady
    ? (knowledgeEmbeddingUsesSiliconFlow ? "chromadb 索引存储已就绪" : "向量库和本地模型依赖已就绪")
    : hasKnowledgeBrokenDeps
      ? "检测到包导入异常"
      : `安装 ${requiredKnowledgePackageText}`;
  const outdatedRuntimeChannels = runtimeStatus?.channels.filter((channel) => channel.needsUpdate) || [];
  const activeRuntimeStatus = runtimeStatus?.channels.find(
    (channel) => channel.runtimeChannel === (environment?.runtimeChannel || form.runtime_channel),
  ) || null;
  const pipIndexSummary = runtimeStatus?.pipIndexes.map((item) => item.label).join(" / ") || "official / tsinghua / aliyun";
  const storageDirectoryMap = new Map((storageOverview?.directories || []).map((entry) => [entry.key, entry]));
  const cacheDirectory = storageDirectoryMap.get("cache") || null;
  const tasksDirectory = storageDirectoryMap.get("tasks") || null;
  const logsDirectory = storageDirectoryMap.get("logs") || null;
  const runtimeDirectory = storageDirectoryMap.get("runtime") || null;
  const cleanupReady = Boolean(serviceOnline && storageOverview?.cleanup.serviceAvailable);
  const cleanupTargetBytes = (storageOverview?.cleanup.orphanTaskBytes || 0) + (storageOverview?.cleanup.cacheCandidateBytes || 0);
  const cleanupTargetCount = (storageOverview?.cleanup.orphanTaskCount || 0) + (storageOverview?.cleanup.cacheCandidateCount || 0);

  function handleConfigIssueClick(issueKey: string) {
    const target = resolveIssueTarget(issueKey);
    if (!target) return;
    setActiveCategory(target.category);
    setPendingFocusTarget(target.targetKey);
  }

  function updateForm(next: ServiceSettings) {
    setIsDirty(true);
    setForm(next);
    setModelAvailability({
      main: { status: "unknown", message: "" },
      visual: { status: "unknown", message: "" },
    });
  }

  function updateUndoPromptValues(oldValues: Record<string, string>) {
    if (Object.keys(oldValues).length === 0) {
      setUndoPromptValues(null);
      return;
    }
    setUndoPromptValues((prev) => ({ ...(prev || {}), ...oldValues }));
  }

  function undoPromptReset() {
    if (!form || !undoPromptValues) return;
    updateForm({ ...form, ...undoPromptValues });
    setUndoPromptValues(null);
  }

  function resetSummaryPrompt(field: "system" | "template") {
    if (!form) return;
    const defaultSystemPrompt = form.defaults?.summary_system_prompt || "";
    const defaultUserTemplate = form.defaults?.summary_user_prompt_template || "";
    const newSystemPrompt = field === "system" ? (defaultSystemPrompt || form.summary_system_prompt) : form.summary_system_prompt;
    const newUserTemplate = field === "template" ? (defaultUserTemplate || form.summary_user_prompt_template) : form.summary_user_prompt_template;
    const oldValues: Record<string, string> = {};
    if (field === "system" && form.summary_system_prompt !== newSystemPrompt) {
      oldValues.summary_system_prompt = form.summary_system_prompt || "";
    }
    if (field === "template" && form.summary_user_prompt_template !== newUserTemplate) {
      oldValues.summary_user_prompt_template = form.summary_user_prompt_template || "";
    }
    updateUndoPromptValues(oldValues);
    updateForm({
      ...form,
      summary_system_prompt: newSystemPrompt,
      summary_user_prompt_template: newUserTemplate,
    });
  }

  function resetKnowledgeNotePrompt(field: "system" | "template") {
    if (!form) return;
    const defaultSystemPrompt = form.defaults?.knowledge_note_system_prompt || form.knowledge_note_system_prompt;
    const defaultUserTemplate = form.defaults?.knowledge_note_user_prompt_template || form.knowledge_note_user_prompt_template;
    const newSystemPrompt = field === "system" ? defaultSystemPrompt : form.knowledge_note_system_prompt;
    const newUserTemplate = field === "template" ? defaultUserTemplate : form.knowledge_note_user_prompt_template;
    const oldValues: Record<string, string> = {};
    if (field === "system" && form.knowledge_note_system_prompt !== newSystemPrompt) {
      oldValues.knowledge_note_system_prompt = form.knowledge_note_system_prompt || "";
    }
    if (field === "template" && form.knowledge_note_user_prompt_template !== newUserTemplate) {
      oldValues.knowledge_note_user_prompt_template = form.knowledge_note_user_prompt_template || "";
    }
    updateUndoPromptValues(oldValues);
    updateForm({
      ...form,
      knowledge_note_system_prompt: newSystemPrompt,
      knowledge_note_user_prompt_template: newUserTemplate,
    });
  }

  function resetVisualNotePrompt(field: "system" | "template" | "planning" | "vlm") {
    if (!form) return;
    const defaultSystemPrompt = form.defaults?.visual_note_system_prompt || form.visual_note_system_prompt;
    const defaultUserTemplate = form.defaults?.visual_note_user_prompt_template || form.visual_note_user_prompt_template;
    const defaultPlanningPrompt = form.defaults?.visual_frame_planning_prompt || form.visual_frame_planning_prompt;
    const defaultVlmPrompt = form.defaults?.visual_vlm_prompt || form.visual_vlm_prompt;
    const newSystemPrompt = field === "system" ? defaultSystemPrompt : form.visual_note_system_prompt;
    const newUserTemplate = field === "template" ? defaultUserTemplate : form.visual_note_user_prompt_template;
    const newPlanningPrompt = field === "planning" ? defaultPlanningPrompt : form.visual_frame_planning_prompt;
    const newVlmPrompt = field === "vlm" ? defaultVlmPrompt : form.visual_vlm_prompt;
    const oldValues: Record<string, string> = {};
    if (field === "system" && form.visual_note_system_prompt !== newSystemPrompt) {
      oldValues.visual_note_system_prompt = form.visual_note_system_prompt || "";
    }
    if (field === "template" && form.visual_note_user_prompt_template !== newUserTemplate) {
      oldValues.visual_note_user_prompt_template = form.visual_note_user_prompt_template || "";
    }
    if (field === "planning" && form.visual_frame_planning_prompt !== newPlanningPrompt) {
      oldValues.visual_frame_planning_prompt = form.visual_frame_planning_prompt || "";
    }
    if (field === "vlm" && form.visual_vlm_prompt !== newVlmPrompt) {
      oldValues.visual_vlm_prompt = form.visual_vlm_prompt || "";
    }
    updateUndoPromptValues(oldValues);
    updateForm({
      ...form,
      visual_note_system_prompt: newSystemPrompt,
      visual_note_user_prompt_template: newUserTemplate,
      visual_frame_planning_prompt: newPlanningPrompt,
      visual_vlm_prompt: newVlmPrompt,
    });
  }

  function applyVisualNotePreset(preset: "text" | "visual" | "multimodal") {
    if (!form) return;
    if (preset === "text") {
      updateForm({
        ...form,
        visual_note_mode: "text",
        visual_evidence_enabled: false,
        visual_evidence_use_llm: false,
        visual_multimodal_enabled: false,
      });
      return;
    }
    if (preset === "visual") {
      updateForm({
        ...form,
        visual_note_mode: "frame_insert",
        visual_evidence_enabled: true,
        visual_evidence_use_llm: false,
        visual_multimodal_enabled: false,
      });
      return;
    }
    updateForm({
      ...form,
      visual_note_mode: "vlm_integrated",
      visual_evidence_enabled: true,
      visual_evidence_use_llm: true,
      visual_multimodal_enabled: true,
    });
  }

  function validateSettingsBeforeSave(nextForm: ServiceSettings): { message: string; category: SettingsCategory; targetKey: string } | null {
    if (!String(nextForm.host || "").trim()) {
      return {
        message: "请先填写监听地址。",
        category: "logs",
        targetKey: "host",
      };
    }
    if (nextForm.transcription_provider === "siliconflow" && !String(nextForm.siliconflow_asr_base_url || "").trim()) {
      return {
        message: "请先填写 SiliconFlow Base URL。",
        category: "transcription",
        targetKey: "siliconflow_asr_base_url",
      };
    }
    if (nextForm.llm_enabled && !String(nextForm.llm_base_url || "").trim()) {
      return {
        message: "请先填写 LLM API Base URL。",
        category: "generation",
        targetKey: "llm_base_url",
      };
    }
    const promptValidationError = validatePromptTemplates(nextForm);
    if (promptValidationError) {
      return promptValidationError;
    }
    return null;
  }

  function hasAllPromptTokens(template: string, tokens: string[]) {
    return tokens.every((token) => template.includes(token));
  }

  function validatePromptTemplates(nextForm: ServiceSettings): { message: string; category: SettingsCategory; targetKey: string } | null {
    const summaryTemplate = String(nextForm.summary_user_prompt_template || "");
    if (!hasAllPromptTokens(summaryTemplate, ["{title}", "{transcript}", "{segments_json}"])) {
      return {
        message: "摘要 User Template 需要保留 {title}、{transcript}、{segments_json} 变量，否则任务无法稳定生成摘要。",
        category: "prompts",
        targetKey: "summary_user_prompt_template",
      };
    }
    for (const fieldName of ["title", "overview", "bulletPoints", "chapters", "chapterGroups"]) {
      if (!summaryTemplate.includes(fieldName)) {
        return {
          message: `摘要 User Template 需要保留 ${fieldName} 输出字段约束。`,
          category: "prompts",
          targetKey: "summary_user_prompt_template",
        };
      }
    }

    const knowledgeTemplate = String(nextForm.knowledge_note_user_prompt_template || "");
    if (!hasAllPromptTokens(knowledgeTemplate, ["{title}", "{summary_json}"])) {
      return {
        message: "知识笔记 User Template 至少需要保留 {title} 与 {summary_json} 变量。",
        category: "prompts",
        targetKey: "knowledge_note_user_prompt_template",
      };
    }
    if (!knowledgeTemplate.includes("knowledgeNoteMarkdown")) {
      return {
        message: "知识笔记 User Template 需要保留 knowledgeNoteMarkdown 输出字段，否则任务会解析失败。",
        category: "prompts",
        targetKey: "knowledge_note_user_prompt_template",
      };
    }

    const visualPlanningPrompt = String(nextForm.visual_frame_planning_prompt || "");
    if (!hasAllPromptTokens(visualPlanningPrompt, ["{title}", "{summary_json}"])) {
      return {
        message: "捕获帧规划 Prompt 至少需要保留 {title} 与 {summary_json} 变量。",
        category: "prompts",
        targetKey: "visual_frame_planning_prompt",
      };
    }

    const visualVlmPrompt = String(nextForm.visual_vlm_prompt || "");
    if (!hasAllPromptTokens(visualVlmPrompt, ["{title}", "{timestamp}"])) {
      return {
        message: "画面理解 Prompt 至少需要保留 {title} 与 {timestamp} 变量。",
        category: "prompts",
        targetKey: "visual_vlm_prompt",
      };
    }

    const visualNoteTemplate = String(nextForm.visual_note_user_prompt_template || "");
    if (!hasAllPromptTokens(visualNoteTemplate, ["{title}", "{knowledge_note_markdown}", "{visual_observations_json}"])) {
      return {
        message: "图文笔记 User Template 需要保留 {title}、{knowledge_note_markdown}、{visual_observations_json} 变量。",
        category: "prompts",
        targetKey: "visual_note_user_prompt_template",
      };
    }
    return null;
  }

  function buildSettingsSavePayload(nextForm: ServiceSettings): Partial<ServiceSettings> {
    const payload: Partial<ServiceSettings> = {
      ...nextForm,
      device_preference: normalizeDevicePreference(nextForm.device_preference),
    };
    if (String(payload.knowledge_embedding_provider || "").trim().toLowerCase() === "siliconflow") {
      payload.siliconflow_embedding_base_url = String(payload.siliconflow_embedding_base_url || DEFAULT_SILICONFLOW_EMBEDDING_BASE_URL).trim();
      payload.siliconflow_embedding_model = String(payload.siliconflow_embedding_model || DEFAULT_SILICONFLOW_EMBEDDING_MODEL).trim();
    }
    if (nextForm.siliconflow_asr_api_key_configured && (!String(nextForm.siliconflow_asr_api_key || "").trim() || isMaskedApiKey(nextForm.siliconflow_asr_api_key))) {
      delete payload.siliconflow_asr_api_key;
    }
    if (nextForm.multimodal_asr_api_key_configured && (!String(nextForm.multimodal_asr_api_key || "").trim() || isMaskedApiKey(nextForm.multimodal_asr_api_key))) {
      delete payload.multimodal_asr_api_key;
    }
    if (nextForm.llm_api_key_configured && (!String(nextForm.llm_api_key || "").trim() || isMaskedApiKey(nextForm.llm_api_key))) {
      delete payload.llm_api_key;
    }
    if (nextForm.knowledge_llm_api_key_configured && (!String(nextForm.knowledge_llm_api_key || "").trim() || isMaskedApiKey(nextForm.knowledge_llm_api_key))) {
      delete payload.knowledge_llm_api_key;
    }
    if (nextForm.siliconflow_embedding_api_key_configured && (!String(nextForm.siliconflow_embedding_api_key || "").trim() || isMaskedApiKey(nextForm.siliconflow_embedding_api_key))) {
      delete payload.siliconflow_embedding_api_key;
    }
    if (nextForm.visual_evidence_api_key_configured && (!String(nextForm.visual_evidence_api_key || "").trim() || isMaskedApiKey(nextForm.visual_evidence_api_key))) {
      delete payload.visual_evidence_api_key;
    }
    if (nextForm.twelvelabs_api_key_configured && (!String(nextForm.twelvelabs_api_key || "").trim() || isMaskedApiKey(nextForm.twelvelabs_api_key))) {
      delete payload.twelvelabs_api_key;
    }
    if (nextForm.siliconflow_embedding_api_key_configured && (!String(nextForm.siliconflow_embedding_api_key || "").trim() || isMaskedApiKey(nextForm.siliconflow_embedding_api_key))) {
      delete payload.siliconflow_embedding_api_key;
    }
    return payload;
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!form || isSaving) return;
    if (!isDirty) {
      setSaveStatus("");
      return;
    }
    const validationError = validateSettingsBeforeSave(form);
    if (validationError) {
      setSaveStatus(validationError.message);
      setActiveCategory(validationError.category);
      setPendingFocusTarget(validationError.targetKey);
      return;
    }
    try {
      setIsSaving(true);
      const response = await api.updateSettings(buildSettingsSavePayload(form));
      const nextSettings = response.settings;
      setForm(maskConfiguredApiKeys(nextSettings));
      setIsDirty(false);
      setSaveStatus(response.message || "设置已保存");
      void (async () => {
        try {
          const nextEnvironment = await api.getEnvironment({ runtimeChannel: nextSettings.runtime_channel });
          setEnvironment(nextEnvironment);
          onSettingsSaved(nextSettings, nextEnvironment);
        } catch {
          onSettingsSaved(nextSettings, null);
        }
      })();
    } catch (error) {
      setSaveStatus(error instanceof Error ? error.message : "保存设置失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function installLocalAsr() {
    if (!form) return;
    const sessionId = `local-asr-${Date.now()}`;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    try {
      setLocalAsrInstalling(true);
      setLocalAsrStatus("正在安装本地语音识别环境...");
      setLocalAsrOutput("");
      // Start polling install log
      pollTimer = setInterval(async () => {
        try {
          const log = await api.getInstallLog(sessionId);
          if (log.log) setLocalAsrOutput(log.log);
          if (log.done && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        } catch { /* ignore poll errors */ }
      }, 1500);
      const response = await api.installLocalAsr({ installSessionId: sessionId });
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      // Final log fetch
      try { const log = await api.getInstallLog(sessionId); if (log.log) setLocalAsrOutput(log.log); } catch { /* ignore */ }
      setLocalAsrStatus(response.installed ? "本地语音识别环境已安装" : "本地语音识别环境安装失败");
      const nextEnvironment = response.environment || (await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true }));
      setEnvironment(nextEnvironment);
      if (response.installed) {
        try {
          const settingsResponse = await api.updateSettings({
            ...buildSettingsSavePayload(form),
            transcription_provider: "local",
          });
          setForm(maskConfiguredApiKeys(settingsResponse.settings));
          setIsDirty(false);
          setSaveStatus(settingsResponse.message || "已切换为本地 ASR");
          onSettingsSaved(settingsResponse.settings, nextEnvironment);
        } catch (error) {
          setLocalAsrStatus(`本地 ASR 已安装，但切换默认转写方式失败：${error instanceof Error ? error.message : "保存设置失败"}`);
        }
      }
      onRefresh();
    } catch (error) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      setLocalAsrStatus(error instanceof Error ? error.message : "安装本地语音识别环境失败");
    } finally {
      setLocalAsrInstalling(false);
    }
  }

  async function installFunAsr() {
    if (!form) return;
    const sessionId = `funasr-${Date.now()}`;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    try {
      setFunasrInstalling(true);
      setFunasrStatus("正在安装 FunASR 语音识别环境（含 torch 等依赖，可能需要较长时间）...");
      setFunasrOutput("");
      // Start polling install log
      pollTimer = setInterval(async () => {
        try {
          const log = await api.getInstallLog(sessionId);
          if (log.log) setFunasrOutput(log.log);
          if (log.done && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        } catch (e) {
          setFunasrOutput(prev => prev + `\n[轮询异常: ${e instanceof Error ? e.message : 'network error'}]`);
        }
      }, 1500);
      const response = await api.installFunAsr({ installSessionId: sessionId });
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      // Final log fetch
      try { const log = await api.getInstallLog(sessionId); if (log.log) setFunasrOutput(log.log); } catch { /* ignore */ }
      setFunasrStatus(response.installed ? "FunASR 语音识别环境已安装" : "FunASR 语音识别环境安装失败");
      const nextEnvironment = response.environment || (await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true }));
      setEnvironment(nextEnvironment);
      if (response.installed) {
        try {
          const settingsResponse = await api.updateSettings({
            ...buildSettingsSavePayload(form),
            transcription_provider: "funasr",
          });
          setForm(maskConfiguredApiKeys(settingsResponse.settings));
          setIsDirty(false);
          setSaveStatus(settingsResponse.message || "已切换为 FunASR");
          onSettingsSaved(settingsResponse.settings, nextEnvironment);
        } catch (error) {
          setFunasrStatus(`FunASR 已安装，但切换默认转写方式失败：${error instanceof Error ? error.message : "保存设置失败"}`);
        }
      }
      onRefresh();
    } catch (error) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      setFunasrStatus(error instanceof Error ? error.message : "安装 FunASR 语音识别环境失败");
    } finally {
      setFunasrInstalling(false);
    }
  }

  async function installKnowledgeDependencies() {
    if (!form) return;
    const sessionId = `knowledge-deps-${Date.now()}`;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    try {
      setKnowledgeDepsInstalling(true);
      setKnowledgeDepsStatus("正在安装知识库依赖...");
      setKnowledgeDepsOutput("");
      // Start polling install log
      pollTimer = setInterval(async () => {
        try {
          const log = await api.getInstallLog(sessionId);
          if (log.log) setKnowledgeDepsOutput(log.log);
          if (log.done && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        } catch { /* ignore poll errors */ }
      }, 1500);
      const response = await api.installKnowledgeDependencies({
        runtime_channel: form.runtime_channel,
        provider: form.knowledge_embedding_provider || "local_huggingface",
        reinstall: Boolean(knowledgeDepsReady) || hasKnowledgeBrokenDeps,
        installSessionId: sessionId,
      });
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      // Final log fetch
      try { const log = await api.getInstallLog(sessionId); if (log.log) setKnowledgeDepsOutput(log.log); } catch { /* ignore */ }
      setKnowledgeDepsStatus(response.installed ? "知识库依赖已安装并完成检测" : "知识库依赖安装后仍未完全就绪");
      const nextEnvironment = response.environment || (await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true }));
      setEnvironment(nextEnvironment);
      await fetchKnowledgeRequirements();
      onRefresh();
    } catch (error) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      setKnowledgeDepsStatus(error instanceof Error ? error.message : "安装知识库依赖失败");
    } finally {
      setKnowledgeDepsInstalling(false);
    }
  }

  async function fetchKnowledgeRequirements() {
    if (!form) return;
    try {
      const result = await api.getKnowledgeRequirements(form.knowledge_embedding_provider || "local_huggingface");
      setKnowledgeRequirements(result);
    } catch (error) {
      console.error("Failed to fetch knowledge requirements:", error);
    }
  }

  async function handleDependencyStatusChange() {
    const nextEnvironment = await api.getEnvironment({ runtimeChannel: form?.runtime_channel, refresh: true });
    setEnvironment(nextEnvironment);
    await fetchKnowledgeRequirements();
    onRefresh();
  }

  async function downloadEmbeddingModel() {
    if (!form) return;
    const sessionId = `emb-dl-${Date.now()}`;
    try {
      setEmbeddingDownloading(true);
      setEmbeddingVerified(false);
      setEmbeddingOutput("");
      setEmbeddingStatus("正在安装依赖并下载模型...");
      const response = await api.downloadEmbeddingModel({
        provider: form.knowledge_embedding_provider || "local_huggingface",
        model: form.knowledge_embedding_model,
        hf_endpoint: form.hf_endpoint,
        installSessionId: sessionId,
      });
      try {
        const logResp = await api.getInstallLog(sessionId);
        setEmbeddingOutput(logResp.log || response.stdoutTail || "");
      } catch {
        setEmbeddingOutput(response.stdoutTail || "");
      }
      setEmbeddingStatus(response.downloaded ? "模型下载完成，可以测试了。" : `下载失败：${response.detail}`);
    } catch (error) {
      setEmbeddingStatus(error instanceof Error ? error.message : "下载模型失败");
    } finally {
      setEmbeddingDownloading(false);
    }
  }

  async function testEmbeddingModel() {
    if (!form) return;
    try {
      setEmbeddingTesting(true);
      setEmbeddingOutput("");
      setEmbeddingStatus(form.knowledge_embedding_provider === "siliconflow" ? "正在请求硅基流动 Embedding API..." : "正在加载并验证 Embedding 模型...");
      const provider = form.knowledge_embedding_provider || "local_huggingface";
      const response = await api.testEmbeddingModel({
        provider,
        model: provider === "siliconflow" ? effectiveSiliconFlowEmbeddingModel : form.knowledge_embedding_model,
        hf_endpoint: form.hf_endpoint,
        api_key: provider === "siliconflow" && !isMaskedApiKey(form.siliconflow_embedding_api_key)
          ? form.siliconflow_embedding_api_key
          : undefined,
        base_url: provider === "siliconflow" ? effectiveSiliconFlowEmbeddingBaseUrl : undefined,
      });
      setEmbeddingOutput(response.stdoutTail || "");
      if (response.verified) {
        setEmbeddingVerified(true);
        setEmbeddingStatus(response.detail || "模型验证成功！");
      } else {
        setEmbeddingVerified(false);
        setEmbeddingStatus(`验证失败：${response.detail}`);
      }
    } catch (error) {
      setEmbeddingVerified(false);
      setEmbeddingStatus(error instanceof Error ? error.message : "验证模型失败");
    } finally {
      setEmbeddingTesting(false);
    }
  }

  async function captureBilibiliLoginCookies() {
    if (!form || bilibiliCookieCapturing) {
      return;
    }
    try {
      setBilibiliCookieCapturing(true);
      setBilibiliQrcodeKey("");
      setBilibiliQrcodeImage("");
      const desktopBilibili = window.desktop?.bilibili;
      if (!desktopBilibili) {
        setBilibiliCookieStatus("正在生成 B 站扫码登录二维码...");
        const login = await api.createBilibiliCookieQrcode();
        const image = await QRCode.toDataURL(login.url, {
          margin: 1,
          width: 180,
          color: {
            dark: "#111111",
            light: "#ffffff",
          },
        });
        setBilibiliQrcodeKey(login.qrcodeKey);
        setBilibiliQrcodeImage(image);
        setBilibiliCookieStatus("请用手机 B 站扫码，并在手机上确认登录。");
        return;
      }
      setBilibiliCookieStatus("请在新窗口登录 B 站，登录成功后会自动保存 cookies...");
      const captured: BilibiliCookieCaptureResult = await desktopBilibili.captureLoginCookies();
      const response = await api.updateSettings({
        ytdlp_cookies_file: captured.cookiesFile,
        ytdlp_cookies_browser: "",
      });
      const nextSettings = maskConfiguredApiKeys(response.settings);
      setForm(nextSettings);
      setIsDirty(false);
      setSaveStatus(response.message || "设置已保存");
      const browserSuffix = captured.browser ? `（${captured.browser}）` : "";
      setBilibiliCookieStatus(`B 站登录态已保存${browserSuffix}，捕获 ${captured.cookieCount} 条 cookies。`);
      onSettingsSaved(response.settings, environment);
    } catch (error) {
      setBilibiliCookieStatus(error instanceof Error ? error.message : "捕获 B 站登录态失败，请按教程手动导出 cookies.txt。");
    } finally {
      setBilibiliCookieCapturing(false);
    }
  }

  function buildLlmTestPayload(scope: GenerationModelScope) {
    if (!form) {
      return null;
    }
    if (scope === "main") {
      return {
        llm_enabled: form.llm_enabled,
        llm_provider: form.llm_provider,
        llm_base_url: form.llm_base_url,
        llm_model: form.llm_model,
        ...(form.llm_api_key.trim() && !isMaskedApiKey(form.llm_api_key) ? { llm_api_key: form.llm_api_key } : {}),
      };
    }
    const visualApiKey = String(form.visual_evidence_api_key || "").trim();
    const mainApiKey = String(form.llm_api_key || "").trim();
    return {
      llm_test_scope: "visual" as const,
      llm_enabled: form.visual_multimodal_enabled,
      llm_provider: form.visual_vlm_provider || form.llm_provider,
      llm_base_url: form.visual_evidence_base_url || form.llm_base_url,
      llm_model: form.visual_evidence_model || form.llm_model,
      ...(!visualApiKey || isMaskedApiKey(visualApiKey)
        ? mainApiKey && !isMaskedApiKey(mainApiKey)
          ? { llm_api_key: mainApiKey }
          : {}
        : { llm_api_key: visualApiKey }),
    };
  }

  async function runLlmConnectionTest(scope: GenerationModelScope) {
    const payload = buildLlmTestPayload(scope);
    if (!payload) {
      throw new Error("模型配置尚未加载。");
    }
    return api.testLlmConnection(payload);
  }

  async function testLlmConnection() {
    if (!form || llmTestBusy) {
      return;
    }
    try {
      setLlmTestBusy(true);
      setLlmTestNoticeVersion((current) => current + 1);
      setLlmTestStatus("正在测试 LLM 连接与 JSON 输出...");
      const response = await runLlmConnectionTest("main");
      const preview = response.jsonPreview || response.responsePreview;
      const suffix = preview ? `，示例：${preview}` : "";
      setLlmTestStatus(`${response.message}${suffix}`);
      setModelAvailability((current) => ({
        ...current,
        main: { status: "available", message: response.message },
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : "LLM 连接测试失败";
      setLlmTestStatus(message);
      setModelAvailability((current) => ({
        ...current,
        main: { status: "unavailable", message },
      }));
    } finally {
      setLlmTestBusy(false);
    }
  }

  async function testKnowledgeLlmConnection() {
    if (!form || llmTestBusy) {
      return;
    }
    try {
      setLlmTestBusy(true);
      setLlmTestNoticeVersion((current) => current + 1);
      setLlmTestStatus("正在测试知识库 LLM 连接与 JSON 输出...");
      const response = await api.testLlmConnection({
        llm_test_scope: "knowledge",
        llm_enabled: form.knowledge_llm_enabled,
        llm_provider: form.knowledge_llm_provider,
        llm_base_url: form.knowledge_llm_base_url,
        llm_model: form.knowledge_llm_model,
        ...(form.knowledge_llm_api_key.trim() && !isMaskedApiKey(form.knowledge_llm_api_key) ? { llm_api_key: form.knowledge_llm_api_key } : {}),
      });
      const preview = response.jsonPreview || response.responsePreview;
      const suffix = preview ? `，示例：${preview}` : "";
      setLlmTestStatus(`${response.message}${suffix}`);
    } catch (error) {
      setLlmTestStatus(error instanceof Error ? error.message : "知识库 LLM 连接测试失败");
    } finally {
      setLlmTestBusy(false);
    }
  }

  async function testVisualLlmConnection() {
    if (!form || llmTestBusy) {
      return;
    }
    try {
      setLlmTestBusy(true);
      setLlmTestNoticeVersion((current) => current + 1);
      setLlmTestStatus("正在测试视觉模型连接与 JSON 输出...");
      const response = await runLlmConnectionTest("visual");
      const preview = response.jsonPreview || response.responsePreview;
      const suffix = preview ? `，示例：${preview}` : "";
      setLlmTestStatus(`${response.message}${suffix}`);
      setModelAvailability((current) => ({
        ...current,
        visual: { status: "available", message: response.message },
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : "视觉模型连接测试失败";
      setLlmTestStatus(message);
      setModelAvailability((current) => ({
        ...current,
        visual: { status: "unavailable", message },
      }));
    } finally {
      setLlmTestBusy(false);
    }
  }

  function closeGenerationModelDialog() {
    const scope = generationModelDialog;
    setGenerationModelDialog(null);
    if (!scope) {
      return;
    }
    void silentlyCheckGenerationModel(scope);
  }

  async function silentlyCheckGenerationModel(scope: GenerationModelScope) {
    if (!form || llmTestBusy) {
      return;
    }
    const runId = silentModelCheckRunId.current + 1;
    silentModelCheckRunId.current = runId;
    setModelAvailability((current) => ({
      ...current,
      [scope]: { status: "checking", message: "" },
    }));
    try {
      const response = await runLlmConnectionTest(scope);
      if (silentModelCheckRunId.current !== runId) {
        return;
      }
      setModelAvailability((current) => ({
        ...current,
        [scope]: { status: "available", message: response.message },
      }));
    } catch (error) {
      if (silentModelCheckRunId.current !== runId) {
        return;
      }
      setModelAvailability((current) => ({
        ...current,
        [scope]: {
          status: "unavailable",
          message: error instanceof Error ? error.message : "模型不可用",
        },
      }));
    }
  }

  async function testAsrConnection() {
    if (!form || asrTestBusy) {
      return;
    }
    try {
      setAsrTestBusy(true);
      setAsrTestStatus("正在测试 ASR 连接...");
      const response = await api.testAsrConnection({
        transcription_provider: form.transcription_provider,
        siliconflow_asr_base_url: form.siliconflow_asr_base_url,
        siliconflow_asr_model: form.siliconflow_asr_model,
        ...(form.siliconflow_asr_api_key.trim() && !isMaskedApiKey(form.siliconflow_asr_api_key) ? { siliconflow_asr_api_key: form.siliconflow_asr_api_key } : {}),
        multimodal_asr_base_url: form.multimodal_asr_base_url,
        multimodal_asr_model: form.multimodal_asr_model,
        ...(form.multimodal_asr_api_key.trim() && !isMaskedApiKey(form.multimodal_asr_api_key) ? { multimodal_asr_api_key: form.multimodal_asr_api_key } : {}),
      });
      const preview = response.responsePreview ? `，示例：${response.responsePreview}` : "";
      setAsrTestStatus(`${response.message}${preview}`);
    } catch (error) {
      setAsrTestStatus(error instanceof Error ? error.message : "ASR 连接测试失败");
    } finally {
      setAsrTestBusy(false);
    }
  }

  const configHealth = getConfigHealth(form, environment);

  function registerFocusTarget(targetKey: string) {
    return (node: HTMLElement | null) => {
      focusTargetRefs.current[targetKey] = node;
    };
  }

  function focusSettingTarget(category: SettingsCategory, targetKey: string) {
    setActiveCategory(category);
    setPendingFocusTarget(targetKey);
    setSettingsSearchQuery("");
  }

  function resolveIssueTarget(issueKey: string): { category: SettingsCategory; targetKey: string } | null {
    if (!form) {
      return null;
    }
    if (issueKey === "siliconflow_asr_api_key") {
      return { category: "transcription", targetKey: "siliconflow_asr_api_key" };
    }
    if (issueKey === "multimodal_asr_base_url") {
      return { category: "transcription", targetKey: "multimodal_asr_base_url" };
    }
    if (issueKey === "local_asr_runtime") {
      return { category: "runtime", targetKey: "local_asr_runtime" };
    }
    if (issueKey === "auto_mindmap_requires_llm") {
      return { category: "generation", targetKey: "llm_enabled" };
    }
    if (issueKey === "knowledge_dependencies") {
      return { category: "runtime", targetKey: "knowledge_runtime" };
    }
    if (issueKey === "siliconflow_embedding_api_key") {
      return { category: "knowledge", targetKey: "siliconflow_embedding_api_key" };
    }
    if (issueKey === "knowledge_llm_configuration") {
      if (String(form.knowledge_llm_mode || "same_as_main").trim().toLowerCase() === "custom") {
        if (!form.knowledge_llm_enabled) {
          return { category: "knowledge", targetKey: "knowledge_llm_enabled" };
        }
        if (!String(form.knowledge_llm_base_url || "").trim()) {
          return { category: "knowledge", targetKey: "knowledge_llm_base_url" };
        }
        if (!hasUsableApiKey(form.knowledge_llm_api_key, form.knowledge_llm_api_key_configured)) {
          return { category: "knowledge", targetKey: "knowledge_llm_api_key" };
        }
        if (!String(form.knowledge_llm_model || "").trim()) {
          return { category: "knowledge", targetKey: "knowledge_llm_model" };
        }
        return { category: "knowledge", targetKey: "knowledge_llm_base_url" };
      }
      if (!form.llm_enabled) {
        return { category: "generation", targetKey: "llm_enabled" };
      }
      if (!String(form.llm_base_url || "").trim()) {
        return { category: "generation", targetKey: "llm_base_url" };
      }
      if (!hasUsableApiKey(form.llm_api_key, form.llm_api_key_configured)) {
        return { category: "generation", targetKey: "llm_api_key" };
      }
      if (!String(form.llm_model || "").trim()) {
        return { category: "generation", targetKey: "llm_model" };
      }
      return { category: "knowledge", targetKey: "knowledge_llm_mode" };
    }
    if (issueKey === "llm_configuration") {
      if (!String(form.llm_base_url || "").trim()) {
        return { category: "generation", targetKey: "llm_base_url" };
      }
      if (!hasUsableApiKey(form.llm_api_key, form.llm_api_key_configured)) {
        return { category: "generation", targetKey: "llm_api_key" };
      }
      if (!String(form.llm_model || "").trim()) {
        return { category: "generation", targetKey: "llm_model" };
      }
      return { category: "generation", targetKey: "llm_base_url" };
    }
    if (issueKey === "ytdlp_cookies_browser" || issueKey === "ytdlp_cookies_file") {
      return { category: "video", targetKey: "ytdlp_cookies_file" };
    }
    return null;
  }

  return (
    <div className="settings-page-wrapper">
      <FloatingNoticeStack
        notices={[
          { id: "settings-save-status", message: saveStatus },
          { id: "settings-cuda-status", message: cudaStatus },
          { id: "settings-local-asr-status", message: localAsrStatus },
          { id: "settings-funasr-status", message: funasrStatus },
          { id: "settings-knowledge-deps-status", message: knowledgeDepsStatus },
          { id: "settings-runtime-status", message: runtimeStatusMessage },
          { id: "settings-asr-test-status", message: asrTestStatus },
          { id: "settings-llm-test-status", message: llmTestStatus, version: llmTestNoticeVersion },
          { id: "settings-storage-status", message: storageStatus },
          { id: "settings-backend-error", message: desktop.backend?.lastError || "", tone: "error" },
          { id: "settings-service-status", message: serviceStatus },
        ]}
      />
      <aside className="settings-nav" ref={settingsNavRef}>
        <div className="settings-nav-header">
          <span className="settings-nav-label-small">BiliSum</span>
          <div className="settings-nav-brand-card">
            <div className="settings-nav-brand-copy">
              <span className="settings-nav-brand-kicker">设置</span>
              <strong>管理应用与运行配置</strong>
              <p>调整目录、模型、服务与环境配置。</p>
            </div>
            <div className="settings-nav-brand-metrics">
              <div className="settings-nav-metric">
                <span>服务</span>
                <strong>{serviceOnline ? "在线" : backendRunning ? "启动中" : "离线"}</strong>
              </div>
              <div className="settings-nav-metric">
                <span>设备</span>
                <strong>{devicePreferenceLabel(form.whisper_device)}</strong>
              </div>
            </div>
          </div>
        </div>
        <div className="settings-nav-list">
          <div className="settings-nav-group">
            <span className="settings-nav-group-label">工作流</span>
            <nav className="settings-nav-links">
              {workflowCategories.map((category) => (
                <button
                  key={category.id}
                  className={`settings-nav-item ${activeCategory === category.id ? "active" : ""}`}
                  type="button"
                  onClick={() => setActiveCategory(category.id)}
                >
                  <span className="settings-nav-icon">{category.icon}</span>
                  <span className="settings-nav-copy">
                    <span className="settings-nav-label">{category.label}</span>
                    <span className="settings-nav-description">{category.description}</span>
                  </span>
                </button>
              ))}
            </nav>
          </div>
          <div className="settings-nav-group">
            <span className="settings-nav-group-label">系统</span>
            <nav className="settings-nav-links">
              {systemCategories.map((category) => (
                <button
                  key={category.id}
                  className={`settings-nav-item ${activeCategory === category.id ? "active" : ""}`}
                  type="button"
                  onClick={() => setActiveCategory(category.id)}
                >
                  <span className="settings-nav-icon">{category.icon}</span>
                  <span className="settings-nav-copy">
                    <span className="settings-nav-label">{category.label}</span>
                    <span className="settings-nav-description">{category.description}</span>
                  </span>
                </button>
              ))}
            </nav>
          </div>
        </div>
        <div className="settings-nav-actions">
          <button className="primary-button settings-save-btn" type="button" disabled={isSaving || !isDirty} onClick={async (e) => { e.preventDefault(); await save(e as FormEvent); }}>
            {isSaving ? "保存中..." : isDirty ? "保存设置" : "已保存"}
          </button>
          <div className="settings-nav-summary">
            <div className="settings-nav-summary-row">
              <span>运行环境</span>
              <strong>{environment?.runtimeChannel || form.runtime_channel || "base"}</strong>
            </div>
            <div className="settings-nav-summary-row">
              <span>LLM</span>
              <strong>{llmReady ? "已配置" : form.llm_enabled ? "待补全" : "关闭"}</strong>
            </div>
            <div className="settings-nav-summary-row">
              <span>自动导图</span>
              <strong>{form.auto_generate_mindmap ? "开启" : "关闭"}</strong>
            </div>
            <div className="settings-nav-summary-row">
              <span>知识库</span>
              <strong>{form.knowledge_enabled ? (knowledgeEmbeddingReady && knowledgeDepsReady ? "就绪" : "待配置") : "关闭"}</strong>
            </div>
          </div>
        </div>
      </aside>

      <main className="settings-content">
        <div className="settings-content-scroll" ref={settingsContentScrollRef}>
          <section className="settings-search-panel" aria-label="搜索设置">
            <div className="settings-search-box">
              <SearchIcon className="settings-search-icon" aria-hidden="true" />
              <input
                className="settings-search-input"
                type="search"
                value={settingsSearchQuery}
                onChange={(event) => setSettingsSearchQuery(event.target.value)}
                placeholder="搜索设置，例如 API Key、输出目录、知识笔记、并发、Cookies"
              />
              {settingsSearchQuery ? (
                <button className="settings-search-clear" type="button" onClick={() => setSettingsSearchQuery("")}>
                  清空
                </button>
              ) : null}
            </div>
            {normalizedSettingsSearchQuery ? (
              <div className="settings-search-results" role="listbox" aria-label="设置搜索结果">
                {settingsSearchResults.length ? settingsSearchResults.map((item) => (
                  <button
                    key={`${item.category}:${item.targetKey}`}
                    className="settings-search-result"
                    type="button"
                    role="option"
                    onClick={() => focusSettingTarget(item.category, item.targetKey)}
                  >
                    <span className="settings-search-result-main">
                      <strong>{item.title}</strong>
                      <span>{item.description}</span>
                    </span>
                    <span className="settings-search-result-category">{item.categoryLabel}</span>
                  </button>
                )) : (
                  <div className="settings-search-empty">没有找到相关设置，换个关键词试试。</div>
                )}
              </div>
            ) : null}
          </section>
          <header className="settings-page-hero">
            <div className="settings-page-hero-copy">
              <span className="settings-page-kicker">Settings</span>
              <h1>{activeCategoryMeta.label}</h1>
              <p>{activeCategoryMeta.description}</p>
            </div>
            <div className="settings-page-hero-meta">
              <span className={`settings-hero-chip ${serviceOnline ? "is-success" : backendRunning ? "is-warning" : "is-danger"}`}>
                {serviceOnline ? "服务在线" : backendRunning ? "启动中..." : "服务离线"}
              </span>
              <span className={`settings-hero-chip ${configHealth.hasBlockingIssues ? "is-danger" : !configHealth.isConfigured ? "is-warning" : "is-success"}`}>
                {configHealth.hasBlockingIssues ? "配置缺失" : configHealth.isConfigured ? "配置完整" : "配置待补全"}
              </span>
              <span className="settings-hero-chip">
                {environment?.runtimeChannel || form.runtime_channel || "base"}
              </span>
              <span className={`settings-hero-chip ${(form.transcription_provider === "funasr" ? form.funasr_device === "cuda" : environment?.cudaAvailable) ? "is-success" : ""}`}>
                {form.transcription_provider === "funasr"
                  ? (form.funasr_device === "cuda" ? "GPU (CUDA)" : "CPU")
                  : form.transcription_provider === "local"
                    ? devicePreferenceLabel(form.whisper_device)
                    : environment?.cudaAvailable
                      ? "CUDA Ready"
                      : "CPU Only"}
              </span>
              <span className={`settings-hero-chip ${llmReady ? "is-success" : ""}`}>
                {llmReady ? "LLM 已配置" : form.llm_enabled ? "LLM 待补全" : "LLM 关闭"}
              </span>
              <span className={`settings-hero-chip ${form.knowledge_enabled && knowledgeEmbeddingReady && knowledgeDepsReady ? "is-success" : form.knowledge_enabled && environment?.runtimeReady !== undefined ? "is-warning" : ""}`}>
                {!environment?.runtimeReady && !environment?.runtimeError ? "检测中..."
                  : form.knowledge_enabled ? (knowledgeEmbeddingReady && knowledgeDepsReady ? "知识库就绪" : "知识库待配置") : "知识库关闭"}
              </span>
            </div>
          </header>

          {configHealth.checked ? (
            <section className={`settings-config-health-card tone-${configHealth.state}`}>
              <div className="settings-config-health-copy">
                <span className="settings-story-kicker">Setup Health</span>
                <h3>{configHealth.hasBlockingIssues ? "先补全关键配置，再开始处理视频" : configHealth.isConfigured ? "当前配置状态良好" : "建议补全增强能力配置"}</h3>
                <p>{configHealth.summary}</p>
              </div>
              {!configHealth.isConfigured ? (
                <div className="settings-config-health-list">
                  {configHealth.issues.map((issue) => (
                    <button
                      className="settings-config-health-item"
                      key={issue.key}
                      type="button"
                      onClick={() => handleConfigIssueClick(issue.key)}
                    >
                      <strong>{issue.title}</strong>
                      <span>{issue.description}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </section>
          ) : null}

          {activeCategory === "overview" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>设置总览</h2>
                <p>查看当前配置、运行状态和常用操作。</p>
              </header>

              <div className="settings-story-card">
                <div className="settings-story-copy">
                  <span className="settings-story-kicker">概览</span>
                  <h3>当前配置与运行状态</h3>
                  <p>这里展示运行环境、模型、摘要模式和服务状态。排障时请切换到环境检测或日志。</p>
                </div>
                <div className="settings-story-stats">
                  <div className="settings-story-stat">
                    <span>服务端口</span>
                    <strong>{form.host}:{form.port}</strong>
                  </div>
                  <div className="settings-story-stat">
                    <span>转写</span>
                    <strong>{form.transcription_provider === "siliconflow" ? "SiliconFlow API" : form.transcription_provider === "funasr" ? "FunASR" : form.transcription_provider === "multimodal" ? "多模态 ASR" : "Whisper 转写"}</strong>
                  </div>
                  <div className="settings-story-stat">
                    <span>摘要模式</span>
                    <strong>{form.summary_mode === "llm" ? "LLM 智能摘要" : "抽取式摘要"}</strong>
                  </div>
                </div>
              </div>

              <div className="overview-status-grid">
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <circle cx="12" cy="12" r="10" />
                      <path d="M12 6v6l4 2" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">服务状态</span>
                    <strong className={`overview-status-value ${serviceOnline ? "text-success" : "text-danger"}`}>
                      {serviceOnline ? "运行中" : "已停止"}
                    </strong>
                  </div>
                </div>
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <rect x="2" y="3" width="20" height="14" rx="2" />
                      <line x1="8" y1="21" x2="16" y2="21" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">运行环境</span>
                    <strong className="overview-status-value">{environment?.runtimeChannel || form.runtime_channel || "base"}</strong>
                  </div>
                </div>
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">推理设备</span>
                    <strong className="overview-status-value">{form.transcription_provider === "local" ? devicePreferenceLabel(form.whisper_device) : form.transcription_provider === "funasr" ? devicePreferenceLabel(form.funasr_device) : "云端识别"}</strong>
                  </div>
                </div>
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M20.2 7.8l-7.7 7.7a4 4 0 0 1-5.7 0l-3-3a1 1 0 0 1 1.4-1.4l3 3a2 2 0 0 0 2.8 0l7.7-7.7a1 1 0 0 1 1.4 1.4z" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">LLM 摘要</span>
                    <strong className={`overview-status-value ${form.llm_enabled ? "text-success" : ""}`}>
                      {form.llm_enabled ? "已启用" : "已关闭"}
                    </strong>
                  </div>
                </div>
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">语音识别服务</span>
                    <strong className={`overview-status-value ${asrReady ? "text-success" : "text-danger"}`}>
                      {form.transcription_provider === "siliconflow"
                        ? asrReady
                          ? "硅基流动已配置"
                          : "硅基流动待补全"
                        : form.transcription_provider === "multimodal"
                          ? asrReady
                            ? "多模态 ASR 已配置"
                            : "多模态 ASR 待补全"
                          : form.transcription_provider === "funasr"
                          ? funasrInstalled
                            ? "FunASR 已安装"
                            : "FunASR 未安装"
                          : localAsrInstalled
                            ? "Whisper 已安装"
                            : "Whisper 未安装"}
                    </strong>
                  </div>
                </div>
                <div className="overview-status-card">
                  <div className="overview-status-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M4 5h16" />
                      <path d="M4 12h10" />
                      <path d="M4 19h16" />
                      <circle cx="18" cy="12" r="2" />
                    </svg>
                  </div>
                  <div className="overview-status-info">
                    <span className="overview-status-label">自动导图</span>
                    <strong className={`overview-status-value ${autoMindMapReady ? "text-success" : ""}`}>
                      {autoMindMapReady ? "已启用" : "已关闭"}
                    </strong>
                  </div>
                </div>
              </div>

              <div className="overview-section">
                <h3 className="overview-section-title">环境信息</h3>
                <div className="overview-info-grid">
                  <div className="overview-info-item">
                    <span className="overview-info-label">Python</span>
                    <span className="overview-info-value">{environment?.pythonVersion || "-"}</span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">Torch</span>
                    <span className={`overview-info-value ${environment?.torchInstalled ? "text-success" : ""}`}>
                      {environment?.torchInstalled ? environment?.torchVersion || "已安装" : "未安装"}
                    </span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">GPU</span>
                    <span className={`overview-info-value ${environment?.cudaAvailable ? "text-success" : ""}`}>
                      {environment?.cudaAvailable ? environment?.gpuName || "已就绪" : "未检测到"}
                    </span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">yt-dlp</span>
                    <span className="overview-info-value">{environment?.ytDlpVersion || "-"}</span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">{form.transcription_provider === "funasr" ? "FunASR" : "Whisper"}</span>
                    <span className={`overview-info-value ${environment?.localAsrInstalled ? "text-success" : ""}`}>
                      {environment?.localAsrInstalled ? environment?.localAsrVersion || "已安装" : "未安装"}
                    </span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">FFmpeg</span>
                    <span className={`overview-info-value ${environment?.ffmpegLocation ? "text-success" : ""}`}>
                      {environment?.ffmpegLocation ? "已安装" : "未安装"}
                    </span>
                  </div>
                </div>
              </div>

              <div className="overview-section">
                <h3 className="overview-section-title">版本信息</h3>
                <div className="overview-info-grid">
                  <div className="overview-info-item">
                    <span className="overview-info-label">应用版本</span>
                    <span className="overview-info-value">v{desktop.version || "-"}</span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">监听地址</span>
                    <span className="overview-info-value">{form.host}:{form.port}</span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">语言</span>
                    <span className="overview-info-value">{form.language === "zh" ? "中文" : form.language === "en" ? "English" : "日本語"}</span>
                  </div>
                  <div className="overview-info-item">
                    <span className="overview-info-label">ASR 模型</span>
                    <span className="overview-info-value">{form.transcription_provider === "local" ? form.fixed_model : form.transcription_provider === "multimodal" ? form.multimodal_asr_model : form.siliconflow_asr_model}</span>
                  </div>
                </div>
              </div>

              <div className="overview-section">
                <h3 className="overview-section-title">快速操作</h3>
                <div className="overview-actions">
                  <button className="tertiary-button" type="button" onClick={() => setActiveCategory("runtime")}>运行环境维护</button>
                  <button className="tertiary-button" type="button" onClick={() => setActiveCategory("logs")}>查看日志</button>
                  <button className="tertiary-button" type="button" onClick={() => setActiveCategory("transcription")}>转写设置</button>
                  <button className="tertiary-button" type="button" onClick={() => setActiveCategory("generation")}>摘要设置</button>
                </div>
              </div>
            </section>
          )}

          

          {activeCategory === "files" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>输出与文件</h2>
                <p>管理导出位置、应用数据目录和本地空间占用。</p>
              </header>
              <div className="settings-form-group">
                <label className="settings-input-group" ref={registerFocusTarget("data_dir") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">数据目录</span>
                  <input className="settings-input-field" value={String(form.data_dir)} onChange={(e) => updateForm({ ...form, data_dir: e.target.value })} />
                  <span className="settings-input-caption">存储视频摘要和元数据</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("cache_dir") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">缓存目录</span>
                  <input className="settings-input-field" value={String(form.cache_dir)} onChange={(e) => updateForm({ ...form, cache_dir: e.target.value })} />
                  <span className="settings-input-caption">临时缓存文件</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("tasks_dir") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">任务目录</span>
                  <input className="settings-input-field" value={String(form.tasks_dir)} onChange={(e) => updateForm({ ...form, tasks_dir: e.target.value })} />
                  <span className="settings-input-caption">任务历史记录</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("output_dir") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">输出目录</span>
                  <input className="settings-input-field" value={String(form.output_dir)} onChange={(e) => updateForm({ ...form, output_dir: e.target.value })} />
                  <span className="settings-input-caption">手动导出的 Markdown / Obsidian 笔记会写入这里。</span>
                </label>
              </div>
            </section>
          )}

          {activeCategory === "files" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>文件管理</h2>
                <p>查看本地空间占用，并安全清理缓存和孤儿任务目录。</p>
              </header>

              <div className="settings-update-overview" ref={registerFocusTarget("storage_cleanup") as (node: HTMLDivElement | null) => void}>
                <div className="settings-update-copy">
                  <span className="settings-story-kicker">Storage</span>
                  <h3>当前本地占用</h3>
                  <p>
                    已托管空间约 {formatStorageSize(storageOverview?.totals.managedBytes || 0)}，
                    共 {formatStorageCount(storageOverview?.totals.managedFiles || 0, "文件")}
                    ，{formatStorageCount(storageOverview?.totals.managedDirectories || 0, "目录")}。
                  </p>
                </div>
                <div className="settings-update-badges">
                  <span className="helper-chip">{storageLoading ? "扫描中..." : "统计已就绪"}</span>
                  <span className={`helper-chip ${cleanupReady ? "status-success" : "status-pending"}`}>
                    {cleanupReady ? "可校验引用关系" : "服务离线，禁用清理"}
                  </span>
                  <span className="helper-chip">可回收 {formatStorageSize(cleanupTargetBytes)}</span>
                </div>
              </div>

              <div className="settings-storage-grid">
                {(storageOverview?.directories || []).map((directory) => (
                  <article key={directory.key} className="settings-storage-card">
                    <div className="settings-storage-card-head">
                      <div>
                        <span className="settings-update-label">{directory.label}</span>
                        <strong>{formatStorageSize(directory.sizeBytes)}</strong>
                      </div>
                      <button className="secondary-button" type="button" onClick={() => void openManagedDirectory(directory.key)}>
                        打开目录
                      </button>
                    </div>
                    <p className="settings-storage-path">{directory.path}</p>
                    <div className="settings-storage-meta">
                      <span>{directory.exists ? "目录存在" : "目录不存在"}</span>
                      <span>{formatStorageCount(directory.fileCount, "文件")}</span>
                      <span>{formatStorageCount(directory.directoryCount, "子目录")}</span>
                    </div>
                  </article>
                ))}
              </div>

              <div className="settings-storage-detail-grid">
                <article className="settings-storage-panel">
                  <span className="settings-update-label">缓存目录</span>
                  <strong>{formatStorageSize(cacheDirectory?.sizeBytes || 0)}</strong>
                  <p>当前仅把 `uploads` 和 `covers` 视为可安全回收的缓存内容，删除后必要资源会自动重新生成。</p>
                  <div className="settings-storage-meta">
                    <span>{formatStorageCount(storageOverview?.cleanup.cacheCandidateCount || 0, "可清理项")}</span>
                    <span>预计释放 {formatStorageSize(storageOverview?.cleanup.cacheCandidateBytes || 0)}</span>
                  </div>
                </article>
                <article className="settings-storage-panel">
                  <span className="settings-update-label">任务结果</span>
                  <strong>{formatStorageSize(tasksDirectory?.sizeBytes || 0)}</strong>
                  <p>仅识别目录名像任务 ID、但数据库中已不存在的孤儿任务目录，不会删除仍被引用的结果文件。</p>
                  <div className="settings-storage-meta">
                    <span>{formatStorageCount(storageOverview?.cleanup.orphanTaskCount || 0, "孤儿目录")}</span>
                    <span>预计释放 {formatStorageSize(storageOverview?.cleanup.orphanTaskBytes || 0)}</span>
                  </div>
                </article>
                <article className="settings-storage-panel">
                  <span className="settings-update-label">日志目录</span>
                  <strong>{formatStorageSize(logsDirectory?.sizeBytes || 0)}</strong>
                  <p>日志仅展示体积和位置，首版不提供清空操作，避免误删排障信息。</p>
                </article>
                <article className="settings-storage-panel">
                  <span className="settings-update-label">运行环境目录</span>
                  <strong>{formatStorageSize(runtimeDirectory?.sizeBytes || 0)}</strong>
                  <p>运行环境目录只做统计，不参与清理，避免影响 Python、Torch 或 CUDA 运行环境。</p>
                </article>
              </div>

              <div className="settings-update-next-step">
                <strong>清理说明</strong>
                <span>
                  {!cleanupReady
                    ? "当前服务离线，无法确认哪些任务目录仍被数据库引用，因此已禁用一键清理。"
                    : cleanupTargetCount > 0
                      ? `预计可安全清理 ${cleanupTargetCount} 项内容，释放约 ${formatStorageSize(cleanupTargetBytes)}。`
                      : "当前没有发现可安全清理的缓存或孤儿任务目录。"}
                </span>
              </div>

              <div className="settings-update-actions">
                <button className="primary-button" type="button" disabled={storageLoading || storageCleaning} onClick={() => void refreshStorageOverview()}>
                  {storageLoading ? "扫描中..." : "刷新统计"}
                </button>
                <button
                  className="secondary-button danger-button"
                  type="button"
                  disabled={!cleanupReady || storageCleaning || storageLoading}
                  onClick={() => void cleanupManagedFiles()}
                >
                  {storageCleaning ? "清理中..." : "一键清理孤儿项"}
                </button>
                <button className="secondary-button" type="button" onClick={() => void openManagedDirectory("data")}>
                  打开数据目录
                </button>
              </div>
            </section>
          )}

          {activeCategory === "video" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>视频获取</h2>
                <p>处理 B 站登录态、下载缓存和转写临时音频。遇到风控、登录或重复下载问题时先看这里。</p>
              </header>
              <div className="settings-form-group">
                <label
                  className={`settings-input-group settings-focus-target ${activeFocusTarget === "ytdlp_cookies_file" ? "is-highlighted" : ""}`}
                  ref={registerFocusTarget("ytdlp_cookies_file") as (node: HTMLLabelElement | null) => void}
                >
                  <span className="settings-input-label">B 站 Cookies 文件</span>
                  <div className="settings-input-action-row">
                    <input
                      className="settings-input-field"
                      value={form.ytdlp_cookies_file || ""}
                      onChange={(e) => updateForm({ ...form, ytdlp_cookies_file: e.target.value })}
                      placeholder="C:\\Users\\you\\Downloads\\cookies.txt"
                    />
                    <button
                      className="secondary-button"
                      type="button"
                      disabled={bilibiliCookieCapturing}
                      onClick={() => void captureBilibiliLoginCookies()}
                    >
                      {bilibiliCookieCapturing ? "获取中..." : "登录获取"}
                    </button>
                  </div>
                  <span className="settings-input-caption">推荐通过提示弹窗打开 B 站登录窗口自动生成；也可以手动填写从已登录浏览器导出的 cookies.txt。</span>
                  {bilibiliQrcodeImage ? (
                    <div className="settings-cookie-qrcode">
                      <img src={bilibiliQrcodeImage} alt="B 站扫码登录二维码" />
                      <span>用手机 B 站扫码确认后会自动写入 cookies 文件。</span>
                    </div>
                  ) : null}
                  {bilibiliCookieStatus ? <span className="settings-input-caption">{bilibiliCookieStatus}</span> : null}
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("enable_cache") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">启用下载缓存</span>
                  <select className="settings-select-field" value={form.enable_cache ? "true" : "false"} onChange={(e) => updateForm({ ...form, enable_cache: e.target.value === "true" })}>
                    <option value="true">开启</option>
                    <option value="false">关闭</option>
                  </select>
                  <span className="settings-input-caption">开启后会复用封面、上传文件和部分中间结果，适合反复处理同一批视频。</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("preserve_temp_audio") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">保留临时音频</span>
                  <select className="settings-select-field" value={form.preserve_temp_audio ? "true" : "false"} onChange={(e) => updateForm({ ...form, preserve_temp_audio: e.target.value === "true" })}>
                    <option value="false">不保留</option>
                    <option value="true">保留</option>
                  </select>
                  <span className="settings-input-caption">排查转写问题时可以临时开启；日常关闭能减少磁盘占用。</span>
                </label>
              </div>
            </section>
          )}

          {activeCategory === "transcription" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>语音转文字</h2>
                <p>配置视频音频如何转成文本：云端 ASR 更省心，本地 ASR 更依赖运行环境和设备。</p>
              </header>
              <div className="settings-form-group">
                <label className="settings-input-group">
                  <span className="settings-input-label">转写方式</span>
                  <select
                    className="settings-select-field"
                    ref={registerFocusTarget("transcription_provider") as (node: HTMLSelectElement | null) => void}
                    value={form.transcription_provider}
                    onChange={(e) => updateForm({ ...form, transcription_provider: e.target.value })}
                  >
                    <option value="siliconflow">硅基流动 API</option>
                    <option value="multimodal">多模态 ASR（第三方）</option>
                    <option value="funasr" disabled={!funasrInstalled}>FunASR（本地）{funasrInstalled ? "" : "（需先安装）"}</option>
                    <option value="local" disabled={!localAsrInstalled}>Whisper 转写（需先安装）</option>
                  </select>
                  <span className="settings-input-caption">默认推荐云端模式（硅基流动的语音识别是免费的！只需要注册然后填上apikey就可以用了）。</span>
                </label>

                <div className="settings-input-group">
                  <div className="settings-toggle-row">
                    <div className="settings-toggle-label">
                      <span className="settings-toggle-title">优先使用 UP 主字幕</span>
                      <span className="settings-toggle-caption">开启后，BiliSum 会尝试从 B 站 API 获取视频自带字幕（CC 字幕、UP 主上传字幕）。如果字幕存在且完整，将跳过 ASR 转写，节省时间。字幕不存在时自动使用 ASR。</span>
                    </div>
                    <label className="toggle-switch">
                      <input
                        type="checkbox"
                        checked={form.prefer_bilibili_subtitle || false}
                        onChange={(e) => updateForm({ ...form, prefer_bilibili_subtitle: e.target.checked })}
                      />
                      <span className="toggle-slider" />
                    </label>
                  </div>
                </div>

                {usesSiliconFlowAsr ? (
                  <>
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "siliconflow_asr_base_url" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("siliconflow_asr_base_url") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">SiliconFlow Base URL</span>
                      <input className="settings-input-field" value={form.siliconflow_asr_base_url} onChange={(e) => updateForm({ ...form, siliconflow_asr_base_url: e.target.value })} placeholder="https://api.siliconflow.cn/v1" />
                      <span className="settings-input-caption">默认保持 `https://api.siliconflow.cn/v1` 即可。</span>
                    </label>
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "siliconflow_asr_api_key" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("siliconflow_asr_api_key") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">SiliconFlow API Key</span>
                      <input className="settings-input-field" type="password" value={form.siliconflow_asr_api_key} onFocus={selectMaskedApiKey} onChange={(e) => updateForm({ ...form, siliconflow_asr_api_key: e.target.value })} placeholder="sk-..." />
                      <SiliconFlowApiKeyHelp />
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("siliconflow_asr_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">语音转写 ASR 模型</span>
                      <input className="settings-input-field" value={form.siliconflow_asr_model} onChange={(e) => updateForm({ ...form, siliconflow_asr_model: e.target.value })} placeholder="TeleAI/TeleSpeechASR" />
                      <span className="settings-input-caption">推荐使用：TeleAI/TeleSpeechASR</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("siliconflow_asr_chunk_duration_seconds") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">ASR 切片时长（秒）</span>
                      <input className="settings-input-field" type="number" min={60} max={3600} value={form.siliconflow_asr_chunk_duration_seconds ?? 1800} onChange={(e) => updateForm({ ...form, siliconflow_asr_chunk_duration_seconds: Number(e.target.value) })} />
                      <span className="settings-input-caption">长音频按此时长切片，默认 1800 秒（30 分钟）。API 单次最长 60 分钟。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("siliconflow_asr_concurrency") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">ASR 并发数</span>
                      <input className="settings-input-field" type="number" min={1} max={8} value={form.siliconflow_asr_concurrency ?? 2} onChange={(e) => updateForm({ ...form, siliconflow_asr_concurrency: Number(e.target.value) })} />
                      <span className="settings-input-caption">同时发送的转写请求数，默认 2。</span>
                    </label>
                    <div className="settings-inline-actions">
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={asrTestBusy}
                        onClick={() => void testAsrConnection()}
                      >
                        {asrTestBusy ? "测试中..." : "测试 ASR 是否可用"}
                      </button>
                      <span className="settings-input-caption">
                        使用当前表单中的 SiliconFlow Base URL、API Key 和 ASR 模型名发起一次临时转写测试，不会保存设置。
                      </span>
                    </div>
                  </>
                ) : usesMultimodalAsr ? (
                  <>
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "multimodal_asr_base_url" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("multimodal_asr_base_url") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">多模态 ASR Base URL</span>
                      <input className="settings-input-field" value={form.multimodal_asr_base_url} onChange={(e) => updateForm({ ...form, multimodal_asr_base_url: e.target.value })} placeholder="https://api.example.com/v1" />
                      <span className="settings-input-caption">支持 OpenAI 兼容的多模态 API 地址。</span>
                    </label>
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "multimodal_asr_api_key" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("multimodal_asr_api_key") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">多模态 ASR API Key</span>
                      <input className="settings-input-field" type="password" value={form.multimodal_asr_api_key} onFocus={selectMaskedApiKey} onChange={(e) => updateForm({ ...form, multimodal_asr_api_key: e.target.value })} placeholder="sk-..." />
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("multimodal_asr_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">多模态 ASR 模型</span>
                      <input className="settings-input-field" value={form.multimodal_asr_model} onChange={(e) => updateForm({ ...form, multimodal_asr_model: e.target.value })} placeholder="mimo-v2-omni" />
                      <span className="settings-input-caption">使用支持音频输入的多模态模型进行语音转文字。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("multimodal_asr_chunk_duration_seconds") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">多模态切片时长（秒）</span>
                      <input className="settings-input-field" type="number" min={30} max={600} value={form.multimodal_asr_chunk_duration_seconds ?? 180} onChange={(e) => updateForm({ ...form, multimodal_asr_chunk_duration_seconds: Number(e.target.value) })} />
                      <span className="settings-input-caption">长音频自动切片时每段的秒数，默认 180 秒（3 分钟）。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("multimodal_asr_max_retries") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">多模态切片重试次数</span>
                      <input className="settings-input-field" type="number" min={0} max={10} value={form.multimodal_asr_max_retries ?? 5} onChange={(e) => updateForm({ ...form, multimodal_asr_max_retries: Number(e.target.value) })} />
                      <span className="settings-input-caption">每段切片返回空时最多重试几次，默认 5 次。</span>
                    </label>
                    <div className="settings-inline-actions">
                      <button
                        className="secondary-button"
                        type="button"
                        disabled={asrTestBusy}
                        onClick={() => void testAsrConnection()}
                      >
                        {asrTestBusy ? "测试中..." : "测试 ASR 是否可用"}
                      </button>
                      <span className="settings-input-caption">
                        使用当前表单中的多模态配置发起一次临时转写测试，不会保存设置。
                      </span>
                    </div>
                  </>
                ) : usesFunAsr ? (
                  <>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">FunASR 模型</span>
                      <select className="settings-select-field" value={form.funasr_model} onChange={(e) => updateForm({ ...form, funasr_model: e.target.value })}>
                        <option value="paraformer-zh">paraformer-zh（中文通用，推荐）</option>
                        <option value="iic/SenseVoiceSmall">SenseVoice-Small（多语言）</option>
                        <option value="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch">SEACO Paraformer（高级，带时间戳）</option>
                        <option value="FunAudioLLM/Fun-ASR-Nano-2512">Fun-ASR-Nano（轻量级）</option>
                      </select>
                      <span className="settings-input-caption">选择 FunASR 语音识别模型</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_device") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">运行设备</span>
                      <select className="settings-select-field" value={form.funasr_device} onChange={(e) => updateForm({ ...form, funasr_device: e.target.value })}>
                        <option value="cpu">CPU</option>
                        <option value="cuda">GPU (CUDA)</option>
                      </select>
                      <span className="settings-input-caption">选择推理设备，GPU 需要 CUDA 支持</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_vad_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">语音活动检测 (VAD)</span>
                      <select className="settings-select-field" value={form.funasr_vad_model} onChange={(e) => updateForm({ ...form, funasr_vad_model: e.target.value })}>
                        <option value="">关闭</option>
                        <option value="fsmn-vad">开启（fsmn-vad）</option>
                      </select>
                      <span className="settings-input-caption">启用语音活动检测以提高识别准确度</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_punc_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">自动标点</span>
                      <select className="settings-select-field" value={form.funasr_punc_model} onChange={(e) => updateForm({ ...form, funasr_punc_model: e.target.value })}>
                        <option value="">关闭</option>
                        <option value="ct-punc">开启（ct-punc）</option>
                      </select>
                      <span className="settings-input-caption">自动添加标点符号</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_spk_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">说话人识别</span>
                      <select className="settings-select-field" value={form.funasr_spk_model} onChange={(e) => updateForm({ ...form, funasr_spk_model: e.target.value })}>
                        <option value="">关闭</option>
                        <option value="cam++">开启（cam++）</option>
                      </select>
                      <span className="settings-input-caption">识别不同说话人</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_hub") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">模型下载源</span>
                      <select className="settings-select-field" value={form.funasr_hub} onChange={(e) => updateForm({ ...form, funasr_hub: e.target.value })}>
                        <option value="ms">ModelScope</option>
                        <option value="hf">HuggingFace</option>
                      </select>
                      <span className="settings-input-caption">选择模型下载源</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("funasr_hotword") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">热词（可选）</span>
                      <input className="settings-input-field" value={form.funasr_hotword} onChange={(e) => updateForm({ ...form, funasr_hotword: e.target.value })} placeholder="多个热词用空格分隔" />
                      <span className="settings-input-caption">提高特定词汇的识别准确度，多个热词用空格分隔</span>
                    </label>
                    <div className="settings-input-group">
                      <span className="settings-input-label">可用性状态</span>
                      <div className="settings-status-display">
                        {funasrInstalled ? (
                          <span className="text-success">✅ FunASR 已安装{environment?.funasrVersion ? `（${environment.funasrVersion}）` : ""}</span>
                        ) : (
                          <span className="text-danger">❌ FunASR 未安装 — 请前往「运行环境」板块安装</span>
                        )}
                      </div>
                      <span className="settings-input-caption">
                        {funasrInstalled
                          ? "FunASR 环境就绪，可直接使用。"
                          : "FunASR 依赖需要单独安装。前往「运行环境」→「FunASR 运行环境」点击安装按钮，安装完成后会自动切换。"}
                      </span>
                    </div>
                  </>
                ) : (
                  <>
                    <label className="settings-input-group" ref={registerFocusTarget("device_preference") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">推理设备</span>
                      <select className="settings-select-field" value={normalizeDevicePreference(form.device_preference)} onChange={(e) => updateForm({ ...form, device_preference: e.target.value })}>
                        <option value="auto">自动选择</option>
                        <option value="cuda">GPU (CUDA)</option>
                        <option value="cpu">CPU</option>
                      </select>
                      <span className="settings-input-caption">选择推理设备，GPU 需要 CUDA 支持</span>
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">模型模式</span>
                      <select className="settings-select-field" value={form.model_mode} onChange={(e) => updateForm({ ...form, model_mode: e.target.value })}>
                        <option value="fixed">固定模型</option>
                        <option value="auto">自动选择</option>
                      </select>
                      <span className="settings-input-caption">自动模式会根据设备选择最优模型</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("fixed_model") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">固定模型</span>
                      <input className="settings-input-field" value={form.fixed_model} onChange={(e) => updateForm({ ...form, fixed_model: e.target.value })} placeholder="tiny / base / small / medium / large-v3" />
                      <span className="settings-input-caption">Whisper 模型名称，小模型速度快但精度低</span>
                    </label>
                  </>
                )}
              </div>
            </section>
          )}

          {activeCategory === "generation" && (
            <section className="settings-category-section generation-settings-section">
              <header className="settings-category-header">
                <h2>摘要生成</h2>
                <p>按"基础生成、模型接入、自动产物、图文截图、长视频切块"分层管理，日常开关留在页面，密钥和模型细节放进悬浮窗。</p>
              </header>
              <div className="generation-settings-tree">
                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">01</span>
                    <div>
                      <h3>基础生成</h3>
                      <p>控制摘要是否使用 LLM、输出语言和失败重试。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label className="settings-input-group">
                      <span className="settings-input-label">启用 LLM 摘要</span>
                      <select
                        className="settings-select-field"
                        ref={registerFocusTarget("llm_enabled") as (node: HTMLSelectElement | null) => void}
                        value={form.llm_enabled ? "true" : "false"}
                        onChange={(e) => updateForm({ ...form, llm_enabled: e.target.value === "true" })}
                      >
                        <option value="false">关闭</option>
                        <option value="true">开启</option>
                      </select>
                      <span className="settings-input-caption">使用大语言模型生成更高质量的视频摘要。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("summary_mode") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">摘要模式</span>
                      <select className="settings-select-field" value={form.summary_mode} onChange={(e) => updateForm({ ...form, summary_mode: e.target.value })}>
                        <option value="llm">LLM 智能摘要</option>
                        <option value="extract">仅转写</option>
                      </select>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("language") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">输出语言</span>
                      <select className="settings-select-field" value={form.language} onChange={(e) => updateForm({ ...form, language: e.target.value })}>
                        <option value="zh">中文</option>
                        <option value="en">English</option>
                        <option value="ja">日本語</option>
                      </select>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("summary_chunk_retry_count") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">重试次数</span>
                      <input className="settings-input-field" type="number" min={1} value={form.summary_chunk_retry_count} onChange={(e) => updateForm({ ...form, summary_chunk_retry_count: parseMinOneInt(e.target.value, 2) })} />
                      <span className="settings-input-caption">摘要 API 调用失败时的重试次数。</span>
                    </label>
                  </div>
                </section>

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">02</span>
                    <div>
                      <h3>模型接入</h3>
                      <p>主摘要模型和视觉理解模型只展示状态，具体地址、密钥和测试放入悬浮窗。</p>
                    </div>
                  </header>
                  <div className="settings-model-summary-grid">
                    <article
                      className={`settings-model-card settings-focus-target ${activeFocusTarget === "llm_base_url" || activeFocusTarget === "llm_api_key" || activeFocusTarget === "llm_model" ? "is-highlighted" : ""}`}
                      ref={(node) => {
                        registerFocusTarget("llm_base_url")(node);
                        registerFocusTarget("llm_api_key")(node);
                        registerFocusTarget("llm_model")(node);
                      }}
                    >
                      <div className="settings-model-card-top">
                        <div>
                          <span className="settings-model-kicker">主摘要模型</span>
                          <strong>{mainModelSummary}</strong>
                        </div>
                        <span className={`settings-status-pill ${mainModelStatusClass}`} title={mainModelAvailability.message || undefined}>{mainModelStatusLabel}</span>
                      </div>
                      <dl className="settings-model-meta">
                        <div>
                          <dt>Provider</dt>
                          <dd>{form.llm_provider || "openai-compatible"}</dd>
                        </div>
                        <div>
                          <dt>Model</dt>
                          <dd>{form.llm_model || "未填写"}</dd>
                        </div>
                        <div>
                          <dt>Base URL</dt>
                          <dd>{form.llm_base_url || "未填写"}</dd>
                        </div>
                      </dl>
                      <button className="secondary-button" type="button" onClick={() => setGenerationModelDialog("main")}>
                        编辑与测试
                      </button>
                    </article>
                    <article
                      className={`settings-model-card settings-focus-target ${activeFocusTarget === "visual_multimodal_enabled" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("visual_multimodal_enabled") as (node: HTMLElement | null) => void}
                    >
                      <div className="settings-model-card-top">
                        <div>
                          <span className="settings-model-kicker">视觉理解模型</span>
                          <strong>{visualModelSummary}</strong>
                        </div>
                        <span className={`settings-status-pill ${visualModelStatusClass}`} title={visualModelAvailability.message || undefined}>{visualModelStatusLabel}</span>
                      </div>
                      <dl className="settings-model-meta">
                        <div>
                          <dt>来源</dt>
                          <dd>{form.visual_evidence_base_url || form.visual_evidence_model || hasUsableApiKey(form.visual_evidence_api_key, form.visual_evidence_api_key_configured) ? "独立配置" : "跟随主 LLM"}</dd>
                        </div>
                        <div>
                          <dt>Model</dt>
                          <dd>{form.visual_evidence_model || form.llm_model || "未填写"}</dd>
                        </div>
                        <div>
                          <dt>Base URL</dt>
                          <dd>{form.visual_evidence_base_url || form.llm_base_url || "未填写"}</dd>
                        </div>
                      </dl>
                      <button className="secondary-button" type="button" onClick={() => setGenerationModelDialog("visual")}>
                        编辑与测试
                      </button>
                    </article>
                  </div>
                </section>

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">03</span>
                    <div>
                      <h3>自动产物</h3>
                      <p>摘要完成后是否自动追加导图和图文笔记。</p>
                    </div>
                  </header>
                  <div className="settings-visual-note-presets" aria-label="知识笔记预设">
                    <button
                      type="button"
                      className={`settings-visual-note-preset ${visualNotePreset === "text" ? "is-active" : ""}`}
                      onClick={() => applyVisualNotePreset("text")}
                    >
                      <strong>纯文本笔记</strong>
                      <span>只生成文本知识笔记。</span>
                    </button>
                    <button
                      type="button"
                      className={`settings-visual-note-preset ${visualNotePreset === "visual" ? "is-active" : ""}`}
                      onClick={() => applyVisualNotePreset("visual")}
                    >
                      <strong>无多模态的图文笔记</strong>
                      <span>抽帧并按文本语义插图。</span>
                    </button>
                    <button
                      type="button"
                      className={`settings-visual-note-preset ${visualNotePreset === "multimodal" ? "is-active" : ""}`}
                      onClick={() => applyVisualNotePreset("multimodal")}
                    >
                      <strong>多模态理解的图文笔记</strong>
                      <span>抽帧并调用 VLM 理解画面。</span>
                    </button>
                  </div>
                  <div className="settings-tree-grid">
                    <label className="settings-input-group" ref={registerFocusTarget("auto_generate_mindmap") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">自动生成思维导图</span>
                      <select className="settings-select-field" value={form.auto_generate_mindmap ? "true" : "false"} onChange={(e) => updateForm({ ...form, auto_generate_mindmap: e.target.value === "true" })}>
                        <option value="false">关闭</option>
                        <option value="true">开启</option>
                      </select>
                      <span className="settings-input-caption">关闭后仍可在详情页手动生成。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("prompt_router_mode") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">Prompt 路由模式</span>
                      <select
                        className="settings-select-field"
                        value={form.prompt_router_mode || "confirm"}
                        onChange={(e) => updateForm({ ...form, prompt_router_mode: e.target.value })}
                      >
                        <option value="confirm">确认后使用推荐</option>
                        <option value="auto">自动套用推荐</option>
                      </select>
                      <span className="settings-input-caption">首页会根据标题推荐摘要 Prompt。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("visual_note_mode") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">知识笔记形式</span>
                      <select
                        className="settings-select-field"
                        value={form.visual_note_mode || "text"}
                        onChange={(e) => {
                          const nextMode = e.target.value as typeof form.visual_note_mode;
                          updateForm({
                            ...form,
                            visual_note_mode: nextMode,
                            visual_evidence_enabled: nextMode !== "text",
                            visual_evidence_use_llm: nextMode === "vlm_integrated",
                            visual_multimodal_enabled: nextMode === "vlm_integrated",
                          });
                        }}
                      >
                        <option value="text">纯文本笔记</option>
                        <option value="frame_insert">插图笔记</option>
                        <option value="vlm_integrated">理解型图文笔记</option>
                      </select>
                      <span className="settings-input-caption">纯文本不抽帧；理解型图文会调用 VLM 解析图片内容。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("visual_evidence_enabled") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">自动生成图文笔记</span>
                      <select className="settings-select-field" value={form.visual_evidence_enabled ? "true" : "false"} disabled={(form.visual_note_mode || "text") === "text"} onChange={(e) => updateForm({ ...form, visual_evidence_enabled: e.target.value === "true" })}>
                        <option value="false">关闭</option>
                        <option value="true">开启</option>
                      </select>
                      <span className="settings-input-caption">摘要完成后在独立队列生成图文版。</span>
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">多模态理解</span>
                      <select className="settings-select-field" value={form.visual_multimodal_enabled ? "true" : "false"} disabled={(form.visual_note_mode || "text") !== "vlm_integrated"} onChange={(e) => updateForm({ ...form, visual_multimodal_enabled: e.target.value === "true", visual_evidence_use_llm: e.target.value === "true" })}>
                        <option value="false">关闭，仅按文本语义插图</option>
                        <option value="true">开启，调用 VLM 理解画面</option>
                      </select>
                      <span className="settings-input-caption">开启后会把压缩识别图送入视觉模型。</span>
                    </label>
                  </div>
                </section>

                {(form.visual_note_mode || "text") !== "text" ? (
                  <section className="settings-tree-panel">
                    <header className="settings-tree-panel-header">
                      <span className="settings-tree-index">04</span>
                      <div>
                        <h3>图文截图</h3>
                        <p>只影响图文笔记抽帧和图片质量，不影响文本总结。</p>
                      </div>
                    </header>
                    <div className="settings-tree-grid">
                      <label className="settings-input-group" ref={registerFocusTarget("visual_download_resolution") as (node: HTMLLabelElement | null) => void}>
                        <span className="settings-input-label">下载分辨率</span>
                        <select className="settings-select-field" value={form.visual_download_resolution || "720p"} onChange={(e) => updateForm({ ...form, visual_download_resolution: e.target.value })}>
                          <option value="auto">自动</option>
                          <option value="360p">360p</option>
                          <option value="480p">480p</option>
                          <option value="720p">720p</option>
                        </select>
                        <span className="settings-input-caption">只影响图文笔记抽帧视频。</span>
                      </label>
                      <label className="settings-input-group">
                        <span className="settings-input-label">最多截图数</span>
                        <input className="settings-input-field" type="number" min={1} max={30} value={form.visual_evidence_max_frames} onChange={(e) => updateForm({ ...form, visual_evidence_max_frames: parseMinOneInt(e.target.value, 12) })} />
                        <span className="settings-input-caption">后端限制在 1-30 张。</span>
                      </label>
                      <label className="settings-input-group">
                        <span className="settings-input-label">截图最小间隔（秒）</span>
                        <input className="settings-input-field" type="number" min={10} value={form.visual_evidence_frame_interval_seconds} onChange={(e) => updateForm({ ...form, visual_evidence_frame_interval_seconds: parseMinOneInt(e.target.value, 10) })} />
                      </label>
                      <label className="settings-input-group">
                        <span className="settings-input-label">笔记图片宽度</span>
                        <input className="settings-input-field" type="number" min={320} max={1600} value={form.visual_evidence_frame_width} onChange={(e) => updateForm({ ...form, visual_evidence_frame_width: parseMinOneInt(e.target.value, 960) })} />
                        <span className="settings-input-caption">控制最终图文笔记中的截图宽度上限。</span>
                      </label>
                      <label className="settings-input-group">
                        <span className="settings-input-label">识别图质量</span>
                        <input className="settings-input-field" type="number" min={1} max={100} value={form.visual_evidence_image_quality} onChange={(e) => updateForm({ ...form, visual_evidence_image_quality: parseMinOneInt(e.target.value, 85) })} />
                        <span className="settings-input-caption">仅用于 VLM 识别压缩图。</span>
                      </label>
                    </div>
                  </section>
                ) : null}

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">{(form.visual_note_mode || "text") !== "text" ? "05" : "04"}</span>
                    <div>
                      <h3>Twelve Labs Pegasus 视频理解</h3>
                      <p>可选。仅对本地视频生效，直接"看"画面生成理解摘要并写入知识笔记。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label className="settings-input-group" ref={registerFocusTarget("twelvelabs_summary_enabled") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">启用 Pegasus 视频摘要</span>
                      <select
                        className="settings-select-field"
                        value={form.twelvelabs_summary_enabled ? "true" : "false"}
                        onChange={(e) => updateForm({ ...form, twelvelabs_summary_enabled: e.target.value === "true" })}
                      >
                        <option value="false">关闭</option>
                        <option value="true">开启</option>
                      </select>
                      <span className="settings-input-caption">仅本地视频生效，不影响在线视频摘要流程。</span>
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">Twelve Labs API Key</span>
                      <input className="settings-input-field" type="password" value={form.twelvelabs_api_key || ""} placeholder="tlk_..." autoComplete="off" onChange={(e) => updateForm({ ...form, twelvelabs_api_key: e.target.value, twelvelabs_api_key_configured: false })} />
                      <span className="settings-input-caption">可在 <a href="https://twelvelabs.io" target="_blank" rel="noreferrer">twelvelabs.io</a> 免费申请。</span>
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">Pegasus 模型</span>
                      <input className="settings-input-field" type="text" value={form.twelvelabs_model || "pegasus1.5"} placeholder="pegasus1.5" onChange={(e) => updateForm({ ...form, twelvelabs_model: e.target.value })} />
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">API Base URL</span>
                      <input className="settings-input-field" type="text" value={form.twelvelabs_base_url || "https://api.twelvelabs.io/v1.3"} placeholder="https://api.twelvelabs.io/v1.3" onChange={(e) => updateForm({ ...form, twelvelabs_base_url: e.target.value })} />
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">Pegasus 提示词（留空用默认）</span>
                      <textarea className="textarea-field" rows={4} value={form.twelvelabs_prompt || ""} placeholder="留空则使用内置默认提示词" onChange={(e) => updateForm({ ...form, twelvelabs_prompt: e.target.value })} />
                    </label>
                  </div>
                </section>

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">{(form.visual_note_mode || "text") !== "text" ? "06" : "05"}</span>
                    <div>
                      <h3>长视频切块</h3>
                      <p>控制长视频拆分摘要的连续性和单块长度。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label className="settings-input-group" ref={registerFocusTarget("summary_chunk_target_chars") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">分块目标字符数</span>
                      <input className="settings-input-field" type="number" min={1} value={form.summary_chunk_target_chars} onChange={(e) => updateForm({ ...form, summary_chunk_target_chars: parseMinOneInt(e.target.value, 2200) })} />
                      <span className="settings-input-caption">LLM 处理时分块的目标字符数。</span>
                    </label>
                    <label className="settings-input-group" ref={registerFocusTarget("summary_chunk_overlap_segments") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">分块重叠段数</span>
                      <input className="settings-input-field" type="number" min={1} value={form.summary_chunk_overlap_segments} onChange={(e) => updateForm({ ...form, summary_chunk_overlap_segments: parseMinOneInt(e.target.value, 2) })} />
                      <span className="settings-input-caption">分块之间保留的重叠段落。</span>
                    </label>
                    <div className="settings-inline-alert info">
                      <strong>分块并发在性能页调整</strong>
                      <span>如果需要控制单个任务内部同时请求的摘要块数量，请前往"性能与资源"。</span>
                    </div>
                  </div>
                </section>
              </div>
            </section>
          )}

          {activeCategory === "knowledge" && (
            <section className="settings-category-section generation-settings-section">
              <header className="settings-category-header">
                <h2>知识库与问答</h2>
                <p>知识库默认关闭，依赖按需安装到当前运行环境。按"基础开关、向量模型、LLM 配置"分层管理。</p>
              </header>
              <div className="generation-settings-tree">
                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">01</span>
                    <div>
                      <h3>基础开关</h3>
                      <p>控制知识库启停、索引策略和依赖安装。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label className="settings-input-group" ref={registerFocusTarget("knowledge_enabled") as (node: HTMLLabelElement | null) => void}>
                      <span className="settings-input-label">启用知识库</span>
                      <select
                        className="settings-select-field"
                        value={form.knowledge_enabled ? "true" : "false"}
                        onChange={(e) => updateForm({ ...form, knowledge_enabled: e.target.value === "true" })}
                      >
                        <option value="false">关闭</option>
                        <option value="true">开启</option>
                      </select>
                      <span className="settings-input-caption">关闭时不会自动构建索引；标签管理仍可使用。</span>
                    </label>
                    <label className="settings-input-group">
                      <span className="settings-input-label">自动维护知识库索引</span>
                      <select
                        className="settings-select-field"
                        value={form.knowledge_index_auto_rebuild || "disabled"}
                        disabled={!form.knowledge_enabled}
                        onChange={(e) => updateForm({ ...form, knowledge_index_auto_rebuild: e.target.value })}
                      >
                        <option value="disabled">关闭自动维护</option>
                        <option value="on_task_completed">视频生成结束后更新索引</option>
                      </select>
                      <span className="settings-input-caption">开启知识库并安装依赖后，才会在任务完成时自动刷新索引。</span>
                    </label>
                  </div>
                </section>

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">02</span>
                    <div>
                      <h3>向量模型</h3>
                      <p>选择 Embedding 模型的下载源和模型名。国内用户建议使用 ModelScope 源或 HuggingFace 镜像。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "knowledge_embedding_provider" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("knowledge_embedding_provider") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">模型来源</span>
                      <select
                        className="settings-select-field"
                        value={form.knowledge_embedding_provider || "local_huggingface"}
                        disabled={!form.knowledge_enabled}
                        onChange={(e) => updateForm({ ...form, knowledge_embedding_provider: e.target.value })}
                      >
                        <option value="siliconflow">硅基流动在线 API</option>
                        <option value="local_huggingface">本地 HuggingFace</option>
                        <option value="local_modelscope">本地 ModelScope</option>
                      </select>
                      <span className="settings-input-caption">
                        {form.knowledge_embedding_provider === "siliconflow" ? "使用在线 API，无需本地向量模型和 GPU；索引存储仍需要 chromadb。" : "向量模型从哪个源下载和加载。"}
                      </span>
                    </label>
                    {form.knowledge_embedding_provider === "siliconflow" ? (
                      <>
                        <label
                          className={`settings-input-group settings-focus-target ${activeFocusTarget === "siliconflow_embedding_api_key" ? "is-highlighted" : ""}`}
                          ref={registerFocusTarget("siliconflow_embedding_api_key") as (node: HTMLLabelElement | null) => void}
                        >
                          <span className="settings-input-label">API Key</span>
                          <input
                            className="settings-input-field"
                            type="password"
                            value={form.siliconflow_embedding_api_key || ""}
                            disabled={!form.knowledge_enabled}
                            onFocus={selectMaskedApiKey}
                            onChange={(e) => updateForm({ ...form, siliconflow_embedding_api_key: e.target.value })}
                            placeholder="sk-..."
                          />
                          <SiliconFlowApiKeyHelp purpose="embedding" />
                        </label>
                        <label className="settings-input-group">
                          <span className="settings-input-label">Base URL</span>
                          <input
                            className="settings-input-field"
                            value={form.siliconflow_embedding_base_url || DEFAULT_SILICONFLOW_EMBEDDING_BASE_URL}
                            disabled={!form.knowledge_enabled}
                            onChange={(e) => updateForm({ ...form, siliconflow_embedding_base_url: e.target.value })}
                            placeholder="https://api.siliconflow.cn/v1"
                          />
                          <span className="settings-input-caption">硅基流动 API 端点，通常不需要修改。</span>
                        </label>
                        <label className="settings-input-group">
                          <span className="settings-input-label">模型名称</span>
                          <input
                            className="settings-input-field"
                            value={form.siliconflow_embedding_model || DEFAULT_SILICONFLOW_EMBEDDING_MODEL}
                            disabled={!form.knowledge_enabled}
                            onChange={(e) => updateForm({ ...form, siliconflow_embedding_model: e.target.value })}
                            placeholder="BAAI/bge-large-zh-v1.5"
                          />
                          <span className="settings-input-caption">
                            推荐使用 BAAI/bge-large-zh-v1.5 或 Qwen/Qwen3-Embedding-8B。
                          </span>
                        </label>
                        <div className="settings-inline-actions" style={{ gridColumn: "1 / -1" }}>
                          <button
                            className="secondary-button"
                            type="button"
                            disabled={embeddingTesting || !form.knowledge_enabled || !knowledgeEmbeddingReady}
                            onClick={() => void testEmbeddingModel()}
                          >
                            {embeddingTesting ? "测试中..." : "测试向量模型"}
                          </button>
                          {embeddingVerified ? (
                            <span className="settings-status-pill success">API 可用</span>
                          ) : siliconflowEmbeddingApiKeyReady ? (
                            <span className="settings-status-pill warning">待测试</span>
                          ) : null}
                        </div>
                        {embeddingStatus ? (
                          <div className={`settings-inline-alert ${embeddingVerified ? "success" : embeddingStatus.includes("失败") || embeddingStatus.includes("未配置") ? "warning" : "info"}`}>
                            <span>{embeddingStatus}</span>
                          </div>
                        ) : null}
                        {embeddingOutput ? (
                          <textarea className="textarea-field log-viewer" rows={6} readOnly value={embeddingOutput}></textarea>
                        ) : null}
                      </>
                    ) : form.knowledge_embedding_provider !== "online" ? (
                      <>
                        {Object.keys(embeddingPresets).length > 0 ? (
                          <label className="settings-input-group">
                            <span className="settings-input-label">推荐模型</span>
                            <select
                              className="settings-select-field"
                              value={form.knowledge_embedding_model && embeddingPresets[form.knowledge_embedding_model] ? form.knowledge_embedding_model : "__custom__"}
                              disabled={!form.knowledge_enabled}
                              onChange={(e) => {
                                if (e.target.value === "__custom__") {
                                  updateForm({ ...form, knowledge_embedding_model: "" });
                                } else {
                                  updateForm({ ...form, knowledge_embedding_model: e.target.value });
                                }
                              }}
                            >
                              {Object.entries(embeddingPresets).map(([id, label]) => (
                                <option key={id} value={id}>{label}</option>
                              ))}
                              <option value="__custom__">自定义模型名...</option>
                            </select>
                            <span className="settings-input-caption">从推荐列表中选择，或选"自定义模型名"手动输入。</span>
                          </label>
                        ) : null}
                        <label
                          className={`settings-input-group settings-focus-target ${activeFocusTarget === "knowledge_embedding_model" ? "is-highlighted" : ""}`}
                          ref={registerFocusTarget("knowledge_embedding_model") as (node: HTMLLabelElement | null) => void}
                        >
                          <span className="settings-input-label">模型名称</span>
                          <input
                            className="settings-input-field"
                            value={form.knowledge_embedding_model}
                            disabled={!form.knowledge_enabled}
                            onChange={(e) => updateForm({ ...form, knowledge_embedding_model: e.target.value })}
                            placeholder="BAAI/bge-small-zh-v1.5"
                          />
                          <span className="settings-input-caption">
                            {form.knowledge_embedding_provider === "local_huggingface"
                              ? "HuggingFace 上的模型 ID，如 BAAI/bge-small-zh-v1.5。"
                              : "ModelScope 上的模型 ID，如 BAAI/bge-small-zh-v1.5。"}
                          </span>
                        </label>
                        {form.knowledge_embedding_provider === "local_huggingface" ? (
                          <label
                            className={`settings-input-group settings-focus-target ${activeFocusTarget === "hf_endpoint" ? "is-highlighted" : ""}`}
                            ref={registerFocusTarget("hf_endpoint") as (node: HTMLLabelElement | null) => void}
                          >
                            <span className="settings-input-label">HuggingFace 镜像地址</span>
                            <input
                              className="settings-input-field"
                              value={form.hf_endpoint}
                              disabled={!form.knowledge_enabled}
                              onChange={(e) => updateForm({ ...form, hf_endpoint: e.target.value })}
                              placeholder="留空使用官方源，或填 https://hf-mirror.com"
                            />
                            <span className="settings-input-caption">国内用户建议填写 https://hf-mirror.com 加速下载。</span>
                          </label>
                        ) : null}
                        <div className="settings-inline-actions" style={{ gridColumn: "1 / -1" }}>
                          <button
                            className="secondary-button"
                            type="button"
                            disabled={embeddingDownloading || !form.knowledge_enabled}
                            onClick={() => void downloadEmbeddingModel()}
                          >
                            {embeddingDownloading ? "下载中..." : "下载模型"}
                          </button>
                          <button
                            className="secondary-button"
                            type="button"
                            disabled={embeddingTesting || embeddingDownloading || !form.knowledge_enabled}
                            onClick={() => void testEmbeddingModel()}
                          >
                            {embeddingTesting ? "验证中..." : "验证模型"}
                          </button>
                          {embeddingVerified ? (
                            <span className="settings-status-pill success">已就绪</span>
                          ) : null}
                        </div>
                        {embeddingStatus ? (
                          <div className={`settings-inline-alert ${embeddingVerified ? "success" : embeddingStatus.includes("失败") ? "warning" : "info"}`}>
                            <span>{embeddingStatus}</span>
                          </div>
                        ) : null}
                        {embeddingOutput ? (
                          <textarea className="textarea-field log-viewer" rows={10} readOnly value={embeddingOutput}></textarea>
                        ) : null}
                      </>
                    ) : null}
                  </div>
                </section>

                {form.knowledge_embedding_provider !== "siliconflow" ? (
                  <section className="settings-tree-panel">
                    <header className="settings-tree-panel-header">
                      <span className="settings-tree-index">03</span>
                      <div>
                        <h3>运行依赖</h3>
                        <p>知识库依赖（chromadb、sentence-transformers）需要到"运行环境"页面统一安装管理。</p>
                      </div>
                    </header>
                    <div className="settings-tree-grid">
                      <div className={`knowledge-deps-status-card ${knowledgeDepsReady ? "status-ready" : hasKnowledgeBrokenDeps ? "status-missing" : "status-missing"}`}>
                        <div className="knowledge-deps-status-icon">
                          {knowledgeDepsReady ? "\u2713" : hasKnowledgeBrokenDeps ? "\u26A0" : "\u26A0"}
                        </div>
                        <div className="knowledge-deps-status-content">
                          <strong className="knowledge-deps-status-title">
                            {knowledgeDepsReady ? "依赖已就绪" : hasKnowledgeBrokenDeps ? "依赖损坏" : `缺失 ${missingKnowledgeDeps.length} 个依赖`}
                          </strong>
                          <p className="knowledge-deps-status-desc">
                            {knowledgeDepsReady
                              ? `chromadb${environment?.chromadbVersion ? ` ${environment.chromadbVersion}` : ""} 与 sentence-transformers${environment?.sentenceTransformersVersion ? ` ${environment.sentenceTransformersVersion}` : ""} 已在当前运行环境可用。`
                              : hasKnowledgeBrokenDeps
                                ? `部分依赖包已安装但无法正常导入，需要重新安装。`
                                : `需要安装：${missingKnowledgeDeps.join("、") || requiredKnowledgePackageText}。`}
                          </p>
                        </div>
                      </div>
                      <div className="knowledge-deps-navigate">
                        <button
                          className="primary-button"
                          type="button"
                          onClick={() => {
                            setActiveCategory("runtime");
                            setTimeout(() => {
                              const target = document.querySelector('[data-focus-key="knowledge_runtime"]');
                              target?.scrollIntoView({ behavior: "smooth", block: "center" });
                            }, 100);
                          }}
                        >
                          前往运行环境管理
                        </button>
                        <span className="settings-input-caption">
                          在运行环境页面可以查看完整依赖树、安装/卸载单个依赖包、查看安装日志。
                        </span>
                      </div>
                    </div>
                  </section>
                ) : null}

                <section className="settings-tree-panel">
                  <header className="settings-tree-panel-header">
                    <span className="settings-tree-index">{form.knowledge_embedding_provider !== "siliconflow" ? "04" : "03"}</span>
                    <div>
                      <h3>知识库 LLM</h3>
                      <p>自动打标和知识库问答可以跟随主 LLM，也可以使用独立配置。</p>
                    </div>
                  </header>
                  <div className="settings-tree-grid">
                    <label
                      className={`settings-input-group settings-focus-target ${activeFocusTarget === "knowledge_llm_mode" ? "is-highlighted" : ""}`}
                      ref={registerFocusTarget("knowledge_llm_mode") as (node: HTMLLabelElement | null) => void}
                    >
                      <span className="settings-input-label">LLM 来源</span>
                      <select
                        className="settings-select-field"
                        value={form.knowledge_llm_mode || "same_as_main"}
                        disabled={!form.knowledge_enabled}
                        onChange={(e) => updateForm({ ...form, knowledge_llm_mode: e.target.value })}
                      >
                        <option value="same_as_main">跟随主 LLM</option>
                        <option value="custom">使用独立配置</option>
                      </select>
                      <span className="settings-input-caption">"跟随主 LLM" 会直接复用摘要 LLM 的 Base URL、API Key 与模型名。</span>
                    </label>
                    {knowledgeLlmUsesCustom ? (
                      <>
                        <label
                          className={`settings-input-group settings-focus-target ${activeFocusTarget === "knowledge_llm_enabled" ? "is-highlighted" : ""}`}
                          ref={registerFocusTarget("knowledge_llm_enabled") as (node: HTMLLabelElement | null) => void}
                        >
                          <span className="settings-input-label">启用知识库 LLM</span>
                          <select
                            className="settings-select-field"
                            value={form.knowledge_llm_enabled ? "true" : "false"}
                            disabled={!form.knowledge_enabled}
                            onChange={(e) => updateForm({ ...form, knowledge_llm_enabled: e.target.value === "true" })}
                          >
                            <option value="false">关闭</option>
                            <option value="true">开启</option>
                          </select>
                        </label>
                        {form.knowledge_llm_enabled ? (
                          <>
                            <label className="settings-input-group" ref={registerFocusTarget("knowledge_llm_provider") as (node: HTMLLabelElement | null) => void}>
                              <span className="settings-input-label">LLM 提供商</span>
                              <select
                                className="settings-select-field"
                                value={form.knowledge_llm_provider || "openai-compatible"}
                                disabled={!form.knowledge_enabled}
                                onChange={(e) => updateForm({ ...form, knowledge_llm_provider: e.target.value })}
                              >
                                <option value="openai-compatible">OpenAI Compatible</option>
                                <option value="openai">OpenAI</option>
                                <option value="anthropic">Anthropic</option>
                                <option value="custom">自建端点</option>
                              </select>
                            </label>
                            <label className="settings-input-group" ref={registerFocusTarget("knowledge_llm_base_url") as (node: HTMLLabelElement | null) => void}>
                              <span className="settings-input-label">API Base URL</span>
                              <input className="settings-input-field" value={form.knowledge_llm_base_url} disabled={!form.knowledge_enabled} onChange={(e) => updateForm({ ...form, knowledge_llm_base_url: e.target.value })} placeholder="https://api.openai.com/v1" />
                              <span className="settings-input-caption">知识库问答与自动打标 LLM API 的基础 URL 地址。</span>
                            </label>
                            <label className="settings-input-group" ref={registerFocusTarget("knowledge_llm_api_key") as (node: HTMLLabelElement | null) => void}>
                              <span className="settings-input-label">API Key</span>
                              <input className="settings-input-field" type="password" value={form.knowledge_llm_api_key} disabled={!form.knowledge_enabled} onFocus={selectMaskedApiKey} onChange={(e) => updateForm({ ...form, knowledge_llm_api_key: e.target.value })} placeholder="sk-..." />
                              <span className="settings-input-caption">知识库 LLM 服务的 API 密钥。</span>
                            </label>
                            <label className="settings-input-group" ref={registerFocusTarget("knowledge_llm_model") as (node: HTMLLabelElement | null) => void}>
                              <span className="settings-input-label">模型名称</span>
                              <input className="settings-input-field" value={form.knowledge_llm_model} disabled={!form.knowledge_enabled} onChange={(e) => updateForm({ ...form, knowledge_llm_model: e.target.value })} placeholder="gpt-4o-mini / claude-3-haiku" />
                              <span className="settings-input-caption">要用于知识库问答和自动打标的 LLM 模型名称。</span>
                            </label>
                            <div className="settings-inline-actions">
                              <button className="secondary-button" type="button" disabled={llmTestBusy || !form.knowledge_enabled} onClick={() => void testKnowledgeLlmConnection()}>
                                {llmTestBusy ? "测试中..." : "测试知识库 LLM"}
                              </button>
                              <span className="settings-input-caption">使用当前表单中的 Base URL、API Key 和模型名临时请求一次，并校验是否能返回合法 JSON，不会保存设置。</span>
                            </div>
                          </>
                        ) : null}
                      </>
                    ) : (
                      <div className={`settings-inline-alert ${knowledgeLlmReady ? "success" : "warning"}`}>
                        <strong>{knowledgeLlmReady ? "知识库当前跟随主 LLM" : "知识库当前跟随主 LLM，但主 LLM 还未补全"}</strong>
                        <span>{knowledgeLlmReady ? "自动打标和问答会直接复用主 LLM 配置。" : "请先启用主 LLM，并补全 API Key、Base URL 与模型名，或切换为独立配置。"}</span>
                      </div>
                    )}
                  </div>
                </section>
              </div>
            </section>
          )}

          {activeCategory === "prompts" && (
            <section className="settings-category-section settings-prompts-section">
              <header className="settings-category-header">
                <h2>提示词</h2>
                <p>这里属于高级个性化区域。想改变知识笔记风格时再调整，日常使用保持默认即可。</p>
              </header>
              <div className="settings-prompt-scope-alert">
                <span className="settings-prompt-scope-kicker">生效范围</span>
                <p>首页选择的 Prompt 预设只影响摘要生成；知识笔记和图文笔记使用下方全局模板。恢复默认或修改模板后，需要点击左侧"保存设置"才会生效。</p>
              </div>
              {hasOuterSectionsOpen && (
                <div className="settings-prompt-global-sticky">
                  <span className="settings-prompt-toolbar-title">已展开 {outerSectionsOpen.size} 项</span>
                  <button className="settings-prompt-inline-action" type="button" onClick={(e) => { e.preventDefault(); collapseAllOuter(); }}>收起全部</button>
                </div>
              )}
              <div className="settings-prompt-list">
                <details className="settings-prompt-collapse" ref={(node) => { promptDetailsRefs.current.summary = node; }} onToggle={(e) => handleOuterToggle("summary", e)}>
                  <summary className="settings-prompt-collapse-summary">
                    <span className="settings-prompt-summary-copy">
                      <span className="settings-prompt-summary-title">摘要 Prompt</span>
                      <span className="settings-prompt-summary-desc">控制视频摘要的角色、结构和输出格式。</span>
                    </span>
                    <span className="settings-prompt-summary-meta">
                      <span className="settings-prompt-chip accent">核心</span>
                      <span className="settings-prompt-open-label">展开</span>
                    </span>
                  </summary>
                  <div className="settings-form-group">
                  <label className="settings-input-group" ref={registerFocusTarget("summary_system_prompt") as (node: HTMLLabelElement | null) => void}>
                    <span className="settings-input-label">摘要 System Prompt</span>
                    <textarea
                      className="textarea-field"
                      rows={5}
                      value={form.summary_system_prompt || ""}
                      onChange={(e) => updateForm({ ...form, summary_system_prompt: e.target.value })}
                    />
                    <div className="settings-inline-actions">
                      <button className="secondary-button" type="button" onClick={() => resetSummaryPrompt("system")}>
                        恢复默认
                      </button>
                      {undoPromptValues?.summary_system_prompt !== undefined && (
                        <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                      )}
                      <span className="settings-input-caption">控制视频摘要生成时的角色、风格和整体约束。</span>
                    </div>
                  </label>
                  <label className="settings-input-group" ref={registerFocusTarget("summary_user_prompt_template") as (node: HTMLLabelElement | null) => void}>
                    <span className="settings-input-label">摘要 User Template</span>
                    <textarea
                      className="textarea-field"
                      rows={14}
                      value={form.summary_user_prompt_template || ""}
                      onChange={(e) => updateForm({ ...form, summary_user_prompt_template: e.target.value })}
                    />
                    <div className="settings-inline-actions">
                      <button className="secondary-button" type="button" onClick={() => resetSummaryPrompt("template")}>
                        恢复默认
                      </button>
                      {undoPromptValues?.summary_user_prompt_template !== undefined && (
                        <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                      )}
                      <span className="settings-input-caption">
                        可用变量：{"{title}"}、{"{transcript}"}、{"{segments_json}"}。生成 JSON 包含 title/overview/bulletPoints/chapters/chapterGroups。
                      </span>
                    </div>
                  </label>
                  </div>
                </details>

                {/* Custom collapse for presets section (no native <details> — breaks position:sticky) */}
                <div className={`settings-prompt-collapse settings-focus-target ${presetsSectionOpen ? " open" : ""}`} ref={registerFocusTarget("prompt_presets_library") as (node: HTMLDivElement | null) => void}>
                  <div
                    className="settings-prompt-collapse-summary"
                    role="button"
                    tabIndex={0}
                    aria-expanded={presetsSectionOpen}
                    onClick={handlePresetsSectionToggle}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handlePresetsSectionToggle(); }}}
                  >
                    <span className="settings-prompt-summary-copy">
                      <span className="settings-prompt-summary-title">Prompt 预设库</span>
                      <span className="settings-prompt-summary-desc">管理首页摘要 Prompt 下拉框和自动推荐候选。</span>
                    </span>
                    <span className="settings-prompt-summary-meta">
                      <span className="settings-prompt-chip">内置 {builtinPresetCount}</span>
                      <span className="settings-prompt-chip muted">隐藏 {hiddenBuiltinPresetCount}</span>
                      <span className="settings-prompt-chip success">新增 {customPresetCount}</span>
                      <span className="settings-prompt-open-label">展开</span>
                    </span>
                  </div>
                  {presetsSectionOpen && (
                    <div className="settings-form-group" style={{ position: "relative" }}>
                    <div className="settings-preset-section-note">
                      <strong>摘要预设</strong>
                      <span>用于首页 Prompt 下拉框和自动推荐，只替换摘要 System Prompt / User Template。内置预设可查看或隐藏，新增预设可编辑或删除。</span>
                    </div>
                    {hiddenBuiltinPresets.length ? (
                      <div className="settings-hidden-preset-list">
                        <span className="settings-input-caption">已隐藏内置预设</span>
                        <div className="settings-hidden-preset-actions">
                          {hiddenBuiltinPresets.map((preset) => (
                            <button className="secondary-button" type="button" key={preset.id} onClick={() => setBuiltinPresetHidden(preset.id, false)}>
                              恢复 {preset.name}
                            </button>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {promptPresetsLoading ? (
                      <span className="settings-input-caption">加载中...</span>
                    ) : promptPresets.length === 0 ? (
                      <span className="settings-input-caption">暂无预设</span>
                    ) : (
                      <>
                        {visiblePresets.map(renderPresetCard)}
                      </>
                    )}
                    {showNewPresetForm && (
                      <div className="settings-preset-card new">
                        <div className="settings-preset-edit-body">
                          <h4>新建预设</h4>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">名称 *</span>
                            <input className="settings-input-field" value={presetForm.name} onChange={(e) => setPresetForm({ ...presetForm, name: e.target.value })} placeholder="预设名称（用作 ID）" />
                          </label>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">描述</span>
                            <input className="settings-input-field" value={presetForm.description || ""} onChange={(e) => setPresetForm({ ...presetForm, description: e.target.value })} />
                          </label>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">分类</span>
                            <input className="settings-input-field" value={presetForm.category || ""} onChange={(e) => setPresetForm({ ...presetForm, category: e.target.value })} placeholder="例如: 教程、会议、娱乐" />
                          </label>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">自动匹配关键词（逗号分隔）</span>
                            <input className="settings-input-field" value={presetForm.auto_match_keywords?.join("、") || ""} onChange={(e) => setPresetForm({ ...presetForm, auto_match_keywords: e.target.value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean) })} placeholder="例如: 教程、教学、入门" />
                          </label>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">System Prompt *</span>
                            <textarea className="textarea-field" rows={4} value={presetForm.system_prompt} onChange={(e) => setPresetForm({ ...presetForm, system_prompt: e.target.value })} />
                          </label>
                          <label className="settings-input-group settings-preset-field">
                            <span className="settings-input-label">User Template *</span>
                            <textarea className="textarea-field" rows={10} value={presetForm.user_prompt_template} onChange={(e) => setPresetForm({ ...presetForm, user_prompt_template: e.target.value })} />
                          </label>
                          <div className="settings-preset-actions">
                            <button className="primary-button" type="button" disabled={presetSaveBusy} onClick={savePreset}>
                              {presetSaveBusy ? "创建中..." : "创建预设"}
                            </button>
                            <button className="secondary-button" type="button" onClick={cancelPresetEdit}>取消</button>
                          </div>
                        </div>
                      </div>
                    )}
                    {presetStatus && <p className="settings-input-caption" style={{ marginTop: 8, color: presetStatus.includes("失败") ? "var(--danger)" : "var(--success)" }}>{presetStatus}</p>}
                    {!showNewPresetForm && !promptPresetsLoading && (
                      <button className="secondary-button" type="button" onClick={startNewPreset} style={{ marginTop: 12 }}>
                        + 新建预设
                      </button>
                    )}
                    </div>
                  )}
                </div>

                <details className="settings-prompt-collapse" ref={(node) => { promptDetailsRefs.current.knowledge = node; }} onToggle={(e) => handleOuterToggle("knowledge", e)}>
                  <summary className="settings-prompt-collapse-summary">
                    <span className="settings-prompt-summary-copy">
                      <span className="settings-prompt-summary-title">知识笔记 Prompt</span>
                      <span className="settings-prompt-summary-desc">控制知识笔记的整理风格、Markdown 结构和变量使用。</span>
                    </span>
                    <span className="settings-prompt-summary-meta">
                      <span className="settings-prompt-chip">全局模板</span>
                      <span className="settings-prompt-open-label">展开</span>
                    </span>
                  </summary>
                  <div className="settings-form-group">
                  <label className="settings-input-group" ref={registerFocusTarget("knowledge_note_system_prompt") as (node: HTMLLabelElement | null) => void}>
                    <span className="settings-input-label">知识笔记 System Prompt</span>
                    <textarea
                      className="textarea-field"
                      rows={5}
                      value={form.knowledge_note_system_prompt || ""}
                      onChange={(e) => updateForm({ ...form, knowledge_note_system_prompt: e.target.value })}
                    />
                    <div className="settings-inline-actions">
                      <button className="secondary-button" type="button" onClick={() => resetKnowledgeNotePrompt("system")}>
                        恢复默认
                      </button>
                      {undoPromptValues?.knowledge_note_system_prompt !== undefined && (
                        <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                      )}
                      <span className="settings-input-caption">控制知识笔记生成时的角色、风格和整体约束。</span>
                    </div>
                  </label>
                  <label className="settings-input-group" ref={registerFocusTarget("knowledge_note_user_prompt_template") as (node: HTMLLabelElement | null) => void}>
                    <span className="settings-input-label">知识笔记 User Template</span>
                    <textarea
                      className="textarea-field"
                      rows={14}
                      value={form.knowledge_note_user_prompt_template || ""}
                      onChange={(e) => updateForm({ ...form, knowledge_note_user_prompt_template: e.target.value })}
                    />
                    <div className="settings-inline-actions">
                      <button className="secondary-button" type="button" onClick={() => resetKnowledgeNotePrompt("template")}>
                        恢复默认
                      </button>
                      {undoPromptValues?.knowledge_note_user_prompt_template !== undefined && (
                        <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                      )}
                      <span className="settings-input-caption">
                        可用变量：{"{title}"}、{"{transcript_excerpt}"}、{"{segments_excerpt}"}、{"{summary_json}"}。
                      </span>
                    </div>
                  </label>
                  <div className="settings-guide-card">
                    <div>
                      <strong>想调整知识笔记样式？</strong>
                      <span>查看变量、默认结构和常见改法，避免破坏 JSON 输出格式。</span>
                    </div>
                    <button className="secondary-button" type="button" onClick={() => setKnowledgePromptGuideOpen(true)}>
                      打开教程
                    </button>
                  </div>
                  </div>
                </details>

                <details className="settings-prompt-collapse" ref={(node) => { promptDetailsRefs.current.visual = node; }} onToggle={(e) => handleOuterToggle("visual", e)}>
                  <summary className="settings-prompt-collapse-summary">
                    <span className="settings-prompt-summary-copy">
                      <span className="settings-prompt-summary-title">图文笔记 Prompt</span>
                      <span className="settings-prompt-summary-desc">控制关键帧选择、画面理解和图文笔记整合。</span>
                    </span>
                    <span className="settings-prompt-summary-meta">
                      <span className="settings-prompt-chip info">VLM</span>
                      <span className="settings-prompt-open-label">展开</span>
                    </span>
                  </summary>
                  <div className="settings-form-group">
                  <label className="settings-input-group" ref={registerFocusTarget("visual_note_system_prompt") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">图文笔记 System Prompt</span>
                  <textarea
                    className="textarea-field"
                    rows={5}
                    value={form.visual_note_system_prompt || ""}
                    onChange={(e) => updateForm({ ...form, visual_note_system_prompt: e.target.value })}
                  />
                  <div className="settings-inline-actions">
                    <button className="secondary-button" type="button" onClick={() => resetVisualNotePrompt("system")}>
                      恢复默认
                    </button>
                    {undoPromptValues?.visual_note_system_prompt !== undefined && (
                      <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                    )}
                    <span className="settings-input-caption">控制理解型图文笔记如何把图片解析整合进正文。</span>
                  </div>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("visual_frame_planning_prompt") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">捕获帧规划 Prompt</span>
                  <textarea
                    className="textarea-field"
                    rows={10}
                    value={form.visual_frame_planning_prompt || ""}
                    onChange={(e) => updateForm({ ...form, visual_frame_planning_prompt: e.target.value })}
                  />
                  <div className="settings-inline-actions">
                    <button className="secondary-button" type="button" onClick={() => resetVisualNotePrompt("planning")}>
                      恢复默认
                    </button>
                    {undoPromptValues?.visual_frame_planning_prompt !== undefined && (
                      <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                    )}
                    <span className="settings-input-caption">
                      可用变量：{"{title}"}、{"{mode}"}、{"{summary_json}"}、{"{knowledge_note_markdown}"}、{"{segments_excerpt}"}、{"{max_frames}"}。
                    </span>
                  </div>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("visual_vlm_prompt") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">画面理解 Prompt</span>
                  <textarea
                    className="textarea-field"
                    rows={10}
                    value={form.visual_vlm_prompt || ""}
                    onChange={(e) => updateForm({ ...form, visual_vlm_prompt: e.target.value })}
                  />
                  <div className="settings-inline-actions">
                    <button className="secondary-button" type="button" onClick={() => resetVisualNotePrompt("vlm")}>
                      恢复默认
                    </button>
                    {undoPromptValues?.visual_vlm_prompt !== undefined && (
                      <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                    )}
                    <span className="settings-input-caption">
                      可用变量：{"{title}"}、{"{timestamp}"}、{"{context}"}、{"{timeline_hint}"}、{"{knowledge_note_markdown}"}。
                    </span>
                  </div>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("visual_note_user_prompt_template") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">图文笔记 User Template</span>
                  <textarea
                    className="textarea-field"
                    rows={12}
                    value={form.visual_note_user_prompt_template || ""}
                    onChange={(e) => updateForm({ ...form, visual_note_user_prompt_template: e.target.value })}
                  />
                  <div className="settings-inline-actions">
                    <button className="secondary-button" type="button" onClick={() => resetVisualNotePrompt("template")}>
                      恢复默认
                    </button>
                    {undoPromptValues?.visual_note_user_prompt_template !== undefined && (
                      <button className="secondary-button" type="button" onClick={undoPromptReset}>回退设置</button>
                    )}
                    <span className="settings-input-caption">
                      可用变量：{"{title}"}、{"{knowledge_note_markdown}"}、{"{visual_observations_json}"}。
                    </span>
                  </div>
                </label>
                  </div>
                </details>
              </div>

            </section>
          )}

          {activeCategory === "performance" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>性能调优</h2>
                <p>控制任务级并发与单任务内部分块并发，减少本地资源争抢和云端限流压力。</p>
              </header>
              <div className="settings-form-group">
                <label className="settings-input-group" ref={registerFocusTarget("task_concurrency") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">任务并发数</span>
                  <input className="settings-input-field" type="number" min={1} value={form.task_concurrency} onChange={(e) => updateForm({ ...form, task_concurrency: parseMinOneInt(e.target.value, recommendedTaskConcurrency) })} />
                  <span className="settings-input-caption">影响下载、转写、摘要的整体链路吞吐；云 API 可能存在并发限流，建议按当前环境推荐值设置。</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("mindmap_concurrency") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">导图并发数</span>
                  <input className="settings-input-field" type="number" min={1} value={form.mindmap_concurrency} onChange={(e) => updateForm({ ...form, mindmap_concurrency: parseMinOneInt(e.target.value, 1) })} />
                  <span className="settings-input-caption">影响摘要完成后的导图生成吞吐，不会占用摘要任务的并发槽位；建议保持 1。</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("summary_chunk_concurrency") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">摘要分块并发数</span>
                  <input className="settings-input-field" type="number" min={1} value={form.summary_chunk_concurrency} onChange={(e) => updateForm({ ...form, summary_chunk_concurrency: parseMinOneInt(e.target.value, 2) })} />
                  <span className="settings-input-caption">仅控制单个摘要任务内部同时请求的分块数量，不等同于任务并发数。</span>
                </label>
              </div>
              <div className="settings-form-group">
                <div className="settings-input-group">
                  <span className="settings-input-label">当前建议</span>
                  <span className="settings-input-caption">{performanceRecommendation}</span>
                </div>
                <div className="settings-input-group settings-performance-tasklist-entry">
                  <span className="settings-input-label">当前任务队列</span>
                  <div className="settings-inline-actions">
                    <button className="secondary-button" type="button" onClick={() => setTaskListOpen(true)}>
                      查看 tasklist
                    </button>
                    <span className="settings-input-caption">使用悬浮窗快速查看最近任务，避免占用主要设置区域。</span>
                  </div>
                </div>
              </div>
            </section>
          )}

          {activeCategory === "performance" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>资源策略</h2>
                <p>根据机器和任务规模调整 CUDA 版本、运行环境通道和缓存策略。</p>
              </header>
              <div className="settings-form-group">
                <label className="settings-input-group" ref={registerFocusTarget("cuda_variant") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">CUDA 变体</span>
                  <select className="settings-select-field" value={form.cuda_variant} onChange={(e) => updateForm({ ...form, cuda_variant: e.target.value })}>
                    <option value="cu128">CUDA 12.8</option>
                    <option value="cu126">CUDA 12.6</option>
                    <option value="cu124">CUDA 12.4</option>
                  </select>
                  <span className="settings-input-caption">PyTorch CUDA 版本</span>
                </label>
                <label className="settings-input-group" ref={registerFocusTarget("runtime_channel") as (node: HTMLLabelElement | null) => void}>
                  <span className="settings-input-label">运行环境通道</span>
                  <select className="settings-select-field" value={form.runtime_channel} onChange={(e) => updateForm({ ...form, runtime_channel: e.target.value })}>
                    <option value="base">基础版</option>
                    <option value="gpu-cu128">GPU CUDA12.8</option>
                    <option value="gpu-cu126">GPU CUDA12.6</option>
                    <option value="gpu-cu124">GPU CUDA12.4</option>
                  </select>
                </label>
              </div>
            </section>
          )}

          {activeCategory === "runtime" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>运行环境</h2>
                <p>环境检测信息、CUDA 配置和本地 ASR 安装。</p>
              </header>
              <div className="env-summary-grid settings-env-grid" ref={registerFocusTarget("runtime_status") as (node: HTMLDivElement | null) => void}>
                <div className="metric-card">
                  <span className="metric-label">推荐设备</span>
                  <strong className="metric-value">{environment?.recommendedDevice || "-"}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">请求设备</span>
                  <strong className="metric-value">{devicePreferenceLabel(form.device_preference)}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">生效设备</span>
                  <strong className={`metric-value ${(form.transcription_provider === "funasr" ? normalizeDevicePreference(form.funasr_device) : normalizeDevicePreference(form.whisper_device)) === "cuda" ? "text-success" : ""}`}>{form.transcription_provider === "funasr" ? devicePreferenceLabel(form.funasr_device) : devicePreferenceLabel(form.whisper_device)}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">推荐模型</span>
                  <strong className="metric-value">{environment?.recommendedModel || "-"}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">GPU 状态</span>
                  <strong className={`metric-value ${environment?.cudaAvailable ? "text-success" : ""}`}>{environment?.cudaAvailable ? "已启用" : "未启用"}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">GPU 名称</span>
                  <strong className="metric-value">{environment?.gpuName || "未检测到"}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">Torch</span>
                  <strong className={`metric-value ${environment?.torchInstalled ? "text-success" : ""}`}>{environment?.torchInstalled ? environment?.torchVersion || "已安装" : "未安装"}</strong>
                </div>
                <div className="metric-card">
                  <span className="metric-label">Python</span>
                  <strong className="metric-value">{environment?.pythonVersion || "-"}</strong>
                </div>
              </div>
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">CUDA 目标版本</h3>
                <div className="cuda-insight-grid">
                  <div className="setting-row">
                    <span className="setting-label">目标运行环境</span>
                    <span className="setting-value">{targetRuntimeChannel}</span>
                  </div>
                  <div className="setting-row">
                    <span className="setting-label">当前运行环境</span>
                    <span className="setting-value">{environment?.runtimeChannel || form.runtime_channel || "base"}</span>
                  </div>
                  <div className="setting-row">
                    <span className="setting-label">运行环境状态</span>
                    <span className="setting-value">{environment?.runtimeReady ? "已就绪" : environment?.runtimeError ? "异常" : "检测中..."}</span>
                  </div>
                </div>
                <span className="input-label">CUDA 目标版本</span>
                <div className="settings-actions cuda-button-row">
                    <select
                      className="select-field cuda-select-field"
                      value={form.cuda_variant}
                      disabled={cudaInstalling}
                      onChange={(event) => updateForm({ ...form, cuda_variant: event.target.value })}
                    >
                      <option value="cu128">CUDA 12.8</option>
                      <option value="cu126">CUDA 12.6</option>
                      <option value="cu124">CUDA 12.4</option>
                    </select>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={cudaInstalling}
                    onClick={async () => {
                      try {
                        setCudaStatus("正在重新检测环境...");
                        const nextEnvironment = await api.getEnvironment({ runtimeChannel: form.runtime_channel, refresh: true });
                        setEnvironment(nextEnvironment);
                        setCudaStatus("环境检测完成");
                        onRefresh();
                      } catch (error) {
                        setCudaStatus(error instanceof Error ? error.message : "环境检测失败");
                      }
                    }}
                  >
                    重新检测
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={cudaInstalling}
                    onClick={async () => {
                      const sessionId = `cuda-${Date.now()}`;
                      let pollTimer: ReturnType<typeof setInterval> | null = null;
                      try {
                        setCudaInstalling(true);
                        setCudaStatus("正在安装 CUDA 支持...");
                        setCudaOutput("");
                        setCudaProgress(0);
                        // Start polling install log
                        pollTimer = setInterval(async () => {
                          try {
                            const log = await api.getInstallLog(sessionId);
                            if (log.log) setCudaOutput(log.log);
                            if (typeof log.progress === "number") setCudaProgress(log.progress);
                            if (log.done && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
                          } catch { /* ignore poll errors */ }
                        }, 1500);
                        const result = await api.installCuda({ cuda_variant: form.cuda_variant, installSessionId: sessionId });
                        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
                        setCudaProgress(100);
                        // Final log fetch
                        try { const log = await api.getInstallLog(sessionId); if (log.log) setCudaOutput(log.log); } catch { /* ignore */ }
                        const nextRuntimeChannel = result.runtimeChannel || form.runtime_channel;
                        setCudaInstalling(false);
                        setCudaStatus(
                          result.restartRequired
                            ? "CUDA 安装完成，请重启应用后切换到新的 GPU 运行环境"
                            : "CUDA 安装完成"
                        );
                        setForm({ ...form, runtime_channel: nextRuntimeChannel, cuda_variant: result.cudaVariant || form.cuda_variant });
                        setIsDirty(false);
                        setEnvironment(await api.getEnvironment({ runtimeChannel: nextRuntimeChannel, refresh: true }));
                        onRefresh();
                      } catch (error) {
                        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
                        setCudaInstalling(false);
                        setCudaStatus(error instanceof Error ? error.message : "CUDA 安装失败");
                      }
                    }}
                  >
                    {cudaInstalling ? "安装中..." : "安装 CUDA 支持"}
                  </button>
                </div>
                {(cudaInstalling || cudaStatus || cudaOutput) ? (
                  <div className="settings-form-group">
                    <div className="settings-input-group">
                      <span className="settings-input-label">CUDA 安装输出</span>
                      {cudaInstalling && cudaProgress > 0 ? (
                        <div className="progress-bar-simple" style={{ marginBottom: 8 }}>
                          <div className="progress-fill-simple" style={{ width: `${cudaProgress}%` }} />
                        </div>
                      ) : null}
                      <textarea className="textarea-field log-viewer" rows={12} readOnly value={cudaOutput || (cudaInstalling ? "安装中..." : cudaStatus)} />
                    </div>
                  </div>
                ) : null}
              </div>
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">运行环境更新检查</h3>
                <div className="settings-runtime-toolbar">
                  <span className={`settings-inline-alert ${outdatedRuntimeChannels.length > 0 ? "warning" : "success"}`}>
                    <strong>{outdatedRuntimeChannels.length > 0 ? "有运行环境需要同步" : "运行环境基础版本一致"}</strong>
                    <span>
                      {outdatedRuntimeChannels.length > 0
                        ? `${outdatedRuntimeChannels.map((channel) => channel.runtimeChannel).join("、")} 需要同步基础文件；同步会保留 CUDA / ASR / 知识库扩展包。`
                        : `当前基础版本 ${runtimeStatus?.baseAppVersion || "-"}，每 30 分钟自动检查一次。`}
                    </span>
                  </span>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={runtimeStatusLoading}
                    onClick={() => void refreshRuntimeStatus()}
                  >
                    {runtimeStatusLoading ? "检查中..." : "检查所有 runtime"}
                  </button>
                  {outdatedRuntimeChannels.length > 0 && (
                    <button
                      className="primary-button compact"
                      type="button"
                      disabled={runtimeSyncing || runtimeStatusLoading}
                      onClick={() => void syncRuntimeChannels()}
                    >
                      {runtimeSyncing ? "同步中..." : "同步需要更新的 runtime"}
                    </button>
                  )}
                </div>
                {runtimeStatusMessage ? (
                  <p className="settings-input-caption runtime-status-message">{runtimeStatusMessage}</p>
                ) : null}
                <div className="runtime-channel-list" role="list">
                  {(runtimeStatus?.channels || []).map((channel) => (
                    <div className="runtime-channel-row" key={channel.runtimeChannel} role="listitem">
                      <div>
                        <strong>{channel.runtimeChannel}</strong>
                        <span>{channel.ready ? "已就绪" : channel.exists ? "缺少 Python" : "未创建"}</span>
                      </div>
                      <div>
                        <span>{channel.appVersion || "-"}</span>
                        <span>{channel.needsUpdate ? "需同步" : channel.exists ? "最新" : "按需创建"}</span>
                      </div>
                    </div>
                  ))}
                </div>
                <span className="settings-input-caption">
                  已启用国内镜像回退：{pipIndexSummary}。安装失败时会自动从官方源切换到国内镜像继续尝试。
                </span>
              </div>
              <div className="runtime-subsection-list">
                <RuntimeSection
                  title="本地 ASR 运行环境"
                  status={localAsrInstalled ? "已安装" : "未安装"}
                  tone={localAsrInstalled ? "installed" : "missing"}
                  summary={localAsrInstalled ? `faster-whisper ${environment?.localAsrVersion || "已就绪"}` : "安装后可切换到本地 Whisper 转写"}
                  open={localAsrSectionOpen}
                  onToggle={() => setLocalAsrSectionOpen(!localAsrSectionOpen)}
                  focusKey="local_asr_runtime"
                  highlighted={activeFocusTarget === "local_asr_runtime"}
                  sectionRef={registerFocusTarget("local_asr_runtime") as (node: HTMLElement | null) => void}
                >
                  <div className="runtime-subsection-intro">
                    <div className="runtime-subsection-copy">
                      <strong>{localAsrInstalled ? "本地 Whisper 已可用" : "安装到当前 runtime"}</strong>
                      <span>
                        {localAsrInstalled
                          ? "当前运行环境可直接使用本地语音识别；重新安装会保留同一运行环境通道并刷新依赖。"
                          : "正式安装包默认不包含本地 ASR，安装完成后会自动切换到本地模式。"}
                      </span>
                    </div>
                    <div className="runtime-subsection-actions">
                      <button className="secondary-button" type="button" disabled={localAsrInstalling} onClick={() => void installLocalAsr()}>
                        {localAsrInstalling ? "安装中..." : localAsrInstalled ? "重新安装本地 ASR" : "安装本地 ASR"}
                      </button>
                      {localAsrStatus ? (
                        <span className={`helper-chip runtime-operation-chip ${localAsrStatus.includes("失败") ? "status-error" : localAsrStatus.includes("安装") || localAsrStatus.includes("已") ? "status-success" : "status-info"}`}>
                          {localAsrStatus}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  <DependencyTree
                    title="本地 ASR 依赖"
                    items={buildAsrDependencyTree(environment, "local")}
                    runtimeChannel={form.runtime_channel}
                    installKind="local"
                    onStatusChange={handleDependencyStatusChange}
                  />
                  {localAsrOutput ? (
                    <div className="runtime-log-block">
                      <span className="runtime-block-title">安装输出</span>
                      <textarea className="textarea-field log-viewer" rows={8} readOnly value={localAsrOutput}></textarea>
                    </div>
                  ) : null}
                </RuntimeSection>

                <RuntimeSection
                  title="FunASR 运行环境"
                  status={funasrInstalled ? "已安装" : "未安装"}
                  tone={funasrInstalled ? "installed" : "missing"}
                  summary={funasrInstalled ? `funasr ${environment?.funasrVersion || "已就绪"}` : "中文转写增强，包含 PyTorch 依赖"}
                  open={funasrSectionOpen}
                  onToggle={() => setFunasrSectionOpen(!funasrSectionOpen)}
                  focusKey="funasr_runtime"
                  highlighted={activeFocusTarget === "funasr_runtime"}
                  sectionRef={registerFocusTarget("funasr_runtime") as (node: HTMLElement | null) => void}
                >
                  <div className="runtime-subsection-intro">
                    <div className="runtime-subsection-copy">
                      <strong>{funasrInstalled ? "FunASR 已可用" : "安装中文 ASR 增强包"}</strong>
                      <span>
                        {funasrInstalled
                          ? "当前运行环境已经通过 FunASR 检测；重新安装会自动修复 torch、torchvision、torchaudio 等子依赖。"
                          : "FunASR 是阿里开源语音识别引擎，依赖包含 PyTorch，安装体积较大，请耐心等待。"}
                      </span>
                    </div>
                    <div className="runtime-subsection-actions">
                      <button className="secondary-button" type="button" disabled={funasrInstalling} onClick={() => void installFunAsr()}>
                        {funasrInstalling ? "安装中..." : funasrInstalled ? "重新安装 FunASR" : "安装 FunASR"}
                      </button>
                      {funasrStatus ? (
                        <span className={`helper-chip runtime-operation-chip ${funasrStatus.includes("失败") ? "status-error" : funasrStatus.includes("已安装") || funasrStatus.includes("成功") ? "status-success" : "status-info"}`}>
                          {funasrStatus}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  <DependencyTree
                    title="FunASR 依赖"
                    items={buildAsrDependencyTree(environment, "funasr")}
                    runtimeChannel={form.runtime_channel}
                    installKind="funasr"
                    onStatusChange={handleDependencyStatusChange}
                  />
                  {funasrOutput ? (
                    <div className="runtime-log-block">
                      <span className="runtime-block-title">安装输出</span>
                      <textarea className="textarea-field log-viewer" rows={8} readOnly value={funasrOutput} />
                    </div>
                  ) : null}
                </RuntimeSection>

                <RuntimeSection
                  title="知识库运行环境"
                  status={knowledgeRuntimeStatus}
                  tone={knowledgeRuntimeTone}
                  summary={knowledgeRuntimeSummary}
                  open={knowledgeSectionOpen}
                  onToggle={() => setKnowledgeSectionOpen(!knowledgeSectionOpen)}
                  focusKey="knowledge_runtime"
                  highlighted={activeFocusTarget === "knowledge_runtime"}
                  sectionRef={registerFocusTarget("knowledge_runtime") as (node: HTMLElement | null) => void}
                >
                  <div className="runtime-subsection-intro">
                    <div className="runtime-subsection-copy">
                      <strong>{knowledgeDepsReady ? "知识库依赖已就绪" : hasKnowledgeBrokenDeps ? "依赖需要修复" : "安装知识库依赖"}</strong>
                      <span>
                        {knowledgeEmbeddingUsesSiliconFlow
                          ? "当前 provider: siliconflow。本区域只负责本地知识库索引存储依赖，在线向量模型请在「知识库与问答」里测试。"
                          : `当前 provider: ${form.knowledge_embedding_provider || "local_huggingface"}。本区域负责知识库索引存储和本地向量模型所需的 Python 依赖。`}
                      </span>
                    </div>
                    <div className="runtime-subsection-actions">
                      <button className="secondary-button" type="button" disabled={knowledgeDepsInstalling} onClick={() => void installKnowledgeDependencies()}>
                        {knowledgeDepsInstalling ? "安装中..." : knowledgeDepsReady ? "重新安装知识库依赖" : hasKnowledgeBrokenDeps ? "重新安装知识库依赖" : "安装知识库依赖"}
                      </button>
                      {knowledgeDepsStatus ? (
                        <span className={`helper-chip runtime-operation-chip ${knowledgeDepsStatus.includes("成功") || knowledgeDepsStatus.includes("就绪") ? "status-success" : knowledgeDepsStatus.includes("失败") ? "status-error" : "status-info"}`}>
                          {knowledgeDepsStatus}
                        </span>
                      ) : null}
                    </div>
                  </div>

                  {knowledgeRequirements && (
                    <div
                      className="runtime-subsection-group"
                      ref={registerFocusTarget("knowledge_dependencies") as (node: HTMLDivElement | null) => void}
                    >
                      <DependencyTree
                        title="知识库依赖"
                        items={buildKnowledgeDependencyTree(knowledgeRequirements, environment)}
                        runtimeChannel={form.runtime_channel}
                        installKind="knowledge"
                        provider={form.knowledge_embedding_provider || "local_huggingface"}
                        onStatusChange={handleDependencyStatusChange}
                      />
                    </div>
                  )}

                  {knowledgeDepsOutput ? (
                    <div className="runtime-log-block">
                      <span className="runtime-block-title">安装输出</span>
                      <textarea className="textarea-field log-viewer" rows={8} readOnly value={knowledgeDepsOutput}></textarea>
                    </div>
                  ) : null}

                  {(environment?.chromadbError || environment?.sentenceTransformersError || environment?.modelscopeError) && (
                    <div className="runtime-log-block">
                      <div className="settings-inline-alert warning runtime-inline-alert">
                        <strong>依赖损坏，需要重新安装</strong>
                        <span>部分依赖包已安装但无法正常导入，请点击上方“重新安装知识库依赖”自动修复。</span>
                      </div>
                      <span className="runtime-block-title runtime-block-title-warning">包导入错误</span>
                      <div className="runtime-error-stack">
                        {environment.chromadbError && (
                          <div className="runtime-error-item">
                            <span className="runtime-error-label">chromadb</span>
                            <textarea className="textarea-field log-viewer runtime-error-textarea" rows={4} readOnly value={environment.chromadbError} />
                          </div>
                        )}
                        {environment.sentenceTransformersError && (
                          <div className="runtime-error-item">
                            <span className="runtime-error-label">sentence-transformers</span>
                            <textarea className="textarea-field log-viewer runtime-error-textarea" rows={4} readOnly value={environment.sentenceTransformersError} />
                          </div>
                        )}
                        {environment.modelscopeError && (
                          <div className="runtime-error-item">
                            <span className="runtime-error-label">modelscope</span>
                            <textarea className="textarea-field log-viewer runtime-error-textarea" rows={4} readOnly value={environment.modelscopeError} />
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {environment?.knowledgeDependenciesError && !environment?.chromadbError && !environment?.sentenceTransformersError && !environment?.modelscopeError && (
                    <div className="runtime-log-block is-error">
                      <span className="runtime-block-title runtime-block-title-danger">知识库依赖错误</span>
                      <textarea className="textarea-field log-viewer runtime-error-textarea is-error" rows={6} readOnly value={environment.knowledgeDependenciesError} />
                    </div>
                  )}
                </RuntimeSection>
              </div>
              {environment?.runtimeError ? (
                <div className="settings-form-group">
                  <div className="settings-input-group">
                    <span className="settings-input-label">运行环境错误详情</span>
                    <textarea className="textarea-field log-viewer" rows={8} readOnly value={environment.runtimeError} />
                  </div>
                </div>
              ) : null}
            </section>
          )}

          {activeCategory === "logs" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>日志与控制</h2>
                <p>查看服务日志、管理后端进程、配置桌面行为与监听参数。</p>
              </header>

              {/* ═══════ 服务状态 ═══════ */}
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">服务状态</h3>
                <div className="control-status-row" ref={registerFocusTarget("service_logs") as (node: HTMLDivElement | null) => void}>
                  <span className={`helper-chip ${serviceOnline ? "status-success" : backendRunning ? "status-running" : "status-failed"}`}>{serviceOnline ? "服务在线" : backendRunning ? "启动中..." : "服务离线"}</span>
                  <span className={`helper-chip ${backendRunning ? (backendReady ? "status-success" : "status-running") : "status-pending"}`}>
                    {backendRunning ? (backendReady ? "内置后端运行中" : "内置后端启动中") : "内置后端未运行"}
                  </span>
                  {desktop.backend?.pid ? <span className="helper-chip">PID {desktop.backend.pid}</span> : null}
                </div>
                <div className="cuda-insight-grid">
                  <div className="setting-row"><span className="setting-label">服务名</span><span className="setting-value">{snapshot.systemInfo?.application?.name || "-"}</span></div>
                  <div className="setting-row"><span className="setting-label">版本</span><span className="setting-value">{snapshot.systemInfo?.application?.version || "-"}</span></div>
                  <div className="setting-row"><span className="setting-label">日志文件</span><span className="setting-value" style={{ fontSize: "0.82rem", wordBreak: "break-all", overflowWrap: "anywhere" }}>{effectiveLogPath}</span></div>
                </div>
              </div>

              {/* ═══════ 服务管理 ═══════ */}
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">服务管理</h3>
                <div className="settings-form-group">
                  <label className="settings-input-group">
                    <span className="settings-input-label">监听地址</span>
                    <input
                      className="settings-input-field"
                      ref={registerFocusTarget("host") as (node: HTMLInputElement | null) => void}
                      value={form.host}
                      onChange={(e) => updateForm({ ...form, host: e.target.value })}
                    />
                    <span className="settings-input-caption">服务绑定的 IP 地址，默认 127.0.0.1。</span>
                  </label>
                  <label className="settings-input-group">
                    <span className="settings-input-label">监听端口</span>
                    <input
                      className="settings-input-field"
                      ref={registerFocusTarget("port") as (node: HTMLInputElement | null) => void}
                      type="number"
                      value={form.port}
                      onChange={(e) => updateForm({ ...form, port: parseInt(e.target.value) || 3838 })}
                    />
                    <span className="settings-input-caption">服务端口号，默认 3838。只有端口冲突或外部接入时再调整。</span>
                  </label>
                </div>
                <div className="desktop-actions">
                  <button
                    className={backendRunning ? "secondary-button danger-button" : "primary-button"}
                    type="button"
                    onClick={async () => {
                      if (backendRunning) {
                        await window.desktop?.backend.stop();
                        setServiceStatus("内置后端已停止");
                      } else {
                        await window.desktop?.backend.start();
                        setServiceStatus("已请求启动内置后端");
                      }
                      onRefresh();
                      await refreshLogs();
                    }}
                  >
                    {backendRunning ? "停止内置后端" : "启动内置后端"}
                  </button>
                  {backendRunning && (
                    <button className="secondary-button" type="button" onClick={async () => {
                      setServiceStatus("正在重启内置后端...");
                      await window.desktop?.backend.stop();
                      await new Promise((r) => setTimeout(r, 1500));
                      await window.desktop?.backend.start();
                      setServiceStatus("内置后端已重启");
                      onRefresh();
                      await refreshLogs();
                    }}>重启后端</button>
                  )}
                  <button
                    className="secondary-button danger-button"
                    type="button"
                    disabled={!serviceOnline}
                    onClick={async () => {
                      await api.shutdownService();
                      setServiceStatus("已向服务发送关闭请求");
                      onRefresh();
                      await refreshLogs();
                    }}
                  >
                    强制关闭服务
                  </button>
                </div>
              </div>

              {/* ═══════ 桌面偏好 ═══════ */}
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">桌面偏好</h3>
                <div className="settings-form-group">
                  <div className="settings-input-group">
                    <div className="settings-toggle-row">
                      <div className="settings-toggle-label">
                        <span className="settings-toggle-title">开机自启动</span>
                        <span className="settings-toggle-caption">系统启动时自动运行 BiliSum。</span>
                      </div>
                      <label className="toggle-switch"><input type="checkbox" checked={autoLaunchEnabled} onChange={async (e) => { const v = e.target.checked; const result = await window.desktop?.app?.setAutoLaunch(v); if (typeof result === "boolean") { setAutoLaunchEnabled(result); if (result !== v) { setSaveStatus(`开机自启动${v ? "开启" : "关闭"}失败，请检查系统权限`); } } }} /><span className="toggle-slider" /></label>
                    </div>
                  </div>
                  <div className="settings-input-group">
                    <div className="settings-toggle-row">
                      <div className="settings-toggle-label">
                        <span className="settings-toggle-title">开机静默启动</span>
                        <span className="settings-toggle-caption">开机自启时隐藏窗口，仅显示托盘图标。</span>
                      </div>
                      <label className="toggle-switch"><input type="checkbox" checked={silentStartEnabled} disabled={!autoLaunchEnabled} onChange={async (e) => { const v = e.target.checked; setSilentStartEnabled(v); await window.desktop?.preferences?.setSilentStart(v); }} /><span className="toggle-slider" /></label>
                    </div>
                  </div>
                  <div className="settings-input-group">
                    <div className="settings-toggle-row">
                      <div className="settings-toggle-label">
                        <span className="settings-toggle-title">崩溃自动重启</span>
                        <span className="settings-toggle-caption">后端服务非预期退出时自动拉起。</span>
                      </div>
                      <label className="toggle-switch"><input type="checkbox" checked={crashAutoRestart} onChange={async (e) => { const v = e.target.checked; setCrashAutoRestart(v); await window.desktop?.preferences?.setCrashAutoRestart(v); }} /><span className="toggle-slider" /></label>
                    </div>
                  </div>
                  <div className="settings-input-group">
                    <div className="settings-toggle-row">
                      <div className="settings-toggle-label">
                        <span className="settings-toggle-title">开发者模式</span>
                        <span className="settings-toggle-caption">开启后显示后台日志和浏览器开发者工具，便于调试问题。</span>
                      </div>
                      <label className="toggle-switch"><input type="checkbox" checked={developerModeEnabled} onChange={async (e) => { const v = e.target.checked; setDeveloperModeEnabled(v); await window.desktop?.preferences?.setDeveloperMode(v); }} /><span className="toggle-slider" /></label>
                    </div>
                  </div>
                  <label className="settings-input-group">
                    <span className="settings-input-label">关闭窗口行为</span>
                    <select className="settings-select-field" style={{ maxWidth: 200 }} value={closeBehavior} onChange={async (e) => { const v = e.target.value as "tray" | "exit"; setCloseBehavior(v); await window.desktop?.preferences?.setCloseBehavior(v); }}>
                      <option value="tray">最小化到托盘</option>
                      <option value="exit">直接退出应用</option>
                    </select>
                    <span className="settings-input-caption">点击窗口关闭按钮时的行为。</span>
                  </label>
                </div>
                <div className="settings-reset-row">
                  <div className="settings-reset-row-copy">
                    <span className="settings-input-label">首页引导</span>
                    <span className="settings-input-caption">重新显示首次进入首页的功能指引。</span>
                  </div>
                  <button className="secondary-button" type="button" onClick={() => { window.localStorage.removeItem("bilisum.homeTourSeen"); window.localStorage.removeItem("bilisum.summaryPreferenceHintSeen"); setSaveStatus("已清空首页引导记录。"); }}>重新显示</button>
                </div>
                <div className="settings-reset-row">
                  <div className="settings-reset-row-copy">
                    <span className="settings-input-label">配置引导</span>
                    <span className="settings-input-caption">重新打开首次配置引导助手。</span>
                  </div>
                  <button className="secondary-button" type="button" onClick={() => { onOpenSetupAssistant(); setSaveStatus("已打开配置引导。"); }}>打开配置引导</button>
                </div>
              </div>

              {/* ═══════ 日志 ═══════ */}
              <div className="settings-cuda-section">
                <h3 className="settings-cuda-title">日志</h3>
                <div className="settings-input-group" style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", gap: 6 }}>
                      {(["all", "ERROR", "WARNING", "INFO"] as const).map((level) => (
                        <button key={level} type="button" className={`filter-pill${logLevelFilter === level ? " active" : ""}`} onClick={() => setLogLevelFilter(level)}>
                          {level === "all" ? "全部" : level}
                        </button>
                      ))}
                    </div>
                    <div style={{ flex: 1 }} />
                    <div className="settings-toggle-row" style={{ gap: 8 }}>
                      <span className="settings-toggle-caption">自动刷新</span>
                      <label className="toggle-switch"><input type="checkbox" checked={logAutoRefresh} onChange={(e) => setLogAutoRefresh(e.target.checked)} /><span className="toggle-slider" /></label>
                    </div>
                  </div>
                </div>
                <textarea ref={logTextareaRef} className="textarea-field log-viewer" rows={22} readOnly value={filteredLogOutput || "暂无日志"}></textarea>
                <div className="desktop-actions" style={{ marginTop: 12 }}>
                  <button className="secondary-button" type="button" onClick={() => void refreshLogs()}>刷新日志</button>
                  <button className="secondary-button" type="button" onClick={async () => { await window.desktop?.shell.openPath(effectiveLogPath); }}>打开日志文件</button>
                  <button className="secondary-button" type="button" onClick={async () => { const p = await (window.desktop as any)?.logs?.exportLog?.(); if (p) setServiceStatus(`日志已导出到 ${p}`); }}>导出日志</button>
                  <button className="secondary-button danger-button" type="button" onClick={async () => { await (window.desktop as any)?.logs?.clearLog?.(); await refreshLogs(); setServiceStatus("日志已清空"); }}>清空日志</button>
                </div>
              </div>
            </section>
          )}

          {activeCategory === "updates" && (
            <section className="settings-category-section">
              <header className="settings-category-header">
                <h2>桌面应用更新</h2>
                <p>{canInstallUpdate ? "检查新版本并管理安装。" : "查看最新版本信息与更新日志。"}</p>
              </header>
              <div className="settings-update-module" ref={registerFocusTarget("app_updates") as (node: HTMLDivElement | null) => void}>
                <div className="settings-update-overview">
                  <div className="settings-update-copy">
                    <span className="settings-story-kicker">Update</span>
                    <h3>{canInstallUpdate ? "手动检查桌面端更新" : "手动检查最新版本"}</h3>
                    <p>{updateSummary}</p>
                  </div>
                  <div className="settings-update-badges">
                    <span className="helper-chip">当前版本 v{currentVersion}</span>
                    <span className={`helper-chip status-${updateStatusTone}`}>状态：{updateStatusLabel}</span>
                    {updateInfo.version ? <span className="helper-chip">最新版本 v{updateInfo.version}</span> : null}
                    {updateInfo.releaseDate ? <span className="helper-chip">发布时间 {formatShortDate(updateInfo.releaseDate)}</span> : null}
                  </div>
                </div>

                <div className="settings-update-grid">
                  <div className="settings-update-panel">
                    <span className="settings-update-label">当前安装版本</span>
                    <strong>v{currentVersion}</strong>
                    <p>{canInstallUpdate ? "检查、下载和安装更新。" : "当前环境仅支持检查最新版本。"}</p>
                  </div>

                  <div className={`settings-update-panel ${updateInfo.status === "available" || updateInfo.status === "downloaded" ? "is-highlight" : ""}`}>
                    <span className="settings-update-label">检查结果</span>
                    <strong>
                      {updateUnsupported
                        ? "当前环境不可更新"
                        : updateInfo.status === "available" || updateInfo.status === "downloaded"
                        ? `发现 v${updateInfo.version || "-"}`
                        : updateInfo.status === "not-available"
                          ? "已是最新版本"
                          : updateInfo.status === "error"
                            ? "检查失败"
                            : updateInfo.status === "checking"
                              ? "正在检查"
                              : updateInfo.status === "downloading"
                                ? `下载中 ${Math.round(updateInfo.downloadProgress)}%`
                                : updateInfo.status === "installing"
                                  ? "正在安装"
                                  : "等待检查"}
                    </strong>
                    <p>
                      {!canInstallUpdate && updateInfo.status === "available"
                        ? `已检测到 v${updateInfo.version || "-"}，请使用桌面安装包完成更新。`
                        : !canInstallUpdate && updateInfo.status === "not-available"
                          ? "当前环境可查看最新版本信息，但不支持自动下载或安装。"
                          : updateInfo.status === "available" || updateInfo.status === "downloaded"
                            ? `当前 v${currentVersion}，最新 v${updateInfo.version || "-"}`
                            : updateInfo.status === "error"
                              ? (updateInfo.errorMessage || "更新检查失败，请重试。")
                              : updateSummary}
                    </p>
                  </div>
                </div>

                <div className="settings-update-actions">
                  <button
                    className="primary-button"
                    type="button"
                    disabled={!canCheckUpdate || updateActionBusy}
                    onClick={async () => {
                      try {
                        if (!canCheckUpdate) {
                          return;
                        }
                        if (canInstallUpdate && updateInfo.status === "available") {
                          await onDownloadUpdate();
                          return;
                        }
                        if (canInstallUpdate && updateInfo.status === "downloaded") {
                          await onInstallUpdate();
                          return;
                        }
                        await onCheckUpdate();
                      } catch {}
                    }}
                  >
                    {!canCheckUpdate
                      ? "当前环境无法检查更新"
                      : !canInstallUpdate
                        ? (updateInfo.status === "checking" ? "检查中..." : "检查更新")
                      : updateInfo.status === "checking"
                        ? "检查中..."
                        : updateInfo.status === "downloading"
                          ? `下载中... ${Math.round(updateInfo.downloadProgress)}%`
                        : updateInfo.status === "installing"
                            ? "安装中..."
                            : updateInfo.status === "available"
                              ? "下载并重启安装"
                              : updateInfo.status === "downloaded"
                                ? "立即重启安装"
                                : updateInfo.status === "error"
                                  ? "重试检查"
                                  : "检查更新"}
                  </button>

                  <button
                    className="secondary-button"
                    type="button"
                    disabled={!canCheckUpdate}
                    onClick={onOpenUpdateDialog}
                  >
                    查看更新详情
                  </button>
                </div>

                {updateInfo.status === "available" || updateInfo.status === "downloaded" ? (
                  <div className="settings-update-next-step">
                    <strong>下一步</strong>
                    <span>
                      {!canInstallUpdate
                        ? "当前环境仅支持查看最新版本信息，请前往桌面安装包完成更新。"
                        : updateInfo.status === "available"
                          ? "已检测到新版本，继续后会下载更新并自动重启安装。"
                          : "更新已下载完成，可以立即重启应用完成安装。"}
                    </span>
                  </div>
                ) : null}
              </div>
            </section>
          )}
          <button
            className="settings-collapse-all-fab"
            type="button"
            aria-label="回到顶部"
            onClick={(e) => { e.preventDefault(); scrollSettingsToTop(); }}
            title="回到顶部"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="18 15 12 9 6 15"/><line x1="12" y1="9" x2="12" y2="21"/><line x1="6" y1="3" x2="18" y2="3"/></svg>
          </button>
        </div>
      </main>
      {generationModelDialog ? (
        <div className="update-dialog-overlay" role="presentation" onClick={closeGenerationModelDialog}>
          <section
            className="update-dialog generation-model-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="generation-model-dialog-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="update-dialog-header">
              <div>
                <span className="settings-model-kicker">模型接入</span>
                <h2 id="generation-model-dialog-title">{generationModelDialog === "main" ? "主摘要模型" : "视觉理解模型"}</h2>
              </div>
              <button className="close-button" type="button" aria-label="关闭模型配置" onClick={closeGenerationModelDialog}>
                ×
              </button>
            </header>
            <div className="update-dialog-body generation-model-dialog-body">
              {generationModelDialog === "main" ? (
                <>
                  <label className="settings-input-group">
                    <span className="settings-input-label">LLM 提供商</span>
                    <select className="settings-select-field" value={form.llm_provider} onChange={(e) => updateForm({ ...form, llm_provider: e.target.value })}>
                      <option value="openai-compatible">OpenAI Compatible</option>
                      <option value="openai">OpenAI</option>
                      <option value="anthropic">Anthropic</option>
                      <option value="custom">自建端点</option>
                    </select>
                  </label>
                  <label className={`settings-input-group settings-focus-target ${activeFocusTarget === "llm_base_url" ? "is-highlighted" : ""}`}>
                    <span className="settings-input-label">API Base URL</span>
                    <input className="settings-input-field" value={form.llm_base_url} onChange={(e) => updateForm({ ...form, llm_base_url: e.target.value })} placeholder="https://api.openai.com/v1" />
                    <span className="settings-input-caption">主摘要 LLM API 的基础 URL 地址。</span>
                  </label>
                  <label className={`settings-input-group settings-focus-target ${activeFocusTarget === "llm_api_key" ? "is-highlighted" : ""}`}>
                    <span className="settings-input-label">API Key</span>
                    <input className="settings-input-field" type="password" value={form.llm_api_key} onFocus={selectMaskedApiKey} onChange={(e) => updateForm({ ...form, llm_api_key: e.target.value })} placeholder="sk-..." />
                    <span className="settings-input-caption">已保存的密钥会用 ****** 显示，直接输入新值即可替换。</span>
                  </label>
                  <label className={`settings-input-group settings-focus-target ${activeFocusTarget === "llm_model" ? "is-highlighted" : ""}`}>
                    <span className="settings-input-label">模型名称</span>
                    <input className="settings-input-field" value={form.llm_model} onChange={(e) => updateForm({ ...form, llm_model: e.target.value })} placeholder="gpt-4o-mini / claude-3-haiku" />
                  </label>
                  <div className={`settings-inline-alert ${llmReady ? "success" : "warning"}`}>
                    <strong>{llmReady ? "主摘要模型配置完整" : "主摘要模型仍需补全"}</strong>
                    <span>{llmReady ? "可以保存后用于摘要、导图和默认视觉模型。": "启用 LLM 摘要时，需要 Base URL、API Key 与模型名称都有效。"}</span>
                  </div>
                </>
              ) : (
                <>
                  <div className="settings-inline-alert info">
                    <strong>默认跟随主 LLM</strong>
                    <span>视觉字段留空时，会复用主摘要模型的 Base URL、API Key 和模型名称；只有需要单独 VLM 时再填写。</span>
                  </div>
                  <label className="settings-input-group">
                    <span className="settings-input-label">视觉模型提供商</span>
                    <select className="settings-select-field" value={form.visual_vlm_provider || "openai-compatible"} onChange={(e) => updateForm({ ...form, visual_vlm_provider: e.target.value })}>
                      <option value="openai-compatible">OpenAI Compatible</option>
                      <option value="openai">OpenAI</option>
                      <option value="anthropic">Anthropic</option>
                      <option value="custom">自建端点</option>
                    </select>
                  </label>
                  <label className="settings-input-group">
                    <span className="settings-input-label">视觉 API Base URL</span>
                    <input className="settings-input-field" value={form.visual_evidence_base_url} onChange={(e) => updateForm({ ...form, visual_evidence_base_url: e.target.value })} placeholder="留空则跟随主 LLM Base URL" />
                  </label>
                  <label className="settings-input-group">
                    <span className="settings-input-label">视觉模型名称</span>
                    <input className="settings-input-field" value={form.visual_evidence_model} onChange={(e) => updateForm({ ...form, visual_evidence_model: e.target.value })} placeholder="留空则跟随主 LLM 模型" />
                  </label>
                  <label className="settings-input-group">
                    <span className="settings-input-label">视觉 API Key</span>
                    <input className="settings-input-field" type="password" value={form.visual_evidence_api_key} onFocus={selectMaskedApiKey} onChange={(e) => updateForm({ ...form, visual_evidence_api_key: e.target.value })} placeholder="留空则跟随主 LLM API Key" />
                  </label>
                  <div className={`settings-inline-alert ${visualLlmReady ? "success" : "warning"}`}>
                    <strong>{visualLlmReady ? "视觉模型配置完整" : "视觉模型会等待有效模型配置"}</strong>
                    <span>{visualLlmReady ? "当前有效配置可用于理解型图文笔记。": "请补全独立视觉配置，或确保主摘要模型已完整配置。"}</span>
                  </div>
                </>
              )}
            </div>
            <footer className="update-dialog-footer">
              <button className="secondary-button" type="button" onClick={closeGenerationModelDialog}>
                关闭
              </button>
              <button
                className="primary-button"
                type="button"
                disabled={llmTestBusy}
                onClick={() => void (generationModelDialog === "main" ? testLlmConnection() : testVisualLlmConnection())}
              >
                {llmTestBusy ? "测试中..." : generationModelDialog === "main" ? "测试主模型" : "测试视觉模型"}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
      {knowledgePromptGuideOpen ? (
        <div className="update-dialog-overlay" role="presentation" onClick={() => setKnowledgePromptGuideOpen(false)}>
          <section
            className="update-dialog knowledge-prompt-guide-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="knowledge-prompt-guide-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="update-dialog-header">
              <h2 id="knowledge-prompt-guide-title">知识笔记提示词教程</h2>
              <button className="close-button" type="button" aria-label="关闭教程" onClick={() => setKnowledgePromptGuideOpen(false)}>
                ×
              </button>
            </header>
            <div className="update-dialog-body knowledge-prompt-guide-body">
              <section className="knowledge-prompt-guide-section">
                <h3>两个输入框分别控制什么</h3>
                <p>
                  System Prompt 负责定义角色、底线和总体风格，例如"严谨的中文知识编辑""不得编造""只输出 JSON"。
                  User Template 负责规定笔记结构、可用素材和输出格式，是调整笔记样式的主要位置。
                </p>
              </section>
              <section className="knowledge-prompt-guide-section">
                <h3>可用变量</h3>
                <div className="knowledge-prompt-guide-grid">
                  <span>{"{title}"}</span>
                  <p>视频标题，适合用于生成笔记标题或判断主题。</p>
                  <span>{"{summary_json}"}</span>
                  <p>前一步结构化摘要，包含要点、章节、结论等信息。可以保留，也可以在模板里弱化它的权重。</p>
                  <span>{"{transcript_excerpt}"}</span>
                  <p>转写节选，适合补充细节和原文语境。</p>
                  <span>{"{segments_excerpt}"}</span>
                  <p>带时间或分段的信息，适合让笔记按章节展开。</p>
                </div>
              </section>
              <section className="knowledge-prompt-guide-section">
                <h3>默认笔记格式</h3>
                <p>默认模板倾向生成一篇长阅读型 Markdown 笔记，通常包含：</p>
                <ul>
                  <li>核心结论：先给出视频最重要的观点。</li>
                  <li>关键概念：整理定义、条件、术语和背景。</li>
                  <li>章节展开：按内容推进顺序补全上下文。</li>
                  <li>易错点或限制：保留限制、争议、例外和注意事项。</li>
                </ul>
              </section>
              <section className="knowledge-prompt-guide-section">
                <h3>怎么修改笔记样式</h3>
                <p>想改格式时，优先改 User Template 里的"写作要求"或"目标结构"。例如：</p>
                <pre>{`请把 knowledgeNoteMarkdown 写成以下结构：
# {title}

## 一句话总结
...

## 关键问题
- ...

## 可执行清单
- ...

## 原文中的限制
- ...`}</pre>
                <p>
                  如果想要更短，就写"每个小节不超过 5 条要点"。如果想要课程笔记风格，就写"优先使用定义、例子、推导、复盘题"。
                </p>
              </section>
              <section className="knowledge-prompt-guide-section warning">
                <h3>不要删掉的约束</h3>
                <p>
                  最终仍必须只返回合法 JSON，并且顶层字段必须是 <code>knowledgeNoteMarkdown</code>。
                  可以改变 Markdown 内容结构，但不要让模型直接输出普通 Markdown，否则任务会解析失败。
                </p>
              </section>
            </div>
            <footer className="update-dialog-footer">
              <button className="primary-button" type="button" onClick={() => setKnowledgePromptGuideOpen(false)}>
                知道了
              </button>
            </footer>
          </section>
        </div>
      ) : null}
      {taskListOpen ? (
        <div className="settings-tasklist-float" role="dialog" aria-modal="false" aria-label="最近任务列表" onClick={() => setTaskListOpen(false)}>
          <div className="settings-tasklist-panel" onClick={(event) => event.stopPropagation()}>
            <div className="settings-tasklist-header">
              <div className="settings-tasklist-copy">
                <span className="settings-nav-brand-kicker">Tasklist</span>
                <strong>最近任务队列</strong>
                <span>聚焦最近 {TASK_LIST_LIMIT} 条任务，方便确认 `queued` 与 `running` 的分布。</span>
              </div>
              <div className="settings-tasklist-actions">
                <button className="secondary-button" type="button" disabled={taskListLoading} onClick={() => void refreshTaskList()}>
                  {taskListLoading ? "刷新中..." : "刷新"}
                </button>
                <button className="secondary-button" type="button" onClick={() => setTaskListOpen(false)}>
                  关闭
                </button>
              </div>
            </div>
            <div className="settings-tasklist-summary">
              <span className="helper-chip">最近 {taskList.length} 条</span>
              <span className={`helper-chip ${runningTaskCount ? "status-running" : "status-pending"}`}>运行中 {runningTaskCount}</span>
              <span className={`helper-chip ${queuedTaskCount ? "status-pending" : "status-success"}`}>排队中 {queuedTaskCount}</span>
            </div>
            {taskListError ? <div className="detail-error-banner" role="status">{taskListError}</div> : null}
            <div className="settings-tasklist-body">
              {taskListLoading && !taskList.length ? <div className="empty-placeholder">正在同步最近任务...</div> : null}
              {!taskListLoading && !taskList.length && !taskListError ? <div className="empty-placeholder">当前还没有任务记录。</div> : null}
              {taskList.map((task) => (
                <article key={task.task_id} className="settings-tasklist-item">
                  <div className="settings-tasklist-item-top">
                    <div className="settings-tasklist-item-meta">
                      <span className={`task-status ${taskStatusClass(task.status)}`}>{taskStatusLabel(task.status)}</span>
                      {task.page_number === 0 ? (
                        <span className="helper-chip">{task.page_title || "全集总结"}</span>
                      ) : task.page_number ? (
                        <span className="helper-chip">P{task.page_number}</span>
                      ) : null}
                      <span className="helper-chip">{task.input_type}</span>
                    </div>
                    <span className="task-history-id">{task.task_id.slice(0, 8)}</span>
                  </div>
                  <strong>{task.title || "未命名任务"}</strong>
                  <div className="settings-tasklist-item-subline">
                    <span>{formatDateTime(task.created_at)}</span>
                    {task.task_duration_seconds != null ? <span>耗时 {Math.max(0, Math.round(task.task_duration_seconds))} 秒</span> : null}
                  </div>
                </article>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
