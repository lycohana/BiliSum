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
  if (task.reused === true) {
    lines.push("reused: true");
  }
  const sources = formatModelSources(task.result && task.result.model_sources);
  if (sources) {
    lines.push(`model_sources: ${sources}`);
  }
  return lines;
}

/** 执行来源 of the steps that ran, e.g. "summary=local, knowledge_note=local" ("none" steps omitted). */
function formatModelSources(sources) {
  if (!sources || typeof sources !== "object") {
    return "";
  }
  return Object.entries(sources)
    .filter(([, source]) => source && source !== "none")
    .map(([step, source]) => `${step}=${source}`)
    .join(", ");
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
    reused: task.reused === true,
    batch_id: task.batch_id ?? null,
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

/** One stderr progress line for a reporter progress event. */
function formatProgressLine({ taskId, progress, stage, message }) {
  const pct = Number(progress);
  const pctText = Number.isFinite(pct) ? `${String(Math.max(0, Math.min(100, Math.round(pct)))).padStart(3)}%` : "  -%";
  return `[${taskId}] ${pctText}${stage ? ` [${stage}]` : ""}${message ? ` ${message}` : ""}`;
}

/** `bilisum status` human-readable block. */
function formatStatusText(task) {
  const lines = [
    `task_id:       ${task.task_id}`,
    `status:        ${task.status}`,
  ];
  if (task.title) {
    lines.push(`title:         ${task.title}`);
  }
  if (task.page_number) {
    lines.push(`page_number:   ${task.page_number}`);
  }
  if (task.error_message) {
    lines.push(`error:         ${task.error_message}`);
  }
  if (task.result && task.result.overview) {
    lines.push(`overview:      ${String(task.result.overview).slice(0, 200)}`);
  }
  return lines.join("\n");
}

/** `bilisum tasks` human-readable list for { tasks, total }. */
function formatTaskListText({ tasks, total }) {
  if (!tasks.length) {
    return "暂无任务。";
  }
  const lines = tasks.map((task) => `${task.task_id}  ${task.status.padEnd(10)} ${task.title || task.source || ""}`);
  if (total > tasks.length) {
    lines.push(`（共 ${total} 个任务，仅显示前 ${tasks.length} 个。用 --limit 调整。）`);
  }
  return lines.join("\n");
}

/** `bilisum env` status block for getEnvStatus() data. */
function formatEnvStatusText(status) {
  const { current } = status;
  const lines = [
    `当前环境:      ${current.env}`,
    `服务地址:      http://${current.host}:${current.port}`,
    `数据根:        ${current.dataRoot}`,
    "",
    "可用环境:",
    `  desktop    ${status.desktopReachable ? "● 桌面端服务在运行" : "○ 未检测到桌面端服务（127.0.0.1:3838）"}`,
    `  cli        ${status.cliReady ? "● 独立环境已就绪" : "○ 未初始化（bilisum env setup）"}  CLI_HOME=${status.cliHome}`,
    `  custom     ${status.customConfigured ? "● 已配置" : "○ 未配置（--host/--port/--token）"}`,
    "  auto       desktop 优先，否则 cli",
    "",
    "切换：bilisum env use desktop|cli|custom|auto（也可直接 bilisum desktop|cli|custom|auto）",
  ];
  if (status.persisted) {
    lines.push(`已持久化选择：${status.persisted}`);
  }
  return lines.join("\n");
}

/** `bilisum doctor` block for runDoctor() data. */
function formatDoctorText(report) {
  const lines = [
    `BiliSum package: ${report.version}`,
    `Environment:     ${report.environment}`,
    `Service:         http://${report.host}:${report.port}`,
    `Data root:       ${report.dataRoot}`,
    `Python:          ${report.python || "未找到（请安装桌面版 BiliSum、Python 3.12，或运行 bilisum env setup）"}`,
    `Token:           ${report.tokenFound ? "已找到" : "未找到（desktop 环境请先启动桌面端；cli 环境运行后自动生成）"}`,
  ];
  if (report.cliEnvReady !== null) {
    lines.push(`CLI env status:  ${report.cliEnvReady ? "就绪" : "未初始化（运行 bilisum env setup）"}`);
  }
  return lines.join("\n");
}

module.exports = {
  pickMarkdown,
  pickBriefMarkdown,
  metadataLines,
  formatMarkdownTask,
  toJson,
  formatTasks,
  emit,
  formatProgressLine,
  formatStatusText,
  formatTaskListText,
  formatEnvStatusText,
  formatDoctorText,
};
