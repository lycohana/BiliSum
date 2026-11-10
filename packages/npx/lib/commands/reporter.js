"use strict";

/**
 * Reporter contract for the command layer. Commands never write stdout and
 * never exit the process; they report through these callbacks:
 *
 * - log(message: string)            human-readable chatter (CLI: stderr)
 * - progress(event)                 task progress change, where event is
 *   { taskId, status, progress, stage, message } (progress may be null)
 *
 * Missing callbacks default to no-ops.
 */

function noop() {}

/** Normalize a partial reporter: missing log / progress callbacks become no-ops. */
function toReporter(reporter = {}) {
  return {
    log: typeof reporter.log === "function" ? reporter.log : noop,
    progress: typeof reporter.progress === "function" ? reporter.progress : noop,
  };
}

const silentReporter = toReporter();

module.exports = { toReporter, silentReporter };
