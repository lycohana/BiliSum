"use strict";

/**
 * Paths and Python resolution for the BiliSum CLI.
 *
 * Two environments share this module:
 * - desktop: the installed BiliSum app (data root under %LOCALAPPDATA%\bilisum,
 *   managed runtime under `<root>/runtime`). The CLI is a thin client.
 * - cli: standalone mode — the CLI owns its data root (CLI_HOME), builds its
 *   own venv from the Python sources bundled in the npm package (runtime/),
 *   and runs its own service. Works without any installed BiliSum.
 */

const { existsSync, mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const { join, resolve } = require("node:path");
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

/** CLI home: own data root for the standalone (cli) environment. */
function cliHomeDir() {
  const override = process.env.BILISUM_CLI_HOME || "";
  if (override.trim()) {
    return override.trim();
  }
  if (process.platform === "win32") {
    return join(
      process.env.LOCALAPPDATA || join(process.env.USERPROFILE || "", "AppData", "Local"),
      "BiliSum",
      "cli",
    );
  }
  return join(process.env.HOME || "", ".bilisum");
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

/**
 * Possible managed-runtime Python interpreters, mirroring the desktop app's
 * runtime_python_candidates() (packages/infra/.../runtime.py):
 * Windows portable builds use python.exe / Scripts\python.exe; macOS/Linux
 * runtimes use bin/python, bin/python3 or a bare `python` at the root.
 */
function pythonCandidatesForRuntime(runtimeRoot) {
  return [
    join(runtimeRoot, "python.exe"),
    join(runtimeRoot, "Scripts", "python.exe"),
    join(runtimeRoot, "bin", "python"),
    join(runtimeRoot, "bin", "python3"),
    join(runtimeRoot, "python"),
  ];
}

function isPython312OrNewer(command, args) {
  const check = spawnSync(command, [
    ...args,
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)",
  ], { stdio: "ignore", windowsHide: true });
  return check.status === 0;
}

/** System Python 3.12+ candidates only (used to bootstrap the CLI venv). */
function findSystemPython() {
  const systemCandidates = [];
  if (process.platform === "win32") {
    systemCandidates.push({ command: "py", args: ["-3.12"] });
  }
  systemCandidates.push({ command: "python3.12", args: [] });
  systemCandidates.push({ command: "python3", args: [] });
  systemCandidates.push({ command: "python", args: [] });

  for (const candidate of systemCandidates) {
    if (isPython312OrNewer(candidate.command, candidate.args)) {
      return candidate;
    }
  }
  return null;
}

/**
 * Find a Python 3.12+ interpreter for the desktop environment. Priority:
 * 1. Desktop managed runtime under `<dataRoot>/runtime/<channel>`.
 * 2. System Python.
 */
function findPython(dataRoot) {
  const runtimeRoot = join(dataRoot, "runtime");
  const channels = ["base", "gpu-cu128", "gpu-cu126", "gpu-cu124"];
  for (const channel of channels) {
    for (const candidate of pythonCandidatesForRuntime(join(runtimeRoot, channel))) {
      if (existsSync(candidate) && isPython312OrNewer(candidate, [])) {
        return { command: candidate, args: [] };
      }
    }
  }
  return findSystemPython();
}

function isRuntimeSourceRoot(path) {
  return existsSync(join(path, "apps", "service", "pyproject.toml"));
}

/**
 * Python/runtime sources for both supported layouts:
 * - published package: packages/npx/runtime (created by prepack)
 * - repository checkout: repository root (runtime is removed by postpack)
 */
function runtimeSourceDir() {
  const packagedRoot = join(PACKAGE_ROOT, "runtime");
  if (isRuntimeSourceRoot(packagedRoot)) {
    return packagedRoot;
  }

  const repositoryRoot = resolve(PACKAGE_ROOT, "..", "..");
  if (isRuntimeSourceRoot(repositoryRoot)) {
    return repositoryRoot;
  }

  // Keep the packaged path in error messages for an incomplete npm install.
  return packagedRoot;
}

/**
 * Resolve the installable Agent Skill from the published package or the
 * repository checkout used during development.
 */
function skillSourceDir() {
  const packagedSkill = join(PACKAGE_ROOT, "skill", "bilisum-video-understanding");
  if (existsSync(join(packagedSkill, "SKILL.md"))) {
    return packagedSkill;
  }

  const repositoryRoot = resolve(PACKAGE_ROOT, "..", "..");
  const repositorySkill = join(repositoryRoot, ".agents", "skills", "bilisum-video-understanding");
  if (existsSync(join(repositorySkill, "SKILL.md"))) {
    return repositorySkill;
  }

  return packagedSkill;
}

/** CLI venv lives under CLI_HOME/venv. */
function cliVenvDir() {
  return join(cliHomeDir(), "venv");
}

function cliVenvPython() {
  return process.platform === "win32"
    ? join(cliVenvDir(), "Scripts", "python.exe")
    : join(cliVenvDir(), "bin", "python");
}

function ensureRuntime() {
  if (!existsSync(join(runtimeSourceDir(), "apps", "service", "pyproject.toml"))) {
    throw new Error("BiliSum runtime files are missing from this npm package. Please reinstall bilisum.");
  }
}

function runChecked(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    windowsHide: false,
    ...options,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed with exit code ${result.status}`);
  }
}

/**
 * Make sure the CLI standalone venv exists and the bundled packages are
 * installed into it. Returns the venv python path. First run creates the venv
 * and installs dependencies (may take a few minutes).
 */
function ensureCliVenv(options, log) {
  const pythonPath = cliVenvPython();
  const marker = join(cliVenvDir(), ".bilisum-cli-installed");
  if (existsSync(pythonPath) && existsSync(marker)) {
    return pythonPath;
  }

  ensureRuntime();
  const system = options.python ? { command: options.python, args: [] } : findSystemPython();
  if (!system) {
    throw new Error(
      "CLI 独立环境需要 Python 3.12 来初始化运行时，但未找到。请安装 Python 3.12 后重试，或改用 desktop 环境连接桌面端。",
    );
  }

  mkdirSync(cliHomeDir(), { recursive: true });
  const runtimeRoot = runtimeSourceDir();
  log("正在初始化 CLI 独立环境（首次运行需创建虚拟环境并安装依赖，可能需要几分钟）...");
  runChecked(system.command, [...system.args, "-m", "venv", cliVenvDir()]);
  runChecked(pythonPath, ["-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "hatchling"]);
  runChecked(pythonPath, [
    "-m",
    "pip",
    "install",
    join(runtimeRoot, "packages", "infra"),
    join(runtimeRoot, "packages", "core"),
    join(runtimeRoot, "apps", "service"),
  ]);
  writeFileSync(marker, readVersion(), "utf8");
  log("CLI 独立环境就绪。");
  return pythonPath;
}

module.exports = {
  APP_SLUG,
  PACKAGE_ROOT,
  readVersion,
  localAppDataDir,
  locateBilisumDataRoot,
  cliHomeDir,
  desktopUserDataCandidates,
  pythonCandidatesForRuntime,
  findSystemPython,
  findPython,
  runtimeSourceDir,
  skillSourceDir,
  cliVenvDir,
  cliVenvPython,
  ensureRuntime,
  ensureCliVenv,
  runChecked,
};
