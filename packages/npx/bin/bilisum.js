#!/usr/bin/env node

"use strict";

const { spawn } = require("node:child_process");
const { existsSync } = require("node:fs");

const {
  baseUrlFor,
  getTask,
  listTasks,
  probeVideo,
  uploadLocalMedia,
  createVideoTask,
  ApiError,
} = require("../lib/api");
const { UsageError, parseTaskCommandArgs } = require("../lib/args");
const { formatTasks, emit } = require("../lib/output");
const { readVersion } = require("../lib/runtime");
const { readToken } = require("../lib/token");
const {
  ensureService,
  stopService,
  spawnService,
} = require("../lib/server");

const RELEASES_URL = "https://github.com/lycohana/BiliSum/releases/latest";
const REPO_URL = "https://github.com/lycohana/BiliSum";
const DEFAULT_PORT = "3838";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function printHelp() {
  const version = readVersion();
  console.log(`BiliSum ${version}`);
  console.log("");
  console.log("AI 视频总结与知识库工具，深度优化 B 站体验，同时支持 YouTube 和本地视频。");
  console.log("");
  console.log("Usage:");
  console.log("  npx bilisum                                 Start the local BiliSum service");
  console.log("  npx bilisum start [options]                 Start the local BiliSum service");
  console.log("  npx bilisum summarize <url|file> [options]  Summarize a video (agent-friendly)");
  console.log("  npx bilisum transcribe <url|file> [options] Print the transcript of a video");
  console.log("  npx bilisum status <task-id> [--json]       Show task status");
  console.log("  npx bilisum tasks [--limit N] [--json]      List tasks");
  console.log("  npx bilisum stop                            Stop the CLI-started BiliSum service");
  console.log("  npx bilisum doctor                          Check Python/runtime setup");
  console.log("  npx bilisum --version                       Print package version");
  console.log("  npx bilisum release                         Open the latest GitHub release");
  console.log("");
  console.log("Options (start/summarize/transcribe):");
  console.log("  --host <host>                        Host, default 127.0.0.1");
  console.log("  --port <port>                        Port, default 3838");
  console.log("  --python <path>                      Python 3.12 executable");
  console.log("  --data <path>                        Data directory");
  console.log("  --token <token>                      Access token (default: env or auth.json)");
  console.log("  --env KEY=VALUE                      Pass an environment variable");
  console.log("  --reinstall                          Recreate the managed Python venv");
  console.log("");
  console.log("Options (summarize/transcribe):");
  console.log("  --page <n>                           Process a specific P for multi-P videos");
  console.log("  --all-pages                          Process every P of a multi-P video");
  console.log("  --visual-note <mode>                 text | frame_insert | vlm_integrated");
  console.log("  --prompt-preset <id>                 Use a summary prompt preset");
  console.log("  --format <fmt>                       json | markdown | transcript (default markdown)");
  console.log("  --output <path> / -o <path>          Write result to a file instead of stdout");
  console.log("  --no-wait                            Create the task and exit immediately");
  console.log("  --timeout <sec>                      Max wait for completion, default 3600");
  console.log("  --startup-timeout <sec>              Max wait for service startup, default 300");
  console.log("  --quiet / -q                         Suppress progress output (stderr)");
  console.log("");
  console.log("If no BiliSum service is running, summarize/transcribe/status start one");
  console.log("headlessly in the background. Use `bilisum stop` to shut it down.");
  console.log("");
  console.log(`Latest release: ${RELEASES_URL}`);
  console.log(`Repository:     ${REPO_URL}`);
}

function parseConnectionOptions(args, { allowTaskId = false } = {}) {
  const options = {
    host: process.env.BILISUM_HOST || "127.0.0.1",
    port: process.env.BILISUM_PORT || DEFAULT_PORT,
    python: process.env.BILISUM_PYTHON || "",
    data: process.env.BILISUM_DATA || require("../lib/runtime").defaultDataDir(),
    token: process.env.BILISUM_TOKEN || "",
    env: [],
    reinstall: false,
    json: false,
    limit: 20,
    help: false,
  };
  const positional = [];
  const readValue = (index, option) => {
    const value = args[index];
    if (!value || value.startsWith("--")) {
      throw new UsageError(`${option} requires a value`);
    }
    return value;
  };
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readValue(++index, arg);
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(++index, arg);
    } else if (arg === "--python") {
      options.python = readValue(++index, arg);
    } else if (arg === "--data") {
      options.data = readValue(++index, arg);
    } else if (arg === "--token") {
      options.token = readValue(++index, arg);
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readValue(++index, arg));
    } else if (arg === "--reinstall") {
      options.reinstall = true;
    } else if (arg === "--json") {
      options.json = true;
    } else if (arg === "--limit") {
      options.limit = Number.parseInt(readValue(++index, arg), 10);
      if (!Number.isFinite(options.limit) || options.limit <= 0) {
        throw new UsageError("--limit 需要是正整数");
      }
    } else if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg.startsWith("-")) {
      throw new UsageError(`Unknown option: ${arg}`);
    } else {
      positional.push(arg);
    }
  }
  if (!allowTaskId && positional.length > 0) {
    throw new UsageError(`Unexpected argument: ${positional[0]}`);
  }
  return { options, positional };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractUrlPageParam(source) {
  try {
    const parsed = new URL(source);
    const raw = parsed.searchParams.get("p");
    if (!raw) {
      return null;
    }
    const page = Number.parseInt(raw, 10);
    return Number.isFinite(page) && page > 0 ? page : null;
  } catch {
    return null;
  }
}

