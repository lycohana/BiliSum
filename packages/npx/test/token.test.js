"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { readToken } = require("../lib/token");
const { desktopUserDataCandidates } = require("../lib/runtime");

function makeDataRoot() {
  return mkdtempSync(join(tmpdir(), "bilisum-token-"));
}

function withIsolatedAppData(fn) {
  const appData = mkdtempSync(join(tmpdir(), "bilisum-appdata-"));
  const previous = process.env.APPDATA;
  process.env.APPDATA = appData;
  try {
    return fn();
  } finally {
    if (previous === undefined) {
      delete process.env.APPDATA;
    } else {
      process.env.APPDATA = previous;
    }
    rmSync(appData, { recursive: true, force: true });
  }
}

function writeServiceAuth(dataRoot, payload) {
  mkdirSync(join(dataRoot, "data"), { recursive: true });
  writeFileSync(join(dataRoot, "data", "auth.json"), JSON.stringify(payload), "utf8");
}

test("readToken: --token wins", () => {
  const dir = makeDataRoot();
  try {
    writeServiceAuth(dir, { access_token: "from-service" });
    assert.equal(readToken({ token: "from-flag", data: dir }, {}), "from-flag");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: env wins over files", () => {
  const dir = makeDataRoot();
  try {
    writeServiceAuth(dir, { access_token: "from-service" });
    assert.equal(readToken({ token: "", data: dir }, { VIDEO_SUM_ACCESS_TOKEN: "from-env" }), "from-env");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: reads desktop app token file (userData)", () => {
  const dir = makeDataRoot();
  const appData = mkdtempSync(join(tmpdir(), "bilisum-appdata-"));
  const previous = process.env.APPDATA;
  try {
    const candidate = join(appData, "BiliSum", "access-token.json");
    mkdirSync(join(appData, "BiliSum"), { recursive: true });
    writeFileSync(candidate, JSON.stringify({ accessToken: "desktop-token" }), "utf8");
    process.env.APPDATA = appData;
    assert.ok(desktopUserDataCandidates().includes(candidate));
    assert.equal(readToken({ token: "", data: dir }, {}), "desktop-token");
  } finally {
    if (previous === undefined) {
      delete process.env.APPDATA;
    } else {
      process.env.APPDATA = previous;
    }
    rmSync(dir, { recursive: true, force: true });
    rmSync(appData, { recursive: true, force: true });
  }
});

test("readToken: falls back to service auth.json under data root", () => {
  const dir = makeDataRoot();
  withIsolatedAppData(() => {
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
  withIsolatedAppData(() => {
    try {
      assert.equal(readToken({ token: "", data: dir }, {}), null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

test("readToken: tolerates corrupt files", () => {
  const dir = makeDataRoot();
  withIsolatedAppData(() => {
    try {
      mkdirSync(join(dir, "data"), { recursive: true });
      writeFileSync(join(dir, "data", "auth.json"), "{not json", "utf8");
      assert.equal(readToken({ token: "", data: dir }, {}), null);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
