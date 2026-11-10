import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

import {
  cleanupMigratedSource,
  copyStorageMigration,
  getActiveTaskCount,
  MIGRATION_EXTERNAL_FOLDER,
  previewStorageMigration,
  rebaseMigrationDestinations,
  resolveStorageRoot,
} from "../electron/storageMigration.ts";

function run(name: string, fn: () => Promise<void> | void) {
  Promise.resolve(fn()).then(() => {
    console.log(`ok - ${name}`);
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

async function makeSandbox() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "bilisum-storage-migration-"));
  return {
    root,
    sourceRoot: path.join(root, "old-root"),
    targetRoot: path.join(root, "new-root"),
    externalRoot: path.join(root, "external"),
  };
}

run("legacy nested paths migrate once, skip locked installer temp and preserve unchecked exports", async () => {
  const sandbox = await makeSandbox();
  const legacy = path.join(sandbox.externalRoot, "briefvid", "data");
  const cache = path.join(legacy, "cache");
  const tasks = path.join(legacy, "tasks");
  const output = path.join(legacy, "out");
  const temporary = path.join(cache, "runtime-temp", "gpu-cu128", "pip-unpack-locked");
  let denied = false;
  const sid = process.platform === "win32" ? execFileSync("whoami", ["/user", "/fo", "csv", "/nh"], { encoding: "utf8", windowsHide: true }).match(/S-1-\d+(?:-\d+)+/)![0] : "";
  const deny = (dir: string) => execFileSync("icacls", [dir, "/deny", `*${sid}:(OI)(CI)(RX)`], { windowsHide: true });
  const restore = (dir: string) => execFileSync("icacls", [dir, "/remove:d", `*${sid}`], { windowsHide: true });
  const locations = [
    { key: "data" as const, label: "数据目录", path: legacy },
    { key: "cache" as const, label: "缓存目录", path: cache },
    { key: "tasks" as const, label: "任务目录", path: tasks },
    { key: "output" as const, label: "导出目录", path: output },
  ];
  try {
    for (const dir of [path.join(sandbox.sourceRoot, "data"), temporary, tasks, output, path.join(cache, "models")]) await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(temporary, "partial.whl"), "disposable");
    await fs.writeFile(path.join(cache, "models", "model.bin"), "keep-model");
    await fs.writeFile(path.join(output, "important.md"), "keep-export");
    await fs.writeFile(path.join(tasks, "checkpoint.json"), JSON.stringify({ transcript_path: path.join(tasks, "transcript.txt"), export_path: path.join(output, "important.md") }));
    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "settings.json"), JSON.stringify({ data_dir: legacy, cache_dir: cache, tasks_dir: tasks, output_dir: output,
      database_url: `sqlite:///${path.join(legacy, "video_sum.db").split(path.sep).join("/")}` }));
    if (sid) {
      deny(temporary); denied = true;
      await assert.rejects(fs.readdir(temporary), (error: NodeJS.ErrnoException) => ["EPERM", "EACCES"].includes(error.code || ""));
    }
    const preview = await previewStorageMigration({ sourceRoot: sandbox.sourceRoot, targetRoot: sandbox.targetRoot, locations, environmentManaged: false });
    assert.equal(preview.canMigrate, true, preview.blockers.join("\n"));
    assert.equal(preview.items.find((item) => item.key === "tasks")!.copySourceKey, "data");
    const stage = path.join(sandbox.root, "stage");
    const copied = await copyStorageMigration(preview, stage, () => {});
    assert.equal(new Set(copied.files.map((file) => file.sourcePath)).size, copied.files.length);
    assert.equal(copied.copiedBytes, preview.requiredBytes);
    assert.equal(copied.files.some((file) => file.sourcePath.startsWith(temporary) || file.sourcePath.startsWith(output)), false);
    const finalData = path.join(sandbox.targetRoot, MIGRATION_EXTERNAL_FOLDER, "data");
    const updated = JSON.parse(await fs.readFile(path.join(stage, "data", "settings.json"), "utf8"));
    assert.equal(updated.data_dir, finalData);
    assert.equal(updated.cache_dir, path.join(finalData, "cache"));
    assert.equal(updated.tasks_dir, path.join(finalData, "tasks"));
    assert.equal(updated.output_dir, output);
    assert.equal(updated.database_url, `sqlite:///${path.join(finalData, "video_sum.db").split(path.sep).join("/")}`);
    assert.equal(await fs.readFile(path.join(stage, MIGRATION_EXTERNAL_FOLDER, "data", "cache", "models", "model.bin"), "utf8"), "keep-model");
    if (denied) {
      restore(temporary); denied = false;
      const protectedData = path.join(tasks, "protected");
      await fs.mkdir(protectedData); deny(protectedData);
      try {
        const blocked = await previewStorageMigration({ sourceRoot: sandbox.sourceRoot, targetRoot: sandbox.targetRoot, locations, environmentManaged: false });
        assert.equal(blocked.canMigrate, false);
        assert.ok(blocked.items.some((item) => item.errorDetails?.includes("protected")));
      } finally { restore(protectedData); }
    }
    await fs.rename(stage, sandbox.targetRoot);
    rebaseMigrationDestinations(copied, stage, sandbox.targetRoot);
    await cleanupMigratedSource(copied);
    assert.equal(await fs.readFile(path.join(output, "important.md"), "utf8"), "keep-export");
    assert.equal(await fs.readFile(path.join(temporary, "partial.whl"), "utf8"), "disposable");
  } finally {
    if (denied) restore(temporary);
    await fs.rm(sandbox.root, { recursive: true, force: true });
  }
});

