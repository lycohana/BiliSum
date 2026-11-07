import { ChildProcess, spawn, SpawnOptions } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import { createServer, IncomingMessage, Server, ServerResponse } from "node:http";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import { Readable } from "node:stream";

import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
  MenuItemConstructorOptions,
  nativeImage,
  nativeTheme,
  OpenDialogOptions,
  shell,
  Tray,
} from "electron";
import { autoUpdater, ProgressInfo, UpdateInfo as ElectronUpdateInfo } from "electron-updater";
import desktopPackage from "../package.json";
import { isAllowedExternalUrl, isCrossOriginNavigation } from "./externalLinks";
import {
  cleanupMigratedSource,
  copyStorageMigration,
  getActiveTaskCount,
  type MigrationCopyResult,
  type MigrationExternalKey,
  type MigrationLocation,
  type MigrationPreview,
  type MigrationProgress,
  previewStorageMigration,
  rebaseMigrationDestinations,
  refreshMigrationDestinationHashes,
  getMigrationPathMappings,
  resolveStorageRoot,
} from "./storageMigration";

type CloseBehavior = "ask" | "tray" | "exit";
type ThemePreference = "light" | "dark";

type DesktopPreferences = {
  closeBehavior: CloseBehavior;
  rememberCloseBehavior: boolean;
  autoLaunch: boolean;
  silentStart: boolean;
  crashAutoRestart: boolean;
  developerMode: boolean;
  appDataRoot?: string;
  themePreference?: ThemePreference;
  lastOpenedVersion?: string;
  lastSeenAnnouncementVersion?: string;
};

type BackendStatus = {
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

type StorageOverview = {
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

type StorageOverviewInput = {
  taskIds?: string[];
};

type StorageCleanupInput = {
  taskIds: string[];
};

type StorageCleanupResult = {
  deletedPaths: string[];
  deletedCount: number;
  removedTaskDirs: number;
  removedCacheEntries: number;
  reclaimedBytes: number;
};

type StorageMigrationRequest = {
  targetRoot: string;
  selectedExternalKeys: MigrationExternalKey[];
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

const isDev = !app.isPackaged;
const APP_SLUG = "bilisum";
const LEGACY_APP_SLUG = "briefvid";
const LEGACY_PRODUCT_NAME = "BriefVid";
const desktopAppVersion = String(desktopPackage.version || "");
const mainWindowBounds = {
  width: 1480,
  height: 920,
  minWidth: 1200,
  minHeight: 760,
};
const splashWindowBounds = {
  width: 860,
  height: 520,
};
const minimumSplashVisibleMs = 1100;
const repoRoot = path.resolve(__dirname, "../../..");
const rendererUrl = process.env.BILISUM_RENDERER_URL ?? process.env.BRIEFVID_RENDERER_URL ?? "http://127.0.0.1:5173";
const backendUrl = "http://127.0.0.1:3838";
const updaterConfigPath = path.join(process.resourcesPath, "app-update.yml");
const iconFileName = process.platform === "darwin" ? "icon.icns" : "icon.ico";
const iconPath = isDev
  ? path.resolve(repoRoot, "apps/desktop/build", iconFileName)
  : path.join(process.resourcesPath, iconFileName);
const fallbackIconPath = process.platform === "darwin"
  ? path.join(process.resourcesPath, "app.asar.unpacked", "build", iconFileName)
  : iconPath;
const preferencesPath = path.join(app.getPath("userData"), "desktop-preferences.json");
const accessTokenPath = path.join(app.getPath("userData"), "access-token.json");
const legacyUserDataPath = path.join(app.getPath("appData"), LEGACY_PRODUCT_NAME);
const legacyPreferencesPath = path.join(legacyUserDataPath, "desktop-preferences.json");
const preferencesFileExistedAtLaunch = fs.existsSync(preferencesPath) || fs.existsSync(legacyPreferencesPath);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backendProcess: ChildProcess | null = null;
let frontendStaticServer: Server | null = null;
let frontendStaticUrl = "";
let applicationLoadPromise: Promise<void> | null = null;
let applicationLoadedTarget = "";
let backendStoppingPromise: Promise<void> | null = null;
let storageMigrationInProgress = false;
let pendingMigratedSourceCleanup: MigrationCopyResult | null = null;
let splashShownAt = 0;
let forceQuit = false;
migrateLegacyDesktopPreferences();
let preferences: DesktopPreferences = loadPreferences();
migrateLegacyLocalData();
let backendStatus: BackendStatus = {
  running: false,
  ready: false,
  pid: null,
  url: backendUrl,
  lastError: "",
};
let desktopAccessToken = "";

// 更新管理器状态
let updateStatus: UpdateInfo = {
  status: "idle",
  version: "",
  releaseDate: "",
  releaseNotes: null,
  downloadProgress: 0,
  errorMessage: null,
};
let pendingUpdateInfo: ElectronUpdateInfo | null = null;
let checkForUpdatesPromise: Promise<UpdateInfo> | null = null;
let downloadUpdatePromise: Promise<UpdateInfo> | null = null;
let downloadedUpdateVersion: string | null = null;
let installRequestedAfterDownload = false;
let quitAfterBackendStop = false;
let quittingForUpdate = false;

function getLocalAppDataDir() {
  if (process.platform === "win32") {
    return process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || app.getPath("home"), "AppData", "Local");
  }
  if (process.platform === "darwin") {
    return app.getPath("appData");
  }
  return process.env.XDG_DATA_HOME || path.join(app.getPath("home"), ".local", "share");
}

function currentLocalDataRoot() {
  return resolveStorageRoot(
    process.env.VIDEO_SUM_APP_DATA_ROOT,
    preferences.appDataRoot,
    path.join(getLocalAppDataDir(), APP_SLUG),
  );
}

function legacyLocalDataRoot() {
  return path.join(getLocalAppDataDir(), LEGACY_APP_SLUG);
}

function copyMissingTree(source: string, destination: string, mergeDepth = 0) {
  if (!fs.existsSync(source)) {
    return;
  }
  const stats = fs.statSync(source);
  if (stats.isDirectory()) {
    fs.mkdirSync(destination, { recursive: true });
    for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
      const childSource = path.join(source, entry.name);
      const childDestination = path.join(destination, entry.name);
      if (fs.existsSync(childDestination)) {
        if (entry.isDirectory() && mergeDepth < 1) {
          copyMissingTree(childSource, childDestination, mergeDepth + 1);
        }
        continue;
      }
      copyMissingTree(childSource, childDestination);
    }
    return;
  }
  if (stats.isFile() && !fs.existsSync(destination)) {
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(source, destination);
  }
}

function migrateLegacyDesktopPreferences() {
  try {
    copyMissingTree(legacyUserDataPath, app.getPath("userData"));
    copyMissingTree(legacyPreferencesPath, preferencesPath);
  } catch (error) {
    console.warn("[Migration] Failed to migrate legacy BriefVid desktop preferences:", error);
  }
}

function migrateLegacyLocalData() {
  try {
    copyMissingTree(legacyLocalDataRoot(), currentLocalDataRoot());
  } catch (error) {
    console.warn("[Migration] Failed to migrate legacy BriefVid application data:", error);
  }
}

function getServiceLogPath() {
  return path.join(currentLocalDataRoot(), "logs", "service.log");
}

function readAccessTokenFile(tokenPath: string, keys: string[]): string {
  try {
    if (!fs.existsSync(tokenPath)) {
      return "";
    }
    const payload = JSON.parse(fs.readFileSync(tokenPath, "utf-8")) as Record<string, unknown>;
    for (const key of keys) {
      const token = String(payload[key] || "").trim();
      if (token) {
        return token;
      }
    }
  } catch (error) {
    console.warn(`[Auth] Failed to read access token file ${tokenPath}:`, error);
  }
  return "";
}

function writeDesktopAccessToken(token: string) {
  try {
    fs.mkdirSync(path.dirname(accessTokenPath), { recursive: true });
    fs.writeFileSync(
      accessTokenPath,
      JSON.stringify({ accessToken: token, createdAt: new Date().toISOString() }, null, 2),
      "utf-8",
    );
  } catch (error) {
    console.warn("[Auth] Failed to persist desktop access token:", error);
  }
}

function writeServiceAccessToken(tokenPath: string, token: string) {
  try {
    fs.mkdirSync(path.dirname(tokenPath), { recursive: true });
    fs.writeFileSync(
      tokenPath,
      JSON.stringify({ access_token: token, created_at: new Date().toISOString() }, null, 2),
      "utf-8",
    );
  } catch (error) {
    console.warn(`[Auth] Failed to persist service access token ${tokenPath}:`, error);
  }
}

function loadOrCreateDesktopAccessToken() {
  const envToken = String(process.env.VIDEO_SUM_ACCESS_TOKEN || "").trim();
  if (envToken) {
    return envToken;
  }

  const serviceTokenPath = path.join(currentLocalDataRoot(), "data", "auth.json");
  const serviceToken = readAccessTokenFile(serviceTokenPath, ["access_token", "accessToken"]);
  if (serviceToken) {
    writeDesktopAccessToken(serviceToken);
    return serviceToken;
  }

  const existingDesktopToken = readAccessTokenFile(accessTokenPath, ["accessToken", "access_token"]);
  if (existingDesktopToken) {
    writeServiceAccessToken(serviceTokenPath, existingDesktopToken);
    return existingDesktopToken;
  }

  const token = crypto.randomBytes(32).toString("base64url");
  writeDesktopAccessToken(token);
  writeServiceAccessToken(serviceTokenPath, token);
  return token;
}

function getDesktopAccessToken() {
  if (!desktopAccessToken) {
    desktopAccessToken = loadOrCreateDesktopAccessToken();
  }
  return desktopAccessToken;
}

function withBackendAuthHeaders(headers?: HeadersInit): Headers {
  const nextHeaders = new Headers(headers);
  nextHeaders.set("Authorization", `Bearer ${getDesktopAccessToken()}`);
  return nextHeaders;
}

function getLogDirPath() {
  return path.dirname(getServiceLogPath());
}

function getRuntimeRootPath() {
  return path.join(currentLocalDataRoot(), "runtime");
}

function getBilibiliCookieExportPath() {
  return path.join(currentLocalDataRoot(), "data", "cookies", "bilibili.txt");
}

const MAX_LOG_CHARS = 20_000;
const MAX_LOG_LINE_CHARS = 1_000;

function trimLogText(content: string) {
  const trimmed = content
    .split(/\r?\n/)
    .map((line) => line.length > MAX_LOG_LINE_CHARS ? `${line.slice(0, MAX_LOG_LINE_CHARS)}... [line truncated]` : line)
    .join("\n");

  if (trimmed.length <= MAX_LOG_CHARS) {
    return trimmed;
  }

  return `... [log truncated, showing last ${MAX_LOG_CHARS} chars]\n${trimmed.slice(-MAX_LOG_CHARS)}`;
}

function readServiceLogTail(lines = 200) {
  const logPath = getServiceLogPath();
  const lineCount = Math.max(20, Math.min(lines, 1000));
  if (!fs.existsSync(logPath)) {
    return { path: logPath, lines: lineCount, content: "" };
  }

  try {
    const content = fs.readFileSync(logPath, "utf-8");
    return {
      path: logPath,
      lines: lineCount,
      content: trimLogText(content.split(/\r?\n/).slice(-lineCount).join("\n")),
    };
  } catch (error) {
    return {
      path: logPath,
      lines: lineCount,
      content: error instanceof Error ? error.message : "读取日志失败",
    };
  }
}

function formatCookieForNetscape(cookie: Electron.Cookie) {
  const domain = String(cookie.domain || "").trim();
  const includeSubdomains = domain.startsWith(".") ? "TRUE" : "FALSE";
  const cookiePath = String(cookie.path || "/").replace(/[\t\r\n]/g, "");
  const secure = cookie.secure ? "TRUE" : "FALSE";
  const expires = Math.max(0, Math.floor(Number(cookie.expirationDate || 0)));
  const name = String(cookie.name || "").replace(/[\t\r\n]/g, "");
  const value = String(cookie.value || "").replace(/[\t\r\n]/g, "");
  return [domain, includeSubdomains, cookiePath, secure, String(expires), name, value].join("\t");
}

async function collectBilibiliCookies(loginWindow: BrowserWindow) {
  const cookieStore = loginWindow.webContents.session.cookies;
  const urls = [
    "https://www.bilibili.com",
    "https://passport.bilibili.com",
    "https://api.bilibili.com",
    "https://space.bilibili.com",
  ];
  const cookieMap = new Map<string, Electron.Cookie>();
  for (const url of urls) {
    const cookies = await cookieStore.get({ url });
    for (const cookie of cookies) {
      if (!String(cookie.domain || "").includes("bilibili.com")) {
        continue;
      }
      cookieMap.set(`${cookie.domain}\t${cookie.path}\t${cookie.name}`, cookie);
    }
  }
  return [...cookieMap.values()];
}

async function exportBilibiliCookies(cookies: Electron.Cookie[]): Promise<BilibiliCookieExportResult> {
  const exportableCookies = cookies.filter((cookie) => cookie.name && cookie.domain && String(cookie.domain).includes("bilibili.com"));
  const cookiesFile = getBilibiliCookieExportPath();
  fs.mkdirSync(path.dirname(cookiesFile), { recursive: true });
  const lines = [
    "# Netscape HTTP Cookie File",
    "# Generated by BiliSum from the in-app Bilibili login window.",
    ...exportableCookies
      .sort((left, right) => `${left.domain}${left.path}${left.name}`.localeCompare(`${right.domain}${right.path}${right.name}`))
      .map(formatCookieForNetscape),
    "",
  ];
  fs.writeFileSync(cookiesFile, lines.join("\n"), "utf-8");
  return { cookiesFile, cookieCount: exportableCookies.length };
}

async function openBilibiliLoginAndCaptureCookies(): Promise<BilibiliCookieExportResult> {
  return new Promise((resolve, reject) => {
    const loginWindow = new BrowserWindow({
      width: 1120,
      height: 760,
      title: "登录 Bilibili - BiliSum",
      parent: mainWindow ?? undefined,
      modal: false,
      show: true,
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#121212" : "#fafbfc",
      webPreferences: {
        partition: "persist:briefvid-bilibili-login",
        nodeIntegration: false,
        contextIsolation: true,
      },
    });

    let settled = false;
    let pollTimer: NodeJS.Timeout | null = null;

    const cleanup = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };
    const settle = (callback: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      callback();
    };
    const tryCapture = async () => {
      try {
        const cookies = await collectBilibiliCookies(loginWindow);
        const hasLoginCookie = cookies.some((cookie) => cookie.name === "SESSDATA" && cookie.value);
        if (!hasLoginCookie) {
          return;
        }
        const result = await exportBilibiliCookies(cookies);
        settle(() => {
          if (!loginWindow.isDestroyed()) {
            loginWindow.close();
          }
          resolve(result);
        });
      } catch (error) {
        settle(() => reject(error));
      }
    };

    loginWindow.on("closed", () => {
      settle(() => reject(new Error("B 站登录窗口已关闭，尚未捕获到登录态。")));
    });
    loginWindow.webContents.setWindowOpenHandler(({ url }) => {
      loginWindow.loadURL(url).catch(() => undefined);
      return { action: "deny" };
    });
    loginWindow.webContents.on("did-navigate", () => {
      void tryCapture();
    });
    loginWindow.webContents.on("did-navigate-in-page", () => {
      void tryCapture();
    });

    pollTimer = setInterval(() => {
      void tryCapture();
    }, 1500);

    loginWindow.loadURL("https://passport.bilibili.com/login").catch((error) => {
      settle(() => reject(error));
    });
  });
}

