"use strict";

/** env: environment status, switch, and standalone cli setup. */

const { existsSync } = require("node:fs");

const { UsageError } = require("../args");
const {
  ENV_NAMES,
  cliHomeDir,
  isDesktopReachable,
  readConfig,
  resolveEnvironment,
  writeConfig,
} = require("../env");
const { cliVenvPython, ensureCliVenv } = require("../runtime");
const { toReporter } = require("./reporter");

/**
 * Returns {
 *   current: { env, host, port, dataRoot },
 *   desktopReachable, cliReady, cliHome, customConfigured,
 *   persisted: saved env name or "" when unset/auto
 * }.
 * `current` follows the persisted choice, or the given connection options
 * (e.g. the MCP server's startup flags) when passed; they are not mutated.
 */
async function getEnvStatus(options = null) {
  const config = readConfig();
  const target = options ? { ...options } : { environment: config.env || "auto" };
  const { env, host, port, dataRoot } = await resolveEnvironment(target);
  const desktopReachable = await isDesktopReachable();
  return {
    current: { env, host, port, dataRoot },
    desktopReachable,
    cliReady: existsSync(cliVenvPython()),
    cliHome: cliHomeDir(),
    customConfigured: Boolean(config.custom),
    persisted: config.env && config.env !== "auto" ? config.env : "",
  };
}

/** Persist the selected environment. Returns { env }. */
function useEnvironment(name) {
  const env = String(name || "").toLowerCase();
  if (!ENV_NAMES.includes(env)) {
    throw new UsageError(`用法：bilisum env use ${ENV_NAMES.join("|")}`);
  }
  writeConfig({ ...readConfig(), env });
  return { env };
}

/** Create (or reuse) the standalone cli venv. Returns { python, cliHome }. */
function setupCliEnvironment(reporter) {
  const { log } = toReporter(reporter);
  const python = ensureCliVenv({ python: process.env.BILISUM_PYTHON || "", environment: "cli" }, log);
  return { python, cliHome: cliHomeDir() };
}

module.exports = { getEnvStatus, useEnvironment, setupCliEnvironment };
