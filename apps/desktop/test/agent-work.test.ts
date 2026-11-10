import assert from "node:assert/strict";

import {
  AGENT_CAPABILITIES,
  AGENT_DELEGATION_STEPS,
  describeAgentWork,
  formatAgentWaiting,
  parseAgentChoices,
  toggleAgentChoice,
} from "../src/agentWork.ts";
import type { AgentWorkView } from "../src/types.ts";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

const NOW = Date.parse("2026-11-09T10:00:00Z");

function view(overrides: Partial<AgentWorkView> = {}): AgentWorkView {
  return {
    task_id: "task-1",
    status: "agent_summarizing",
    round: 1,
    waiting_since: "2026-11-09T09:15:00Z",
    use_builtin: false,
    lease: null,
    ...overrides,
  };
}

run("formats how long a task has waited for an Agent", () => {
  assert.equal(formatAgentWaiting("2026-11-09T09:59:30Z", NOW), "已等待不到 1 分钟");
  assert.equal(formatAgentWaiting("2026-11-09T09:15:00Z", NOW), "已等待 45 分钟");
  assert.equal(formatAgentWaiting("2026-11-09T07:58:00Z", NOW), "已等待 2 小时 2 分钟");
  assert.equal(formatAgentWaiting("2026-11-07T09:00:00Z", NOW), "已等待 2 天 1 小时");
  assert.equal(formatAgentWaiting(null, NOW), "");
  assert.equal(formatAgentWaiting("not a date", NOW), "");
});

run("an unclaimed task waits for an Agent to claim it", () => {
  assert.deepEqual(describeAgentWork(view(), NOW), {
    headline: "等待 Agent 领取",
    waiting: "已等待 45 分钟",
    claimedBy: null,
  });
});

run("a task under an unexpired lease names the Agent processing it", () => {
  const leased = view({
    lease: {
      agent: { name: "Claude Code", model: "claude-opus", self_reported: true },
      claimed_at: "2026-11-09T09:50:00Z",
      expires_at: "2026-11-09T10:05:00Z",
    },
  });
  assert.deepEqual(describeAgentWork(leased, NOW), {
    headline: "Claude Code 处理中",
    waiting: "已等待 45 分钟",
    claimedBy: "Claude Code",
  });
});

run("an expired lease means the task is waiting to be claimed again", () => {
  const expired = view({
    lease: {
      agent: { name: "Codex", model: null, self_reported: true },
      claimed_at: "2026-11-09T09:20:00Z",
      expires_at: "2026-11-09T09:30:00Z",
    },
  });
  assert.equal(describeAgentWork(expired, NOW)?.headline, "等待 Agent 领取");
});

run("agent settings lists toggle one choice and keep the canonical order", () => {
  assert.equal(toggleAgentChoice("summary,mindmap", "knowledge_note", AGENT_DELEGATION_STEPS), "summary,knowledge_note,mindmap");
  assert.equal(toggleAgentChoice("text,vision", "text", AGENT_CAPABILITIES), "vision");
  assert.equal(toggleAgentChoice("vision", "vision", AGENT_CAPABILITIES), "");
  assert.equal(toggleAgentChoice(undefined, "text", AGENT_CAPABILITIES), "text");
  assert.deepEqual(parseAgentChoices(" Summary ,bogus,mindmap", AGENT_DELEGATION_STEPS), ["summary", "mindmap"]);
});

run("a task that is not waiting for an Agent has nothing to describe", () => {
  assert.equal(describeAgentWork(view({ status: "running", waiting_since: null }), NOW), null);
  assert.equal(describeAgentWork(null, NOW), null);
});
