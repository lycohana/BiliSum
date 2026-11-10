"use strict";

/**
 * Agent mode work loop (design agent-mode 6 / 7.4), shared by the CLI and MCP shells:
 *
 *   nextAgentWork(options, reporter)          POST /agent-work/claim, re-claiming while
 *                                             in-scope tasks are still being processed
 *   submitAgentWork(taskId, options, reporter) PUT /tasks/:id/agent-work
 *   startLeaseHeartbeat(connection, lease)     in-process lease renewal loop
 *
 * Claimed rounds are returned exactly as the service sent them, so item fields
 * added later (e.g. `images` for vision items) pass through untouched.
 */

const { ApiError, agentHeartbeat, claimAgentWork, submitAgentWork: putAgentWork } = require("../api");
const { connect } = require("./connection");
const { toReporter } = require("./reporter");

const CLAIM_POLL_INTERVAL_MS = 2000;
// Renew at a third of the remaining lease, within these bounds.
const MIN_HEARTBEAT_MS = 1000;
const MAX_HEARTBEAT_MS = 120_000;
const MAX_HEARTBEAT_FAILURES = 3;

/** Resolve after `ms` milliseconds. */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Agent identity sent to the service: name, plus model when given. */
function agentIdentity(options) {
  const agent = { name: options.agentName };
  if (options.agentModel) {
    agent.model = options.agentModel;
  }
  return agent;
}

/** Request body for POST /agent-work/claim from the chosen scope and Agent identity. */
function claimBody(options) {
  const body = { scope: options.scope };
  if (options.scope === "batch") {
    body.batch_id = options.batchId;
  } else if (options.scope === "task") {
    body.task_id = options.taskId;
  }
  body.agent = agentIdentity(options);
  return body;
}

/** Error with the service's text that keeps `status` and the parsed body (`payload`). */
function serviceError(action, error) {
  return Object.assign(new Error(describeApiError(action, error)), { status: error.status, payload: error.payload });
}

/** "BiliSum API 409 ...: <detail>" plus, for 422, one line per rejected item. */
function describeApiError(action, error) {
  const payload = error.payload && typeof error.payload === "object" ? error.payload.detail : null;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const lines = [`${action}（${error.status}）：${payload.message || error.detail}`];
    for (const item of Array.isArray(payload.errors) ? payload.errors : []) {
      lines.push(`- ${item.item_id}：${item.error}`);
    }
    return lines.join("\n");
  }
  return `${action}（${error.status}）：${error.detail || error.message}`;
}

/**
 * Claim the next round of Agent work in a scope.
 *
 * options: connection fields + scope ("batch" | "all" | "task"), batchId, taskId,
 * agentName, agentModel, waitSeconds (keep re-claiming while the service reports
 * in-scope tasks still transcribing / replaying; 0 = claim once), and
 * onLease(connection, round): optional hook called with a claimed round so the
 * shell can keep its lease alive (CLI: detached heartbeat process; MCP: in-process).
 *
 * Returns the claim response: { task_id, batch_id, round, lease_id, lease_expires_at,
 * items, ... } or { task_id: null, pending_tasks }.
 * Errors: 404 -> "任务不存在：id" (scope task); other API errors keep the service's text.
 */
async function nextAgentWork(options, reporter) {
  const report = toReporter(reporter);
  const connection = await connect(options, report);
  const deadline = Date.now() + Math.max(0, options.waitSeconds || 0) * 1000;
  const body = claimBody(options);
  let announced = -1;
  while (true) {
    let claim;
    try {
      claim = await claimAgentWork(connection.baseUrl, connection.token, body);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && options.scope === "task") {
        throw new Error(`任务不存在：${options.taskId}`);
      }
      if (error instanceof ApiError && error.status) {
        throw serviceError("领取失败", error);
      }
      throw error;
    }
    if (claim && claim.task_id) {
      const count = Array.isArray(claim.items) ? claim.items.length : 0;
      report.log(`已领取任务 ${claim.task_id} 第 ${claim.round} 轮，共 ${count} 项（独占至 ${claim.lease_expires_at}）。`);
      if (typeof options.onLease === "function") {
        options.onLease(connection, claim);
      }
      return claim;
    }
    const pending = Number((claim && claim.pending_tasks) || 0);
    if (pending <= 0) {
      report.log("暂无待办：范围内没有待处理的任务。");
      return claim;
    }
    if (Date.now() >= deadline) {
      report.log(`暂无待办：${pending} 个任务仍在处理（转写或重放中），稍后再领取。`);
      return claim;
    }
    if (pending !== announced) {
      announced = pending;
      report.log(`暂无待办，${pending} 个任务仍在处理，等待后重新领取...`);
    }
    await sleep(Math.max(0, Math.min(CLAIM_POLL_INTERVAL_MS, deadline - Date.now())));
  }
}

