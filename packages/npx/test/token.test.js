"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { readToken } = require("../lib/token");

function makeDataDir() {
  return mkdtempSync(join(tmpdir(), "bilisum-token-"));
}

test("readToken: --token wins", () => {
  const dir = makeDataDir();
  try {
    writeFileSync(join(dir, "auth.json"), JSON.stringify({ access_token: "from-file" }), "utf8");
    assert.equal(readToken({ token: "from-flag", data: dir }, {}), "from-flag");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: env wins over auth.json", () => {
  const dir = makeDataDir();
  try {
    writeFileSync(join(dir, "auth.json"), JSON.stringify({ access_token: "from-file" }), "utf8");
    assert.equal(readToken({ token: "", data: dir }, { VIDEO_SUM_ACCESS_TOKEN: "from-env" }), "from-env");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: reads auth.json from data dir", () => {
  const dir = makeDataDir();
  try {
    writeFileSync(join(dir, "auth.json"), JSON.stringify({ access_token: "from-file" }), "utf8");
    assert.equal(readToken({ token: "", data: dir }, {}), "from-file");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: returns null when nothing found", () => {
  const dir = makeDataDir();
  try {
    assert.equal(readToken({ token: "", data: dir }, {}), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readToken: tolerates corrupt auth.json", () => {
  const dir = makeDataDir();
  try {
    writeFileSync(join(dir, "auth.json"), "{not json", "utf8");
    assert.equal(readToken({ token: "", data: dir }, {}), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
