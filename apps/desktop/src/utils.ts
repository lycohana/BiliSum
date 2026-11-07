import type { TaskEvent, TaskStatus } from "./types";

const HTML_ENTITY_MAP: Record<string, string> = {
  amp: "&",
  apos: "'",
  gt: ">",
  hellip: "...",
  lt: "<",
  nbsp: " ",
  quot: "\"",
};

export function formatDateTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function formatDuration(value?: number | null) {
  if (value == null) return "-";
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function formatTaskDuration(value?: number | null) {
  if (value == null) return "-";
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}小时${minutes}分${seconds}秒`;
  if (minutes > 0) return `${minutes}分${seconds}秒`;
  return `${seconds}秒`;
}

export function formatTokenCount(value?: number | null) {
  if (value == null) return "-";
  return Number(value).toLocaleString("zh-CN");
}

export function taskStatusLabel(status?: TaskStatus | null) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
    paused: "转写已保存，等待续跑",
    cancelled: "已取消",
  };
  return (status && labels[status]) || "未开始";
}

export function summarizeEvents(events: TaskEvent[]) {
  const filtered: TaskEvent[] = [];
  const merged = new Map<string, number>();

  for (const event of events) {
    if (event.stage === "downloading" || event.stage === "transcribing") {
      const index = merged.get(event.stage);
      if (index == null) {
        filtered.push(event);
        merged.set(event.stage, filtered.length - 1);
      } else {
        filtered[index] = event;
      }
      continue;
    }
    filtered.push(event);
  }

  let currentEvent: TaskEvent | null = null;
  let failedEvent: TaskEvent | null = null;
  const latestEvent = events.at(-1) ?? null;
  const latestStage = latestEvent?.stage ?? null;
  const isMindMapStageActive = latestStage === "mindmap_llm_request" || latestStage === "mindmap_generating";
  const isMindMapStageFailed = latestStage === "mindmap_failed";
  const isMindMapStageCompleted = latestStage === "mindmap_completed";
  for (const event of events) {
    if (event.stage === "failed" || event.stage === "mindmap_failed") {
      failedEvent = event;
      continue;
    }
    currentEvent = event;
  }

  const isCompleted = isMindMapStageCompleted
    ? true
    : isMindMapStageActive || isMindMapStageFailed
      ? false
      : events.some((event) => event.stage === "completed");
  const progress = isMindMapStageActive
    ? latestEvent?.progress ?? currentEvent?.progress ?? 0
    : failedEvent
      ? failedEvent.progress
      : isCompleted
        ? 100
        : currentEvent?.progress ?? 0;

  return {
    filtered,
    currentEvent,
    failedEvent,
    progress,
    isCompleted,
    hasError: Boolean(failedEvent),
  };
}

export function normalizeRenderableMarkdown(content: string) {
  const raw = String(content || "");
  const normalizedControls = normalizeControlCharacters(raw);
  const normalizedNewlines = normalizeEscapedNewlines(normalizedControls).replace(/\r\n?/g, "\n");
  const decodedEntities = decodeHtmlEntities(normalizedNewlines);
  const repairedMarkdown = normalizeMarkdownDecorationArtifacts(decodedEntities);

  return repairedMarkdown
    .split("\n")
    .map((line) => normalizeMarkdownLine(line))
    .join("\n");
}

export function sanitizeMindMapLabel(label: string, fallbackText = "") {
  const normalized = normalizeRenderableMarkdown(label).trim();
  if (!normalized) {
    return deriveReadableMindMapLabel(fallbackText);
  }

  const repaired = repairBrokenMindMapLabel(normalized);
  if (!looksBrokenMindMapLabel(repaired)) {
    return repaired;
  }

  return deriveReadableMindMapLabel(fallbackText) || repaired || normalized;
}

function normalizeControlCharacters(content: string) {
  return content
    .replace(/\f(?=[A-Za-z])/g, "\\f")
    .replace(/\v/g, " ")
    .replace(/\u0000/g, "");
}

function normalizeEscapedNewlines(content: string) {
  const escapedBreakCount = (content.match(/\\n/g) || []).length;
  const actualBreakCount = (content.match(/\n/g) || []).length;
  if (escapedBreakCount === 0 || actualBreakCount > escapedBreakCount) {
    return content;
  }
  return content.replace(/\\n/g, "\n").replace(/\\t/g, "  ");
}

function decodeHtmlEntities(content: string) {
  return content.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (entity, token: string) => {
    const named = HTML_ENTITY_MAP[token.toLowerCase()];
    if (named != null) {
      return named;
    }

    if (token.startsWith("#x") || token.startsWith("#X")) {
      const codePoint = Number.parseInt(token.slice(2), 16);
      return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : entity;
    }

    if (token.startsWith("#")) {
      const codePoint = Number.parseInt(token.slice(1), 10);
      return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : entity;
    }

    return entity;
  });
}

function normalizeMarkdownDecorationArtifacts(content: string) {
  return content
    .replace(/\\([*#>`])/g, "$1")
    .replace(/==\s*(\*\*[^*\n]+\*\*)\s*==/g, "$1")
    .replace(/(\*\*[^*\n]+\*\*)\s*==(?=$|[\s，。；;:：!?！？])/g, "$1")
    .replace(/==\s*([^=\n]{1,80}?)\s*==/g, "**$1**");
}