/**
 * Validate a results payload: an array of { item_id, content } | { item_id, declined: true, reason? },
 * or { results: [...] }. Returns the array; throws on anything else.
 */
function normalizeResults(payload) {
  const results = Array.isArray(payload) ? payload : payload && Array.isArray(payload.results) ? payload.results : null;
  if (!results || results.some((entry) => !entry || typeof entry !== "object" || typeof entry.item_id !== "string")) {
    throw new Error(
      "结果文件格式不对：需要 [{\"item_id\": ..., \"content\": ...} 或 {\"item_id\": ..., \"declined\": true, \"reason\": ...}]，"
      + "或 {\"results\": [...]}。",
    );
  }
  return results;
}

/**
 * Submit one round. options: connection fields + leaseId, agentName, agentModel,
 * results (array, see normalizeResults). Returns the 202 body { task_id, status, round }.
 * 409 / 422 come back as Errors carrying the service's text (422: one line per item),
 * plus `status` and `payload` (the parsed error body) for shells that pass them on.
 */
async function submitAgentWork(taskId, options, reporter) {
  const report = toReporter(reporter);
  const results = normalizeResults(options.results);
  const connection = await connect(options, report);
  try {
    const accepted = await putAgentWork(connection.baseUrl, connection.token, taskId, {
      lease_id: options.leaseId,
      agent: agentIdentity(options),
      results,
    });
    const declined = results.filter((entry) => entry.declined === true).length;
    report.log(`已提交任务 ${taskId} 的 ${results.length} 项${declined ? `（其中拒绝 ${declined} 项）` : ""}，任务继续处理。`);
    return accepted;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      throw new Error(`任务不存在：${taskId}`);
    }
    if (error instanceof ApiError && error.status) {
      throw serviceError("提交失败", error);
    }
    throw error;
  }
}

/** Delay before the next heartbeat: a third of the lease's remaining time, clamped to the min/max interval. */
function heartbeatDelay(expiresAt) {
  const remaining = Date.parse(expiresAt) - Date.now();
  if (!Number.isFinite(remaining)) {
    return MIN_HEARTBEAT_MS;
  }
  return Math.min(MAX_HEARTBEAT_MS, Math.max(MIN_HEARTBEAT_MS, Math.floor(remaining / 3)));
}

/**
 * Keep a claimed lease alive: POST agent-heartbeat at a third of the remaining
 * lease until the service refuses it (409: submitted, expired or taken over;
 * 404: task gone), stop() is called, or maxHoldMs has passed since the start
 * (default: no cap; the detached CLI heartbeat passes one so a vanished Agent
 * cannot hold a task forever). Transient errors are retried
 * up to MAX_HEARTBEAT_FAILURES times in a row, and never past the lease's expiry.
 *
 * lease = { task_id, lease_id, lease_expires_at }. Returns { stop(), done }
 * where done resolves with the reason: "refused" | "stopped" | "max_hold" | "expired".
 */
function startLeaseHeartbeat({ baseUrl, token }, lease, { maxHoldMs = Infinity } = {}) {
  let stopped = false;
  let timer = null;
  let wake = null;
  const holdUntil = Date.now() + maxHoldMs;
  const wait = (ms) => new Promise((resolve) => {
    wake = resolve;
    timer = setTimeout(resolve, ms);
  });
  const done = (async () => {
    let expiresAt = lease.lease_expires_at;
    let failures = 0;
    while (true) {
      await wait(Math.max(0, Math.min(heartbeatDelay(expiresAt), holdUntil - Date.now())));
      if (stopped) {
        return "stopped";
      }
      if (Date.now() >= holdUntil) {
        return "max_hold";
      }
      try {
        const renewed = await agentHeartbeat(baseUrl, token, lease.task_id, lease.lease_id);
        expiresAt = (renewed && renewed.lease_expires_at) || expiresAt;
        failures = 0;
      } catch (error) {
        if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
          return "refused";
        }
        failures += 1;
        if (failures >= MAX_HEARTBEAT_FAILURES || Date.parse(expiresAt) <= Date.now()) {
          return "expired";
        }
      }
    }
  })();
  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
      if (wake) {
        wake();
      }
    },
    done,
  };
}

module.exports = {
  nextAgentWork,
  submitAgentWork,
  startLeaseHeartbeat,
  normalizeResults,
};
