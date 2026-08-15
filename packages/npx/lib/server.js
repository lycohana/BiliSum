"use strict";

/**
 * Service lifecycle for the BiliSum CLI (thin-client mode).
 *
 * The CLI talks to the existing BiliSum program — normally the desktop app's
 * backend on http://127.0.0.1:3838. When nothing is reachable there, task
 * commands may start a headless service that shares the desktop data root
 * (`VIDEO_SUM_APP_DATA_ROOT` = desktop app data root), so it uses the same
 * database, tasks, cache and auth as the desktop app.
 *
 * The spawned process pid is recorded in `<dataRoot>/cli-runtime.json` so
 * `bilisum stop` can shut it down.
 */

const { spawn } = require("node:child_process");
const { existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const { ApiError, baseUrlFor, health } = require("./api");
const { findPython, ensureCliVenv, runtimeSourceDir } = require("./runtime");

const SERVICE_NAME = "BiliSum";
const DEFAULT_STARTUP_TIMEOUT_MS = 300_000;
const HEALTH_POLL_INTERVAL_MS = 1000;

/** Per-port runtime record, so several CLI-started services can coexist. */
function cliRuntimeFilePath(dataRoot, port) {
  return join(dataRoot, `cli-runtime-${port}.json`);
}

/** Background service log (stdout+stderr), per port. */
function cliServiceLogPath(dataRoot, port) {
  return join(dataRoot, `cli-service-${port}.log`);
}

/**
 * Read the service's persisted settings ({dataRoot}/data/settings.json).
 * The service overlays these over environment variables, so a persisted
 * `port` (or `data_dir`) wins over what the CLI passes via env. Returns null
 * when the file is absent or unreadable.
 */
function readServiceSettings(dataRoot) {
  const path = join(dataRoot, "data", "settings.json");
  if (!existsSync(path)) {
    return null;
  }
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

/** Validate CLI --port/--data against persisted service settings. */
function assertSpawnablePort(options) {
  const settings = readServiceSettings(options.data);
  if (!settings) {
    return;
  }
  const persistedPort = String(settings.port ?? "").trim();
  if (persistedPort && persistedPort !== String(options.port)) {
    throw new Error(
      `数据根 ${options.data} 的 settings.json 中服务端口为 ${persistedPort}，与 --port ${options.port} 冲突。` +
      "服务会按持久化设置启动（与桌面端一致）。请去掉 --port，或使用 --data 指定不含该设置的数据根。",
    );
  }
}

/**
 * Build the environment for a spawned service. The service derives
 * data/cache/tasks/db from VIDEO_SUM_APP_DATA_ROOT (same as the desktop app).
 * When an access token is known, it is injected like the desktop app does
 * (VIDEO_SUM_ACCESS_TOKEN), so the CLI and the spawned service always agree.
 */
function buildServiceEnv(options, dataRoot, accessToken = null) {
  const webStaticDir = join(dataRoot, "cli-web-static");
  mkdirSync(webStaticDir, { recursive: true });
  const env = {
    ...process.env,
    VIDEO_SUM_HOST: options.host,
    VIDEO_SUM_PORT: options.port,
    VIDEO_SUM_APP_DATA_ROOT: dataRoot,
    VIDEO_SUM_WEB_STATIC_DIR: webStaticDir,
  };
  if (accessToken) {
    env.VIDEO_SUM_ACCESS_TOKEN = accessToken;
  }
  for (const item of options.env || []) {
    const equals = item.indexOf("=");
    if (equals <= 0) {
      throw new Error(`Invalid --env value: ${item}`);
    }
    env[item.slice(0, equals)] = item.slice(equals + 1);
  }
  return env;
}

function writeCliRuntimeFile(dataRoot, port, payload) {
  mkdirSync(dataRoot, { recursive: true });
  writeFileSync(cliRuntimeFilePath(dataRoot, port), JSON.stringify(payload, null, 2), "utf8");
}

function readCliRuntimeFile(dataRoot, port) {
  const path = cliRuntimeFilePath(dataRoot, port);
  if (!existsSync(path)) {
    return null;
  }
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function removeCliRuntimeFile(dataRoot, port) {
  try {
    rmSync(cliRuntimeFilePath(dataRoot, port), { force: true });
  } catch {
    // Best effort cleanup.
  }
}

/**
 * Start the service. In desktop mode the desktop-managed runtime (or system
 * Python) is used; in cli mode the CLI's own venv (built from the bundled
 * runtime sources) is used. Returns { child, url, logPath }.
 * `background: true` detaches and ignores stdio; otherwise stdio is inherited
 * and process exit is tied to the child.
 */
function spawnService(options, { background = false, accessToken = null, log = console.error } = {}) {
  assertSpawnablePort(options);
  const dataRoot = options.data;
  const env = buildServiceEnv(options, dataRoot, accessToken);

  let python;
  if (options.environment === "cli") {
    // Standalone mode: the CLI venv serves the bundled runtime sources.
    python = { command: ensureCliVenv(options, log), args: [] };
    const staticDir = join(runtimeSourceDir(), "apps", "web", "static");
    if (existsSync(staticDir)) {
      env.VIDEO_SUM_WEB_STATIC_DIR = staticDir;
    }
  } else {
    python = options.python ? { command: options.python, args: [] } : findPython(dataRoot);
    if (!python) {
      throw new Error(
        "没有找到可用的 Python 3.12。请先安装桌面版 BiliSum（自带运行时），" +
        "或安装 Python 3.12 后重试，或使用 cli 环境（bilisum env use cli）。",
      );
    }
  }

  if (background) {
    // CLI-managed background service: auto shut down after `idleTimeout`
    // seconds without activity AND without active tasks. The desktop app never
    // sets these vars, so it can never be affected.
    env.VIDEO_SUM_CLI_MANAGED = "1";
    env.VIDEO_SUM_CLI_IDLE_TIMEOUT_SECONDS = String(options.idleTimeout ?? 600);
  }
  const url = baseUrlFor(options.host, options.port);
  const logPath = cliServiceLogPath(dataRoot, options.port);

  let stdio;
  if (background) {
    mkdirSync(dataRoot, { recursive: true });
    const logFd = openSync(logPath, "a");
    stdio = ["ignore", logFd, logFd];
  } else {
    stdio = "inherit";
  }

  const child = spawn(python.command, [...python.args, "-m", "video_sum_service"], {
    env,
    cwd: dataRoot,
    stdio,
    windowsHide: true,
    detached: background,
  });

  writeCliRuntimeFile(dataRoot, options.port, {
    pid: child.pid,
    python: [python.command, ...python.args].join(" "),
    host: options.host,
    port: String(options.port),
    dataRoot,
    logPath,
    startedAt: new Date().toISOString(),
  });

  if (background) {
    child.unref();
  } else {
    child.on("exit", (code, signal) => {
      removeCliRuntimeFile(dataRoot, options.port);
      if (signal) {
        process.kill(process.pid, signal);
        return;
      }
      process.exitCode = code || 0;
    });
  }

  return { child, url, logPath };
}

async function waitForHealth(baseUrl, { timeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, onTick, signal } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  let lastTickAt = 0;
  while (Date.now() < deadline) {
    if (signal && signal.aborted) {
      throw new Error("服务启动已中止（子进程提前退出）。");
    }
    try {
      const payload = await health(baseUrl);
      if (payload && String(payload.service || "").trim() === SERVICE_NAME) {
        return payload;
      }
      if (payload && payload.service) {
        throw new Error(
          `${baseUrl} 上已运行其他服务（service=${payload.service}），不是 BiliSum。请用 --host/--port 指定 BiliSum 服务地址。`,
        );
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 0) {
        lastError = error;
      } else {
        throw error;
      }
    }
    if (onTick && Date.now() - lastTickAt >= 5000) {
      lastTickAt = Date.now();
      onTick(lastError);
    }
    await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_INTERVAL_MS));
  }
  throw new Error(
    `等待 BiliSum 服务启动超时（${Math.round(timeoutMs / 1000)}s）。` +
    (lastError ? ` 最近一次错误：${lastError.message}` : ""),
  );
}

async function isServiceReachable(baseUrl) {
  try {
    const payload = await health(baseUrl);
    return Boolean(payload && String(payload.service || "").trim() === SERVICE_NAME);
  } catch {
    return false;
  }
}

function readLogTail(logPath, maxLines = 3) {
  try {
    const content = readFileSync(logPath, "utf8");
    return content.split(/\r?\n/).filter(Boolean).slice(-maxLines).join(" | ");
  } catch {
    return "";
  }
}

function buildStartupFailureHint(logPath) {
  const tail = readLogTail(logPath);
  if (/No module named video_sum_service/.test(tail)) {
    return (
      "检测到系统 Python 未安装 BiliSum 服务依赖（No module named video_sum_service）。" +
      "请安装桌面版 BiliSum（自带运行时），或给该 Python 安装依赖后重试。"
    );
  }
  return `详细日志：${logPath}`;
}

/**
 * Make sure a BiliSum service is reachable. If none is running, start one
 * headlessly sharing the desktop data root. When `accessToken` is known it is
 * injected into the spawned service so CLI and service always agree.
 * Returns { started, payload }.
 */
async function ensureService(options, { startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, log = console.error, accessToken = null } = {}) {
  const url = baseUrlFor(options.host, options.port);
  if (await isServiceReachable(url)) {
    return { started: false, payload: await health(url) };
  }

  log(`BiliSum 服务未在 ${url} 运行，正在用桌面端数据目录后台启动...`);
  const { child, logPath } = spawnService(options, { background: true, accessToken });
  const controller = new AbortController();
  const earlyExit = new Promise((resolve) => {
    child.once("exit", (code, signal) => {
      controller.abort();
      resolve({ code, signal });
    });
    child.once("error", (error) => {
      controller.abort();
      resolve({ error });
    });
  });

  let payload;
  try {
    payload = await Promise.race([
      waitForHealth(url, {
        timeoutMs: startupTimeoutMs,
        signal: controller.signal,
        onTick: (lastError) => {
          if (lastError) {
            log(`等待服务就绪中（${lastError.message}）`);
          }
        },
      }),
      earlyExit.then((info) => {
        if (info && info.code !== null && info.code !== undefined) {
          throw new Error(
            `BiliSum 服务启动后立即退出（exit code=${info.code}${info.signal ? `, signal=${info.signal}` : ""}）。` +
            `${buildStartupFailureHint(logPath)}。也可以运行 \`bilisum start\` 前台启动查看输出，或直接打开桌面端 BiliSum。`,
          );
        }
        if (info && info.error) {
          throw new Error(`启动 BiliSum 服务失败：${info.error.message}（日志：${logPath}）`);
        }
        return null;
      }),
    ]);
  } catch (error) {
    removeCliRuntimeFile(options.data, options.port);
    throw error;
  }
  log(`BiliSum 服务已就绪：${url}`);
  return { started: true, payload };
}

function killPid(pid) {
  if (!pid) {
    return false;
  }
  try {
    if (process.platform === "win32") {
      const { spawnSync } = require("node:child_process");
      const result = spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      return result.status === 0;
    }
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      process.kill(pid, "SIGKILL");
    }
    return true;
  } catch {
    return false;
  }
}

