"use strict";

/**
 * C1: summarize / brief / transcribe look up a reusable task before creating
 * one (GET /api/v1/videos/:id/reusable-task); --force skips the lookup.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { startFakeService, runCli } = require("./helpers/process-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";

function completedTask(taskId, overrides = {}) {
  return {
    task_id: taskId,
    status: "completed",
    title: "Demo",
    video_id: "BV1xx",
    source: URL_SOURCE,
    result: { knowledge_note_markdown: "# Note body", overview: "Short overview", transcript_text: "hello" },
    ...overrides,
  };
}

function routes({ reusable = null, pages = [] } = {}) {
  return {
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages } },
    "GET /api/v1/videos/:id/reusable-task": (req) =>
      typeof reusable === "function"
        ? reusable(req)
        : reusable || { status: 404, json: { detail: "没有可复用的任务。" } },
    "POST /api/v1/videos/:id/tasks": (req) => ({
      task_id: req.body.page_number ? `new${req.body.page_number}` : "new1",
      status: "queued",
      title: "Demo",
    }),
    "GET /api/v1/tasks/:id/progress": { status: "completed", progress: 100 },
    "GET /api/v1/tasks/:id": (req) => completedTask(req.params.id),
  };
}

async function run(args, options) {
  const service = await startFakeService(routes(options));
  try {
    const result = await runCli([...args, ...service.connectArgs]);
    return { ...result, requests: service.requests };
  } finally {
    await service.close();
  }
}

const created = (requests) => requests.filter((r) => r.method === "POST" && r.path.endsWith("/tasks"));
const lookups = (requests) => requests.filter((r) => r.path.endsWith("/reusable-task"));

test("a completed reusable task is returned without creating a new one", async () => {
  const result = await run(["summarize", URL_SOURCE], {
    reusable: { reused: true, match: "completed", task: completedTask("old1"), batch_id: null },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(created(result.requests).length, 0);
  assert.match(result.stdout, /^---\ntask_id: old1\nstatus: completed\n/);
  assert.match(result.stdout, /\nreused: true\n/);
  assert.match(result.stderr, /复用已有任务：old1（已完成）/);
});

test("the lookup sends page and the compared options", async () => {
  const result = await run(
    ["brief", URL_SOURCE, "--page", "2", "--prompt-preset", "p1", "--visual-note", "text"],
    { pages: [{ page: 1 }, { page: 2 }] },
  );
  assert.equal(result.status, 0, result.stderr);
  const [lookup] = lookups(result.requests);
  assert.equal(lookup.path, "/api/v1/videos/BV1xx/reusable-task");
  assert.deepEqual(lookup.query, {
    page_number: "2",
    summary_scope: "summary",
    prompt_preset_id: "p1",
    visual_note_mode: "text",
  });
  assert.equal(created(result.requests).length, 1);
});

test("transcribe looks up transcript-only tasks", async () => {
  const result = await run(["transcribe", URL_SOURCE, "-q"]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(lookups(result.requests)[0].query, { summary_scope: "transcript_only" });
});

test("an in-progress task is attached to and waited on", async () => {
  const result = await run(["summarize", URL_SOURCE, "--format", "json"], {
    reusable: { reused: true, match: "in_progress", task: { task_id: "run1", status: "running" }, batch_id: null },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(created(result.requests).length, 0);
  assert.ok(result.requests.some((r) => r.path === "/api/v1/tasks/run1/progress"));
  const body = JSON.parse(result.stdout);
  assert.equal(body.task_id, "run1");
  assert.equal(body.reused, true);
  assert.match(result.stderr, /接上进行中的任务：run1/);
});

test("no reusable task: a new task is created and marked as not reused", async () => {
  const result = await run(["summarize", URL_SOURCE, "--format", "json"]);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(lookups(result.requests).length, 1);
  assert.equal(created(result.requests).length, 1);
  assert.equal(JSON.parse(result.stdout).reused, false);
});

test("--force skips the lookup and creates a new task", async () => {
  const result = await run(["summarize", URL_SOURCE, "--force", "--format", "json"], {
    reusable: { reused: true, match: "completed", task: completedTask("old1"), batch_id: null },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(lookups(result.requests).length, 0);
  assert.equal(created(result.requests).length, 1);
  assert.equal(JSON.parse(result.stdout).task_id, "new1");
});

test("--no-wait reports the reused task and its flag", async () => {
  const result = await run(["summarize", URL_SOURCE, "--no-wait"], {
    reusable: { reused: true, match: "completed", task: completedTask("old1"), batch_id: null },
  });
  assert.equal(result.status, 0, result.stderr);
  const body = JSON.parse(result.stdout);
  assert.equal(body.task_id, "old1");
  assert.equal(body.reused, true);
  assert.equal(created(result.requests).length, 0);
});

test("--all-pages looks up each page and only creates the missing ones", async () => {
  const result = await run(["summarize", URL_SOURCE, "--all-pages", "--format", "json"], {
    pages: [{ page: 1 }, { page: 2 }],
    reusable: (req) =>
      req.query.page_number === "1"
        ? { reused: true, match: "completed", task: completedTask("old1"), batch_id: null }
        : { status: 404, json: { detail: "没有可复用的任务。" } },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(lookups(result.requests).map((r) => r.query.page_number), ["1", "2"]);
  assert.deepEqual(created(result.requests).map((r) => r.body.page_number), [2]);
  const body = JSON.parse(result.stdout);
  assert.deepEqual(body.tasks.map((t) => [t.task_id, t.reused]), [["old1", true], ["new2", false]]);
});
