"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const {
  ENV_NAMES,
  cliHomeDir,
  configPath,
  readConfig,
  writeConfig,
  normalizeEnvName,
  currentEnvName,
  resolveEnvironment,
} = require("../lib/env");
const { locateBilisumDataRoot } = require("../lib/runtime");

async function withCliHome(fn) {
  const home = mkdtempSync(join(tmpdir(), "bilisum-cli-home-"));
  const previous = process.env.BILISUM_CLI_HOME;
  process.env.BILISUM_CLI_HOME = home;
  try {
    return await fn(home);
  } finally {
    if (previous === undefined) {
      delete process.env.BILISUM_CLI_HOME;
    } else {
      process.env.BILISUM_CLI_HOME = previous;
    }
    rmSync(home, { recursive: true, force: true });
  }
}

test("ENV_NAMES are stable", () => {
  assert.deepEqual(ENV_NAMES, ["auto", "desktop", "cli", "custom"]);
});

test("cliHomeDir honors BILISUM_CLI_HOME", async () => {
  await withCliHome(async (home) => {
    assert.equal(cliHomeDir(), home);
  });
});

test("config roundtrip and default", async () => {
  await withCliHome(async () => {
    assert.deepEqual(readConfig(), { env: "auto" });
    writeConfig({ env: "cli", custom: { host: "10.0.0.1", port: "9000" } });
    const config = readConfig();
    assert.equal(config.env, "cli");
    assert.equal(config.custom.host, "10.0.0.1");
    assert.ok(configPath().endsWith("config.json"));
  });
});

test("normalizeEnvName and currentEnvName", () => {
  assert.equal(normalizeEnvName("CLI"), "cli");
  assert.equal(normalizeEnvName("nope"), null);
  assert.equal(currentEnvName({ env: "custom" }), "custom");
  assert.equal(currentEnvName({}), "auto");
  assert.equal(currentEnvName({ env: "junk" }), "auto");
});

test("resolveEnvironment: --environment cli uses CLI_HOME and 3839", async () => {
  await withCliHome(async () => {
    const options = { environment: "cli" };
    const target = await resolveEnvironment(options);
    assert.equal(target.env, "cli");
    assert.equal(target.host, "127.0.0.1");
    assert.equal(target.port, "3839");
    assert.equal(target.dataRoot, cliHomeDir());
  });
});

test("resolveEnvironment: desktop uses 3838 and desktop data root", async () => {
  await withCliHome(async () => {
    const options = { environment: "desktop" };
    const target = await resolveEnvironment(options);
    assert.equal(target.env, "desktop");
    assert.equal(target.port, "3838");
    assert.equal(target.dataRoot, locateBilisumDataRoot());
  });
});

test("resolveEnvironment: explicit --host forces custom", async () => {
  await withCliHome(async () => {
    const options = { environment: "cli", host: "192.168.1.5", port: "9999", _hostSet: true, _portSet: true };
    const target = await resolveEnvironment(options);
    assert.equal(target.env, "custom");
    assert.equal(target.host, "192.168.1.5");
    assert.equal(target.port, "9999");
  });
});

test("resolveEnvironment: config.env is honored", async () => {
  await withCliHome(async () => {
    writeConfig({ env: "cli" });
    const target = await resolveEnvironment({});
    assert.equal(target.env, "cli");
  });
});

test("resolveEnvironment: auto probes desktop first", async () => {
  await withCliHome(async () => {
    const target = await resolveEnvironment({ _desktopProbe: async () => true });
    assert.equal(target.env, "desktop");
    const fallback = await resolveEnvironment({ _desktopProbe: async () => false });
    assert.equal(fallback.env, "cli");
    assert.equal(fallback.port, "3839");
  });
});

test("resolveEnvironment: explicit auto overrides persisted environment", async () => {
  await withCliHome(async () => {
    writeConfig({ env: "cli" });
    const target = await resolveEnvironment({
      environment: "auto",
      _desktopProbe: async () => true,
    });
    assert.equal(target.env, "desktop");
    assert.equal(target.port, "3838");
  });
});

test("resolveEnvironment: invalid explicit environment is rejected", async () => {
  await withCliHome(async () => {
    writeConfig({ env: "cli" });
    await assert.rejects(resolveEnvironment({ environment: "invalid" }), /未知环境/);
  });
});

test("resolveEnvironment: custom reads config.custom", async () => {
  await withCliHome(async () => {
    writeConfig({ env: "custom", custom: { host: "10.1.2.3", port: "9000", token: "tok" } });
    const target = await resolveEnvironment({});
    assert.equal(target.env, "custom");
    assert.equal(target.host, "10.1.2.3");
    assert.equal(target.port, "9000");
    assert.equal(target.token, "tok");
  });
});
