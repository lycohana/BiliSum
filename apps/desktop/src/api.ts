import type {
  AuthStatus,
  EnvironmentInfo,
  RuntimeStatus,
  KnowledgeAskResponse,
  KnowledgeAutoTagResponse,
  KnowledgeNetworkResponse,
  KnowledgeReasoningDelta,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  KnowledgeStatsResponse,
  KnowledgeTagListResponse,
  KnowledgeToolTrace,
  LlmTestResponse,
  PromptMatchResult,
  PromptPreset,
  PromptPresetCreateRequest,
  ServiceSettings,
  SystemLogResponse,
  SystemInfo,
  TaskDetail,
  TaskEvent,
  TaskMarkdownExportResponse,
  TaskMindMapResponse,
  TaskVisualEvidenceResponse,
  TaskSummary,
  VideoKnowledgeTagListResponse,
  VideoAssetDetail,
  VideoFolder,
  VideoLibraryPreferences,
  VideoLibraryState,
  VideoProbeResult,
  VideoAssetSummary,
  VideoTaskBatchRequest,
  VideoTaskBatchResponse,
} from "./types";

export type UpdateSettingsResponse = {
  saved: boolean;
  settings: ServiceSettings;
  message: string;
};

export type AppUpdateInfo = {
  status: "idle" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "installing" | "error";
  version: string;
  releaseDate: string;
  releaseNotes: string | null;
  downloadProgress: number;
  errorMessage: string | null;
};

export type LlmTestPayload = Partial<ServiceSettings> & {
  llm_test_scope?: "main" | "knowledge" | "visual";
};

export type BilibiliCookieCaptureResponse = {
  cookiesFile: string;
  cookieCount: number;
  browser?: string;
};

export type BilibiliQrcodeLoginResponse = {
  url: string;
  qrcodeKey: string;
  expiresIn: number;
};

export type BilibiliQrcodePollResponse = {
  status: "pending" | "scanned" | "expired" | "confirmed";
  message: string;
  cookiesFile?: string;
  cookieCount?: number;
};

export class AuthRequiredError extends Error {
  constructor(message = "需要输入 BiliSum 访问密钥。") {
    super(message);
    this.name = "AuthRequiredError";
  }
}

