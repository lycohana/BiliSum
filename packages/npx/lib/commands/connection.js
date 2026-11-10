"use strict";

/**
 * Shared connection step for every command: resolve the environment, make
 * sure a service is reachable (starting a background one for desktop/cli) and
 * return the base URL plus access token.
 */

const { randomBytes } = require("node:crypto");

const { baseUrlFor } = require("../api");
const { UsageError } = require("../args");
const { resolveEnvironment } = require("../env");
const { ensureService } = require("../server");
const { readToken } = require("../token");

/**
 * Token to inject into a CLI-spawned service. The standalone cli environment
 * has no desktop token file, so generate one (same pattern as the desktop app)
 * to avoid depending on auth.json creation timing; desktop mode reuses the
 * desktop token / auth.json so both sides always agree.
 */
function resolveServiceToken(options) {
  let token = readToken(options);
  if (!token && options.environment === "cli") {
    token = randomBytes(32).toString("hex");
  }
  return token;
}

/** Start or reach the service and return a usable access token; throws UsageError when none can be found. */
async function ensureAuthenticatedService(options, log) {
  let token = resolveServiceToken(options);
  await ensureService(options, { log, accessToken: token });
  if (!token) {
    // The CLI just spawned the service, which may have created {data}/auth.json.
    token = readToken(options);
  }
  if (!token) {
    throw new UsageError(
      "无法获取访问令牌：请用 --token 指定，或设置 VIDEO_SUM_ACCESS_TOKEN，" +
      "或先启动一次桌面端 BiliSum 生成令牌；没有桌面端时可使用 --environment cli。",
      { showHelp: false },
    );
  }
  return token;
}

/**
 * Resolve the environment (mutates options.host/port/data/token/environment),
 * ensure the service, and return { baseUrl, token }.
 */
async function connect(options, reporter) {
  await resolveEnvironment(options);
  const baseUrl = baseUrlFor(options.host, options.port);
  const token = await ensureAuthenticatedService(options, reporter.log);
  return { baseUrl, token };
}

module.exports = { connect, ensureAuthenticatedService, resolveServiceToken };