function sanitizeTaskIds(taskIds?: string[]) {
  const valid = new Set<string>();
  for (const taskId of taskIds || []) {
    if (/^[0-9a-f]{32}$/i.test(String(taskId || "").trim())) {
      valid.add(String(taskId).trim().toLowerCase());
    }
  }
  return valid;
}

function resolveManagedPath(targetPath: string) {
  return path.resolve(String(targetPath || ""));
}

function isPathWithin(parentPath: string, candidatePath: string) {
  const relative = path.relative(resolveManagedPath(parentPath), resolveManagedPath(candidatePath));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function getTrustedStorageLocations() {
  const fallbackDataDir = path.join(currentLocalDataRoot(), "data");
  const fallbackCacheDir = path.join(fallbackDataDir, "cache");
  const fallbackTasksDir = path.join(fallbackDataDir, "tasks");
  const fallbackOutputDir = path.join(fallbackDataDir, "exports");
  try {
    const response = await fetch(`${backendUrl}/api/v1/settings`, {
      headers: withBackendAuthHeaders(),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const settings = await response.json() as { data_dir?: string; cache_dir?: string; tasks_dir?: string; output_dir?: string };
    return {
      dataDir: resolveManagedPath(settings.data_dir || fallbackDataDir),
      cacheDir: resolveManagedPath(settings.cache_dir || fallbackCacheDir),
      tasksDir: resolveManagedPath(settings.tasks_dir || fallbackTasksDir),
      outputDir: resolveManagedPath(settings.output_dir || fallbackOutputDir),
    };
  } catch {
    return {
      dataDir: resolveManagedPath(fallbackDataDir),
      cacheDir: resolveManagedPath(fallbackCacheDir),
      tasksDir: resolveManagedPath(fallbackTasksDir),
      outputDir: resolveManagedPath(fallbackOutputDir),
    };
  }
}

function getMigrationLocations(locations: Awaited<ReturnType<typeof getTrustedStorageLocations>>): MigrationLocation[] {
  return [
    { key: "data", label: "自定义数据目录", path: locations.dataDir },
    { key: "cache", label: "自定义缓存目录", path: locations.cacheDir },
    { key: "tasks", label: "自定义任务目录", path: locations.tasksDir },
    { key: "output", label: "导出目录", path: locations.outputDir },
  ];
}

async function createStorageMigrationPreview(input: StorageMigrationRequest): Promise<MigrationPreview> {
  const sourceRoot = currentLocalDataRoot();
  const environmentManaged = Boolean(String(process.env.VIDEO_SUM_APP_DATA_ROOT || "").trim());
  const blockers: string[] = [];
  let locations = {
    dataDir: path.join(sourceRoot, "data"),
    cacheDir: path.join(sourceRoot, "data", "cache"),
    tasksDir: path.join(sourceRoot, "data", "tasks"),
    outputDir: path.join(sourceRoot, "data", "exports"),
  };

  if (!backendStatus.running || !backendStatus.ready) {
    blockers.push("BiliSum 服务尚未就绪，无法确认当前设置和后台任务。 ");
  }
  if (!backendProcess && backendStatus.running) {
    blockers.push("当前服务不是由桌面端托管，桌面端无法安全停止它。 ");
  }

  try {
    const healthResponse = await fetch(`${backendUrl}/health`, { headers: withBackendAuthHeaders() });
    if (!healthResponse.ok) {
      throw new Error(`HTTP ${healthResponse.status}`);
    }
    const health = await healthResponse.json() as {
      status?: string;
      runtime_startup?: { status?: string };
    };
    if (health.status !== "ok" || health.runtime_startup?.status !== "ready") {
      blockers.push("服务或托管运行环境尚未通过就绪检查。 ");
    }
  } catch (error) {
    blockers.push(`无法确认服务健康状态：${error instanceof Error ? error.message : "服务不可用。"}`);
  }

  try {
    const settingsResponse = await fetch(`${backendUrl}/api/v1/settings`, { headers: withBackendAuthHeaders() });
    if (!settingsResponse.ok) {
      throw new Error(`HTTP ${settingsResponse.status}`);
    }
    const settings = await settingsResponse.json() as {
      data_dir?: string;
      cache_dir?: string;
      tasks_dir?: string;
      output_dir?: string;
    };
    locations = {
      dataDir: resolveManagedPath(settings.data_dir || locations.dataDir),
      cacheDir: resolveManagedPath(settings.cache_dir || locations.cacheDir),
      tasksDir: resolveManagedPath(settings.tasks_dir || locations.tasksDir),
      outputDir: resolveManagedPath(settings.output_dir || locations.outputDir),
    };
  } catch (error) {
    blockers.push(`无法读取当前目录设置：${error instanceof Error ? error.message : "服务不可用。"}`);
  }

  try {
    const taskResponse = await fetch(`${backendUrl}/api/v1/tasks`, { headers: withBackendAuthHeaders() });
    if (!taskResponse.ok) {
      throw new Error(`HTTP ${taskResponse.status}`);
    }
    const tasks = await taskResponse.json() as Array<{ status?: string }>;
    const activeCount = getActiveTaskCount(tasks);
    if (activeCount > 0) {
      blockers.push(`还有 ${activeCount} 个后台任务正在排队或运行，请等它们全部结束后再迁移。`);
    }
  } catch (error) {
    blockers.push(`无法确认后台任务状态：${error instanceof Error ? error.message : "服务不可用。"}`);
  }

  const preview = await previewStorageMigration({
    sourceRoot,
    targetRoot: input.targetRoot,
    locations: getMigrationLocations(locations),
    selectedExternalKeys: input.selectedExternalKeys,
    environmentManaged,
  });
  const allBlockers = [...new Set([...preview.blockers, ...blockers])];
  return { ...preview, blockers: allBlockers, canMigrate: allBlockers.length === 0 };
}

function sendStorageMigrationProgress(sender: Electron.WebContents, progress: MigrationProgress) {
  if (!sender.isDestroyed()) {
    sender.send("desktop:file-manager:migration-progress", progress);
  }
}

async function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (exited: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", onExit);
      child.removeListener("error", onError);
      resolve(exited);
    };
    const onExit = () => finish(true);
    const onError = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    child.once("exit", onExit);
    child.once("error", onError);
  });
}

