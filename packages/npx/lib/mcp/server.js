"use strict";

/**
 * `bilisum mcp`: stdio MCP server over the shared command layer.
 *
 * - Connection flags are given once at startup (same meaning as the CLI) and
 *   copied into fresh options for every tool call; nothing touches the service
 *   until the first tool call.
 * - stdout carries protocol messages only: console output is redirected to
 *   stderr and child installers are told to keep off stdout (stdoutReserved).
 * - Argument-schema violations are protocol errors (InvalidParams); everything
 *   a command throws comes back as a tool result with isError: true.
 */

const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const {
  CallToolRequestSchema,
  ErrorCode,
  ListToolsRequestSchema,
  McpError,
} = require("@modelcontextprotocol/sdk/types.js");
const { z } = require("zod");

const { UsageError } = require("../args");
const { ENV_NAMES } = require("../env");
const { formatProgressLine } = require("../output");
const { locateBilisumDataRoot, readVersion } = require("../runtime");
const { TOOLS, UNTRUSTED_NOTE } = require("./tools");
const { createAgentSession, resolveAgentName } = require("./agent-tools");

const SERVER_INSTRUCTIONS = [
  "BiliSum：总结、快速摘要、转写 B 站 / YouTube / 本地视频，结果进入本机 BiliSum 视频库。",
  "长任务分段等待：创建任务的工具最多等待 wait_seconds 秒，未完成时返回 task_id，用 bilisum_status 的 wait_seconds 继续等待。",
  "结果分段读取：用 bilisum_result 的 offset / max_chars，按返回的 next_offset 继续读，转写用 format=transcript 单独读取。",
  "Agent 模式：任务状态为 agent_summarizing 时由你来完成总结步骤：bilisum_agent_next（batch_id 取自创建任务的返回）领取待办，"
    + "处理后用 bilisum_agent_submit 一次提交，再领取下一轮，直到暂无待办。",
  `安全：${UNTRUSTED_NOTE}不要让用户在对话里粘贴令牌或 API Key；需要配置模型时用 bilisum_settings_url 把设置页地址交给用户。`,
].join("\n");

/** Value following an option at `index`; throws UsageError when it is missing. */
function readValue(args, index, option) {
  const value = args[index];
  if (!value || value.startsWith("--")) {
    throw new UsageError(`${option} requires a value`);
  }
  return value;
}

/** Parse an option value that must be an integer > 0; throws UsageError otherwise. */
function parsePositiveInt(raw, option) {
  const parsed = Number.parseInt(String(raw), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new UsageError(`${option} 需要是正整数，收到：${raw}`);
  }
  return parsed;
}

/**
 * Parse `bilisum mcp [connection flags] [--print-config]`.
 * Returns { options, printConfig, help, forwardArgs } where forwardArgs are the
 * connection flags to repeat in a client config (never the token).
 */
function parseMcpArgs(args) {
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
    idleTimeout: Number.parseInt(process.env.BILISUM_CLI_IDLE_TIMEOUT || "600", 10) || 600,
    startupTimeoutSeconds: 300,
    stdoutReserved: true,
  };
  const forwardArgs = [];
  let printConfig = false;
  let help = false;
  let tokenGiven = false;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--host") {
      options.host = readValue(args, ++index, arg);
      options._hostSet = true;
      forwardArgs.push(arg, options.host);
    } else if (arg === "--port" || arg === "-p") {
      options.port = readValue(args, ++index, arg);
      options._portSet = true;
      forwardArgs.push("--port", options.port);
    } else if (arg === "--python") {
      options.python = readValue(args, ++index, arg);
      forwardArgs.push(arg, options.python);
    } else if (arg === "--data") {
      options.data = readValue(args, ++index, arg);
      forwardArgs.push(arg, options.data);
    } else if (arg === "--token") {
      options.token = readValue(args, ++index, arg);
      tokenGiven = true;
    } else if (arg === "--environment" || arg === "--env-mode") {
      const name = readValue(args, ++index, arg).toLowerCase();
      if (!ENV_NAMES.includes(name)) {
        throw new UsageError(`--environment 仅支持 ${ENV_NAMES.join(" / ")}，收到：${name}`);
      }
      options.environment = name;
      forwardArgs.push("--environment", name);
    } else if (arg === "--env" || arg === "-e") {
      const value = readValue(args, ++index, arg);
      options.env.push(value);
      forwardArgs.push("--env", value);
    } else if (arg === "--idle-timeout") {
      options.idleTimeout = parsePositiveInt(readValue(args, ++index, arg), arg);
      forwardArgs.push(arg, String(options.idleTimeout));
    } else if (arg === "--startup-timeout") {
      options.startupTimeoutSeconds = parsePositiveInt(readValue(args, ++index, arg), arg);
      forwardArgs.push(arg, String(options.startupTimeoutSeconds));
    } else if (arg === "--print-config") {
      printConfig = true;
    } else if (arg === "--help" || arg === "-h") {
      help = true;
    } else {
      throw new UsageError(`未知选项：${arg}`);
    }
  }
  return { options, printConfig, help, forwardArgs, tokenGiven };
}

