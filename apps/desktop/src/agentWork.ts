import { isTaskAgentSummarizing } from "./taskStatus.ts";
import type { AgentWorkView } from "./types";

// Display copy for a task in "Agent 总结中" (design F2 前端). Pure, so list cards
// and the detail page describe the waiting state the same way.

export type AgentWorkDescription = {
  /** "等待 Agent 领取" or "{Agent 名称} 处理中" (unexpired lease). */
  headline: string;
  /** "已等待 N 分钟"; empty when the start time is unknown. */
  waiting: string;
  /** Self-reported name of the Agent holding the lease, if any. */
  claimedBy: string | null;
};

// Settings vocabulary (mirrors video_sum_infra.config AGENT_CAPABILITIES / AGENT_DELEGATION_STEPS).
export const AGENT_CAPABILITIES = ["text", "vision"] as const;
export const AGENT_DELEGATION_STEPS = [
  "summary",
  "knowledge_note",
  "keyframe_plan",
  "visual_frame",
  "visual_compose",
  "mindmap",
] as const;

export const AGENT_CAPABILITY_LABELS: Record<(typeof AGENT_CAPABILITIES)[number], string> = {
  text: "文本",
  vision: "画面（看图）",
};

export const AGENT_STEP_LABELS: Record<(typeof AGENT_DELEGATION_STEPS)[number], string> = {
  summary: "摘要",
  knowledge_note: "知识笔记",
  keyframe_plan: "截图规划",
  visual_frame: "画面理解",
  visual_compose: "图文合成",
  mindmap: "思维导图",
};

/** Known choices of a comma list setting, lowercased, in canonical order. */
export function parseAgentChoices(value: string | null | undefined, allowed: readonly string[]): string[] {
  const picked = new Set(String(value ?? "").split(",").map((item) => item.trim().toLowerCase()));
  return allowed.filter((item) => picked.has(item));
}

/** Adds or removes one choice of a comma list setting ("summary,mindmap"). */
export function toggleAgentChoice(value: string | null | undefined, item: string, allowed: readonly string[]): string {
  const current = parseAgentChoices(value, allowed);
  const next = current.includes(item) ? current.filter((entry) => entry !== item) : [...current, item];
  return allowed.filter((entry) => next.includes(entry)).join(",");
}

/** How long a task has waited since `since` (ISO time), e.g. "已等待 2 小时 2 分钟". */
export function formatAgentWaiting(since: string | null | undefined, now: number = Date.now()): string {
  const start = since ? Date.parse(since) : Number.NaN;
  if (!Number.isFinite(start)) return "";
  const minutes = Math.max(0, Math.floor((now - start) / 60_000));
  if (minutes < 1) return "已等待不到 1 分钟";
  if (minutes < 60) return `已等待 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest ? `已等待 ${hours} 小时 ${rest} 分钟` : `已等待 ${hours} 小时`;
  }
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `已等待 ${days} 天 ${restHours} 小时` : `已等待 ${days} 天`;
}

/** Waiting state of a task in "Agent 总结中"; null for any other status. */
export function describeAgentWork(view: AgentWorkView | null | undefined, now: number = Date.now()): AgentWorkDescription | null {
  if (!view || !isTaskAgentSummarizing(view.status)) return null;
  const lease = view.lease;
  const expires = lease ? Date.parse(lease.expires_at) : Number.NaN;
  const claimedBy = lease && Number.isFinite(expires) && expires > now ? lease.agent.name || "Agent" : null;
  return {
    headline: claimedBy ? `${claimedBy} 处理中` : "等待 Agent 领取",
    waiting: formatAgentWaiting(view.waiting_since, now),
    claimedBy,
  };
}