async function waitForBackendPortClosed(timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (!(await probeBackendPortBusy(250))) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function stopBackendForStorageMigration() {
  const current = backendProcess;
  if (!current) {
    if (backendStatus.running || await probeBackendPortBusy(250)) {
      throw new Error("无法确认服务已由桌面端托管，迁移已取消。");
    }
    return;
  }

  if (current.exitCode === null && current.signalCode === null) {
    current.kill();
    let exited = await waitForChildExit(current, 15_000);
    if (!exited) {
      current.kill("SIGKILL");
      exited = await waitForChildExit(current, 8_000);
    }
    if (!exited) {
      throw new Error("服务进程未能安全停止，迁移已取消，源文件保持不变。");
    }
  }

  if (backendProcess === current) backendProcess = null;
  updateBackendStatus({ running: false, ready: false, pid: null, lastError: "" });
  if (!(await waitForBackendPortClosed())) {
    throw new Error("服务端口仍被占用，迁移已取消，源文件保持不变。");
  }
}

async function waitForStorageMigrationHealth(
  timeoutMs = 5 * 60_000,
  copiedBytes = 0,
  totalBytes = 0,
  onProgress?: (progress: MigrationProgress) => void,
) {
  const start = Date.now();
  let lastProgressAt = 0;
  while (Date.now() - start < timeoutMs) {
    try {
      const response = await fetch(`${backendUrl}/health`, { headers: withBackendAuthHeaders() });
      if (response.ok) {
        const health = await response.json() as {
          status?: string;
          runtime_startup?: { status?: string; message?: string };
        };
        if (health.status === "ok" && health.runtime_startup?.status === "ready") {
          return true;
        }
        if (health.runtime_startup?.status === "error") {
          throw new Error(`新位置的运行环境启动失败：${health.runtime_startup.message || "运行环境检查未通过。"}`);
        }
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("新位置的运行环境启动失败：")) throw error;
    }
    if (Date.now() - lastProgressAt > 5000) {
      lastProgressAt = Date.now();
      const progress: MigrationProgress = {
        phase: "starting",
        message: "正在验证新位置的服务与托管运行环境…",
        copiedBytes,
        totalBytes,
      };
      if (onProgress) onProgress(progress);
      else if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("desktop:file-manager:migration-progress", progress);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return false;
}

async function startStorageMigration(input: StorageMigrationRequest, sender: Electron.WebContents): Promise<StorageMigrationResult> {
  const sourceRoot = currentLocalDataRoot();
  const targetRoot = path.resolve(String(input.targetRoot || ""));
  if (storageMigrationInProgress) {
    throw new Error("已有存储迁移正在进行。 ");
  }
  if (pendingMigratedSourceCleanup) {
    throw new Error("上次迁移的旧位置尚未处理，请先清理或保留旧副本。 ");
  }

  storageMigrationInProgress = true;
  let stopped = false;
  let preferenceChanged = false;
  let preferenceRestored = true;
  let stageRoot = "";
  let copied: MigrationCopyResult | null = null;
  const oldPreferenceRoot = preferences.appDataRoot;
  try {
    sendStorageMigrationProgress(sender, {
      phase: "preparing",
      message: "正在复核目录、磁盘空间和后台任务…",
      copiedBytes: 0,
      totalBytes: 0,
    });
    const preview = await createStorageMigrationPreview({
      targetRoot,
      selectedExternalKeys: input.selectedExternalKeys,
    });
    if (!preview.canMigrate) {
      throw new Error(preview.blockers.join("\n"));
    }
    if (!backendProcess) {
      throw new Error("服务不是由桌面端托管，无法保证迁移期间停止写入。 ");
    }

    sendStorageMigrationProgress(sender, {
      phase: "preparing",
      message: "正在安全停止 BiliSum 服务…",
      copiedBytes: 0,
      totalBytes: preview.requiredBytes,
    });
    stopped = true;
    await stopBackendForStorageMigration();

    await fs.promises.mkdir(path.dirname(targetRoot), { recursive: true });
    stageRoot = path.join(path.dirname(targetRoot), `.${path.basename(targetRoot) || "BiliSum"}.migration-${crypto.randomBytes(8).toString("hex")}.tmp`);
    copied = await copyStorageMigration(preview, stageRoot, (progress) => sendStorageMigrationProgress(sender, progress));
    // Rewrite references only in the verified staging copy. The old database
    // remains untouched until the new service passes its health checks.
    const manifestPath = path.join(stageRoot, ".bilisum-migration-manifest.json");
    const mappings = getMigrationPathMappings(preview);
    await fs.promises.writeFile(manifestPath, JSON.stringify({ stageRoot, mappings, files: copied.files.map((file) => file.destinationPath) }), { encoding: "utf-8", flag: "wx" });
    try {
      const target = isDev ? resolveDevPython() : resolvePackagedBackend();
      await new Promise<void>((resolve, reject) => {
        const child = spawn(target.command, [...target.args, "--migrate-storage", manifestPath], {
          cwd: target.cwd, windowsHide: true, stdio: ["ignore", "ignore", "pipe"],
        });
        let errorText = "";
        child.stderr?.on("data", (chunk) => { errorText = (errorText + String(chunk)).slice(-2000); });
        const timer = setTimeout(() => { child.kill(); reject(new Error("迁移数据库路径更新超时。")); }, 5 * 60_000);
        child.once("error", (error) => { clearTimeout(timer); reject(error); });
        child.once("exit", (code) => {
          clearTimeout(timer);
          if (code === 0) resolve();
          else reject(new Error(`迁移数据库路径更新失败：${errorText || code}`));
        });
      });
      await refreshMigrationDestinationHashes(copied);
    } finally {
      await fs.promises.unlink(manifestPath).catch(() => undefined);
    }

    try {
      const targetStats = await fs.promises.lstat(targetRoot);
      if (!targetStats.isDirectory() || targetStats.isSymbolicLink() || (await fs.promises.readdir(targetRoot)).length > 0) {
        throw new Error("复制完成后目标目录已被占用，已取消切换。 ");
      }
      await fs.promises.rmdir(targetRoot);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    await fs.promises.rename(stageRoot, targetRoot);
    rebaseMigrationDestinations(copied, stageRoot, targetRoot);
    stageRoot = "";

    sendStorageMigrationProgress(sender, {
      phase: "switching",
      message: "校验完成，正在切换桌面端保存的存储位置…",
      copiedBytes: copied.copiedBytes,
      totalBytes: preview.requiredBytes,
    });
    preferences = { ...preferences, appDataRoot: targetRoot };
    savePreferences();
    preferenceChanged = true;

    sendStorageMigrationProgress(sender, {
      phase: "starting",
      message: "正在从新位置启动服务…",
      copiedBytes: copied.copiedBytes,
      totalBytes: preview.requiredBytes,
    });
    const startStatus = await startBackend();
    if (!startStatus.running) {
      throw new Error(startStatus.lastError || "新位置的服务进程未能启动。 ");
    }
    if (!(await waitForStorageMigrationHealth(
      5 * 60_000,
      copied.copiedBytes,
      preview.requiredBytes,
      (progress) => sendStorageMigrationProgress(sender, progress),
    ))) {
      throw new Error("新位置的服务或托管运行环境未能在限定时间内通过启动验证。 ");
    }

    pendingMigratedSourceCleanup = copied;
    sendStorageMigrationProgress(sender, {
      phase: "complete",
      message: "迁移完成，新位置的服务和托管运行环境已通过验证。",
      copiedBytes: copied.copiedBytes,
      totalBytes: preview.requiredBytes,
    });
    return {
      success: true,
      sourceRoot,
      targetRoot,
      error: null,
      rolledBack: false,
      serviceRestored: true,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : "存储迁移失败。";
    sendStorageMigrationProgress(sender, {
      phase: "rollback",
      message: "迁移未完成，正在恢复旧位置并重新启动服务…",
      copiedBytes: copied?.copiedBytes || 0,
      totalBytes: copied?.copiedBytes || 0,
    });
    if (stageRoot) {
      await fs.promises.rm(stageRoot, { recursive: true, force: true }).catch(() => undefined);
    }
    if (preferenceChanged || preferences.appDataRoot !== oldPreferenceRoot) {
      preferences = { ...preferences, appDataRoot: oldPreferenceRoot };
      try {
        savePreferences();
      } catch (saveError) {
        preferenceRestored = false;
        console.error("[StorageMigration] Failed to restore the previous desktop preference:", saveError);
      }
    }

    let serviceRestored = !stopped && Boolean(backendStatus.running && backendStatus.ready);
    if (stopped) {
      try {
        if (backendProcess || backendStatus.running) {
          await stopBackendForStorageMigration();
        }
        const restoredStatus = await startBackend();
        serviceRestored = Boolean(restoredStatus.running && await waitForStorageMigrationHealth());
      } catch (restoreError) {
        serviceRestored = false;
        console.error("[StorageMigration] Failed to restart the service from the previous location:", restoreError);
      }
    }
    sendStorageMigrationProgress(sender, {
      phase: "failed",
      message: serviceRestored
        ? "迁移失败，已恢复旧位置并重新启动服务；源文件仍保留。"
        : "迁移失败，已恢复旧位置；服务未能自动启动，请稍后重试。源文件仍保留。",
      copiedBytes: copied?.copiedBytes || 0,
      totalBytes: copied?.copiedBytes || 0,
    });
    return {
      success: false,
      sourceRoot,
      targetRoot,
      error: message,
      rolledBack: preferenceRestored && preferences.appDataRoot === oldPreferenceRoot,
      serviceRestored,
    };
  } finally {
    storageMigrationInProgress = false;
  }
}

async function cleanupPreviousStorageLocation(): Promise<StorageMigrationCleanupResult> {
  if (!pendingMigratedSourceCleanup) {
    throw new Error("没有待确认清理的旧位置。 ");
  }
  const result = await cleanupMigratedSource(pendingMigratedSourceCleanup);
  pendingMigratedSourceCleanup = null;
  return result;
}

function keepPreviousStorageLocation() {
  pendingMigratedSourceCleanup = null;
  return true;
}

function hasPendingStorageMigrationCleanup() {
  return Boolean(pendingMigratedSourceCleanup);
}

/**
 * 异步收集路径统计信息（避免阻塞主进程）
 */
async function collectPathStats(targetPath: string): Promise<{ exists: boolean; sizeBytes: number; fileCount: number; directoryCount: number }> {
  const resolvedTarget = resolveManagedPath(targetPath);
  let stats: fs.Stats;
  try {
    stats = await fs.promises.stat(resolvedTarget);
  } catch {
    return { exists: false, sizeBytes: 0, fileCount: 0, directoryCount: 0 };
  }

  if (stats.isFile()) {
    return { exists: true, sizeBytes: stats.size, fileCount: 1, directoryCount: 0 };
  }

  let sizeBytes = 0;
  let fileCount = 0;
  let directoryCount = 0;
  const stack = [resolvedTarget];

  while (stack.length) {
    const current = stack.pop();
    if (!current) {
      continue;
    }
    let entries: fs.Dirent[];
    try {
      entries = await fs.promises.readdir(current, { withFileTypes: true });
    } catch {
      continue;
    }

    // 分批处理，避免单次事件循环处理过多文件
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      try {
        if (entry.isDirectory()) {
          directoryCount += 1;
          stack.push(entryPath);
          continue;
        }
        if (entry.isFile()) {
          const entryStats = await fs.promises.stat(entryPath);
          sizeBytes += entryStats.size;
          fileCount += 1;
        }
      } catch {
        continue;
      }
    }

    // 每处理一批条目后让出事件循环，避免阻塞 UI
    if (stack.length > 0) {
      await new Promise(resolve => setImmediate(resolve));
    }
  }

  return { exists: true, sizeBytes, fileCount, directoryCount };
}

async function buildDirectoryStat(key: StorageLocationKind, label: string, targetPath: string): Promise<StorageDirectoryStat> {
  const stats = await collectPathStats(targetPath);
  return {
    key,
    label,
    path: resolveManagedPath(targetPath),
    exists: stats.exists,
    sizeBytes: stats.sizeBytes,
    fileCount: stats.fileCount,
    directoryCount: stats.directoryCount,
  };
}

function listImmediateEntries(targetPath: string) {
  try {
    return fs.readdirSync(targetPath, { withFileTypes: true }).map((entry) => path.join(targetPath, entry.name));
  } catch {
    return [];
  }
}

/**
 * 获取缓存清理候选项列表
 *
 * 清理策略：
 * 1. uploads 目录：所有文件视为可清理的缓存（用户上传的临时文件）
 * 2. covers 目录：不清理，封面图需要保留
 * 3. 已知任务目录中的 mp3 文件：视为可重新生成的缓存，清理以释放空间
 *
 * @param cacheDir - 缓存目录路径
 * @param tasksDir - 任务目录路径（可选）
 * @param taskIds - 已知任务 ID 列表（用于识别哪些 mp3 属于已知任务）
 * @returns 可清理的文件路径列表
 */
function getCacheCleanupCandidates(cacheDir: string, tasksDir?: string, taskIds?: string[]) {
  const resolvedCacheDir = resolveManagedPath(cacheDir);
  const candidates: string[] = [];
  // 只清理 uploads 目录下的孤儿文件，不清理 covers 目录（封面图需要保留）
  const targetDir = path.join(resolvedCacheDir, "uploads");
  if (fs.existsSync(targetDir) && isPathWithin(resolvedCacheDir, targetDir)) {
    for (const entryPath of listImmediateEntries(targetDir)) {
      if (isPathWithin(targetDir, entryPath)) {
        candidates.push(entryPath);
      }
    }
  }
  // 清理已完成任务的 mp3 文件（保留 jsonl、json 等文本结果）
  // 只清理已知任务（非孤儿）目录中的 mp3 文件
  if (tasksDir && Array.isArray(taskIds)) {
    const resolvedTasksDir = resolveManagedPath(tasksDir);
    if (fs.existsSync(resolvedTasksDir)) {
      try {
        const entries = fs.readdirSync(resolvedTasksDir, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.isDirectory() && /^[0-9a-f]{32}$/i.test(entry.name)) {
            const taskPath = path.join(resolvedTasksDir, entry.name);
            const taskId = entry.name.toLowerCase();
            // 只清理已知任务（非孤儿）目录中的 mp3 文件
            if (isPathWithin(resolvedTasksDir, taskPath) && taskIds.includes(taskId)) {
              // 查找任务目录中的所有 mp3 文件
              try {
                const taskFiles = fs.readdirSync(taskPath);
                for (const file of taskFiles) {
                  if (file.toLowerCase().endsWith(".mp3")) {
                    candidates.push(path.join(taskPath, file));
                  }
                }
              } catch {
                // 忽略无法读取的任务目录
              }
            }
          }
        }
      } catch {
        // 忽略无法读取的任务目录
      }
    }
  }
  return candidates;
}

function getOrphanTaskDirectories(tasksDir: string, taskIds?: string[]) {
  const resolvedTasksDir = resolveManagedPath(tasksDir);
  const knownTaskIds = sanitizeTaskIds(taskIds);
  if (!fs.existsSync(resolvedTasksDir)) {
    return [];
  }

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(resolvedTasksDir, { withFileTypes: true });
  } catch {
    return [];
  }

  return entries
    .filter((entry) => entry.isDirectory() && /^[0-9a-f]{32}$/i.test(entry.name))
    .map((entry) => path.join(resolvedTasksDir, entry.name))
    .filter((entryPath) => !knownTaskIds.has(path.basename(entryPath).toLowerCase()) && isPathWithin(resolvedTasksDir, entryPath));
}

async function getStorageOverview(input: StorageOverviewInput): Promise<StorageOverview> {
  const { dataDir, cacheDir, tasksDir } = await getTrustedStorageLocations();
  const logsDir = resolveManagedPath(getLogDirPath());
  const runtimeDir = resolveManagedPath(getRuntimeRootPath());

  // 并行统计所有目录
  const directories = await Promise.all([
    buildDirectoryStat("data", "数据目录", dataDir),
    buildDirectoryStat("cache", "缓存目录", cacheDir),
    buildDirectoryStat("tasks", "任务结果", tasksDir),
    buildDirectoryStat("logs", "日志目录", logsDir),
    buildDirectoryStat("runtime", "运行环境目录", runtimeDir),
  ]);
  const dataStats = directories.find((item) => item.key === "data") || directories[0];
  const logsStats = directories.find((item) => item.key === "logs");
  const runtimeStats = directories.find((item) => item.key === "runtime");
  const orphanTaskDirs = getOrphanTaskDirectories(tasksDir, input.taskIds);
  
  // 异步计算孤儿目录大小
  let orphanTaskBytes = 0;
  for (const entryPath of orphanTaskDirs) {
    const stats = await collectPathStats(entryPath);
    orphanTaskBytes += stats.sizeBytes;
  }
  
  const cacheCandidates = getCacheCleanupCandidates(cacheDir, tasksDir, input.taskIds);
  
  // 异步计算缓存候选项大小
  let cacheCandidateBytes = 0;
  for (const entryPath of cacheCandidates) {
    const stats = await collectPathStats(entryPath);
    cacheCandidateBytes += stats.sizeBytes;
  }

  return {
    generatedAt: new Date().toISOString(),
    totals: {
      managedBytes: dataStats.sizeBytes + (logsStats?.sizeBytes || 0) + (runtimeStats?.sizeBytes || 0),
      managedFiles: dataStats.fileCount + (logsStats?.fileCount || 0) + (runtimeStats?.fileCount || 0),
      managedDirectories: dataStats.directoryCount + (logsStats?.directoryCount || 0) + (runtimeStats?.directoryCount || 0),
    },
    directories,
    cleanup: {
      serviceAvailable: Array.isArray(input.taskIds),
      orphanTaskCount: orphanTaskDirs.length,
      orphanTaskBytes,
      cacheCandidateCount: cacheCandidates.length,
      cacheCandidateBytes,
    },
  };
}

async function removePathIfPresent(targetPath: string): Promise<number> {
  const resolvedTarget = resolveManagedPath(targetPath);
  try {
    const stats = await fs.promises.stat(resolvedTarget);
    const sizeBytes = (await collectPathStats(resolvedTarget)).sizeBytes;
    if (stats.isDirectory()) {
      await fs.promises.rm(resolvedTarget, { recursive: true, force: true });
    } else {
      await fs.promises.rm(resolvedTarget, { force: true });
    }
    return sizeBytes;
  } catch {
    return 0;
  }
}

async function cleanupOrphans(input: StorageCleanupInput): Promise<StorageCleanupResult> {
  const { cacheDir, tasksDir } = await getTrustedStorageLocations();
  const orphanTaskDirs = getOrphanTaskDirectories(tasksDir, input.taskIds);
  const cacheCandidates = getCacheCleanupCandidates(cacheDir, tasksDir, input.taskIds);
  const deletedPaths: string[] = [];
  let reclaimedBytes = 0;
  let removedTaskDirs = 0;
  let removedCacheEntries = 0;

  // 删除孤儿任务目录
  for (const targetPath of orphanTaskDirs) {
    reclaimedBytes += await removePathIfPresent(targetPath);
    deletedPaths.push(targetPath);
    removedTaskDirs += 1;
  }

  // 删除 uploads 目录中的孤儿文件和任务目录中的 mp3 文件
  for (const targetPath of cacheCandidates) {
    reclaimedBytes += await removePathIfPresent(targetPath);
    deletedPaths.push(targetPath);
    removedCacheEntries += 1;
  }

  return {
    deletedPaths,
    deletedCount: deletedPaths.length,
    removedTaskDirs,
    removedCacheEntries,
    reclaimedBytes,
  };
}

function resolveDirectoryByKind(kind: StorageLocationKind, locations: { dataDir: string; cacheDir: string; tasksDir: string }) {
  if (kind === "logs") {
    return getLogDirPath();
  }
  if (kind === "runtime") {
    return getRuntimeRootPath();
  }
  if (kind === "cache") {
    return locations.cacheDir;
  }
  if (kind === "tasks") {
    return locations.tasksDir;
  }
  return locations.dataDir;
}

function loadPreferences(): DesktopPreferences {
  try {
    const raw = fs.readFileSync(preferencesPath, "utf-8");
    const parsed = JSON.parse(raw) as Partial<DesktopPreferences>;
    const appDataRoot = typeof parsed.appDataRoot === "string" && parsed.appDataRoot.trim()
      ? path.resolve(parsed.appDataRoot.trim())
      : undefined;
    return {
      closeBehavior: "ask",
      rememberCloseBehavior: false,
      autoLaunch: false,
      silentStart: false,
      crashAutoRestart: false,
      developerMode: false,
      ...parsed,
      appDataRoot,
    };
  } catch {
    return {
      closeBehavior: "ask",
      rememberCloseBehavior: false,
      autoLaunch: false,
      silentStart: false,
      crashAutoRestart: false,
      developerMode: false,
    };
  }
}

function savePreferences() {
  fs.mkdirSync(path.dirname(preferencesPath), { recursive: true });
  const temporaryPath = `${preferencesPath}.tmp-${process.pid}`;
  try {
    fs.writeFileSync(temporaryPath, JSON.stringify(preferences, null, 2), "utf-8");
    fs.renameSync(temporaryPath, preferencesPath);
  } catch (error) {
    try {
      fs.rmSync(temporaryPath, { force: true });
    } catch {
      // Preserve the original preference file if the atomic save cannot complete.
    }
    throw error;
  }
}

function getPreferences(): DesktopPreferences {
  return { ...preferences };
}

function setCloseBehavior(value: CloseBehavior, remember: boolean) {
  preferences = { ...preferences, closeBehavior: value, rememberCloseBehavior: remember };
  savePreferences();
}

function resetCloseBehavior(): CloseBehavior {
  preferences = { ...preferences, closeBehavior: "ask", rememberCloseBehavior: false };
  savePreferences();
  return "ask";
}

function setThemePreference(value: ThemePreference): ThemePreference {
  preferences = { ...preferences, themePreference: value };
  savePreferences();
  return value;
}

function getAnnouncementPath() {
  return path.join(app.getAppPath(), "announcement.md");
}

function readStartupAnnouncementContent() {
  try {
    return fs.readFileSync(getAnnouncementPath(), "utf-8").trim();
  } catch {
    return "";
  }
}

function shouldShowStartupAnnouncement() {
  const currentVersion = desktopAppVersion || app.getVersion();
  if (!currentVersion) {
    return false;
  }
  if (preferences.lastSeenAnnouncementVersion === currentVersion) {
    return false;
  }
  if (preferences.lastOpenedVersion && preferences.lastOpenedVersion !== currentVersion) {
    return true;
  }
  return preferencesFileExistedAtLaunch && !preferences.lastOpenedVersion;
}

function getStartupAnnouncement(): StartupAnnouncement | null {
  const content = readStartupAnnouncementContent();
  if (!content || !shouldShowStartupAnnouncement()) {
    return null;
  }
  return {
    version: desktopAppVersion || app.getVersion(),
    title: "更新公告",
    content,
  };
}

function markStartupAnnouncementSeen(version: string) {
  const currentVersion = desktopAppVersion || app.getVersion();
  preferences = {
    ...preferences,
    lastOpenedVersion: currentVersion,
    lastSeenAnnouncementVersion: version || currentVersion,
  };
  savePreferences();
}

function recordOpenedVersionIfNoAnnouncement() {
  if (getStartupAnnouncement()) {
    return;
  }
  const currentVersion = desktopAppVersion || app.getVersion();
  if (preferences.lastOpenedVersion !== currentVersion) {
    preferences = { ...preferences, lastOpenedVersion: currentVersion };
    savePreferences();
  }
}

function getStartupHidden(): boolean {
  const prefs = getPreferences();
  return prefs.silentStart && process.argv.includes("--hidden");
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function getSplashMarkup(message = "正在启动 BiliSum 服务...") {
  const version = desktopAppVersion || app.getVersion();
  const themeClass = preferences.themePreference === "dark" ? "theme-dark" : preferences.themePreference === "light" ? "theme-light" : "";
  const escapedMessage = escapeHtml(message);
  const escapedVersion = escapeHtml(version);
  const escapedThemeClass = escapeHtml(themeClass);
  return `
    <html lang="zh-CN" class="${escapedThemeClass}">
      <head>
        <meta charset="utf-8" />
        <title>BiliSum</title>
        <style>
          :root {
            color-scheme: light;
            --brand-400: #ff9aba;
            --brand-500: #fb7299;
            --brand-600: #f85d8e;
            --brand-700: #d94674;
            --info: #567eff;
            --bg-base: #fafbfc;
            --bg-canvas: #ffffff;
            --bg-soft: #f5f6f8;
            --bg-subtle: #f8f9fc;
            --bg-elevated: #ffffff;
            --bg-accent: rgba(251, 114, 153, 0.06);
            --bg-accent-strong: rgba(251, 114, 153, 0.1);
            --text-primary: #1a1a1a;
            --text-secondary: #3f4754;
            --text-muted: #7e8898;
            --border-subtle: rgba(0, 0, 0, 0.06);
            --border-default: rgba(0, 0, 0, 0.1);
            --accent-border: rgba(251, 114, 153, 0.18);
            --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.1);
            --highlight: rgba(255, 255, 255, 0.76);
            --surface-top: rgba(255, 255, 255, 0.9);
            --surface-bottom: rgba(255, 255, 255, 0.78);
            --accent-wash: rgba(251, 114, 153, 0.075);
            --info-wash: rgba(86, 126, 255, 0.055);
          }
          :root.theme-dark {
            color-scheme: dark;
            --bg-base: #121212;
            --bg-canvas: #1a1a1a;
            --bg-soft: #222222;
            --bg-subtle: #282828;
            --bg-elevated: #2d2d2d;
            --bg-accent: rgba(251, 114, 153, 0.1);
            --bg-accent-strong: rgba(251, 114, 153, 0.16);
            --text-primary: #f5f5f5;
            --text-secondary: #b0b0b0;
            --text-muted: #666666;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-default: rgba(255, 255, 255, 0.12);
            --accent-border: rgba(251, 114, 153, 0.24);
            --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.4);
            --highlight: rgba(255, 255, 255, 0.06);
            --surface-top: rgba(26, 26, 26, 0.96);
            --surface-bottom: rgba(18, 18, 18, 0.92);
            --accent-wash: rgba(251, 114, 153, 0.052);
            --info-wash: rgba(86, 126, 255, 0.044);
          }
          @media (prefers-color-scheme: dark) {
            :root:not(.theme-light) {
              color-scheme: dark;
              --bg-base: #121212;
              --bg-canvas: #1a1a1a;
              --bg-soft: #222222;
              --bg-subtle: #282828;
              --bg-elevated: #2d2d2d;
              --bg-accent: rgba(251, 114, 153, 0.1);
              --bg-accent-strong: rgba(251, 114, 153, 0.16);
              --text-primary: #f5f5f5;
              --text-secondary: #b0b0b0;
              --text-muted: #666666;
              --border-subtle: rgba(255, 255, 255, 0.08);
              --border-default: rgba(255, 255, 255, 0.12);
              --accent-border: rgba(251, 114, 153, 0.24);
              --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.4);
              --highlight: rgba(255, 255, 255, 0.06);
              --surface-top: rgba(26, 26, 26, 0.96);
              --surface-bottom: rgba(18, 18, 18, 0.92);
              --accent-wash: rgba(251, 114, 153, 0.052);
              --info-wash: rgba(86, 126, 255, 0.044);
            }
          }
          body {
            margin: 0;
            min-height: 100vh;
            overflow: hidden;
            background: linear-gradient(180deg, var(--bg-base) 0%, var(--bg-soft) 100%);
            background-color: #121212;
            color: var(--text-primary);
            font-family: "Inter", "Plus Jakarta Sans", "Manrope", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", "Segoe UI", sans-serif;
            user-select: none;
          }
          .splash-container {
            position: relative;
            box-sizing: border-box;
            width: 100vw;
            height: 100vh;
            padding: 54px 56px 46px;
            border-radius: 28px;
            border: 1px solid var(--border-subtle);
            background:
              linear-gradient(180deg, var(--surface-top), var(--surface-bottom)),
              linear-gradient(135deg, var(--accent-wash) 0%, transparent 46%),
              linear-gradient(225deg, var(--info-wash) 0%, transparent 52%);
            box-shadow: inset 0 1px 0 var(--highlight), var(--shadow-lg);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
          }
          .splash-container::before {
            content: "";
            position: absolute;
            inset: 0;
            border-radius: inherit;
            background:
              linear-gradient(115deg, var(--highlight), transparent 28%),
              linear-gradient(155deg, transparent 18%, var(--accent-wash) 46%, transparent 72%);
            opacity: 0.56;
            pointer-events: none;
          }
          .close-button {
            position: absolute;
            z-index: 3;
            top: 18px;
            right: 18px;
            width: 32px;
            height: 32px;
            border: none;
            background: color-mix(in srgb, var(--bg-elevated) 86%, transparent);
            color: var(--text-muted);
            border-radius: 10px;
            cursor: pointer;
            display: grid;
            place-items: center;
            box-shadow: inset 0 0 0 1px var(--border-subtle);
            transition: background-color 0.15s ease, color 0.15s ease;
          }
          .close-button:hover {
            background: var(--bg-accent-strong);
            color: var(--brand-600);
          }
          .brand {
            position: relative;
            z-index: 2;
            display: flex;
            align-items: center;
            gap: 20px;
          }
          .brand-mark {
            width: 72px;
            height: 72px;
            border-radius: 20px;
            background:
              linear-gradient(145deg, var(--bg-elevated), var(--bg-subtle)),
              linear-gradient(135deg, var(--bg-accent), rgba(86, 126, 255, 0.08));
            display: grid;
            place-items: center;
            box-shadow:
              0 18px 42px rgba(251, 114, 153, 0.14),
              inset 0 1px 0 var(--highlight),
              inset 0 0 0 1px var(--accent-border);
            animation: brandFloat 3.2s ease-in-out infinite;
          }
          .brand-mark svg {
            width: 48px;
            height: 48px;
            filter: drop-shadow(0 6px 10px rgba(251, 114, 153, 0.2));
          }
          h1 {
            margin: 0 0 8px;
            font-size: 30px;
            line-height: 1;
            font-weight: 750;
            letter-spacing: 0;
            color: var(--text-primary);
          }
          .tagline {
            margin: 0;
            color: var(--brand-600);
            font-size: 15px;
            font-weight: 600;
            letter-spacing: 0;
          }
          .center-light {
            position: absolute;
            inset: 0;
            background:
              linear-gradient(135deg, transparent 12%, var(--accent-wash) 36%, transparent 58%),
              linear-gradient(225deg, transparent 22%, var(--info-wash) 48%, transparent 72%);
            opacity: 0.78;
            pointer-events: none;
            animation: ambientSweep 4.8s ease-in-out infinite;
          }
          .progress-zone {
            position: relative;
            z-index: 2;
          }
          .progress-track {
            position: relative;
            height: 3px;
            border-radius: 999px;
            overflow: hidden;
            background: color-mix(in srgb, var(--border-default) 64%, var(--bg-accent) 36%);
          }
          .progress-bar {
            position: absolute;
            inset: 0 auto 0 0;
            width: 0%;
            border-radius: inherit;
            background: linear-gradient(90deg, var(--brand-500), var(--brand-600), var(--info));
            box-shadow: 0 0 18px rgba(251, 114, 153, 0.26);
            transition: width 0.6s ease;
          }
          .status-row {
            margin-top: 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 24px;
            color: var(--text-muted);
            font-size: 15px;
            font-weight: 600;
          }
          #status-message {
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            transition: opacity 0.2s ease;
          }
          .version {
            flex: 0 0 auto;
            color: color-mix(in srgb, var(--text-muted) 72%, transparent);
          }
          .fade-out {
            opacity: 0;
          }
          @keyframes brandFloat {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-4px); }
          }
          @keyframes ambientSweep {
            0%, 100% { transform: translateX(-1.5%); opacity: 0.58; }
            50% { transform: translateX(1.5%); opacity: 0.78; }
          }
        </style>
      </head>
      <body>
        <div class="splash-container">
          <div class="center-light"></div>
          <button class="close-button" onclick="window.close()" aria-label="关闭">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </button>
          <div class="brand">
            <div class="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
                <defs>
                  <linearGradient id="splash-icon-bg" x1="56" y1="56" x2="456" y2="456" gradientUnits="userSpaceOnUse">
                    <stop stop-color="#FF9ABA"/>
                    <stop offset="1" stop-color="#F85D8E"/>
                  </linearGradient>
                </defs>
                <rect x="56" y="56" width="400" height="400" rx="96" fill="url(#splash-icon-bg)"/>
                <rect x="132" y="112" width="248" height="288" rx="42" fill="#FFFFFF"/>
                <path d="M314 112H340C362.091 112 380 129.909 380 152V178H354C331.909 178 314 160.091 314 138V112Z" fill="#FFE6EE"/>
                <circle cx="318" cy="172" r="32" fill="#FB7299"/>
                <path d="M307 157L329 172L307 187V157Z" fill="#FFFFFF"/>
                <rect x="172" y="172" width="96" height="20" rx="10" fill="#FB7299"/>
                <rect x="172" y="222" width="160" height="20" rx="10" fill="#FB7299" opacity="0.9"/>
                <rect x="172" y="272" width="138" height="20" rx="10" fill="#FB7299" opacity="0.72"/>
                <rect x="172" y="322" width="112" height="20" rx="10" fill="#FB7299" opacity="0.5"/>
              </svg>
            </div>
            <div>
              <h1>BiliSum</h1>
              <p class="tagline">视频内容总结与知识整理工具</p>
            </div>
          </div>
          <div class="progress-zone">
            <div class="progress-track">
              <div class="progress-bar" id="progress-bar"></div>
            </div>
            <div class="status-row">
              <span id="status-message">${escapedMessage}</span>
              <span class="version">v${escapedVersion}</span>
            </div>
          </div>
        </div>
      </body>
    </html>
  `;
}

function loadSplash(message = "正在启动 BiliSum 服务...") {
  // A controlled stop/restart must keep the renderer and its migration IPC
  // promise alive, including while recovering from a failed new-location start.
  if (storageMigrationInProgress && applicationLoadedTarget) return;
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  applicationLoadedTarget = "";
  splashShownAt = Date.now();
  mainWindow.setResizable(false);
  mainWindow.setMinimumSize(splashWindowBounds.width, splashWindowBounds.height);
  mainWindow.setSize(splashWindowBounds.width, splashWindowBounds.height);
  mainWindow.center();
  void mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(getSplashMarkup(message))}`);
  
  // 注入 IPC 监听脚本
  mainWindow.webContents.once('did-finish-load', () => {
    if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) {
      return;
    }
    const script = `
      (function() {
        var messageElement = document.getElementById('status-message');
        var barElement = document.getElementById('progress-bar');
        window.updateStatusMessage = function(msg) {
          if (messageElement) {
            messageElement.classList.add('fade-out');
            setTimeout(function() {
              messageElement.textContent = msg;
              messageElement.classList.remove('fade-out');
            }, 200);
          }
        };
        window.updateSplashProgress = function(pct) {
          if (barElement) {
            barElement.style.width = Math.max(0, Math.min(100, pct)) + '%';
          }
        };
      })();
    `;
    mainWindow.webContents.executeJavaScript(script);
  });
}

function updateSplashMessage(message: string) {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) {
    return;
  }
  mainWindow.webContents.executeJavaScript(`window.updateStatusMessage(${JSON.stringify(message)})`).catch(() => {});
}

function updateSplashProgress(pct: number) {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) {
    return;
  }
  mainWindow.webContents.executeJavaScript(`window.updateSplashProgress(${pct})`).catch(() => {});
}

function sendBackendStatus() {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) {
    return;
  }
  mainWindow.webContents.send("desktop:backend:status-changed", backendStatus);
}

function updateBackendStatus(patch: Partial<BackendStatus>) {
  const previousReady = backendStatus.ready;
  backendStatus = { ...backendStatus, ...patch };
  rebuildTrayMenu();
  sendBackendStatus();

  if (!previousReady && backendStatus.ready) {
    if (!storageMigrationInProgress) {
      setTimeout(() => {
        if (!storageMigrationInProgress) void loadApplication();
      }, 300);
    }
  } else if (!backendStatus.running && backendStatus.lastError) {
    updateSplashMessage(backendStatus.lastError);
  } else if (!backendStatus.running && !backendStatus.ready) {
    updateSplashMessage("BiliSum 服务已停止，正在等待重新启动。");
  }
}

async function waitForBackendReady(timeoutMs = 60_000): Promise<boolean> {
  const start = Date.now();
  let attempt = 0;
  while (Date.now() - start < timeoutMs) {
    try {
      const response = await fetch(`${backendUrl}/health`, { headers: withBackendAuthHeaders() });
      if (response.ok) {
        updateBackendStatus({ ready: true, lastError: "" });
        updateSplashProgress(80);
        updateSplashMessage("服务已就绪，正在加载应用...");
        return true;
      }
    } catch {
      // Keep polling until timeout.
    }
    attempt++;
    updateSplashProgress(Math.min(80, 40 + attempt * 2));
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
  updateBackendStatus({ ready: false, lastError: "Backend health check timed out." });
  return false;
}

async function probeBackendReady(timeoutMs = 300, updateStatus = true): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${backendUrl}/health`, {
      headers: withBackendAuthHeaders(),
      signal: controller.signal,
    });
    if (response.ok) {
      if (updateStatus) {
        updateBackendStatus({ ready: true, lastError: "" });
      }
      return true;
    }
  } catch {
    // Fast probe only checks whether an already-running backend is immediately available.
  } finally {
    clearTimeout(timeout);
  }
  return false;
}

