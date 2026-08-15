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
const { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const { ApiError, baseUrlFor, health } = require("./api");
const { findPython } = require("./runtime");

const SERVICE_NAME = "BiliSum";
const CLI_RUNTIME_FILE = "cli-runtime.json";
const DEFAULT_STARTUP_TIMEOUT_MS = 300_000;
const HEALTH_POLL_INTERVAL_MS = 1000;

function cliRuntimeFilePath(dataRoot) {
  return join(dataRoot, CLI_RUNTIME_FILE);
}

/**
 * Build the environment for a spawned service. The service derives
 * data/cache/tasks/db from VIDEO_SUM_APP_DATA_ROOT (same as the desktop app).
 */
function buildServiceEnv(options, dataRoot) {
  const webStaticDir = join(dataRoot, "cli-web-static");
  mkdirSync(webStaticDir, { recursive: true });
  const env = {
    ...process.env,
    VIDEO_SUM_HOST: options.host,
    VIDEO_SUM_PORT: options.port,
    VIDEO_SUM_APP_DATA_ROOT: dataRoot,
    VIDEO_SUM_WEB_STATIC_DIR: webStaticDir,
  };
  for (const item of options.env || []) {
    const equals = item.indexOf("=");
    if (equals <= 0) {
      throw new Error(`Invalid --env value: ${item}`);
    }
    env[item.slice(0, equals)] = item.slice(equals + 1);
  }
  return env;
}

function writeCliRuntimeFile(dataRoot, payload) {
  mkdirSync(dataRoot, { recursive: true });
  writeFileSync(cliRuntimeFilePath(dataRoot), JSON.stringify(payload, null, 2), "utf8");
}

function readCliRuntimeFile(dataRoot) {
  const path = cliRuntimeFilePath(dataRoot);
  if (!existsSync(path)) {
    return null;
  }
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function removeCliRuntimeFile(dataRoot) {
  try {
    rmSync(cliRuntimeFilePath(dataRoot), { force: true });
  } catch {
    // Best effort cleanup.
  }
}

/**
 * Start the service with the desktop data root. Returns { child, url }.
 * `background: true` detaches and ignores stdio; otherwise stdio is inherited
 * and process exit is tied to the child.
 */
function spawnService(options, { background = false } = {}) {
  const dataRoot = options.data;
  const python = options.python
    ? { command: options.python, args: [] }
    : findPython(dataRoot);
  if (!python) {
    throw new Error(
      "没有找到可用的 Python 3.12。请先安装桌面版 BiliSum（自带运行时），" +
      "或安装 Python 3.12 后重试。",
    );
  }

  const env = buildServiceEnv(options, dataRoot);
  const url = baseUrlFor(options.host, options.port);

  const child = spawn(python.command, [...python.args, "-m", "video_sum_service"], {
    env,
    cwd: dataRoot,
    stdio: background ? "ignore" : "inherit",
    windowsHide: true,
    detached: background,
  });

  writeCliRuntimeFile(dataRoot, {
    pid: child.pid,
    python: [python.command, ...python.args].join(" "),
    host: options.host,
    port: String(options.port),
    dataRoot,
    startedAt: new Date().toISOString(),
  });

  if (background) {
    child.unref();
  } else {
    child.on("exit", (code, signal) => {
      removeCliRuntimeFile(dataRoot);
      if (signal) {
        process.kill(process.pid, signal);
        return;
      }
      process.exitCode = code || 0;
    });
  }

  return { child, url };
}

async function waitForHealth(baseUrl, { timeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, onTick } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
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
    if (onTick) {
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

/**
 * Make sure a BiliSum service is reachable. If none is running, start one
 * headlessly sharing the desktop data root. Returns { started, payload }.
 */
async function ensureService(options, { startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, log = console.error } = {}) {
  const url = baseUrlFor(options.host, options.port);
  if (await isServiceReachable(url)) {
    return { started: false, payload: await health(url) };
  }

  log(`BiliSum 服务未在 ${url} 运行，正在用桌面端数据目录后台启动...`);
  const { child } = spawnService(options, { background: true });
  const earlyExit = new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve({ code, signal }));
    child.once("error", (error) => resolve({ error }));
  });

  let payload;
  try {
    payload = await Promise.race([
      waitForHealth(url, {
        timeoutMs: startupTimeoutMs,
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
            "请先运行 `bilisum start` 前台启动查看日志，或直接打开桌面端 BiliSum。",
          );
        }
        if (info && info.error) {
          throw new Error(`启动 BiliSum 服务失败：${info.error.message}`);
        }
        return null;
      }),
    ]);
  } catch (error) {
    removeCliRuntimeFile(options.data);
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
  const runtimeInfo = readCliRuntimeFile(options.data);
  if (runtimeInfo && runtimeInfo.pid) {
    const killed = killPid(runtimeInfo.pid);
    removeCliRuntimeFile(options.data);
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
      `BiliSum 服务正在 ${url} 运行，但它不是由 CLI 启动的（没有 cli-runtime.json 记录）。` +
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
};
