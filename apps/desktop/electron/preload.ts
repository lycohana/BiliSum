import { contextBridge, ipcRenderer, webUtils } from "electron";

const fileDropListeners = new Set<(paths: string[]) => void>();

function getFilePaths(files: File[] | FileList | null | undefined) {
  return Array.from(files || [])
    .map((file) => webUtils.getPathForFile(file))
    .filter(Boolean);
}

function allowFileDrop(event: DragEvent) {
  event.preventDefault();
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = "copy";
  }
}

function registerFileDropGuards(target: Window | Document | HTMLElement | null) {
  if (!target) {
    return;
  }
  target.addEventListener("dragenter", allowFileDrop as EventListener, true);
  target.addEventListener("dragover", allowFileDrop as EventListener, true);
}

registerFileDropGuards(window);
registerFileDropGuards(document);

window.addEventListener(
  "drop",
  (event) => {
    event.preventDefault();
    const paths = getFilePaths(event.dataTransfer?.files);
    if (!paths.length) {
      return;
    }
    window.dispatchEvent(new CustomEvent<string[]>("bilisum:desktop-file-drop", { detail: paths }));
    for (const listener of fileDropListeners) {
      listener(paths);
    }
  },
  true,
);

// 禁用鼠标中键导航（防止打开新窗口）
// 在 DOM 加载完成后注册事件监听
document.addEventListener(
  "DOMContentLoaded",
  () => {
    registerFileDropGuards(document.body);

    window.addEventListener(
      "auxclick",
      (event) => {
        if (event.button === 1) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      },
      { capture: true },
    );

    // 同时阻止 click 事件中的中键
    window.addEventListener(
      "click",
      (event) => {
        if (event.button === 1) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      },
      { capture: true },
    );

  },
  { once: true },
);

type CloseBehavior = "ask" | "tray" | "exit";
type ThemePreference = "light" | "dark";

export type DesktopBackendStatus = {
  running: boolean;
  ready: boolean;
  pid: number | null;
  url: string;
  lastError: string;
};

export type UpdateStatus = "idle" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "installing" | "error";

export type UpdateInfo = {
  status: UpdateStatus;
  version: string;
  releaseDate: string;
  releaseNotes: string | null;
  downloadProgress: number;
  errorMessage: string | null;
};

export type StartupAnnouncement = {
  version: string;
  title: string;
  content: string;
};

export type StorageLocationKind = "data" | "cache" | "tasks" | "logs" | "runtime";

export type StorageDirectoryStat = {
  key: StorageLocationKind;
  label: string;
  path: string;
  exists: boolean;
  sizeBytes: number;
  fileCount: number;
  directoryCount: number;
};

export type StorageOverview = {
  generatedAt: string;
  totals: {
    managedBytes: number;
    managedFiles: number;
    managedDirectories: number;
  };
  directories: StorageDirectoryStat[];
  cleanup: {
    serviceAvailable: boolean;
    orphanTaskCount: number;
    orphanTaskBytes: number;
    cacheCandidateCount: number;
    cacheCandidateBytes: number;
  };
};

export type StorageCleanupResult = {
  deletedPaths: string[];
  deletedCount: number;
  removedTaskDirs: number;
  removedCacheEntries: number;
  reclaimedBytes: number;
};

export type MigrationExternalKey = "data" | "cache" | "tasks" | "output";
export type MigrationItemKey = "app-root" | MigrationExternalKey;

export type StorageMigrationPreview = {
  sourceRoot: string;
  targetRoot: string;
  environmentManaged: boolean;
  items: Array<{
    key: MigrationItemKey;
    label: string;
    path: string;
    exists: boolean;
    sizeBytes: number;
    selected: boolean;
    required: boolean;
    carriedByRoot: boolean;
    selectable: boolean;
    reason: string | null;
    errorDetails: string | null;
    destinationPath: string | null;
    copySourceKey: MigrationItemKey | null;
    includedInLabel: string | null;
  }>;
  requiredBytes: number;
  availableBytes: number | null;
  blockers: string[];
  canMigrate: boolean;
  warnings: string[];
  excludedPaths: string[];
};

export type StorageMigrationProgress = {
  phase: "preparing" | "copying" | "verifying" | "switching" | "starting" | "rollback" | "complete" | "failed";
  message: string;
  copiedBytes: number;
  totalBytes: number;
};

