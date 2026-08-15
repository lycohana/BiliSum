"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { parseTaskCommandArgs, UsageError } = require("../lib/args");

test("parseTaskCommandArgs: returns source and defaults", () => {
  const { source, options } = parseTaskCommandArgs(["https://bilibili.com/video/BV1xx"]);
  assert.equal(source, "https://bilibili.com/video/BV1xx");
  assert.equal(options.host, "127.0.0.1");
  assert.equal(options.port, "3838");
  assert.equal(options.format, "markdown");
  assert.equal(options.wait, true);
  assert.equal(options.quiet, false);
  assert.equal(options.page, null);
  assert.equal(options.allPages, false);
  assert.equal(options.timeoutSeconds, 3600);
});

test("parseTaskCommandArgs: transcribe default format", () => {
  const { options } = parseTaskCommandArgs(["video.mp4"], { defaultFormat: "transcript" });
  assert.equal(options.format, "transcript");
});

test("parseTaskCommandArgs: full options", () => {
  const { source, options } = parseTaskCommandArgs([
    "https://bilibili.com/video/BV1xx",
    "--page", "3",
    "--visual-note", "frame_insert",
    "--prompt-preset", "p1",
    "--format", "json",
    "--output", "out.json",
    "--timeout", "120",
    "--startup-timeout", "60",
    "--quiet",
    "--token", "tok",
    "--data", "C:/tmp/data",
  ]);
  assert.equal(source, "https://bilibili.com/video/BV1xx");
  assert.equal(options.page, 3);
  assert.equal(options.visualNote, "frame_insert");
  assert.equal(options.promptPreset, "p1");
  assert.equal(options.format, "json");
  assert.equal(options.output, "out.json");
  assert.equal(options.timeoutSeconds, 120);
  assert.equal(options.startupTimeoutSeconds, 60);
  assert.equal(options.quiet, true);
  assert.equal(options.token, "tok");
  assert.equal(options.data, "C:/tmp/data");
});

test("parseTaskCommandArgs: --no-wait and --all-pages", () => {
  const { options } = parseTaskCommandArgs(["url", "--no-wait", "--all-pages"]);
  assert.equal(options.wait, false);
  assert.equal(options.allPages, true);
});

test("parseTaskCommandArgs: --with-transcript", () => {
  const { options } = parseTaskCommandArgs(["url", "--with-transcript"]);
  assert.equal(options.withTranscript, true);
  const { options: without } = parseTaskCommandArgs(["url"]);
  assert.equal(without.withTranscript, false);
});

test("parseTaskCommandArgs: --idle-timeout", () => {
  const { options } = parseTaskCommandArgs(["url", "--idle-timeout", "300"]);
  assert.equal(options.idleTimeout, 300);
  assert.throws(() => parseTaskCommandArgs(["url", "--idle-timeout", "0"]), UsageError);
  assert.throws(() => parseTaskCommandArgs(["url", "--idle-timeout", "abc"]), UsageError);
});

test("parseTaskCommandArgs: --environment", () => {
  const { options } = parseTaskCommandArgs(["url", "--environment", "cli"]);
  assert.equal(options.environment, "cli");
  assert.throws(() => parseTaskCommandArgs(["url", "--environment", "nope"]), UsageError);
  const { options: none } = parseTaskCommandArgs(["url"]);
  assert.equal(none.environment, "");
});

test("parseTaskCommandArgs: explicit host/port flags are tracked", () => {
  const { options } = parseTaskCommandArgs(["url", "--host", "10.0.0.1", "--port", "9999"]);
  assert.equal(options._hostSet, true);
  assert.equal(options._portSet, true);
  const { options: defaults } = parseTaskCommandArgs(["url"]);
  assert.equal(defaults._hostSet, false);
  assert.equal(defaults._portSet, false);
});

test("parseTaskCommandArgs: missing source throws", () => {
  assert.throws(() => parseTaskCommandArgs([]), UsageError);
  assert.throws(() => parseTaskCommandArgs(["--quiet"]), UsageError);
});

test("parseTaskCommandArgs: too many sources throws", () => {
  assert.throws(() => parseTaskCommandArgs(["a", "b"]), UsageError);
});

test("parseTaskCommandArgs: --page and --all-pages conflict", () => {
  assert.throws(() => parseTaskCommandArgs(["url", "--page", "1", "--all-pages"]), UsageError);
});

test("parseTaskCommandArgs: invalid format throws", () => {
  assert.throws(() => parseTaskCommandArgs(["url", "--format", "html"]), UsageError);
});

test("parseTaskCommandArgs: invalid page throws", () => {
  assert.throws(() => parseTaskCommandArgs(["url", "--page", "abc"]), UsageError);
  assert.throws(() => parseTaskCommandArgs(["url", "--page", "0"]), UsageError);
});

test("parseTaskCommandArgs: unknown option throws", () => {
  assert.throws(() => parseTaskCommandArgs(["url", "--nope"]), UsageError);
});

test("parseTaskCommandArgs: --help does not require source", () => {
  const { options } = parseTaskCommandArgs(["--help"]);
  assert.equal(options.help, true);
});
