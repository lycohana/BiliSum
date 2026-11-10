"use strict";

/**
 * Transcript reading (C1): filter a task result's timestamped segments by time
 * range and keyword, and page any text by offset / max_chars. Pure functions;
 * shared by `bilisum result` and the MCP `bilisum_result` tool.
 */

const { UsageError } = require("./args");

const DEFAULT_CONTEXT = 2;
const NO_MATCH_TEXT = "（没有符合条件的转写片段。）";
const GAP_LINE = "  ……";

/** "SS", "MM:SS" or "H:MM:SS" -> seconds, or null when malformed. */
function parseTimestamp(raw) {
  const text = String(raw || "").trim();
  if (!/^\d+(:\d{1,2}){0,2}$/.test(text)) {
    return null;
  }
  const parts = text.split(":").map((part) => Number.parseInt(part, 10));
  if (parts.slice(1).some((part) => part >= 60)) {
    return null;
  }
  return parts.reduce((total, part) => total * 60 + part, 0);
}

/** "12:00-15:30" -> { start, end } seconds (half-open [start, end)). */
function parseTimeRange(raw) {
  const match = /^\s*([^-]+?)\s*-\s*([^-]+?)\s*$/.exec(String(raw || ""));
  const start = match ? parseTimestamp(match[1]) : null;
  const end = match ? parseTimestamp(match[2]) : null;
  if (start === null || end === null) {
    throw new UsageError(`--time 格式应为 开始-结束，例如 12:00-15:30，收到：${raw}`);
  }
  if (end <= start) {
    throw new UsageError(`--time 的结束时间必须晚于开始时间，收到：${raw}`);
  }
  return { start, end };
}

/** Seconds as `mm:ss`, or `h:mm:ss` from one hour on. */
function formatTimestamp(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Segments with numeric start/end (end >= start) and trimmed text; malformed entries are dropped. */
function normalizeSegments(segments) {
  return (Array.isArray(segments) ? segments : [])
    .filter((segment) => segment && typeof segment === "object" && Number.isFinite(Number(segment.start)))
    .map((segment) => {
      const start = Number(segment.start);
      const end = Number.isFinite(Number(segment.end)) ? Number(segment.end) : start;
      return { start, end: Math.max(start, end), text: String(segment.text || "").trim() };
    });
}

/** A segment is in range when it overlaps [start, end); zero-length ones when start is inside. */
function inRange(segment, range) {
  if (segment.end === segment.start) {
    return segment.start >= range.start && segment.start < range.end;
  }
  return segment.end > range.start && segment.start < range.end;
}

/** One transcript line: marker, `[start-end]` timestamps and text. */
function segmentLine(segment, marker) {
  return `${marker}[${formatTimestamp(segment.start)}-${formatTimestamp(segment.end)}] ${segment.text}`;
}

/**
 * Filter segments and render them with timestamps.
 *   timeRange: { start, end } seconds, or null
 *   query:     keyword (case-insensitive substring), or ""
 *   context:   segments of context on each side of a hit (default 2), within the time range
 * Without a query every in-range segment is printed plainly; with one, hits are
 * prefixed "* ", context "  ", and non-adjacent windows are split by "  ……".
 */
function filterTranscript(segments, { timeRange = null, query = "", context = DEFAULT_CONTEXT } = {}) {
  const selected = normalizeSegments(segments).filter((segment) => !timeRange || inRange(segment, timeRange));
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) {
    return selected.length ? selected.map((segment) => segmentLine(segment, "")).join("\n") : NO_MATCH_TEXT;
  }

  const hits = new Set();
  selected.forEach((segment, index) => {
    if (segment.text.toLowerCase().includes(needle)) {
      hits.add(index);
    }
  });
  if (!hits.size) {
    return NO_MATCH_TEXT;
  }
  const shown = new Set();
  for (const index of hits) {
    for (let near = Math.max(0, index - context); near <= Math.min(selected.length - 1, index + context); near += 1) {
      shown.add(near);
    }
  }
  const lines = [];
  let previous = null;
  for (const index of [...shown].sort((a, b) => a - b)) {
    if (previous !== null && index !== previous + 1) {
      lines.push(GAP_LINE);
    }
    lines.push(segmentLine(selected[index], hits.has(index) ? "* " : "  "));
    previous = index;
  }
  return lines.join("\n");
}

/** Whether the result has any timestamped segment to filter on. */
function hasSegments(segments) {
  return normalizeSegments(segments).length > 0;
}

/**
 * Page a text: { content, offset, next_offset (null at the end), total_chars }.
 * maxChars null/0 = everything from offset. Offsets count UTF-16 code units.
 */
function paginateText(text, { offset = 0, maxChars = null } = {}) {
  const whole = String(text || "");
  const start = Math.min(Math.max(0, offset), whole.length);
  const end = maxChars ? Math.min(whole.length, start + maxChars) : whole.length;
  return {
    content: whole.slice(start, end),
    offset: start,
    next_offset: end < whole.length ? end : null,
    total_chars: whole.length,
  };
}

module.exports = {
  DEFAULT_CONTEXT,
  NO_MATCH_TEXT,
  parseTimestamp,
  parseTimeRange,
  formatTimestamp,
  filterTranscript,
  hasSegments,
  paginateText,
};
