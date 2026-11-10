"use strict";

/**
 * C1: `bilisum result <task-id>` — read a finished task's result, with the
 * transcript filtered by time range (--time) and keyword (--grep) and paged
 * by --offset / --max-chars. Filtering is local, from the result's segments.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { startFakeService, runCli } = require("./helpers/process-seam");

const SEGMENTS = [
  { start: 0, end: 30, text: "开场介绍" },
  { start: 30, end: 60, text: "第一部分讲缓存" },
  { start: 60, end: 90, text: "缓存失效的问题" },
  { start: 90, end: 120, text: "中场休息" },
  { start: 120, end: 150, text: "第二部分讲数据库" },
  { start: 150, end: 180, text: "数据库索引" },
  { start: 180, end: 210, text: "总结回顾" },
  { start: 3600, end: 3630, text: "彩蛋：缓存再谈" },
];

function task(overrides = {}) {
  return {
    task_id: "t1",
    status: "completed",
    title: "Demo",
    video_id: "BV1xx",
    source: "https://www.bilibili.com/video/BV1xx",
    result: {
      overview: "Short overview",
      key_points: ["point A"],
      knowledge_note_markdown: "# Note body",
      transcript_text: SEGMENTS.map((s) => s.text).join("\n"),
      segments: SEGMENTS,
    },
    ...overrides,
  };
}

async function result(args, { taskBody = task() } = {}) {
  const service = await startFakeService({
    "GET /api/v1/tasks/:id": (req) =>
      req.params.id === "missing" ? { status: 404, json: { detail: "Task not found." } } : { ...taskBody, task_id: req.params.id },
  });
  try {
    const run = await runCli(["result", ...args, ...service.connectArgs]);
    return { ...run, requests: service.requests };
  } finally {
    await service.close();
  }
}

test("result defaults to markdown: metadata + knowledge note", async () => {
  const run = await result(["t1"]);
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stdout, /^---\ntask_id: t1\nstatus: completed\n/);
  assert.match(run.stdout, /# Note body\n$/);
  assert.deepEqual(run.requests.map((r) => `${r.method} ${r.path}`), ["GET /api/v1/tasks/t1"]);
});

test("result --format brief prints the summary card", async () => {
  const run = await result(["t1", "--format", "brief"]);
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stdout, /Short overview\n\n## 要点\n- point A\n$/);
});

test("result --format transcript prints the full transcript", async () => {
  const run = await result(["t1", "--format", "transcript"]);
  assert.equal(run.status, 0, run.stderr);
  assert.equal(run.stdout, `${SEGMENTS.map((s) => s.text).join("\n")}\n`);
});

test("result --format json omits the transcript", async () => {
  const run = await result(["t1", "--format", "json"]);
  assert.equal(run.status, 0, run.stderr);
  const body = JSON.parse(run.stdout);
  assert.equal(body.task_id, "t1");
  assert.equal(body.result.overview, "Short overview");
  assert.equal(body.result.transcript_text, undefined);
  assert.equal(body.result.segments, undefined);
});

const LOCAL_SOURCES = { summary: "local", knowledge_note: "local", mindmap: "none" };

test("result --format json carries model_sources", async () => {
  const taskBody = task();
  taskBody.result.model_sources = LOCAL_SOURCES;
  const run = await result(["t1", "--format", "json"], { taskBody });
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout).result.model_sources, LOCAL_SOURCES);
});

test("result --json envelope carries model_sources", async () => {
  const taskBody = task();
  taskBody.result.model_sources = LOCAL_SOURCES;
  const run = await result(["t1", "--json"], { taskBody });
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout).model_sources, LOCAL_SOURCES);
});

test("result markdown header names the steps that ran and their source", async () => {
  const taskBody = task();
  taskBody.result.model_sources = LOCAL_SOURCES;
  const run = await result(["t1"], { taskBody });
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stdout, /\nmodel_sources: summary=local, knowledge_note=local\n---\n/);
});

test("--time keeps segments overlapping the range, half-open at both ends", async () => {
  const run = await result(["t1", "--format", "transcript", "--time", "00:30-02:00"]);
  assert.equal(run.status, 0, run.stderr);
  assert.equal(
    run.stdout,
    "[00:30-01:00] 第一部分讲缓存\n[01:00-01:30] 缓存失效的问题\n[01:30-02:00] 中场休息\n",
  );
});

test("--time cuts through a segment and still includes it", async () => {
  const run = await result(["t1", "--format", "transcript", "--time", "0:45-1:05"]);
  assert.equal(run.stdout, "[00:30-01:00] 第一部分讲缓存\n[01:00-01:30] 缓存失效的问题\n");
});

test("--time accepts hours and prints H:MM:SS timestamps", async () => {
  const run = await result(["t1", "--format", "transcript", "--time", "1:00:00-1:01:00"]);
  assert.equal(run.stdout, "[1:00:00-1:00:30] 彩蛋：缓存再谈\n");
});

test("--time with nothing inside says so", async () => {
  const run = await result(["t1", "--format", "transcript", "--time", "10:00-20:00"]);
  assert.equal(run.status, 0, run.stderr);
  assert.equal(run.stdout, "（没有符合条件的转写片段。）\n");
});

test("--time rejects malformed and reversed ranges", async () => {
  for (const bad of ["12:00", "abc-1:00", "2:00-1:00", "1:70-2:00"]) {
    const run = await result(["t1", "--format", "transcript", "--time", bad]);
    assert.equal(run.status, 1, bad);
    assert.match(run.stderr, /--time/, bad);
  }
});

test("--grep returns hits with surrounding context, hits marked, groups separated", async () => {
  const run = await result(["t1", "--format", "transcript", "--grep", "数据库", "--context", "1"]);
  assert.equal(run.status, 0, run.stderr);
  assert.equal(
    run.stdout,
    [
      "  [01:30-02:00] 中场休息",
      "* [02:00-02:30] 第二部分讲数据库",
      "* [02:30-03:00] 数据库索引",
      "  [03:00-03:30] 总结回顾",
      "",
    ].join("\n"),
  );
});

test("--grep separate hit windows are split by an ellipsis line", async () => {
  const run = await result(["t1", "--format", "transcript", "--grep", "缓存", "--context", "0"]);
  assert.equal(
    run.stdout,
    [
      "* [00:30-01:00] 第一部分讲缓存",
      "* [01:00-01:30] 缓存失效的问题",
      "  ……",
      "* [1:00:00-1:00:30] 彩蛋：缓存再谈",
      "",
    ].join("\n"),
  );
});

test("--grep is case-insensitive and context defaults to 2 segments", async () => {
  const body = task({
    result: {
      segments: [
        { start: 0, end: 1, text: "a" },
        { start: 1, end: 2, text: "b" },
        { start: 2, end: 3, text: "c" },
        { start: 3, end: 4, text: "Redis here" },
        { start: 4, end: 5, text: "d" },
        { start: 5, end: 6, text: "e" },
        { start: 6, end: 7, text: "f" },
      ],
    },
  });
  const run = await result(["t1", "--grep", "redis"], { taskBody: body });
  assert.equal(run.status, 0, run.stderr);
  assert.equal(
    run.stdout,
    [
      "  [00:01-00:02] b",
      "  [00:02-00:03] c",
      "* [00:03-00:04] Redis here",
      "  [00:04-00:05] d",
      "  [00:05-00:06] e",
      "",
    ].join("\n"),
  );
});

test("--time and --grep combine: hits only inside the range, context stays in range", async () => {
  const run = await result(["t1", "--time", "0:00-3:00", "--grep", "缓存", "--context", "1"]);
  assert.equal(run.status, 0, run.stderr);
  assert.equal(
    run.stdout,
    [
      "  [00:00-00:30] 开场介绍",
      "* [00:30-01:00] 第一部分讲缓存",
      "* [01:00-01:30] 缓存失效的问题",
      "  [01:30-02:00] 中场休息",
      "",
    ].join("\n"),
  );
});

test("--max-chars pages a filtered transcript; pages concatenate to the whole", async () => {
  const whole = (await result(["t1", "--time", "0:00-3:30"])).stdout;
  const pages = [];
  let offset = 0;
  for (let guard = 0; guard < 50; guard += 1) {
    const run = await result(["t1", "--time", "0:00-3:30", "--offset", String(offset), "--max-chars", "40", "--json"]);
    assert.equal(run.status, 0, run.stderr);
    const body = JSON.parse(run.stdout);
    assert.ok(body.content.length <= 40);
    pages.push(body.content);
    assert.equal(body.offset, offset);
    assert.equal(body.total_chars, whole.length - 1);
    if (body.next_offset === null) {
      break;
    }
    offset = body.next_offset;
  }
  assert.ok(pages.length > 1);
  assert.equal(`${pages.join("")}\n`, whole);
});

test("--max-chars on plain text output notes the next offset on stderr", async () => {
  const run = await result(["t1", "--time", "0:00-3:30", "--max-chars", "40"]);
  assert.equal(run.status, 0, run.stderr);
  assert.ok(run.stdout.length <= 41);
  assert.match(run.stderr, /--offset 40/);
});

test("--time / --grep imply transcript and refuse other explicit formats", async () => {
  const run = await result(["t1", "--format", "brief", "--grep", "缓存"]);
  assert.equal(run.status, 1);
  assert.match(run.stderr, /--time \/ --grep 只能用于 transcript/);
});

test("filtering a transcript without segments explains why", async () => {
  const body = task({ result: { transcript_text: "no timestamps", segments: [] } });
  const run = await result(["t1", "--grep", "no"], { taskBody: body });
  assert.equal(run.status, 1);
  assert.match(run.stderr, /没有带时间戳的分段/);
});

test("result of an unfinished task is an error naming its status", async () => {
  const run = await result(["t1"], { taskBody: task({ status: "running", result: null }) });
  assert.equal(run.status, 1);
  assert.match(run.stderr, /任务尚未完成（running）/);
});

test("result of a missing task is an error", async () => {
  const run = await result(["missing"]);
  assert.equal(run.status, 1);
  assert.match(run.stderr, /任务不存在：missing/);
});

test("result without a task id is a usage error", async () => {
  const run = await result([]);
  assert.equal(run.status, 1);
  assert.match(run.stderr, /缺少 task-id/);
});
