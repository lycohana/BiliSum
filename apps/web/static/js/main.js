import { api } from "./api.js";
import { state } from "./state.js";
import { parseAppLocation, routeMeta } from "./utils.js";
import { buildTaskProgressView, renderHomeRegions, renderHomeView, renderTaskProgressEvents } from "./views/home.js";
import { renderSettingsView } from "./views/settings.js";

const elements = {
  nav: document.getElementById("nav"),
  pageEyebrow: document.getElementById("page-eyebrow"),
  pageTitle: document.getElementById("page-title"),
  serviceBadge: document.getElementById("service-badge"),
  liveStatus: document.getElementById("live-status"),
  viewRoot: document.getElementById("view-root"),
};
let renderedViewKey = "";
const transientStatusTimers = new Map();

bootstrap().catch((error) => {
  elements.viewRoot.innerHTML = `<div class="grid-card empty-state">
    <div class="empty-detail">
      <svg class="empty-icon" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="8" x2="12" y2="12"></line>
        <line x1="12" y1="16" x2="12.01" y2="16"></line>
      </svg>
      <h3>页面初始化失败</h3>
      <p>${escapeHtml(error.message || "未知错误")}</p>
    </div>
  </div>`;
});

async function bootstrap() {
  syncRouteFromLocation();
  bindNavigation();
  bindViewEvents();
  window.addEventListener("popstate", async () => {
    syncRouteFromLocation();
    await refreshApp({ fullView: true });
  });
  await refreshApp({ fullView: true });
  startPolling();
}

function syncRouteFromLocation() {
  const route = parseAppLocation(window.location.pathname);
  state.route = route.route;
  state.page = route.page;
  state.selectedVideoId = route.videoId || state.selectedVideoId;
}

function navigateTo(path) {
  if (window.location.pathname === path) return;
  window.history.pushState({}, "", path);
  syncRouteFromLocation();
}

function inferTransientStatusTone(message) {
  if (/失败|错误|不可用|未就绪|无效|已取消|关闭/i.test(message)) {
    return "error";
  }
  if (/完成|成功|已保存|已刷新|已开始|已删除|已更新|已复制|已请求/i.test(message)) {
    return "success";
  }
  return "info";
}

function resolveTransientStatusDuration(message, tone) {
  if (!message) {
    return null;
  }
  if (/^正在|检测到.+请先|请先选择/i.test(message)) {
    return null;
  }
  return tone === "error" ? 6500 : 4800;
}

function clearTransientStatus(key, { shouldRender = true } = {}) {
  const timer = transientStatusTimers.get(key);
  if (timer) {
    window.clearTimeout(timer);
    transientStatusTimers.delete(key);
  }
  if (!state[key]) {
    return;
  }
  state[key] = "";
  if (shouldRender) {
    render({ fullView: true });
  }
}

function setTransientStatus(key, message, { shouldRender = true } = {}) {
  const nextMessage = String(message || "");
  const previousMessage = String(state[key] || "");
  const timer = transientStatusTimers.get(key);
  if (timer) {
    window.clearTimeout(timer);
    transientStatusTimers.delete(key);
  }

  state[key] = nextMessage;

  if (nextMessage) {
    const tone = inferTransientStatusTone(nextMessage);
    const duration = resolveTransientStatusDuration(nextMessage, tone);
    if (duration != null) {
      transientStatusTimers.set(key, window.setTimeout(() => {
        if (state[key] === nextMessage) {
          state[key] = "";
          transientStatusTimers.delete(key);
          render({ fullView: true });
        }
      }, duration));
    }
  }

  if (shouldRender && previousMessage !== nextMessage) {
    render({ fullView: true });
  } else if (shouldRender) {
    render({ fullView: true });
  }
}

function bindNavigation() {
  elements.nav.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-route]");
    if (!target) return;
    if (target.dataset.route === "settings") {
      navigateTo("/settings");
      state.route = "settings";
      state.page = "settings";
    } else {
      navigateTo("/");
      state.route = "library";
      state.page = "library";
    }
    render({ fullView: true });
  });
}