run("storage root prefers the environment, then desktop preference, then system default", () => {
  const environmentRoot = path.join(os.tmpdir(), "BiliSumEnv");
  const desktopRoot = path.join(os.tmpdir(), "BiliSumDesktop");
  const systemDefaultRoot = path.join(os.tmpdir(), "BiliSumDefault");
  assert.equal(resolveStorageRoot(` ${environmentRoot} `, desktopRoot, systemDefaultRoot), path.resolve(environmentRoot));
  assert.equal(resolveStorageRoot("", desktopRoot, systemDefaultRoot), path.resolve(desktopRoot));
  assert.equal(resolveStorageRoot(undefined, undefined, systemDefaultRoot), path.resolve(systemDefaultRoot));
});

run("migration task guard counts queued, running and Agent-waiting tasks only", () => {
  assert.equal(getActiveTaskCount([
    { status: "queued" },
    { status: "running" },
    { status: "agent_summarizing" },
    { status: "completed" },
    { status: "failed" },
    { status: "paused" },
  ]), 3);
});

run("migration preview counts root-contained directories once and skips export by default", async () => {
  const sandbox = await makeSandbox();
  try {
    await fs.mkdir(path.join(sandbox.sourceRoot, "data", "cache"), { recursive: true });
    await fs.mkdir(path.join(sandbox.sourceRoot, "data", "tasks"), { recursive: true });
    await fs.mkdir(path.join(sandbox.externalRoot, "tasks"), { recursive: true });
    await fs.mkdir(path.join(sandbox.externalRoot, "output"), { recursive: true });
    await fs.writeFile(path.join(sandbox.sourceRoot, "runtime.bin"), "runtime-data");
    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "cache", "cover.bin"), "cover");
    await fs.writeFile(path.join(sandbox.externalRoot, "tasks", "record.json"), "task-record");
    await fs.writeFile(path.join(sandbox.externalRoot, "output", "note.md"), "exported-note");

    const preview = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [
        { key: "data", label: "数据目录", path: path.join(sandbox.sourceRoot, "data") },
        { key: "cache", label: "缓存目录", path: path.join(sandbox.sourceRoot, "data", "cache") },
        { key: "tasks", label: "任务目录", path: path.join(sandbox.externalRoot, "tasks") },
        { key: "output", label: "导出目录", path: path.join(sandbox.externalRoot, "output") },
      ],
      environmentManaged: false,
    });

    const data = preview.items.find((item) => item.key === "data");
    const cache = preview.items.find((item) => item.key === "cache");
    const tasks = preview.items.find((item) => item.key === "tasks");
    const output = preview.items.find((item) => item.key === "output");
    const rootStats = await fs.stat(path.join(sandbox.sourceRoot, "runtime.bin"));
    const taskStats = await fs.stat(path.join(sandbox.externalRoot, "tasks", "record.json"));

    assert.equal(data?.carriedByRoot, true);
    assert.equal(cache?.carriedByRoot, true);
    assert.equal(cache?.sizeBytes, 0);
    assert.equal(tasks?.selected, true);
    assert.equal(output?.selected, false);
    assert.equal(preview.requiredBytes, rootStats.size + 5 + taskStats.size);
    assert.equal(preview.canMigrate, true);
  } finally {
    await fs.rm(sandbox.root, { recursive: true, force: true });
  }
});

