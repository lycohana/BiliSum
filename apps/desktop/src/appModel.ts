import type { UpdateInfo } from "./components/UpdateDialog";
import type { EnvironmentInfo, ServiceSettings, SystemInfo, TaskStatus, VideoAssetSummary, VideoFolder, VideoLibraryPreferences } from "./types";

export type Snapshot = {
  serviceOnline: boolean;
  systemInfo: SystemInfo | null;
  environment: EnvironmentInfo | null;
  settings: ServiceSettings | null;
  videos: VideoAssetSummary[];
  folders: VideoFolder[];
  libraryPreferences: VideoLibraryPreferences;
  error: string;
  runtimeInitializing: boolean;
};

export type DesktopState = {
  version: string;
  backend: {
    running: boolean;
    ready: boolean;
    pid: number | null;
    url: string;
    lastError: string;
  } | null;
  logPath: string;
};

export type UpdateState = {
  status: "idle" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "installing" | "error";
  version: string;
  releaseDate: string;
  releaseNotes: string | null;
  downloadProgress: number;
  errorMessage: string | null;
};

export type LibraryFilter = "all" | "completed" | "running" | "with-result" | "favorite";
export type MetricTone = "default" | "accent" | "success" | "info";
export type DevicePreference = "auto" | "cpu" | "cuda";
export type SelectOption = { value: string; label: string };
export type ConfigIssueSeverity = "critical" | "warning";
export type ConfigIssue = {
  key: string;
  title: string;
  description: string;
  severity: ConfigIssueSeverity;
};
export type ConfigHealth = {
  checked: boolean;
  state: "ready" | "warning" | "critical";
  isConfigured: boolean;
  hasBlockingIssues: boolean;
  issues: ConfigIssue[];
  blockingIssues: ConfigIssue[];
  summary: string;
  actionText: string;
};

export const emptySnapshot: Snapshot = {
  serviceOnline: false,
  systemInfo: null,
  environment: null,
  settings: null,
  videos: [],
  folders: [],
  libraryPreferences: { new_video_position: "front" },
  error: "",
  runtimeInitializing: false,
};

const MASKED_API_KEY = "******";

function hasUsableApiKey(value: string | undefined | null, configured: boolean | undefined): boolean {
  const trimmed = String(value || "").trim();
  return Boolean(configured || (trimmed && trimmed !== MASKED_API_KEY));
}

export const devicePreferenceOptions: SelectOption[] = [
  { value: "auto", label: "自动选择" },
  { value: "cuda", label: "GPU (CUDA)" },
  { value: "cpu", label: "CPU" },
];

export function normalizeDevicePreference(value?: string | null): DevicePreference {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "gpu") {
    return "cuda";
  }
  if (normalized === "auto" || normalized === "cuda" || normalized === "cpu") {
    return normalized;
  }
  return "cpu";
}

export function devicePreferenceLabel(value?: string | null): string {
  const normalized = normalizeDevicePreference(value);
  if (normalized === "cuda") {
    return "GPU (CUDA)";
  }
  if (normalized === "auto") {
    return "自动选择";
  }
  return "CPU";
}

export function deriveRuntimeDeviceLabel({
  transcriptionProvider,
  whisperDevice,
  cudaAvailable,
  hasSettings,
}: {
  transcriptionProvider?: string | null;
  whisperDevice?: string | null;
  cudaAvailable?: boolean;
  hasSettings: boolean;
}): string {
  const provider = String(transcriptionProvider || "").trim().toLowerCase();
  if (provider === "siliconflow") {
    return "云端";
  }
  const effectiveDevice = normalizeDevicePreference(whisperDevice);
  if (effectiveDevice === "cuda") {
    return "GPU";
  }
  if (!hasSettings && cudaAvailable) {
    return "GPU";
  }
  return "CPU";
}

