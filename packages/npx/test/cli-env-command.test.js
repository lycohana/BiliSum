"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { spawnSync } = require("node:child_process");
const { mkdtempSync, readFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const CLI_PATH = join(__dirname, "..", "bin", "bilisum.js");

function runCli(home, args) {
  return spawnSync(process.execPath, [CLI_PATH, ...args], {
    encoding: "utf8",
    env: { ...process.env, BILISUM_CLI_HOME: home },
    windowsHide: true,
  });
}

test("top-level environment shortcut persists the selection", () => {
  const home = mkdtempSync(join(tmpdir(), "bilisum-cli-command-"));
  try {
    const result = runCli(home, ["desktop"]);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /已切换到 desktop 环境/);
    assert.equal(JSON.parse(readFileSync(join(home, "config.json"), "utf8")).env, "desktop");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("env use accepts auto and persists it", () => {
  const home = mkdtempSync(join(tmpdir(), "bilisum-cli-command-"));
  try {
    const result = runCli(home, ["env", "use", "auto"]);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /已切换到 auto 环境/);
    assert.equal(JSON.parse(readFileSync(join(home, "config.json"), "utf8")).env, "auto");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("env rejects unknown subcommands instead of silently showing status", () => {
  const home = mkdtempSync(join(tmpdir(), "bilisum-cli-command-"));
  try {
    const result = runCli(home, ["env", "invalid"]);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /未知 env 子命令/);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