function bindViewEvents() {
  elements.viewRoot.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.id === "probe-form") {
      event.preventDefault();
      handleProbeSubmit(event).catch(console.error);
      return;
    }
    if (form.id === "settings-form") {
      event.preventDefault();
      handleSettingsSubmit(event).catch(console.error);
    }
  });

  elements.viewRoot.addEventListener("input", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.id === "library-search") {
      state.librarySearch = target.value;
      render({ regions: ["list"] });
    }
  });

  elements.viewRoot.addEventListener("click", async (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (!target) return;

    const dismissButton = target.closest("[data-dismiss-status]");
    if (dismissButton instanceof HTMLElement) {
      event.preventDefault();
      event.stopPropagation();
      clearTransientStatus(dismissButton.dataset.dismissStatus || "");
      return;
    }

    const actionNode = target.closest("[data-action]");
    if (actionNode instanceof HTMLElement) {
      event.stopPropagation();
      await handleVideoAction(actionNode.dataset.action, actionNode.dataset);
      return;
    }

    if (target.closest("#refresh-env")) {
      await handleRefreshEnvironment();
      return;
    }

    if (target.closest("#refresh-logs")) {
      await handleRefreshLogs();
      return;
    }

    if (target.closest("#shutdown-service")) {
      await handleShutdownService();
      return;
    }

    if (target.closest("#install-cuda")) {
      await handleInstallCuda();
      return;
    }

    if (target.closest("#install-local-asr")) {
      await handleInstallLocalAsr();
      return;
    }

    const videoCard = target.closest("[data-video-id]");
    if (videoCard instanceof HTMLElement) {
      await handleVideoAction("open-video", { videoId: videoCard.dataset.videoId });
    }
  });
  
  // 支持键盘导航视频卡片
  elements.viewRoot.addEventListener("keydown", async (event) => {
    if (event.key === "Enter" || event.key === " ") {
      const videoCard = event.target.closest("[data-video-id]");
      if (videoCard instanceof HTMLElement) {
        event.preventDefault();
        await handleVideoAction("open-video", { videoId: videoCard.dataset.videoId });
      }
    }
  });
}

async function refreshApp({ fullView = false } = {}) {
  const prevState = getRenderState();
  try {
    const [health, systemInfo, settings, videos, environment] = await Promise.all([
      api.getHealth(),
      api.getSystemInfo(),
      api.getSettings(),
      api.listVideos(),
      api.getEnvironment(),
    ]);
    state.serviceOnline = health.status === "ok";
    state.systemInfo = systemInfo;
    state.settings = settings;
    state.environment = environment;
    state.videos = videos;
    state.logPath = systemInfo?.service?.log_file || state.logPath;

    if (!state.selectedVideoId && videos.length) {
      state.selectedVideoId = videos[0].video_id;
    }
    if (state.selectedVideoId) {
      await loadSelectedVideo({ preserveRender: true });
    }

    const nextState = getRenderState();
    const changedRegions = getChangedRegions(prevState, nextState);
    if (fullView || hasShellChanges(prevState, nextState) || changedRegions.length) {
      render({ fullView, regions: changedRegions });
    }
  } catch (error) {
    state.serviceOnline = false;
    render({ errorMessage: error.message || "服务不可用" });
  }
}

async function loadSelectedVideo({ preserveRender = false } = {}) {
  if (!state.selectedVideoId) return;
  const [video, tasks] = await Promise.all([
    api.getVideo(state.selectedVideoId),
    api.getVideoTasks(state.selectedVideoId),
  ]);
  state.selectedVideoDetail = video;
  state.selectedVideoTasks = tasks;
  const nextTaskId =
    state.selectedTaskId && tasks.some((task) => task.task_id === state.selectedTaskId)
      ? state.selectedTaskId
      : tasks[0]?.task_id ?? null;
  state.selectedTaskId = nextTaskId;
  if (nextTaskId) {
    const [detail, events] = await Promise.all([
      api.getTaskResult(nextTaskId),
      api.getTaskEvents(nextTaskId),
    ]);
    state.selectedTaskDetail = detail;
    state.selectedTaskEvents = events;
    state.eventSourceAfter = events.length ? events[events.length - 1].created_at : null;
    syncTaskEventStream();
  } else {
    state.selectedTaskDetail = null;
    state.selectedTaskEvents = [];
    closeTaskEventStream();
  }
  if (!preserveRender) {
    render({ fullView: true });
  }
}