export function getConfigHealth(
  settings?: ServiceSettings | null,
  environment?: EnvironmentInfo | null,
): ConfigHealth {
  if (!settings) {
    return {
      checked: false,
      state: "ready",
      isConfigured: true,
      hasBlockingIssues: false,
      issues: [],
      blockingIssues: [],
      summary: "正在读取当前配置。",
      actionText: "前往设置",
    };
  }

  const issues: ConfigIssue[] = [];
  const transcriptionProvider = String(settings.transcription_provider || "").trim().toLowerCase();
  const knowledgeEnabled = Boolean(settings.knowledge_enabled);
  const knowledgeUsesCustomLlm = String(settings.knowledge_llm_mode || "same_as_main").trim().toLowerCase() === "custom";
  const mainLlmApiKeyReady = hasUsableApiKey(settings.llm_api_key, settings.llm_api_key_configured);
  const knowledgeLlmApiKeyReady = hasUsableApiKey(settings.knowledge_llm_api_key, settings.knowledge_llm_api_key_configured);
  const knowledgeLlmReady = knowledgeUsesCustomLlm
    ? Boolean(settings.knowledge_llm_enabled && knowledgeLlmApiKeyReady && String(settings.knowledge_llm_base_url || "").trim() && String(settings.knowledge_llm_model || "").trim())
    : Boolean(settings.llm_enabled && mainLlmApiKeyReady && String(settings.llm_base_url || "").trim() && String(settings.llm_model || "").trim());

  if (transcriptionProvider === "siliconflow" && !settings.siliconflow_asr_api_key_configured) {
    issues.push({
      key: "siliconflow_asr_api_key",
      title: "缺少语音识别 API Key",
      description: "当前使用 SiliconFlow 转写，但还没有填写 API Key，无法开始视频转写。",
      severity: "critical",
    });
  }

  if (transcriptionProvider === "multimodal") {
    const multimodalMissingParts: string[] = [];
    if (!String(settings.multimodal_asr_base_url || "").trim()) {
      multimodalMissingParts.push("Base URL");
    }
    if (!String(settings.multimodal_asr_model || "").trim()) {
      multimodalMissingParts.push("模型名");
    }
    if (!settings.multimodal_asr_api_key_configured) {
      multimodalMissingParts.push("API Key");
    }
    if (multimodalMissingParts.length > 0) {
      issues.push({
        key: "multimodal_asr_base_url",
        title: "多模态 ASR 配置未补全",
        description: `当前使用多模态 ASR 转写，但以下项目仍为空：${multimodalMissingParts.join("、")}，无法开始视频转写。`,
        severity: "critical",
      });
    }
  }

  if (transcriptionProvider === "local" && environment?.localAsrAvailable === false) {
    issues.push({
      key: "local_asr_runtime",
      title: "本地 ASR 运行环境未就绪",
      description: "当前使用本地转写，但本地 ASR 尚未安装或当前运行环境不可用，请先安装本地 ASR 或切回云端转写。",
      severity: "critical",
    });
  }

  const llmMissingParts: string[] = [];
  if (settings.llm_enabled) {
    if (!mainLlmApiKeyReady) {
      llmMissingParts.push("API Key");
    }
    if (!String(settings.llm_base_url || "").trim()) {
      llmMissingParts.push("Base URL");
    }
    if (!String(settings.llm_model || "").trim()) {
      llmMissingParts.push("模型名");
    }
  }

  if (llmMissingParts.length > 0) {
    issues.push({
      key: "llm_configuration",
      title: "LLM 配置未补全",
      description: `当前已启用 LLM，但以下项目仍为空：${llmMissingParts.join("、")}。摘要会回退为本地规则模式。`,
      severity: "warning",
    });
  }

  if (settings.auto_generate_mindmap && !settings.llm_enabled) {
    issues.push({
      key: "auto_mindmap_requires_llm",
      title: "自动导图依赖 LLM",
      description: "你已开启自动生成思维导图，但当前 LLM 处于关闭状态，导图不会自动生成。",
      severity: "warning",
    });
  }

  if (knowledgeEnabled && environment && environment.knowledgeDependenciesReady === false) {
    issues.push({
      key: "knowledge_dependencies",
      title: "缺少知识库依赖",
      description: "当前运行环境缺少 chromadb 或 sentence-transformers，无法构建知识库索引。",
      severity: "warning",
    });
  }

  if (knowledgeEnabled && !knowledgeLlmReady) {
    issues.push({
      key: "knowledge_llm_configuration",
      title: "知识库 LLM 未补全",
      description: knowledgeUsesCustomLlm
        ? "知识库当前使用独立 LLM，但还没有补全启用状态、API Key、Base URL 或模型名，自动打标和问答暂不可用。"
        : "知识库当前跟随主 LLM；请先启用主 LLM，并补全 API Key、Base URL 与模型名。",
      severity: "warning",
    });
  }

  const blockingIssues = issues.filter((issue) => issue.severity === "critical");
  const hasBlockingIssues = blockingIssues.length > 0;
  const state = hasBlockingIssues ? "critical" : issues.length > 0 ? "warning" : "ready";
  const summary = hasBlockingIssues
    ? `当前有 ${blockingIssues.length} 项关键配置缺失，开始总结前需要先补全。`
    : issues.length > 0
      ? `当前有 ${issues.length} 项增强能力待补全，不影响基础流程但会影响体验。`
      : "当前运行配置完整，可以直接开始总结。";

  return {
    checked: true,
    state,
    isConfigured: issues.length === 0,
    hasBlockingIssues,
    issues,
    blockingIssues,
    summary,
    actionText: hasBlockingIssues ? "前往设置补全配置" : "前往设置优化配置",
  };
}

export function shouldShowSetupAssistant(configHealth: ConfigHealth, settings?: ServiceSettings | null): boolean {
  if (!configHealth.checked || !settings) {
    return false;
  }
  return settings.settings_file_exists === false && configHealth.issues.length > 0;
}

