"use strict";

/**
 * bilisum MCP seam: spawn `bin/bilisum.js mcp <args>` and drive it with the
 * official SDK client over its stdio, keeping every raw stdout byte so tests
 * can assert stdout carries nothing but protocol messages.
 *
 *   const service = await startFakeService(routes);   // process-seam.js
 *   const mcp = await startMcp(service.connectArgs);
 *   const result = await mcp.client.callTool({ name: "bilisum_status", arguments: { task_id: "t1" } });
 *   mcp.stdout   // raw stdout text
 *   mcp.stderr   // raw stderr text
 *   await mcp.close();
 */

const { spawn } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { ReadBuffer, serializeMessage } = require("@modelcontextprotocol/sdk/shared/stdio.js");

const { CLI_PATH, isolatedEnv } = require("./process-seam");

/** SDK Transport over an already-spawned child, recording raw stdout. */
class ChildStdioTransport {
  constructor(child) {
    this.child = child;
    this.stdout = "";
    this.stderr = "";
    this.parseErrors = [];
    this._buffer = new ReadBuffer();
  }

  async start() {
    this.child.stdout.on("data", (chunk) => {
      this.stdout += chunk.toString("utf8");
      this._buffer.append(chunk);
      while (true) {
        let message;
        try {
          message = this._buffer.readMessage();
        } catch (error) {
          this.parseErrors.push(error);
          continue;
        }
        if (message === null) {
          break;
        }
        this.onmessage?.(message);
      }
    });
    this.child.stderr.on("data", (chunk) => {
      this.stderr += chunk.toString("utf8");
    });
    this.child.on("close", () => this.onclose?.());
  }

  async send(message) {
    this.child.stdin.write(serializeMessage(message));
  }

  async close() {
    this.child.stdin.end();
  }
}

async function startMcp(args = [], { home, env = {}, clientName = "bilisum-test" } = {}) {
  const ownHome = !home;
  const cliHome = home || mkdtempSync(join(tmpdir(), "bilisum-mcp-seam-"));
  const child = spawn(process.execPath, [CLI_PATH, "mcp", ...args], {
    env: isolatedEnv(cliHome, env),
    windowsHide: true,
  });
  const exited = new Promise((resolve) => child.on("close", (code) => resolve(code)));
  const transport = new ChildStdioTransport(child);
  const client = new Client({ name: clientName, version: "0.0.0" });
  await client.connect(transport);
  return {
    client,
    child,
    get stdout() {
      return transport.stdout;
    },
    get stderr() {
      return transport.stderr;
    },
    get parseErrors() {
      return transport.parseErrors;
    },
    async close() {
      await client.close();
      const timer = setTimeout(() => child.kill(), 5000);
      const code = await exited;
      clearTimeout(timer);
      if (ownHome) {
        rmSync(cliHome, { recursive: true, force: true });
      }
      return code;
    },
  };
}

module.exports = { startMcp };