async function handleProbeSubmit(event) {
  event.preventDefault();
  const url = document.getElementById("probe-url-input")?.value.trim();
  if (!url) {
    setTransientStatus("submitStatus", "请输入 Bilibili / YouTube 视频链接，或直接输入 BV 号");
    return;
  }
  setTransientStatus("submitStatus", "正在抓取视频信息并准备开始总结...");
  try {
    let response = await api.probeVideo({ url, force_refresh: false });
    if (response.requires_selection && Array.isArray(response.pages) && response.pages.length > 0) {
      const pageChoices = response.pages.map((item) => `P${item.page}: ${item.title}`).join("\n");
      const selected = window.prompt(`检测到多 P 视频，请输入要解析的 P 编号：\n${pageChoices}`, String(response.pages[0].page));
      if (!selected) {
        setTransientStatus("submitStatus", "已取消解析");
        return;
      }
      const selectedPage = response.pages.find((item) => String(item.page) === selected.trim());
      if (!selectedPage) {
        setTransientStatus("submitStatus", "输入的 P 编号无效");
        return;
      }
      setTransientStatus("submitStatus", `已选择 P${selectedPage.page}，正在创建任务...`);
      state.probePreview = response.video;
      state.selectedVideoId = response.video.video_id;
      navigateTo(`/videos/${response.video.video_id}`);
      const created = await api.createVideoTask(response.video.video_id, { page_number: selectedPage.page });
      state.selectedTaskId = created.task_id;
      setTransientStatus("submitStatus", `P${selectedPage.page} 已开始生成摘要`, { shouldRender: false });
      await refreshApp({ fullView: true });
      return;
    }
    state.probePreview = response.video;
    state.selectedVideoId = response.video.video_id;
    navigateTo(`/videos/${response.video.video_id}`);
    const created = await api.createVideoTask(response.video.video_id);
    state.selectedTaskId = created.task_id;
    setTransientStatus("submitStatus", response.cached ? "已从视频库读取并开始总结" : "视频已加入本地库并开始总结", { shouldRender: false });
    await refreshApp({ fullView: true });
  } catch (error) {
    setTransientStatus("submitStatus", error.message || "开始总结失败");
  }
}

async function handleVideoAction(action, dataset) {
  if (action === "open-video" && dataset.videoId) {
    state.selectedVideoId = dataset.videoId;
    navigateTo(`/videos/${dataset.videoId}`);
    await loadSelectedVideo({ preserveRender: false });
    return;
  }

  if (action === "back-library") {
    navigateTo("/");
    state.page = "library";
    render({ fullView: true });
    return;
  }

  if (action === "start-task" && dataset.videoId) {
    setTransientStatus("submitStatus", "正在创建处理任务...");
    try {
      const created = await api.createVideoTask(dataset.videoId);
      state.selectedTaskId = created.task_id;
      await refreshApp({ fullView: true });
    } catch (error) {
      setTransientStatus("submitStatus", error.message || "创建任务失败");
    }
    return;
  }

  if (action === "refresh-video" && dataset.videoUrl) {
    setTransientStatus("submitStatus", "正在重新获取视频信息...");
    try {
      const response = await api.refreshVideo(dataset.videoUrl);
      if (response?.video?.video_id) {
        state.selectedVideoId = response.video.video_id;
      }
      setTransientStatus("submitStatus", "视频信息已更新", { shouldRender: false });
      await refreshApp({ fullView: state.page === "video-detail" });
    } catch (error) {
      setTransientStatus("submitStatus", error.message || "重新获取视频信息失败");
    }
    return;
  }

  if (action === "delete-video" && dataset.videoId) {
    if (!confirm("确定要删除这个视频吗？这将同时删除所有相关任务。")) {
      return;
    }
    try {
      await api.deleteVideo(dataset.videoId);
      if (state.selectedVideoId === dataset.videoId) {
        state.selectedVideoId = null;
        state.selectedTaskId = null;
        state.selectedTaskDetail = null;
        state.selectedTaskEvents = [];
        state.selectedVideoDetail = null;
        state.selectedVideoTasks = [];
      }
      if (state.page === "video-detail" && dataset.videoId) {
        navigateTo("/");
        state.page = "library";
      }
      setTransientStatus("submitStatus", "视频已删除", { shouldRender: false });
      await refreshApp({ fullView: true });
    } catch (error) {
      setTransientStatus("submitStatus", error.message || "删除视频失败");
    }
    return;
  }

  if (action === "delete-task" && dataset.taskId) {
    if (!confirm("确定要删除这个任务吗？")) {
      return;
    }
    try {
      await api.deleteTask(dataset.taskId);
      if (state.selectedTaskId === dataset.taskId) {
        state.selectedTaskId = null;
      }
      await refreshApp({ fullView: true });
    } catch (error) {
      setTransientStatus("submitStatus", error.message || "删除任务失败");
    }
    return;
  }

  if (action === "select-task" && dataset.taskId) {
    state.selectedTaskId = dataset.taskId;
    await loadSelectedVideo({ preserveRender: false });
  }
}

