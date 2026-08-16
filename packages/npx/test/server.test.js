"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, mkdirSync, rmSync, readFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const {
  buildServiceEnv,
  cliRuntimeFilePath,
  cliServiceLogPath,
  writeCliRuntimeFile,
  readCliRuntimeFile,
  removeCliRuntimeFile,
  readServiceSettings,
  assertSpawnablePort,
  ensureService,
  buildAutoStartMessage,
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
    // No web static dir is passed here, so the CLI must not invent an empty
    // cli-web-static directory (spawnService supplies the bundled path).
    assert.equal(env.VIDEO_SUM_WEB_STATIC_DIR, undefined);
    assert.equal(env.VIDEO_SUM_LLM_ENABLED, "true");
    // The service derives data/cache/tasks/db from APP_DATA_ROOT; the CLI must not
    // point them at a separate npx directory.
    assert.equal(env.VIDEO_SUM_DATA_DIR, undefined);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("buildServiceEnv wires the web static dir and lets --env override it", () => {
  const staticDir = "C:/pkg/runtime/apps/web/static";
  const env = buildServiceEnv({ host: "h", port: "1" }, "C:/data", null, staticDir);
  assert.equal(env.VIDEO_SUM_WEB_STATIC_DIR, staticDir);

  // An explicit --env must win over the CLI's bundled static dir.
  const overridden = buildServiceEnv(
    { host: "h", port: "1", env: ["VIDEO_SUM_WEB_STATIC_DIR=C:/override"] },
    "C:/data",
    null,
    staticDir,
  );
  assert.equal(overridden.VIDEO_SUM_WEB_STATIC_DIR, "C:/override");
});

test("buildServiceEnv rejects malformed --env", () => {
  assert.throws(
    () => buildServiceEnv({ host: "h", port: "1", env: ["NO_EQUALS"] }, "C:/data"),
    /Invalid --env value/,
  );
});

test("cli runtime file roundtrip is per port", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-cli-"));
  try {
    writeCliRuntimeFile(dir, "3838", { pid: 1234, host: "127.0.0.1", port: "3838" });
    writeCliRuntimeFile(dir, "3839", { pid: 5678, host: "127.0.0.1", port: "3839" });
    assert.equal(readCliRuntimeFile(dir, "3838").pid, 1234);
    assert.equal(readCliRuntimeFile(dir, "3839").pid, 5678);
    assert.ok(readFileSync(cliRuntimeFilePath(dir, "3838"), "utf8").includes("1234"));
    removeCliRuntimeFile(dir, "3838");
    assert.equal(readCliRuntimeFile(dir, "3838"), null);
    // The other port's record must survive.
    assert.equal(readCliRuntimeFile(dir, "3839").pid, 5678);
    // Log path is per port too.
    assert.equal(cliServiceLogPath(dir, "3839"), join(dir, "cli-service-3839.log"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readCliRuntimeFile tolerates missing/corrupt file", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-cli-"));
  try {
    assert.equal(readCliRuntimeFile(dir, "3838"), null);
    const { writeFileSync } = require("node:fs");
    writeFileSync(cliRuntimeFilePath(dir, "3838"), "not json", "utf8");
    assert.equal(readCliRuntimeFile(dir, "3838"), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readServiceSettings reads persisted settings and tolerates absence", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-settings-"));
  try {
    assert.equal(readServiceSettings(dir), null);
    mkdirSync(join(dir, "data"), { recursive: true });
    const { writeFileSync } = require("node:fs");
    writeFileSync(
      join(dir, "data", "settings.json"),
      JSON.stringify({ port: 3838, data_dir: "C:/legacy/briefvid/data" }),
      "utf8",
    );
    const settings = readServiceSettings(dir);
    assert.equal(settings.port, 3838);
    assert.equal(settings.data_dir, "C:/legacy/briefvid/data");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("assertSpawnablePort rejects --port conflicting with persisted settings", () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-settings-"));
  try {
    mkdirSync(join(dir, "data"), { recursive: true });
    const { writeFileSync } = require("node:fs");
    writeFileSync(join(dir, "data", "settings.json"), JSON.stringify({ port: 3838 }), "utf8");
    assert.throws(() => assertSpawnablePort({ data: dir, port: "3839" }), /settings\.json 中服务端口为 3838/);
    // Matching port is fine; absent settings are fine.
    assert.doesNotThrow(() => assertSpawnablePort({ data: dir, port: "3838" }));
    const emptyDir = mkdtempSync(join(tmpdir(), "bilisum-settings-empty-"));
    try {
      assert.doesNotThrow(() => assertSpawnablePort({ data: emptyDir, port: "3839" }));
    } finally {
      rmSync(emptyDir, { recursive: true, force: true });
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("ensureService does not auto-start an unreachable custom target", async () => {
  await assert.rejects(
    ensureService(
      { host: "127.0.0.1", port: "1", environment: "custom" },
      { log: () => {} },
    ),
    /无法连接自定义 BiliSum 服务/,
  );
});

test("auto-start message describes the resolved environment data root", () => {
  assert.match(
    buildAutoStartMessage({ host: "127.0.0.1", port: "3839", environment: "cli" }),
    /CLI 独立数据目录/,
  );
  assert.match(
    buildAutoStartMessage({ host: "127.0.0.1", port: "3838", environment: "desktop" }),
    /桌面端数据目录/,
  );
});
