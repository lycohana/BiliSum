"use strict";

/**
 * Locating the BiliSum desktop data root and a usable Python interpreter.
 *
 * The CLI is a thin client for the existing BiliSum program (desktop app or a
 * service started by the user). It does NOT bundle a Python runtime anymore:
 *
 * - Data root defaults to the same location the desktop app uses
 *   (Windows: %LOCALAPPDATA%\bilisum, macOS: ~/Library/Application Support/bilisum),
 *   overridable with BILISUM_DATA_ROOT or --data.
 * - When no service is reachable, the CLI can start one with the desktop's
 *   managed runtime first, then any system Python 3.12.
 */

const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

const PACKAGE_ROOT = join(__dirname, "..");
const APP_SLUG = "bilisum";

function readVersion() {
  const packagePath = join(PACKAGE_ROOT, "package.json");
  if (!existsSync(packagePath)) {
    return "unknown";
  }
  return JSON.parse(readFileSync(packagePath, "utf8")).version || "unknown";
}

function localAppDataDir() {
  if (process.platform === "darwin") {
    return join(process.env.HOME || "", "Library", "Application Support");
  }
  if (process.platform === "win32") {
    return process.env.LOCALAPPDATA || join(process.env.USERPROFILE || "", "AppData", "Local");
  }
  return process.env.XDG_DATA_HOME || join(process.env.HOME || "", ".local", "share");
}

/**
 * Desktop app data root (the same value the desktop main process passes as
 * VIDEO_SUM_APP_DATA_ROOT). The service keeps its DB/tasks/cache under
 * `<root>/data` and its managed runtime under `<root>/runtime`.
 */
function locateBilisumDataRoot() {
  const override = process.env.BILISUM_DATA_ROOT || "";
  if (override.trim()) {
    return override.trim();
  }
  return join(localAppDataDir(), APP_SLUG);
}

/** Paths where the desktop app may have written its access token. */
function desktopUserDataCandidates() {
  const appData = process.platform === "win32"
    ? process.env.APPDATA || join(process.env.USERPROFILE || "", "AppData", "Roaming")
    : process.platform === "darwin"
      ? join(process.env.HOME || "", "Library", "Application Support")
      : process.env.XDG_CONFIG_HOME || join(process.env.HOME || "", ".config");
  return [
    join(appData, "BiliSum", "access-token.json"),
    join(appData, "bilisum-desktop", "access-token.json"),
  ];
}

function pythonCandidatesForRuntime(runtimeRoot) {
  return [
    join(runtimeRoot, "python.exe"),
    join(runtimeRoot, "Scripts", "python.exe"),
    join(runtimeRoot, "bin", "python"),
  ];
}

/**
 * Find a Python 3.12+ interpreter. Priority:
 * 1. Desktop managed runtime under `<dataRoot>/runtime/<channel>` (base first,
 *    then any gpu-* channel).
 * 2. System Python (py -3.12 / python3.12 / python3 / python).
 * Returns { command, args } compatible with spawn, or null.
 */
function findPython(dataRoot) {
  const runtimeRoot = join(dataRoot, "runtime");
  const channels = [];
  for (const name of ["base", "gpu-cu128", "gpu-cu126", "gpu-cu124"]) {
    channels.push(name);
  }
  for (const channel of channels) {
    for (const candidate of pythonCandidatesForRuntime(join(runtimeRoot, channel))) {
      if (existsSync(candidate)) {
        return { command: candidate, args: [] };
      }
    }
  }

  const systemCandidates = [];
  if (process.platform === "win32") {
    systemCandidates.push({ command: "py", args: ["-3.12"] });
  }
  systemCandidates.push({ command: "python3.12", args: [] });
  systemCandidates.push({ command: "python3", args: [] });
  systemCandidates.push({ command: "python", args: [] });

  for (const candidate of systemCandidates) {
    const check = spawnSync(candidate.command, [
      ...candidate.args,
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)",
    ], { stdio: "ignore", windowsHide: true });
    if (check.status === 0) {
      return candidate;
    }
  }
  return null;
}

module.exports = {
  APP_SLUG,
  readVersion,
  localAppDataDir,
  locateBilisumDataRoot,
  desktopUserDataCandidates,
  findPython,
};