async function probeBackendPortBusy(timeoutMs = 300): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port: 3838 });
    const timer = setTimeout(() => {
      socket.destroy();
      resolve(false);
    }, timeoutMs);
    socket.once("connect", () => {
      clearTimeout(timer);
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => {
      clearTimeout(timer);
      resolve(false);
    });
  });
}

function resolveDevPython(): { command: string; args: string[]; cwd: string; forceHidden?: boolean } {
  // 1. 优先使用环境变量指定的 Python
  const devPython = process.env.BILISUM_DEV_PYTHON ?? process.env.BRIEFVID_DEV_PYTHON;
  if (devPython) {
    return {
      command: devPython,
      args: ["-m", "video_sum_service"],
      cwd: repoRoot,
      forceHidden: true,
    };
  }

  if (process.platform === "win32") {
    // 2. 开发态默认使用仓库虚拟环境，避免被用户数据目录里的旧 managed runtime 影响。
    const venvPythonw = path.resolve(repoRoot, ".venv/Scripts/pythonw.exe");
    if (fs.existsSync(venvPythonw)) {
      return { command: venvPythonw, args: ["-m", "video_sum_service"], cwd: repoRoot };
    }

    // 3. 回退到 .venv 的 python.exe
    const venvPython = path.resolve(repoRoot, ".venv/Scripts/python.exe");
    if (fs.existsSync(venvPython)) {
      return { command: venvPython, args: ["-m", "video_sum_service"], cwd: repoRoot, forceHidden: true };
    }

    const runtimeRoot = getRuntimeRootPath();

    // 4. 兜底使用 GPU runtime 根目录的 pythonw.exe（portable runtime）
    const gpuRuntimePythonw = path.join(runtimeRoot, "gpu-cu128", "pythonw.exe");
    if (fs.existsSync(gpuRuntimePythonw)) {
      return { command: gpuRuntimePythonw, args: ["-m", "video_sum_service"], cwd: repoRoot };
    }

    // 5. 兼容旧 runtime：回退到 Scripts 目录的 pythonw.exe
    const legacyGpuRuntimePythonw = path.join(runtimeRoot, "gpu-cu128", "Scripts", "pythonw.exe");
    if (fs.existsSync(legacyGpuRuntimePythonw)) {
      return { command: legacyGpuRuntimePythonw, args: ["-m", "video_sum_service"], cwd: repoRoot };
    }

    // 6. 兜底使用 GPU runtime 根目录的 python.exe（portable runtime）
    const gpuRuntimePython = path.join(runtimeRoot, "gpu-cu128", "python.exe");
    if (fs.existsSync(gpuRuntimePython)) {
      return { command: gpuRuntimePython, args: ["-m", "video_sum_service"], cwd: repoRoot, forceHidden: true };
    }

    // 7. 兼容旧 runtime：回退到 Scripts 目录的 python.exe
    const legacyGpuRuntimePython = path.join(runtimeRoot, "gpu-cu128", "Scripts", "python.exe");
    if (fs.existsSync(legacyGpuRuntimePython)) {
      return { command: legacyGpuRuntimePython, args: ["-m", "video_sum_service"], cwd: repoRoot, forceHidden: true };
    }
  }

  // 8. 最后回退到系统 python
  return { command: "python", args: ["-m", "video_sum_service"], cwd: repoRoot, forceHidden: true };
}

