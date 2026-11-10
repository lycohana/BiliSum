import { useCallback, useEffect, useState } from "react";

import { describeAgentWork, type AgentWorkDescription } from "./agentWork";
import { api } from "./api";
import { isTaskAgentSummarizing } from "./taskStatus";
import type { AgentWorkView, TaskStatus } from "./types";

const POLL_MS = 20_000;

/**
 * Polls GET /tasks/{id}/agent-work while the task is in "Agent 总结中" and
 * describes it ("等待 Agent 领取" / "{name} 处理中" + waiting time). Never notifies.
 */
export function useAgentWork(taskId: string | null | undefined, status: TaskStatus | null | undefined): {
  view: AgentWorkView | null;
  description: AgentWorkDescription | null;
  refresh: () => Promise<void>;
} {
  const enabled = Boolean(taskId) && isTaskAgentSummarizing(status);
  const [view, setView] = useState<AgentWorkView | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async () => {
    if (!taskId) return;
    try {
      const next = await api.getAgentWork(taskId);
      setView(next);
    } catch {
      // Keep the last known view; the status badge still shows "Agent 总结中".
    } finally {
      setNow(Date.now());
    }
  }, [taskId]);

  useEffect(() => {
    if (!enabled) {
      setView(null);
      return;
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, refresh]);

  const description = enabled ? describeAgentWork(view, now) : null;
  return { view: enabled ? view : null, description, refresh };
}
