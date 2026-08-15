"use strict";

/**
 * Service lifecycle for the BiliSum CLI.
 *
 * Task commands need a reachable service. If none is running on the target
 * host/port, the CLI starts one headlessly in the background (reusing the same
 * managed venv as `bilisum start`) and waits for /health to come up. The
 * spawned process pid is recorded in {dataDir}/runtime.json so `bilisum stop`
 * can shut it down later.
 */

const { spawn } = require("node:child_process");
const { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const { ApiError, baseUrlFor, health } = require("./api");
const { ensureVenv } = require("./runtime");

const SERVICE_NAME = "BiliSum";
const RUNTIME_FILE_NAME = "runtime.json";
const DEFAULT_STARTUP_TIMEOUT_MS = 300_000;
const HEALTH_POLL_INTERVAL_MS = 1000;

function runtimeFilePath(dataDir) {
  return join(dataDir, RUNTIME_FILE_NAME);
}

function buildServiceEnv(options, dataDir) {
  const cacheDir = join(dataDir, "cache");
  const tasksDir = join(dataDir, "tasks");
  const env = {
    ...process.env,
    VIDEO_SUM_HOST: options.host,
    VIDEO_SUM_PORT: options.port,
    VIDEO_SUM_APP_DATA_ROOT: dataDir,
    VIDEO_SUM_DATA_DIR: dataDir,
    VIDEO_SUM_CACHE_DIR: cacheDir,
    VIDEO_SUM_TASKS_DIR: tasksDir,
    VIDEO_SUM_DATABASE_URL: `sqlite:///${join(dataDir, "video_sum.db").replace(/\\/g, "/")}`,
    VIDEO_SUM_WEB_STATIC_DIR: join(require("./runtime").RUNTIME_ROOT, "apps", "web", "static"),
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

function writeRuntimeFile(dataDir, payload) {
  mkdirSync(dataDir, { recursive: true });
  writeFileSync(runtimeFilePath(dataDir), JSON.stringify(payload, null, 2), "utf8");
}

function readRuntimeFile(dataDir) {
  const path = runtimeFilePath(dataDir);
  if (!existsSync(path)) {
    return null;
  }
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function removeRuntimeFile(dataDir) {
  try {
    rmSync(runtimeFilePath(dataDir), { force: true });
  } catch {
    // Best effort cleanup.
  }
}

function spawnService(options, { background = false, open = false } = {}) {
  const pythonPath = ensureVenv(options);
  const dataDir = options.data;
  const cacheDir = join(dataDir, "cache");
  const tasksDir = join(dataDir, "tasks");
  mkdirSync(cacheDir, { recursive: true });
  mkdirSync(tasksDir, { recursive: true });

  const env = buildServiceEnv(options, dataDir);
  const url = baseUrlFor(options.host, options.port);

  const child = spawn(pythonPath, ["-m", "video_sum_service"], {
    env,
    stdio: background ? "ignore" : "inherit",
    windowsHide: false,
    detached: background,
  });

  writeRuntimeFile(dataDir, {
    pid: child.pid,
    host: options.host,
    port: String(options.port),
    dataDir,
    startedAt: new Date().toISOString(),
  });

  if (background) {
    child.unref();
  } else {
    child.on("exit", (code, signal) => {
      removeRuntimeFile(dataDir);
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
 * Make sure a BiliSum service is reachable, starting one headlessly if needed.
 * Returns { started, payload } where payload is the /health response.
 */
async function ensureService(options, { startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, log = console.error } = {}) {
  const url = baseUrlFor(options.host, options.port);
  if (await isServiceReachable(url)) {
    return { started: false, payload: await health(url) };
  }

  log(`BiliSum 服务未在 ${url} 运行，正在后台启动...`);
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
            "请先运行 `bilisum start` 前台启动查看日志。",
          );
        }
        if (info && info.error) {
          throw new Error(`启动 BiliSum 服务失败：${info.error.message}`);
        }
        return null;
      }),
    ]);
  } catch (error) {
    removeRuntimeFile(options.data);
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
  const runtimeInfo = readRuntimeFile(options.data);
  if (runtimeInfo && runtimeInfo.pid) {
    const killed = killPid(runtimeInfo.pid);
    removeRuntimeFile(options.data);
    if (killed) {
      log(`已停止 BiliSum 服务（pid=${runtimeInfo.pid}）。`);
    } else {
      log(`未找到进程 pid=${runtimeInfo.pid}，已清理运行记录。`);
    }
    return;
  }

  const url = baseUrlFor(options.host, options.port);
  if (await isServiceReachable(url)) {
    throw new Error(
      `BiliSum 服务正在 ${url} 运行，但它不是由 CLI 启动的（没有运行记录）。` +
      "请手动停止它，或用 --data 指定 CLI 管理的数据目录。",
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
  readRuntimeFile,
  writeRuntimeFile,
  removeRuntimeFile,
  runtimeFilePath,
};
