#!/usr/bin/env node

"use strict";

const { spawn } = require("node:child_process");
const { existsSync } = require("node:fs");

const {
  baseUrlFor,
  getTask,
  getTaskProgress,
  listTasks,
  probeVideo,
  uploadLocalMedia,
  createVideoTask,
  ApiError,
} = require("../lib/api");
const { UsageError, parseTaskCommandArgs } = require("../lib/args");
const { formatTasks, emit } = require("../lib/output");
const { readVersion, locateBilisumDataRoot, findPython, cliHomeDir, ensureCliVenv, cliVenvPython, findSystemPython } = require("../lib/runtime");
const { readToken } = require("../lib/token");
const {
  ensureService,
  stopService,
  spawnService,
} = require("../lib/server");
const {
  resolveEnvironment,
  readConfig,
  writeConfig,
  ENV_NAMES,
  isDesktopReachable,
} = require("../lib/env");

const RELEASES_URL = "https://github.com/lycohana/BiliSum/releases/latest";
const REPO_URL = "https://github.com/lycohana/BiliSum";
const DEFAULT_PORT = "3838";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function printHelp() {
  const version = readVersion();
  console.log(`BiliSum ${version}`);
  console.log("");
  console.log("AI 视频总结与知识库工具：CLI 直接控制已安装的 BiliSum（桌面端/服务）。");
  console.log("");
  console.log("Usage:");
  console.log("  npx bilisum summarize <url|file> [options]  Summarize a video (knowledge note)");
  console.log("  npx bilisum brief <url|file> [options]      Fast summary card only (skips note)");
  console.log("  npx bilisum transcribe <url|file> [options] Print the transcript of a video");
  console.log("  npx bilisum status <task-id> [--json]       Show task status");
  console.log("  npx bilisum tasks [--limit N] [--json]      List tasks");
  console.log("  npx bilisum env [use <name>|setup]          Environment status / switch / setup");
  console.log("  npx bilisum desktop|cli|custom|auto         Switch environment (shortcut)");
  console.log("  npx bilisum --setting                       Open the web settings page");
  console.log("  npx bilisum start [options]                 Start the service");
  console.log("  npx bilisum stop                            Stop the CLI-started BiliSum service");
  console.log("  npx bilisum doctor                          Check environment/service/Python setup");
  console.log("  npx bilisum --version                       Print package version");
  console.log("  npx bilisum release                         Open the latest GitHub release");
  console.log("");
  console.log("环境（bilisum env）：desktop 连桌面端服务 / cli 独立环境（自带运行时）/ custom 自定义 / auto 自动");
  console.log("");
  console.log("默认按已保存的环境连接；auto 模式优先桌面端，否则使用 CLI 独立环境。");
  console.log("");
  console.log("Options (all commands):");
  console.log("  --host <host>                        Host, default 127.0.0.1");
  console.log("  --port <port>                        Port, default 3838");
  console.log("  --data <path>                        Data root (default: env-dependent)");
  console.log("  --token <token>                      Access token (default: env-dependent)");
  console.log("  --environment <name>                 desktop | cli | custom | auto (one-shot)");
  console.log("  --env KEY=VALUE                      Extra env for a CLI-started service");
  console.log("");
  console.log("Options (summarize/brief/transcribe):");
  console.log("  --page <n>                           Process a specific P for multi-P videos");
  console.log("  --all-pages                          Process every P of a multi-P video");
  console.log("  --visual-note <mode>                 text | frame_insert | vlm_integrated");
  console.log("  --prompt-preset <id>                 Use a summary prompt preset");
  console.log("  --format <fmt>                       json | markdown | transcript (default markdown)");
  console.log("  --output <path> / -o <path>          Write result to a file instead of stdout");
  console.log("  --with-transcript                    Append the full transcript to markdown output");
  console.log("  --no-wait                            Create the task and exit immediately");
  console.log("  --idle-timeout <sec>                 CLI 后台服务无任务自动关闭秒数，默认 600");
  console.log("  --timeout <sec>                      Max wait for completion, default 3600");
  console.log("  --startup-timeout <sec>              Max wait for service startup, default 300");
  console.log("  --quiet / -q                         Suppress progress output (stderr)");
  console.log("");
  console.log("默认输出为元数据 + 总结内容（不含字幕），需要字幕请加 --with-transcript。");
  console.log("brief 只生成摘要卡片（跳过知识笔记 LLM 调用，更快）。");
  console.log("Options (start):");
  console.log("  --no-open                            Do not open the browser");
  console.log("");
  console.log(`Latest release: ${RELEASES_URL}`);
  console.log(`Repository:     ${REPO_URL}`);
}

