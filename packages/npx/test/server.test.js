"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, rmSync, readFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { buildServiceEnv, runtimeFilePath, writeRuntimeFile, readRuntimeFile, removeRuntimeFile } = require("../lib/server");
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

test("buildServiceEnv sets VIDEO_SUM_* variables and --env overrides", () => {
  const dataDir = "C:/data";
  const env = buildServiceEnv(
    { host: "127.0.0.1", port: "3838", env: ["VIDEO_SUM_LLM_ENABLED=true"] },
    dataDir,
  );
  assert.equal(env.VIDEO_SUM_HOST, "127.0.0.1");
  assert.equal(env.VIDEO_SUM_PORT, "3838");
  assert.equal(env.VIDEO_SUM_DATA_DIR, dataDir);
  assert.equal(env.VIDEO_SUM_DATABASE_URL, "sqlite:///C:/data/video_sum.db");
  assert.equal(env.VIDEO_SUM_LLM_ENABLED, "true");
});

test("buildServiceEnv rejects malformed --env", () => {
  assert.throws(
    () => buildServiceEnv({ host: "h", port: "1", env: ["NO_EQUALS"] }, "C:/data"),
    /Invalid --env value/,
  );
});

test("runtime file roundtrip", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-runtime-"));
  try {
    writeRuntimeFile(dir, { pid: 1234, host: "127.0.0.1", port: "3838" });
    const info = readRuntimeFile(dir);
    assert.equal(info.pid, 1234);
    assert.equal(info.host, "127.0.0.1");
    assert.ok(readFileSync(runtimeFilePath(dir), "utf8").includes("1234"));
    removeRuntimeFile(dir);
    assert.equal(readRuntimeFile(dir), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readRuntimeFile tolerates missing/corrupt file", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-runtime-"));
  try {
    assert.equal(readRuntimeFile(dir), null);
    const { writeFileSync } = require("node:fs");
    writeFileSync(runtimeFilePath(dir), "not json", "utf8");
    assert.equal(readRuntimeFile(dir), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
