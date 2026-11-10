"use strict";

/**
 * F2-5: Agent mode from the CLI, through the process seam (real CLI binary,
 * fake BiliSum HTTP service).
 *
 *   summarize --engine / --agent-capabilities / --agent-name -> routing fields + batch_id
 *   agent next (--batch | --all | --task)                       -> POST /agent-work/claim
 *   agent submit <task> --lease <id> --results <file>           -> PUT /tasks/:id/agent-work
 *   automatic heartbeat while the lease is held                 -> POST /tasks/:id/agent-heartbeat
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { startFakeService, runCli } = require("./helpers/process-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";

function createRoutes(extra = {}) {
  return {
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages: [] } },
    "GET /api/v1/videos/:id/reusable-task": { status: 404, json: { detail: "没有可复用的任务。" } },
    "POST /api/v1/videos/:id/tasks": { task_id: "t1", status: "queued", title: "Demo", batch_id: "batch-1" },
    ...extra,
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

function claimRequests(service) {
  return service.requests.filter((r) => r.method === "POST" && r.path === "/api/v1/agent-work/claim");
}

function heartbeats(service) {
  return service.requests.filter((r) => r.path.endsWith("/agent-heartbeat"));
}

// The detached heartbeat of a far-future lease would otherwise outlive the test.
const SHORT_HOLD = { BILISUM_AGENT_MAX_HOLD: "1" };

const CLAIMED = {
  task_id: "t1",
  batch_id: "batch-1",
  round: 1,
  lease_id: "lease-1",
  lease_expires_at: "2099-01-01T00:00:00Z",
  items: [
    {
      item_id: "summary_full#0",
      step: "summary",
      usage_stage: "summary_full",
      index: 0,
      kind: "text",
      messages: [{ role: "user", content: "总结这段转写" }],
      response_format: "json",
      request_hash: "h",
      images: [{ path: "/tmp/f0.jpg", url: "/api/v1/tasks/t1/visual-evidence/media/f0.jpg" }],
    },
  ],
};

function tempFile(name, content) {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-agent-"));
  const path = join(dir, name);
  writeFileSync(path, content, "utf8");
  return { path, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- creating tasks -------------------------------------------------------

test("summarize --engine agent sends the routing fields and declares an Agent caller", async () => {
  await withService(createRoutes(), async (service) => {
    const result = await runCli([
      "summarize", URL_SOURCE, "--engine", "agent", "--agent-capabilities", "text", "--agent-name", "Claude Code",
      "--no-wait", ...service.connectArgs,
    ]);
    assert.equal(result.status, 0, result.stderr);
    const create = service.requests.find((r) => r.method === "POST" && r.path.endsWith("/tasks"));
    assert.equal(create.body.engine, "agent");
    assert.deepEqual(create.body.agent_capabilities, ["text"]);
    assert.equal(create.body.agent_caller, true);
    const lookup = service.requests.find((r) => r.path.endsWith("/reusable-task"));
    assert.equal(lookup.query.engine, "agent");
    assert.equal(JSON.parse(result.stdout).batch_id, "batch-1");
  });
});

test("--agent-name alone declares an Agent caller and leaves the engine to the default", async () => {
  await withService(createRoutes(), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--agent-name", "Codex", "--no-wait", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    const create = service.requests.find((r) => r.method === "POST" && r.path.endsWith("/tasks"));
    assert.equal(create.body.agent_caller, true);
    assert.equal("engine" in create.body, false);
    assert.equal("agent_capabilities" in create.body, false);
    const lookup = service.requests.find((r) => r.path.endsWith("/reusable-task"));
    assert.equal("engine" in lookup.query, false);
  });
});

test("--engine builtin is sent without declaring an Agent caller", async () => {
  await withService(createRoutes(), async (service) => {
    const result = await runCli(["summarize", URL_SOURCE, "--engine", "builtin", "--no-wait", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    const create = service.requests.find((r) => r.method === "POST" && r.path.endsWith("/tasks"));
    assert.equal(create.body.engine, "builtin");
    assert.equal("agent_caller" in create.body, false);
  });
});

test("invalid --engine / --agent-capabilities values are usage errors", async () => {
  const engine = await runCli(["summarize", URL_SOURCE, "--engine", "gpt"]);
  assert.equal(engine.status, 1);
  assert.match(engine.stderr, /--engine 仅支持 builtin \/ agent/);
  const caps = await runCli(["summarize", URL_SOURCE, "--agent-capabilities", "text,audio"]);
  assert.equal(caps.status, 1);
  assert.match(caps.stderr, /--agent-capabilities 仅支持 text \/ vision/);
});

test("waiting stops once a task needs the Agent and prints the batch to claim", async () => {
  await withService(createRoutes({
    "GET /api/v1/tasks/:id/progress": { task_id: "t1", status: "agent_summarizing", progress: 60 },
    "GET /api/v1/tasks/:id": { task_id: "t1", status: "agent_summarizing", title: "Demo", batch_id: "batch-1", result: null },
  }), async (service) => {
    const result = await runCli([
      "summarize", URL_SOURCE, "--engine", "agent", "--agent-name", "Claude Code", ...service.connectArgs,
    ]);
    assert.equal(result.status, 0, result.stderr);
    const body = JSON.parse(result.stdout);
    assert.equal(body.status, "agent_summarizing");
    assert.equal(body.batch_id, "batch-1");
    assert.match(result.stderr, /bilisum agent next --batch batch-1/);
  });
});

// --- agent next ----------------------------------------------------------

test("agent next --batch claims a round and prints it unchanged", async () => {
  await withService({
    "POST /api/v1/agent-work/claim": CLAIMED,
    "POST /api/v1/tasks/:id/agent-heartbeat": { status: 409, json: { detail: "已被其他 Agent 处理" } },
  }, async (service) => {
    const result = await runCli([
      "agent", "next", "--batch", "batch-1", "--agent-name", "Claude Code", "--agent-model", "opus", ...service.connectArgs,
    ], { env: SHORT_HOLD });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), CLAIMED);
    assert.deepEqual(claimRequests(service).map((r) => r.body), [
      { scope: "batch", batch_id: "batch-1", agent: { name: "Claude Code", model: "opus" } },
    ]);
    assert.match(result.stderr, /bilisum agent submit t1 --lease lease-1/);
  });
});

test("agent next --all and --task select the claim scope", async () => {
  await withService({
    "POST /api/v1/agent-work/claim": { task_id: null, pending_tasks: 0 },
  }, async (service) => {
    const all = await runCli(["agent", "next", "--all", "--agent-name", "A", ...service.connectArgs]);
    const one = await runCli(["agent", "next", "--task", "t9", "--agent-name", "A", ...service.connectArgs]);
    assert.equal(all.status, 0, all.stderr);
    assert.equal(one.status, 0, one.stderr);
    assert.deepEqual(claimRequests(service).map((r) => r.body), [
      { scope: "all", agent: { name: "A" } },
      { scope: "task", task_id: "t9", agent: { name: "A" } },
    ]);
  });
});

test("agent next needs exactly one scope and an Agent name", async () => {
  const none = await runCli(["agent", "next", "--agent-name", "A"]);
  assert.equal(none.status, 1);
  assert.match(none.stderr, /--batch <id>、--all 或 --task <id>/);
  const both = await runCli(["agent", "next", "--all", "--batch", "b", "--agent-name", "A"]);
  assert.equal(both.status, 1);
  assert.match(both.stderr, /只能指定一个/);
  const noName = await runCli(["agent", "next", "--all"]);
  assert.equal(noName.status, 1);
  assert.match(noName.stderr, /--agent-name/);
});

test("BILISUM_AGENT_NAME supplies the Agent name", async () => {
  await withService({ "POST /api/v1/agent-work/claim": { task_id: null, pending_tasks: 0 } }, async (service) => {
    const result = await runCli(["agent", "next", "--all", ...service.connectArgs], { env: { BILISUM_AGENT_NAME: "Codex" } });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(claimRequests(service)[0].body.agent, { name: "Codex" });
  });
});

test("agent next --wait 0 returns 'nothing to claim' with the pending count", async () => {
  await withService({ "POST /api/v1/agent-work/claim": { task_id: null, pending_tasks: 2 } }, async (service) => {
    const result = await runCli(["agent", "next", "--batch", "b", "--agent-name", "A", "--wait", "0", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), { task_id: null, pending_tasks: 2 });
    assert.equal(claimRequests(service).length, 1);
    assert.match(result.stderr, /2 个任务仍在处理/);
  });
});

test("agent next keeps claiming while tasks are still being processed", async () => {
  let calls = 0;
  await withService({
    "POST /api/v1/agent-work/claim": () => {
      calls += 1;
      return calls < 2 ? { task_id: null, pending_tasks: 1 } : CLAIMED;
    },
    "POST /api/v1/tasks/:id/agent-heartbeat": { status: 409, json: { detail: "已被其他 Agent 处理" } },
  }, async (service) => {
    const result = await runCli(["agent", "next", "--task", "t1", "--agent-name", "A", "--wait", "30", ...service.connectArgs], { env: SHORT_HOLD });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).lease_id, "lease-1");
    assert.equal(claimRequests(service).length, 2);
  });
});

test("agent next does not wait when nothing is pending in the scope", async () => {
  await withService({ "POST /api/v1/agent-work/claim": { task_id: null, pending_tasks: 0 } }, async (service) => {
    const result = await runCli(["agent", "next", "--batch", "b", "--agent-name", "A", "--wait", "30", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(claimRequests(service).length, 1);
    assert.match(result.stderr, /没有待处理的任务/);
  });
});

test("agent next passes 409 through and reports a missing task", async () => {
  await withService({
    "POST /api/v1/agent-work/claim": (req) => (req.body.task_id === "gone"
      ? { status: 404, json: { detail: "任务不存在" } }
      : { status: 409, json: { detail: "任务正由 Claude Code 处理" } }),
  }, async (service) => {
    const busy = await runCli(["agent", "next", "--task", "t1", "--agent-name", "A", ...service.connectArgs]);
    assert.equal(busy.status, 1);
    assert.match(busy.stderr, /409.*任务正由 Claude Code 处理/);
    const gone = await runCli(["agent", "next", "--task", "gone", "--agent-name", "A", ...service.connectArgs]);
    assert.equal(gone.status, 1);
    assert.match(gone.stderr, /任务不存在：gone/);
  });
});

// --- agent submit --------------------------------------------------------

const RESULTS = [
  { item_id: "summary_full#0", content: "{\"title\":\"t\"}" },
  { item_id: "visual_frame#0", declined: true, reason: "无法看图" },
];

test("agent submit sends the results with the lease and the Agent name", async () => {
  const file = tempFile("results.json", JSON.stringify(RESULTS));
  try {
    await withService({
      "PUT /api/v1/tasks/:id/agent-work": { status: 202, json: { task_id: "t1", status: "running", round: 1 } },
    }, async (service) => {
      const result = await runCli([
        "agent", "submit", "t1", "--lease", "lease-1", "--results", file.path, "--agent-name", "Claude Code",
        ...service.connectArgs,
      ]);
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(JSON.parse(result.stdout), { task_id: "t1", status: "running", round: 1 });
      const put = service.requests.find((r) => r.method === "PUT");
      assert.equal(put.path, "/api/v1/tasks/t1/agent-work");
      assert.deepEqual(put.body, { lease_id: "lease-1", agent: { name: "Claude Code" }, results: RESULTS });
    });
  } finally {
    file.cleanup();
  }
});

test("agent submit reads the results from stdin with --results -", async () => {
  await withService({
    "PUT /api/v1/tasks/:id/agent-work": { status: 202, json: { task_id: "t1", status: "running", round: 1 } },
  }, async (service) => {
    const result = await runCli(
      ["agent", "submit", "t1", "--lease", "lease-1", "--results", "-", "--agent-name", "A", ...service.connectArgs],
      { input: JSON.stringify({ results: RESULTS }) },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), { task_id: "t1", status: "running", round: 1 });
    const put = service.requests.find((r) => r.method === "PUT");
    assert.deepEqual(put.body, { lease_id: "lease-1", agent: { name: "A" }, results: RESULTS });
  });
});

test("agent submit accepts {results: [...]} and needs a results file and lease", async () => {
  const file = tempFile("results.json", JSON.stringify({ results: RESULTS }));
  try {
    await withService({
      "PUT /api/v1/tasks/:id/agent-work": { status: 202, json: { task_id: "t1", status: "running", round: 1 } },
    }, async (service) => {
      const ok = await runCli(["agent", "submit", "t1", "--lease", "l", "--results", file.path, "--agent-name", "A", ...service.connectArgs]);
      assert.equal(ok.status, 0, ok.stderr);
      assert.deepEqual(service.requests.find((r) => r.method === "PUT").body.results, RESULTS);
    });
    const noLease = await runCli(["agent", "submit", "t1", "--results", file.path, "--agent-name", "A"]);
    assert.equal(noLease.status, 1);
    assert.match(noLease.stderr, /--lease/);
    const noResults = await runCli(["agent", "submit", "t1", "--lease", "l", "--agent-name", "A"]);
    assert.equal(noResults.status, 1);
    assert.match(noResults.stderr, /--results/);
  } finally {
    file.cleanup();
  }
});

test("agent submit rejects a results file that is not a result list", async () => {
  const file = tempFile("results.json", "{\"oops\": 1}");
  try {
    const result = await runCli(["agent", "submit", "t1", "--lease", "l", "--results", file.path, "--agent-name", "A"]);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /结果文件/);
  } finally {
    file.cleanup();
  }
});

test("agent submit passes 409 and 422 through", async () => {
  const file = tempFile("results.json", JSON.stringify(RESULTS));
  try {
    await withService({
      "PUT /api/v1/tasks/:id/agent-work": (req) => (req.body.lease_id === "stale"
        ? { status: 409, json: { detail: "已被其他 Agent 处理" } }
        : {
          status: 422,
          json: { detail: { message: "提交内容校验失败", errors: [{ item_id: "summary_full#0", error: "不是合法的 JSON" }] } },
        }),
    }, async (service) => {
      const stale = await runCli(["agent", "submit", "t1", "--lease", "stale", "--results", file.path, "--agent-name", "A", ...service.connectArgs]);
      assert.equal(stale.status, 1);
      assert.match(stale.stderr, /409.*已被其他 Agent 处理/);
      const invalid = await runCli(["agent", "submit", "t1", "--lease", "l", "--results", file.path, "--agent-name", "A", ...service.connectArgs]);
      assert.equal(invalid.status, 1);
      assert.match(invalid.stderr, /422.*提交内容校验失败/);
      assert.match(invalid.stderr, /summary_full#0：不是合法的 JSON/);
    });
  } finally {
    file.cleanup();
  }
});

// --- heartbeat -----------------------------------------------------------

test("the lease is renewed automatically after agent next exits, until the service refuses", async () => {
  let beats = 0;
  await withService({
    "POST /api/v1/agent-work/claim": () => ({
      ...CLAIMED,
      lease_expires_at: new Date(Date.now() + 3000).toISOString(),
    }),
    "POST /api/v1/tasks/:id/agent-heartbeat": (req) => {
      beats += 1;
      if (beats >= 3) {
        return { status: 409, json: { detail: "已被其他 Agent 处理" } };
      }
      return { task_id: req.params.id, lease_id: req.body.lease_id, lease_expires_at: new Date(Date.now() + 3000).toISOString() };
    },
  }, async (service) => {
    const result = await runCli(["agent", "next", "--batch", "batch-1", "--agent-name", "A", ...service.connectArgs]);
    assert.equal(result.status, 0, result.stderr);
    const deadline = Date.now() + 15000;
    while (heartbeats(service).length < 3 && Date.now() < deadline) {
      await sleep(200);
    }
    assert.equal(heartbeats(service).length, 3);
    for (const beat of heartbeats(service)) {
      assert.equal(beat.path, "/api/v1/tasks/t1/agent-heartbeat");
      assert.deepEqual(beat.body, { lease_id: "lease-1" });
      assert.equal(beat.headers.authorization, "Bearer fake-token");
    }
    await sleep(2500);
    assert.equal(heartbeats(service).length, 3, "heartbeats stop after a 409");
  });
});
