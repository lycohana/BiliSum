"use strict";

// F1a: `bilisum transcribe` creates transcript_only tasks unless --with-summary.
// Drives the real CLI process against a fake BiliSum HTTP service.

const test = require("node:test");
const assert = require("node:assert/strict");

const http = require("node:http");
const { spawn } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const CLI_PATH = join(__dirname, "..", "bin", "bilisum.js");
const TOKEN = "fake-token";

function startFakeService() {
  const taskBodies = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      const send = (status, payload) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(payload));
      };
      if (req.method === "GET" && req.url === "/health") {
        return send(200, { service: "BiliSum", status: "ok" });
      }
      if (req.method === "POST" && req.url === "/api/v1/videos/probe") {
        return send(200, { video: { video_id: "vid-1", title: "测试视频", pages: [] } });
      }
      if (req.method === "POST" && req.url === "/api/v1/videos/vid-1/tasks") {
        taskBodies.push(JSON.parse(raw || "{}"));
        return send(201, { task_id: `task-${taskBodies.length}`, status: "queued" });
      }
      return send(404, { detail: `unexpected ${req.method} ${req.url}` });
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port, taskBodies }));
  });
}

function runCli(home, args) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLI_PATH, ...args], {
      env: { ...process.env, BILISUM_CLI_HOME: home },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

async function createdTaskBody(extraArgs) {
  const home = mkdtempSync(join(tmpdir(), "bilisum-transcribe-scope-"));
  const { server, port, taskBodies } = await startFakeService();
  try {
    const result = await runCli(home, [
      ...extraArgs,
      "https://www.bilibili.com/video/BV1xx411c7mD",
      "--host", "127.0.0.1",
      "--port", String(port),
      "--token", TOKEN,
      "--no-wait",
      "--quiet",
    ]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(taskBodies.length, 1);
    return taskBodies[0];
  } finally {
    server.close();
    rmSync(home, { recursive: true, force: true });
  }
}

test("transcribe creates a transcript_only task by default", async () => {
  const body = await createdTaskBody(["transcribe"]);
  assert.equal(body.summary_scope, "transcript_only");
});

test("transcribe --with-summary keeps the default summary scope", async () => {
  const body = await createdTaskBody(["transcribe", "--with-summary"]);
  assert.equal(body.summary_scope, undefined);
});

test("summarize does not send a summary scope", async () => {
  const body = await createdTaskBody(["summarize"]);
  assert.equal(body.summary_scope, undefined);
});

test("brief still requests the summary scope", async () => {
  const body = await createdTaskBody(["brief"]);
  assert.equal(body.summary_scope, "summary");
});
