"use strict";

/**
 * Managed Python runtime helpers for the BiliSum CLI.
 *
 * These functions were extracted from bin/bilisum.js so that task commands
 * (summarize / transcribe / ...) can reuse the same venv bootstrapping used by
 * `bilisum start`. Behavior is identical to the original implementation.
 */

const { spawnSync } = require("node:child_process");
const { existsSync, mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const { dirname, join } = require("node:path");

const PACKAGE_ROOT = join(__dirname, "..");
const RUNTIME_ROOT = join(PACKAGE_ROOT, "runtime");

function readVersion() {
  const packagePath = join(PACKAGE_ROOT, "package.json");
  if (!existsSync(packagePath)) {
    return "unknown";
  }
  return JSON.parse(readFileSync(packagePath, "utf8")).version || "unknown";
}

function defaultBaseDir() {
  if (process.env.BILISUM_NPX_HOME) {
    return process.env.BILISUM_NPX_HOME;
  }
  if (process.platform === "win32") {
    return join(process.env.LOCALAPPDATA || process.env.USERPROFILE || process.cwd(), "BiliSum", "npx");
  }
  return join(process.env.XDG_DATA_HOME || join(process.env.HOME || process.cwd(), ".local", "share"), "bilisum", "npx");
}

function defaultDataDir() {
  return join(defaultBaseDir(), "data");
}

function venvDir() {
  return join(defaultBaseDir(), `venv-${readVersion()}`);
}

function pythonInVenv() {
  return process.platform === "win32" ? join(venvDir(), "Scripts", "python.exe") : join(venvDir(), "bin", "python");
}

function findPython(explicitPython) {
  const candidates = [];
  if (explicitPython) {
    candidates.push({ command: explicitPython, args: [] });
  }
  if (process.platform === "win32") {
    candidates.push({ command: "py", args: ["-3.12"] });
  }
  candidates.push({ command: "python3.12", args: [] });
  candidates.push({ command: "python3", args: [] });
  candidates.push({ command: "python", args: [] });

  for (const candidate of candidates) {
    const check = spawnSync(candidate.command, [
      ...candidate.args,
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)",
    ], { stdio: "ignore", windowsHide: true });
    if (check.status === 0) {
      return candidate;
    }
  }

  throw new Error("Python 3.12 was not found. Install Python 3.12 or pass --python <path>.");
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

function ensureRuntime() {
  if (!existsSync(join(RUNTIME_ROOT, "apps", "service", "pyproject.toml"))) {
    throw new Error("BiliSum runtime files are missing from this npm package. Please reinstall bilisum.");
  }
  if (!existsSync(join(RUNTIME_ROOT, "apps", "web", "static", "index.html"))) {
    throw new Error("BiliSum web static files are missing from this npm package. Please reinstall bilisum.");
  }
}

function ensureVenv(options) {
  ensureRuntime();
  const pythonPath = pythonInVenv();
  const marker = join(venvDir(), ".bilisum-installed");
  if (existsSync(pythonPath) && existsSync(marker) && !options.reinstall) {
    return pythonPath;
  }

  const sourcePython = findPython(options.python);
  mkdirSync(dirname(venvDir()), { recursive: true });

  console.error("Preparing BiliSum Python runtime. This may take a minute on first run...");
  runChecked(sourcePython.command, [...sourcePython.args, "-m", "venv", venvDir()]);

  runChecked(pythonPath, ["-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "hatchling"]);
  runChecked(pythonPath, [
    "-m",
    "pip",
    "install",
    join(RUNTIME_ROOT, "packages", "infra"),
    join(RUNTIME_ROOT, "packages", "core"),
    join(RUNTIME_ROOT, "apps", "service"),
  ]);

  mkdirSync(venvDir(), { recursive: true });
  writeFileSync(marker, readVersion(), "utf8");
  return pythonPath;
}

module.exports = {
  PACKAGE_ROOT,
  RUNTIME_ROOT,
  readVersion,
  defaultBaseDir,
  defaultDataDir,
  venvDir,
  pythonInVenv,
  findPython,
  runChecked,
  ensureRuntime,
  ensureVenv,
};
