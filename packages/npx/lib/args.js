"use strict";

/**
 * Shared option parsing for BiliSum task commands (summarize / transcribe).
 */

const { locateBilisumDataRoot } = require("./runtime");
const { ENV_NAMES } = require("./env");

class UsageError extends Error {
  constructor(message) {
    super(message);
    this.name = "UsageError";
  }
}

const FORMATS = new Set(["json", "markdown", "transcript"]);
const VISUAL_NOTE_MODES = new Set(["text", "frame_insert", "vlm_integrated"]);

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
    withTranscript: false,
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

module.exports = { UsageError, parseTaskCommandArgs, FORMATS, VISUAL_NOTE_MODES };
