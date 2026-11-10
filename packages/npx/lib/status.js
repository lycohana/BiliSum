"use strict";

// Task status sets, mirroring video_sum_core.models.tasks. The CLI must use
// these helpers instead of comparing status strings.

// Still occupying the service (waiting or being processed).
const ACTIVE_STATUSES = new Set(["queued", "running", "agent_summarizing"]);
// Ended; will not change on its own. "paused" = transcript kept, LLM failed;
// it needs the user to re-run the LLM step, so waiting must stop.
const TERMINAL_STATUSES = new Set(["completed", "failed", "paused", "cancelled"]);
// Ended without a result.
const FAILED_STATUSES = new Set(["failed", "paused", "cancelled"]);

// Waiting for an external Agent to claim and submit its work (Agent mode).
// Active, but nothing happens until an Agent acts: waiting loops stop here.
const AGENT_WAITING_STATUSES = new Set(["agent_summarizing"]);

/** Whether the task is waiting for an Agent to claim and submit its work. */
function isAgentWaitingStatus(status) {
  return AGENT_WAITING_STATUSES.has(status);
}

/** Whether the task still occupies the service (queued, running or waiting for an Agent). */
function isActiveStatus(status) {
  return ACTIVE_STATUSES.has(status);
}

/** Whether the task has ended and will not change on its own. */
function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

/** Whether the task ended without a result (failed, paused or cancelled). */
function isFailedStatus(status) {
  return FAILED_STATUSES.has(status);
}

module.exports = {
  ACTIVE_STATUSES,
  TERMINAL_STATUSES,
  FAILED_STATUSES,
  AGENT_WAITING_STATUSES,
  isActiveStatus,
  isAgentWaitingStatus,
  isTerminalStatus,
  isFailedStatus,
};
