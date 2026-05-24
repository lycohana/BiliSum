import { type ChangeEvent, type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import {
  deriveRuntimeDeviceLabel,
  emptySnapshot,
  getConfigHealth,
  getUpdateDialogSignal,
  isUpdateUnsupported,
  shouldShowSetupAssistant,
  toUpdateState,
  type DesktopState,
  type LibraryFilter,
  type Snapshot,
  type UpdateState,
} from "./appModel";
import { AuthRequiredError, api } from "./api";
import { HomeIcon, KnowledgeIcon, LibraryIcon, SettingsIcon } from "./components/AppIcons";
import { CookieHelpDialog } from "./components/CookieHelpDialog";
import { MultiPageSelectDialog } from "./components/MultiPageSelectDialog";
import { SetupAssistantDialog } from "./components/SetupAssistantDialog";
import { StartupAnnouncementDialog, type StartupAnnouncement } from "./components/StartupAnnouncementDialog";
import { TitleBar } from "./components/TitleBar";
import { UpdateDialog, type UpdateInfo } from "./components/UpdateDialog";
import { HomeTour, isHomeTourSeen } from "./components/HomeTour";
import { HomePage } from "./pages/HomePage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { LibraryPage } from "./pages/LibraryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { VideoDetailPage } from "./pages/VideoDetailPage";
import type { VideoAssetSummary, VideoPageBatchOption, VideoProbeResult } from "./types";

const PROMPT_PRESET_STORAGE_KEY = "bilisum.promptPresetId";

function isAuthRequiredError(error: unknown): error is AuthRequiredError {
  return error instanceof AuthRequiredError
    || (error instanceof Error && error.name === "AuthRequiredError")
    || (error instanceof Error && /访问密钥|unauthorized|401/i.test(error.message));
}

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<Snapshot>(emptySnapshot);
  const [desktop, setDesktop] = useState<DesktopState>({ version: "", backend: null, logPath: "" });
  const [query, setQuery] = useState("");
  const [libraryFilter, setLibraryFilter] = useState<LibraryFilter>("all");
  const [probeUrl, setProbeUrl] = useState("");
  const [submitStatus, setSubmitStatus] = useState("");
  const [probePreview, setProbePreview] = useState<VideoAssetSummary | null>(null);
  const [multiPageDialogOpen, setMultiPageDialogOpen] = useState(false);
  const [multiPageProbeVideo, setMultiPageProbeVideo] = useState<VideoAssetSummary | null>(null);
  const [multiPageOptions, setMultiPageOptions] = useState<VideoPageBatchOption[]>([]);
  const [refreshSeed, setRefreshSeed] = useState(0);
  const [detailRefreshToken, setDetailRefreshToken] = useState(0);
  const [settingsFocusRequest, setSettingsFocusRequest] = useState<{ issueKey: string; nonce: number } | null>(null);
  const [settingsPromptRequest, setSettingsPromptRequest] = useState<{ presetId: string; nonce: number } | null>(null);
  const [darkMode, setDarkMode] = useState<boolean>(() => {
    if (typeof window === "undefined") {
      return false;
    }
    const savedTheme = localStorage.getItem("theme");
    if (savedTheme === "dark") {
      return true;
    }
    if (savedTheme === "light") {
      return false;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") {
      return false;
    }
    return localStorage.getItem("sidebar-collapsed") === "true";
  });
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [updateDialogOpen, setUpdateDialogOpen] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState<boolean>(() => Boolean(window.desktop));
  const [authToken, setAuthToken] = useState("");
  const [authError, setAuthError] = useState("");
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [startupAnnouncement, setStartupAnnouncement] = useState<StartupAnnouncement | null>(null);
  const [setupAssistantOpen, setSetupAssistantOpen] = useState(false);
  const [cookieHelpDialogOpen, setCookieHelpDialogOpen] = useState(false);
  const [setupAssistantDismissed, setSetupAssistantDismissed] = useState(false);
  const setupAssistantForceRef = useRef(false);
  const [homeTourOpen, setHomeTourOpen] = useState(() => !isHomeTourSeen());

  useEffect(() => {
    if (location.pathname === "/" && !isHomeTourSeen()) {
      setHomeTourOpen(true);
    }
  }, [location.pathname]);
  const [updateState, setUpdateState] = useState<UpdateState>({
    status: "idle",
    version: "",
    releaseDate: "",
    releaseNotes: null,
    downloadProgress: 0,
    errorMessage: null,
  });
  const localVideoInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let disposed = false;
    async function checkAuth() {
      if (window.desktop) {
        try {
          await api.createAuthSession(await window.desktop.backend.getAccessToken());
        } catch {
          // Auth headers are still attached to desktop fetches; session setup mainly supports EventSource.
        }
        if (!disposed) {
          setAuthenticated(true);
          setAuthChecked(true);
        }
        return;
      }
      try {
        const status = await api.getAuthStatus();
        if (!disposed) {
          setAuthenticated(Boolean(status.authenticated));
          setAuthChecked(true);
        }
      } catch {
        if (!disposed) {
          setAuthenticated(false);
          setAuthChecked(true);
        }
      }
    }
    void checkAuth();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    const theme = darkMode ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
    void window.desktop?.preferences?.setTheme?.(theme);
  }, [darkMode]);

  useEffect(() => {
    localStorage.setItem("sidebar-collapsed", sidebarCollapsed ? "true" : "false");
  }, [sidebarCollapsed]);

  useEffect(() => {
    let backendCleanup: (() => void) | undefined;
    let updateCleanup: (() => void) | undefined;
    let disposed = false;

    async function bootstrap() {
      if (!window.desktop) return;
      const [version, backend, logPath, currentUpdateStatus, announcement] = await Promise.all([
        window.desktop.app.getVersion(),
        window.desktop.backend.status(),
        window.desktop.logs.getServiceLogPath(),
        window.desktop.update?.getStatus?.() ?? Promise.resolve(null),
        window.desktop.app.getStartupAnnouncement?.() ?? Promise.resolve(null),
      ]);
      if (disposed) return;
      setDesktop({ version, backend, logPath });
      setStartupAnnouncement(announcement);
      if (currentUpdateStatus) {
        setUpdateState(toUpdateState(currentUpdateStatus));
      }
      backendCleanup = window.desktop.backend.onStatus((status) => setDesktop((current) => ({ ...current, backend: status })));
      updateCleanup = window.desktop.update?.onStatus((status) => {
        setUpdateState(toUpdateState(status));
      });
    }

    void bootstrap();
    return () => {
      disposed = true;
      backendCleanup?.();
      updateCleanup?.();
    };
  }, []);

  useEffect(() => {
    let disposed = false;

    async function refresh() {
      if (!authenticated) {
        return;
      }
      try {
        const [health, settings, library] = await Promise.all([
          api.getHealth(),
          api.getSettings(),
          api.getVideoLibrary(),
        ]);
        if (!disposed) {
          setSnapshot((current) => ({
            ...current,
            serviceOnline: health.status === "ok",
            settings,
            videos: library.videos,
            folders: library.folders,
            libraryPreferences: library.preferences,
            error: "",
          }));
        }

        try {
          const systemInfo = await api.getSystemInfo();
          if (!disposed) {
            const runtimeStartupStatus = systemInfo.runtimeStartup?.status;
            const environment = systemInfo.environment ?? systemInfo.runtimeStartup?.environment ?? null;
            const runtimeInitializing = runtimeStartupStatus === "initializing" || environment?.runtimeReady === false;
            setSnapshot((current) => ({
              ...current,
              serviceOnline: health.status === "ok",
              systemInfo,
              environment,
              error: "",
              runtimeInitializing,
            }));
          }
        } catch (error) {
          if (!disposed) {
            setSnapshot((current) => ({
              ...current,
              serviceOnline: health.status === "ok",
              error: current.error,
              runtimeInitializing: true,
            }));
          }
        }
      } catch (error) {
        if (!disposed) {
          if (isAuthRequiredError(error)) {
            setAuthenticated(false);
            setAuthError("");
            return;
          }
          setSnapshot((current) => ({
            ...current,
            serviceOnline: false,
            error: error instanceof Error ? error.message : "服务暂不可用",
            runtimeInitializing: false,
          }));
        }
      }
    }

    void refresh();
    const timer = window.setInterval(() => void refresh(), 8000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [authenticated, refreshSeed]);

  async function handleAuthSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = authToken.trim();
    if (!token) {
      setAuthError("请输入访问密钥。");
      return;
    }
    setAuthSubmitting(true);
    setAuthError("");
    try {
      const status = await api.createAuthSession(token);
      setAuthenticated(Boolean(status.authenticated));
      setAuthToken("");
      setRefreshSeed((current) => current + 1);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "访问密钥无效。");
    } finally {
      setAuthSubmitting(false);
    }
  }

  function openUpdateDialog() {
    setUpdateDialogOpen(true);
  }

  function closeUpdateDialog() {
    setUpdateDialogOpen(false);
  }

  async function closeStartupAnnouncement() {
    const announcement = startupAnnouncement;
    setStartupAnnouncement(null);
    if (announcement) {
      await window.desktop?.app.markStartupAnnouncementSeen?.(announcement.version);
    }
  }

  const updateDialogSignal = useMemo(
    () => getUpdateDialogSignal(updateState),
    [updateState],
  );

  useEffect(() => {
    if (!updateDialogSignal) {
      return;
    }
    setUpdateDialogOpen(true);
  }, [updateDialogSignal]);

  async function handleCheckForUpdates() {
    if (!window.desktop?.update) {
      const result = await api.getAppUpdate();
      setUpdateState(toUpdateState(result));
      return result;
    }
    const result = await window.desktop.update.check();
    setUpdateState(toUpdateState(result));
    return result;
  }

  async function handleDownloadUpdate() {
    if (!window.desktop?.update) {
      throw new Error("当前环境不支持桌面自动更新。");
    }
    const result = await window.desktop.update.download();
    setUpdateState(toUpdateState(result));
    return result;
  }

  async function handleInstallUpdate() {
    if (!window.desktop?.update) {
      throw new Error("当前环境不支持桌面自动更新。");
    }
    await window.desktop.update.install();
  }

  const latestVideo = useMemo(() => {
    return [...snapshot.videos].sort(
      (left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
    )[0] ?? null;
  }, [snapshot.videos]);

  const libraryCounts = useMemo(() => {
    const favorite = snapshot.videos.filter((item) => item.is_favorite).length;
    const completed = snapshot.videos.filter((item) => item.latest_status === "completed").length;
    const running = snapshot.videos.filter((item) => item.latest_status === "running").length;
    const withResult = snapshot.videos.filter((item) => item.has_result).length;
    return {
      total: snapshot.videos.length,
      favorite,
      completed,
      running,
      withResult,
    };
  }, [snapshot.videos]);

  const filteredVideos = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return [...snapshot.videos]
      .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())
      .filter((video) => {
        if (!keyword) return true;
        return video.title.toLowerCase().includes(keyword) || video.source_url.toLowerCase().includes(keyword);
      })
      .filter((video) => {
        if (libraryFilter === "favorite") return video.is_favorite;
        if (libraryFilter === "completed") return video.latest_status === "completed";
        if (libraryFilter === "running") return video.latest_status === "running";
        if (libraryFilter === "with-result") return video.has_result;
        return true;
      });
  }, [libraryFilter, query, snapshot.videos]);

  const favoriteVideos = useMemo(() => {
    return [...snapshot.videos]
      .filter((video) => video.is_favorite)
      .sort((left, right) => {
        const leftTime = new Date(left.favorite_updated_at || left.updated_at).getTime();
        const rightTime = new Date(right.favorite_updated_at || right.updated_at).getTime();
        return rightTime - leftTime;
      });
  }, [snapshot.videos]);

  const recentVideos = useMemo(() => {
    return [...snapshot.videos]
      .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())
      .slice(0, 6);
  }, [snapshot.videos]);

  async function handleToggleFavorite(videoId: string, nextFavorite: boolean) {
    const previousVideos = snapshot.videos;
    const favoriteUpdatedAt = nextFavorite ? new Date().toISOString() : null;
    setSnapshot((current) => ({
      ...current,
      videos: current.videos.map((video) => (
        video.video_id === videoId
          ? { ...video, is_favorite: nextFavorite, favorite_updated_at: favoriteUpdatedAt }
          : video
      )),
    }));
    try {
      const updated = await api.setVideoFavorite(videoId, { is_favorite: nextFavorite });
      setSnapshot((current) => ({
        ...current,
        videos: current.videos.map((video) => (
          video.video_id === videoId
            ? { ...video, ...updated }
            : video
        )),
      }));
    } catch (error) {
      setSnapshot((current) => ({ ...current, videos: previousVideos }));
      throw error;
    }
  }

  const runtimeDeviceLabel = useMemo(() => {
    return deriveRuntimeDeviceLabel({
      transcriptionProvider: snapshot.settings?.transcription_provider,
      whisperDevice: snapshot.settings?.whisper_device,
      cudaAvailable: snapshot.environment?.cudaAvailable,
      hasSettings: Boolean(snapshot.settings),
    });
  }, [snapshot.environment?.cudaAvailable, snapshot.settings]);
  const configHealth = useMemo(() => getConfigHealth(snapshot.settings, snapshot.environment), [snapshot.environment, snapshot.settings]);

  const runtimeVersionLabel = desktop.version || snapshot.systemInfo?.application?.version || "-";
  const runtimeStartupStatus = snapshot.systemInfo?.runtimeStartup?.status;
  const runtimeReady = runtimeStartupStatus === "ready" || (
    Boolean(snapshot.environment)
    && snapshot.environment?.runtimeReady !== false
    && !snapshot.runtimeInitializing
  );
  const runtimeStatusText = useMemo(() => {
    if (snapshot.runtimeInitializing) return "运行环境准备中";
    if (runtimeStartupStatus === "error") return "运行环境异常";
    if (snapshot.environment?.runtimeReady === false) return "运行环境未就绪";
    if (snapshot.environment) return "运行环境就绪";
    return snapshot.serviceOnline ? "检测中" : "等待服务";
  }, [runtimeStartupStatus, snapshot.environment, snapshot.runtimeInitializing, snapshot.serviceOnline]);

  useEffect(() => {
    const shouldOpen = shouldShowSetupAssistant(configHealth, snapshot.settings);
    if (!shouldOpen) {
      if (!setupAssistantForceRef.current) {
        setSetupAssistantOpen(false);
      }
      setSetupAssistantDismissed(false);
      return;
    }
    setupAssistantForceRef.current = false;
    if (!setupAssistantDismissed) {
      setSetupAssistantOpen(true);
    }
  }, [configHealth, setupAssistantDismissed, snapshot.settings]);

  function navigateToConfigIssue(issueKey: string) {
    setupAssistantForceRef.current = false;
    setSetupAssistantOpen(false);
    setSetupAssistantDismissed(true);
    setSettingsFocusRequest({ issueKey, nonce: Date.now() });
    navigate("/settings");
  }

  function navigateToPromptPreset(presetId: string) {
    setupAssistantForceRef.current = false;
    setSetupAssistantOpen(false);
    setSettingsPromptRequest({ presetId, nonce: Date.now() });
    navigate("/settings");
  }

  async function updatePromptRouterMode(mode: "auto" | "confirm") {
    const response = await api.updateSettings({ prompt_router_mode: mode });
    setSnapshot((current) => ({
      ...current,
      settings: response.settings,
      systemInfo: current.systemInfo
        ? {
            ...current.systemInfo,
            settings: response.settings,
          }
        : current.systemInfo,
    }));
  }

  function mergeVideos(nextVideos: VideoAssetSummary[]) {
    setSnapshot((current) => {
      const updated = new Map(nextVideos.map((video) => [video.video_id, video]));
      return {
        ...current,
        videos: current.videos.map((video) => updated.get(video.video_id) || video),
      };
    });
  }

  async function refreshLibrarySnapshot() {
    const library = await api.getVideoLibrary();
    setSnapshot((current) => ({
      ...current,
      videos: library.videos,
      folders: library.folders,
      libraryPreferences: library.preferences,
    }));
    return library;
  }

  async function handleCreateFolder(name: string, parentId?: string | null) {
    const folder = await api.createVideoFolder({ name, parent_id: parentId ?? null });
    setSnapshot((current) => ({ ...current, folders: [folder, ...current.folders] }));
    return folder;
  }

  async function handleUpdateFolder(folderId: string, payload: { name?: string | null; parent_id?: string | null; position?: number | null }) {
    const folder = await api.updateVideoFolder(folderId, payload);
    setSnapshot((current) => ({
      ...current,
      folders: current.folders.map((item) => (item.folder_id === folderId ? folder : item)),
    }));
    return folder;
  }

  async function handleDeleteFolder(folderId: string) {
    await api.deleteVideoFolder(folderId);
    await refreshLibrarySnapshot();
  }

  async function handleMoveVideo(videoId: string, folderId?: string | null, folderIds?: string[]) {
    const updated = await api.moveVideoToFolder(videoId, folderIds ? { folder_ids: folderIds } : { folder_id: folderId ?? null });
    mergeVideos([updated]);
    return updated;
  }

  async function handleDeleteVideo(videoId: string) {
    const previousVideos = snapshot.videos;
    setSnapshot((current) => ({
      ...current,
      videos: current.videos.filter((video) => video.video_id !== videoId),
    }));
    try {
      await api.deleteVideo(videoId);
    } catch (error) {
      setSnapshot((current) => ({ ...current, videos: previousVideos }));
      throw error;
    }
  }

  async function handleSetVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }) {
    const updated = await api.setVideoPin(videoId, payload);
    mergeVideos([updated]);
    return updated;
  }

  async function handleReorderVideos(videoIds: string[], folderId?: string | null) {
    const updated = await api.reorderVideos({ video_ids: videoIds, folder_id: folderId ?? null });
    mergeVideos(updated);
  }

  async function handleUpdateLibraryPreferences(newVideoPosition: "front" | "back") {
    const preferences = await api.updateVideoLibraryPreferences({ new_video_position: newVideoPosition });
    setSnapshot((current) => ({ ...current, libraryPreferences: preferences }));
  }

  function openConfigAssist(issueKey?: string) {
    if (issueKey) {
      navigateToConfigIssue(issueKey);
      return;
    }
    if (shouldShowSetupAssistant(configHealth, snapshot.settings)) {
      setSetupAssistantDismissed(false);
      setSetupAssistantOpen(true);
      return;
    }
    navigate("/settings");
  }

  function closeSetupAssistant() {
    setupAssistantForceRef.current = false;
    setSetupAssistantOpen(false);
    setSetupAssistantDismissed(true);
  }

  function openSettingsFromAssistant() {
    setupAssistantForceRef.current = false;
    setSetupAssistantOpen(false);
    const targetIssueKey = configHealth.blockingIssues[0]?.key || configHealth.issues[0]?.key;
    if (targetIssueKey) {
      navigateToConfigIssue(targetIssueKey);
      return;
    }
    navigate("/settings");
  }

  function isBilibiliCookieHelpError(message: string) {
    return /HTTP\s*412|cookies?\.txt|B\s*站返回|Bilibili rejected|风控拦截|登录态|cookiesfrombrowser|DPAPI/i.test(message);
  }

  function showBilibiliCookieHelp(message: string) {
    setSubmitStatus(message || "B 站请求被拦截，请导出登录态 cookies 后重试。");
    setCookieHelpDialogOpen(true);
  }

  function openCookieExportTutorial() {
    window.open("https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp", "_blank", "noopener,noreferrer");
  }

  function openYtdlpCookieSettings() {
    setCookieHelpDialogOpen(false);
    setSettingsFocusRequest({ issueKey: "ytdlp_cookies_file", nonce: Date.now() });
    navigate("/settings");
  }

  async function captureBilibiliLoginCookies() {
    try {
      const currentProbeUrl = probeUrl.trim();
      if (!window.desktop?.bilibili) {
        setSubmitStatus("当前环境不支持打开 B 站登录窗口，请按教程手动导出 cookies.txt。");
        return;
      }
      setSubmitStatus("请在新窗口登录 B 站，登录成功后会自动保存 cookies...");
      const captured = await window.desktop.bilibili.captureLoginCookies();
      const response = await api.updateSettings({
        ytdlp_cookies_file: captured.cookiesFile,
        ytdlp_cookies_browser: "",
      });
      setSnapshot((current) => ({
        ...current,
        settings: response.settings,
        systemInfo: current.systemInfo
          ? {
              ...current.systemInfo,
              settings: response.settings,
            }
          : current.systemInfo,
      }));
      if (currentProbeUrl) {
        await api.probeVideo({ url: currentProbeUrl, force_refresh: true });
        setSubmitStatus(`B 站登录态已保存，捕获 ${captured.cookieCount} 条 cookies。可以重新提交任务。`);
      } else {
        setSubmitStatus(`B 站登录态已保存，捕获 ${captured.cookieCount} 条 cookies。重新提交任务即可。`);
      }
      setCookieHelpDialogOpen(false);
    } catch (error) {
      setSubmitStatus(error instanceof Error ? error.message : "捕获 B 站登录态失败，请按教程手动导出 cookies.txt。");
      setCookieHelpDialogOpen(true);
    }
  }

  function resolveVisualNoteModeFromPreference(): string | null {
    try {
      const rawValue = window.localStorage.getItem("bilisum.summaryPreference");
      if (!rawValue) return null;
      const parsed = JSON.parse(rawValue) as { noteMode?: unknown };
      return parsed.noteMode === "visual" ? "frame_insert" : "text";
    } catch {
      return null;
    }
  }

  function resolveManualPromptPresetId(): string | null {
    try {
      const value = window.localStorage.getItem(PROMPT_PRESET_STORAGE_KEY)?.trim();
      return value && value !== "general" ? value : null;
    } catch {
      return null;
    }
  }

  async function resolveTaskPromptPresetId(sourceText?: string | null): Promise<string | null> {
    const promptRouterMode = snapshot.settings?.prompt_router_mode || "confirm";
    if (promptRouterMode !== "auto") {
      return resolveManualPromptPresetId();
    }
    const title = sourceText?.trim();
    if (!title) {
      return null;
    }
    try {
      const result = await api.matchPrompt(title);
      return result.preset.id && result.preset.id !== "general" ? result.preset.id : null;
    } catch {
      return null;
    }
  }

  async function buildCreateTaskPayload(pageNumber?: number | null, sourceText?: string | null) {
    return {
      page_number: pageNumber ?? null,
      visual_note_mode: resolveVisualNoteModeFromPreference(),
      prompt_preset_id: await resolveTaskPromptPresetId(sourceText),
    };
  }

  async function createVideoTaskAfterNavigation(video: VideoAssetSummary, sourceText?: string | null) {
    try {
      setSubmitStatus("正在匹配摘要 Prompt 并创建任务...");
      await api.createVideoTask(video.video_id, await buildCreateTaskPayload(null, sourceText || video.title || video.source_url));
      setSubmitStatus("视频已加入本地库并开始总结");
      setRefreshSeed((value) => value + 1);
      setDetailRefreshToken((value) => value + 1);
    } catch (error) {
      const message = error instanceof Error ? error.message : "创建摘要任务失败";
      if (isBilibiliCookieHelpError(message)) {
        showBilibiliCookieHelp(message);
        return;
      }
      setSubmitStatus(message);
    }
  }

  async function handleProbe(event: FormEvent) {
    event.preventDefault();
    if (!probeUrl.trim()) {
      setSubmitStatus("请输入 Bilibili / YouTube 视频链接，或直接输入 BV 号");
      return;
    }
    if (configHealth.hasBlockingIssues) {
      setSubmitStatus(`当前缺少必需配置：${configHealth.blockingIssues.map((item) => item.title).join("、")}。请先完成设置。`);
      if (shouldShowSetupAssistant(configHealth, snapshot.settings)) {
        setSetupAssistantDismissed(false);
        setSetupAssistantOpen(true);
      } else {
        navigate("/settings");
      }
      return;
    }
    setSubmitStatus("正在抓取视频信息并准备开始总结...");
    try {
      const response = await api.probeVideo({ url: probeUrl.trim(), force_refresh: false });
      setProbePreview(response.video);
      if (response.requires_selection && response.pages.length > 0) {
        setMultiPageProbeVideo(response.video);
        setMultiPageOptions(response.pages.map((page) => ({ ...page, aggregate_status: "not_started", has_completed_result: false })));
        setMultiPageDialogOpen(true);
        setSubmitStatus(`检测到 ${response.pages.length} 个分 P，请先勾选要处理的内容`);
        return;
      }
      setSubmitStatus(response.cached ? "已从视频库读取，正在进入详情页..." : "视频已加入本地库，正在进入详情页...");
      const submittedUrl = probeUrl.trim();
      setProbeUrl("");
      setRefreshSeed((value) => value + 1);
      navigate(`/videos/${response.video.video_id}`);
      void createVideoTaskAfterNavigation(response.video, response.video.title || submittedUrl);
    } catch (error) {
      const message = error instanceof Error ? error.message : "开始总结失败";
      if (isBilibiliCookieHelpError(message)) {
        showBilibiliCookieHelp(message);
        return;
      }
      setSubmitStatus(message);
    }
  }

  async function createLocalPathTasks(filePaths: string[]) {
    if (!filePaths.length) {
      return;
    }

    setSubmitStatus(
      filePaths.length > 1 ? `正在读取 ${filePaths.length} 个本地媒体文件并创建任务...` : "正在读取本地媒体信息并准备开始总结...",
    );
    try {
      const responses: VideoProbeResult[] = [];
      for (const filePath of filePaths) {
        const response = await api.probeVideo({ url: filePath, force_refresh: false });
        responses.push(response);
      }
      const firstResponse = responses[0];
      if (!firstResponse) {
        throw new Error("未返回本地媒体信息。");
      }
      setProbePreview(firstResponse.video);
      setProbeUrl("");
      setSubmitStatus(
        responses.length > 1
          ? `已读取 ${responses.length} 个本地媒体文件，正在后台创建摘要任务...`
          : firstResponse.cached ? "已从本地媒体库读取，正在进入详情页..." : "本地媒体已加入视频库，正在进入详情页...",
      );
      setRefreshSeed((value) => value + 1);
      navigate(`/videos/${firstResponse.video.video_id}`);
      void Promise.all(
        responses.map((response) => createVideoTaskAfterNavigation(response.video, response.video.title || response.video.source_url)),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入本地媒体失败";
      if (isBilibiliCookieHelpError(message)) {
        showBilibiliCookieHelp(message);
        return;
      }
      setSubmitStatus(message);
    }
  }

  async function handleImportLocalVideo() {
    if (configHealth.hasBlockingIssues) {
      setSubmitStatus(`当前缺少必需配置：${configHealth.blockingIssues.map((item) => item.title).join("、")}。请先完成设置。`);
      if (shouldShowSetupAssistant(configHealth, snapshot.settings)) {
        setSetupAssistantDismissed(false);
        setSetupAssistantOpen(true);
      } else {
        navigate("/settings");
      }
      return;
    }

    if (window.desktop?.media) {
      const filePaths = window.desktop.media.pickVideoFiles
        ? await window.desktop.media.pickVideoFiles()
        : [await window.desktop.media.pickVideoFile()].filter((filePath): filePath is string => Boolean(filePath));
      await createLocalPathTasks(filePaths);
      return;
    }

    localVideoInputRef.current?.click();
  }

  async function handleWebLocalVideoPicked(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) {
      return;
    }

    await handleLocalFilesSelected(files);
  }

  async function handleLocalFilesSelected(files: File[]) {
    if (!files.length) {
      return;
    }
    if (configHealth.hasBlockingIssues) {
      setSubmitStatus(`当前缺少必需配置：${configHealth.blockingIssues.map((item) => item.title).join("、")}。请先完成设置。`);
      if (shouldShowSetupAssistant(configHealth, snapshot.settings)) {
        setSetupAssistantDismissed(false);
        setSetupAssistantOpen(true);
      } else {
        navigate("/settings");
      }
      return;
    }

    setSubmitStatus(
      files.length > 1 ? `正在上传 ${files.length} 个本地媒体文件并创建任务...` : "正在上传本地媒体并准备开始总结...",
    );
    try {
      const responses = files.length > 1
        ? await api.uploadBatchVideos(files)
        : [await api.uploadLocalVideo(files[0])];
      const firstResponse = responses[0];
      if (!firstResponse) {
        throw new Error("未返回上传结果。");
      }
      setProbePreview(firstResponse.video);
      setProbeUrl("");
      setSubmitStatus(
        responses.length > 1
          ? `已上传 ${responses.length} 个本地媒体文件，正在后台创建摘要任务...`
          : firstResponse.cached ? "已从本地媒体库读取，正在进入详情页..." : "本地媒体已加入视频库，正在进入详情页...",
      );
      setRefreshSeed((value) => value + 1);
      navigate(`/videos/${firstResponse.video.video_id}`);
      void Promise.all(
        responses.map((response) => createVideoTaskAfterNavigation(response.video, response.video.title || response.video.source_url)),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入本地媒体失败";
      if (isBilibiliCookieHelpError(message)) {
        showBilibiliCookieHelp(message);
        return;
      }
      setSubmitStatus(message);
    }
  }

  async function handleConfirmMultiPage(input: { pageNumbers: number[]; confirm: boolean }) {
    if (!multiPageProbeVideo) {
      throw new Error("当前视频信息已失效，请重新探测。");
    }
    const currentProbeVideo = multiPageProbeVideo;
    setSubmitStatus(input.confirm ? "正在确认批量任务..." : "正在进入详情页并准备批量任务...");
    setProbePreview(currentProbeVideo);
    setProbeUrl("");
    setRefreshSeed((value) => value + 1);
    navigate(`/videos/${currentProbeVideo.video_id}`);

    const promptPresetId = await resolveTaskPromptPresetId(currentProbeVideo.title || currentProbeVideo.source_url);
    const response = await api.createVideoTasksBatch(currentProbeVideo.video_id, {
      page_numbers: input.pageNumbers,
      confirm: input.confirm,
      prompt_preset_id: promptPresetId,
    });
    if (response.requires_confirmation) {
      setSubmitStatus(`所选内容中有 ${response.conflict_pages.length} 个分 P 已有成功摘要，请确认后继续。`);
      return response;
    }
    setMultiPageDialogOpen(false);
    setMultiPageProbeVideo(null);
    setMultiPageOptions([]);
    const createdCount = response.created_tasks.length;
    const skippedCount = response.skipped_pages.length;
    setSubmitStatus(
      createdCount > 0
        ? `已创建 ${createdCount} 个批量任务${skippedCount ? `，跳过 ${skippedCount} 个已完成分 P` : ""}`
        : `没有创建新任务${skippedCount ? `，已跳过 ${skippedCount} 个分 P` : ""}`,
    );
    setRefreshSeed((value) => value + 1);
    setDetailRefreshToken((value) => value + 1);
    return response;
  }

  function handleCloseMultiPageDialog() {
    setMultiPageDialogOpen(false);
    setMultiPageProbeVideo(null);
    setMultiPageOptions([]);
  }

  const pageMeta = location.pathname.startsWith("/settings")
    ? { eyebrow: "设置中心", title: "运行配置、环境检测与日志", description: "围绕本地推理环境、模型配置与桌面端服务控制，统一管理 BiliSum 的运行能力。" }
    : location.pathname.startsWith("/library")
      ? { eyebrow: "视频库", title: "视频库", description: "集中管理已抓取的视频、摘要结果与当前处理状态。" }
      : location.pathname.startsWith("/videos/")
        ? { eyebrow: "视频详情", title: "本地摘要结果与任务记录", description: "围绕单个视频集中查看摘要、时间轴、转写全文与任务处理进度。" }
        : { eyebrow: "BiliSum Workspace", title: "懒得看视频？一键省流！", description: "把时间留给真正有质量的视频" };

  const isSettingsRoute = location.pathname.startsWith("/settings");
  const isLibraryRoute = location.pathname.startsWith("/library");
  const canCheckUpdates = true;
  const canInstallUpdates = Boolean(window.desktop?.update) && !isUpdateUnsupported(updateState);
  const canImportLocalVideo = Boolean(window.desktop?.media) || typeof window !== "undefined";

  if (!window.desktop && authChecked && !authenticated) {
    return (
      <AuthGate
        token={authToken}
        error={authError}
        submitting={authSubmitting}
        onTokenChange={setAuthToken}
        onSubmit={handleAuthSubmit}
      />
    );
  }

  if (!window.desktop && !authChecked) {
    return (
      <div className="auth-shell">
        <section className="auth-panel">
          <div className="auth-brand">
            <img src="/static/assets/icons/icon.svg" alt="" />
            <div>
              <span>BiliSum</span>
              <h1>正在检查访问权限</h1>
            </div>
          </div>
          <p className="auth-muted">请稍候。</p>
        </section>
      </div>
    );
  }

  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${mobileSidebarOpen ? "mobile-sidebar-open" : ""}`}>
      <input
        ref={localVideoInputRef}
        type="file"
        multiple
        accept="video/mp4,video/quicktime,video/x-matroska,video/x-msvideo,video/x-ms-wmv,video/webm,video/x-flv,video/mp2t,video/mpeg,audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/flac,audio/ogg,.mp4,.mov,.mkv,.avi,.wmv,.webm,.flv,.m4v,.ts,.mpeg,.mpg,.mp3,.wav,.m4a,.aac,.flac,.ogg"
        style={{ display: "none" }}
        onChange={(event) => void handleWebLocalVideoPicked(event)}
      />
      <TitleBar
        darkMode={darkMode}
        onToggleTheme={() => setDarkMode((current) => !current)}
        serviceOnline={snapshot.serviceOnline}
        backendRunning={desktop.backend?.running}
        runtimeDeviceLabel={runtimeDeviceLabel}
        runtimeReady={runtimeReady}
        runtimeStatusText={runtimeStatusText}
        version={runtimeVersionLabel}
        updateState={updateState}
        configHealth={configHealth}
        onOpenSettings={openConfigAssist}
        onOpenUpdateDialog={openUpdateDialog}
      />
      <aside className="sidebar">
        <nav className="nav">
          <Link className={`nav-item ${location.pathname === "/" ? "active" : ""}`} to="/" aria-label="首页" title="首页" onClick={() => setMobileSidebarOpen(false)}>
            <span className="nav-icon" aria-hidden="true"><HomeIcon /></span>
            <span className="nav-copy">
              <strong>首页</strong>
              <small>工作台总览</small>
            </span>
          </Link>
          <Link className={`nav-item ${location.pathname === "/library" ? "active" : ""}`} to="/library" aria-label="视频库" title="视频库" onClick={() => setMobileSidebarOpen(false)}>
            <span className="nav-icon" aria-hidden="true"><LibraryIcon /></span>
            <span className="nav-copy">
              <strong>视频库</strong>
              <small>资产管理</small>
            </span>
          </Link>
          <Link className={`nav-item ${location.pathname === "/knowledge" ? "active" : ""}`} to="/knowledge" aria-label="知识库" title="知识库" onClick={() => setMobileSidebarOpen(false)}>
            <span className="nav-icon" aria-hidden="true"><KnowledgeIcon /></span>
            <span className="nav-copy">
              <strong>知识库</strong>
              <small>检索与问答</small>
            </span>
          </Link>
          <Link className={`nav-item ${location.pathname.startsWith("/settings") ? "active" : ""}`} to="/settings" aria-label="设置" title="设置" onClick={() => setMobileSidebarOpen(false)}>
            <span className="nav-icon" aria-hidden="true"><SettingsIcon /></span>
            <span className="nav-copy">
              <strong>设置</strong>
              <small>运行配置</small>
            </span>
          </Link>
        </nav>

        <div className="nav-section-divider"></div>
        <button
          className="sidebar-corner-toggle"
          type="button"
          onClick={() => setSidebarCollapsed((current) => !current)}
          aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
          aria-pressed={sidebarCollapsed}
          title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          <IconSidebarToggle collapsed={sidebarCollapsed} />
        </button>
      </aside>

      {/* 移动端菜单按钮 - 仅在竖屏显示 */}
      <button
        className="mobile-menu-toggle"
        type="button"
        onClick={() => setMobileSidebarOpen(true)}
        aria-label="打开菜单"
        title="打开菜单"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>

      {/* 移动端遮罩层 - 点击关闭 sidebar */}
      {mobileSidebarOpen && (
        <div
          className="mobile-sidebar-overlay"
          onClick={() => setMobileSidebarOpen(false)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setMobileSidebarOpen(false);
            }
          }}
          role="button"
          tabIndex={0}
          aria-label="关闭菜单"
        />
      )}

      <main className="content">
        <div className={`content-frame ${isSettingsRoute ? "content-frame-settings" : ""}`}>
          {snapshot.error && !snapshot.serviceOnline ? (
            <section className="grid-card empty-state-card">
              <div className="spinner"></div>
              <h3>后端暂未就绪</h3>
              <p>{snapshot.error}</p>
              <div className="desktop-actions">
                <button
                  className="primary-button"
                  type="button"
                  onClick={async () => {
                    await window.desktop?.backend.start();
                    setRefreshSeed((value) => value + 1);
                  }}
                >
                  重新拉起后端
                </button>
                <button className="secondary-button" type="button" onClick={() => setRefreshSeed((value) => value + 1)}>重新检测</button>
              </div>
            </section>
          ) : (
            <Routes>
              <Route
                path="/"
                element={(
                  <HomePage
                    configHealth={configHealth}
                    probePreview={probePreview}
                    probeUrl={probeUrl}
                    setProbeUrl={setProbeUrl}
                    submitStatus={submitStatus}
                    onProbe={handleProbe}
                    onImportLocalVideo={handleImportLocalVideo}
                    onImportLocalFiles={handleLocalFilesSelected}
                    onImportLocalPaths={createLocalPathTasks}
                    canImportLocalVideo={canImportLocalVideo}
                    promptRouterMode={snapshot.settings?.prompt_router_mode || "confirm"}
                    onPromptRouterModeChange={updatePromptRouterMode}
                    onOpenSetupAssistant={openConfigAssist}
                    onOpenConfigIssue={navigateToConfigIssue}
                    onEditPromptPreset={navigateToPromptPreset}
                    favoriteVideos={favoriteVideos}
                    recentVideos={recentVideos}
                    onToggleFavorite={handleToggleFavorite}
                  />
                )}
              />
              <Route
                path="/library"
                element={(
                  <LibraryPage
                    snapshot={snapshot}
                    filteredVideos={filteredVideos}
                    libraryCounts={libraryCounts}
                    latestVideo={latestVideo}
                    query={query}
                    setQuery={setQuery}
                    activeFilter={libraryFilter}
                    setLibraryFilter={setLibraryFilter}
                    serviceOnline={snapshot.serviceOnline}
                    runtimeDeviceLabel={runtimeDeviceLabel}
                    onToggleFavorite={handleToggleFavorite}
                    onCreateFolder={handleCreateFolder}
                    onUpdateFolder={handleUpdateFolder}
                    onDeleteFolder={handleDeleteFolder}
                    onDeleteVideo={handleDeleteVideo}
                    onMoveVideo={handleMoveVideo}
                    onSetVideoPin={handleSetVideoPin}
                    onReorderVideos={handleReorderVideos}
                    onUpdateLibraryPreferences={handleUpdateLibraryPreferences}
                  />
                )}
              />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route
                path="/videos/:videoId"
                element={(
                  <VideoDetailPage
                    refreshToken={detailRefreshToken}
                    onRefresh={() => setRefreshSeed((value) => value + 1)}
                    onOpenCookieSettings={openYtdlpCookieSettings}
                    onOpenCookieTutorial={openCookieExportTutorial}
                    onCaptureLoginCookies={captureBilibiliLoginCookies}
                  />
                )}
              />
              <Route
                path="/settings"
                element={(
                  <SettingsPage
                    desktop={desktop}
                    onOpenUpdateDialog={openUpdateDialog}
                    onCheckUpdate={handleCheckForUpdates}
                    onDownloadUpdate={handleDownloadUpdate}
                    onInstallUpdate={handleInstallUpdate}
                    onOpenSetupAssistant={() => {
                      setupAssistantForceRef.current = true;
                      setSetupAssistantDismissed(false);
                      setSetupAssistantOpen(true);
                      navigate("/");
                    }}
                    onRefresh={() => setRefreshSeed((value) => value + 1)}
                    onSettingsSaved={(settings, environment) => {
                      setSnapshot((current) => ({
                        ...current,
                        settings,
                        environment: environment ?? current.environment,
                        systemInfo: current.systemInfo
                          ? {
                              ...current.systemInfo,
                              settings,
                              environment: environment ?? current.systemInfo.environment,
                            }
                          : current.systemInfo,
                      }));
                    }}
                    snapshot={snapshot}
                    focusIssueRequest={settingsFocusRequest}
                    promptPresetRequest={settingsPromptRequest}
                    updateInfo={updateState}
                    canCheckUpdate={canCheckUpdates}
                    canInstallUpdate={canInstallUpdates}
                  />
                )}
              />
            </Routes>
          )}
        </div>
      </main>

      <UpdateDialog
        isOpen={updateDialogOpen}
        updateInfo={updateState as UpdateInfo}
        currentVersion={runtimeVersionLabel}
        canInstallUpdate={canInstallUpdates}
        onClose={closeUpdateDialog}
        onCheck={handleCheckForUpdates}
        onDownload={handleDownloadUpdate}
        onInstall={handleInstallUpdate}
      />
      <StartupAnnouncementDialog
        announcement={startupAnnouncement}
        onClose={() => void closeStartupAnnouncement()}
      />
      <MultiPageSelectDialog
        isOpen={multiPageDialogOpen}
        mode="create"
        video={multiPageProbeVideo}
        pages={multiPageOptions}
        onClose={handleCloseMultiPageDialog}
        onSubmit={handleConfirmMultiPage}
      />
      <SetupAssistantDialog
        isOpen={setupAssistantOpen}
        force={setupAssistantForceRef.current}
        configHealth={configHealth}
        onClose={closeSetupAssistant}
        onOpenSettings={openSettingsFromAssistant}
        onNavigateToIssue={navigateToConfigIssue}
      />
      <CookieHelpDialog
        isOpen={cookieHelpDialogOpen}
        onClose={() => setCookieHelpDialogOpen(false)}
        onOpenTutorial={openCookieExportTutorial}
        onOpenSettings={openYtdlpCookieSettings}
        onCaptureLoginCookies={captureBilibiliLoginCookies}
      />
      {homeTourOpen && location.pathname === "/" && !configHealth.hasBlockingIssues ? (
        <HomeTour onClose={() => setHomeTourOpen(false)} />
      ) : null}
    </div>
  );
}

function IconSidebarToggle({ collapsed }: { collapsed: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <path d="M9 4.5v15" />
      {collapsed ? <path d="m13.5 12 3-3m-3 3 3 3" /> : <path d="m15.5 12-3-3m3 3-3 3" />}
    </svg>
  );
}

function AuthGate({
  token,
  error,
  submitting,
  onTokenChange,
  onSubmit,
}: {
  token: string;
  error: string;
  submitting: boolean;
  onTokenChange(value: string): void;
  onSubmit(event: FormEvent<HTMLFormElement>): void;
}) {
  return (
    <div className="auth-shell">
      <section className="auth-panel">
        <div className="auth-brand">
          <img src="/static/assets/icons/icon.svg" alt="" />
          <div>
            <span>BiliSum</span>
            <h1>输入访问密钥</h1>
          </div>
        </div>
        <p className="auth-muted">
          Web 端访问需要 BiliSum 服务密钥。桌面端会自动注入密钥；Docker 或 npx 部署可通过
          {" "}
          <code>VIDEO_SUM_ACCESS_TOKEN</code>
          {" "}
          配置。
        </p>
        <form className="auth-form" onSubmit={onSubmit}>
          <label htmlFor="access-token">访问密钥</label>
          <input
            id="access-token"
            type="password"
            value={token}
            autoComplete="current-password"
            autoFocus
            onChange={(event) => onTokenChange(event.target.value)}
          />
          {error ? <p className="auth-error">{error}</p> : null}
          <button type="submit" disabled={submitting}>
            {submitting ? "正在验证..." : "进入 BiliSum"}
          </button>
        </form>
      </section>
    </div>
  );
}
