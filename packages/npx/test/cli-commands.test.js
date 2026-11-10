"use strict";

/**
 * Characterization tests for the CLI shell over the shared command layer:
 * stdout, stderr and exit code of each command against a fake BiliSum service.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { mkdtempSync, readFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { startFakeService, runCli, FAKE_TOKEN } = require("./helpers/process-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";
const VERSION = require("../package.json").version;

let helpText;
test.before(async () => {
  helpText = (await runCli(["help"])).stdout;
});

function completedTask(overrides = {}) {
  return {
    task_id: "t1",
    status: "completed",
    title: "Demo",
    video_id: "BV1xx",
    source: URL_SOURCE,
    result: {
      knowledge_note_markdown: "# Note body",
      overview: "Short overview",
      key_points: ["point A"],
      transcript_text: "hello transcript",
    },
    ...overrides,
  };
}

function videoRoutes({ pages = [], task = completedTask(), progress } = {}) {
  let created = 0;
  return {
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages } },
    "POST /api/v1/videos/:id/tasks": (req) => {
      created += 1;
      const pageNumber = req.body.page_number;
      return { task_id: pageNumber ? `t${pageNumber}` : `t${created}`, status: "queued", title: "Demo" };
    },
    "GET /api/v1/tasks/:id/progress": progress || { status: "completed", progress: 100 },
    "GET /api/v1/tasks/:id": (req) => ({ ...task, task_id: req.params.id }),
  };
}

async function withService(routes, fn) {
  const service = await startFakeService(routes);
  try {
    return await fn(service);
  } finally {
    await service.close();
  }
}

test("summarize waits and prints metadata + knowledge note", async () => {
  await withService(videoRoutes(), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      `---\ntask_id: t1\nstatus: completed\ntitle: Demo\nvideo_id: BV1xx\nsource: ${URL_SOURCE}\n---\n\n# Note body\n`,
    );
    assert.equal(result.stderr, `探测视频信息：${URL_SOURCE}\n提交任务：Demo\n任务已创建：t1\n`);
    const probe = service.requests.find((r) => r.path === "/api/v1/videos/probe");
    assert.deepEqual(probe.body, { url: URL_SOURCE, force_refresh: false });
    assert.equal(probe.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    const create = service.requests.find((r) => r.method === "POST" && r.path === "/api/v1/videos/BV1xx/tasks");
    assert.deepEqual(create.body, {});
  });
});

test("brief sends summary scope and prints the summary card", async () => {
  await withService(videoRoutes(), async (service) => {
    const result = await runCli(["brief", URL_SOURCE, "--visual-note", "text", "--prompt-preset", "p1", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      `---\ntask_id: t1\nstatus: completed\ntitle: Demo\nvideo_id: BV1xx\nsource: ${URL_SOURCE}\n---\n\nShort overview\n\n## 要点\n- point A\n`,
    );
    const create = service.requests.find((r) => r.method === "POST" && r.path.endsWith("/tasks"));
    assert.deepEqual(create.body, { visual_note_mode: "text", prompt_preset_id: "p1", summary_scope: "summary" });
  });
});

test("transcribe prints only the transcript; --quiet silences stderr", async () => {
  await withService(videoRoutes(), async (service) => {
    const result = await runCli(["transcribe", URL_SOURCE, "-q", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "hello transcript\n");
    assert.equal(result.stderr, "");
  });
});

test("--format json --with-transcript emits the stable json shape", async () => {
  await withService(videoRoutes(), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--format", "json", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {
      task_id: "t1",
      video_id: "BV1xx",
      title: "Demo",
      page_number: null,
      status: "completed",
      reused: false,
      batch_id: null,
      error_code: null,
      error_message: null,
      result: completedTask().result,
    });
  });
});

test("--no-wait prints created tasks as json without polling", async () => {
  await withService(videoRoutes(), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--no-wait", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      `${JSON.stringify({ task_id: "t1", video_id: null, title: "Demo", page_number: null, status: "queued", reused: false, batch_id: null, error_code: null, error_message: null, result: null }, null, 2)}\n`,
    );
    assert.equal(
      result.stderr,
      `探测视频信息：${URL_SOURCE}\n提交任务：Demo\n任务已创建：t1\n任务已创建（不等待）：t1\n`,
    );
    assert.equal(service.requests.some((r) => r.path.includes("/progress")), false);
  });
});

test("--all-pages creates one task per page and joins the markdown", async () => {
  const pages = [{ page: 1 }, { page: 2 }];
  await withService(videoRoutes({ pages }), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--all-pages", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    const one = `---\ntask_id: t1\nstatus: completed\ntitle: Demo\nvideo_id: BV1xx\nsource: ${URL_SOURCE}\n---\n\n# Note body`;
    assert.equal(result.stdout, `${one}\n\n---\n\n${one.replace("task_id: t1", "task_id: t2")}\n`);
    assert.equal(
      result.stderr,
      `探测视频信息：${URL_SOURCE}\n提交任务：Demo（P1）\n任务已创建：t1\n提交任务：Demo（P2）\n任务已创建：t2\n`,
    );
  });
});

test("progress updates are reported once per change on stderr", async () => {
  let calls = 0;
  const progress = () => {
    calls += 1;
    return calls === 1
      ? { status: "running", progress: 42.4, latest_stage: "transcribe", latest_message: "正在转写" }
      : { status: "completed", progress: 100 };
  };
  await withService(videoRoutes({ progress }), async (service) => {
    const result = await runCli(["transcribe", URL_SOURCE, ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stderr,
      `探测视频信息：${URL_SOURCE}\n提交任务：Demo\n任务已创建：t1\n[t1]  42% [transcribe] 正在转写\n`,
    );
  });
});

test("--output writes the file and reports the path on stderr", async () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-out-"));
  const out = join(dir, "note.md");
  try {
    await withService(videoRoutes(), async (service) => {
      const result = await runCli(["transcribe", URL_SOURCE, "-o", out, ...service.connectArgs]);
      assert.equal(result.status, 0, result.stderr);
      assert.equal(result.stdout, "");
      assert.match(result.stderr, new RegExp(`结果已写入：${out.replace(/\\/g, "\\\\")}\\n$`));
      assert.equal(readFileSync(out, "utf8"), "hello transcript\n");
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a failed task prints json, the error, and exits 1", async () => {
  const task = completedTask({ status: "failed", error_message: "boom", result: null });
  await withService(videoRoutes({ task, progress: { status: "failed" } }), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, ...service.connectArgs]);
    assert.equal(result.status, 1);
    assert.equal(JSON.parse(result.stdout).error_message, "boom");
    assert.equal(
      result.stderr,
      `探测视频信息：${URL_SOURCE}\n提交任务：Demo\n任务已创建：t1\n[t1] failed：boom\n1 个任务失败。\n`,
    );
  });
});

test("a missing local file fails before contacting the service", async () => {
  await withService({}, async (service) => {
    const result = await runCli(["summarize", "no-such-file.mp4", ...service.connectArgs]);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, `本地文件不存在：no-such-file.mp4\n\n`);
    assert.equal(result.stdout, helpText);
    assert.equal(service.requests.length, 0);
  });
});

test("an unknown --page is a usage error with help", async () => {
  await withService(videoRoutes({ pages: [{ page: 1 }, { page: 2 }] }), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--page", "9", ...service.connectArgs]);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, `探测视频信息：${URL_SOURCE}\n分 P 9 不存在。可选分 P：1, 2\n\n`);
    assert.equal(result.stdout, helpText);
  });
});

test("status --json prints the raw task", async () => {
  const task = completedTask();
  await withService({ "GET /api/v1/tasks/:id": task }, async (service) => {
    const result = await runCli(["status", "t1", "--json", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, `${JSON.stringify(task, null, 2)}\n`);
    assert.equal(result.stderr, "");
  });
});

test("status prints aligned fields", async () => {
  const task = completedTask({ page_number: 2, error_message: "warn", result: { overview: "x".repeat(250) } });
  await withService({ "GET /api/v1/tasks/:id": task }, async (service) => {
    const result = await runCli(["status", "t1", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      [
        "task_id:       t1",
        "status:        completed",
        "title:         Demo",
        "page_number:   2",
        "error:         warn",
        `overview:      ${"x".repeat(200)}`,
        "",
      ].join("\n"),
    );
  });
});

test("status of an unknown task exits 1 with a hint", async () => {
  await withService({}, async (service) => {
    const result = await runCli(["status", "nope", ...service.connectArgs]);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "任务不存在：nope\n");
  });
});

test("status without a task id is a usage error", async () => {
  await withService({}, async (service) => {
    const result = await runCli(["status", ...service.connectArgs]);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, `缺少 task-id。用法：bilisum status <task-id> [--json]\n\n`);
    assert.equal(result.stdout, helpText);
  });
});

test("an unreachable custom service is reported without spawning", async () => {
  const service = await startFakeService({});
  const args = service.connectArgs;
  await service.close();
  const result = await runCli(["tasks", ...args]);
  assert.equal(result.status, 1);
  assert.equal(
    result.stderr,
    `无法连接自定义 BiliSum 服务（http://127.0.0.1:${args[3]}）。请确认服务已启动，并检查 --host/--port 配置。\n`,
  );
});

const LIST = [
  { task_id: "a", status: "completed", title: "First" },
  { task_id: "b", status: "running", source: "src-b" },
  { task_id: "c", status: "queued" },
];

test("tasks prints one line per task and a truncation note", async () => {
  await withService({ "GET /api/v1/tasks": LIST }, async (service) => {
    const result = await runCli(["tasks", "--limit", "2", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      "a  completed  First\nb  running    src-b\n（共 3 个任务，仅显示前 2 个。用 --limit 调整。）\n",
    );
  });
});

test("tasks --json prints the limited list", async () => {
  await withService({ "GET /api/v1/tasks": LIST }, async (service) => {
    const result = await runCli(["list", "--json", "--limit", "1", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, `${JSON.stringify(LIST.slice(0, 1), null, 2)}\n`);
  });
});

test("tasks with no tasks says so", async () => {
  await withService({ "GET /api/v1/tasks": [] }, async (service) => {
    const result = await runCli(["tasks", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "暂无任务。\n");
  });
});

test("doctor reports the resolved environment", async () => {
  await withService({}, async (service) => {
    const result = await runCli(["doctor", "--python", "fake-python", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      result.stdout,
      [
        `BiliSum package: ${VERSION}`,
        "Environment:     custom",
        `Service:         http://127.0.0.1:${service.port}`,
        `Data root:       ${result.home}`,
        "Python:          fake-python",
        "Token:           已找到",
        "",
      ].join("\n"),
    );
  });
});

test("env status lists environments and the persisted choice", async () => {
  const home = mkdtempSync(join(tmpdir(), "bilisum-env-status-"));
  try {
    await runCli(["custom"], { home });
    const result = await runCli(["env"], { home });
    assert.equal(result.status, 0, result.stderr);
    const lines = result.stdout.split("\n");
    assert.equal(lines[0], "当前环境:      custom");
    assert.equal(lines[1], "服务地址:      http://127.0.0.1:3838");
    assert.equal(lines[2], `数据根:        ${home}`);
    assert.equal(lines[4], "可用环境:");
    assert.match(lines[5], /^ {2}desktop {4}(● 桌面端服务在运行|○ 未检测到桌面端服务（127\.0\.0\.1:3838）)$/);
    assert.equal(lines[6], `  cli        ○ 未初始化（bilisum env setup）  CLI_HOME=${home}`);
    assert.equal(lines[7], "  custom     ○ 未配置（--host/--port/--token）");
    assert.equal(lines[8], "  auto       desktop 优先，否则 cli");
    assert.equal(lines.slice(9).join("\n"), "\n切换：bilisum env use desktop|cli|custom|auto（也可直接 bilisum desktop|cli|custom|auto）\n已持久化选择：custom\n");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("env use rejects an unknown environment", async () => {
  const result = await runCli(["env", "use", "mars"]);
  assert.equal(result.status, 1);
  assert.equal(result.stderr, `用法：bilisum env use auto|desktop|cli|custom\n\n`);
    assert.equal(result.stdout, helpText);
});
