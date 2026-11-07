type CloseBehavior = "ask" | "tray" | "exit";
type ThemePreference = "light" | "dark";

type DesktopBackendStatus = {
  running: boolean;
  ready: boolean;
  pid: number | null;
  url: string;
  lastError: string;
};

type UpdateStatus = "idle" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "installing" | "error";
type UpdateInfo = {
  status: UpdateStatus;
  version: string;
  releaseDate: string;
  releaseNotes: string | null;
  downloadProgress: number;
  errorMessage: string | null;
};

type StartupAnnouncement = {
  version: string;
  title: string;
  content: string;
};

type StorageLocationKind = "data" | "cache" | "tasks" | "logs" | "runtime";

type StorageDirectoryStat = {
  key: StorageLocationKind;
  label: string;
  path: string;
  exists: boolean;
  sizeBytes: number;
  fileCount: number;
  directoryCount: number;
};

type StorageCleanupSummary = {
  serviceAvailable: boolean;
  orphanTaskCount: number;
  orphanTaskBytes: number;
  cacheCandidateCount: number;
  cacheCandidateBytes: number;
};

type StorageOverview = {
  generatedAt: string;
  totals: {
    managedBytes: number;
    managedFiles: number;
    managedDirectories: number;
  };
  directories: StorageDirectoryStat[];
  cleanup: StorageCleanupSummary;
};

type StorageCleanupResult = {
  deletedPaths: string[];
  deletedCount: number;
  removedTaskDirs: number;
  removedCacheEntries: number;
  reclaimedBytes: number;
};

type MigrationExternalKey = "data" | "cache" | "tasks" | "output";
type MigrationItemKey = "app-root" | MigrationExternalKey;

type StorageMigrationPreview = {
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

type StorageMigrationProgress = {
  phase: "preparing" | "copying" | "verifying" | "switching" | "starting" | "rollback" | "complete" | "failed";
  message: string;
  copiedBytes: number;
  totalBytes: number;
};

type StorageMigrationResult = {
  success: boolean;
  sourceRoot: string;
  targetRoot: string;
  error: string | null;
  rolledBack: boolean;
  serviceRestored: boolean;
};

type StorageMigrationCleanupResult = {
  deletedFiles: number;
  skippedFiles: number;
  removedDirectories: number;
  retainedPaths: string[];
};

type BilibiliCookieExportResult = {
  cookiesFile: string;
  cookieCount: number;
};

type DesktopBridge = {
  app: {
    getVersion(): Promise<string>;
    getAutoLaunch(): Promise<boolean>;
    setAutoLaunch(enabled: boolean): Promise<boolean>;
    getStartupAnnouncement(): Promise<StartupAnnouncement | null>;
    markStartupAnnouncementSeen(version: string): Promise<void>;
  };
  window: {
    platform: NodeJS.Platform;
    show(): Promise<void>;
    minimize(): Promise<void>;
    maximize(): Promise<void>;
    close(): Promise<void>;
    isMaximized(): Promise<boolean>;
  };
  backend: {
    start(): Promise<DesktopBackendStatus>;
    stop(): Promise<DesktopBackendStatus>;
    status(): Promise<DesktopBackendStatus>;
    getAccessToken(): Promise<string>;
    onStatus(listener: (status: DesktopBackendStatus) => void): () => void;
  };
  clipboard: {
    writeImage(dataUrl: string): Promise<void>;
  };
  media: {
    pickVideoFile(): Promise<string | null>;
    pickVideoFiles(): Promise<string[]>;
    getFilePaths(files: File[] | FileList): string[];
    onFileDrop(listener: (paths: string[]) => void): () => void;
  };
  bilibili: {
    captureLoginCookies(): Promise<BilibiliCookieExportResult>;
  };
  shell: {
    openPath(targetPath: string): Promise<string>;
  };
  dialog: {
    pickDirectory(defaultPath?: string, title?: string): Promise<string | null>;
  };
  logs: {
    getServiceLogPath(): Promise<string>;
    readServiceLogTail(lines?: number): Promise<{ path: string; lines: number; content: string }>;
  };
  preferences: {
    getCloseBehavior(): Promise<CloseBehavior>;
    setCloseBehavior(value: CloseBehavior): Promise<CloseBehavior>;
    resetCloseBehavior(): Promise<CloseBehavior>;
    setTheme(value: ThemePreference): Promise<ThemePreference>;
    getSilentStart(): Promise<boolean>;
    setSilentStart(enabled: boolean): Promise<boolean>;
    getCrashAutoRestart(): Promise<boolean>;
    setCrashAutoRestart(enabled: boolean): Promise<boolean>;
    getDeveloperMode(): Promise<boolean>;
    setDeveloperMode(enabled: boolean): Promise<boolean>;
  };
  logs: {
    getServiceLogPath(): Promise<string>;
    readServiceLogTail(lines?: number): Promise<{ path: string; lines: number; content: string }>;
    clearLog(): Promise<boolean>;
    exportLog(): Promise<string | null>;
  };
  update: {
    check(): Promise<UpdateInfo>;
    download(): Promise<UpdateInfo>;
    install(): Promise<void>;
    getStatus(): Promise<UpdateInfo>;
    onStatus(listener: (status: UpdateInfo) => void): () => void;
  };
  fileManager: {
    getStorageOverview(input: { taskIds?: string[] }): Promise<StorageOverview>;
    cleanupOrphans(input: { taskIds: string[] }): Promise<StorageCleanupResult>;
    previewMigration(input: { targetRoot: string; selectedExternalKeys?: MigrationExternalKey[] }): Promise<StorageMigrationPreview>;
    startMigration(input: { targetRoot: string; selectedExternalKeys: MigrationExternalKey[] }): Promise<StorageMigrationResult>;
    cleanupMigratedSource(): Promise<StorageMigrationCleanupResult>;
    keepMigratedSource(): Promise<boolean>;
    hasPendingMigrationCleanup(): Promise<boolean>;
    onMigrationProgress(listener: (progress: StorageMigrationProgress) => void): () => void;
    openDirectory(kind: StorageLocationKind): Promise<string>;
  };
};

declare global {
  interface Window {
    desktop?: DesktopBridge;
  }
}

export {};