async function ensureAuthenticatedService(options, log) {
  const token = readToken(options);
  if (!token) {
    throw new UsageError(
      "无法获取访问令牌：请用 --token 指定，或设置 VIDEO_SUM_ACCESS_TOKEN，" +
      "或使用 CLI 管理的数据目录（--data 默认值）让服务自动生成 auth.json。",
    );
  }
  await ensureService(options, { log });
  return token;
}

async function resolveAndCreateTasks(source, options, token, baseUrl, log) {
  const isUrl = /^https?:\/\//i.test(source);
  let video;
  if (isUrl) {
    log(`探测视频信息：${source}`);
    const probe = await probeVideo(baseUrl, token, source);
    video = probe.video;
  } else {
    if (!existsSync(source)) {
      throw new UsageError(`本地文件不存在：${source}`);
    }
    log(`上传本地媒体：${source}`);
    const probe = await uploadLocalMedia(baseUrl, token, source);
    video = probe.video;
  }

  const pages = video.pages || [];
  const pageNumbers = [];
  if (options.allPages) {
    if (!pages.length) {
      log("当前视频没有分 P，按单个任务处理。");
      pageNumbers.push(null);
    } else {
      for (const page of pages) {
        pageNumbers.push(page.page);
      }
    }
  } else if (options.page !== null) {
    if (pages.length && !pages.some((page) => page.page === options.page)) {
      throw new UsageError(
        `分 P ${options.page} 不存在。可选分 P：${pages.map((page) => page.page).join(", ")}`,
      );
    }
    pageNumbers.push(options.page);
  } else {
    const urlPage = isUrl ? extractUrlPageParam(source) : null;
    if (urlPage !== null && pages.length && !pages.some((page) => page.page === urlPage)) {
      throw new UsageError(`URL 指定的分 P ${urlPage} 不存在。可选分 P：${pages.map((page) => page.page).join(", ")}`);
    }
    pageNumbers.push(urlPage);
  }

  const created = [];
  for (const pageNumber of pageNumbers) {
    log(`提交任务：${video.title || source}${pageNumber ? `（P${pageNumber}）` : ""}`);
    const task = await createVideoTask(baseUrl, token, video.video_id, {
      pageNumber,
      visualNoteMode: options.visualNote,
      promptPresetId: options.promptPreset,
    });
    created.push(task);
    log(`任务已创建：${task.task_id}`);
  }
  return created;
}

async function waitForTasks(taskIds, options, token, baseUrl, log) {
  const deadline = Date.now() + options.timeoutSeconds * 1000;
  const seenProgress = new Set();
  const states = new Map(taskIds.map((id) => [id, { status: "queued" }]));

  while (true) {
    const allTerminal = [];
    for (const taskId of taskIds) {
      const state = states.get(taskId);
      if (TERMINAL_STATUSES.has(state.status)) {
        allTerminal.push(true);
        continue;
      }
      let progress = null;
      try {
        progress = await getTask(baseUrl, token, taskId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          throw new Error(`任务不存在：${taskId}`);
        }
        throw error;
      }
      state.status = progress.status;
      states.set(taskId, state);
      if (!TERMINAL_STATUSES.has(progress.status)) {
        const marker = `${taskId}:${progress.status}`;
        if (!seenProgress.has(marker)) {
          seenProgress.add(marker);
          log(`[${taskId}] ${progress.status}（${progress.title || "视频"}）`);
        }
      }
      allTerminal.push(TERMINAL_STATUSES.has(progress.status));
    }

    if (allTerminal.length && allTerminal.every(Boolean)) {
      const results = [];
      for (const taskId of taskIds) {
        results.push(await getTask(baseUrl, token, taskId));
      }
      return results;
    }
    if (Date.now() > deadline) {
      throw new Error(
        `任务在 ${options.timeoutSeconds}s 内未完成。可运行 bilisum status ${taskIds.join(" ")} 继续查看进度。`,
      );
    }
    await sleep(2000);
  }
}