run("migration preview groups nested directories and still blocks unsafe targets", async () => {
  const sandbox = await makeSandbox();
  try {
    const parent = path.join(sandbox.externalRoot, "shared");
    const nested = path.join(parent, "nested");
    await fs.mkdir(nested, { recursive: true });
    await fs.mkdir(sandbox.sourceRoot, { recursive: true });
    await fs.writeFile(path.join(nested, "file.txt"), "nested");

    const overlapping = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [
        { key: "tasks", label: "任务目录", path: parent },
        { key: "output", label: "导出目录", path: nested },
      ],
      selectedExternalKeys: ["tasks", "output"],
      environmentManaged: false,
    });
    assert.equal(overlapping.canMigrate, true);
    assert.equal(overlapping.requiredBytes, 6);
    assert.equal(overlapping.items.find((item) => item.key === "output")?.copySourceKey, "tasks");

    const selectedParent = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: path.join(sandbox.root, "another-target"),
      locations: [
        { key: "tasks", label: "任务目录", path: parent },
        { key: "output", label: "导出目录", path: nested },
      ],
      selectedExternalKeys: ["tasks"],
      environmentManaged: false,
    });
    assert.equal(selectedParent.canMigrate, true);
    assert.equal(selectedParent.requiredBytes, 0);
    assert.equal(selectedParent.items.find((item) => item.key === "output")?.destinationPath, null);

    const externalContainsRoot = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: path.join(sandbox.root, "separate-target"),
      locations: [{ key: "tasks", label: "任务目录", path: sandbox.root }],
      environmentManaged: false,
    });
    assert.ok(externalContainsRoot.blockers.some((item) => item.includes("与应用根目录重叠")));

    await fs.mkdir(sandbox.targetRoot, { recursive: true });
    await fs.writeFile(path.join(sandbox.targetRoot, "occupied.txt"), "keep");
    const occupied = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [],
      environmentManaged: false,
    });
    assert.ok(occupied.blockers.some((item) => item.includes("不为空")));
  } finally {
    await fs.rm(sandbox.root, { recursive: true, force: true });
  }
});

run("migration preview reports insufficient destination space and environment-managed roots", async () => {
  const sandbox = await makeSandbox();
  try {
    await fs.mkdir(sandbox.sourceRoot, { recursive: true });
    await fs.writeFile(path.join(sandbox.sourceRoot, "runtime.bin"), "runtime-data");
    const lowSpace = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [],
      environmentManaged: false,
    }, { availableBytesForPath: async () => 0 });
    assert.equal(lowSpace.availableBytes, 0);
    assert.ok(lowSpace.blockers.some((item) => item.includes("空间不足")));

    const environmentManaged = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [],
      environmentManaged: true,
    });
    assert.equal(environmentManaged.canMigrate, false);
    assert.ok(environmentManaged.blockers.some((item) => item.includes("VIDEO_SUM_APP_DATA_ROOT")));

    const overlappingTarget = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: path.join(sandbox.sourceRoot, "nested-target"),
      locations: [],
      environmentManaged: false,
    });
    assert.ok(overlappingTarget.blockers.some((item) => item.includes("不能相同，也不能互相包含")));
  } finally {
    await fs.rm(sandbox.root, { recursive: true, force: true });
  }
});

