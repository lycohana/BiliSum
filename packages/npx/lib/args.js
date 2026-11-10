"use strict";

/**
 * Shared option parsing for BiliSum task commands (summarize / transcribe).
 */

const { locateBilisumDataRoot } = require("./runtime");
const { ENV_NAMES } = require("./env");

class UsageError extends Error {
  constructor(message, { showHelp = true } = {}) {
    super(message);
    this.name = "UsageError";
    this.showHelp = showHelp;
  }
}

const FORMATS = new Set(["json", "markdown", "transcript"]);
const VISUAL_NOTE_MODES = new Set(["text", "frame_insert", "vlm_integrated"]);
/** Same cap as the service (TaskOptions.extra_instructions). */
const MAX_INSTRUCTIONS_CHARS = 1000;

/**
 * Normalize 补充要求 text: trimmed, blank = not given. Throws UsageError when too long.
 * Shared by the CLI and MCP shells.
 */
function normalizeInstructions(raw) {
  const text = String(raw ?? "").trim();
  const length = [...text].length;
  if (length > MAX_INSTRUCTIONS_CHARS) {
    throw new UsageError(`补充要求不能超过 ${MAX_INSTRUCTIONS_CHARS} 字（当前 ${length} 字）。`, { showHelp: false });
  }
  return text;
}

const ENGINES = ["builtin", "agent"];
const AGENT_CAPABILITIES = ["text", "vision"];

/** Validate `--engine` (builtin / agent), case-insensitive. */
function parseEngine(raw) {
  const engine = String(raw).trim().toLowerCase();
  if (!ENGINES.includes(engine)) {
    throw new UsageError(`--engine 仅支持 ${ENGINES.join(" / ")}，收到：${raw}`);
  }
  return engine;
}

/** "text,vision" -> ["text", "vision"] (deduplicated, given order). */
function parseAgentCapabilities(raw) {
  const values = String(raw).split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
  const unknown = values.filter((value) => !AGENT_CAPABILITIES.includes(value));
  if (!values.length || unknown.length) {
    throw new UsageError(`--agent-capabilities 仅支持 ${AGENT_CAPABILITIES.join(" / ")}（逗号分隔），收到：${raw}`);
  }
  return [...new Set(values)];
}

/** Agent name reported to the service: --agent-name, else BILISUM_AGENT_NAME. */
function defaultAgentName() {
  return String(process.env.BILISUM_AGENT_NAME || "").trim();
}

function readValue(args, index, option) {
  const value = args[index];
  if (!value || value.startsWith("--")) {
    throw new UsageError(`${option} requires a value`);
  }
  return value;
}

function parsePositiveInt(raw, option) {
  const parsed = Number.parseInt(String(raw), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new UsageError(`${option} 需要是正整数，收到：${raw}`);
  }
  return parsed;
}

/**
 * Parse `bilisum summarize|transcribe <source> [options]`.
 * Returns { source, options }.
 */
