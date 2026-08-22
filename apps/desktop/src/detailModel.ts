import type { LlmUsageRecord, PageAggregateStatus, TaskDetail, TaskMindMapResponse, TaskResult, TaskSummary, TimelineItem, VideoPageBatchOption, VideoPageOption } from "./types";

export type DetailTab = "knowledge" | "summary" | "mindmap";
export type TaskPanelState = "collapsed" | "expanded";
export type KnowledgeCardKind = "overview" | "key-point" | "chapter";

export type TokenUsageMetrics = {
  totalTokens: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  cacheHitTokens: number | null;
  cacheMissTokens: number | null;
  cacheCreationTokens: number | null;
  cacheHitRate: number | null;
  cacheReported: boolean;
  breakdown: LlmUsageRecord[];
};

function normalizeOptionalCount(value?: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function buildTokenUsageMetrics(result?: Pick<
  TaskResult,
  | "llm_total_tokens"
  | "llm_prompt_tokens"
  | "llm_completion_tokens"
  | "llm_prompt_cache_hit_tokens"
  | "llm_prompt_cache_miss_tokens"
  | "llm_prompt_cache_creation_tokens"
  | "llm_usage_breakdown"
> | null): TokenUsageMetrics {
  const totalTokens = normalizeOptionalCount(result?.llm_total_tokens);
  const promptTokens = normalizeOptionalCount(result?.llm_prompt_tokens);
  const completionTokens = normalizeOptionalCount(result?.llm_completion_tokens);
  const cacheHitTokens = normalizeOptionalCount(result?.llm_prompt_cache_hit_tokens);
  const reportedCacheMissTokens = normalizeOptionalCount(result?.llm_prompt_cache_miss_tokens);
  const cacheCreationTokens = normalizeOptionalCount(result?.llm_prompt_cache_creation_tokens);
  const cacheReported = cacheHitTokens != null || reportedCacheMissTokens != null || cacheCreationTokens != null;
  const cacheMissTokens = reportedCacheMissTokens
    ?? (promptTokens != null && cacheHitTokens != null ? Math.max(0, promptTokens - cacheHitTokens) : null);
  const cacheHitRate = cacheReported && promptTokens != null && promptTokens > 0 && cacheHitTokens != null
    ? Math.max(0, Math.min(1, cacheHitTokens / promptTokens))
    : null;

  return {
    totalTokens,
    promptTokens,
    completionTokens,
    cacheHitTokens,
    cacheMissTokens,
    cacheCreationTokens,
    cacheHitRate,
    cacheReported,
    breakdown: Array.isArray(result?.llm_usage_breakdown) ? result.llm_usage_breakdown : [],
  };
}

export type KnowledgeCard = {
  id: string;
  kind: KnowledgeCardKind;
  eyebrow: string;
  title: string;
  content: string;
  meta?: string;
  timestampSeconds?: number | null;
  pageNumber?: number | null;
  anchorLabel?: string;
};

export type ChapterGroup = {
  id: string;
  title: string;
  meta: string;
  items: KnowledgeCard[];
};

export type TaskContentState = {
  tone: "empty" | "pending" | "failed";
  title: string;
  description: string;
  detail?: string;
};

export type MindMapWorkspaceState = {
  tone: "default" | "pending" | "failed" | "accent";
  title: string;
  description: string;
  actionLabel?: string;
  actionEnabled?: boolean;
};

export const AGGREGATE_SUMMARY_PAGE_NUMBER = 0;

export function getTaskPageNumber(task: Pick<TaskSummary, "page_number">): number {
  return task.page_number ?? 1;
}

export function isAggregateSummaryTask(task: Pick<TaskSummary, "page_number">): boolean {
  return task.page_number === AGGREGATE_SUMMARY_PAGE_NUMBER;
}

export function taskPageLabel(task: Pick<TaskSummary, "page_number" | "page_title">): string {
  if (isAggregateSummaryTask(task)) {
    return task.page_title || "全集总结";
  }
  return task.page_number ? `P${task.page_number}` : "";
}

export function derivePageAggregateStatus(tasks: TaskSummary[]): PageAggregateStatus {
  if (tasks.some((task) => task.status === "running" || task.status === "queued")) {
    return "in_progress";
  }
  if (tasks.some((task) => task.status === "completed")) {
    return "completed";
  }
  if (tasks.some((task) => task.status === "failed" || task.status === "cancelled")) {
    return "failed";
  }
  return "not_started";
}

export function buildVideoPageBatchOptions(pages: VideoPageOption[], tasks: TaskSummary[]): VideoPageBatchOption[] {
  return pages.map((page) => {
    const tasksForPage = tasks.filter((task) => !isAggregateSummaryTask(task) && getTaskPageNumber(task) === page.page);
    const latestTask = tasksForPage[0] ?? null;
    return {
      ...page,
      aggregate_status: derivePageAggregateStatus(tasksForPage),
      latest_task_status: latestTask?.status ?? null,
      latest_task_updated_at: latestTask?.updated_at ?? null,
      has_completed_result: tasksForPage.some((task) => task.status === "completed"),
    };
  });
}

export function filterTasksForPage(tasks: TaskSummary[], pageNumber: number | null): TaskSummary[] {
  if (pageNumber === AGGREGATE_SUMMARY_PAGE_NUMBER) {
    return tasks.filter(isAggregateSummaryTask);
  }
  if (pageNumber == null) {
    return tasks.filter((task) => !isAggregateSummaryTask(task));
  }
  return tasks.filter((task) => !isAggregateSummaryTask(task) && getTaskPageNumber(task) === pageNumber);
}

export type KnowledgeBuildOptions = {
  chapterAnchor?: "time" | "page";
};

export function describeUserFacingErrorMessage(rawMessage?: string | null): string {
  const message = String(rawMessage || "").trim();
  if (!message) {
    return "";
  }

  const normalized = message.toLowerCase();

  if (/llm request failed with status 404|http 404|status 404: not found/.test(normalized)) {
    return "大模型接口地址无法访问。通常是 Base URL 填写不对，或当前服务商不支持这个接口路径。请到设置页检查 LLM 的 Base URL。";
  }
  if (/status 401|authentication failed|unauthorized|invalid api key|incorrect api key/.test(normalized)) {
    return "API Key 校验失败。请检查密钥是否正确、是否过期，或是否复制了多余空格。";
  }
  if (/status 403|forbidden|permission denied/.test(normalized)) {
    return "当前账号没有访问这个模型或接口的权限。请检查服务商后台的权限、余额或模型开通状态。";
  }
  if (/status 429|rate limit|too many requests/.test(normalized)) {
    return "请求过于频繁，或当前额度不足。请稍后重试，并检查服务商后台是否有限流或余额不足。";
  }
  if (/status 500|status 502|status 503|status 504|server error|bad gateway|service unavailable|gateway timeout/.test(normalized)) {
    return "模型服务暂时不可用，通常是服务商接口异常或网络波动。建议稍后再试。";
  }
  if (/connect timeout|read timeout|timed out|timeout/.test(normalized)) {
    return "请求超时了。可能是当前网络较慢，或模型响应时间过长，建议稍后重试。";
  }
  if (/connection failed|connect error|network error|failed to establish|name or service not known|getaddrinfo/.test(normalized)) {
    return "连接模型服务失败。请检查网络是否正常，以及接口地址是否填写正确。";
  }
  if (/not a valid json|未返回合法 json/.test(normalized)) {
    return "模型接口能连通，但没有按要求返回合法 JSON，所以无法继续生成摘要。建议更换模型，或先到设置页点击测试按钮检查。";
  }
  if (/no readable message content|没有返回可读取的消息内容/.test(normalized)) {
    return "模型接口已响应，但没有返回可读取的消息内容。系统会按设置重试；如果仍失败，请检查模型兼容性和响应格式。";
  }
  if (/siliconflow asr authentication failed|asr 测试失败：认证失败/.test(normalized)) {
    return "语音识别 API Key 校验失败。请检查 SiliconFlow API Key 是否正确。";
  }
  if (/siliconflow asr request failed|asr request failed/.test(normalized)) {
    return "语音识别服务调用失败。请检查 SiliconFlow 的 Base URL、模型名和网络连接。";
  }
  if (/llm/.test(normalized) && /api key/.test(normalized)) {
    return "LLM 的 API Key 似乎有问题。请到设置页重新检查并测试。";
  }

  return message;
}

export function pickDetailTaskId(tasks: TaskSummary[], preferredTaskId?: string | null): string | null {
  if (preferredTaskId && tasks.some((item) => item.task_id === preferredTaskId)) {
    return preferredTaskId;
  }
  const latestCompleted = tasks.find((item) => item.status === "completed");
  return latestCompleted?.task_id ?? tasks[0]?.task_id ?? null;
}

export function buildKnowledgeCards(result?: TaskResult | null, options: KnowledgeBuildOptions = {}): KnowledgeCard[] {
  if (!result) {
    return [];
  }

  const cards: KnowledgeCard[] = [];
  const overview = String(result.overview || "").trim();
  const keyPoints = Array.isArray(result.key_points) ? result.key_points : [];
  const timeline = Array.isArray(result.timeline) ? result.timeline : [];
  const segmentSummaries = Array.isArray(result.segment_summaries) ? result.segment_summaries : [];
  const chapterCount = timeline.length || segmentSummaries.length;

  if (overview) {
    cards.push({
      id: "overview",
      kind: "overview",
      eyebrow: "Overview",
      title: "核心概览",
      content: overview,
      meta: `${keyPoints.length} 个要点 · ${chapterCount} 个章节`,
    });
  }

  keyPoints
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item, index) => {
      cards.push({
        id: `key-point-${index}`,
        kind: "key-point",
        eyebrow: "Key Point",
        title: "",
        content: item,
      });
    });

  if (timeline.length) {
    timeline.forEach((item, index) => {
      const title = resolveContentTitle(String(item.title || "").trim(), [], String(item.summary || "").trim(), `章节 ${index + 1}`);
      const content = String(item.summary || "").trim();
      const anchorNumber = typeof item.start === "number" ? item.start : null;
      const pageNumber = options.chapterAnchor === "page" && anchorNumber != null
        ? Math.max(1, Math.round(anchorNumber))
        : null;
      cards.push({
        id: `chapter-${index}`,
        kind: "chapter",
        eyebrow: "Chapter",
        title,
        content: content || "当前章节暂未生成摘要。",
        timestampSeconds: options.chapterAnchor === "page" ? null : anchorNumber,
        pageNumber,
        anchorLabel: pageNumber != null ? `P${pageNumber}` : undefined,
      });
    });
  } else {
    segmentSummaries
      .map((item) => item.trim())
      .filter(Boolean)
      .forEach((item, index) => {
        cards.push({
          id: `chapter-${index}`,
          kind: "chapter",
          eyebrow: "Chapter",
          title: `章节 ${index + 1}`,
          content: item,
        });
      });
  }

  return cards;
}

