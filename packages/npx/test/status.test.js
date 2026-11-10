"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { isTerminalStatus, isActiveStatus, isFailedStatus } = require("../lib/status");

test("terminal statuses end the wait loop", () => {
  assert.equal(isTerminalStatus("completed"), true);
  assert.equal(isTerminalStatus("failed"), true);
  assert.equal(isTerminalStatus("cancelled"), true);
  assert.equal(isTerminalStatus("queued"), false);
  assert.equal(isTerminalStatus("running"), false);
  assert.equal(isTerminalStatus(undefined), false);
});

test("active statuses are still being processed", () => {
  assert.equal(isActiveStatus("queued"), true);
  assert.equal(isActiveStatus("running"), true);
  assert.equal(isActiveStatus("completed"), false);
});

test("failed and cancelled tasks count as failures", () => {
  assert.equal(isFailedStatus("failed"), true);
  assert.equal(isFailedStatus("cancelled"), true);
  assert.equal(isFailedStatus("completed"), false);
  assert.equal(isFailedStatus("running"), false);
});
