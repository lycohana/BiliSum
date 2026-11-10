"use strict";

/**
 * result: read a finished task's result in one format, optionally filtering
 * the transcript by time range / keyword, and page it by offset / maxChars.
 * Shared by `bilisum result` and the MCP `bilisum_result` tool.
 */

const { UsageError } = require("../args");
const { formatMarkdownTask, pickBriefMarkdown, toJson } = require("../output");
const { DEFAULT_CONTEXT, filterTranscript, hasSegments, paginateText, parseTimeRange } = require("../transcript");
const { getTaskStatus } = require("./status");

/** Structured result fields without the transcript (format "json"). */
function resultJson(task, { withTranscript = false } = {}) {
  const payload = toJson(task);
  if (payload.result && !withTranscript) {
    const { transcript_text: _text, segments: _segments, ...rest } = payload.result;
    payload.result = rest;
  }
  return JSON.stringify(payload, null, 2);
}

/** Transcript text for a result: the whole transcript, or the segments matching a time range / keyword. */
function transcriptText(result, { timeRange, query, context }) {
  if (!timeRange && !query) {
    return String(result.transcript_text || "").trim();
  }
  if (!hasSegments(result.segments)) {
    throw new Error("该任务的转写没有带时间戳的分段，无法按时间段或关键词筛选。");
  }
  return filterTranscript(result.segments, { timeRange, query, context });
}

/** Render a task result in the requested format (markdown, brief, transcript, json). */
function renderResult(task, format, filters, options) {
  const result = task.result || {};
  if (format === "transcript") {
    return transcriptText(result, filters);
  }
  if (format === "brief") {
    return pickBriefMarkdown(result);
  }
  if (format === "json") {
    return resultJson(task, { withTranscript: options.withTranscript });
  }
  return formatMarkdownTask(task, { includeTranscript: options.withTranscript });
}

/**
 * options: connection fields + format (markdown|brief|transcript|json),
 * withTranscript, time ("12:00-15:30" or ""), timeRange ({start,end} seconds,
 * alternative to time), grep / query, context, offset, maxChars (null = all).
 * Returns { task_id, status, title, video_id, page_number, format, filters,
 *           content, offset, next_offset, total_chars }.
 */
async function getTaskResult(taskId, options, reporter) {
  const format = options.format || "markdown";
  const timeRange = options.timeRange || (options.time ? parseTimeRange(options.time) : null);
  const query = String(options.query || options.grep || "").trim();
  const context = Number.isInteger(options.context) ? options.context : DEFAULT_CONTEXT;
  if ((timeRange || query) && format !== "transcript") {
    throw new UsageError("--time / --grep 只能用于 transcript 格式。");
  }

  const task = await getTaskStatus(taskId, options, reporter);
  if (task.status !== "completed" || !task.result) {
    const reason = task.error_message ? `：${task.error_message}` : "";
    throw new Error(`任务尚未完成（${task.status}）${reason}：${taskId}`);
  }

  const text = renderResult(task, format, { timeRange, query, context }, options);
  const page = paginateText(text, { offset: options.offset || 0, maxChars: options.maxChars || null });
  return {
    task_id: task.task_id,
    status: task.status,
    title: task.title ?? null,
    video_id: task.video_id ?? null,
    page_number: task.page_number ?? null,
    model_sources: task.result.model_sources ?? null,
    format,
    filters: {
      time_range: timeRange,
      query: query || null,
      context: query ? context : null,
    },
    ...page,
  };
}

module.exports = { getTaskResult };