export function buildChapterGroups(
  cards: KnowledgeCard[],
  result?: TaskResult | null,
  options: KnowledgeBuildOptions = {},
): ChapterGroup[] {
  const backendGroups = Array.isArray(result?.chapter_groups) ? result?.chapter_groups : [];
  if (backendGroups.length) {
    const groups: ChapterGroup[] = [];
    backendGroups.forEach((group, groupIndex) => {
      if (!group || typeof group !== "object") {
        return;
      }
      const children = Array.isArray(group.children) ? group.children : [];
      const items = children
        .map((item, childIndex) => buildChapterCardFromTimelineItem(item, `${groupIndex}-${childIndex}`, options))
        .filter((item): item is KnowledgeCard => Boolean(item));

      if (!items.length) {
        return;
      }

      const title = resolveContentTitle(String(group.title || "").trim(), items.map((item) => item.title), String(group.summary || "").trim(), "主题");
      const explicitMeta = String(group.summary || "").trim();
      const anchorRange = options.chapterAnchor === "page"
        ? formatPageAnchorRange(items)
        : [
          formatOptionalMarkdownDuration(typeof group.start === "number" ? group.start : items[0]?.timestampSeconds),
          formatOptionalMarkdownDuration(items[items.length - 1]?.timestampSeconds),
        ]
          .filter(Boolean)
          .join(" - ");
      const meta = explicitMeta || [anchorRange, `${items.length} 个小章节`].filter(Boolean).join(" · ");

      groups.push({
        id: `chapter-group-${groupIndex}`,
        title,
        meta,
        items,
      });
    });

    if (groups.length) {
      return groups;
    }
  }

  const chapterCards = cards.filter((item) => item.kind === "chapter");
  if (!chapterCards.length) {
    return [];
  }

  const groupSize = chapterCards.length <= 4 ? 2 : 3;
  const groups: ChapterGroup[] = [];

  for (let index = 0; index < chapterCards.length; index += groupSize) {
    const items = chapterCards.slice(index, index + groupSize);
    const first = items[0];
    const last = items[items.length - 1];
    const title = resolveContentTitle("", items.map((item) => item.title), items.map((item) => item.title).join("；"), "主题");
    const anchorRange = options.chapterAnchor === "page"
      ? formatPageAnchorRange(items)
      : [formatOptionalMarkdownDuration(first.timestampSeconds), formatOptionalMarkdownDuration(last.timestampSeconds)]
        .filter(Boolean)
        .join(" - ");
    const meta = [anchorRange, `${items.length} 个小章节`].filter(Boolean).join(" · ");

    groups.push({
      id: `chapter-group-${groups.length}`,
      title,
      meta,
      items,
    });
  }

  return groups;
}

