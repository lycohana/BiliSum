"use strict";

/**
 * `bilisum mcp` over the process seam: the official SDK client drives the
 * stdio server, which talks to a fake BiliSum service.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { startFakeService, runCli } = require("./helpers/process-seam");
const { startMcp } = require("./helpers/mcp-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";

async function withService(routes, fn) {
  const service = await startFakeService(routes);
  try {
    return await fn(service);
  } finally {
    await service.close();
  }
}

async function withMcp(service, fn, options) {
  const mcp = await startMcp(service.connectArgs, options);
  try {
    return await fn(mcp);
  } finally {
    await mcp.close();
  }
}

test("tool list: names, read-only hints and parameters", async () => {
  await withService({}, async (service) => {
    await withMcp(service, async (mcp) => {
      const { tools } = await mcp.client.listTools();
      const snapshot = tools.map((tool) => ({
        name: tool.name,
        readOnly: tool.annotations.readOnlyHint,
        params: Object.keys(tool.inputSchema.properties || {}).sort(),
        required: (tool.inputSchema.required || []).slice().sort(),
      }));
      const createParams = [
        "agent_capabilities", "all_pages", "engine", "force", "instructions", "language", "page", "prompt_preset", "source",
        "visual_note", "wait_seconds",
      ];
      assert.deepEqual(snapshot, [
        { name: "bilisum_summarize", readOnly: false, params: createParams, required: ["source"] },
        { name: "bilisum_brief", readOnly: false, params: createParams, required: ["source"] },
        {
          name: "bilisum_transcribe",
          readOnly: false,
          params: ["all_pages", "force", "page", "source", "wait_seconds"],
          required: ["source"],
        },
        { name: "bilisum_status", readOnly: true, params: ["task_id", "wait_seconds"], required: ["task_id"] },
        { name: "bilisum_result", readOnly: true, params: ["format", "max_chars", "offset", "query", "task_id", "time_range"], required: ["task_id"] },
        { name: "bilisum_tasks", readOnly: true, params: ["limit"], required: [] },
        { name: "bilisum_env", readOnly: true, params: [], required: [] },
        { name: "bilisum_doctor", readOnly: true, params: [], required: [] },
        { name: "bilisum_settings_url", readOnly: true, params: [], required: [] },
        {
          name: "bilisum_agent_next",
          readOnly: false,
          params: ["agent_model", "batch_id", "limit", "offset", "scope", "task_id", "wait_seconds"],
          required: [],
        },
        { name: "bilisum_agent_submit", readOnly: false, params: ["lease_id", "results", "task_id"], required: ["lease_id", "results", "task_id"] },
      ]);
      for (const tool of tools) {
        assert.ok(tool.description, `${tool.name} has a description`);
        assert.equal(tool.annotations.destructiveHint, false, `${tool.name} is not destructive`);
      }
      const version = require("../package.json").version;
      assert.deepEqual(mcp.client.getServerVersion(), { name: "bilisum", version });
      assert.match(mcp.client.getInstructions(), /不可信/);
    });
    // Starting the server and listing tools never contacts the service.
    assert.equal(service.healthChecks, 0);
    assert.deepEqual(service.requests, []);
  });
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
    "GET /api/v1/tasks": [completedTask(), completedTask({ task_id: "t2", status: "running", title: "Other" })],
  };
}

function apiCalls(requests) {
  return requests.map(({ method, path, query, body }) => ({ method, path, query, body }));
}

test("CLI and MCP send the same API requests for the same input", async () => {
  // The MCP client is an Agent caller named after its clientInfo (seam default "bilisum-test").
  const asAgent = ["--agent-name", "bilisum-test"];
  const cases = [
    {
      cli: ["summarize", URL_SOURCE, "--page", "2", "--visual-note", "text", "--prompt-preset", "p1", ...asAgent],
      tool: "bilisum_summarize",
      args: { source: URL_SOURCE, page: 2, visual_note: "text", prompt_preset: "p1" },
      pages: [{ page: 1 }, { page: 2 }],
    },
    {
      cli: ["brief", URL_SOURCE, "--all-pages", "--instructions", "  侧重代码  ", "--language", "en", ...asAgent],
      tool: "bilisum_brief",
      args: { source: URL_SOURCE, all_pages: true, instructions: "  侧重代码  ", language: "en" },
      pages: [{ page: 1 }, { page: 2 }],
    },
    {
      cli: ["transcribe", URL_SOURCE, ...asAgent],
      tool: "bilisum_transcribe",
      args: { source: URL_SOURCE },
    },
    { cli: ["status", "t9"], tool: "bilisum_status", args: { task_id: "t9" } },
    { cli: ["tasks", "--limit", "1"], tool: "bilisum_tasks", args: { limit: 1 } },
  ];
  for (const { cli, tool, args, pages } of cases) {
    const viaCli = await withService(videoRoutes({ pages }), async (service) => {
      const result = await runCli([...cli, ...service.connectArgs]);
      assert.equal(result.status, 0, result.stderr);
      return apiCalls(service.requests);
    });
    const viaMcp = await withService(videoRoutes({ pages }), async (service) => {
      await withMcp(service, async (mcp) => {
        const result = await mcp.client.callTool({ name: tool, arguments: args });
        assert.notEqual(result.isError, true, JSON.stringify(result.content));
      });
      return apiCalls(service.requests);
    });
    assert.ok(viaCli.length > 0);
    assert.deepEqual(viaMcp, viaCli, `${tool} requests match ${cli[0]}`);
  }
});

test("summarize returns the knowledge note and task metadata when done in time", async () => {
  await withService(videoRoutes(), async (service) => {
    await withMcp(service, async (mcp) => {
      const result = await mcp.client.callTool({ name: "bilisum_summarize", arguments: { source: URL_SOURCE } });
      assert.equal(result.isError, undefined);
      assert.equal(
        result.content[0].text,
        `---\ntask_id: t1\nstatus: completed\ntitle: Demo\nvideo_id: BV1xx\nsource: ${URL_SOURCE}\n---\n\n# Note body`,
      );
      assert.deepEqual(result.structuredContent, {
        done: true,
        tasks: [{ task_id: "t1", status: "completed", title: "Demo", video_id: "BV1xx", reused: false }],
      });
    });
  });
});

test("brief returns the summary card; transcribe points to the transcript", async () => {
  await withService(videoRoutes(), async (service) => {
    await withMcp(service, async (mcp) => {
      const brief = await mcp.client.callTool({ name: "bilisum_brief", arguments: { source: URL_SOURCE } });
      assert.match(brief.content[0].text, /Short overview\n\n## 要点\n- point A$/);
      assert.doesNotMatch(brief.content[0].text, /Note body/);
      const transcribe = await mcp.client.callTool({ name: "bilisum_transcribe", arguments: { source: URL_SOURCE } });
      assert.doesNotMatch(transcribe.content.map((block) => block.text).join("\n"), /hello transcript/);
      assert.match(transcribe.content.at(-1).text, /bilisum_result（task_id=t2，format=transcript）/);
    });
  });
});

test("an expired wait returns the task id and progress, and status keeps waiting", async () => {
  const running = { status: "running", progress: 42, latest_stage: "transcribe", latest_message: "正在转写" };
  const routes = videoRoutes({ progress: running, task: completedTask({ status: "running", result: null }) });
  await withService(routes, async (service) => {
    await withMcp(service, async (mcp) => {
      const started = Date.now();
      const created = await mcp.client.callTool({
        name: "bilisum_summarize",
        arguments: { source: URL_SOURCE, wait_seconds: 1 },
      });
      assert.ok(Date.now() - started < 5000);
      assert.equal(created.isError, undefined);
      assert.deepEqual(created.structuredContent, {
        done: false,
        tasks: [{ task_id: "t1", status: "running", title: "Demo", progress: 42, stage: "transcribe", reused: false }],
      });
      assert.match(created.content[0].text, /\[t1\]  42% \[transcribe\] 正在转写/);
      assert.match(created.content[0].text, /bilisum_status/);

      const before = service.requests.filter((r) => r.path === "/api/v1/tasks/t1/progress").length;
      const status = await mcp.client.callTool({ name: "bilisum_status", arguments: { task_id: "t1", wait_seconds: 1 } });
      assert.equal(status.structuredContent.done, false);
      assert.equal(status.structuredContent.task_id, "t1");
      assert.equal(status.structuredContent.status, "running");
      const after = service.requests.filter((r) => r.path === "/api/v1/tasks/t1/progress").length;
      assert.ok(after - before >= 2, "status polled progress while waiting");
    });
  });
});

test("result reads in chunks that join back to the full content", async () => {
  const transcript = `开头${"字".repeat(45)}😀结尾`;
  const task = completedTask({ result: { ...completedTask().result, transcript_text: transcript, segments: [{ start: 0 }] } });
  await withService(videoRoutes({ task }), async (service) => {
    await withMcp(service, async (mcp) => {
      let offset = 0;
      let joined = "";
      let rounds = 0;
      while (offset !== null) {
        const chunk = await mcp.client.callTool({
          name: "bilisum_result",
          arguments: { task_id: "t1", format: "transcript", offset, max_chars: 20 },
        });
        assert.equal(chunk.isError, undefined);
        joined += chunk.content[0].text;
        offset = chunk.structuredContent.next_offset;
        rounds += 1;
      }
      assert.equal(joined, transcript);
      assert.equal(rounds, 3);

      const json = await mcp.client.callTool({ name: "bilisum_result", arguments: { task_id: "t1", format: "json" } });
      const parsed = JSON.parse(json.content[0].text);
      assert.equal(parsed.task_id, "t1");
      assert.equal(parsed.result.overview, "Short overview");
      assert.equal(parsed.result.transcript_text, undefined);
      assert.equal(parsed.result.segments, undefined);
      assert.equal(json.structuredContent.next_offset, null);

      const markdown = await mcp.client.callTool({ name: "bilisum_result", arguments: { task_id: "t1" } });
      assert.match(markdown.content[0].text, /# Note body$/);
    });
  });
});

test("result filters the transcript by time range and keyword", async () => {
  const segments = [
    { start: 0, end: 30, text: "开场白" },
    { start: 30, end: 60, text: "讲到 Python 的部分" },
    { start: 60, end: 90, text: "结束语" },
  ];
  const task = completedTask({ result: { ...completedTask().result, segments } });
  await withService(videoRoutes({ task }), async (service) => {
    await withMcp(service, async (mcp) => {
      const ranged = await mcp.client.callTool({
        name: "bilisum_result",
        arguments: { task_id: "t1", format: "transcript", time_range: "00:30-01:00" },
      });
      assert.equal(ranged.content[0].text, "[00:30-01:00] 讲到 Python 的部分");
      assert.deepEqual(ranged.structuredContent.filters, { time_range: { start: 30, end: 60 }, query: null, context: null });

      const hit = await mcp.client.callTool({
        name: "bilisum_result",
        arguments: { task_id: "t1", format: "transcript", query: "python" },
      });
      assert.match(hit.content[0].text, /^\* \[00:30-01:00\] 讲到 Python 的部分$/m);
    });
  });
});

test("business errors are tool results with isError; bad arguments are protocol errors", async () => {
  const routes = {
    ...videoRoutes(),
    "GET /api/v1/tasks/missing": { status: 404, json: { detail: "not found" } },
    "GET /api/v1/tasks/bad": completedTask({ task_id: "bad", status: "failed", error_message: "下载失败" }),
  };
  await withService(routes, async (service) => {
    await withMcp(service, async (mcp) => {
      const missing = await mcp.client.callTool({ name: "bilisum_status", arguments: { task_id: "missing" } });
      assert.equal(missing.isError, true);
      assert.equal(missing.content[0].text, "任务不存在：missing");

      const tooLong = await mcp.client.callTool({
        name: "bilisum_summarize",
        arguments: { source: URL_SOURCE, instructions: "长".repeat(1001) },
      });
      assert.equal(tooLong.isError, true);
      assert.match(tooLong.content[0].text, /1000/);

      const local = await mcp.client.callTool({ name: "bilisum_summarize", arguments: { source: "no-such-file.mp4" } });
      assert.equal(local.isError, true);
      assert.equal(local.content[0].text, "本地文件不存在：no-such-file.mp4");

      const failed = await mcp.client.callTool({ name: "bilisum_result", arguments: { task_id: "bad" } });
      assert.equal(failed.isError, true);
      assert.equal(failed.content[0].text, "任务尚未完成（failed）：下载失败：bad");

      await assert.rejects(
        mcp.client.callTool({ name: "bilisum_status", arguments: { task_id: "t1", wait_seconds: 999 } }),
        (error) => error.code === -32602,
      );
      await assert.rejects(
        mcp.client.callTool({ name: "bilisum_delete", arguments: {} }),
        (error) => error.code === -32602,
      );
    });
  });
});

test("a failed task returns its error message as an error result", async () => {
  const failedTask = completedTask({ status: "failed", error_message: "模型不可用", result: null });
  await withService(videoRoutes({ task: failedTask, progress: { status: "failed" } }), async (service) => {
    await withMcp(service, async (mcp) => {
      const result = await mcp.client.callTool({ name: "bilisum_summarize", arguments: { source: URL_SOURCE } });
      assert.equal(result.isError, true);
      assert.equal(result.content[0].text, "[t1] failed：模型不可用\n1 个任务失败。");
    });
  });
});

test("the service is first contacted by the first tool call", async () => {
  await withService(videoRoutes(), async (service) => {
    await withMcp(service, async (mcp) => {
      await mcp.client.listTools();
      assert.equal(service.healthChecks, 0);
      await mcp.client.callTool({ name: "bilisum_tasks", arguments: {} });
      assert.ok(service.healthChecks > 0);
    });
  });
});

test("stdout carries only protocol messages across a full tool run", async () => {
  const progress = { status: "running", progress: 10, latest_stage: "download" };
  let polls = 0;
  const routes = {
    ...videoRoutes(),
    "GET /api/v1/tasks/:id/progress": () => (++polls < 2 ? progress : { status: "completed", progress: 100 }),
  };
  await withService(routes, async (service) => {
    const mcp = await startMcp(service.connectArgs);
    const notes = [];
    try {
      await mcp.client.listTools();
      await mcp.client.callTool(
        { name: "bilisum_summarize", arguments: { source: URL_SOURCE } },
        undefined,
        { onprogress: (note) => notes.push(note) },
      );
      for (const [name, args] of [
        ["bilisum_brief", { source: URL_SOURCE }],
        ["bilisum_transcribe", { source: URL_SOURCE }],
        ["bilisum_status", { task_id: "t1" }],
        ["bilisum_result", { task_id: "t1", format: "transcript" }],
        ["bilisum_tasks", {}],
        ["bilisum_env", {}],
        ["bilisum_doctor", {}],
        ["bilisum_settings_url", {}],
        ["bilisum_status", { task_id: "nope" }],
      ]) {
        await mcp.client.callTool({ name, arguments: args });
      }
    } finally {
      await mcp.close();
    }
    assert.deepEqual(mcp.parseErrors, []);
    const lines = mcp.stdout.split("\n");
    assert.equal(lines.pop(), "", "stdout ends with a newline");
    assert.ok(lines.length > 10);
    for (const line of lines) {
      const message = JSON.parse(line);
      assert.equal(message.jsonrpc, "2.0", line);
    }
    assert.equal(notes.length, 1);
    assert.match(notes[0].message, /\[t1\]  10% \[download\]/);
  });
});

test("settings_url returns the page address without the token", async () => {
  await withService(videoRoutes(), async (service) => {
    await withMcp(service, async (mcp) => {
      const result = await mcp.client.callTool({ name: "bilisum_settings_url", arguments: {} });
      assert.deepEqual(result.structuredContent, { url: `http://127.0.0.1:${service.port}/settings` });
      assert.doesNotMatch(result.content[0].text, /fake-token/);
    });
  });
});

test("--print-config prints a pasteable client config with the startup flags", async () => {
  const result = await runCli(["mcp", "--environment", "cli", "--idle-timeout", "900", "--token", "secret", "--print-config"]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    mcpServers: {
      bilisum: { command: "npx", args: ["-y", "bilisum", "mcp", "--environment", "cli", "--idle-timeout", "900"] },
    },
  });
  assert.doesNotMatch(result.stdout, /secret/);
});
