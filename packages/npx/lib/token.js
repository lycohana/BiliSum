"use strict";

/**
 * Access-token resolution for the BiliSum CLI (thin-client mode).
 *
 * Precedence:
 * 1. --token flag
 * 2. VIDEO_SUM_ACCESS_TOKEN env
 * 3. Desktop app token file ({userData}/access-token.json) — the desktop app
 *    generates this and passes it to its backend as VIDEO_SUM_ACCESS_TOKEN.
 * 4. Service auth file ({dataRoot}/data/auth.json) — created automatically by
 *    a service started without an env token (e.g. by `bilisum start`).
 */

const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");

const { desktopUserDataCandidates } = require("./runtime");

function readJsonTokenFile(filePath, keys) {
  if (!existsSync(filePath)) {
    return null;
  }
  try {
    const payload = JSON.parse(readFileSync(filePath, "utf8"));
    for (const key of keys) {
      const value = String(payload[key] || "").trim();
      if (value) {
        return value;
      }
    }
  } catch {
    // Corrupt file: fall through.
  }
  return null;
}

function readToken(options, env) {
  const explicit = String(options.token || "").trim();
  if (explicit) {
    return explicit;
  }
  const envToken = String((env || process.env).VIDEO_SUM_ACCESS_TOKEN || "").trim();
  if (envToken) {
    return envToken;
  }

  for (const candidate of desktopUserDataCandidates()) {
    const token = readJsonTokenFile(candidate, ["accessToken", "access_token"]);
    if (token) {
      return token;
    }
  }

  const serviceAuthFile = join(options.data || "", "data", "auth.json");
  const serviceToken = readJsonTokenFile(serviceAuthFile, ["access_token", "accessToken"]);
  if (serviceToken) {
    return serviceToken;
  }
  return null;
}

module.exports = { readToken };