function getDevPythonPathEntries() {
  return [
    path.resolve(repoRoot, "packages/core/src"),
    path.resolve(repoRoot, "packages/infra/src"),
    path.resolve(repoRoot, "apps/service/src"),
  ];
}

function resolvePackagedBackend(): { command: string; args: string[]; cwd: string } {
  const executableName = process.platform === "win32" ? "BiliSum.exe" : "BiliSum";
  const candidateRoots = [
    path.join(process.resourcesPath, "backend", "BiliSum"),
    path.join(path.dirname(process.execPath), "resources", "backend", "BiliSum"),
    path.join(path.dirname(app.getAppPath()), "backend", "BiliSum"),
  ];

  for (const backendRoot of candidateRoots) {
    const command = path.join(backendRoot, executableName);
    if (fs.existsSync(command) && fs.existsSync(backendRoot)) {
      if (process.platform !== "win32") {
        try {
          fs.chmodSync(command, 0o755);
        } catch (error) {
          console.warn("[Backend] Failed to mark packaged backend executable:", error);
        }
      }
      return {
        command,
        args: [],
        cwd: backendRoot,
      };
    }
  }

  throw new Error(
    [
      `未找到内置后端文件 ${executableName}。`,
      `请确认安装目录下存在 ${path.join("resources", "backend", "BiliSum", executableName)}。`,
      `当前 resourcesPath: ${process.resourcesPath}`,
    ].join(" "),
  );
}

function resolvePackagedWebStaticRoot(): string {
  const backendRoots = [
    path.join(process.resourcesPath, "backend", "BiliSum"),
    path.join(path.dirname(process.execPath), "resources", "backend", "BiliSum"),
    path.join(path.dirname(app.getAppPath()), "backend", "BiliSum"),
  ];
  const candidateRoots = backendRoots.flatMap((backendRoot) => [
    path.join(backendRoot, "_internal", "web", "static"),
    path.join(backendRoot, "web", "static"),
  ]);

  for (const staticRoot of candidateRoots) {
    if (fs.existsSync(path.join(staticRoot, "index.html"))) {
      return staticRoot;
    }
  }

  throw new Error("未找到内置前端静态文件 index.html。");
}