run("copy rewrites migrated settings and cleanup preserves changed or unlisted source files", async () => {
  const sandbox = await makeSandbox();
  const sourceSettingsPath = path.join(sandbox.sourceRoot, "data", "settings.json");
  const externalCache = path.join(sandbox.externalRoot, "cache");
  const externalOutput = path.join(sandbox.externalRoot, "output");
  try {
    await fs.mkdir(path.dirname(sourceSettingsPath), { recursive: true });
    await fs.mkdir(externalCache, { recursive: true });
    await fs.mkdir(externalOutput, { recursive: true });
    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "auth.json"), "{\"access_token\":\"kept\"}");
    await fs.writeFile(path.join(sandbox.sourceRoot, "runtime.bin"), "runtime");
    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "mutable.txt"), "before");
    await fs.writeFile(path.join(externalCache, "cached.bin"), "cache-data");
    await fs.writeFile(path.join(externalOutput, "existing.md"), "old-export");
    await fs.writeFile(sourceSettingsPath, JSON.stringify({
      data_dir: path.join(sandbox.sourceRoot, "data"),
      cache_dir: externalCache,
      tasks_dir: path.join(sandbox.sourceRoot, "data", "tasks"),
      output_dir: externalOutput,
      ytdlp_cookies_file: path.join(sandbox.sourceRoot, "data", "cookies", "bilibili.txt"),
      database_url: `sqlite:///${path.join(sandbox.sourceRoot, "data", "video_sum.db").split(path.sep).join("/")}`,
      cache_database_url: `sqlite:///${path.join(externalCache, "cache.sqlite").split(path.sep).join("/")}`,
    }, null, 2));

    const preview = await previewStorageMigration({
      sourceRoot: sandbox.sourceRoot,
      targetRoot: sandbox.targetRoot,
      locations: [
        { key: "data", label: "数据目录", path: path.join(sandbox.sourceRoot, "data") },
        { key: "cache", label: "缓存目录", path: externalCache },
        { key: "tasks", label: "任务目录", path: path.join(sandbox.sourceRoot, "data", "tasks") },
        { key: "output", label: "导出目录", path: externalOutput },
      ],
      selectedExternalKeys: ["cache"],
      environmentManaged: false,
    });
    assert.equal(preview.canMigrate, true);

    const stageRoot = path.join(sandbox.root, ".new-root.stage");
    const copied = await copyStorageMigration(preview, stageRoot, () => undefined);
    const stagedSettings = JSON.parse(await fs.readFile(path.join(stageRoot, "data", "settings.json"), "utf-8")) as Record<string, string>;
    assert.equal(stagedSettings.data_dir, path.join(sandbox.targetRoot, "data"));
    assert.equal(stagedSettings.cache_dir, path.join(sandbox.targetRoot, MIGRATION_EXTERNAL_FOLDER, "cache"));
    assert.equal(stagedSettings.tasks_dir, path.join(sandbox.targetRoot, "data", "tasks"));
    assert.equal(stagedSettings.output_dir, externalOutput);
    assert.equal(stagedSettings.ytdlp_cookies_file, path.join(sandbox.targetRoot, "data", "cookies", "bilibili.txt"));
    assert.equal(stagedSettings.cache_database_url, `sqlite:///${path.join(sandbox.targetRoot, MIGRATION_EXTERNAL_FOLDER, "cache", "cache.sqlite").split(path.sep).join("/")}`);
    assert.equal(await fs.readFile(path.join(stageRoot, "data", "auth.json"), "utf-8"), "{\"access_token\":\"kept\"}");

    await fs.rename(stageRoot, sandbox.targetRoot);
    rebaseMigrationDestinations(copied, stageRoot, sandbox.targetRoot);

    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "mutable.txt"), "changed-after-copy");
    await fs.writeFile(path.join(sandbox.sourceRoot, "new-unlisted.txt"), "keep-me");
    const cleanup = await cleanupMigratedSource(copied);
    assert.ok(cleanup.skippedFiles >= 1);
    assert.equal(await fs.readFile(path.join(sandbox.sourceRoot, "data", "mutable.txt"), "utf-8"), "changed-after-copy");
    assert.equal(await fs.readFile(path.join(sandbox.sourceRoot, "new-unlisted.txt"), "utf-8"), "keep-me");
    const settingsStillExists = await fs.stat(sourceSettingsPath).then(() => true, () => false);
    assert.equal(settingsStillExists, false);
  } finally {
    await fs.rm(sandbox.root, { recursive: true, force: true });
  }
});

