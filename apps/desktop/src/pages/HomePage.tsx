import { useEffect, useRef, useState, type ClipboardEvent, type CSSProperties, type DragEvent, type FormEvent, type MouseEvent } from "react";

import { api } from "../api";
import type { ConfigHealth } from "../appModel";
import { platformLabel } from "../appModel";
import { LinkIcon, LocalVideoIcon } from "../components/AppIcons";
import { FloatingNoticeStack } from "../components/FloatingNoticeStack";
import { VideoCard } from "../components/VideoCard";
import type { PromptPreset, VideoAssetSummary } from "../types";
import { formatDuration } from "../utils";

type HomePageProps = {
  configHealth: ConfigHealth;
  probePreview: VideoAssetSummary | null;
  probeUrl: string;
  setProbeUrl(value: string): void;
  submitStatus: string;
  onProbe(event: FormEvent): Promise<void>;
  onImportLocalVideo(): Promise<void>;
  onImportLocalFiles(files: File[]): Promise<void>;
  onImportLocalPaths(paths: string[]): Promise<void>;
  canImportLocalVideo: boolean;
  promptRouterMode: string;
  onPromptRouterModeChange(mode: "auto" | "confirm"): Promise<void>;
  onOpenSetupAssistant(issueKey?: string): void;
  onOpenConfigIssue(issueKey: string): void;
  onEditPromptPreset(presetId: string): void;
  favoriteVideos: VideoAssetSummary[];
  recentVideos: VideoAssetSummary[];
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
};

type SummaryPreference = {
  noteMode: "text" | "visual";
  generateMindmap: boolean;
};

const SUMMARY_PREFERENCE_STORAGE_KEY = "bilisum.summaryPreference";
const PREFERENCE_HINT_SEEN_KEY = "bilisum.summaryPreferenceHintSeen";
const PROMPT_PRESET_STORAGE_KEY = "bilisum.promptPresetId";
const HIDDEN_PROMPT_PRESETS_STORAGE_KEY = "bilisum.hiddenPromptPresetIds";
const SUPPORTED_LOCAL_MEDIA_EXTENSIONS = new Set([
  ".mp4",
  ".mov",
  ".mkv",
  ".avi",
  ".wmv",
  ".webm",
  ".flv",
  ".m4v",
  ".ts",
  ".mpeg",
  ".mpg",
  ".mp3",
  ".wav",
  ".m4a",
  ".aac",
  ".flac",
  ".ogg",
]);
const DEFAULT_SUMMARY_PREFERENCE: SummaryPreference = {
  noteMode: "text",
  generateMindmap: false,
};

