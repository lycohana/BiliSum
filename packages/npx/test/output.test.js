"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { pickMarkdown, toJson, formatTasks } = require("../lib/output");

function task(overrides = {}) {
  return {
    task_id: "t1",
    video_id: "v1",
    title: "标题",
    page_number: 1,
    status: "completed",
    error_code: null,
    error_message: null,
    result: {
      overview: "概览",
      knowledge_note_markdown: "# 笔记",
      transcript_text: "转写全文",
      key_points: ["要点一", "要点二"],
    },
    ...overrides,
  };
}

test("pickMarkdown prefers knowledge_note_markdown", () => {
  assert.equal(pickMarkdown(task()), "# 笔记");
});

test("pickMarkdown falls back to overview + key points", () => {
  const t = task({
    result: { overview: "概览", key_points: ["要点一", "要点二"], knowledge_note_markdown: "" },
  });
  const md = pickMarkdown(t);
  assert.match(md, /概览/);
  assert.match(md, /要点一/);
  assert.match(md, /要点二/);
});

test("pickMarkdown returns empty without result", () => {
  assert.equal(pickMarkdown({}), "");
});

test("toJson keeps agent-friendly shape", () => {
  const json = toJson(task());
  assert.deepEqual(Object.keys(json).sort(), [
    "error_code",
    "error_message",
    "page_number",
    "result",
    "status",
    "task_id",
    "title",
    "video_id",
  ]);
  assert.equal(json.task_id, "t1");
  assert.equal(json.result.overview, "概览");
});

test("formatTasks json: single task is an object", () => {
  const text = formatTasks([task()], "json");
  const parsed = JSON.parse(text);
  assert.equal(parsed.task_id, "t1");
  assert.equal(Array.isArray(parsed), false);
});

test("formatTasks json: multiple tasks wrap in tasks array", () => {
  const text = formatTasks([task(), task({ task_id: "t2" })], "json");
  const parsed = JSON.parse(text);
  assert.equal(parsed.tasks.length, 2);
  assert.equal(parsed.tasks[1].task_id, "t2");
});

test("formatTasks markdown joins with separator", () => {
  const text = formatTasks([task(), task({ task_id: "t2", result: { knowledge_note_markdown: "# 二" } })], "markdown");
  assert.match(text, /# 笔记/);
  assert.match(text, /# 二/);
  assert.match(text, /---/);
});

test("formatTasks markdown includes metadata header by default", () => {
  const text = formatTasks([task({ source: "https://bilibili.com/video/BV1xx" })], "markdown");
  assert.match(text, /task_id: t1/);
  assert.match(text, /status: completed/);
  assert.match(text, /title: 标题/);
  assert.match(text, /source: https:\/\/bilibili\.com\/video\/BV1xx/);
  // Metadata header appears before the summary body.
  assert.ok(text.indexOf("task_id: t1") < text.indexOf("# 笔记"));
});

test("formatTasks markdown omits transcript unless requested", () => {
  const without = formatTasks([task()], "markdown");
  assert.ok(!without.includes("转写全文"));
  assert.ok(!without.includes("转写全文"));
  const withTranscript = formatTasks([task()], "markdown", { includeTranscript: true });
  assert.ok(withTranscript.includes("## 转写全文"));
  assert.ok(withTranscript.includes("转写全文"));
});

test("formatTasks transcript prints transcript_text", () => {
  assert.equal(formatTasks([task()], "transcript"), "转写全文");
});