async function handleSettingsSubmit(event) {
  event.preventDefault();
  const readValue = (id, fallback = "") => {
    const element = document.getElementById(id);
    if (!element) return fallback;
    return "value" in element ? element.value : fallback;
  };
  const readTrimmedValue = (id, fallback = "") => String(readValue(id, fallback)).trim();
  const readNumberValue = (id, fallback = 0) => {
    const parsed = Number(readValue(id, fallback));
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const readChecked = (id, fallback = false) => {
    const element = document.getElementById(id);
    if (!element) return fallback;
    return "checked" in element ? element.checked : fallback;
  };
  const current = state.settings || {};
  const payload = {
    host: readTrimmedValue("host", current.host || ""),
    port: readNumberValue("port", current.port || 3838),
    data_dir: readTrimmedValue("data_dir", current.data_dir || ""),
    cache_dir: readTrimmedValue("cache_dir", current.cache_dir || ""),
    tasks_dir: readTrimmedValue("tasks_dir", current.tasks_dir || ""),
    database_url: readTrimmedValue("database_url", current.database_url || ""),
    transcription_provider: readValue("transcription_provider", current.transcription_provider || "siliconflow"),
    whisper_model: readTrimmedValue("whisper_model", current.whisper_model || ""),
    whisper_device: readTrimmedValue("whisper_device", current.whisper_device || ""),
    whisper_compute_type: readTrimmedValue("whisper_compute_type", current.whisper_compute_type || ""),
    device_preference: readValue("device_preference", current.device_preference || "cpu"),
    compute_type: readValue("compute_type", current.compute_type || "int8"),
    model_mode: readValue("model_mode", current.model_mode || "fixed"),
    fixed_model: readValue("fixed_model", current.fixed_model || "tiny"),
    siliconflow_asr_base_url: readTrimmedValue("siliconflow_asr_base_url", current.siliconflow_asr_base_url || ""),
    siliconflow_asr_model: readTrimmedValue("siliconflow_asr_model", current.siliconflow_asr_model || "TeleAI/TeleSpeechASR"),
    siliconflow_asr_api_key: readValue("siliconflow_asr_api_key", current.siliconflow_asr_api_key || ""),
    siliconflow_asr_chunk_duration_seconds: readNumberValue("siliconflow_asr_chunk_duration_seconds", current.siliconflow_asr_chunk_duration_seconds ?? 1800),
    siliconflow_asr_concurrency: readNumberValue("siliconflow_asr_concurrency", current.siliconflow_asr_concurrency ?? 2),
    multimodal_asr_base_url: readTrimmedValue("multimodal_asr_base_url", current.multimodal_asr_base_url || ""),
    multimodal_asr_model: readTrimmedValue("multimodal_asr_model", current.multimodal_asr_model || "mimo-v2-omni"),
    multimodal_asr_api_key: readValue("multimodal_asr_api_key", current.multimodal_asr_api_key || ""),
    multimodal_asr_chunk_duration_seconds: readNumberValue("multimodal_asr_chunk_duration_seconds", current.multimodal_asr_chunk_duration_seconds ?? 180),
    multimodal_asr_max_retries: readNumberValue("multimodal_asr_max_retries", current.multimodal_asr_max_retries ?? 5),
    cuda_variant: readValue("cuda_variant", current.cuda_variant || "cu128"),
    runtime_channel: readValue("runtime_channel", current.runtime_channel || "base"),
    output_dir: readTrimmedValue("output_dir", current.output_dir || ""),
    preserve_temp_audio: readChecked("preserve_temp_audio", Boolean(current.preserve_temp_audio)),
    enable_cache: readChecked("enable_cache", Boolean(current.enable_cache)),
    language: readTrimmedValue("language", current.language || "zh"),
    summary_mode: readValue("summary_mode", current.summary_mode || "llm"),
    llm_enabled: readChecked("llm_enabled", Boolean(current.llm_enabled)),
    llm_provider: readTrimmedValue("llm_provider", current.llm_provider || ""),
    llm_base_url: readTrimmedValue("llm_base_url", current.llm_base_url || ""),
    llm_model: readTrimmedValue("llm_model", current.llm_model || ""),
    llm_api_key: readValue("llm_api_key", current.llm_api_key || ""),
    twelvelabs_summary_enabled: readChecked("twelvelabs_summary_enabled", Boolean(current.twelvelabs_summary_enabled)),
    twelvelabs_api_key: readValue("twelvelabs_api_key", current.twelvelabs_api_key || ""),
    twelvelabs_model: readTrimmedValue("twelvelabs_model", current.twelvelabs_model || "pegasus1.5"),
    twelvelabs_base_url: readTrimmedValue("twelvelabs_base_url", current.twelvelabs_base_url || ""),
    twelvelabs_prompt: readValue("twelvelabs_prompt", current.twelvelabs_prompt || ""),
    knowledge_llm_mode: readValue("knowledge_llm_mode", current.knowledge_llm_mode || "same_as_main"),
    knowledge_llm_enabled: readChecked("knowledge_llm_enabled", Boolean(current.knowledge_llm_enabled)),
    knowledge_llm_provider: readTrimmedValue("knowledge_llm_provider", current.knowledge_llm_provider || "openai-compatible"),
    knowledge_llm_base_url: readTrimmedValue("knowledge_llm_base_url", current.knowledge_llm_base_url || ""),
    knowledge_llm_model: readTrimmedValue("knowledge_llm_model", current.knowledge_llm_model || ""),
    knowledge_llm_api_key: readValue("knowledge_llm_api_key", current.knowledge_llm_api_key || ""),
    knowledge_enabled: readChecked("knowledge_enabled", Boolean(current.knowledge_enabled)),
    knowledge_index_auto_rebuild: readValue("knowledge_index_auto_rebuild", current.knowledge_index_auto_rebuild || "disabled"),
    summary_system_prompt: readValue("summary_system_prompt", current.summary_system_prompt || ""),
    summary_user_prompt_template: readValue(
      "summary_user_prompt_template",
      current.summary_user_prompt_template || "",
    ),
    summary_chunk_target_chars: Number(readValue("summary_chunk_target_chars", current.summary_chunk_target_chars || 2200)),
    summary_chunk_overlap_segments: Number(readValue("summary_chunk_overlap_segments", current.summary_chunk_overlap_segments || 2)),
    summary_chunk_concurrency: Number(readValue("summary_chunk_concurrency", current.summary_chunk_concurrency || 2)),
    summary_chunk_retry_count: Number(readValue("summary_chunk_retry_count", current.summary_chunk_retry_count || 2)),
  };
  setTransientStatus("settingsSaveStatus", "正在保存设置...");
  try {
    const response = await api.updateSettings(payload);
    state.settings = response.settings;
    setTransientStatus("settingsSaveStatus", response.message || "设置已保存", { shouldRender: false });
    await refreshApp({ fullView: true });
  } catch (error) {
    setTransientStatus("settingsSaveStatus", error.message || "设置保存失败");
  }
}

async function handleRefreshEnvironment() {
  setTransientStatus("cudaActionStatus", "正在检测本机环境...");
  try {
    const runtimeChannel = document.getElementById("runtime_channel")?.value || state.settings?.runtime_channel || "base";
    state.environment = await api.getEnvironment({ runtimeChannel, refresh: true });
    setTransientStatus("cudaActionStatus", "环境检测完成", { shouldRender: false });
  } catch (error) {
    setTransientStatus("cudaActionStatus", error.message || "环境检测失败", { shouldRender: false });
  }
  render({ fullView: true });
}

async function handleRefreshLogs() {
  setTransientStatus("serviceActionStatus", "正在读取后端日志...");
  try {
    const response = await api.getSystemLogs(200);
    state.logOutput = response.content || "";
    state.logPath = response.path || "";
    setTransientStatus("serviceActionStatus", "日志已刷新", { shouldRender: false });
  } catch (error) {
    setTransientStatus("serviceActionStatus", error.message || "读取日志失败", { shouldRender: false });
  }
  render({ fullView: true });
}

async function handleInstallCuda() {
  setTransientStatus("cudaActionStatus", "正在安装 CUDA 支持，这可能需要几分钟...", { shouldRender: false });
  state.cudaInstallOutput = "";
  render({ fullView: true });
  try {
    const response = await api.installCuda({
      cuda_variant: document.getElementById("cuda_variant")?.value || "cu128",
    });
    state.cudaInstallOutput = response.stdoutTail || "";
    if (state.settings) {
      state.settings = {
        ...state.settings,
        cuda_variant: response.cudaVariant,
        runtime_channel: response.runtimeChannel || state.settings.runtime_channel,
      };
    }
    state.environment = await api.getEnvironment({
      runtimeChannel: response.runtimeChannel || state.settings?.runtime_channel || "base",
      refresh: true,
    });
    setTransientStatus(
      "cudaActionStatus",
      response.restartRequired
        ? "CUDA 安装完成，请重启应用后切换到新的 GPU 运行环境"
        : "CUDA 安装完成",
      { shouldRender: false },
    );
  } catch (error) {
    setTransientStatus("cudaActionStatus", error.message || "CUDA 安装失败", { shouldRender: false });
  }
  render({ fullView: true });
}

async function handleInstallLocalAsr() {
  setTransientStatus("localAsrActionStatus", "正在安装本地语音识别环境...", { shouldRender: false });
  try {
    const response = await api.installLocalAsr({});
    state.localAsrInstallOutput = response.stdoutTail || "";
    if (response.environment) {
      state.environment = response.environment;
    } else {
      state.environment = await api.getEnvironment({
        runtimeChannel: state.settings?.runtime_channel || "base",
        refresh: true,
      });
    }
    if (response.installed && state.settings) {
      try {
        const settingsResponse = await api.updateSettings({
          ...state.settings,
          transcription_provider: "local",
        });
        state.settings = settingsResponse.settings;
        setTransientStatus("settingsSaveStatus", settingsResponse.message || "已切换为本地 ASR", { shouldRender: false });
      } catch (error) {
        setTransientStatus(
          "localAsrActionStatus",
          `本地 ASR 已安装，但切换默认转写方式失败：${error.message || "保存设置失败"}`,
          { shouldRender: false },
        );
        render({ fullView: true });
        return;
      }
    }
    setTransientStatus("localAsrActionStatus", response.installed ? "本地语音识别环境已安装" : "本地语音识别环境安装失败", { shouldRender: false });
  } catch (error) {
    setTransientStatus("localAsrActionStatus", error.message || "本地语音识别环境安装失败", { shouldRender: false });
  }
  render({ fullView: true });
}

async function handleShutdownService() {
  setTransientStatus("serviceActionStatus", "正在关闭后端服务...");
  try {
    const response = await api.shutdownService();
    setTransientStatus("serviceActionStatus", response.message || "服务正在关闭", { shouldRender: false });
    state.serviceOnline = false;
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  } catch (error) {
    setTransientStatus("serviceActionStatus", error.message || "关闭服务失败", { shouldRender: false });
  }
  render({ fullView: true });
}

function render({ errorMessage = "", fullView = false, regions = [] } = {}) {
  const meta = routeMeta(state.route);
  elements.pageEyebrow.textContent = meta.eyebrow;
  elements.pageTitle.textContent =
    state.page === "video-detail" && state.selectedVideoDetail
      ? state.selectedVideoDetail.title
      : meta.title;
  elements.serviceBadge.textContent = state.serviceOnline ? "服务在线" : "服务离线";
  elements.serviceBadge.className = `service-badge ${state.serviceOnline ? "service-online" : "service-offline"}`;
  elements.liveStatus.innerHTML = renderLiveStatus(errorMessage);
  highlightNav();

  const nextViewKey = getViewKey();
  if (fullView || renderedViewKey !== nextViewKey || state.route === "settings") {
    if (state.route === "settings") {
      elements.viewRoot.innerHTML = renderSettingsView(state);
    } else {
      elements.viewRoot.innerHTML = renderHomeView(state);
    }
    renderedViewKey = nextViewKey;
    return;
  }

  if (state.route !== "library") return;

  const regionHtml = renderHomeRegions(state);
  for (const region of regions) {
    if (region === "progress" && patchTaskProgressNodes()) {
      continue;
    }
    const targetId = getRegionTargetId(region);
    const html = regionHtml[region];
    if (!targetId || html == null) continue;
    const node = document.getElementById(targetId);
    if (node) {
      node.innerHTML = html;
    }
  }
}

function patchTaskProgressNodes() {
  if (state.route !== "library" || state.page !== "video-detail" || !state.selectedTaskDetail) {
    return false;
  }

  const bar = document.getElementById("task-progress-bar");
  const fill = document.getElementById("task-progress-fill");
  const percent = document.getElementById("task-progress-percent");
  const status = document.getElementById("task-progress-status");
  const title = document.getElementById("task-progress-title");
  const subtitle = document.getElementById("task-progress-subtitle");
  const events = document.getElementById("task-progress-events");

  if (!bar || !fill || !percent || !status || !title || !subtitle || !events) {
    return false;
  }

  const view = buildTaskProgressView(state.selectedTaskEvents || []);
  const fillClass = view.hasError ? "error" : view.isCompleted ? "success" : "";

  bar.setAttribute("aria-valuenow", String(Math.round(view.progress)));
  fill.className = `progress-fill-simple ${fillClass}`.trim();
  fill.style.width = `${view.progress}%`;
  percent.textContent = `${Math.round(view.progress)}%`;
  status.textContent = view.headlineEvent?.message || "等待开始...";
  title.textContent = view.title;
  subtitle.textContent = view.subtitle;
  events.innerHTML = renderTaskProgressEvents(state.selectedTaskEvents || []);
  return true;
}

function getViewKey() {
  return state.route === "library" && state.page === "video-detail"
    ? `${state.route}:${state.page}:${state.selectedVideoId || ""}`
    : `${state.route}:${state.page}`;
}

function getRegionTargetId(region) {
  const map = {
    intake: "library-intake-region",
    summary: "library-summary-region",
    grid: "library-grid-region",
    list: "library-list-region",
    hero: "video-detail-hero-region",
    result: "video-detail-result-region",
    progress: "video-detail-progress-region",
    history: "video-detail-history-region",
  };
  return map[region] || "";
}

function getRenderState() {
  return {
    route: state.route,
    page: state.page,
    serviceOnline: state.serviceOnline,
    videos: state.videos,
    librarySearch: state.librarySearch,
    selectedVideoId: state.selectedVideoId,
    selectedVideoDetail: state.selectedVideoDetail,
    selectedVideoTasks: state.selectedVideoTasks,
    selectedTaskId: state.selectedTaskId,
    selectedTaskDetail: state.selectedTaskDetail,
    selectedTaskEvents: state.selectedTaskEvents,
    settings: state.settings,
    environment: state.environment,
    submitStatus: state.submitStatus,
    settingsSaveStatus: state.settingsSaveStatus,
    probePreview: state.probePreview,
    cudaActionStatus: state.cudaActionStatus,
    cudaInstallOutput: state.cudaInstallOutput,
    logOutput: state.logOutput,
    logPath: state.logPath,
    serviceActionStatus: state.serviceActionStatus,
  };
}

function isEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function hasShellChanges(prevState, nextState) {
  return !isEqual(
    {
      route: prevState.route,
      page: prevState.page,
      serviceOnline: prevState.serviceOnline,
      videos: prevState.videos,
      selectedVideoDetail: prevState.selectedVideoDetail,
    },
    {
      route: nextState.route,
      page: nextState.page,
      serviceOnline: nextState.serviceOnline,
      videos: nextState.videos,
      selectedVideoDetail: nextState.selectedVideoDetail,
    },
  );
}

function getChangedRegions(prevState, nextState) {
  if (prevState.route !== nextState.route || prevState.page !== nextState.page) {
    return [];
  }

  if (nextState.route !== "library") {
    return [];
  }

  if (nextState.page === "library") {
    const changed = [];
    if (!isEqual(
      { submitStatus: prevState.submitStatus, probePreview: prevState.probePreview },
      { submitStatus: nextState.submitStatus, probePreview: nextState.probePreview },
    )) {
      changed.push("intake");
    }
    if (!isEqual(prevState.videos, nextState.videos)) {
      changed.push("summary", "list");
    }
    if (prevState.librarySearch !== nextState.librarySearch) {
      changed.push("list");
    }
    return [...new Set(changed)];
  }

  const changed = [];
  if (!isEqual(
    { selectedVideoDetail: prevState.selectedVideoDetail, selectedTaskDetail: prevState.selectedTaskDetail },
    { selectedVideoDetail: nextState.selectedVideoDetail, selectedTaskDetail: nextState.selectedTaskDetail },
  )) {
    changed.push("hero");
  }
  if (!isEqual(prevState.selectedVideoDetail?.latest_result, nextState.selectedVideoDetail?.latest_result)) {
    changed.push("result");
  }
  if (!isEqual(
    { selectedTaskDetail: prevState.selectedTaskDetail, selectedTaskEvents: prevState.selectedTaskEvents },
    { selectedTaskDetail: nextState.selectedTaskDetail, selectedTaskEvents: nextState.selectedTaskEvents },
  )) {
    changed.push("progress");
  }
  if (!isEqual(
    { selectedVideoTasks: prevState.selectedVideoTasks, selectedTaskId: prevState.selectedTaskId },
    { selectedVideoTasks: nextState.selectedVideoTasks, selectedTaskId: nextState.selectedTaskId },
  )) {
    changed.push("history");
  }
  return [...new Set(changed)];
}

function renderLiveStatus(errorMessage) {
  const latest = state.videos[0];
  const latestTitle = latest ? latest.title : "暂无视频";
  const latestStatus = latest?.latest_status || "未开始";
  const statusColor = state.serviceOnline ? "var(--color-success)" : "var(--color-error)";
  const statusText = state.serviceOnline ? "运行中" : "不可访问";
  
  // 截断标题，最多显示20个字符
  const truncatedTitle = latestTitle.length > 20 ? latestTitle.slice(0, 20) + "..." : latestTitle;
  
  return `
    <div class="status-card">
      <strong>服务状态</strong>
      <div class="status-caption" style="color: ${statusColor}; font-weight: 600;">${statusText}</div>
    </div>
    <div class="status-card">
      <strong>视频数量</strong>
      <div class="status-caption" style="font-size: 18px; font-weight: 700;">${state.videos.length}</div>
    </div>
    <div class="status-card">
      <strong>最近视频</strong>
      <div class="status-caption" title="${escapeHtml(latestTitle)}">${escapeHtml(truncatedTitle)}</div>
    </div>
    <div class="status-card">
      <strong>最近状态</strong>
      <div class="status-caption">${escapeHtml(latestStatus)}</div>
    </div>
    ${errorMessage ? `<div class="status-card" style="background: var(--bg-error); border: 1px solid var(--error);"><strong style="color: var(--error);">错误</strong><div class="status-caption">${escapeHtml(errorMessage)}</div></div>` : ""}
  `;
}

function highlightNav() {
  for (const item of elements.nav.querySelectorAll("[data-route]")) {
    const isActive = item.dataset.route === state.route;
    item.classList.toggle("active", isActive);
    item.setAttribute("aria-selected", String(isActive));
  }
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    await refreshApp({ fullView: false });
  }, 5000);
}

