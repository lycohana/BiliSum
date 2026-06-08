#!/usr/bin/env node

const { spawn, spawnSync } = require("node:child_process");
const { existsSync, mkdirSync, readFileSync } = require("node:fs");
const { dirname, join } = require("node:path");

const RELEASES_URL = "https://github.com/qianshengli/vidmind/releases/latest";
const REPO_URL = "https://github.com/qianshengli/vidmind";
const DEFAULT_PORT = "3838";
const PACKAGE_ROOT = join(__dirname, "..");
const RUNTIME_ROOT = join(PACKAGE_ROOT, "runtime");

function readVersion() {
  const packagePath = join(PACKAGE_ROOT, "package.json");
  if (!existsSync(packagePath)) {
    return "unknown";
  }
  return JSON.parse(readFileSync(packagePath, "utf8")).version || "unknown";
}

function printHelp() {
  const version = readVersion();
  console.log(`VidMind ${version}`);
  console.log("");
  console.log("AI 视频总结与知识库工具，深度优化 B 站体验，同时支持 YouTube 和本地视频。");
  console.log("");
  console.log("Usage:");
  console.log("  npx vidmind                         Start the local VidMind service");
  console.log("  npx vidmind start [options]          Start the local VidMind service");
  console.log("  npx vidmind doctor                   Check Python/runtime setup");
  console.log("  npx vidmind --version                Print package version");
  console.log("  npx vidmind release                  Open the latest GitHub release");
  console.log("");
  console.log("Options:");
  console.log("  --host <host>                        Host, default 127.0.0.1");
  console.log("  --port <port>                        Port, default 3838");
  console.log("  --python <path>                      Python 3.12 executable");
  console.log("  --data <path>                        Data directory");
  console.log("  --env KEY=VALUE                      Pass an environment variable");
  console.log("  --reinstall                          Recreate the managed Python venv");
  console.log("  --no-open                            Do not open the browser");
  console.log("");
  console.log(`Latest release: ${RELEASES_URL}`);
  console.log(`Repository:     ${REPO_URL}`);
}

function parseStartOptions(args) {
  const options = {
    host: process.env.VIDMIND_HOST || "127.0.0.1",
    port: process.env.VIDMIND_PORT || DEFAULT_PORT,
    python: process.env.VIDMIND_PYTHON || "",
    data: process.env.VIDMIND_DATA || defaultDataDir(),
    env: [],
    reinstall: false,
    open: true,
  };

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readOptionValue(args, ++index, arg);
    } else if (arg === "--port" || arg === "-p") {
      options.port = readOptionValue(args, ++index, arg);
    } else if (arg === "--python") {
      options.python = readOptionValue(args, ++index, arg);
    } else if (arg === "--data") {
      options.data = readOptionValue(args, ++index, arg);
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readOptionValue(args, ++index, arg));
    } else if (arg === "--reinstall") {
      options.reinstall = true;
    } else if (arg === "--no-open") {
      options.open = false;
    } else {
      throw new Error(`Unknown option: ${arg}`);
    }
  }

  return options;
}

