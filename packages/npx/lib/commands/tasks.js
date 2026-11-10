"use strict";

/** tasks: list tasks, newest first as the service returns them. */

const { listTasks } = require("../api");
const { connect } = require("./connection");
const { toReporter } = require("./reporter");

/** Returns { tasks: first options.limit tasks, total: count before limiting }. */
async function listTaskSummaries(options, reporter) {
  const { baseUrl, token } = await connect(options, toReporter(reporter));
  const all = await listTasks(baseUrl, token);
  return { tasks: all.slice(0, options.limit), total: all.length };
}

module.exports = { listTaskSummaries };