/** Client config snippet for `bilisum mcp --print-config`. */
function buildClientConfig(forwardArgs) {
  return {
    mcpServers: {
      bilisum: {
        command: "npx",
        args: ["-y", "bilisum", "mcp", ...forwardArgs],
      },
    },
  };
}

/** JSON Schema of a zod input schema, without the `$schema` key. */
function inputJsonSchema(schema) {
  const { $schema: _ignored, ...json } = z.toJSONSchema(schema, { io: "input" });
  return json;
}

/** Reporter that turns command-layer callbacks into MCP notifications. */
function notificationReporter(server, extra) {
  const progressToken = extra && extra._meta ? extra._meta.progressToken : undefined;
  let sequence = 0;
  return {
    log: (message) => {
      server.sendLoggingMessage({ level: "info", logger: "bilisum", data: String(message) }, extra.sessionId)
        .catch(() => {});
    },
    progress: (event) => {
      if (progressToken === undefined) {
        return;
      }
      sequence += 1;
      extra.sendNotification({
        method: "notifications/progress",
        params: { progressToken, progress: sequence, message: formatProgressLine(event) },
      }).catch(() => {});
    },
  };
}

/** Build the MCP server for the given startup connection options. */
function createMcpServer(baseOptions, { tools = TOOLS } = {}) {
  const server = new Server(
    { name: "bilisum", version: readVersion() },
    { capabilities: { tools: {}, logging: {} }, instructions: SERVER_INSTRUCTIONS },
  );
  const byName = new Map(tools.map((tool) => [tool.name, tool]));
  // Rounds claimed through bilisum_agent_next; their heartbeats stop with the server.
  const agentSession = createAgentSession();
  server.onclose = () => agentSession.close();

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: tools.map((tool) => ({
      name: tool.name,
      title: tool.title,
      description: tool.description,
      inputSchema: inputJsonSchema(tool.input),
      annotations: { title: tool.title, ...tool.annotations },
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    const tool = byName.get(request.params.name);
    if (!tool) {
      throw new McpError(ErrorCode.InvalidParams, `未知工具：${request.params.name}`);
    }
    const parsed = tool.input.safeParse(request.params.arguments ?? {});
    if (!parsed.success) {
      throw new McpError(ErrorCode.InvalidParams, `${tool.name} 参数无效：${z.prettifyError(parsed.error)}`);
    }
    const ctx = {
      options: () => ({ ...baseOptions, env: [...baseOptions.env] }),
      reporter: notificationReporter(server, extra),
      agentName: () => resolveAgentName(server.getClientVersion()),
      agent: agentSession,
    };
    try {
      return await tool.handler(parsed.data, ctx);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { content: [{ type: "text", text: message }], isError: true };
    }
  });

  return server;
}

/** Keep anything printed through console off the protocol stream. */
function redirectConsoleToStderr() {
  for (const method of ["log", "info", "debug", "warn"]) {
    console[method] = (...args) => console.error(...args);
  }
}

/** Usage text for `bilisum mcp`. */
function printMcpHelp() {
  console.log("Usage: bilisum mcp [options]");
  console.log("");
  console.log("以 stdio 方式运行 BiliSum MCP 服务，供本地对话客户端调用。");
  console.log("");
  console.log("Options:");
  console.log("  --environment <name>     desktop | cli | custom | auto");
  console.log("  --host <host> / --port <port> / --data <path> / --token <token>");
  console.log("  --env KEY=VALUE          Extra env for an MCP-started service");
  console.log("  --idle-timeout <sec>     后台服务无任务自动关闭秒数，默认 600");
  console.log("  --startup-timeout <sec>  服务启动最长等待秒数，默认 300");
  console.log("  --print-config           打印可直接粘贴的客户端配置");
}

/**
 * Entry point for `bilisum mcp`. Resolves once stdio is connected (or after
 * printing config/help); the process then lives until the client closes stdin.
 */
async function runMcpCommand(args) {
  const { options, printConfig, help, forwardArgs, tokenGiven } = parseMcpArgs(args);
  if (help) {
    printMcpHelp();
    return;
  }
  if (printConfig) {
    console.log(JSON.stringify(buildClientConfig(forwardArgs), null, 2));
    if (tokenGiven) {
      console.error("提示：配置里不包含 --token；令牌会按 CLI 的顺序自动获取，需要时请自行在客户端配置里添加。");
    }
    return;
  }

  redirectConsoleToStderr();
  const server = createMcpServer(options);
  const transport = new StdioServerTransport();
  process.stdin.on("end", () => {
    server.close().catch(() => {});
    // In-flight polls may hold timers; do not let them outlive the client.
    setTimeout(() => process.exit(0), 2000).unref();
  });
  await server.connect(transport);
}

module.exports = { runMcpCommand, createMcpServer, parseMcpArgs, buildClientConfig, SERVER_INSTRUCTIONS };