function buildChapterCardFromTimelineItem(
  item: TimelineItem,
  idSuffix: string,
  options: KnowledgeBuildOptions = {},
): KnowledgeCard | null {
  if (!item || typeof item !== "object") {
    return null;
  }
  const summary = String(item.summary || "").trim();
  const title = resolveContentTitle(String(item.title || "").trim(), [], summary, "章节");
  const content = String(item.summary || "").trim();
  if (!title && !content) {
    return null;
  }
  const anchorNumber = typeof item.start === "number" ? item.start : null;
  const pageNumber = options.chapterAnchor === "page" && anchorNumber != null
    ? Math.max(1, Math.round(anchorNumber))
    : null;
  return {
    id: `chapter-${idSuffix}`,
    kind: "chapter",
    eyebrow: "Chapter",
    title: title || "章节",
    content: content || "当前章节暂未生成摘要。",
    timestampSeconds: options.chapterAnchor === "page" ? null : anchorNumber,
    pageNumber,
    anchorLabel: pageNumber != null ? `P${pageNumber}` : undefined,
  };
}

function formatPageAnchorRange(items: KnowledgeCard[]) {
  const pageNumbers = items
    .map((item) => item.pageNumber)
    .filter((item): item is number => typeof item === "number" && Number.isFinite(item));
  if (!pageNumbers.length) {
    return "";
  }
  const first = Math.min(...pageNumbers);
  const last = Math.max(...pageNumbers);
  return first === last ? `P${first}` : `P${first} - P${last}`;
}

