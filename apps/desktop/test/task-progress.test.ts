import assert from "node:assert/strict";

import type { TaskEvent } from "../src/types.ts";
import { summarizeEvents } from "../src/utils.ts";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

let nextId = 0;
function event(stage: string, progress: number): TaskEvent {
  nextId += 1;
  return {
    event_id: `e${nextId}`,
    task_id: "task-1",
    stage,
    progress,
    message: stage,
    created_at: `2026-11-09T10:00:${String(nextId).padStart(2, "0")}Z`,
    payload: {},
  };
}

run("progress never goes backwards while an Agent task replays", () => {
  const firstRound = [event("transcribing", 40), event("summarizing", 70), event("agent_summarizing", 72)];
  // The Agent submitted: the task replays from the cached steps and reports lower stage progress again.
  const replay = [...firstRound, event("summarizing", 55), event("summarizing", 60)];

  assert.equal(summarizeEvents(firstRound).progress, 72);
  assert.equal(summarizeEvents(replay).progress, 72);
  assert.equal(summarizeEvents([...replay, event("knowledge_note", 85)]).progress, 85);
});

run("a completed task still shows 100 and a failed one its failure point", () => {
  assert.equal(summarizeEvents([event("summarizing", 70), event("completed", 100)]).progress, 100);
  assert.equal(summarizeEvents([event("summarizing", 70), event("failed", 30)]).progress, 30);
});
