"use strict";

/**
 * Environment resolution for the BiliSum CLI.
 *
 * Environments:
 * - desktop: talk to an installed desktop app service (127.0.0.1:3838, desktop
 *   data root + desktop token). This is the "control the existing program" mode.
 * - cli: standalone mode — the CLI provides its own service (own data root
 *   under CLI_HOME, own managed venv, own settings page). Works without any
 *   installed BiliSum.
 * - custom: any host/port/token (Docker, remote, ...).
 * - auto (default): probe 127.0.0.1:3838 — reachable => desktop, else cli.
 *
 * Selection precedence:
 * 1. explicit --host/--port (treated as a custom target)
 * 2. --environment desktop|cli|custom|auto (one-shot override)
 * 3. config.env persisted by `bilisum env use <name>`
 * 4. auto
 */

const { existsSync, mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const { health } = require("./api");
const { locateBilisumDataRoot, cliHomeDir } = require("./runtime");

const ENV_NAMES = ["auto", "desktop", "cli", "custom"];
const DESKTOP_HOST = "127.0.0.1";
const DESKTOP_PORT = "3838";
const CLI_PORT = "3839";

function configPath() {
  return join(cliHomeDir(), "config.json");
}

function readConfig() {
  try {
    return JSON.parse(readFileSync(configPath(), "utf8"));
  } catch {
    return { env: "auto" };
  }
}

function writeConfig(config) {
  mkdirSync(cliHomeDir(), { recursive: true });
  writeFileSync(configPath(), JSON.stringify(config, null, 2), "utf8");
}

function normalizeEnvName(name) {
  const normalized = String(name || "").trim().toLowerCase();
  return ENV_NAMES.includes(normalized) ? normalized : null;
}

function currentEnvName(config) {
  return normalizeEnvName((config && config.env) || "auto") || "auto";
}

async function isDesktopReachable(port = DESKTOP_PORT) {
  try {
    const payload = await health(`http://${DESKTOP_HOST}:${port}`);
    return Boolean(payload && String(payload.service || "").trim() === "BiliSum");
  } catch {
    return false;
  }
}

/**
 * Resolve which environment a command should talk to and return concrete
 * { env, host, port, dataRoot, token }. Mutates `options.host/port/data/token`
 * to the resolved values (explicit flags win).
 */
async function resolveEnvironment(options = {}) {
  const config = readConfig();
  const explicitHost = Boolean(options._hostSet);
  const explicitPort = Boolean(options._portSet);
  const rawFlag = String(options.environment || "").trim();
  const flag = normalizeEnvName(rawFlag);

  if (rawFlag && !flag) {
    throw new Error(`未知环境：${rawFlag}。可选值：${ENV_NAMES.join(" / ")}`);
  }

  let requestedEnv;
  if (explicitHost || explicitPort) {
    // Explicit connection target always wins (custom).
    requestedEnv = "custom";
  } else if (flag) {
    // An explicit `auto` is a real one-shot override. It must not fall back to
    // a persisted desktop/cli/custom selection.
    requestedEnv = flag;
  } else {
    requestedEnv = currentEnvName(config);
  }

  let env = requestedEnv;
  if (env === "auto") {
    const probe = options._desktopProbe || isDesktopReachable;
    env = (await probe()) ? "desktop" : "cli";
  }

  let host;
  let port;
  let dataRoot;
  let token;

  if (env === "desktop") {
    host = explicitHost ? options.host : DESKTOP_HOST;
    port = explicitPort ? options.port : DESKTOP_PORT;
    dataRoot = options.data || locateBilisumDataRoot();
    token = options.token || "";
  } else if (env === "cli") {
    const cliPort = String((config.cli && config.cli.port) || CLI_PORT);
    host = DESKTOP_HOST;
    port = explicitPort ? options.port : cliPort;
    dataRoot = cliHomeDir();
    token = options.token || "";
  } else {
    const custom = (config.custom && typeof config.custom === "object") ? config.custom : {};
    host = explicitHost ? options.host : String(custom.host || DESKTOP_HOST);
    port = explicitPort ? options.port : String(custom.port || DESKTOP_PORT);
    dataRoot = cliHomeDir();
    token = options.token || String(custom.token || "");
  }

  options.host = host;
  options.port = port;
  options.data = dataRoot;
  options.token = token;
  options.environment = env;
  return { env, host, port, dataRoot, token };
}

module.exports = {
  ENV_NAMES,
  DESKTOP_HOST,
  DESKTOP_PORT,
  CLI_PORT,
  cliHomeDir,
  configPath,
  readConfig,
  writeConfig,
  normalizeEnvName,
  currentEnvName,
  isDesktopReachable,
  resolveEnvironment,
};