function resolveContentTitle(title: string, childTitles: string[], fallbackText: string, prefix: string) {
  const normalizedTitle = normalizeTitle(title);
  if (normalizedTitle && !isPlaceholderTitle(normalizedTitle)) {
    return normalizedTitle;
  }

  for (const childTitle of childTitles) {
    const normalizedChildTitle = normalizeTitle(childTitle);
    if (normalizedChildTitle && !isPlaceholderTitle(normalizedChildTitle)) {
      return normalizedChildTitle;
    }
  }

  const derived = deriveTitleFromText(fallbackText);
  if (derived) {
    return derived;
  }

  return prefix;
}

function normalizeTitle(value: string) {
  return value.trim().replace(/^[\-•\d.()、\s]+/, "").slice(0, 24);
}

function isPlaceholderTitle(value: string) {
  const normalized = value.trim().toLowerCase();
  return /^(大)?章节\s*\d+$/.test(value.trim())
    || /^第?\s*\d+\s*(章|节|部分)$/.test(value.trim())
    || /^(part|section|chapter)\s*[-:：]?\s*\d+$/.test(normalized);
}

function deriveTitleFromText(value: string) {
  const compact = value
    .trim()
    .replace(/^(这一部分|本部分|这里|该部分|这一章|本章|本节|这一节)(主要)?(讲|介绍|讨论|说明|分析|围绕)?/, "")
    .split(/[。；!！?？\n]/)[0]
    ?.trim()
    .replace(/^[：:\-]+|[，,、；;：:]+$/g, "")
    .replace(/\s+/g, "");
  if (!compact) {
    return "";
  }
  return compact.slice(0, 24);
}

export function resolveKnowledgeNoteMarkdown(result?: TaskResult | null): string {
  if (!result) {
    return "";
  }

  const directMarkdown = String(result.knowledge_note_markdown || "").trim();
  if (directMarkdown) {
    return directMarkdown;
  }

  const sections: string[] = [];
  const overview = String(result.overview || "").trim();
  const keyPoints = Array.isArray(result.key_points) ? result.key_points.map((item) => item.trim()).filter(Boolean) : [];
  const timeline = Array.isArray(result.timeline) ? result.timeline : [];

  if (overview) {
    sections.push("## 摘要概览", "", overview);
  }

  if (keyPoints.length) {
    sections.push("", "## 关键要点", "");
    sections.push(...keyPoints.map((item) => `- ${item}`));
  }

  if (timeline.length) {
    sections.push("", "## 时间轴", "");
    timeline.forEach((item, index) => {
      const title = String(item.title || "").trim() || `章节 ${index + 1}`;
      const summary = String(item.summary || "").trim();
      const start = typeof item.start === "number" ? item.start : null;
      sections.push(`### ${title}`);
      if (start != null) {
        sections.push("", `- 时间点：${formatMarkdownDuration(start)}`);
      }
      if (summary) {
        sections.push("", summary);
      }
      sections.push("");
    });
  }

  return sections.join("\n").trim();
}