export type StorageMigrationResult = {
  success: boolean;
  sourceRoot: string;
  targetRoot: string;
  error: string | null;
  rolledBack: boolean;
  serviceRestored: boolean;
};

export type StorageMigrationCleanupResult = {
  deletedFiles: number;
  skippedFiles: number;
  removedDirectories: number;
  retainedPaths: string[];
};

export type BilibiliCookieExportResult = {
  cookiesFile: string;
  cookieCount: number;
};

const desktop = {
  app: {
    getVersion: () => ipcRenderer.invoke("desktop:app:get-version") as Promise<string>,
    getAutoLaunch: () => ipcRenderer.invoke("desktop:app:get-auto-launch") as Promise<boolean>,
    setAutoLaunch: (enabled: boolean) =>
      ipcRenderer.invoke("desktop:app:set-auto-launch", enabled) as Promise<boolean>,
    getStartupAnnouncement: () =>
      ipcRenderer.invoke("desktop:app:get-startup-announcement") as Promise<StartupAnnouncement | null>,
    markStartupAnnouncementSeen: (version: string) =>
      ipcRenderer.invoke("desktop:app:mark-startup-announcement-seen", version) as Promise<void>,
  },
  window: {
    platform: (process.env.BILISUM_DEV_PLATFORM as NodeJS.Platform) || process.platform,
    show: () => ipcRenderer.invoke("desktop:window:show") as Promise<void>,
    minimize: () => ipcRenderer.invoke("desktop:window:minimize") as Promise<void>,
    maximize: () => ipcRenderer.invoke("desktop:window:maximize") as Promise<void>,
    close: () => ipcRenderer.invoke("desktop:window:close") as Promise<void>,
    isMaximized: () => ipcRenderer.invoke("desktop:window:isMaximized") as Promise<boolean>,
  },
  backend: {
    start: () => ipcRenderer.invoke("desktop:backend:start") as Promise<DesktopBackendStatus>,
    stop: () => ipcRenderer.invoke("desktop:backend:stop") as Promise<DesktopBackendStatus>,
    status: () => ipcRenderer.invoke("desktop:backend:status") as Promise<DesktopBackendStatus>,
    getAccessToken: () => ipcRenderer.invoke("desktop:backend:get-access-token") as Promise<string>,
    onStatus: (listener: (status: DesktopBackendStatus) => void) => {
      const wrapped = (_event: unknown, payload: DesktopBackendStatus) => listener(payload);
      ipcRenderer.on("desktop:backend:status-changed", wrapped);
      return () => {
        ipcRenderer.removeListener("desktop:backend:status-changed", wrapped);
      };
    },
  },
  clipboard: {
    writeImage: (dataUrl: string) => ipcRenderer.invoke("desktop:clipboard:write-image", dataUrl) as Promise<void>,
  },
  media: {
    pickVideoFile: () => ipcRenderer.invoke("desktop:media:pick-video-file") as Promise<string | null>,
    pickVideoFiles: () => ipcRenderer.invoke("desktop:media:pick-video-files") as Promise<string[]>,
    getFilePaths,
    onFileDrop: (listener: (paths: string[]) => void) => {
      fileDropListeners.add(listener);
      return () => {
        fileDropListeners.delete(listener);
      };
    },
  },
  bilibili: {
    captureLoginCookies: () =>
      ipcRenderer.invoke("desktop:bilibili:capture-login-cookies") as Promise<BilibiliCookieExportResult>,
  },
  shell: {
    openPath: (targetPath: string) => ipcRenderer.invoke("desktop:shell:open-path", targetPath) as Promise<string>,
  },
  dialog: {
    pickDirectory: (defaultPath?: string, title?: string) =>
      ipcRenderer.invoke("desktop:dialog:pick-directory", defaultPath, title) as Promise<string | null>,
  },
  logs: {
    getServiceLogPath: () => ipcRenderer.invoke("desktop:logs:get-service-log-path") as Promise<string>,
    readServiceLogTail: (lines = 200) =>
      ipcRenderer.invoke("desktop:logs:read-service-log-tail", lines) as Promise<{ path: string; lines: number; content: string }>,
    clearLog: () => ipcRenderer.invoke("desktop:logs:clear") as Promise<boolean>,
    exportLog: () => ipcRenderer.invoke("desktop:logs:export") as Promise<string | null>,
  },
  preferences: {
    getCloseBehavior: () => ipcRenderer.invoke("desktop:preferences:get-close-behavior") as Promise<CloseBehavior>,
    setCloseBehavior: (value: CloseBehavior) =>
      ipcRenderer.invoke("desktop:preferences:set-close-behavior", value) as Promise<CloseBehavior>,
    resetCloseBehavior: () => ipcRenderer.invoke("desktop:preferences:reset-close-behavior") as Promise<CloseBehavior>,
    setTheme: (value: ThemePreference) =>
      ipcRenderer.invoke("desktop:preferences:set-theme", value) as Promise<ThemePreference>,
    getSilentStart: () => ipcRenderer.invoke("desktop:preferences:get-silent-start") as Promise<boolean>,
    setSilentStart: (enabled: boolean) =>
      ipcRenderer.invoke("desktop:preferences:set-silent-start", enabled) as Promise<boolean>,
    getCrashAutoRestart: () => ipcRenderer.invoke("desktop:preferences:get-crash-auto-restart") as Promise<boolean>,
    setCrashAutoRestart: (enabled: boolean) =>
      ipcRenderer.invoke("desktop:preferences:set-crash-auto-restart", enabled) as Promise<boolean>,
    getDeveloperMode: () => ipcRenderer.invoke("desktop:preferences:get-developer-mode") as Promise<boolean>,
    setDeveloperMode: (enabled: boolean) =>
      ipcRenderer.invoke("desktop:preferences:set-developer-mode", enabled) as Promise<boolean>,
  },
  update: {
    check: () => ipcRenderer.invoke("desktop:update:check") as Promise<UpdateInfo>,
    download: () => ipcRenderer.invoke("desktop:update:download") as Promise<UpdateInfo>,
    install: () => ipcRenderer.invoke("desktop:update:install") as Promise<void>,
    getStatus: () => ipcRenderer.invoke("desktop:update:get-status") as Promise<UpdateInfo>,
    onStatus: (listener: (status: UpdateInfo) => void) => {
      const wrapped = (_event: unknown, payload: UpdateInfo) => listener(payload);
      ipcRenderer.on("desktop:update:status-changed", wrapped);
      return () => {
        ipcRenderer.removeListener("desktop:update:status-changed", wrapped);
      };
    },
  },
  fileManager: {
    getStorageOverview: (input: { taskIds?: string[] }) =>
      ipcRenderer.invoke("desktop:file-manager:get-storage-overview", input) as Promise<StorageOverview>,
    cleanupOrphans: (input: { taskIds: string[] }) =>
      ipcRenderer.invoke("desktop:file-manager:cleanup-orphans", input) as Promise<StorageCleanupResult>,
    previewMigration: (input: { targetRoot: string; selectedExternalKeys?: MigrationExternalKey[] }) =>
      ipcRenderer.invoke("desktop:file-manager:preview-migration", input) as Promise<StorageMigrationPreview>,
    startMigration: (input: { targetRoot: string; selectedExternalKeys: MigrationExternalKey[] }) =>
      ipcRenderer.invoke("desktop:file-manager:start-migration", input) as Promise<StorageMigrationResult>,
    cleanupMigratedSource: () =>
      ipcRenderer.invoke("desktop:file-manager:cleanup-migrated-source") as Promise<StorageMigrationCleanupResult>,
    keepMigratedSource: () => ipcRenderer.invoke("desktop:file-manager:keep-migrated-source") as Promise<boolean>,
    hasPendingMigrationCleanup: () => ipcRenderer.invoke("desktop:file-manager:has-pending-migration-cleanup") as Promise<boolean>,
    onMigrationProgress: (listener: (progress: StorageMigrationProgress) => void) => {
      const wrapped = (_event: unknown, payload: StorageMigrationProgress) => listener(payload);
      ipcRenderer.on("desktop:file-manager:migration-progress", wrapped);
      return () => ipcRenderer.removeListener("desktop:file-manager:migration-progress", wrapped);
    },
    openDirectory: (kind: StorageLocationKind) =>
      ipcRenderer.invoke("desktop:file-manager:open-directory", kind) as Promise<string>,
  },
};

contextBridge.exposeInMainWorld("desktop", desktop);
