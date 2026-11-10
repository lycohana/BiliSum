#!/usr/bin/env node

"use strict";

/**
 * CLI shell: parse arguments -> call a lib/commands/ function -> format the
 * returned data with lib/output.js. Commands report progress through a
 * reporter that writes stderr; only this file writes stdout or exits.
 */

const { spawn } = require("node:child_process");
const { readFileSync } = require("node:fs");

const {
  UsageError,
  parseAgentCommandArgs,
  parseResultCommandArgs,
  parseTaskCommandArgs,
  resolveSummaryScope,
} = require("../lib/args");
const {
  emit,
  formatDoctorText,
  formatEnvStatusText,
  formatProgressLine,
  formatStatusText,
  formatTaskListText,
  formatTasks,
} = require("../lib/output");
const { readVersion, locateBilisumDataRoot, cliHomeDir } = require("../lib/runtime");
const { runSkillCommand } = require("../lib/skill");
const { stopService, spawnService } = require("../lib/server");
const { resolveEnvironment, ENV_NAMES } = require("../lib/env");
const { ensureAuthenticatedService, resolveServiceToken } = require("../lib/commands/connection");
const { runTaskCommand } = require("../lib/commands/task");
const { getTaskStatus } = require("../lib/commands/status");
const { getTaskResult } = require("../lib/commands/result");
const { listTaskSummaries } = require("../lib/commands/tasks");
const { getEnvStatus, useEnvironment, setupCliEnvironment } = require("../lib/commands/env");
const { runDoctor } = require("../lib/commands/doctor");
const { nextAgentWork, submitAgentWork, startLeaseHeartbeat } = require("../lib/commands/agent");

const RELEASES_URL = "https://github.com/lycohana/BiliSum/releases/latest";
const REPO_URL = "https://github.com/lycohana/BiliSum";
const DEFAULT_PORT = "3838";

