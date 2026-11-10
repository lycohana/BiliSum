"use strict";

/**
 * bilisum process seam: a fake BiliSum HTTP service plus a helper that runs
 * the real CLI binary against it in an isolated CLI home.
 *
 * Reuse from later tickets (MCP server, agent commands, reuse/result reading):
 *
 *   const service = await startFakeService({
 *     "POST /api/v1/videos/probe": { video: { video_id: "BV1", pages: [] } },
 *     "GET /api/v1/tasks/:id": (req) => ({ task_id: req.params.id, status: "completed" }),
 *     "GET /api/v1/tasks/missing": { status: 404, json: { detail: "not found" } },
 *   });
 *   const result = await runCli(["status", "t1", ...service.connectArgs]);
 *   service.requests  // [{ method, path, query, headers, body }]
 *   await service.close();
 *
 * Route values: a JSON payload (status 200), `{ status, json }` (a Buffer `json`
 * is sent as raw bytes, e.g. frame images), or a function
 * `(req) => payload | { status, json }`. `:name` segments bind `req.params`.
 * Exact paths win over parameterised ones. `GET /health` answers as BiliSum
 * unless overridden and is only counted (`service.healthChecks`), not
 * recorded. Unknown routes return 404.
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const CLI_PATH = join(__dirname, "..", "..", "bin", "bilisum.js");
const FAKE_TOKEN = "fake-token";

function isResponse(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).every((key) => key === "status" || key === "json")
    && typeof value.status === "number";
}

function compileRoutes(routes) {
  return Object.entries({ "GET /health": { service: "BiliSum" }, ...routes }).map(([key, handler]) => {
    const [method, pattern] = key.split(" ");
    const parts = pattern.split("/");
    return { method, parts, handler, exact: !pattern.includes(":") };
  }).sort((a, b) => Number(b.exact) - Number(a.exact));
}

function matchRoute(compiled, method, path) {
  const segments = path.split("/");
  for (const route of compiled) {
    if (route.method !== method || route.parts.length !== segments.length) {
      continue;
    }
    const params = {};
    const ok = route.parts.every((part, index) => {
      if (part.startsWith(":")) {
        params[part.slice(1)] = decodeURIComponent(segments[index]);
        return true;
      }
      return part === segments[index];
    });
    if (ok) {
      return { route, params };
    }
  }
  return null;
}

async function startFakeService(routes = {}) {
  const compiled = compileRoutes(routes);
  const requests = [];
  const health = { count: 0 };
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const url = new URL(req.url, "http://fake");
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = raw;
      if (String(req.headers["content-type"] || "").includes("application/json") && raw) {
        try {
          body = JSON.parse(raw);
        } catch {
          body = raw;
        }
      }
      const record = {
        method: req.method,
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        headers: req.headers,
        body,
      };
      if (url.pathname === "/health") {
        health.count += 1;
      } else {
        requests.push(record);
      }
      const match = matchRoute(compiled, req.method, url.pathname);
      let status = 404;
      let json = { detail: "Not Found" };
      if (match) {
        let value = match.route.handler;
        if (typeof value === "function") {
          value = value({ ...record, params: match.params });
        }
        if (isResponse(value)) {
          status = value.status;
          json = value.json;
        } else {
          status = 200;
          json = value;
        }
      }
      if (Buffer.isBuffer(json)) {
        res.writeHead(status, { "Content-Type": "application/octet-stream" });
        res.end(json);
        return;
      }
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(json === undefined ? "" : JSON.stringify(json));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    port,
    requests,
    get healthChecks() {
      return health.count;
    },
    connectArgs: ["--host", "127.0.0.1", "--port", String(port), "--token", FAKE_TOKEN],
    close: () => new Promise((resolve) => {
      server.closeAllConnections?.();
      server.close(() => resolve());
    }),
  };
}

/** Inherited env minus BILISUM_* / VIDEO_SUM_ACCESS_TOKEN, plus an isolated CLI home. */
function isolatedEnv(cliHome, env = {}) {
  const baseEnv = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith("BILISUM_") || key === "VIDEO_SUM_ACCESS_TOKEN") {
      continue;
    }
    baseEnv[key] = value;
  }
  return { ...baseEnv, BILISUM_CLI_HOME: cliHome, ...env };
}

/**
 * Run the CLI as a child process (async, so an in-process fake service keeps
 * serving). Uses a fresh BILISUM_CLI_HOME unless `home` is given, and strips
 * BILISUM_* / VIDEO_SUM_ACCESS_TOKEN from the inherited env.
 * Resolves { status, stdout, stderr, rawStatus, rawStderr, home }.
 */
function runCli(args, { home, env = {}, cwd, input } = {}) {
  const ownHome = !home;
  const cliHome = home || mkdtempSync(join(tmpdir(), "bilisum-seam-"));
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLI_PATH, ...args], {
      env: isolatedEnv(cliHome, env),
      cwd,
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    if (input !== undefined) {
      child.stdin.end(input);
    }
    child.on("close", (rawStatus) => {
      if (ownHome) {
        rmSync(cliHome, { recursive: true, force: true });
      }
      const crashed = WIN_EXIT_CRASH.test(stderr);
      resolve({
        status: crashed && rawStatus === WIN_EXIT_CRASH_STATUS ? 1 : rawStatus,
        stdout,
        stderr: stderr.replace(WIN_EXIT_CRASH, ""),
        rawStatus,
        rawStderr: stderr,
        home: cliHome,
      });
    });
  });
}

// Pre-existing Node-on-Windows crash: `process.exit(1)` after a fetch() aborts
// with this libuv assertion and exit code 0xC0000409 instead of 1. It is not
// part of the CLI's own output, so runCli folds it back to exit 1 and strips
// the line; `rawStatus` / `rawStderr` keep the unmodified values.
const WIN_EXIT_CRASH = /Assertion failed: !\(handle->flags & UV_HANDLE_CLOSING\)[^\n]*\n?/;
const WIN_EXIT_CRASH_STATUS = 3221226505;

module.exports = { startFakeService, runCli, isolatedEnv, CLI_PATH, FAKE_TOKEN };