function contentTypeFor(filePath: string): string {
  switch (path.extname(filePath).toLowerCase()) {
    case ".html":
      return "text/html; charset=utf-8";
    case ".js":
      return "text/javascript; charset=utf-8";
    case ".css":
      return "text/css; charset=utf-8";
    case ".svg":
      return "image/svg+xml";
    case ".png":
      return "image/png";
    case ".ico":
      return "image/x-icon";
    case ".woff":
      return "font/woff";
    case ".woff2":
      return "font/woff2";
    case ".ttf":
      return "font/ttf";
    default:
      return "application/octet-stream";
  }
}

function isPathInside(parentPath: string, childPath: string): boolean {
  const relative = path.relative(parentPath, childPath);
  return Boolean(relative) && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function isBackendProxyPath(pathname: string): boolean {
  return (
    pathname === "/health" ||
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/media" ||
    pathname.startsWith("/media/")
  );
}

function buildProxyHeaders(request: IncomingMessage): Headers {
  const headers = new Headers();
  const skippedHeaders = new Set([
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "origin",
    "proxy-authenticate",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ]);
  for (const [key, value] of Object.entries(request.headers)) {
    if (value === undefined) {
      continue;
    }
    if (skippedHeaders.has(key.toLowerCase())) {
      continue;
    }
    headers.set(key, Array.isArray(value) ? value.join(", ") : value);
  }
  headers.set("Authorization", `Bearer ${getDesktopAccessToken()}`);
  return headers;
}

function buildBackendProxyUrl(requestUrl: URL): URL {
  const targetUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`, backendUrl);
  if (targetUrl.origin !== backendUrl) {
    throw new Error(`Refusing to proxy outside backend origin: ${targetUrl.origin}`);
  }
  return targetUrl;
}

function buildBackendResponseHeaders(backendResponse: Response): Record<string, string> {
  const headers: Record<string, string> = {};
  const skippedHeaders = new Set([
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ]);
  for (const [key, value] of backendResponse.headers.entries()) {
    if (!skippedHeaders.has(key.toLowerCase())) {
      headers[key] = value;
    }
  }
  return headers;
}

function sendBackendStartingResponse(response: ServerResponse) {
  if (response.headersSent || response.destroyed) {
    response.destroy();
    return;
  }
  response.writeHead(503, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify({ detail: "BiliSum 后端正在启动，请稍后重试。" }));
}

async function proxyBackendRequest(request: IncomingMessage, response: ServerResponse, requestUrl: URL) {
  const controller = new AbortController();
  const abortProxy = () => controller.abort();
  request.once("aborted", abortProxy);
  response.once("close", () => {
    if (!response.writableEnded) {
      abortProxy();
    }
  });

  try {
    const targetUrl = buildBackendProxyUrl(requestUrl);
    const backendResponse = await fetch(targetUrl, {
      method: request.method,
      headers: buildProxyHeaders(request),
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request,
      duplex: "half",
      signal: controller.signal,
    } as RequestInit & { duplex: "half" });

    if (response.destroyed) {
      return;
    }
    response.writeHead(backendResponse.status, buildBackendResponseHeaders(backendResponse));
    if (!backendResponse.body) {
      response.end();
      return;
    }

    const responseBody = Readable.fromWeb(backendResponse.body as import("node:stream/web").ReadableStream);
    responseBody.once("error", () => response.destroy());
    response.once("close", () => responseBody.destroy());
    responseBody.pipe(response);
  } catch (error) {
    if ((error as Error).name !== "AbortError") {
      console.warn("Backend proxy request failed:", error);
    }
    sendBackendStartingResponse(response);
  }
}

async function startPackagedFrontendServer(): Promise<string> {
  if (frontendStaticUrl) {
    return frontendStaticUrl;
  }

  const staticRoot = resolvePackagedWebStaticRoot();
  const resolvedStaticRoot = fs.realpathSync.native(staticRoot);
  const indexPath = path.join(resolvedStaticRoot, "index.html");

  frontendStaticServer = createServer((request, response) => {
    const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
    let decodedPath = "/";
    try {
      decodedPath = decodeURIComponent(requestUrl.pathname);
    } catch {
      response.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Bad request");
      return;
    }
    if (isBackendProxyPath(decodedPath)) {
      void proxyBackendRequest(request, response, requestUrl);
      return;
    }

    let filePath = indexPath;

    if (decodedPath.startsWith("/static/")) {
      filePath = path.join(resolvedStaticRoot, decodedPath.slice("/static/".length));
    }

    const resolvedFilePath = path.resolve(filePath);
    if (resolvedFilePath !== resolvedStaticRoot && !isPathInside(resolvedStaticRoot, resolvedFilePath)) {
      response.writeHead(403);
      response.end("Forbidden");
      return;
    }
    if (!fs.existsSync(resolvedFilePath) || !fs.statSync(resolvedFilePath).isFile()) {
      response.writeHead(decodedPath.startsWith("/static/") ? 404 : 200, {
        "Content-Type": decodedPath.startsWith("/static/") ? "text/plain; charset=utf-8" : contentTypeFor(indexPath),
        "Cache-Control": "no-store",
      });
      response.end(decodedPath.startsWith("/static/") ? "Not found" : fs.readFileSync(indexPath));
      return;
    }

    const realFilePath = fs.realpathSync.native(resolvedFilePath);
    if (realFilePath !== resolvedStaticRoot && !isPathInside(resolvedStaticRoot, realFilePath)) {
      response.writeHead(403);
      response.end("Forbidden");
      return;
    }

    response.writeHead(200, {
      "Content-Type": contentTypeFor(realFilePath),
      "Cache-Control": realFilePath === indexPath ? "no-store" : "public, max-age=31536000, immutable",
    });
    fs.createReadStream(realFilePath).pipe(response);
  });

  await new Promise<void>((resolve, reject) => {
    frontendStaticServer?.once("error", reject);
    frontendStaticServer?.listen(0, "127.0.0.1", () => resolve());
  });

  const address = frontendStaticServer.address();
  if (!address || typeof address === "string") {
    throw new Error("前端静态服务启动失败。");
  }
  frontendStaticUrl = `http://127.0.0.1:${address.port}`;
  return frontendStaticUrl;
}

async function startBackend(): Promise<BackendStatus> {
  if (backendProcess && !backendProcess.killed) {
    const ready = await waitForBackendReady(15_000);
    return { ...backendStatus, ready };
  }

  if (isDev) {
    // dev 模式下先尝试复用已有后端（用户可能手动启动了 uv run / python -m video_sum_service）
    const noBackend = process.env.BILISUM_NO_BACKEND === "1";
    if (noBackend) {
      console.log("[Backend] BILISUM_NO_BACKEND=1, skipping backend auto-start.");
      updateBackendStatus({
        running: false,
        ready: false,
        pid: null,
        lastError: "",
      });
      return backendStatus;
    }

    // 重试探测已有后端（每次 800ms，最多 4 次 ≈ 3.2s），避免因后端启动慢而误判
    if (await probeBackendReady(800)) {
      updateBackendStatus({ running: true, pid: null });
      return { ...backendStatus, running: true, ready: true, pid: null, lastError: "" };
    }
    for (let i = 0; i < 3; i++) {
      console.log("[Backend] Probe retry %d/3 for existing backend...", i + 1);
      if (await probeBackendReady(800)) {
        updateBackendStatus({ running: true, pid: null });
        return { ...backendStatus, running: true, ready: true, pid: null, lastError: "" };
      }
    }
    // 后端不可用 → 继续走本函数的自动 spawn 逻辑
  }
  if (!isDev && await probeBackendPortBusy(300)) {
    const message = "BiliSum 服务端口已被占用，请退出旧版 BiliSum 后重试。";
    updateBackendStatus({
      running: false,
      ready: false,
      pid: null,
      lastError: message,
    });
    loadSplash(message);
    return backendStatus;
  }

  let target: { command: string; args: string[]; cwd: string; forceHidden?: boolean };
  try {
    target = isDev ? resolveDevPython() : resolvePackagedBackend();
  } catch (error) {
    const message = error instanceof Error ? error.message : "后端启动目标解析失败。";
    console.error("[Backend] Failed to resolve backend target:", error);
    updateBackendStatus({
      running: false,
      ready: false,
      pid: null,
      lastError: message,
    });
    if (!isDev) {
      loadSplash(message);
    }
    return backendStatus;
  }

  console.log("[Backend] Starting backend with:", {
    command: target.command,
    args: target.args,
    cwd: target.cwd,
    isDev,
  });
  getDesktopAccessToken();

  updateSplashProgress(20);
  updateSplashMessage("正在启动后端服务...");

  // Windows 上隐藏控制台窗口
  const spawnOptions: SpawnOptions = {
    cwd: target.cwd,
    env: {
      ...process.env,
      VIDEO_SUM_HOST: "127.0.0.1",
      VIDEO_SUM_PORT: "3838",
      VIDEO_SUM_ACCESS_TOKEN: getDesktopAccessToken(),
      VIDEO_SUM_APP_DATA_ROOT: currentLocalDataRoot(),
      ...(isDev
        ? {
            PYTHONPATH: [
              ...getDevPythonPathEntries(),
              process.env.PYTHONPATH || "",
            ].filter(Boolean).join(path.delimiter),
          }
        : {}),
    },
    stdio: ["ignore", "ignore", "ignore"],  // 完全忽略子进程的 stdio，避免窗口弹出和管道阻塞
    detached: false,
    windowsHide: true,
    shell: false,
  };

  // 在 Windows 上，使用 CREATE_NO_WINDOW 标志隐藏控制台窗口
  // CREATE_NO_WINDOW = 0x08000000
  if (process.platform === "win32") {
    const CREATE_NO_WINDOW = 0x08000000;
    (spawnOptions as any).windowsVerbatimArguments = true;
    (spawnOptions as any).creationFlags = CREATE_NO_WINDOW;
    console.log("[Backend] Windows: setting creationFlags = CREATE_NO_WINDOW (0x08000000)");
  }

  try {
    backendProcess = spawn(target.command, target.args, spawnOptions);
  } catch (error) {
    const message = error instanceof Error ? error.message : "后端进程创建失败。";
    console.error("[Backend] Failed to spawn backend:", error);
    updateBackendStatus({
      running: false,
      ready: false,
      pid: null,
      lastError: message,
    });
    if (!isDev) {
      loadSplash(message);
    }
    return backendStatus;
  }

  backendProcess.once("error", (error) => {
    backendProcess = null;
    const message = error instanceof Error ? error.message : "后端进程启动失败。";
    console.error("[Backend] Child process error:", error);
    updateBackendStatus({
      running: false,
      ready: false,
      pid: null,
      lastError: message,
    });
    if (!forceQuit && !isDev) {
      loadSplash(message);
    }
  });

  backendProcess.once("exit", (_code, signal) => {
    backendProcess = null;
    updateBackendStatus({
      running: false,
      ready: false,
      pid: null,
      lastError: signal ? `Backend exited with signal ${signal}.` : "Backend exited.",
    });
    if (!forceQuit && !isDev) {
      loadSplash("BiliSum 服务已停止，正在等待重新启动。");
    }
  });

  updateBackendStatus({
    running: true,
    ready: false,
    pid: backendProcess.pid ?? null,
    lastError: "",
  });

  updateSplashProgress(40);
  updateSplashMessage("后端已启动，等待服务就绪...");

  const ready = await waitForBackendReady();
  if (!ready) {
    loadSplash("后端启动超时，请检查日志后重试。");
  }
  return { ...backendStatus, ready };
}

async function stopBackend(): Promise<BackendStatus> {
  if (!backendProcess) {
    updateBackendStatus({ running: false, ready: false, pid: null });
    return backendStatus;
  }
  const current = backendProcess;
  backendProcess = null;
  backendStoppingPromise = new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) {
        return;
      }
      settled = true;
      if (backendStoppingPromise) {
        backendStoppingPromise = null;
      }
      resolve();
    };
    current.once("exit", finish);
    current.once("error", finish);
    setTimeout(finish, 5_000);
  });
  current.kill();
  updateBackendStatus({
    running: false,
    ready: false,
    pid: null,
    lastError: "",
  });
  if (!isDev) {
    loadSplash("BiliSum 服务已停止。");
  }
  await backendStoppingPromise;
  return backendStatus;
}

async function loadApplication() {
  updateSplashProgress(100);
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }

  const targetUrl = isDev ? rendererUrl : await startPackagedFrontendServer();
  if (!targetUrl) {
    loadSplash();
    return;
  }
  if (applicationLoadedTarget === targetUrl) {
    if (!getStartupHidden()) {
      mainWindow.show();
    }
    return;
  }
  if (applicationLoadPromise) {
    await applicationLoadPromise;
    return;
  }

  applicationLoadPromise = (async () => {
    const splashRemainingMs = Math.max(0, minimumSplashVisibleMs - (Date.now() - splashShownAt));
    if (splashRemainingMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, splashRemainingMs));
    }
    if (isDev) {
      await mainWindow?.webContents.session.clearCache();
    }
    if (!mainWindow || mainWindow.isDestroyed()) {
      return;
    }
    mainWindow.setMinimumSize(mainWindowBounds.minWidth, mainWindowBounds.minHeight);
    mainWindow.setSize(mainWindowBounds.width, mainWindowBounds.height);
    mainWindow.setResizable(true);
    mainWindow.center();
    await mainWindow.loadURL(targetUrl);
    applicationLoadedTarget = targetUrl;

    if (!getStartupHidden()) {
      mainWindow.show();
    }
  })();

  try {
    await applicationLoadPromise;
  } finally {
    applicationLoadPromise = null;
  }
}