function syncTaskEventStream() {
  const shouldStream =
    state.route === "library" &&
    state.selectedTaskDetail &&
    !["completed", "failed", "cancelled"].includes(state.selectedTaskDetail.status);
  if (!shouldStream) {
    closeTaskEventStream();
    return;
  }
  if (state.eventSource && state.eventSourceTaskId === state.selectedTaskId) return;
  closeTaskEventStream();
  const source = api.streamTaskEvents(state.selectedTaskId, state.eventSourceAfter);
  state.eventSource = source;
  state.eventSourceTaskId = state.selectedTaskId;
  source.addEventListener("progress", (event) => {
    const payload = JSON.parse(event.data);
    const nextEvent = payload.event;
    if (!state.selectedTaskEvents.some((item) => item.event_id === nextEvent.event_id)) {
      state.selectedTaskEvents = [...state.selectedTaskEvents, nextEvent];
      state.eventSourceAfter = nextEvent.created_at;
      if (state.selectedTaskDetail) {
        state.selectedTaskDetail = {
          ...state.selectedTaskDetail,
          status: payload.status,
          updated_at: payload.updated_at,
          result: payload.result === undefined ? state.selectedTaskDetail.result : payload.result,
          llm_total_tokens: payload.result?.llm_total_tokens ?? state.selectedTaskDetail.llm_total_tokens,
        };
      }
      if (state.selectedVideoDetail) {
        state.selectedVideoDetail = {
          ...state.selectedVideoDetail,
          latest_status: payload.status,
          updated_at: payload.updated_at,
          latest_result: payload.result === undefined ? state.selectedVideoDetail.latest_result : payload.result,
        };
      }
      state.selectedVideoTasks = state.selectedVideoTasks.map((task) =>
        task.task_id === state.selectedTaskId
          ? {
              ...task,
              status: payload.status,
              updated_at: payload.updated_at,
              llm_total_tokens: payload.result?.llm_total_tokens ?? task.llm_total_tokens,
            }
          : task,
      );
      state.videos = state.videos.map((video) =>
        video.video_id === state.selectedVideoId
          ? {
              ...video,
              latest_status: payload.status,
              latest_stage: nextEvent.stage,
              latest_task_id: state.selectedTaskId,
              updated_at: payload.updated_at,
            }
          : video,
      );
      render({ regions: state.page === "video-detail" ? ["hero", "progress", "list", "summary"] : ["list", "summary"] });
    }
    if (["completed", "failed", "cancelled"].includes(payload.status)) {
      closeTaskEventStream();
      refreshApp({ fullView: false }).catch(() => {});
    }
  });
  source.addEventListener("error", () => {
    closeTaskEventStream();
  });
}

function closeTaskEventStream() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
  state.eventSourceTaskId = null;
}

function snapshot() {
  return JSON.stringify(getRenderState());
}

// 简单的 HTML 转义函数（用于错误消息）
function escapeHtml(str) {
  if (typeof str !== "string") return str;
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