run("selected child inside an unchecked parent has its own copy unit and prefix mapping", async () => {
  const sandbox = await makeSandbox();
  try {
    const parent = path.join(sandbox.externalRoot, "data");
    const unchecked = path.join(parent, "output");
    const selectedChild = path.join(unchecked, "tasks");
    await fs.mkdir(path.join(sandbox.sourceRoot, "data"), { recursive: true });
    await fs.mkdir(selectedChild, { recursive: true });
    await fs.writeFile(path.join(parent, "record.txt"), "data");
    await fs.writeFile(path.join(unchecked, "export.md"), "export");
    await fs.writeFile(path.join(selectedChild, "transcript.txt"), "transcript");
    await fs.writeFile(path.join(sandbox.sourceRoot, "data", "settings.json"), JSON.stringify({data_dir: parent, output_dir: unchecked, tasks_dir: selectedChild}));
    const preview = await previewStorageMigration({sourceRoot: sandbox.sourceRoot, targetRoot: sandbox.targetRoot, environmentManaged: false,
      locations: [{key: "data", label: "数据", path: parent}, {key: "output", label: "导出", path: unchecked}, {key: "tasks", label: "任务", path: selectedChild}]});
    assert.equal(preview.canMigrate, true, preview.blockers.join("\n"));
    assert.equal(preview.items.find((item) => item.key === "tasks")!.copySourceKey, "tasks");
    const stage = path.join(sandbox.root, "stage");
    const copied = await copyStorageMigration(preview, stage, () => {});
    assert.equal(copied.copiedBytes, preview.requiredBytes);
    assert.equal(copied.files.some((file) => file.sourcePath.endsWith("export.md")), false);
    const settings = JSON.parse(await fs.readFile(path.join(stage, "data", "settings.json"), "utf8"));
    assert.equal(settings.output_dir, unchecked);
    assert.equal(settings.tasks_dir, path.join(sandbox.targetRoot, MIGRATION_EXTERNAL_FOLDER, "tasks"));
    assert.equal(await fs.readFile(path.join(stage, MIGRATION_EXTERNAL_FOLDER, "tasks", "transcript.txt"), "utf8"), "transcript");
    const alias = await previewStorageMigration({sourceRoot: sandbox.sourceRoot, targetRoot: sandbox.targetRoot, environmentManaged: false,
      locations: [{key: "data", label: "数据", path: parent}, {key: "output", label: "导出", path: parent}]});
    assert.equal(alias.canMigrate, false);
    assert.ok(alias.blockers.some((blocker) => blocker.includes("同一路径")));
  } finally { await fs.rm(sandbox.root, {recursive: true, force: true}); }
});