function loadSummaryPreference(): SummaryPreference {
  if (typeof window === "undefined") {
    return DEFAULT_SUMMARY_PREFERENCE;
  }
  try {
    const rawValue = window.localStorage.getItem(SUMMARY_PREFERENCE_STORAGE_KEY);
    if (!rawValue) {
      return DEFAULT_SUMMARY_PREFERENCE;
    }
    const parsed = JSON.parse(rawValue) as Partial<SummaryPreference>;
    return {
      noteMode: parsed.noteMode === "visual" ? "visual" : "text",
      generateMindmap: Boolean(parsed.generateMindmap),
    };
  } catch {
    return DEFAULT_SUMMARY_PREFERENCE;
  }
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
function loadPromptPresetId() {
  if (typeof window === "undefined") {
    return "general";
  }
  return window.localStorage.getItem(PROMPT_PRESET_STORAGE_KEY) || "general";
}

function isSupportedLocalMediaFile(file: File) {
  const lowerName = file.name.toLowerCase();
  return isSupportedLocalMediaPath(lowerName);
}

function isSupportedLocalMediaPath(path: string) {
  const lowerName = path.toLowerCase();
  const dotIndex = lowerName.lastIndexOf(".");
  if (dotIndex < 0) {
    return false;
  }
  return SUPPORTED_LOCAL_MEDIA_EXTENSIONS.has(lowerName.slice(dotIndex));
}

export function HomePage({
  configHealth,
  probePreview,
  probeUrl,
  setProbeUrl,
  submitStatus,
  onProbe,
  onImportLocalVideo,
  onImportLocalFiles,
  onImportLocalPaths,
  canImportLocalVideo,
  promptRouterMode,
  onPromptRouterModeChange,
  onOpenSetupAssistant,
  onOpenConfigIssue,
  onEditPromptPreset,
  favoriteVideos,
  recentVideos,
  onToggleFavorite,
}: HomePageProps) {
  const preferenceMenuRef = useRef<HTMLDivElement | null>(null);
  const promptMenuRef = useRef<HTMLDivElement | null>(null);
  const [summaryPreference, setSummaryPreference] = useState<SummaryPreference>(() => loadSummaryPreference());
  const [preferenceMenuOpen, setPreferenceMenuOpen] = useState(false);
  const [preferenceHintDismissed, setPreferenceHintDismissed] = useState(() => {
    if (typeof window !== "undefined") {
      return window.localStorage.getItem(PREFERENCE_HINT_SEEN_KEY) === "1";
    }
    return true;
  });
  const [promptPresets, setPromptPresets] = useState<PromptPreset[]>([]);
  const [hiddenPromptPresetIds, setHiddenPromptPresetIds] = useState<Set<string>>(() => loadHiddenPromptPresetIds());
  const [promptPresetId, setPromptPresetId] = useState(() => loadPromptPresetId());
  const [promptContextMenu, setPromptContextMenu] = useState<{ x: number; y: number; presetId: string } | null>(null);
  const [promptSelectOpen, setPromptSelectOpen] = useState(false);
  const [promptLoading, setPromptLoading] = useState(false);
  const [promptStatus, setPromptStatus] = useState("");
  const [promptModeSaving, setPromptModeSaving] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [batchUploading, setBatchUploading] = useState(false);

  useEffect(() => {
    window.localStorage.setItem(SUMMARY_PREFERENCE_STORAGE_KEY, JSON.stringify(summaryPreference));
  }, [summaryPreference]);

  useEffect(() => {
    window.localStorage.setItem(PROMPT_PRESET_STORAGE_KEY, promptPresetId);
  }, [promptPresetId]);

  useEffect(() => {
    let disposed = false;
    async function loadPromptPresets() {
      setPromptLoading(true);
      try {
        const presets = await api.listPromptPresets();
        if (disposed) {
          return;
        }
        setPromptPresets(presets);
      } catch (error) {
        if (!disposed) {
          setPromptStatus(error instanceof Error ? error.message : "Prompt 加载失败");
        }
      } finally {
        if (!disposed) {
          setPromptLoading(false);
        }
      }
    }
    void loadPromptPresets();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    function handlePromptVisibilityChanged() {
      setHiddenPromptPresetIds(loadHiddenPromptPresetIds());
    }
    window.addEventListener("bilisum:prompt-presets-visibility-changed", handlePromptVisibilityChanged);
    window.addEventListener("storage", handlePromptVisibilityChanged);
    return () => {
      window.removeEventListener("bilisum:prompt-presets-visibility-changed", handlePromptVisibilityChanged);
      window.removeEventListener("storage", handlePromptVisibilityChanged);
    };
  }, []);

  useEffect(() => {
    const visiblePresets = promptPresets.filter((preset) => !hiddenPromptPresetIds.has(preset.id));
    if (visiblePresets.length && (!promptPresetId || !visiblePresets.some((preset) => preset.id === promptPresetId))) {
      setPromptPresetId(visiblePresets[0]?.id || "general");
    }
  }, [hiddenPromptPresetIds, promptPresetId, promptPresets]);

  const showPreferenceHint = preferenceMenuOpen && !preferenceHintDismissed;

  function dismissPreferenceHint() {
    setPreferenceHintDismissed(true);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(PREFERENCE_HINT_SEEN_KEY, "1");
    }
  }

  useEffect(() => {
    if (!preferenceMenuOpen) {
      setPromptSelectOpen(false);
      return;
    }

    function handlePointerDown(event: globalThis.MouseEvent) {
      const target = event.target as Node;
      if (preferenceMenuRef.current?.contains(target)) {
        if (!(target instanceof Element) || !target.closest(".home-prompt-select-shell")) {
          setPromptSelectOpen(false);
        }
        return;
      }
      if (promptMenuRef.current?.contains(target)) {
        return;
      }
      setPreferenceMenuOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPromptSelectOpen(false);
        setPreferenceMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [preferenceMenuOpen]);

  useEffect(() => {
    if (!promptContextMenu) {
      return;
    }

    function handlePointerDown(event: globalThis.MouseEvent) {
      if (promptMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      setPromptContextMenu(null);
    }

    function closePromptContextMenu() {
      setPromptContextMenu(null);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPromptContextMenu(null);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("scroll", closePromptContextMenu, true);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("scroll", closePromptContextMenu, true);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [promptContextMenu]);

  function updateSummaryPreference(nextPreference: SummaryPreference) {
    setSummaryPreference(nextPreference);
  }

  function updatePromptPreset(nextPresetId: string) {
    setPromptPresetId(nextPresetId || "general");
    setPromptStatus("");
    setPromptContextMenu(null);
    setPromptSelectOpen(false);
  }

  function openPromptContextMenu(event: MouseEvent<HTMLElement>) {
    if (!selectedPrompt) {
      return;
    }
    event.preventDefault();
    setPromptContextMenu({ x: event.clientX, y: event.clientY, presetId: selectedPrompt.id });
  }

  function editSelectedPromptPreset() {
    if (!promptContextMenu?.presetId) {
      return;
    }
    onEditPromptPreset(promptContextMenu.presetId);
    setPromptContextMenu(null);
  }

  async function changePromptRouterMode(mode: "auto" | "confirm") {
    if (mode === promptRouterMode || promptModeSaving) {
      return;
    }
    setPromptModeSaving(true);
    setPromptStatus("");
    try {
      await onPromptRouterModeChange(mode);
    } catch (error) {
      setPromptStatus(error instanceof Error ? error.message : "Prompt 模式保存失败");
    } finally {
      setPromptModeSaving(false);
    }
  }

  function collectMediaFiles(files: Iterable<File>) {
    return Array.from(files).filter(isSupportedLocalMediaFile);
  }

  function openBatchConfirm(files: File[]) {
    if (!files.length) {
      setPromptStatus("未识别到可导入的媒体文件");
      return;
    }
    setPendingFiles(files);
    setPromptStatus("");
  }

  function resolveDesktopFilePaths(files: File[]) {
    const filePaths = window.desktop?.media?.getFilePaths?.(files) || [];
    return filePaths.filter(isSupportedLocalMediaPath);
  }

  function importTransferredFiles(files: Iterable<File>) {
    const mediaFiles = collectMediaFiles(files);
    if (!mediaFiles.length) {
      setPromptStatus("未识别到可导入的媒体文件");
      return;
    }
    const desktopFilePaths = resolveDesktopFilePaths(mediaFiles);
    if (desktopFilePaths.length) {
      setPromptStatus("");
      void onImportLocalPaths(desktopFilePaths);
      return;
    }
    openBatchConfirm(mediaFiles);
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragActive(false);
    if (!canImportLocalVideo) {
      return;
    }
    if (!event.dataTransfer.files.length) {
      return;
    }
    importTransferredFiles(event.dataTransfer.files);
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    event.stopPropagation();
    if (canImportLocalVideo) {
      event.dataTransfer.dropEffect = "copy";
      setIsDragActive(true);
    }
  }

  function handleDragLeave(event: DragEvent<HTMLElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setIsDragActive(false);
    }
  }

  function handlePaste(event: ClipboardEvent<HTMLElement>) {
    if (!canImportLocalVideo) {
      return;
    }
    if (!event.clipboardData.files.length) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    importTransferredFiles(event.clipboardData.files);
  }

  useEffect(() => {
    if (!canImportLocalVideo) {
      return;
    }

    function handleDesktopFileDrop(event: Event) {
      const paths = (event as CustomEvent<string[]>).detail || [];
      const mediaPaths = paths.filter(isSupportedLocalMediaPath);
      setIsDragActive(false);
      if (!mediaPaths.length) {
        setPromptStatus(`未识别到可导入的媒体文件，收到 ${paths.length} 个文件路径`);
        return;
      }
      setPromptStatus(`已接收 ${mediaPaths.length} 个本地媒体文件，正在导入...`);
      void onImportLocalPaths(mediaPaths);
    }

    function preventWindowFileNavigation(event: globalThis.DragEvent) {
      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
      setIsDragActive(true);
    }

    function handleWindowDrop(event: globalThis.DragEvent) {
      event.preventDefault();
      setIsDragActive(false);
    }

    function handleWindowDragLeave(event: globalThis.DragEvent) {
      if (event.relatedTarget === null) {
        setIsDragActive(false);
      }
    }

    window.addEventListener("dragenter", preventWindowFileNavigation, true);
    window.addEventListener("dragover", preventWindowFileNavigation, true);
    window.addEventListener("drop", handleWindowDrop, true);
    window.addEventListener("dragleave", handleWindowDragLeave, true);
    window.addEventListener("bilisum:desktop-file-drop", handleDesktopFileDrop);
    return () => {
      window.removeEventListener("dragenter", preventWindowFileNavigation, true);
      window.removeEventListener("dragover", preventWindowFileNavigation, true);
      window.removeEventListener("drop", handleWindowDrop, true);
      window.removeEventListener("dragleave", handleWindowDragLeave, true);
      window.removeEventListener("bilisum:desktop-file-drop", handleDesktopFileDrop);
    };
  }, [canImportLocalVideo, onImportLocalPaths]);

  async function confirmBatchImport() {
    if (!pendingFiles.length) {
      return;
    }
    setBatchUploading(true);
    try {
      await onImportLocalFiles(pendingFiles);
      setPendingFiles([]);
    } finally {
      setBatchUploading(false);
    }
  }

  const selectablePromptPresets = promptPresets.filter((preset) => !hiddenPromptPresetIds.has(preset.id));
  const selectedPrompt = selectablePromptPresets.find((preset) => preset.id === promptPresetId) || selectablePromptPresets[0] || null;
  const promptModeLabel = promptRouterMode === "auto" ? "AI 识别场景" : "手动选择";

  return (
    <section className="home-page">
      <FloatingNoticeStack notices={[{ id: "home-submit-status", message: submitStatus }]} />
      <div className="section">
        <h2 className="section-title">开始总结</h2>
        <form className="task-form" onSubmit={onProbe} onPaste={handlePaste}>
          <div className="task-form-row">
            <label className="input-row input-row-hero" style={{ flex: 1 }}>
              <div
                className={`input-with-icon local-media-input ${isDragActive ? "is-drag-active" : ""}`.trim()}
                style={{ flex: 1 }}
                data-home-tour="input"
                onDragEnterCapture={handleDragOver}
                onDragOverCapture={handleDragOver}
                onDragLeave={handleDragLeave}
                onDropCapture={handleDrop}
                onPaste={handlePaste}
              >
                <span className="input-icon" aria-hidden="true"><LinkIcon /></span>
                <input
                  className={`input-field input-field-hero ${canImportLocalVideo ? "input-field-with-action" : ""}`.trim()}
                  type="text"
                  value={probeUrl}
                  onChange={(event) => setProbeUrl(event.target.value)}
                  placeholder="粘贴 Bilibili / YouTube 链接、BV 号，或粘贴/拖入本地媒体文件"
                  required
                />
                {canImportLocalVideo ? (
                  <button
                    className="input-inline-action"
                    type="button"
                    aria-label="导入本地视频"
                    title="导入本地视频"
                    data-home-tour="local-video"
                    onClick={() => void onImportLocalVideo()}
                  >
                    <span className="input-inline-action-label">导入视频</span>
                    <LocalVideoIcon />
                  </button>
                ) : null}
              </div>
            </label>
            <div className="summary-submit-control" ref={preferenceMenuRef} data-home-tour="submit">
              <div className="summary-split-button">
                <button className="primary-button primary-button-hero summary-submit-main" type="submit">
                  开始总结
                </button>
                <button
                  className="summary-submit-menu-button"
                  type="button"
                  aria-label="打开生成偏好"
                  aria-expanded={preferenceMenuOpen}
                  aria-controls="summary-preference-menu"
                  title="生成偏好"
                  onClick={() => setPreferenceMenuOpen((open) => !open)}
                >
                  <IconChevronDown />
                </button>
              </div>
              {preferenceMenuOpen ? (
                <div className="summary-preference-menu" id="summary-preference-menu" role="menu">
                  {showPreferenceHint ? (
                    <div className="summary-preference-hint">
                      <p>本次生成的笔记形式，切换到"图文笔记"后会为本次任务生成带截图的图文笔记。</p>
                      <button
                        className="summary-preference-hint-close"
                        type="button"
                        aria-label="知道了"
                        onClick={dismissPreferenceHint}
                      >
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                          <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        </svg>
                      </button>
                    </div>
                  ) : null}
                  <div className="summary-preference-group">
                    <span className="summary-preference-label">笔记形式</span>
                    <label className={`summary-preference-option ${summaryPreference.noteMode === "text" ? "is-selected" : ""}`}>
                      <input
                        type="radio"
                        name="summary-note-mode"
                        checked={summaryPreference.noteMode === "text"}
                        onChange={() => { updateSummaryPreference({ ...summaryPreference, noteMode: "text" }); dismissPreferenceHint(); }}
                      />
                      <span>文字笔记</span>
                    </label>
                    <label className={`summary-preference-option ${summaryPreference.noteMode === "visual" ? "is-selected" : ""}`}>
                      <input
                        type="radio"
                        name="summary-note-mode"
                        checked={summaryPreference.noteMode === "visual"}
                        onChange={() => { updateSummaryPreference({ ...summaryPreference, noteMode: "visual" }); dismissPreferenceHint(); }}
                      />
                      <span>图文笔记</span>
                    </label>
                  </div>
                  <div className="summary-preference-divider" />
                  <label className={`summary-preference-option summary-preference-toggle ${summaryPreference.generateMindmap ? "is-selected" : ""}`}>
                    <input
                      type="checkbox"
                      checked={summaryPreference.generateMindmap}
                      onChange={(event) => { updateSummaryPreference({ ...summaryPreference, generateMindmap: event.target.checked }); dismissPreferenceHint(); }}
                    />
                    <span>生成导图</span>
                  </label>
                  <div className="summary-preference-divider" />
                  <div className="summary-preference-group summary-prompt-group" onContextMenu={openPromptContextMenu}>
                    <div className="summary-preference-label-row">
                      <span className="summary-preference-label">摘要 Prompt</span>
                      <span className="helper-chip">模式 {promptModeLabel}</span>
                    </div>
                    <div className="home-prompt-mode" role="group" aria-label="Prompt 模式选择">
                      <button
                        className={`home-prompt-mode-option ${promptRouterMode !== "auto" ? "is-active" : ""}`}
                        type="button"
                        disabled={promptModeSaving}
                        onClick={() => void changePromptRouterMode("confirm")}
                      >
                        手动选择
                      </button>
                      <button
                        className={`home-prompt-mode-option ${promptRouterMode === "auto" ? "is-active" : ""}`}
                        type="button"
                        disabled={promptModeSaving}
                        onClick={() => void changePromptRouterMode("auto")}
                      >
                        AI 识别场景
                      </button>
                    </div>
                    <div className="home-prompt-select-shell">
                      <button
                        className="select-field home-prompt-select"
                        type="button"
                        role="combobox"
                        aria-label="选择摘要 Prompt"
                        aria-expanded={promptSelectOpen}
                        aria-controls="summary-prompt-options"
                        disabled={promptRouterMode === "auto" || promptLoading || !selectablePromptPresets.length}
                        onClick={() => setPromptSelectOpen((open) => !open)}
                      >
                        <span>{selectedPrompt?.name || "暂无可用 Prompt"}</span>
                        <IconChevronDown />
                      </button>
                      {promptSelectOpen && selectablePromptPresets.length ? (
                        <div className="home-prompt-options" id="summary-prompt-options" role="listbox" aria-label="摘要 Prompt 选项">
                          {selectablePromptPresets.map((preset) => (
                            <button
                              key={preset.id}
                              className={`home-prompt-option ${preset.id === selectedPrompt?.id ? "is-selected" : ""}`}
                              type="button"
                              role="option"
                              aria-selected={preset.id === selectedPrompt?.id}
                              onClick={() => updatePromptPreset(preset.id)}
                            >
                              <span>{preset.name}</span>
                              {preset.id === selectedPrompt?.id ? <span aria-hidden="true">✓</span> : null}
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    {selectedPrompt?.description ? (
                      <span className="home-prompt-description">{selectedPrompt.description}</span>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        </form>
        {promptContextMenu ? (
          <div
            className="home-prompt-context-menu"
            ref={promptMenuRef}
            role="menu"
            style={{ left: promptContextMenu.x, top: promptContextMenu.y }}
          >
            <button type="button" role="menuitem" onClick={editSelectedPromptPreset}>
              编辑
            </button>
          </div>
        ) : null}
        {pendingFiles.length ? (
          <div className="batch-import-panel" role="dialog" aria-modal="false" aria-label="确认批量导入">
            <div className="batch-import-head">
              <strong>确认导入 {pendingFiles.length} 个文件</strong>
              <button className="close-button" type="button" aria-label="取消批量导入" onClick={() => setPendingFiles([])}>
                ×
              </button>
            </div>
            <div className="batch-import-list">
              {pendingFiles.map((file) => (
                <span key={`${file.name}-${file.size}`}>{file.name}</span>
              ))}
            </div>
            <div className="batch-import-actions">
              <button className="secondary-button" type="button" disabled={batchUploading} onClick={() => setPendingFiles([])}>
                取消
              </button>
              <button className="primary-button" type="button" disabled={batchUploading} onClick={() => void confirmBatchImport()}>
                {batchUploading ? "导入中..." : "开始导入"}
              </button>
            </div>
          </div>
        ) : null}
        {promptStatus ? <div className="detail-error-banner" role="status">{promptStatus}</div> : null}

        {configHealth.checked && !configHealth.isConfigured ? (
          <article className={`config-alert-card tone-${configHealth.state}`}>
            <div className="config-alert-copy">
              <span className="section-kicker">运行配置提醒</span>
              <strong>{configHealth.hasBlockingIssues ? "当前缺少开始总结所需配置" : "当前有增强配置待补全"}</strong>
              <p>{configHealth.summary}</p>
              <div className="config-alert-list">
                {configHealth.issues.map((issue) => (
                  <button className="config-alert-item" type="button" key={issue.key} onClick={() => onOpenConfigIssue(issue.key)}>
                    <strong>{issue.title}</strong>
                    <span>{issue.description}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="config-alert-actions">
              <button
                className={configHealth.hasBlockingIssues ? "primary-button danger-button" : "secondary-button"}
                type="button"
                onClick={() => onOpenSetupAssistant(configHealth.blockingIssues[0]?.key || configHealth.issues[0]?.key)}
              >
                {configHealth.actionText}
              </button>
            </div>
          </article>
        ) : null}

        {probePreview && (
          <article className="probe-preview">
            <img src={probePreview.cover_url} alt={probePreview.title} />
            <div className="probe-preview-copy">
              <span className="section-kicker">即将加入视频库</span>
              <strong>{probePreview.title}</strong>
              <small>{formatDuration(probePreview.duration)} · {platformLabel(probePreview.platform)}</small>
            </div>
          </article>
        )}
      </div>

      {favoriteVideos.length > 0 ? (
        <div className="section">
          <VideoSection
            title="收藏视频"
            videos={favoriteVideos}
            onToggleFavorite={onToggleFavorite}
          />
        </div>
      ) : null}

      <div className="section">
        {recentVideos.length > 0 ? (
          <VideoSection
            title="最近视频"
            videos={recentVideos}
            onToggleFavorite={onToggleFavorite}
          />
        ) : (
          <div className="empty-placeholder">
            还没有视频，先输入一个链接开始总结吧。
          </div>
        )}
      </div>
    </section>
  );
}

function IconChevronDown() {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.9" viewBox="0 0 24 24" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function VideoSection({
  title,
  videos,
  onToggleFavorite,
}: {
  title: string;
  videos: VideoAssetSummary[];
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(videos.length > 1);
  const [isScrollable, setIsScrollable] = useState(false);
  const [cardsPerView, setCardsPerView] = useState(() => getCardsPerView(typeof window === "undefined" ? 1180 : window.innerWidth));
  const [cardWidth, setCardWidth] = useState(0);
  const pageStep = Math.max(1, cardsPerView * (cardWidth + 16));
  const totalPages = Math.max(1, Math.ceil(videos.length / Math.max(1, cardsPerView)));
  const currentPage = isScrollable && cardWidth > 0
    ? Math.min(totalPages, Math.max(1, Math.round((viewportRef.current?.scrollLeft ?? 0) / pageStep) + 1))
    : 1;

  function getCardsPerView(width: number) {
    if (width >= 1240) {
      return 4;
    }
    if (width >= 820) {
      return 3;
    }
    if (width >= 560) {
      return 2;
    }
    return 1;
  }

  function updateLayout() {
    const viewport = viewportRef.current;
    if (!viewport) {
      setCardsPerView(getCardsPerView(typeof window === "undefined" ? 1180 : window.innerWidth));
      setIsScrollable(videos.length > 1);
      setCanScrollLeft(false);
      setCanScrollRight(videos.length > 1);
      return;
    }

    const nextCardsPerView = getCardsPerView(viewport.clientWidth);
    const gap = 16;
    setCardsPerView(nextCardsPerView);
    setCardWidth(Math.max(176, (viewport.clientWidth - gap * (nextCardsPerView - 1)) / nextCardsPerView));

    const maxScrollLeft = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    const nextScrollable = maxScrollLeft > 8;
    setIsScrollable(nextScrollable);
    setCanScrollLeft(viewport.scrollLeft > 8);
    setCanScrollRight(nextScrollable && viewport.scrollLeft < maxScrollLeft - 8);
  }

  useEffect(() => {
    updateLayout();
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }

    const handleScroll = () => updateLayout();
    const handleResize = () => updateLayout();
    const resizeObserver = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => updateLayout())
      : null;

    viewport.addEventListener("scroll", handleScroll, { passive: true });
    window.addEventListener("resize", handleResize);
    resizeObserver?.observe(viewport);

    return () => {
      viewport.removeEventListener("scroll", handleScroll);
      window.removeEventListener("resize", handleResize);
      resizeObserver?.disconnect();
    };
  }, [videos.length]);

  function scrollByPage(direction: "left" | "right") {
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }

    const firstCard = viewport.querySelector<HTMLElement>(".home-carousel-item");
    const cardWidth = firstCard?.offsetWidth ?? viewport.clientWidth;
    const gap = 16;
    const nextOffset = (cardWidth + gap) * cardsPerView * (direction === "left" ? -1 : 1);

    viewport.scrollBy({
      left: nextOffset,
      behavior: "smooth",
    });
  }

  return (
    <>
      <div className="home-carousel-head">
        <h2 className="section-title home-carousel-title">{title}</h2>
        <div className="home-carousel-controls">
          <span className="helper-chip">{videos.length} 个视频</span>
        </div>
      </div>

      {videos.length > 0 ? (
        <div className="home-carousel-shell">
          <div className="home-carousel-stage" ref={viewportRef}>
            <div className="home-carousel-track">
              {videos.map((video) => (
                <div
                  className="home-carousel-item"
                  key={video.video_id}
                  style={cardWidth > 0 ? ({ width: `${cardWidth}px`, flexBasis: `${cardWidth}px` } as CSSProperties) : undefined}
                >
                  <VideoCard
                    video={video}
                    onToggleFavorite={onToggleFavorite}
                  />
                </div>
              ))}
            </div>
          </div>
          {isScrollable ? (
            <div className="home-carousel-footer">
              <div className="home-carousel-nav-row">
                <span className="home-carousel-page-indicator">
                  {currentPage} / {totalPages}
                </span>
                <div className="home-carousel-nav-group">
                  <button
                    className="home-carousel-button home-carousel-button-left"
                    type="button"
                    onClick={() => scrollByPage("left")}
                    disabled={!canScrollLeft}
                    aria-label={`${title}上一页`}
                  >
                    <IconChevron direction="left" />
                  </button>
                  <button
                    className="home-carousel-button home-carousel-button-right"
                    type="button"
                    onClick={() => scrollByPage("right")}
                    disabled={!canScrollRight}
                    aria-label={`${title}下一页`}
                  >
                    <IconChevron direction="right" />
                  </button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function IconChevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" viewBox="0 0 24 24" aria-hidden="true">
      {direction === "left" ? <path d="m15 18-6-6 6-6" /> : <path d="m9 6 6 6-6 6" />}
    </svg>
  );
}
