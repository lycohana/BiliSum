"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, rmSync, readFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const {
  buildServiceEnv,
  cliRuntimeFilePath,
  writeCliRuntimeFile,
  readCliRuntimeFile,
  removeCliRuntimeFile,
} = require("../lib/server");
const { baseUrlFor, ApiError } = require("../lib/api");

test("baseUrlFor builds http url", () => {
  assert.equal(baseUrlFor("127.0.0.1", "3838"), "http://127.0.0.1:3838");
  assert.equal(baseUrlFor("0.0.0.0", 9999), "http://0.0.0.0:9999");
});

test("ApiError carries status and detail", () => {
  const error = new ApiError("boom", 401, "访问密钥无效");
  assert.equal(error.status, 401);
  assert.equal(error.detail, "访问密钥无效");
  assert.equal(error.name, "ApiError");
});

test("buildServiceEnv points at the desktop data root and --env overrides", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-env-"));
  try {
    const dataRoot = join(dir, "appdata");
    const env = buildServiceEnv(
      { host: "127.0.0.1", port: "3838", env: ["VIDEO_SUM_LLM_ENABLED=true"] },
      dataRoot,
    );
    assert.equal(env.VIDEO_SUM_HOST, "127.0.0.1");
    assert.equal(env.VIDEO_SUM_PORT, "3838");
    assert.equal(env.VIDEO_SUM_APP_DATA_ROOT, dataRoot);
    assert.equal(env.VIDEO_SUM_WEB_STATIC_DIR, join(dataRoot, "cli-web-static"));
    assert.equal(env.VIDEO_SUM_LLM_ENABLED, "true");
    // The service derives data/cache/tasks/db from APP_DATA_ROOT; the CLI must not
    // point them at a separate npx directory.
    assert.equal(env.VIDEO_SUM_DATA_DIR, undefined);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("buildServiceEnv rejects malformed --env", () => {
  assert.throws(
    () => buildServiceEnv({ host: "h", port: "1", env: ["NO_EQUALS"] }, "C:/data"),
    /Invalid --env value/,
  );
});

test("cli runtime file roundtrip", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-cli-"));
  try {
    writeCliRuntimeFile(dir, { pid: 1234, host: "127.0.0.1", port: "3838" });
    const info = readCliRuntimeFile(dir);
    assert.equal(info.pid, 1234);
    assert.equal(info.host, "127.0.0.1");
    assert.ok(readFileSync(cliRuntimeFilePath(dir), "utf8").includes("1234"));
    removeCliRuntimeFile(dir);
    assert.equal(readCliRuntimeFile(dir), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readCliRuntimeFile tolerates missing/corrupt file", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-cli-"));
  try {
    assert.equal(readCliRuntimeFile(dir), null);
    const { writeFileSync } = require("node:fs");
    writeFileSync(cliRuntimeFilePath(dir), "not json", "utf8");
    assert.equal(readCliRuntimeFile(dir), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
