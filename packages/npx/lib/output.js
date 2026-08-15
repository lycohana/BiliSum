"use strict";

/**
 * Output formatting for BiliSum task results.
 *
 * - `--format json`: stable machine-readable shape for agents (full result,
 *   including transcript when present).
 * - `--format markdown` (default): metadata header + summary content only;
 *   transcript is omitted unless `--with-transcript` is passed.
 * - `--format transcript`: transcript text only (for `bilisum transcribe`).
 *
 * Machine-readable output goes to stdout; progress chatter goes to stderr.
 */

const { writeFileSync } = require("node:fs");

function pickMarkdown(task, { brief = false } = {}) {
  const result = task && task.result;
  if (!result) {
    return "";
  }
  if (!brief) {
    const note = String(result.knowledge_note_markdown || "").trim();
    if (note) {
      return note;
    }
  }
  return pickBriefMarkdown(result);
}

/** Summary-card body: overview + key points + chapters (no knowledge note). */
function pickBriefMarkdown(result) {
  const overview = String(result.overview || "").trim();
  const keyPoints = Array.isArray(result.key_points) ? result.key_points.filter((item) => String(item).trim()) : [];
  const chapters = Array.isArray(result.timeline) ? result.timeline.filter((item) => item && typeof item === "object") : [];
  const parts = [];
  if (overview) {
    parts.push(overview);
  }
  if (keyPoints.length) {
    parts.push("", "## 要点", ...keyPoints.map((item) => `- ${item}`));
  }
  if (chapters.length) {
    parts.push("", "## 章节");
    for (const chapter of chapters) {
      const start = formatSeconds(chapter.start);
      const title = String(chapter.title || "").trim() || "章节";
      const summary = String(chapter.summary || "").trim();
      parts.push(`- ${start ? `[${start}] ` : ""}${title}${summary ? `：${summary}` : ""}`);
    }
  }
  return parts.join("\n").trim();
}

function formatSeconds(value) {
  const total = Number(value);
  if (!Number.isFinite(total) || total < 0) {
    return "";
  }
  const minutes = Math.floor(total / 60);
  const seconds = Math.floor(total % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function metadataLines(task) {
  const lines = [
    `task_id: ${task.task_id}`,
    `status: ${task.status}`,
  ];
  if (task.title) {
    lines.push(`title: ${task.title}`);
  }
  if (task.video_id) {
    lines.push(`video_id: ${task.video_id}`);
  }
  if (task.page_number) {
    lines.push(`page: ${task.page_number}`);
  }
  if (task.source) {
    lines.push(`source: ${task.source}`);
  }
  return lines;
}

/** Markdown body: metadata header + summary (+ transcript on request). */
function formatMarkdownTask(task, { includeTranscript = false, brief = false } = {}) {
  const meta = metadataLines(task).join("\n");
  const body = pickMarkdown(task, { brief });
  const parts = [`---\n${meta}\n---`];
  if (body) {
    parts.push(body);
  }
  if (includeTranscript) {
    const transcript = String((task.result && task.result.transcript_text) || "").trim();
    if (transcript) {
      parts.push(`## 转写全文\n\n${transcript}`);
    }
  }
  return parts.join("\n\n");
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

function formatTasks(tasks, format, { includeTranscript = false, brief = false } = {}) {
  if (format === "json") {
    const payload = tasks.length === 1 ? toJson(tasks[0]) : { tasks: tasks.map(toJson) };
    return JSON.stringify(payload, null, 2);
  }
  if (format === "markdown") {
    return tasks
      .map((task) => formatMarkdownTask(task, { includeTranscript, brief }))
      .filter((body) => body)
      .join("\n\n---\n\n");
  }
  // transcript
  return tasks
    .map((task) => String((task.result && task.result.transcript_text) || "").trim())
    .filter((body) => body)
    .join("\n\n---\n\n");
}

function emit(text, options) {
  if (options.output) {
    writeFileSync(options.output, text.endsWith("\n") ? text : `${text}\n`, "utf8");
    return `结果已写入：${options.output}`;
  }
  process.stdout.write(`${text}\n`);
  return "";
}

module.exports = { pickMarkdown, pickBriefMarkdown, metadataLines, formatMarkdownTask, toJson, formatTasks, emit };