function normalizeMarkdownLine(line: string) {
  if (!line.trim() || line.includes("```")) {
    return line;
  }
  if (looksLikeMarkdownTableRow(line)) {
    return normalizeMarkdownTableLine(line);
  }

  const normalizedMathText = normalizeDollarArtifacts(normalizeBrokenMathText(line));
  const convertedDelimiters = normalizedMathText
    .replace(/\\\(/g, "$")
    .replace(/\\\)/g, "$")
    .replace(/\\\[/g, "$$")
    .replace(/\\\]/g, "$$");

  const wrappedWholeMathLine = wrapWholeMathLine(convertedDelimiters);
  if (wrappedWholeMathLine !== convertedDelimiters) {
    return wrappedWholeMathLine;
  }
  if (convertedDelimiters.includes("$$")) {
    return convertedDelimiters;
  }

  return wrapBareLatexRuns(convertedDelimiters);
}

function looksLikeMarkdownTableRow(line: string) {
  const trimmed = line.trim();
  if (trimmed.startsWith("|")) {
    return trimmed.split("|").length >= 3;
  }
  return (line.match(/\s\|\s/g) || []).length >= 2;
}

function normalizeBrokenMathText(line: string) {
  return line
    .replace(/\|\$([^$]+)\$\|/g, "\\lvert $1 \\rvert")
    .replace(/([“"'`])((?:\\[A-Za-z]+|[A-Za-z0-9{}_^]+)[^“"'`\n]*?)\1/g, (match, quote: string, inner: string) => {
      if (!/[\\{}_^]/.test(inner) || inner.includes("$")) {
        return match;
      }
      const normalizedInner = inner.replace(/(?<=\b\w|\}|])\s+o\s+(?=\d|\\|\w)/g, " \\to ");
      return `${quote}$${normalizedInner.trim()}$${quote}`;
    });
}

function repairBrokenMindMapLabel(label: string) {
  let repaired = label.trim();

  for (let attempt = 0; attempt < 2; attempt += 1) {
    repaired = repaired
      .replace(/\\[A-Za-z]{1,5}$/g, "")
      .replace(/\$([^$\n]{1,120})\$\s*([^\s$，。；;:：!?！？]+)(?=$|[\s，。；;:：!?！？])/g, (_, inline: string, tail: string) => {
        if (!/[\\_^(){}\d]/.test(tail)) {
          return `$${inline}$${tail}`;
        }
        return `$${inline} ${tail.trim()}$`;
      })
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  return repaired;
}

function looksBrokenMindMapLabel(label: string) {
  const trimmed = label.trim();
  if (!trimmed) {
    return false;
  }

  const dollarCount = (trimmed.match(/\$/g) || []).length;
  if (dollarCount % 2 === 1) {
    return true;
  }
  if (/\\[A-Za-z]{1,5}$/.test(trimmed)) {
    return true;
  }

  const segments = trimmed.split(/(\${1,2}[\s\S]*?\${1,2})/g);
  let hasMathSegment = false;
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    if (!segment) {
      continue;
    }
    if (index % 2 === 1) {
      hasMathSegment = true;
      continue;
    }
    if (!hasMathSegment) {
      continue;
    }
    if (/[\\_^{}]/.test(segment)) {
      return true;
    }
  }

  return false;
}