function getTrayImage() {
  for (const candidate of [iconPath, fallbackIconPath]) {
    const image = nativeImage.createFromPath(candidate);
    if (!image.isEmpty()) {
      return image;
    }
  }
  return nativeImage.createEmpty();
}

function setAutoLaunch(enabled: boolean): boolean {
  try {
    const loginSettings: Electron.Settings = { openAtLogin: enabled };
    // 如果开启静默启动，添加 --hidden 参数
    if (enabled && preferences.silentStart) {
      loginSettings.args = ['--hidden'];
    }
    app.setLoginItemSettings(loginSettings);
  } catch (err) {
    console.error("setLoginItemSettings failed:", err);
  }
  const actual = app.getLoginItemSettings().openAtLogin;
  preferences = { ...preferences, autoLaunch: actual };
  preferences = { ...preferences, autoLaunch: actual };
  savePreferences();
  rebuildTrayMenu();
  return actual;
}

function _trayIcon(svgMarkup: string): Electron.NativeImage {
  const dataUrl = `data:image/svg+xml,${encodeURIComponent(svgMarkup)}`;
  return nativeImage.createFromDataURL(dataUrl).resize({ width: 16, height: 16 });
}

function rebuildTrayMenu() {
  if (!tray) {
    return;
  }
  const prefs = getPreferences();
  const template: MenuItemConstructorOptions[] = [];

  // Simple 16x16 SVG icons for tray menu
  const ICON_SHOW = _trayIcon('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect x="1" y="2" width="14" height="11" rx="1" fill="none" stroke="#555" stroke-width="1.5"/><path d="M5 16V13M11 16V13" stroke="#555" stroke-width="1.5"/><line x1="2" y1="13" x2="14" y2="13" stroke="#555" stroke-width="1.5"/></svg>');
  const ICON_STOP  = _trayIcon('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="#e14b5a"/></svg>');
  const ICON_START = _trayIcon('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5" fill="none" stroke="#22a866" stroke-width="1.5"/><polygon points="6,5 6,11 12,8" fill="#22a866"/></svg>');
  const ICON_LOGS  = _trayIcon('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect x="2" y="1" width="12" height="14" rx="1" fill="none" stroke="#555" stroke-width="1.3"/><line x1="5" y1="5" x2="11" y2="5" stroke="#555" stroke-width="1.2"/><line x1="5" y1="8" x2="11" y2="8" stroke="#555" stroke-width="1.2"/><line x1="5" y1="11" x2="9" y2="11" stroke="#555" stroke-width="1.2"/></svg>');
  const ICON_EXIT  = _trayIcon('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><line x1="4" y1="4" x2="12" y2="12" stroke="#555" stroke-width="1.8" stroke-linecap="round"/><line x1="12" y1="4" x2="4" y2="12" stroke="#555" stroke-width="1.8" stroke-linecap="round"/></svg>');

  // Only show window toggle when the window is hidden/minimized
  if (!mainWindow?.isVisible()) {
    template.push({
      label: "显示主窗口",
      icon: ICON_SHOW,
      click: () => {
        mainWindow?.show();
        mainWindow?.focus();
      },
    });
  }

  template.push(
    {
      label: backendStatus.running ? "停止后端" : "启动后端",
      icon: backendStatus.running ? ICON_STOP : ICON_START,
      click: () => {
        void (backendStatus.running ? stopBackend() : startBackend());
      },
    },
    { type: "separator" },
    {
      label: "开机自启动",
      type: "checkbox",
      checked: prefs.autoLaunch,
      click: (menuItem) => {
        setAutoLaunch(Boolean(menuItem.checked));
      },
    },
    {
      label: "开机静默启动",
      type: "checkbox",
      checked: prefs.silentStart,
      click: (menuItem) => {
        preferences = { ...preferences, silentStart: Boolean(menuItem.checked) };
        savePreferences();
        rebuildTrayMenu();
      },
    },
    {
      label: "关闭窗口时",
      submenu: [
        {
          label: prefs.closeBehavior === "tray" ? "\u25CF 最小化到托盘" : "\u25CB 最小化到托盘",
          click: () => {
            preferences = { ...preferences, closeBehavior: "tray", rememberCloseBehavior: true };
            savePreferences();
            rebuildTrayMenu();
          },
        },
        {
          label: prefs.closeBehavior === "exit" ? "\u25CF 直接退出应用" : "\u25CB 直接退出应用",
          click: () => {
            preferences = { ...preferences, closeBehavior: "exit", rememberCloseBehavior: true };
            savePreferences();
            rebuildTrayMenu();
          },
        },
      ],
    },
    { type: "separator" },
    {
      label: "打开日志目录",
      icon: ICON_LOGS,
      click: () => {
        void shell.openPath(path.dirname(getServiceLogPath()));
      },
    },
    {
      label: "退出应用",
      icon: ICON_EXIT,
      click: () => {
        forceQuit = true;
        void stopBackend().finally(() => app.quit());
      },
    },
  );

  tray.setToolTip(`BiliSum ${app.getVersion()} · ${backendStatus.running ? "运行中" : "已停止"}`);
  tray.setContextMenu(Menu.buildFromTemplate(template));
}