async function runTaskCommand(args, { defaultFormat }) {
  const { source, options } = parseTaskCommandArgs(args, { defaultFormat });
  if (options.help) {
    printHelp();
    return;
  }
  const log = options.quiet ? () => {} : console.error;
  const baseUrl = baseUrlFor(options.host, options.port);
  const token = await ensureAuthenticatedService(options, log);
  const tasks = await resolveAndCreateTasks(source, options, token, baseUrl, log);

  if (!options.wait) {
    for (const task of tasks) {
      log(`任务已创建（不等待）：${task.task_id}`);
    }
    emit(
      formatTasks(
        tasks.map((task) => ({ ...task, result: null })),
        "json",
      ),
      options,
    );
    return;
  }

  const completed = await waitForTasks(
    tasks.map((task) => task.task_id),
    options,
    token,
    baseUrl,
    log,
  );
  const failed = completed.filter((task) => task.status === "failed" || task.status === "cancelled");
  for (const task of failed) {
    log(`[${task.task_id}] ${task.status}：${task.error_message || "未知错误"}`);
  }
  if (failed.length) {
    emit(formatTasks(completed, "json"), options);
    throw new Error(`${failed.length} 个任务失败。`);
  }
  const written = emit(formatTasks(completed, options.format), options);
  if (written) {
    log(written);
  }
}

async function statusCommand(args) {
  const { options, positional } = parseConnectionOptions(args, { allowTaskId: true });
  if (options.help) {
    printHelp();
    return;
  }
  if (positional.length === 0) {
    throw new UsageError("缺少 task-id。用法：bilisum status <task-id> [--json]");
  }
  const log = console.error;
  const baseUrl = baseUrlFor(options.host, options.port);
  const token = await ensureAuthenticatedService(options, log);
  const taskId = positional[0];
  let task;
  try {
    task = await getTask(baseUrl, token, taskId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      throw new Error(`任务不存在：${taskId}`);
    }
    throw error;
  }
  if (options.json) {
    process.stdout.write(`${JSON.stringify(task, null, 2)}\n`);
    return;
  }
  console.log(`task_id:       ${task.task_id}`);
  console.log(`status:        ${task.status}`);
  if (task.title) {
    console.log(`title:         ${task.title}`);
  }
  if (task.page_number) {
    console.log(`page_number:   ${task.page_number}`);
  }
  if (task.error_message) {
    console.log(`error:         ${task.error_message}`);
  }
  if (task.result && task.result.overview) {
    console.log(`overview:      ${String(task.result.overview).slice(0, 200)}`);
  }
}

async function tasksCommand(args) {
  const { options } = parseConnectionOptions(args);
  if (options.help) {
    printHelp();
    return;
  }
  const log = console.error;
  const baseUrl = baseUrlFor(options.host, options.port);
  const token = await ensureAuthenticatedService(options, log);
  const tasks = await listTasks(baseUrl, token);
  const limited = tasks.slice(0, options.limit);
  if (options.json) {
    process.stdout.write(`${JSON.stringify(limited, null, 2)}\n`);
    return;
  }
  if (!limited.length) {
    console.log("暂无任务。");
    return;
  }
  for (const task of limited) {
    const title = task.title || task.source || "";
    console.log(`${task.task_id}  ${task.status.padEnd(10)} ${title}`);
  }
  if (tasks.length > limited.length) {
    console.log(`（共 ${tasks.length} 个任务，仅显示前 ${limited.length} 个。用 --limit 调整。）`);
  }
}

function startService(args) {
  const { options } = parseConnectionOptions(args);
  const dataDir = options.data;
  const url = `http://${options.host}:${options.port}`;
  console.log(`Starting BiliSum at ${url}`);
  console.log(`Data directory: ${dataDir}`);
  console.log("");
  const { child } = spawnService(options, { background: false });
  setTimeout(() => openUrl(url), 1800);
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code || 0;
  });
}

function doctor(args) {
  const { options } = parseConnectionOptions(args);
  const runtime = require("../lib/runtime");
  runtime.ensureRuntime();
  const python = runtime.findPython(options.python);
  console.log(`BiliSum package: ${runtime.readVersion()}`);
  console.log(`Python command: ${[python.command, ...python.args].join(" ")}`);
  console.log(`Runtime root:   ${runtime.RUNTIME_ROOT}`);
  console.log(`Venv:           ${runtime.venvDir()}`);
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

async function main() {
  const [command, ...args] = process.argv.slice(2);

  try {
    if (!command || command === "start" || command === "serve") {
      startService(args);
      return;
    }
    if (command === "summarize" || command === "sum") {
      await runTaskCommand(args, { defaultFormat: "markdown" });
      return;
    }
    if (command === "transcribe" || command === "trans") {
      await runTaskCommand(args, { defaultFormat: "transcript" });
      return;
    }
    if (command === "status") {
      await statusCommand(args);
      return;
    }
    if (command === "tasks" || command === "list") {
      await tasksCommand(args);
      return;
    }
    if (command === "stop") {
      const { options } = parseConnectionOptions(args);
      await stopService(options);
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
    const message = error instanceof Error ? error.message : String(error);
    console.error(message);
    if (error instanceof UsageError) {
      console.error("");
      printHelp();
    }
    process.exitCode = 1;
  }
}

main();
