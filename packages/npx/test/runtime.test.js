"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { join } = require("node:path");

const { locateBilisumDataRoot, desktopUserDataCandidates, findPython } = require("../lib/runtime");

test("locateBilisumDataRoot honors BILISUM_DATA_ROOT", () => {
  const previous = process.env.BILISUM_DATA_ROOT;
  try {
    process.env.BILISUM_DATA_ROOT = "C:/custom/bilisum";
    assert.equal(locateBilisumDataRoot(), "C:/custom/bilisum");
  } finally {
    if (previous === undefined) {
      delete process.env.BILISUM_DATA_ROOT;
    } else {
      process.env.BILISUM_DATA_ROOT = previous;
    }
  }
});

test("locateBilisumDataRoot defaults under LOCALAPPDATA on win32", () => {
  const previousPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  const previousLocal = process.env.LOCALAPPDATA;
  try {
    Object.defineProperty(process, "platform", { value: "win32" });
    process.env.LOCALAPPDATA = "C:/Users/tester/AppData/Local";
    assert.equal(locateBilisumDataRoot(), join("C:/Users/tester/AppData/Local", "bilisum"));
  } finally {
    Object.defineProperty(process, "platform", previousPlatform);
    if (previousLocal === undefined) {
      delete process.env.LOCALAPPDATA;
    } else {
      process.env.LOCALAPPDATA = previousLocal;
    }
  }
});

test("desktopUserDataCandidates include BiliSum and bilisum-desktop", () => {
  const previousPlatform = Object.getOwnPropertyDescriptor(process, "platform");
  const previousAppData = process.env.APPDATA;
  try {
    Object.defineProperty(process, "platform", { value: "win32" });
    process.env.APPDATA = "C:/Users/tester/AppData/Roaming";
    const candidates = desktopUserDataCandidates();
    assert.ok(candidates.some((path) => path.includes("BiliSum") && path.endsWith("access-token.json")));
    assert.ok(candidates.some((path) => path.includes("bilisum-desktop") && path.endsWith("access-token.json")));
  } finally {
    Object.defineProperty(process, "platform", previousPlatform);
    if (previousAppData === undefined) {
      delete process.env.APPDATA;
    } else {
      process.env.APPDATA = previousAppData;
    }
  }
});

test("findPython returns a candidate object or null (never throws)", () => {
  const dataRoot = "C:/definitely/not/a/real/bilisum/root";
  const result = findPython(dataRoot);
  if (result === null) {
    assert.equal(result, null);
  } else {
    assert.equal(typeof result.command, "string");
    assert.ok(Array.isArray(result.args));
  }
});
