"use strict";

/**
 * Output formatting for BiliSum task results.
 *
 * `--format json` emits a stable, machine-readable shape for agents;
 * `markdown` and `transcript` emit human-readable text. Everything machine
 * readable goes to stdout; progress chatter goes to stderr (see bin/bilisum.js).
 */

const { writeFileSync } = require("node:fs");

function pickMarkdown(task) {
  const result = task && task.result;
  if (!result) {
    return "";
  }
  const note = String(result.knowledge_note_markdown || "").trim();
  if (note) {
    return note;
  }
  const overview = String(result.overview || "").trim();
  const keyPoints = Array.isArray(result.key_points) ? result.key_points.filter((item) => String(item).trim()) : [];
  const parts = [];
  if (overview) {
    parts.push(overview);
  }
  if (keyPoints.length) {
    parts.push("", "## 要点", ...keyPoints.map((item) => `- ${item}`));
  }
  return parts.join("\n").trim();
}

function toJson(task) {
  return {
    task_id: task.task_id,
    video_id: task.video_id ?? null,
    title: task.title ?? null,
    page_number: task.page_number ?? null,
    status: task.status,
    error_code: task.error_code ?? null,
    error_message: task.error_message ?? null,
    result: task.result ?? null,
  };
}

function formatTasks(tasks, format) {
  if (format === "json") {
    const payload = tasks.length === 1 ? toJson(tasks[0]) : { tasks: tasks.map(toJson) };
    return JSON.stringify(payload, null, 2);
  }
  const bodies = tasks.map((task) => {
    if (format === "transcript") {
      return String((task.result && task.result.transcript_text) || "").trim();
    }
    return pickMarkdown(task);
  });
  return bodies.filter((body) => body).join("\n\n---\n\n");
}

function emit(text, options) {
  if (options.output) {
    writeFileSync(options.output, text.endsWith("\n") ? text : `${text}\n`, "utf8");
    return `结果已写入：${options.output}`;
  }
  process.stdout.write(`${text}\n`);
  return "";
}

module.exports = { pickMarkdown, toJson, formatTasks, emit };
