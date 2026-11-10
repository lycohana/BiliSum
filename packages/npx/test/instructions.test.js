"use strict";

/**
 * C2: `--instructions` (补充要求) and `--language` reach the create-task request.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { startFakeService, runCli } = require("./helpers/process-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";

function routes() {
  return {
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages: [] } },
    "POST /api/v1/videos/:id/tasks": { task_id: "t1", status: "queued", title: "Demo" },
  };
}

async function withService(fn) {
  const service = await startFakeService(routes());
  try {
    return await fn(service);
  } finally {
    await service.close();
  }
}

function createBody(service) {
  const create = service.requests.find((r) => r.method === "POST" && r.path === "/api/v1/videos/BV1xx/tasks");
  assert.ok(create, "task was not created");
  return create.body;
}

test("summarize sends --instructions and --language with the task", async () => {
  await withService(async (service) => {
    const result = await runCli([
      "summarize", URL_SOURCE, "--no-wait", "-q",
      "--instructions", "重点讲技术细节", "--language", "en",
      ...service.connectArgs,
    ]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(createBody(service), { extra_instructions: "重点讲技术细节", language: "en" });
  });
});

test("brief also forwards instructions alongside its summary scope", async () => {
  await withService(async (service) => {
    const result = await runCli([
      "brief", URL_SOURCE, "--no-wait", "-q", "--instructions", "只要结论", ...service.connectArgs,
    ]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(createBody(service), { summary_scope: "summary", extra_instructions: "只要结论" });
  });
});

test("without the flags the request body is unchanged", async () => {
  await withService(async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--no-wait", "-q", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(createBody(service), {});
  });
});

test("instructions longer than 1000 characters fail before contacting the service", async () => {
  await withService(async (service) => {
    const result = await runCli([
      "summarize", URL_SOURCE, "--instructions", "字".repeat(1001), ...service.connectArgs,
    ]);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /补充要求不能超过 1000 字/);
    assert.equal(service.requests.length, 0);
  });
});

test("help lists the new options", async () => {
  const result = await runCli(["help"]);
  assert.match(result.stdout, /--instructions <text>/);
  assert.match(result.stdout, /--language <code>/);
});