export function canExportKnowledgeNote(task?: Pick<TaskDetail, "status" | "result"> | null): boolean {
  if (!task || task.status !== "completed" || !task.result) {
    return false;
  }
  return Boolean(resolveKnowledgeNoteMarkdown(task.result).trim());
}

function formatMarkdownDuration(totalSeconds: number) {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = safeSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatOptionalMarkdownDuration(totalSeconds?: number | null) {
  if (typeof totalSeconds !== "number") {
    return "";
  }
  return formatMarkdownDuration(totalSeconds);
}

export function describeTaskContentState(task?: Pick<TaskDetail, "status" | "result" | "error_message"> | null): TaskContentState | null {
  if (task?.result) {
    return null;
  }
  if (!task) {
    return {
      tone: "empty",
      title: "还没有可展示的任务内容",
      description: "开始处理后，这里会整理出摘要结果和知识卡片。",
    };
  }
  if (task.status === "running" || task.status === "queued") {
    return {
      tone: "pending",
      title: "当前任务还在处理中",
      description: "可以先查看顶部任务药丸中的实时进度，结果生成后这里会自动可用。",
    };
  }
  if (task.status === "failed" || task.status === "cancelled") {
    return {
      tone: "failed",
      title: "当前任务未生成可用结果",
      description: "可以切换到其他已完成任务，或重新发起摘要生成。",
      detail: describeUserFacingErrorMessage(task.error_message) || undefined,
    };
  }
  return {
    tone: "empty",
    title: "当前任务还没有结果",
    description: "请选择其他已完成任务，或者重新生成摘要。",
  };
}

export function describeMindMapWorkspace(
  task?: Pick<TaskDetail, "status" | "result" | "error_message"> | null,
  mindmap?: Pick<TaskMindMapResponse, "status" | "error_message" | "mindmap"> | null,
): MindMapWorkspaceState | null {
  if (mindmap?.status === "ready" && mindmap.mindmap) {
    return null;
  }
  if (!task) {
    return {
      tone: "default",
      title: "当前还没有可生成导图的任务",
      description: "请选择一个已完成任务。导图会基于当前任务的知识卡片和知识笔记按需生成。",
      actionLabel: "等待可用任务",
      actionEnabled: false,
    };
  }
  if (mindmap?.status === "generating" || task.result?.mindmap_status === "generating") {
    return {
      tone: "pending",
      title: "思维导图正在生成",
      description: "系统正在基于当前任务的摘要结果组织主题树，稍后会自动显示在这里。",
      actionLabel: "生成中",
      actionEnabled: false,
    };
  }
  if (mindmap?.status === "failed" || task.result?.mindmap_status === "failed") {
    return {
      tone: "failed",
      title: "思维导图生成失败",
      description: describeUserFacingErrorMessage(
        mindmap?.error_message || task.result?.mindmap_error_message || task.error_message,
      ) || "这次处理没有成功生成可展示的导图。",
      actionLabel: "重新生成导图",
      actionEnabled: true,
    };
  }
  if (task.status === "running" || task.status === "queued") {
    return {
      tone: "pending",
      title: "任务完成后才能生成导图",
      description: "当前任务仍在处理中。等摘要和知识笔记准备好后，就可以按需生成思维导图。",
      actionLabel: "处理中",
      actionEnabled: false,
    };
  }
  if (task.status === "failed" || task.status === "cancelled") {
    return {
      tone: "failed",
      title: "当前任务暂时无法生成导图",
      description: describeUserFacingErrorMessage(task.error_message) || "这次处理没有产出可用于导图组织的结果。",
      actionLabel: "等待可用结果",
      actionEnabled: false,
    };
  }
  return {
    tone: "accent",
    title: "当前任务已可生成思维导图",
    description: "导图会按主题重组知识卡片、章节结构和知识笔记，并在叶子节点保留可回看时间点。",
    actionLabel: "生成思维导图",
    actionEnabled: true,
  };
}
