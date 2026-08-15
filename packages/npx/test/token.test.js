"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, dirname } = require("node:path");

const { readToken } = require("../lib/token");
const { desktopUserDataCandidates } = require("../lib/runtime");

function makeDataRoot() {
  return mkdtempSync(join(tmpdir(), "bilisum-token-"));
}

/**
 * Isolate userData resolution regardless of the host platform: force the
 * Linux branch (XDG_CONFIG_HOME) pointing at an empty temp dir, so tests are
 * deterministic on Windows/macOS/Linux CI alike.
 */
function withIsolatedUserData(fn) {
  const previousPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  const previousXdg = process.env.XDG_CONFIG_HOME;
  const xdg = mkdtempSync(join(tmpdir(), "bilisum-xdg-"));
  Object.defineProperty(process, "platform", { value: "linux" });
  process.env.XDG_CONFIG_HOME = xdg;
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", previousPlatform);
    if (previousXdg === undefined) {
      delete process.env.XDG_CONFIG_HOME;
    } else {
      process.env.XDG_CONFIG_HOME = previousXdg;
    }
    rmSync(xdg, { recursive: true, force: true });
  }
}

function writeServiceAuth(dataRoot, payload) {
  mkdirSync(join(dataRoot, "data"), { recursive: true });
  writeFileSync(join(dataRoot, "data", "auth.json"), JSON.stringify(payload), "utf8");
}

test("readToken: --token wins", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      writeServiceAuth(dir, { access_token: "from-service" });
      assert.equal(readToken({ token: "from-flag", data: dir }, {}), "from-flag");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: env wins over files", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      writeServiceAuth(dir, { access_token: "from-service" });
      assert.equal(readToken({ token: "", data: dir }, { VIDEO_SUM_ACCESS_TOKEN: "from-env" }), "from-env");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: reads desktop app token file (userData)", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      // Write the desktop token into every candidate location so the test is
      // platform-independent (each platform branch has its own candidates).
      for (const candidate of desktopUserDataCandidates()) {
        mkdirSync(dirname(candidate), { recursive: true });
        writeFileSync(candidate, JSON.stringify({ accessToken: "desktop-token" }), "utf8");
      }
      assert.equal(readToken({ token: "", data: dir }, {}), "desktop-token");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: falls back to service auth.json under data root", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      writeServiceAuth(dir, { access_token: "from-service" });
      assert.equal(readToken({ token: "", data: dir }, {}), "from-service");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: returns null when nothing found", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      assert.equal(readToken({ token: "", data: dir }, {}), null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: tolerates corrupt files", () => {
  const dir = makeDataRoot();
  withIsolatedUserData(() => {
    try {
      mkdirSync(join(dir, "data"), { recursive: true });
      writeFileSync(join(dir, "data", "auth.json"), "{not json", "utf8");
      assert.equal(readToken({ token: "", data: dir }, {}), null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