export function toUpdateState(info: UpdateInfo): UpdateState {
  return {
    status: info.status,
    version: info.version,
    releaseDate: info.releaseDate,
    releaseNotes: info.releaseNotes,
    downloadProgress: info.downloadProgress,
    errorMessage: info.errorMessage,
  };
}

export function getUpdateDialogSignal(update: Pick<UpdateState, "status" | "version">): string | null {
  if (update.status !== "available" && update.status !== "downloaded") {
    return null;
  }
  return `${update.status}:${update.version || "unknown"}`;
}

export function isUpdateUnsupported(update: Pick<UpdateState, "status" | "errorMessage">): boolean {
  return update.status === "not-available" && Boolean(update.errorMessage);
}

export function getUpdateStatusLabel(update: Pick<UpdateState, "status" | "errorMessage">): string {
  if (isUpdateUnsupported(update)) {
    return "不可用";
  }
  switch (update.status) {
    case "checking":
      return "检查中";
    case "available":
      return "发现新版本";
    case "not-available":
      return "已是最新";
    case "downloading":
      return "下载中";
    case "downloaded":
      return "待安装";
    case "installing":
      return "安装中";
    case "error":
      return "检查失败";
    default:
      return "未检查";
  }
}

export function getUpdateStatusTone(update: Pick<UpdateState, "status" | "errorMessage">): "success" | "failed" | "running" | "pending" {
  if (isUpdateUnsupported(update)) {
    return "failed";
  }
  switch (update.status) {
    case "available":
    case "downloaded":
      return "success";
    case "checking":
    case "downloading":
    case "installing":
      return "running";
    case "error":
      return "failed";
    default:
      return "pending";
  }
}

export function getUpdateSummary(update: UpdateState, currentVersion: string): string {
  const installedVersion = currentVersion || "-";
  if (isUpdateUnsupported(update)) {
    return update.errorMessage || "当前环境不支持自动更新。";
  }
  switch (update.status) {
    case "checking":
      return `正在检查更新，当前版本 v${installedVersion}。`;
    case "available":
      return `发现新版本 v${update.version || "-"}，当前版本 v${installedVersion}。`;
    case "not-available":
      return `当前版本 v${installedVersion} 已是最新版本。`;
    case "downloading":
      return `正在下载 v${update.version || "-"}，进度 ${Math.round(update.downloadProgress)}%。`;
    case "downloaded":
      return `v${update.version || "-"} 已下载完成，可立即安装。`;
    case "installing":
      return `正在安装 v${update.version || "-"}。`;
    case "error":
      return update.errorMessage || "检查更新失败，请稍后重试。";
    default:
      return `当前版本 v${installedVersion}，尚未检查更新。`;
  }
}

export function formatShortDate(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, "0")}.${String(date.getDate()).padStart(2, "0")}`;
}

export function platformLabel(platform?: string | null) {
  const labels: Record<string, string> = {
    bilibili: "Bilibili",
    youtube: "YouTube",
    local: "本地",
  };
  return (platform && labels[platform.toLowerCase()]) || "Video";
}

export function stageLabel(stage?: string | null) {
  const labels: Record<string, string> = {
    queued: "排队中",
    downloading: "下载中",
    transcribing: "转写中",
    summarizing: "总结中",
    mindmap_queued: "导图待生成",
    mindmap_llm_request: "调用 LLM 中",
    mindmap_generating: "导图生成中",
    mindmap_completed: "导图完成",
    mindmap_failed: "导图失败",
    visual_queued: "图文笔记排队",
    visual_generating: "图文笔记生成中",
    visual_source_preparing: "准备画面来源",
    visual_frame_extracting: "抽取关键画面",
    visual_frame_extracted: "图片索引整理",
    visual_frame_analyzing: "VLM 解析画面",
    visual_insert_planning: "规划插图位置",
    visual_note_composing: "整合图文笔记",
    visual_completed: "图文笔记完成",
    visual_partial: "图文笔记降级完成",
    visual_failed: "图文笔记失败",
    completed: "已完成",
    failed: "失败",
  };
  return (stage && labels[stage]) || "待开始";
}

export function taskStatusClass(status?: TaskStatus | null) {
  if (status === "completed") return "status-success";
  if (status === "running") return "status-running";
  if (status === "failed") return "status-failed";
  return "status-pending";
}

export function progressEventClass(stage?: string | null) {
  if (stage === "completed") return "completed";
  if (stage === "failed") return "error";
  if (stage === "mindmap_completed") return "completed";
  if (stage === "mindmap_failed") return "error";
  if (stage === "visual_completed" || stage === "visual_partial") return "completed";
  if (stage === "visual_failed") return "error";
  if (stage === "summarizing" || stage === "transcribing" || stage === "downloading" || stage === "mindmap_queued" || stage === "mindmap_llm_request" || stage === "mindmap_generating" || stage?.startsWith("visual_")) return "active";
  return "";
}
