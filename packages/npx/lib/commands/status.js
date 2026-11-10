"use strict";

/** status: fetch one task's detail, optionally waiting a bounded time for it to finish. */

const { ApiError, getTask } = require("../api");
const { isTerminalStatus } = require("../status");
const { connect } = require("./connection");
const { toReporter } = require("./reporter");
const { waitForTasks } = require("./task");

/**
 * Returns the task detail object from the service. With options.waitSeconds > 0
 * and a task that is not terminal yet, keeps polling for up to that long; the
 * task is then the final detail, or on time-out the detail overlaid with the
 * latest progress snapshot (status, progress, latest_stage, latest_message).
 */
async function getTaskStatus(taskId, options, reporter) {
  const report = toReporter(reporter);
  const connection = await connect(options, report);
  let task;
  try {
    task = await getTask(connection.baseUrl, connection.token, taskId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      throw new Error(`任务不存在：${taskId}`);
    }
    throw error;
  }
  if (!(options.waitSeconds > 0) || isTerminalStatus(task.status)) {
    return task;
  }
  const waited = await waitForTasks([taskId], options, connection, report);
  return waited.timedOut ? { ...task, ...waited.tasks[0] } : waited.tasks[0];
}

module.exports = { getTaskStatus };