function parseTaskCommandArgs(args, { defaultFormat = "markdown" } = {}) {
  const options = {
    host: process.env.BILISUM_HOST || "127.0.0.1",
    port: process.env.BILISUM_PORT || "3838",
    python: process.env.BILISUM_PYTHON || "",
    data: process.env.BILISUM_DATA || locateBilisumDataRoot(),
    token: process.env.BILISUM_TOKEN || "",
    env: [],
    environment: "",
    _hostSet: false,
    _portSet: false,
    format: defaultFormat,
    output: "",
    wait: true,
    quiet: false,
    page: null,
    allPages: false,
    visualNote: "",
    promptPreset: "",
    instructions: "",
    language: "",
    withTranscript: false,
    withSummary: false,
    force: false,
    engine: "",
    agentCapabilities: [],
    agentName: defaultAgentName(),
    idleTimeout: Number.parseInt(process.env.BILISUM_CLI_IDLE_TIMEOUT || "600", 10) || 600,
    timeoutSeconds: 3600,
    startupTimeoutSeconds: 300,
  };

  const positional = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readValue(args, ++index, arg);
      options._hostSet = true;
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(args, ++index, arg);
      options._portSet = true;
    } else if (arg === "--python") {
      options.python = readValue(args, ++index, arg);
    } else if (arg === "--environment" || arg === "--env-mode") {
      const name = readValue(args, ++index, arg).toLowerCase();
      if (!ENV_NAMES.includes(name)) {
        throw new UsageError(`--environment 仅支持 ${ENV_NAMES.join(" / ")}，收到：${name}`);
      }
      options.environment = name;
    } else if (arg === "--data") {
      options.data = readValue(args, ++index, arg);
    } else if (arg === "--token") {
      options.token = readValue(args, ++index, arg);
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readValue(args, ++index, arg));
    } else if (arg === "--format" || arg === "-f") {
      const format = readValue(args, ++index, arg);
      if (!FORMATS.has(format)) {
        throw new UsageError(`--format 仅支持 ${[...FORMATS].join(" / ")}，收到：${format}`);
      }
      options.format = format;
    } else if (arg === "--output" || arg === "-o") {
      options.output = readValue(args, ++index, arg);
    } else if (arg === "--no-wait") {
      options.wait = false;
    } else if (arg === "--with-transcript") {
      options.withTranscript = true;
    } else if (arg === "--with-summary") {
      options.withSummary = true;
    } else if (arg === "--force") {
      options.force = true;
    } else if (arg === "--quiet" || arg === "-q") {
      options.quiet = true;
    } else if (arg === "--page") {
      options.page = parsePositiveInt(readValue(args, ++index, arg), "--page");
    } else if (arg === "--all-pages") {
      options.allPages = true;
    } else if (arg === "--visual-note") {
      const mode = readValue(args, ++index, arg);
      if (!VISUAL_NOTE_MODES.has(mode)) {
        throw new UsageError(`--visual-note 仅支持 ${[...VISUAL_NOTE_MODES].join(" / ")}，收到：${mode}`);
      }
      options.visualNote = mode;
    } else if (arg === "--prompt-preset") {
      options.promptPreset = readValue(args, ++index, arg);
    } else if (arg === "--instructions") {
      options.instructions = normalizeInstructions(readValue(args, ++index, arg));
    } else if (arg === "--language") {
      options.language = readValue(args, ++index, arg).trim();
    } else if (arg === "--engine") {
      options.engine = parseEngine(readValue(args, ++index, arg));
    } else if (arg === "--agent-capabilities") {
      options.agentCapabilities = parseAgentCapabilities(readValue(args, ++index, arg));
    } else if (arg === "--agent-name") {
      options.agentName = readValue(args, ++index, arg).trim();
    } else if (arg === "--idle-timeout") {
      options.idleTimeout = parsePositiveInt(readValue(args, ++index, arg), "--idle-timeout");
    } else if (arg === "--timeout") {
      options.timeoutSeconds = parsePositiveInt(readValue(args, ++index, arg), "--timeout");
    } else if (arg === "--startup-timeout") {
      options.startupTimeoutSeconds = parsePositiveInt(readValue(args, ++index, arg), "--startup-timeout");
    } else if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg.startsWith("-")) {
      throw new UsageError(`未知选项：${arg}`);
    } else {
      positional.push(arg);
    }
  }

  if (!options.help && positional.length === 0) {
    throw new UsageError("缺少视频来源：请提供 B 站/YouTube 链接或本地视频文件路径。");
  }
  if (positional.length > 1) {
    throw new UsageError(`只接受一个视频来源，收到 ${positional.length} 个。批量请多次执行或使用 --all-pages。`);
  }
  if (options.page !== null && options.allPages) {
    throw new UsageError("--page 与 --all-pages 不能同时使用。");
  }

  return { source: positional[0] || "", options };
}

const RESULT_FORMATS = new Set(["markdown", "brief", "transcript", "json"]);

/** Parse an option value that must be an integer >= 0; throws UsageError otherwise. */
function parseNonNegativeInt(raw, option) {
  const text = String(raw);
  const parsed = Number.parseInt(text, 10);
  if (!/^\d+$/.test(text) || !Number.isFinite(parsed)) {
    throw new UsageError(`${option} 需要是非负整数，收到：${raw}`);
  }
  return parsed;
}

/**
 * Parse `bilisum result <task-id> [options]`. Returns { taskId, options }.
 * options: connection fields + format, withTranscript, time (raw "12:00-15:30"),
 * grep, context, offset, maxChars (null = no limit), json, output, help.
 * --time / --grep switch the default format to transcript.
 */