function deriveReadableMindMapLabel(content: string) {
  const plain = normalizeRenderableMarkdown(content)
    .replace(/\[(.*?)\]\((.*?)\)/g, "$1")
    .replace(/\${1,2}([\s\S]*?)\${1,2}/g, "$1")
    .replace(/[`*_>#~-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  if (!plain) {
    return "";
  }

  const firstSentence = plain.split(/[。；;!！?？\n]/, 1)[0]?.trim() || plain;
  if (firstSentence.length <= 22) {
    return firstSentence;
  }

  const clipped = firstSentence.slice(0, 22);
  const safeBoundary = clipped.search(/[，,、：:\s][^，,、：:\s]*$/);
  return (safeBoundary > 8 ? clipped.slice(0, safeBoundary) : clipped).trim();
}

function normalizeDollarArtifacts(line: string) {
  let collapsed = line.replace(/\${3,}/g, "$$");
  const displayFenceCount = (collapsed.match(/\$\$/g) || []).length;
  if (displayFenceCount % 2 === 1) {
    collapsed = `${collapsed}$$`;
  }

  const dollarCount = (collapsed.match(/\$/g) || []).length;
  if (dollarCount % 2 === 1 && collapsed.trimEnd().endsWith("$") && !collapsed.trimStart().startsWith("$")) {
    return collapsed.replace(/\$(\s*)$/, "$1");
  }
  return collapsed;
}

function wrapWholeMathLine(line: string) {
  const trimmed = line.trim();
  if (!trimmed || trimmed.includes("$") || trimmed.startsWith("#") || trimmed.startsWith("- ") || /^\d+\.\s/.test(trimmed)) {
    return line;
  }

  const latexCommandCount = (trimmed.match(/\\[A-Za-z]+/g) || []).length;
  const mathSignalCount = (trimmed.match(/[∀∃ε∈∞]|\\(?:forall|exists|varepsilon|in|mathbb|lim|to|text|frac|cdot|iff|Rightarrow|rightarrow)/g) || []).length;
  if (latexCommandCount < 2 || mathSignalCount < 2) {
    return line;
  }

  const leading = line.match(/^\s*/)?.[0] ?? "";
  const trailing = line.match(/\s*$/)?.[0] ?? "";
  return `${leading}$${trimmed}$${trailing}`;
}

function normalizeMarkdownTableLine(line: string) {
  const trimmed = line.trim();
  if (!trimmed.includes("|") || /^[:\-\s|]+$/.test(trimmed)) {
    return line;
  }

  const cells = line.split("|");
  const normalizedCells = cells.map((cell, index) => {
    if ((index === 0 || index === cells.length - 1) && cell.trim() === "") {
      return cell;
    }

    const leading = cell.match(/^\s*/)?.[0] ?? "";
    const trailing = cell.match(/\s*$/)?.[0] ?? "";
    const core = cell.trim();
    if (!looksLikeMathCell(core) || core.includes("$")) {
      return cell;
    }
    return `${leading}$${core}$${trailing}`;
  });

  return normalizedCells.join("|");
}

function looksLikeMathCell(content: string) {
  if (!content) {
    return false;
  }
  if (/[\u4e00-\u9fff]/.test(content)) {
    return false;
  }
  return /\\[A-Za-z]+|[_^=]/.test(content);
}

function wrapBareLatexRuns(line: string) {
  const segments = line.split(/(\${1,2}[\s\S]*?\${1,2}|!?\[[^\]\n]*\]\([^()\s]*(?:\([^)]*\)[^()\s]*)*\))/g);
  return segments
    .map((segment, index) => (index % 2 === 1 ? segment : wrapBareLatexSegment(segment)))
    .join("");
}

function wrapBareLatexSegment(segment: string) {
  return segment.replace(
    /(^|[\s(（\[【"'“‘：:，,])((?:\\[A-Za-z]+(?:\{[^{}\n]+\}){0,2}|\\\{[^{}\n]+\\\}|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9{}]+|[A-Za-z][A-Za-z0-9]*\^\{?[^{}\s]+\}?)(?:\s*(?:=|\+|-|\*|\/|\\to)\s*(?:\\[A-Za-z]+(?:\{[^{}\n]+\}){0,2}|\\\{[^{}\n]+\\\}|[A-Za-z0-9{}_^.+\-\/()]+))*)(?=$|[\s)）\]】"'”’：:，,。；;!?！？])/g,
    (_, prefix: string, mathRun: string) => `${prefix}$${mathRun}$`,
  );
}