async function handleCloseAction(): Promise<"hide" | "exit" | "cancel"> {
  const preferences = getPreferences();
  if (preferences.rememberCloseBehavior) {
    return preferences.closeBehavior === "exit" ? "exit" : "hide";
  }

  const result = await dialog.showMessageBox(mainWindow!, {
    type: "question",
    buttons: ["隐藏到托盘", "直接退出", "取消"],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
    checkboxLabel: "记住我的选择",
    title: "关闭 BiliSum",
    message: "关闭窗口时，你希望 BiliSum 如何处理？",
    detail: "隐藏到托盘后，后台任务会继续运行；直接退出会关闭桌面端并停止内置后端。",
  });

  if (result.response === 2) {
    return "cancel";
  }

  const behavior: CloseBehavior = result.response === 1 ? "exit" : "tray";
  if (result.checkboxChecked) {
    setCloseBehavior(behavior, true);
  }
  return behavior === "exit" ? "exit" : "hide";
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: splashWindowBounds.width,
    height: splashWindowBounds.height,
    minWidth: splashWindowBounds.width,
    minHeight: splashWindowBounds.height,
    show: false,
    resizable: false,
    roundedCorners: true,
    title: "BiliSum",
    icon: getTrayImage(),
    frame: false,
    titleBarStyle: "hidden",
    trafficLightPosition: { x: 12, y: 12 },
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#121212" : "#fafbfc",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  // 禁用鼠标中键导航（防止打开新窗口），但允许外部链接通过 shell.openExternal 打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalUrl(url)) {
      void shell.openExternal(url).catch(() => undefined);
    }
    // 阻止在 Electron 中创建新窗口
    return { action: "deny" };
  });

  // 拦截页面内导航，外部链接在系统浏览器中打开
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!mainWindow) {
      return;
    }
    // 如果是跨域导航（外链），在系统浏览器中打开
    if (isCrossOriginNavigation(url, mainWindow.webContents.getURL())) {
      event.preventDefault();
      if (isAllowedExternalUrl(url)) {
        void shell.openExternal(url).catch(() => undefined);
      }
    }
    // 同域导航允许正常进行
  });

  mainWindow.on("close", async (event) => {
    if (forceQuit) {
      return;
    }
    event.preventDefault();
    const action = await handleCloseAction();
    if (action === "cancel") {
      return;
    }
    if (action === "hide") {
      mainWindow?.hide();
      return;
    }
    forceQuit = true;
    await stopBackend();
    app.quit();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  loadSplash();
  if (!getStartupHidden()) {
    mainWindow.show();
  }

  // 如果开发者模式已开启，自动打开 DevTools
  if (preferences.developerMode) {
    mainWindow.webContents.openDevTools();
  }
}

function createTray() {
  if (tray) {
    return;
  }
  tray = new Tray(getTrayImage());
  tray.on("double-click", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
  rebuildTrayMenu();
}

// 更新管理器函数
function sendUpdateStatus() {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) {
    return;
  }
  mainWindow.webContents.send("desktop:update:status-changed", updateStatus);
}

function updateUpdateStatus(patch: Partial<UpdateInfo>) {
  updateStatus = { ...updateStatus, ...patch };
  sendUpdateStatus();
}

function getUpdateSnapshot(info?: Partial<ElectronUpdateInfo> | null): UpdateInfo {
  return {
    status: updateStatus.status,
    version: info?.version ?? updateStatus.version,
    releaseDate: info?.releaseDate ?? updateStatus.releaseDate,
    releaseNotes: (info?.releaseNotes as string | null | undefined) ?? updateStatus.releaseNotes,
    downloadProgress: updateStatus.downloadProgress,
    errorMessage: updateStatus.errorMessage,
  };
}

function getUpdaterUnavailableMessage() {
  return `当前安装包未包含自动更新配置：${updaterConfigPath}`;
}

function getAutoUpdaterDisabledMessage() {
  return "开发环境不支持桌面自动更新，请使用打包后的安装包进行验证。";
}

function canUseAutoUpdater() {
  return !isDev && fs.existsSync(updaterConfigPath);
}

function initializeUpdater() {
  // 开发环境下完全禁用 autoUpdater，避免读取 app-update.yml 文件
  if (isDev) {
    updateUpdateStatus({ status: "not-available", errorMessage: getAutoUpdaterDisabledMessage() });
    return;
  }

  if (!canUseAutoUpdater()) {
    updateUpdateStatus({
      status: "not-available",
      errorMessage: getUpdaterUnavailableMessage(),
    });
    return;
  }

  try {
    // 配置 autoUpdater
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.allowDowngrade = false;
    autoUpdater.allowPrerelease = false;
    if (process.platform === "darwin") {
      autoUpdater.disableDifferentialDownload = true;
    }

    // 监听更新事件
    autoUpdater.on("checking-for-update", () => {
      if (updateStatus.status === "downloaded" || updateStatus.status === "installing") {
        return;
      }
      updateUpdateStatus({ status: "checking", errorMessage: null });
    });

    autoUpdater.on("update-available", (info: ElectronUpdateInfo) => {
      pendingUpdateInfo = info;
      const alreadyDownloaded = downloadedUpdateVersion === info.version
        || (updateStatus.status === "downloaded" && updateStatus.version === info.version)
        || (updateStatus.status === "installing" && updateStatus.version === info.version);
      
      // 处理 releaseNotes：electron-updater 可能返回数组或字符串
      let releaseNotes: string | null = null;
      if (info.releaseNotes) {
        if (Array.isArray(info.releaseNotes)) {
          // 如果是数组，提取所有 note 内容并合并
          releaseNotes = info.releaseNotes
            .map((note) => (typeof note === "string" ? note : note.note || ""))
            .filter(Boolean)
            .join("\n\n");
        } else if (typeof info.releaseNotes === "string") {
          releaseNotes = info.releaseNotes;
        } else if (typeof info.releaseNotes === "object" && info.releaseNotes !== null) {
          // 如果是对象，尝试获取 note 属性
          releaseNotes = (info.releaseNotes as { note?: string }).note || null;
        }
      }
      
      updateUpdateStatus({
        ...getUpdateSnapshot(info),
        releaseNotes,
        status: alreadyDownloaded ? updateStatus.status : "available",
        downloadProgress: alreadyDownloaded ? 100 : 0,
        errorMessage: null,
      });
    });

    autoUpdater.on("update-not-available", (info: ElectronUpdateInfo) => {
      if (updateStatus.status === "downloaded" || updateStatus.status === "installing") {
        return;
      }
      // 当已是最新版本时，获取当前版本的更新日志
      let releaseNotes: string | null = null;
      if (info.releaseNotes) {
        if (Array.isArray(info.releaseNotes)) {
          releaseNotes = info.releaseNotes
            .map((note) => (typeof note === "string" ? note : note.note || ""))
            .filter(Boolean)
            .join("\n\n");
        } else if (typeof info.releaseNotes === "string") {
          releaseNotes = info.releaseNotes;
        } else if (typeof info.releaseNotes === "object" && info.releaseNotes !== null) {
          releaseNotes = (info.releaseNotes as { note?: string }).note || null;
        }
      }
      updateUpdateStatus({
        ...getUpdateSnapshot(info),
        status: "not-available",
        releaseNotes,
        errorMessage: null,
      });
    });

    autoUpdater.on("download-progress", (progress: ProgressInfo) => {
      updateUpdateStatus({
        status: "downloading",
        downloadProgress: progress.percent,
      });
    });

    autoUpdater.on("update-downloaded", (info: ElectronUpdateInfo) => {
      pendingUpdateInfo = info;
      downloadedUpdateVersion = info.version;
      updateUpdateStatus({
        ...getUpdateSnapshot(info),
        status: "downloaded",
        downloadProgress: 100,
      });
      if (installRequestedAfterDownload) {
        setTimeout(() => installAndRestart(), 250);
      }
    });

    autoUpdater.on("error", (error: Error) => {
      installRequestedAfterDownload = false;
      updateUpdateStatus({
        status: "error",
        errorMessage: error.message,
      });
    });

    // 启动时静默检查更新（延迟 5 秒）
    setTimeout(() => {
      if (!updateStatus.status || updateStatus.status === "idle") {
        checkForUpdates();
      }
    }, 5000);
  } catch (error) {
    // 如果初始化失败（例如缺少 app-update.yml），禁用更新功能
    console.error("Failed to initialize autoUpdater:", error);
    updateStatus.status = "error";
    updateStatus.errorMessage = error instanceof Error ? error.message : "更新系统初始化失败";
  }
}

function checkForUpdates(): Promise<UpdateInfo> {
  if (isDev) {
    updateUpdateStatus({
      status: "not-available",
      errorMessage: getAutoUpdaterDisabledMessage(),
      releaseNotes: null,
    });
    return Promise.resolve(updateStatus);
  }

  if (!canUseAutoUpdater()) {
    updateUpdateStatus({
      status: "not-available",
      errorMessage: getUpdaterUnavailableMessage(),
      releaseNotes: null,
    });
    return Promise.resolve(updateStatus);
  }

  if (updateStatus.status === "downloading" || updateStatus.status === "downloaded" || updateStatus.status === "installing") {
    return Promise.resolve(updateStatus);
  }

  if (checkForUpdatesPromise) {
    return checkForUpdatesPromise;
  }

  checkForUpdatesPromise = (async () => {
    try {
      await autoUpdater.checkForUpdates();
      return updateStatus;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "检查更新失败";
      updateUpdateStatus({ status: "error", errorMessage });
      return updateStatus;
    } finally {
      checkForUpdatesPromise = null;
    }
  })();

  return checkForUpdatesPromise;
}

function downloadUpdate(): Promise<UpdateInfo> {
  if (isDev) {
    const error = new Error(getAutoUpdaterDisabledMessage());
    updateUpdateStatus({ status: "not-available", errorMessage: error.message });
    return Promise.reject(error);
  }

  if (!canUseAutoUpdater()) {
    const error = new Error(getUpdaterUnavailableMessage());
    updateUpdateStatus({ status: "not-available", errorMessage: error.message });
    return Promise.reject(error);
  }

  if (updateStatus.status === "installing") {
    return Promise.resolve(updateStatus);
  }

  if (updateStatus.status === "downloaded") {
    installRequestedAfterDownload = true;
    installAndRestart();
    return Promise.resolve(updateStatus);
  }

  if (updateStatus.status === "downloading" && downloadUpdatePromise) {
    installRequestedAfterDownload = true;
    return downloadUpdatePromise;
  }

  if (updateStatus.status !== "available") {
    return Promise.reject(new Error("没有可用的更新"));
  }

  installRequestedAfterDownload = true;

  downloadUpdatePromise = (async () => {
    try {
      await autoUpdater.downloadUpdate();
      return updateStatus;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "下载更新失败";
      installRequestedAfterDownload = false;
      updateUpdateStatus({
        status: "error",
        errorMessage,
      });
      throw error;
    } finally {
      downloadUpdatePromise = null;
    }
  })();

  return downloadUpdatePromise;
}

async function installAndRestart(): Promise<void> {
  if (isDev || !canUseAutoUpdater()) {
    return;
  }
  if (updateStatus.status !== "downloaded") {
    console.warn("Cannot install: update not downloaded");
    return;
  }
  installRequestedAfterDownload = false;
  updateUpdateStatus({ status: "installing", errorMessage: null });
  try {
    if (backendProcess) {
      await stopBackend();
    } else if (backendStoppingPromise) {
      await backendStoppingPromise;
    } else {
      const portStillBusy = await probeBackendReady(300, false);
      if (portStillBusy) {
        throw new Error("后端端口仍被占用，无法安全安装更新。请退出旧版 BiliSum 后重试。");
      }
    }
    setTimeout(() => {
      quittingForUpdate = true;
      forceQuit = true;
      autoUpdater.quitAndInstall();
    }, 200);
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : "安装更新前停止后端失败";
    updateUpdateStatus({ status: "error", errorMessage });
  }
}

function registerIpcHandlers() {
  ipcMain.handle("desktop:app:get-version", () => desktopAppVersion || app.getVersion());
  ipcMain.handle("desktop:app:get-auto-launch", () => app.getLoginItemSettings().openAtLogin);
  ipcMain.handle("desktop:app:set-auto-launch", (_event, enabled: boolean) => setAutoLaunch(Boolean(enabled)));
  ipcMain.handle("desktop:app:get-startup-announcement", () => getStartupAnnouncement());
  ipcMain.handle("desktop:app:mark-startup-announcement-seen", (_event, version: string) => markStartupAnnouncementSeen(version));
  ipcMain.handle("desktop:window:show", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
  ipcMain.handle("desktop:window:minimize", () => mainWindow?.minimize());
  ipcMain.handle("desktop:window:maximize", () => {
    if (mainWindow?.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow?.maximize();
    }
  });
  ipcMain.handle("desktop:window:close", () => mainWindow?.close());
  ipcMain.handle("desktop:window:isMaximized", () => mainWindow?.isMaximized() ?? false);
  ipcMain.handle("desktop:backend:start", async () => startBackend());
  ipcMain.handle("desktop:backend:stop", async () => stopBackend());
  ipcMain.handle("desktop:backend:status", () => backendStatus);
  ipcMain.handle("desktop:backend:get-access-token", () => getDesktopAccessToken());
  ipcMain.handle("desktop:clipboard:write-image", (_event, dataUrl: string) => {
    const image = nativeImage.createFromDataURL(dataUrl);
    if (image.isEmpty()) {
      throw new Error("图片写入剪贴板失败。");
    }
    clipboard.writeImage(image);
  });
  async function pickMediaFiles() {
    const dialogOptions: OpenDialogOptions = {
      title: "选择本地视频或音频",
      properties: ["openFile", "multiSelections"],
      filters: [
        {
          name: "媒体文件",
          extensions: [
            "mp4",
            "mov",
            "mkv",
            "avi",
            "wmv",
            "webm",
            "flv",
            "m4v",
            "ts",
            "mpeg",
            "mpg",
            "mp3",
            "wav",
            "m4a",
            "aac",
            "flac",
            "ogg",
          ],
        },
      ],
    };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, dialogOptions)
      : await dialog.showOpenDialog(dialogOptions);
    return result.canceled ? [] : result.filePaths;
  }

  ipcMain.handle("desktop:media:pick-video-files", async () => {
    return pickMediaFiles();
  });
  ipcMain.handle("desktop:media:pick-video-file", async () => {
    const filePaths = await pickMediaFiles();
    return filePaths[0] ?? null;
  });
  ipcMain.handle("desktop:bilibili:capture-login-cookies", async () => openBilibiliLoginAndCaptureCookies());
  ipcMain.handle("desktop:shell:open-path", (_event, targetPath: string) => shell.openPath(targetPath));
  ipcMain.handle("desktop:dialog:pick-directory", async (_event, defaultPath?: string, title?: string) => {
    const dialogOptions: OpenDialogOptions = {
      title: typeof title === "string" && title.trim() ? title.slice(0, 80) : "选择导出目录",
      properties: ["openDirectory", "createDirectory"],
    };
    if (defaultPath && fs.existsSync(defaultPath)) {
      dialogOptions.defaultPath = defaultPath;
    }
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, dialogOptions)
      : await dialog.showOpenDialog(dialogOptions);
    return result.canceled ? null : result.filePaths[0] ?? null;
  });
  ipcMain.handle("desktop:logs:get-service-log-path", () => getServiceLogPath());
  ipcMain.handle("desktop:logs:read-service-log-tail", (_event, lines = 200) => readServiceLogTail(lines));
  ipcMain.handle("desktop:logs:clear", () => {
    const logPath = getServiceLogPath();
    fs.writeFileSync(logPath, "", "utf-8");
    return true;
  });
  ipcMain.handle("desktop:logs:export", async () => {
    const result = await dialog.showSaveDialog(mainWindow!, {
      title: "导出日志",
      defaultPath: `bilisum-${new Date().toISOString().slice(0, 10)}.log`,
      filters: [{ name: "日志文件", extensions: ["log", "txt"] }],
    });
    if (result.canceled || !result.filePath) return null;
    fs.copyFileSync(getServiceLogPath(), result.filePath);
    return result.filePath;
  });
  ipcMain.handle("desktop:preferences:get-close-behavior", () => getPreferences().closeBehavior);
  ipcMain.handle("desktop:preferences:set-silent-start", (_event, enabled: boolean) => {
    preferences = { ...preferences, silentStart: Boolean(enabled) };
    savePreferences();
    rebuildTrayMenu();
    // 如果开启了自启动，更新启动参数
    if (preferences.autoLaunch) {
      setAutoLaunch(true);
    }
    return preferences.silentStart;
  });
  ipcMain.handle("desktop:preferences:get-silent-start", () => getPreferences().silentStart);
  ipcMain.handle("desktop:preferences:get-crash-auto-restart", () => getPreferences().crashAutoRestart);
  ipcMain.handle("desktop:preferences:set-crash-auto-restart", (_event, enabled: boolean) => {
    preferences = { ...preferences, crashAutoRestart: Boolean(enabled) };
    savePreferences();
    return preferences.crashAutoRestart;
  });
  ipcMain.handle("desktop:preferences:get-developer-mode", () => getPreferences().developerMode);
  ipcMain.handle("desktop:preferences:set-developer-mode", (_event, enabled: boolean) => {
    preferences = { ...preferences, developerMode: Boolean(enabled) };
    savePreferences();
    // 开启或关闭开发者工具
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (enabled) {
        mainWindow.webContents.openDevTools();
      } else {
        mainWindow.webContents.closeDevTools();
      }
    }
    return preferences.developerMode;
  });
  ipcMain.handle("desktop:preferences:set-close-behavior", (_event, value: CloseBehavior) => {
    setCloseBehavior(value, true);
    return value;
  });
  ipcMain.handle("desktop:preferences:reset-close-behavior", () => resetCloseBehavior());
  ipcMain.handle("desktop:preferences:set-theme", (_event, value: ThemePreference) => {
    if (value !== "light" && value !== "dark") {
      return getPreferences().themePreference ?? "light";
    }
    return setThemePreference(value);
  });
  
  // 更新相关 IPC
  ipcMain.handle("desktop:update:check", async () => checkForUpdates());
  ipcMain.handle("desktop:update:download", async () => downloadUpdate());
  ipcMain.handle("desktop:update:install", () => installAndRestart());
  ipcMain.handle("desktop:update:get-status", () => updateStatus);
  ipcMain.handle("desktop:file-manager:get-storage-overview", async (_event, input: StorageOverviewInput) => getStorageOverview(input));
  ipcMain.handle("desktop:file-manager:cleanup-orphans", async (_event, input: StorageCleanupInput) => cleanupOrphans(input));
  ipcMain.handle("desktop:file-manager:preview-migration", async (_event, input: StorageMigrationRequest) => createStorageMigrationPreview(input));
  ipcMain.handle("desktop:file-manager:start-migration", async (event, input: StorageMigrationRequest) => startStorageMigration(input, event.sender));
  ipcMain.handle("desktop:file-manager:cleanup-migrated-source", async () => cleanupPreviousStorageLocation());
  ipcMain.handle("desktop:file-manager:keep-migrated-source", () => keepPreviousStorageLocation());
  ipcMain.handle("desktop:file-manager:has-pending-migration-cleanup", () => hasPendingStorageMigrationCleanup());
  ipcMain.handle("desktop:file-manager:open-directory", async (_event, kind: StorageLocationKind) => {
    const targetPath = resolveDirectoryByKind(kind, await getTrustedStorageLocations());
    fs.mkdirSync(targetPath, { recursive: true });
    return shell.openPath(targetPath);
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
}

app.whenReady().then(async () => {
  registerIpcHandlers();
  recordOpenedVersionIfNoAnnouncement();
  initializeUpdater();
  createWindow();
  createTray();

  if (preferences.autoLaunch && !app.getLoginItemSettings().openAtLogin) {
    setAutoLaunch(true);
  }

  void loadApplication();
  void startBackend();

  app.on("activate", async () => {
    if (!mainWindow) {
      createWindow();
    }
    await loadApplication();
    mainWindow?.show();
  });
});

app.on("before-quit", (event) => {
  if (quittingForUpdate) {
    forceQuit = true;
    frontendStaticServer?.close();
    return;
  }
  if (backendProcess && !quitAfterBackendStop) {
    event.preventDefault();
    quitAfterBackendStop = true;
    void stopBackend().finally(() => {
      forceQuit = true;
      app.quit();
    });
    return;
  }
  forceQuit = true;
  frontendStaticServer?.close();
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill();
  }
  // 清理 autoUpdater 监听器，防止在应用退出后仍触发
  autoUpdater.removeAllListeners();
});

app.on("window-all-closed", () => {
  if (forceQuit) {
    app.quit();
  }
});