function readOptionValue(args, index, option) {
  const value = args[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${option} requires a value`);
  }
  return value;
}

function defaultBaseDir() {
  if (process.env.VIDMIND_NPX_HOME) {
    return process.env.VIDMIND_NPX_HOME;
  }
  if (process.platform === "win32") {
    return join(process.env.LOCALAPPDATA || process.env.USERPROFILE || process.cwd(), "VidMind", "npx");
  }
  return join(process.env.XDG_DATA_HOME || join(process.env.HOME || process.cwd(), ".local", "share"), "vidmind", "npx");
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
    throw new Error("VidMind runtime files are missing from this npm package. Please reinstall vidmind.");
  }
  if (!existsSync(join(RUNTIME_ROOT, "apps", "web", "static", "index.html"))) {
    throw new Error("VidMind web static files are missing from this npm package. Please reinstall vidmind.");
  }
}

function ensureVenv(options) {
  ensureRuntime();
  const pythonPath = pythonInVenv();
  const marker = join(venvDir(), ".vidmind-installed");
  if (existsSync(pythonPath) && existsSync(marker) && !options.reinstall) {
    return pythonPath;
  }

  const sourcePython = findPython(options.python);
  mkdirSync(dirname(venvDir()), { recursive: true });

  console.log("Preparing VidMind Python runtime. This may take a minute on first run...");
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
  require("node:fs").writeFileSync(marker, readVersion(), "utf8");
  return pythonPath;
}

function startService(args) {
  const options = parseStartOptions(args);
  const pythonPath = ensureVenv(options);
  const dataDir = options.data;
  const cacheDir = join(dataDir, "cache");
  const tasksDir = join(dataDir, "tasks");
  mkdirSync(cacheDir, { recursive: true });
  mkdirSync(tasksDir, { recursive: true });

  const env = {
    ...process.env,
    VIDEO_SUM_HOST: options.host,
    VIDEO_SUM_PORT: options.port,
    VIDEO_SUM_APP_DATA_ROOT: dataDir,
    VIDEO_SUM_DATA_DIR: dataDir,
    VIDEO_SUM_CACHE_DIR: cacheDir,
    VIDEO_SUM_TASKS_DIR: tasksDir,
    VIDEO_SUM_DATABASE_URL: `sqlite:///${join(dataDir, "video_sum.db").replace(/\\/g, "/")}`,
    VIDEO_SUM_WEB_STATIC_DIR: join(RUNTIME_ROOT, "apps", "web", "static"),
  };
  for (const item of options.env) {
    const equals = item.indexOf("=");
    if (equals <= 0) {
      throw new Error(`Invalid --env value: ${item}`);
    }
    env[item.slice(0, equals)] = item.slice(equals + 1);
  }

  const url = `http://${options.host}:${options.port}`;
  console.log(`Starting VidMind at ${url}`);
  console.log(`Data directory: ${dataDir}`);
  console.log("");

  const child = spawn(pythonPath, ["-m", "video_sum_service"], {
    env,
    stdio: "inherit",
    windowsHide: false,
  });

  if (options.open) {
    setTimeout(() => openUrl(url), 1800);
  }

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code || 0;
  });
}

function doctor(args) {
  const options = parseStartOptions(args);
  ensureRuntime();
  const python = findPython(options.python);
  console.log(`VidMind package: ${readVersion()}`);
  console.log(`Python command: ${[python.command, ...python.args].join(" ")}`);
  console.log(`Runtime root:   ${RUNTIME_ROOT}`);
  console.log(`Venv:           ${venvDir()}`);
  console.log(`Data:           ${options.data}`);
}

function openUrl(url) {
  const platform = process.platform;
  const command = platform === "win32" ? "cmd" : platform === "darwin" ? "open" : "xdg-open";
  const args = platform === "win32" ? ["/c", "start", "", url] : [url];
  const child = spawn(command, args, {
    detached: true,
    stdio: "ignore",
    windowsHide: true,
  });
  child.on("error", () => {
    console.log(url);
  });
  child.unref();
}

function main() {
  const [command, ...args] = process.argv.slice(2);

  try {
    if (!command) {
      startService([]);
      return;
    }

    if (command === "start" || command === "serve") {
      startService(args);
      return;
    }

    if (command === "doctor") {
      doctor(args);
      return;
    }

    if (command === "help" || command === "-h" || command === "--help") {
      printHelp();
      return;
    }

    if (command === "version" || command === "-v" || command === "--version") {
      console.log(readVersion());
      return;
    }

    if (command === "release" || command === "releases" || command === "download") {
      openUrl(RELEASES_URL);
      return;
    }

    if (command === "repo" || command === "github") {
      openUrl(REPO_URL);
      return;
    }

    throw new Error(`Unknown command: ${command}`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    console.error("");
    printHelp();
    process.exitCode = 1;
  }
}

main();