function parseResultCommandArgs(args) {
  const options = {
    host: process.env.BILISUM_HOST || "127.0.0.1",
    port: process.env.BILISUM_PORT || "3838",
    python: process.env.BILISUM_PYTHON || "",
    data: process.env.BILISUM_DATA || locateBilisumDataRoot(),
    token: process.env.BILISUM_TOKEN || "",
    env: [],
    environment: "",
    _hostSet: false,
    _portSet: false,
    format: "",
    withTranscript: false,
    time: "",
    grep: "",
    context: null,
    offset: 0,
    maxChars: null,
    json: false,
    output: "",
    quiet: false,
    idleTimeout: Number.parseInt(process.env.BILISUM_CLI_IDLE_TIMEOUT || "600", 10) || 600,
    startupTimeoutSeconds: 300,
    help: false,
  };
  const positional = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readValue(args, ++index, arg);
      options._hostSet = true;
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(args, ++index, arg);
      options._portSet = true;
    } else if (arg === "--python") {
      options.python = readValue(args, ++index, arg);
    } else if (arg === "--environment" || arg === "--env-mode") {
      const name = readValue(args, ++index, arg).toLowerCase();
      if (!ENV_NAMES.includes(name)) {
        throw new UsageError(`--environment 仅支持 ${ENV_NAMES.join(" / ")}，收到：${name}`);
      }
      options.environment = name;
    } else if (arg === "--data") {
      options.data = readValue(args, ++index, arg);
    } else if (arg === "--token") {
      options.token = readValue(args, ++index, arg);
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readValue(args, ++index, arg));
    } else if (arg === "--format" || arg === "-f") {
      const format = readValue(args, ++index, arg);
      if (!RESULT_FORMATS.has(format)) {
        throw new UsageError(`--format 仅支持 ${[...RESULT_FORMATS].join(" / ")}，收到：${format}`);
      }
      options.format = format;
    } else if (arg === "--with-transcript") {
      options.withTranscript = true;
    } else if (arg === "--time") {
      options.time = readValue(args, ++index, arg);
    } else if (arg === "--grep") {
      options.grep = readValue(args, ++index, arg);
    } else if (arg === "--context") {
      options.context = parseNonNegativeInt(readValue(args, ++index, arg), "--context");
    } else if (arg === "--offset") {
      options.offset = parseNonNegativeInt(readValue(args, ++index, arg), "--offset");
    } else if (arg === "--max-chars") {
      options.maxChars = parsePositiveInt(readValue(args, ++index, arg), "--max-chars");
    } else if (arg === "--json") {
      options.json = true;
    } else if (arg === "--output" || arg === "-o") {
      options.output = readValue(args, ++index, arg);
    } else if (arg === "--quiet" || arg === "-q") {
      options.quiet = true;
    } else if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg.startsWith("-")) {
      throw new UsageError(`未知选项：${arg}`);
    } else {
      positional.push(arg);
    }
  }
  if (!options.help && positional.length === 0) {
    throw new UsageError("缺少 task-id。用法：bilisum result <task-id> [--format markdown|brief|transcript|json] [--time 12:00-15:30] [--grep 关键词]");
  }
  if (positional.length > 1) {
    throw new UsageError(`只接受一个 task-id，收到 ${positional.length} 个。`);
  }
  const filtering = Boolean(options.time || options.grep);
  if (!options.format) {
    options.format = filtering ? "transcript" : "markdown";
  } else if (filtering && options.format !== "transcript") {
    throw new UsageError("--time / --grep 只能用于 transcript 格式。");
  }
  return { taskId: positional[0] || "", options };
}

/** Default seconds `agent next` keeps re-claiming while in-scope tasks are still processing. */
const DEFAULT_AGENT_WAIT_SECONDS = 90;

/**
 * Parse `bilisum agent next|submit ...`. Returns { subcommand, taskId, options }.
 *   next:   exactly one of --batch <id> / --all / --task <id> -> options.scope, batchId, taskId;
 *           --wait <sec> (default 90, 0 = claim once) -> options.waitSeconds.
 *   submit: <task-id> --lease <id> --results <file|-> -> taskId, options.leaseId, options.results.
 * Both need an Agent name (--agent-name or BILISUM_AGENT_NAME); --agent-model is optional.
 */