function printHelp() {
  const version = readVersion();
  console.log(`BiliSum ${version}`);
  console.log("");
  console.log("AI 视频总结与知识库工具：CLI 直接控制已安装的 BiliSum（桌面端/服务）。");
  console.log("");
  console.log("Usage:");
  console.log("  npx bilisum summarize <url|file> [options]  Summarize a video (knowledge note)");
  console.log("  npx bilisum brief <url|file> [options]      Fast summary card only (skips note)");
  console.log("  npx bilisum transcribe <url|file> [options] Transcript only (no summary; --with-summary to add one)");
  console.log("  npx bilisum status <task-id> [--json]       Show task status");
  console.log("  npx bilisum result <task-id> [options]      Read a finished task's result");
  console.log("  npx bilisum tasks [--limit N] [--json]      List tasks");
  console.log("  npx bilisum agent next|submit [options]     Agent mode: claim / submit pending items (agent --help)");
  console.log("  npx bilisum env [use <name>|setup]          Environment status / switch / setup");
  console.log("  npx bilisum desktop|cli|custom|auto         Switch environment (shortcut)");
  console.log("  npx bilisum --setting                       Open the web settings page");
  console.log("  npx bilisum start [options]                 Start the service");
  console.log("  npx bilisum stop                            Stop the CLI-started BiliSum service");
  console.log("  npx bilisum doctor                          Check environment/service/Python setup");
  console.log("  npx bilisum mcp [--print-config]            Run the MCP server (stdio) for chat clients");
  console.log("  npx bilisum skill [path|install]            Install the Agent video-understanding skill");
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
  console.log("  --instructions <text>                补充要求 for this summary (content focus only, max 1000 chars)");
  console.log("  --language <code>                    Output language, e.g. zh / en / ja (default: not set, prompts unchanged)");
  console.log("  --format <fmt>                       json | markdown | transcript (default markdown)");
  console.log("  --output <path> / -o <path>          Write result to a file instead of stdout");
  console.log("  --with-transcript                    Append the full transcript to markdown output");
  console.log("  --with-summary                       transcribe: also generate the summary (default: transcript only)");
  console.log("  --force                              Always create a new task (default: reuse a matching one)");
  console.log("  --engine <engine>                    builtin | agent (default: BiliSum's default_engine)");
  console.log("  --agent-capabilities <list>          Agent mode: text,vision (default: agent_default_capabilities)");
  console.log("  --agent-name <name>                  Declare an Agent caller (or BILISUM_AGENT_NAME)");
  console.log("  --no-wait                            Create the task and exit immediately");
  console.log("  --idle-timeout <sec>                 CLI 后台服务无任务自动关闭秒数，默认 600");
  console.log("  --timeout <sec>                      Max wait for completion, default 3600");
  console.log("  --startup-timeout <sec>              Max wait for service startup, default 300");
  console.log("  --quiet / -q                         Suppress progress output (stderr)");
  console.log("");
  console.log("默认输出为元数据 + 总结内容（不含字幕），需要字幕请加 --with-transcript。");
  console.log("brief 只生成摘要卡片（跳过知识笔记 LLM 调用，更快）。");
  console.log("Options (result):");
  console.log("  --format <fmt>                       markdown | brief | transcript | json (default markdown)");
  console.log("  --time <start-end>                   transcript within a time range, e.g. 12:00-15:30");
  console.log("  --grep <keyword>                     transcript segments containing the keyword (+ context)");
  console.log("  --context <n>                        segments of context around each --grep hit, default 2");
  console.log("  --offset <n> / --max-chars <n>       Read the output in pages");
  console.log("  --json                               Print the paged envelope (content, next_offset, ...)");
  console.log("");
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

/** Reporter for the CLI: log and progress lines go to stderr. */
function stderrReporter({ quiet = false } = {}) {
  if (quiet) {
    return { log: () => {}, progress: () => {} };
  }
  return {
    log: console.error,
    progress: (event) => console.error(formatProgressLine(event)),
  };
}

/** `summarize` / `brief` / `transcribe`: parse options, create or reuse tasks, wait and print the result. */
async function taskCommand(args, { defaultFormat, brief = false, transcribe = false }) {
  const { source, options } = parseTaskCommandArgs(args, { defaultFormat });
  options.brief = brief;
  options.summaryScope = resolveSummaryScope({ brief, transcribe, withSummary: options.withSummary });
  if (options.help) {
    printHelp();
    return;
  }
  const reporter = stderrReporter({ quiet: options.quiet });
  const outcome = await runTaskCommand(source, options, reporter);

  if (!outcome.waited) {
    emit(formatTasks(outcome.tasks.map((task) => ({ ...task, result: null })), "json"), options);
    return;
  }
  if (outcome.failed.length) {
    emit(formatTasks(outcome.tasks, "json"), options);
    throw new Error(`${outcome.failed.length} 个任务失败。`);
  }
  if (outcome.awaitingAgent && outcome.awaitingAgent.length) {
    // Agent mode: the work now waits for an Agent; print the tasks and how to claim them.
    emit(formatTasks(outcome.tasks, "json"), options);
    reporter.log(agentClaimHint(outcome.awaitingAgent, options));
    return;
  }
  const written = emit(
    formatTasks(outcome.tasks, options.format, { includeTranscript: options.withTranscript, brief }),
    options,
  );
  if (written) {
    reporter.log(written);
  }
}

/** Quote a value for a copy-paste shell command; simple tokens stay bare. */
function shellQuote(value) {
  return /^[\w.@:/-]+$/.test(value) ? value : `"${String(value).replace(/"/g, '\\"')}"`;
}

/** Next-step hint printed when tasks wait for an Agent: the `bilisum agent next` command to run. */
function agentClaimHint(tasks, options) {
  const batches = [...new Set(tasks.map((task) => task.batch_id).filter(Boolean))];
  const name = options.agentName ? ` --agent-name ${shellQuote(options.agentName)}` : " --agent-name <名称>";
  const scopes = batches.length
    ? batches.map((batch) => `--batch ${batch}`)
    : tasks.map((task) => `--task ${task.task_id}`);
  return [
    `${tasks.length} 个任务等待 Agent 处理（agent_summarizing）。领取待办：`,
    ...scopes.map((scope) => `  bilisum agent next ${scope}${name}`),
  ].join("\n");
}

const HEARTBEAT_ENV = "BILISUM_AGENT_HEARTBEAT";
const DEFAULT_MAX_HOLD_SECONDS = 7200;

/**
 * The CLI exits right after `agent next`, so the lease is renewed by a
 * detached `bilisum agent _heartbeat` process. It stops when the service
 * refuses the lease (submitted / expired / taken over) or after the max hold
 * time (BILISUM_AGENT_MAX_HOLD seconds, default 7200).
 */
function spawnDetachedHeartbeat(connection, claim) {
  const maxHold = Number.parseInt(process.env.BILISUM_AGENT_MAX_HOLD || "", 10);
  const config = {
    baseUrl: connection.baseUrl,
    token: connection.token,
    lease: { task_id: claim.task_id, lease_id: claim.lease_id, lease_expires_at: claim.lease_expires_at },
    maxHoldMs: (Number.isFinite(maxHold) && maxHold > 0 ? maxHold : DEFAULT_MAX_HOLD_SECONDS) * 1000,
  };
  const child = spawn(process.execPath, [__filename, "agent", "_heartbeat"], {
    detached: true,
    stdio: "ignore",
    windowsHide: true,
    env: { ...process.env, [HEARTBEAT_ENV]: JSON.stringify(config) },
  });
  child.on("error", () => {});
  child.unref();
}

/** Internal `agent _heartbeat` process: renews the lease described in BILISUM_AGENT_HEARTBEAT until it stops. */
async function heartbeatProcess() {
  const config = JSON.parse(process.env[HEARTBEAT_ENV] || "null");
  if (!config || !config.lease) {
    throw new UsageError("agent _heartbeat 是内部命令。", { showHelp: false });
  }
  await startLeaseHeartbeat(config, config.lease, { maxHoldMs: config.maxHoldMs }).done;
}

/** Read the `agent submit --results` JSON from a file or stdin (`-`); strips a BOM. */
function readResultsPayload(source) {
  let text;
  try {
    text = source === "-" ? readFileSync(0, "utf8") : readFileSync(source, "utf8");
  } catch {
    throw new UsageError(`结果文件不存在或无法读取：${source}`, { showHelp: false });
  }
  try {
    return JSON.parse(text.replace(/^﻿/, ""));
  } catch (error) {
    throw new UsageError(`结果文件不是合法的 JSON：${error.message}`, { showHelp: false });
  }
}

/** Usage text for `bilisum agent`. */
function printAgentHelp() {
  console.log("Usage:");
  console.log("  bilisum agent next (--batch <id> | --all | --task <id>) --agent-name <name> [options]");
  console.log("      Claim one task's pending items (JSON on stdout). The lease is renewed in the background.");
  console.log("      --wait <sec>          Keep claiming while tasks are still processing (default 90, 0 = once)");
  console.log("  bilisum agent submit <task-id> --lease <lease-id> --results <file|-> --agent-name <name>");
  console.log("      Submit every item of the round: [{\"item_id\", \"content\"} | {\"item_id\", \"declined\": true, \"reason\"}]");
  console.log("");
  console.log("  --agent-name <name>       Agent name reported to BiliSum (or BILISUM_AGENT_NAME)");
  console.log("  --agent-model <model>     Optional model name (self-reported)");
  console.log("  --output <path> / -o      Write the JSON to a file instead of stdout");
}

/** `bilisum agent next | submit` (plus the internal `_heartbeat`). */
async function agentCommand(args) {
  if (args[0] === "_heartbeat") {
    await heartbeatProcess();
    return;
  }
  const { subcommand, taskId, options } = parseAgentCommandArgs(args);
  if (options.help) {
    printAgentHelp();
    return;
  }
  const reporter = stderrReporter({ quiet: options.quiet });
  if (subcommand === "next") {
    const claim = await nextAgentWork({ ...options, onLease: spawnDetachedHeartbeat }, reporter);
    const written = emit(JSON.stringify(claim, null, 2), options);
    if (written) {
      reporter.log(written);
    }
    if (claim && claim.task_id) {
      reporter.log(
        `处理完后提交：bilisum agent submit ${claim.task_id} --lease ${claim.lease_id} --results <file>`
        + ` --agent-name ${shellQuote(options.agentName)}（做不了的项提交 declined）`,
      );
    }
    return;
  }
  const results = readResultsPayload(options.results);
  const accepted = await submitAgentWork(taskId, { ...options, results }, reporter);
  const written = emit(JSON.stringify(accepted, null, 2), options);
  if (written) {
    reporter.log(written);
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
  const task = await getTaskStatus(positional[0], options, stderrReporter());
  console.log(options.json ? JSON.stringify(task, null, 2) : formatStatusText(task));
}

/** `bilisum result <task-id>`: read an existing task's result, with transcript filters and paging. */
async function resultCommand(args) {
  const { taskId, options } = parseResultCommandArgs(args);
  if (options.help) {
    printHelp();
    return;
  }
  const reporter = stderrReporter({ quiet: options.quiet });
  const page = await getTaskResult(taskId, options, reporter);
  if (options.json) {
    emit(JSON.stringify(page, null, 2), options);
    return;
  }
  const written = emit(page.content, options);
  if (written) {
    reporter.log(written);
  }
  if (page.next_offset !== null) {
    reporter.log(`（内容未读完，共 ${page.total_chars} 字；继续读取：加上 --offset ${page.next_offset}）`);
  }
}

async function tasksCommand(args) {
  const { options } = parseConnectionOptions(args);
  if (options.help) {
    printHelp();
    return;
  }
  const listing = await listTaskSummaries(options, stderrReporter());
  console.log(options.json ? JSON.stringify(listing.tasks, null, 2) : formatTaskListText(listing));
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

/** `bilisum doctor`: print the environment diagnosis. */
async function doctorCommand(args) {
  const { options } = parseConnectionOptions(args);
  console.log(formatDoctorText(await runDoctor(options)));
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

  if (sub === "use") {
    if (args.length !== 2) {
      throw new UsageError(`用法：bilisum env use ${ENV_NAMES.join("|")}`);
    }
    const { env } = useEnvironment(args[1]);
    const suffix = env === "auto" ? "（桌面端可用时优先 desktop，否则 cli）" : "";
    console.log(`已切换到 ${env} 环境${suffix}。`);
    return;
  }

  if (sub === "setup") {
    console.log(`初始化 CLI 独立环境（${cliHomeDir()}）...`);
    const { python } = setupCliEnvironment(stderrReporter());
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

  console.log(formatEnvStatusText(await getEnvStatus()));
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
      await taskCommand(args, { defaultFormat: "markdown" });
      return;
    }
    if (command === "brief") {
      await taskCommand(args, { defaultFormat: "markdown", brief: true });
      return;
    }
    if (command === "transcribe" || command === "trans") {
      await taskCommand(args, { defaultFormat: "transcript", transcribe: true });
      return;
    }
    if (command === "status") {
      await statusCommand(args);
      return;
    }
    if (command === "result") {
      await resultCommand(args);
      return;
    }
    if (command === "agent") {
      await agentCommand(args);
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
    if (command === "mcp") {
      // Loaded lazily: only `mcp` needs the MCP SDK dependency.
      await require("../lib/mcp/server").runMcpCommand(args);
      return;
    }
    if (command === "doctor") {
      await doctorCommand(args);
      return;
    }
    if (command === "skill") {
      await runSkillCommand(args);
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
    if (error instanceof UsageError && error.showHelp) {
      console.error("");
      printHelp();
    }
    // Exit immediately so orphaned pollers/timers cannot keep the process alive.
    process.exit(1);
  }
}

main();
