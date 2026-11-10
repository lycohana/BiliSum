import type { TaskStatus } from "./types";

// Single place that knows which task statuses mean what. Pages and components
// must go through these helpers instead of comparing status strings
// (enforced by test/task-status.test.ts).

export const ACTIVE_TASK_STATUSES: readonly TaskStatus[] = ["queued", "running", "agent_summarizing"];
export const TERMINAL_TASK_STATUSES: readonly TaskStatus[] = ["completed", "failed", "paused", "cancelled"];

type MaybeStatus = TaskStatus | string | null | undefined;

function inSet(set: readonly string[], status: MaybeStatus): boolean {
  return typeof status === "string" && set.includes(status);
}

/** Task still owns a place in the pipeline (waiting or being processed). */
export function isTaskActive(status: MaybeStatus): boolean {
  return inSet(ACTIVE_TASK_STATUSES, status);
}

/** Task has ended and will not change on its own. */
export function isTaskTerminal(status: MaybeStatus): boolean {
  return inSet(TERMINAL_TASK_STATUSES, status);
}

export function isTaskQueued(status: MaybeStatus): boolean {
  return status === "queued";
}

export function isTaskRunning(status: MaybeStatus): boolean {
  return status === "running";
}

/** Transcript is ready; the task waits for an Agent to submit its model steps (no timeout). */
export function isTaskAgentSummarizing(status: MaybeStatus): boolean {
  return status === "agent_summarizing";
}

export function isTaskCompleted(status: MaybeStatus): boolean {
  return status === "completed";
}

export function isTaskFailed(status: MaybeStatus): boolean {
  return status === "failed";
}

/** LLM step failed after the transcript was saved; waits for "仅重跑 LLM". */
export function isTaskPaused(status: MaybeStatus): boolean {
  return status === "paused";
}

/** The text-note step can be re-run on the saved transcript of this task. */
export function isTaskLlmResumable(status: MaybeStatus): boolean {
  return status === "paused" || status === "failed";
}

/** Ended without producing a result. */
export function isTaskFailedOrCancelled(status: MaybeStatus): boolean {
  return status === "failed" || status === "cancelled" || status === "paused";
}

const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "处理中",
  agent_summarizing: "Agent 总结中",
  completed: "已完成",
  failed: "失败",
  paused: "转写已保存，等待续跑",
  cancelled: "已取消",
};

export function taskStatusLabel(status?: TaskStatus | null): string {
  return (status && TASK_STATUS_LABELS[status]) || "未开始";
}

const TASK_STATUS_CLASSES: Record<TaskStatus, string> = {
  queued: "status-pending",
  running: "status-running",
  agent_summarizing: "status-running",
  completed: "status-success",
  failed: "status-failed",
  paused: "status-failed",
  cancelled: "status-pending",
};

export function taskStatusClass(status?: TaskStatus | null): string {
  return (status && TASK_STATUS_CLASSES[status]) || "status-pending";
}

const TASK_RESULT_STATE_LABELS: Record<TaskStatus, string> = {
  queued: "等待开始处理",
  running: "正在生成摘要",
  agent_summarizing: "等待 Agent 生成摘要",
  completed: "未产出摘要",
  failed: "本次生成失败",
  paused: "转写已保存，等待续跑",
  cancelled: "已取消生成",
};

/** Library card copy for a video that has no result yet, keyed by its latest task status. */
export function taskResultStateLabel(status?: TaskStatus | null): string {
  return (status && TASK_RESULT_STATE_LABELS[status]) || "尚未生成摘要";
}

// Worker queue items (title bar) report task statuses plus a few legacy aliases.

export function isQueueItemRunning(status: string | null | undefined): boolean {
  return status === "running";
}

export function isQueueItemPending(status: string | null | undefined): boolean {
  return status === "queued" || status === "pending";
}

export function isQueueItemFinished(status: string | null | undefined): boolean {
  return isTaskTerminal(status) || status === "done";
}

/** Outcome used to style a queue item; case-insensitive like the worker payload. */
export function queueItemOutcome(status: string | null | undefined): "failed" | "completed" | null {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "failed" || normalized === "cancelled" || normalized === "paused" || normalized === "error") return "failed";
  if (normalized === "completed" || normalized === "done") return "completed";
  return null;
}
