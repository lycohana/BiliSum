"use strict";

/**
 * Access-token resolution for the BiliSum CLI.
 *
 * Precedence: --token flag > VIDEO_SUM_ACCESS_TOKEN env > {dataDir}/auth.json
 * (the file the service writes automatically when no env token is configured).
 */

const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");

function readToken(options, env) {
  const explicit = String(options.token || "").trim();
  if (explicit) {
    return explicit;
  }
  const envToken = String((env || process.env).VIDEO_SUM_ACCESS_TOKEN || "").trim();
  if (envToken) {
    return envToken;
  }
  const authFile = join(options.data || "", "auth.json");
  if (existsSync(authFile)) {
    try {
      const payload = JSON.parse(readFileSync(authFile, "utf8"));
      const token = String(payload.access_token || "").trim();
      if (token) {
        return token;
      }
    } catch (error) {
      // Fall through to a missing-token error with a helpful message.
    }
  }
  return null;
}

module.exports = { readToken };
