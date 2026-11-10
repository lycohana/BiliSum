import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import {
  isTaskActive,
  isTaskAgentSummarizing,
  isTaskTerminal,
  taskResultStateLabel,
  taskStatusClass,
  taskStatusLabel,
} from "../src/taskStatus.ts";

function run(name: string, fn: () => Promise<void> | void) {
  Promise.resolve(fn()).then(() => {
    console.log(`ok - ${name}`);
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

const srcDir = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

const TASK_STATUS_LITERAL = String.raw`["'](?:queued|running|agent_summarizing|completed|failed|paused|cancelled)["']`;

// Status fields that belong to other state machines (knowledge jobs, knowledge
// trace tools, multi-page aggregates, visual notes, mind maps). They share some
// words with task statuses but are not task statuses.
const NON_TASK_STATUS_EXPRESSIONS = [
  /\btool\??\.status$/,
  /\bjob\??\.status$/,
  /aggregate_?[sS]tatus$/,
  /visual\w*[sS]tatus$/i,
  /mindmap\w*[sS]tatus$/i,
];

const STATUS_COMPARISON = new RegExp(
  String.raw`([\w$?.\]\)]*[sS]tatus)\s*[!=]==?\s*${TASK_STATUS_LITERAL}|${TASK_STATUS_LITERAL}\s*[!=]==?\s*([\w$?.]*[sS]tatus)\b`,
  "g",
);
const STATUS_CASE = new RegExp(String.raw`\bcase\s+${TASK_STATUS_LITERAL}\s*:`, "g");
const STATUS_LIST_INCLUDES = new RegExp(String.raw`\[[^\]]*${TASK_STATUS_LITERAL}[^\]]*\]\.includes\(`, "g");

function collectViewFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return collectViewFiles(path);
    return /\.tsx?$/.test(name) ? [path] : [];
  });
}

function findDirectStatusComparisons(): string[] {
  const files = [
    join(srcDir, "App.tsx"),
    ...collectViewFiles(join(srcDir, "pages")),
    ...collectViewFiles(join(srcDir, "components")),
  ];
  const offenders: string[] = [];
  for (const file of files) {
    const lines = readFileSync(file, "utf8").split(/\r?\n/);
    lines.forEach((line, index) => {
      const where = `${relative(srcDir, file)}:${index + 1}`;
      for (const match of line.matchAll(STATUS_COMPARISON)) {
        const expression = match[1] ?? match[2] ?? "";
        if (NON_TASK_STATUS_EXPRESSIONS.some((pattern) => pattern.test(expression))) continue;
        offenders.push(`${where} ${match[0]}`);
      }
      for (const match of line.matchAll(STATUS_CASE)) offenders.push(`${where} ${match[0]}`);
      for (const match of line.matchAll(STATUS_LIST_INCLUDES)) offenders.push(`${where} ${match[0]}`);
    });
  }
  return offenders;
}

run("pages and components never compare task status strings directly", () => {
  assert.deepEqual(findDirectStatusComparisons(), [], "use the helpers in src/taskStatus.ts instead");
});

run("groups task statuses into active and terminal", () => {
  assert.equal(isTaskActive("queued"), true);
  assert.equal(isTaskActive("running"), true);
  assert.equal(isTaskActive("completed"), false);
  assert.equal(isTaskActive(null), false);
  assert.equal(isTaskTerminal("completed"), true);
  assert.equal(isTaskTerminal("failed"), true);
  assert.equal(isTaskTerminal("cancelled"), true);
  assert.equal(isTaskTerminal("running"), false);
  assert.equal(isTaskTerminal(undefined), false);
});

run("keeps task status labels and badge classes", () => {
  assert.equal(taskStatusLabel("queued"), "排队中");
  assert.equal(taskStatusLabel("running"), "处理中");
  assert.equal(taskStatusLabel("completed"), "已完成");
  assert.equal(taskStatusLabel("failed"), "失败");
  assert.equal(taskStatusLabel("cancelled"), "已取消");
  assert.equal(taskStatusLabel(null), "未开始");
  assert.equal(taskStatusClass("completed"), "status-success");
  assert.equal(taskStatusClass("running"), "status-running");
  assert.equal(taskStatusClass("failed"), "status-failed");
  assert.equal(taskStatusClass("queued"), "status-pending");
  assert.equal(taskStatusClass("cancelled"), "status-pending");
  assert.equal(taskStatusClass(undefined), "status-pending");
});

run("describes a video's result state from its latest task status", () => {
  assert.equal(taskResultStateLabel("running"), "正在生成摘要");
  assert.equal(taskResultStateLabel("queued"), "等待开始处理");
  assert.equal(taskResultStateLabel("failed"), "本次生成失败");
  assert.equal(taskResultStateLabel("cancelled"), "已取消生成");
  assert.equal(taskResultStateLabel("completed"), "未产出摘要");
  assert.equal(taskResultStateLabel(null), "尚未生成摘要");
});

run("agent_summarizing is an active, non-terminal status shown as Agent 总结中", () => {
  assert.equal(isTaskActive("agent_summarizing"), true);
  assert.equal(isTaskTerminal("agent_summarizing"), false);
  assert.equal(isTaskAgentSummarizing("agent_summarizing"), true);
  assert.equal(isTaskAgentSummarizing("running"), false);
  assert.equal(taskStatusLabel("agent_summarizing"), "Agent 总结中");
  assert.equal(taskStatusClass("agent_summarizing"), "status-running");
  assert.equal(taskResultStateLabel("agent_summarizing"), "等待 Agent 生成摘要");
});