function parseConnectionOptions(args, { allowTaskId = false } = {}) {
  const options = {
    host: process.env.BILISUM_HOST || "127.0.0.1",
    port: process.env.BILISUM_PORT || DEFAULT_PORT,
    python: process.env.BILISUM_PYTHON || "",
    data: process.env.BILISUM_DATA || locateBilisumDataRoot(),
    token: process.env.BILISUM_TOKEN || "",
    env: [],
    environment: "",
    _hostSet: false,
    _portSet: false,
    json: false,
    limit: 20,
    help: false,
    open: true,
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
      options._hostSet = true;
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(++index, arg);
      options._portSet = true;
    } else if (arg === "--python") {
      options.python = readValue(++index, arg);
    } else if (arg === "--data") {
      options.data = readValue(++index, arg);
    } else if (arg === "--token") {
      options.token = readValue(++index, arg);
    } else if (arg === "--environment" || arg === "--env-mode") {
      const name = readValue(++index, arg).toLowerCase();
      if (!ENV_NAMES.includes(name)) {
        throw new UsageError(`--environment 仅支持 ${ENV_NAMES.join(" / ")}，收到：${name}`);
      }
      options.environment = name;
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readValue(++index, arg));
    } else if (arg === "--json") {
      options.json = true;
    } else if (arg === "--no-open") {
      options.open = false;
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

function resolveServiceToken(options) {
  // Resolve a token to inject into a CLI-spawned service. The standalone cli
  // environment has no desktop token file, so generate one (same pattern as
  // the desktop app) to avoid depending on auth.json creation timing; desktop
  // mode reuses the desktop token / auth.json so both sides always agree.
  let token = readToken(options);
  if (!token && options.environment === "cli") {
    token = require("node:crypto").randomBytes(32).toString("hex");
  }
  return token;
}

async function ensureAuthenticatedService(options, log) {
  // Resolve the token first (desktop token / auth.json / --token / env) and
  // inject it into a service the CLI spawns, so both always agree.
  let token = resolveServiceToken(options);
  await ensureService(options, { log, accessToken: token });
  if (!token) {
    // The CLI just spawned the service, which may have created {data}/auth.json.
    token = readToken(options);
  }
  if (!token) {
    throw new UsageError(
      "无法获取访问令牌：请用 --token 指定，或设置 VIDEO_SUM_ACCESS_TOKEN，" +
      "或先启动一次桌面端 BiliSum 生成令牌。",
    );
  }
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
      summaryScope: options.brief ? "summary" : undefined,
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
        progress = await getTaskProgress(baseUrl, token, taskId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          throw new Error(`任务不存在：${taskId}`);
        }
        // Fall back to the detail endpoint if progress is unavailable.
        progress = await getTask(baseUrl, token, taskId);
      }
      state.status = progress.status;
      states.set(taskId, state);
      if (!TERMINAL_STATUSES.has(progress.status)) {
        const marker = `${taskId}:${progress.status}:${progress.progress ?? ""}:${progress.latest_stage ?? ""}`;
        if (!seenProgress.has(marker)) {
          seenProgress.add(marker);
          const pct = Number(progress.progress);
          const pctText = Number.isFinite(pct) ? `${String(Math.max(0, Math.min(100, Math.round(pct)))).padStart(3)}%` : "  -%";
          const stage = progress.latest_stage ? ` [${progress.latest_stage}]` : "";
          const message = progress.latest_message ? ` ${progress.latest_message}` : "";
          log(`[${taskId}] ${pctText}${stage}${message}`);
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

async function runTaskCommand(args, { defaultFormat, brief = false }) {
  const { source, options } = parseTaskCommandArgs(args, { defaultFormat });
  options.brief = brief;
  if (options.help) {
    printHelp();
    return;
  }
  // Validate local inputs before any service work (no pointless auto-start).
  if (!/^https?:\/\//i.test(source) && !existsSync(source)) {
    throw new UsageError(`本地文件不存在：${source}`);
  }
  const log = options.quiet ? () => {} : console.error;
  await resolveEnvironment(options);
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
  const written = emit(
    formatTasks(completed, options.format, { includeTranscript: options.withTranscript, brief }),
    options,
  );
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
  await resolveEnvironment(options);
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
  await resolveEnvironment(options);
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

async function startService(args) {
  const { options } = parseConnectionOptions(args);
  await resolveEnvironment(options);
  const token = resolveServiceToken(options);
  const baseUrl = `http://${options.host}:${options.port}`;
  console.log(`Starting BiliSum at ${baseUrl}`);
  console.log(`Environment:    ${options.environment}`);
  console.log(`Data root:      ${options.data}`);
  console.log("");
  const { child } = spawnService(options, { background: false, accessToken: token });
  if (options.open) {
    const openTarget = token ? `${baseUrl}#token=${encodeURIComponent(token)}` : baseUrl;
    setTimeout(() => openUrl(openTarget), 1800);
  }
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code || 0;
  });
}

async function doctor(args) {
  const { options } = parseConnectionOptions(args);
  await resolveEnvironment(options);
  const python = options.environment === "cli"
    ? { command: cliVenvPython(), args: [] }
    : options.python
      ? { command: options.python, args: [] }
      : findPython(options.data);
  console.log(`BiliSum package: ${readVersion()}`);
  console.log(`Environment:     ${options.environment}`);
  console.log(`Service:         http://${options.host}:${options.port}`);
  console.log(`Data root:       ${options.data}`);
  console.log(`Python:          ${python && python.command ? [python.command, ...python.args].join(" ") : "未找到（请安装桌面版 BiliSum、Python 3.12，或运行 bilisum env setup）"}`);
  console.log(`Token:           ${readToken(options) ? "已找到" : "未找到（desktop 环境请先启动桌面端；cli 环境运行后自动生成）"}`);
  if (options.environment === "cli") {
    console.log(`CLI env status:  ${require("node:fs").existsSync(cliVenvPython()) ? "就绪" : "未初始化（运行 bilisum env setup）"}`);
  }
}

async function settingsCommand(args) {
  const { options } = parseConnectionOptions(args);
  if (options.help) {
    printHelp();
    return;
  }
  const log = console.error;
  await resolveEnvironment(options);
  const token = await ensureAuthenticatedService(options, log);
  // Pass the token in the URL fragment so the web UI can auto-authenticate
  // (the frontend consumes it and clears the fragment). The fragment is never
  // sent to the server.
  const url = `http://${options.host}:${options.port}/settings#token=${encodeURIComponent(token)}`;
  log(`打开设置页：${url}`);
  openUrl(url);
}

async function envCommand(args) {
  const sub = (args[0] || "").toLowerCase();
  const config = readConfig();

  if (sub === "use") {
    const name = String(args[1] || "").toLowerCase();
    if (!ENV_NAMES.includes(name) || args.length !== 2) {
      throw new UsageError(`用法：bilisum env use ${ENV_NAMES.join("|")}`);
    }
    writeConfig({ ...config, env: name });
    const suffix = name === "auto" ? "（桌面端可用时优先 desktop，否则 cli）" : "";
    console.log(`已切换到 ${name} 环境${suffix}。`);
    return;
  }

  if (sub === "setup") {
    console.log(`初始化 CLI 独立环境（${cliHomeDir()}）...`);
    const python = ensureCliVenv({ python: process.env.BILISUM_PYTHON || "", environment: "cli" }, console.error);
    console.log(`CLI 独立环境就绪：${python}`);
    return;
  }

  if (sub === "help" || sub === "--help" || sub === "-h") {
    printEnvHelp();
    return;
  }

  if (sub) {
    throw new UsageError(`未知 env 子命令：${sub}`);
  }

  // default: status
  const current = await resolveEnvironment({ environment: config.env || "auto" });
  const desktopUp = await isDesktopReachable();
  console.log(`当前环境:      ${current.env}`);
  console.log(`服务地址:      http://${current.host}:${current.port}`);
  console.log(`数据根:        ${current.dataRoot}`);
  console.log("");
  console.log("可用环境:");
  console.log(`  desktop    ${desktopUp ? "● 桌面端服务在运行" : "○ 未检测到桌面端服务（127.0.0.1:3838）"}`);
  const venvReady = require("node:fs").existsSync(cliVenvPython());
  console.log(`  cli        ${venvReady ? "● 独立环境已就绪" : "○ 未初始化（bilisum env setup）"}  CLI_HOME=${cliHomeDir()}`);
  console.log(`  custom     ${config.custom ? "● 已配置" : "○ 未配置（--host/--port/--token）"}`);
  console.log(`  auto       desktop 优先，否则 cli`);
  console.log("");
  console.log("切换：bilisum env use desktop|cli|custom|auto（也可直接 bilisum desktop|cli|custom|auto）");
  if (config.env && config.env !== "auto") {
    console.log(`已持久化选择：${config.env}`);
  }
}

function printEnvHelp() {
  console.log("Usage:");
  console.log("  bilisum env                     Show environment status");
  console.log("  bilisum env use <name>          Switch environment (desktop|cli|custom|auto)");
  console.log("  bilisum <name>                  Shortcut for `bilisum env use <name>`");
  console.log("  bilisum env setup               Initialize the standalone cli environment");
  console.log("");
  console.log("Environments:");
  console.log("  desktop  Connect to the installed desktop app service (127.0.0.1:3838)");
  console.log("  cli      Standalone: own data root, own venv, own settings page (127.0.0.1:3839)");
  console.log("  custom   Any host/port/token (Docker, remote)");
  console.log("  auto     Desktop if reachable, otherwise cli (default)");
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
    if (!command) {
      // No command: print help, never start a service implicitly.
      printHelp();
      return;
    }
    if (command === "start" || command === "serve") {
      startService(args);
      return;
    }
    if (command === "summarize" || command === "sum") {
      await runTaskCommand(args, { defaultFormat: "markdown" });
      return;
    }
    if (command === "brief") {
      await runTaskCommand(args, { defaultFormat: "markdown", brief: true });
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
    if (command === "env") {
      await envCommand(args);
      return;
    }
    if (ENV_NAMES.includes(command)) {
      if (args.length > 0) {
        throw new UsageError(`用法：bilisum ${command}`);
      }
      await envCommand(["use", command]);
      return;
    }
    if (command === "settings" || command === "--setting" || command === "config") {
      await settingsCommand(args);
      return;
    }
    if (command === "stop") {
      const { options } = parseConnectionOptions(args);
      await resolveEnvironment(options);
      await stopService(options);
      return;
    }
    if (command === "doctor") {
      await doctor(args);
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
    // Exit immediately so orphaned pollers/timers cannot keep the process alive.
    process.exit(1);
  }
}

main();