async function stopService(options, { log = console.error } = {}) {
  const runtimeInfo = readCliRuntimeFile(options.data, options.port);
  if (runtimeInfo && runtimeInfo.pid) {
    const killed = killPid(runtimeInfo.pid);
    removeCliRuntimeFile(options.data, options.port);
    if (killed) {
      log(`已停止 CLI 启动的 BiliSum 服务（pid=${runtimeInfo.pid}）。`);
    } else {
      log(`未找到进程 pid=${runtimeInfo.pid}，已清理运行记录。`);
    }
    return;
  }

  const url = baseUrlFor(options.host, options.port);
  if (await isServiceReachable(url)) {
    throw new Error(
      `BiliSum 服务正在 ${url} 运行，但它不是由 CLI 启动的（没有 cli-runtime-${options.port}.json 记录）。` +
      "请直接关闭桌面端 BiliSum，或用 --data 指定正确的数据根目录。",
    );
  }
  log("没有检测到由 CLI 启动的 BiliSum 服务。");
}

module.exports = {
  SERVICE_NAME,
  buildServiceEnv,
  spawnService,
  ensureService,
  stopService,
  waitForHealth,
  isServiceReachable,
  readCliRuntimeFile,
  writeCliRuntimeFile,
  removeCliRuntimeFile,
  cliRuntimeFilePath,
  cliServiceLogPath,
  readServiceSettings,
  assertSpawnablePort,
};