async function withDesktopAuth(options?: RequestInit): Promise<RequestInit | undefined> {
  const token = await window.desktop?.backend?.getAccessToken?.();
  if (!token) {
    return options;
  }
  const headers = new Headers(options?.headers);
  if (!headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return { ...options, headers };
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, await withDesktopAuth(options));
  if (!response.ok) {
    const text = await response.text();
    let detail = text || `Request failed: ${response.status}`;
    try {
      const payload = JSON.parse(text) as { detail?: string; message?: string };
      detail = payload.detail || payload.message || detail;
    } catch {
      detail = text || `Request failed: ${response.status}`;
    }
    if (response.status === 401) {
      throw new AuthRequiredError(detail);
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

async function fetchJsonWithAuth<T>(url: string, options?: RequestInit): Promise<T> {
  return fetchJson<T>(url, options);
}

function parseSseBlock(block: string): { event: string; data: string } | null {
  const lines = block.split(/\r?\n/);
  let event = "message";
  const dataLines: string[] = [];
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) {
    return null;
  }
  return { event, data: dataLines.join("\n") };
}

export const api = {
  getAuthStatus() {
    return fetchJson<AuthStatus>("/api/v1/auth/status");
  },
  createAuthSession(token: string) {
    return fetchJson<AuthStatus>("/api/v1/auth/session", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
  },
  getHealth() {
    return fetchJson<{ status: string }>("/health");
  },
  getSystemInfo(options?: { runtimeChannel?: string; refresh?: boolean }) {
    const url = new URL("/api/v1/system/info", window.location.origin);
    if (options?.runtimeChannel) {
      url.searchParams.set("runtime_channel", options.runtimeChannel);
    }
    if (options?.refresh) {
      url.searchParams.set("refresh", "1");
    }
    return fetchJsonWithAuth<SystemInfo>(url.toString());
  },
  getEnvironment(options?: { runtimeChannel?: string; refresh?: boolean }) {
    const url = new URL("/api/v1/environment", window.location.origin);
    if (options?.runtimeChannel) {
      url.searchParams.set("runtime_channel", options.runtimeChannel);
    }
    if (options?.refresh) {
      url.searchParams.set("refresh", "1");
    }
    return fetchJsonWithAuth<EnvironmentInfo>(url.toString());
  },
  getRuntimeStatus() {
    return fetchJsonWithAuth<RuntimeStatus>("/api/v1/runtime/status");
  },
  syncRuntime(payload?: { runtime_channel?: string }) {
    return fetchJsonWithAuth<{
      synced: boolean;
      runtimeChannel?: string;
      channels?: Array<{ runtimeChannel: string; synced: boolean }>;
      runtimeStatus?: RuntimeStatus;
      environment?: EnvironmentInfo;
    }>("/api/v1/runtime/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  getSettings() {
    return fetchJsonWithAuth<ServiceSettings>("/api/v1/settings");
  },
  getAppUpdate() {
    return fetchJsonWithAuth<AppUpdateInfo>("/api/v1/app/update");
  },
  listPromptPresets() {
    return fetchJsonWithAuth<PromptPreset[]>("/api/v1/prompts/presets");
  },
  createPromptPreset(payload: PromptPresetCreateRequest) {
    return fetchJsonWithAuth<PromptPreset>("/api/v1/prompts/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  deletePromptPreset(presetId: string) {
    return fetchJsonWithAuth<{ deleted: boolean; preset_id: string }>(
      `/api/v1/prompts/presets/${encodeURIComponent(presetId)}`,
      { method: "DELETE" },
    );
  },
  matchPrompt(title: string) {
    return fetchJsonWithAuth<PromptMatchResult>("/api/v1/prompts/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
  },
  updateSettings(payload: Partial<ServiceSettings>) {
    return fetchJson<UpdateSettingsResponse>("/api/v1/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  testLlmConnection(payload: LlmTestPayload) {
    return fetchJson<LlmTestResponse>("/api/v1/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  testAsrConnection(payload: Partial<ServiceSettings>) {
    return fetchJson<LlmTestResponse>("/api/v1/asr/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  captureBilibiliCookiesFromBrowser() {
    return fetchJson<BilibiliCookieCaptureResponse>("/api/v1/bilibili/cookies/capture", {
      method: "POST",
    });
  },
  createBilibiliCookieQrcode() {
    return fetchJson<BilibiliQrcodeLoginResponse>("/api/v1/bilibili/cookies/qrcode", {
      method: "POST",
    });
  },
  pollBilibiliCookieQrcode(qrcodeKey: string) {
    return fetchJson<BilibiliQrcodePollResponse>(`/api/v1/bilibili/cookies/qrcode/${encodeURIComponent(qrcodeKey)}`);
  },
  getSystemLogs(lines = 200) {
    return fetchJson<SystemLogResponse>(`/api/v1/system/logs?lines=${lines}`);
  },
  shutdownService() {
    return fetchJson<{ ok: boolean }>("/api/v1/system/shutdown", { method: "POST" });
  },
  listVideos() {
    return fetchJson<VideoAssetSummary[]>("/api/v1/videos");
  },
  getVideoLibrary() {
    return fetchJson<VideoLibraryState>("/api/v1/videos/library");
  },
  updateVideoLibraryPreferences(payload: VideoLibraryPreferences) {
    return fetchJson<VideoLibraryPreferences>("/api/v1/videos/library/preferences", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  createVideoFolder(payload: { name: string; parent_id?: string | null }) {
    return fetchJson<VideoFolder>("/api/v1/videos/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  updateVideoFolder(folderId: string, payload: { name?: string | null; parent_id?: string | null; position?: number | null }) {
    return fetchJson<VideoFolder>(`/api/v1/videos/folders/${encodeURIComponent(folderId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  deleteVideoFolder(folderId: string) {
    return fetchJson<{ deleted: boolean; folder_id: string }>(`/api/v1/videos/folders/${encodeURIComponent(folderId)}`, { method: "DELETE" });
  },
  getVideo(videoId: string) {
    return fetchJson<VideoAssetDetail>(`/api/v1/videos/${videoId}`);
  },
  setVideoFavorite(videoId: string, payload: { is_favorite: boolean }) {
    return fetchJson<VideoAssetDetail>(`/api/v1/videos/${videoId}/favorite`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  deleteVideo(videoId: string) {
    return fetchJson<{ deleted: boolean }>(`/api/v1/videos/${videoId}`, { method: "DELETE" });
  },
  moveVideoToFolder(videoId: string, payload: { folder_id?: string | null; folder_ids?: string[] | null }) {
    return fetchJson<VideoAssetDetail>(`/api/v1/videos/${videoId}/move`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  setVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }) {
    return fetchJson<VideoAssetDetail>(`/api/v1/videos/${videoId}/pin`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  reorderVideos(payload: { video_ids: string[]; folder_id?: string | null }) {
    return fetchJson<VideoAssetSummary[]>("/api/v1/videos/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  probeVideo(payload: { url: string; force_refresh: boolean }) {
    return fetchJson<VideoProbeResult>("/api/v1/videos/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  uploadLocalVideo(file: File) {
    const url = new URL("/api/v1/videos/upload", window.location.origin);
    url.searchParams.set("filename", file.name);
    return fetchJson<VideoProbeResult>(url.toString(), {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
      },
      body: file,
    });
  },
  uploadBatchVideos(files: File[] | FileList) {
    const form = new FormData();
    Array.from(files).forEach((file) => {
      form.append("files", file, file.name);
    });
    return fetchJson<VideoProbeResult[]>("/api/v1/videos/upload/batch", {
      method: "POST",
      body: form,
    });
  },
  getVideoTasks(videoId: string) {
    return fetchJson<TaskSummary[]>(`/api/v1/videos/${videoId}/tasks`);
  },
  createVideoTask(
    videoId: string,
    payload?: {
      page_number?: number | null;
      visual_note_mode?: string | null;
      prompt_preset_id?: string | null;
    },
  ) {
    return fetchJson<TaskDetail>(`/api/v1/videos/${videoId}/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  createVideoTasksBatch(videoId: string, payload: VideoTaskBatchRequest) {
    return fetchJson<VideoTaskBatchResponse>(`/api/v1/videos/${videoId}/tasks/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  resummarizeVideoTask(videoId: string, payload: { task_id?: string | null; page_number?: number | null }) {
    return fetchJson<TaskDetail>(`/api/v1/videos/${videoId}/tasks/resummary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  resummarizeVideoTasksBatch(videoId: string, payload: VideoTaskBatchRequest) {
    return fetchJson<VideoTaskBatchResponse>(`/api/v1/videos/${videoId}/tasks/resummary/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  createAggregateSummaryTask(videoId: string, payload?: { page_numbers?: number[] | null }) {
    return fetchJson<TaskDetail>(`/api/v1/videos/${videoId}/tasks/aggregate-summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  getTaskResult(taskId: string) {
    return fetchJson<TaskDetail>(`/api/v1/tasks/${taskId}/result`);
  },
  getTaskEvents(taskId: string) {
    return fetchJson<TaskEvent[]>(`/api/v1/tasks/${taskId}/events`);
  },
  listTasks() {
    return fetchJson<TaskSummary[]>("/api/v1/tasks");
  },
  getTaskMindMap(taskId: string) {
    return fetchJson<TaskMindMapResponse>(`/api/v1/tasks/${taskId}/mindmap`);
  },
  getTaskVisualEvidence(taskId: string) {
    return fetchJson<TaskVisualEvidenceResponse>(`/api/v1/tasks/${taskId}/visual-evidence`);
  },
  exportTaskMarkdown(
    taskId: string,
    payload?: { target?: "markdown" | "obsidian"; include_transcript?: boolean; output_dir?: string },
  ) {
    return fetchJson<TaskMarkdownExportResponse>(`/api/v1/tasks/${taskId}/exports/markdown`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  exportTaskTranscript(taskId: string, payload?: { output_dir?: string }) {
    return fetchJson<TaskMarkdownExportResponse>(`/api/v1/tasks/${taskId}/exports/transcript`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  generateTaskMindMap(taskId: string, options?: { force?: boolean }) {
    const url = new URL(`/api/v1/tasks/${taskId}/mindmap`, window.location.origin);
    if (options?.force) {
      url.searchParams.set("force", "1");
    }
    return fetchJson<TaskMindMapResponse>(url.toString(), { method: "POST" });
  },
  generateTaskVisualEvidence(taskId: string, options?: { force?: boolean; mode?: string }) {
    const url = new URL(`/api/v1/tasks/${taskId}/visual-evidence`, window.location.origin);
    if (options?.force) {
      url.searchParams.set("force", "1");
    }
    if (options?.mode) {
      url.searchParams.set("mode", options.mode);
    }
    return fetchJson<TaskVisualEvidenceResponse>(url.toString(), { method: "POST" });
  },
  deleteTask(taskId: string) {
    return fetchJson<{ deleted: boolean }>(`/api/v1/tasks/${taskId}`, { method: "DELETE" });
  },
  installCuda(payload: { cuda_variant: string }) {
    return fetchJson<{
      installed: boolean;
      cudaVariant: string;
      runtimeChannel?: string;
      restartRequired?: boolean;
      stdoutTail?: string;
      environment?: EnvironmentInfo;
    }>("/api/v1/cuda/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  installLocalAsr(payload?: { reinstall?: boolean }) {
    return fetchJson<{
      installed: boolean;
      runtimeChannel?: string;
      stdoutTail?: string;
      environment?: EnvironmentInfo;
    }>("/api/v1/asr/local/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  installKnowledgeDependencies(payload?: { reinstall?: boolean }) {
    return fetchJson<{
      installed: boolean;
      runtimeChannel?: string;
      stdoutTail?: string;
      environment?: EnvironmentInfo;
    }>("/api/v1/knowledge/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  createTaskEventSource(taskId: string, after?: string | null) {
    const url = new URL(`/api/v1/tasks/${taskId}/events/stream`, window.location.origin);
    if (after) {
      url.searchParams.set("after", after);
    }
    return new EventSource(url.toString());
  },
  getKnowledgeTags(videoId?: string) {
    const url = new URL("/api/v1/knowledge/tags", window.location.origin);
    if (videoId) {
      url.searchParams.set("video_id", videoId);
    }
    return fetchJson<KnowledgeTagListResponse | VideoKnowledgeTagListResponse>(url.toString());
  },
  addKnowledgeTag(payload: { video_id: string; tag: string }) {
    return fetchJson<VideoKnowledgeTagListResponse>("/api/v1/knowledge/tags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  deleteKnowledgeTag(videoId: string, tag: string) {
    return fetchJson<VideoKnowledgeTagListResponse>(`/api/v1/knowledge/tags/${encodeURIComponent(videoId)}/${encodeURIComponent(tag)}`, {
      method: "DELETE",
    });
  },
  autoTagKnowledge(payload?: { video_ids?: string[] }) {
    return fetchJson<KnowledgeAutoTagResponse>("/api/v1/knowledge/auto-tag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
  },
  getKnowledgeNetwork(options?: { selectedTags?: string[]; maxTags?: number; maxVideos?: number }) {
    const url = new URL("/api/v1/knowledge/network", window.location.origin);
    for (const tag of options?.selectedTags || []) {
      if (tag) {
        url.searchParams.append("selected_tag", tag);
      }
    }
    if (options?.maxTags) {
      url.searchParams.set("max_tags", String(options.maxTags));
    }
    if (options?.maxVideos) {
      url.searchParams.set("max_videos", String(options.maxVideos));
    }
    return fetchJson<KnowledgeNetworkResponse>(url.toString());
  },
  searchKnowledge(payload: KnowledgeSearchRequest) {
    return fetchJson<KnowledgeSearchResponse>("/api/v1/knowledge/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  askKnowledge(payload: { query: string; context_limit?: number; history?: Array<{ role: "user" | "assistant"; content: string }> }) {
    return fetchJson<KnowledgeAskResponse>("/api/v1/knowledge/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  async streamKnowledgeAsk(
    payload: { query: string; context_limit?: number; history?: Array<{ role: "user" | "assistant"; content: string }> },
    handlers: {
      onTool?(tool: KnowledgeToolTrace): void;
      onReasoningDelta?(delta: string): void;
      onTextDelta?(delta: string): void;
      onSources?(sources: KnowledgeAskResponse["sources"]): void;
      onDone?(result: KnowledgeAskResponse): void;
      onError?(message: string): void;
    },
    options?: { signal?: AbortSignal },
  ) {
    const response = await fetch("/api/v1/knowledge/ask/stream", await withDesktopAuth({
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
      signal: options?.signal,
    }));
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Request failed: ${response.status}`);
    }
    if (!response.body) {
      throw new Error("浏览器不支持流式响应。");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";

      for (const block of blocks) {
        const parsed = parseSseBlock(block);
        if (!parsed) {
          continue;
        }
        let data: unknown = parsed.data;
        try {
          data = JSON.parse(parsed.data);
        } catch {
          data = parsed.data;
        }
        switch (parsed.event) {
          case "tool":
            handlers.onTool?.(data as KnowledgeToolTrace);
            break;
          case "text_delta":
            handlers.onTextDelta?.(String((data as { delta?: string }).delta || ""));
            break;
          case "reasoning_delta":
            handlers.onReasoningDelta?.(String((data as KnowledgeReasoningDelta).delta || ""));
            break;
          case "sources":
            handlers.onSources?.(((data as { sources?: KnowledgeAskResponse["sources"] }).sources || []));
            break;
          case "done":
            handlers.onDone?.(data as KnowledgeAskResponse);
            break;
          case "error":
            handlers.onError?.(String((data as { message?: string }).message || "流式问答失败"));
            break;
          default:
            break;
        }
      }

      if (done) {
        break;
      }
    }
  },
  getKnowledgeStats() {
    return fetchJson<KnowledgeStatsResponse>("/api/v1/knowledge/stats");
  },
  rebuildKnowledgeIndex() {
    return fetchJson<{ indexed_videos: number }>("/api/v1/knowledge/rebuild-index", {
      method: "POST",
    });
  },
};