function parseAgentCommandArgs(args) {
  const subcommand = String(args[0] || "").toLowerCase();
  const options = {
    host: process.env.BILISUM_HOST || "127.0.0.1",
    port: process.env.BILISUM_PORT || "3838",
    python: process.env.BILISUM_PYTHON || "",
    data: process.env.BILISUM_DATA || locateBilisumDataRoot(),
    token: process.env.BILISUM_TOKEN || "",
    env: [],
    environment: "",
    _hostSet: false,
    _portSet: false,
    scope: "",
    batchId: "",
    taskId: "",
    agentName: defaultAgentName(),
    agentModel: "",
    waitSeconds: DEFAULT_AGENT_WAIT_SECONDS,
    leaseId: "",
    results: "",
    output: "",
    quiet: false,
    idleTimeout: Number.parseInt(process.env.BILISUM_CLI_IDLE_TIMEOUT || "600", 10) || 600,
    startupTimeoutSeconds: 300,
    help: false,
  };
  if (subcommand === "help" || subcommand === "--help" || subcommand === "-h" || !subcommand) {
    options.help = true;
    return { subcommand: "help", taskId: "", options };
  }
  if (subcommand !== "next" && subcommand !== "submit") {
    throw new UsageError(`未知 agent 子命令：${args[0]}（可选：next / submit）`);
  }
  const scopes = [];
  const positional = [];
  for (let index = 1; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readValue(args, ++index, arg);
      options._hostSet = true;
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(args, ++index, arg);
      options._portSet = true;
    } else if (arg === "--python") {
      options.python = readValue(args, ++index, arg);
    } else if (arg === "--environment" || arg === "--env-mode") {
      const name = readValue(args, ++index, arg).toLowerCase();
      if (!ENV_NAMES.includes(name)) {
        throw new UsageError(`--environment 仅支持 ${ENV_NAMES.join(" / ")}，收到：${name}`);
      }
      options.environment = name;
    } else if (arg === "--data") {
      options.data = readValue(args, ++index, arg);
    } else if (arg === "--token") {
      options.token = readValue(args, ++index, arg);
    } else if (arg === "--env" || arg === "-e") {
      options.env.push(readValue(args, ++index, arg));
    } else if (arg === "--agent-name") {
      options.agentName = readValue(args, ++index, arg).trim();
    } else if (arg === "--agent-model") {
      options.agentModel = readValue(args, ++index, arg).trim();
    } else if (subcommand === "next" && arg === "--batch") {
      scopes.push("batch");
      options.batchId = readValue(args, ++index, arg);
    } else if (subcommand === "next" && arg === "--all") {
      scopes.push("all");
    } else if (subcommand === "next" && arg === "--task") {
      scopes.push("task");
      options.taskId = readValue(args, ++index, arg);
    } else if (subcommand === "next" && arg === "--wait") {
      options.waitSeconds = parseNonNegativeInt(readValue(args, ++index, arg), "--wait");
    } else if (subcommand === "submit" && arg === "--lease") {
      options.leaseId = readValue(args, ++index, arg);
    } else if (subcommand === "submit" && arg === "--results") {
      const value = args[++index];
      if (!value || (value.startsWith("--"))) {
        throw new UsageError("--results requires a value");
      }
      options.results = value;
    } else if (arg === "--output" || arg === "-o") {
      options.output = readValue(args, ++index, arg);
    } else if (arg === "--quiet" || arg === "-q") {
      options.quiet = true;
    } else if (arg === "--idle-timeout") {
      options.idleTimeout = parsePositiveInt(readValue(args, ++index, arg), "--idle-timeout");
    } else if (arg === "--startup-timeout") {
      options.startupTimeoutSeconds = parsePositiveInt(readValue(args, ++index, arg), "--startup-timeout");
    } else if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg.startsWith("-") && arg !== "-") {
      throw new UsageError(`未知选项：${arg}`);
    } else {
      positional.push(arg);
    }
  }
  if (options.help) {
    return { subcommand, taskId: "", options };
  }
  let taskId = "";
  if (subcommand === "next") {
    if (positional.length) {
      throw new UsageError(`Unexpected argument: ${positional[0]}`);
    }
    if (scopes.length === 0) {
      throw new UsageError("请指定领取范围：--batch <id>、--all 或 --task <id>。");
    }
    if (scopes.length > 1) {
      throw new UsageError("--batch、--all、--task 只能指定一个。");
    }
    options.scope = scopes[0];
  } else {
    if (positional.length !== 1) {
      throw new UsageError("用法：bilisum agent submit <task-id> --lease <lease-id> --results <file|->");
    }
    taskId = positional[0];
    if (!options.leaseId) {
      throw new UsageError("缺少 --lease：请使用 bilisum agent next 返回的 lease_id。");
    }
    if (!options.results) {
      throw new UsageError("缺少 --results：请提供结果 JSON 文件（- 表示从标准输入读取）。");
    }
  }
  if (!options.agentName) {
    throw new UsageError("缺少 --agent-name：请上报 Agent 名称（例如 --agent-name \"Claude Code\"），或设置 BILISUM_AGENT_NAME。");
  }
  return { subcommand, taskId, options };
}

/**
 * The `summary_scope` a task command sends. `transcribe` is transcript-only
 * (no summary, no model calls) unless `--with-summary`; `brief` asks for the
 * summary card only; `summarize` leaves the service default.
 */
function resolveSummaryScope({ brief = false, transcribe = false, withSummary = false } = {}) {
  if (brief) {
    return "summary";
  }
  if (transcribe && !withSummary) {
    return "transcript_only";
  }
  return undefined;
}

module.exports = {
  UsageError,
  parseTaskCommandArgs,
  parseResultCommandArgs,
  parseAgentCommandArgs,
  parseEngine,
  parseAgentCapabilities,
  resolveSummaryScope,
  ENGINES,
  AGENT_CAPABILITIES,
  DEFAULT_AGENT_WAIT_SECONDS,
  normalizeInstructions,
  MAX_INSTRUCTIONS_CHARS,
  FORMATS,
  RESULT_FORMATS,
  VISUAL_NOTE_MODES,
};
