"use strict";

/**
 * M3: Agent mode through `bilisum mcp` (process seam: real MCP server, SDK client,
 * fake BiliSum service).
 *
 *   create tools: engine / agent_capabilities, MCP client = Agent caller
 *   bilisum_agent_next:   claim with the client's name, items + image blocks, offset / limit
 *   bilisum_agent_submit: results, 422 / 409 passed through
 *   in-process heartbeat while a claimed round is held
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const jpeg = require("jpeg-js");

const { startFakeService } = require("./helpers/process-seam");
const { startMcp } = require("./helpers/mcp-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";
const CLAIM = "/api/v1/agent-work/claim";

function makeJpeg(width, height) {
  const data = Buffer.alloc(width * height * 4);
  for (let i = 0; i < width * height; i += 1) {
    data[i * 4] = (i * 7) % 256;
    data[i * 4 + 1] = 120;
    data[i * 4 + 2] = 200;
    data[i * 4 + 3] = 255;
  }
  return jpeg.encode({ width, height, data }, 80).data;
}

async function withService(routes, fn) {
  const service = await startFakeService(routes);
  try {
    return await fn(service);
  } finally {
    await service.close();
  }
}

async function withMcp(service, fn, options = {}) {
  const mcp = await startMcp(service.connectArgs, { env: { BILISUM_AGENT_MAX_HOLD: "30" }, ...options });
  try {
    return await fn(mcp);
  } finally {
    await mcp.close();
  }
}

function textItem(id) {
  return {
    item_id: id,
    step: "summary",
    usage_stage: "summary_full",
    index: 0,
    kind: "text",
    messages: [{ role: "user", content: `prompt ${id}` }],
    response_format: "json",
    request_hash: "h",
    images: [],
  };
}

function frameItem(id, images) {
  return {
    item_id: id,
    step: "visual_frame",
    usage_stage: "visual_frame",
    index: 0,
    kind: "vision",
    messages: [{ role: "user", content: [{ type: "text", text: "看图" }, { type: "image_ref", index: 0 }] }],
    response_format: "json",
    request_hash: "h",
    images,
  };
}

function round(items, overrides = {}) {
  return {
    task_id: "t1",
    batch_id: "batch-1",
    round: 1,
    lease_id: "lease-1",
    lease_expires_at: "2099-01-01T00:00:00Z",
    items,
    ...overrides,
  };
}

function requestsTo(service, method, suffix) {
  return service.requests.filter((r) => r.method === method && r.path.endsWith(suffix));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- create tools ----------------------------------------------------------

test("create tools send engine / agent_capabilities and stop for the Agent with a claim hint", async () => {
  await withService({
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages: [] } },
    "GET /api/v1/videos/:id/reusable-task": { status: 404, json: { detail: "没有可复用的任务。" } },
    "POST /api/v1/videos/:id/tasks": { task_id: "t1", status: "queued", title: "Demo", batch_id: "batch-1" },
    "GET /api/v1/tasks/:id/progress": { task_id: "t1", status: "agent_summarizing", progress: 60 },
    "GET /api/v1/tasks/:id": { task_id: "t1", status: "agent_summarizing", title: "Demo", batch_id: "batch-1", result: null },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      const result = await mcp.client.callTool({
        name: "bilisum_summarize",
        arguments: { source: URL_SOURCE, engine: "agent", agent_capabilities: ["text"] },
      });
      assert.notEqual(result.isError, true, JSON.stringify(result.content));
      assert.match(result.content[0].text, /bilisum_agent_next（batch_id=batch-1）/);
      assert.equal(result.structuredContent.done, false);
      assert.equal(result.structuredContent.tasks[0].batch_id, "batch-1");
      const [create] = requestsTo(service, "POST", "/tasks");
      assert.equal(create.body.engine, "agent");
      assert.deepEqual(create.body.agent_capabilities, ["text"]);
      assert.equal(create.body.agent_caller, true);

      const status = await mcp.client.callTool({ name: "bilisum_status", arguments: { task_id: "t1" } });
      assert.match(status.content[0].text, /bilisum_agent_next/);
    }, { clientName: "Claude Desktop" });
  });
});

test("create tools without engine still declare the MCP client an Agent caller", async () => {
  await withService({
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages: [] } },
    "GET /api/v1/videos/:id/reusable-task": { status: 404, json: { detail: "没有可复用的任务。" } },
    "POST /api/v1/videos/:id/tasks": { task_id: "t1", status: "queued", title: "Demo" },
    "GET /api/v1/tasks/:id/progress": { status: "completed", progress: 100 },
    "GET /api/v1/tasks/:id": { task_id: "t1", status: "completed", title: "Demo", result: { overview: "o" } },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      await mcp.client.callTool({ name: "bilisum_brief", arguments: { source: URL_SOURCE } });
      const [create] = requestsTo(service, "POST", "/tasks");
      assert.equal(create.body.agent_caller, true);
      assert.equal("engine" in create.body, false);
      assert.equal("agent_capabilities" in create.body, false);
    });
  });
});

// --- bilisum_agent_next ------------------------------------------------------

test("agent_next claims with the client name and returns frame images per item_id", async () => {
  const dir = mkdtempSync(join(tmpdir(), "bilisum-mcp-frames-"));
  const small = makeJpeg(64, 48);
  const smallPath = join(dir, "t1000.analysis.jpg");
  writeFileSync(smallPath, small);
  const tall = makeJpeg(720, 1600);
  const claimed = round([
    textItem("summary_full#0"),
    frameItem("visual_frame#0", [{ path: smallPath, url: "/api/v1/tasks/t1/visual-evidence/media/t1000.analysis.jpg" }]),
    // Not on this machine: fetched from the media endpoint, then shrunk.
    frameItem("visual_frame#1", [{ path: join(dir, "missing.jpg"), url: "/api/v1/tasks/t1/visual-evidence/media/t2000.analysis.jpg" }]),
  ]);
  try {
    await withService({
      [`POST ${CLAIM}`]: claimed,
      "GET /api/v1/tasks/t1/visual-evidence/media/t2000.analysis.jpg": { status: 200, json: tall },
      "POST /api/v1/tasks/:id/agent-heartbeat": { status: 409, json: { detail: "已被其他 Agent 处理" } },
    }, async (service) => {
      await withMcp(service, async (mcp) => {
        const result = await mcp.client.callTool({
          name: "bilisum_agent_next",
          arguments: { batch_id: "batch-1", agent_model: "opus" },
        });
        assert.notEqual(result.isError, true, JSON.stringify(result.content));
        assert.deepEqual(requestsTo(service, "POST", CLAIM).map((r) => r.body), [
          { scope: "batch", batch_id: "batch-1", agent: { name: "Claude Desktop", model: "opus" } },
        ]);
        assert.equal(result.structuredContent.lease_id, "lease-1");
        assert.equal(result.structuredContent.total_items, 3);
        assert.equal(result.structuredContent.next_offset, null);
        assert.deepEqual(result.structuredContent.items.map((item) => [item.item_id, item.image_count]), [
          ["summary_full#0", 0], ["visual_frame#0", 1], ["visual_frame#1", 1],
        ]);

        // Every image block is preceded by a label naming its item and index.
        const images = [];
        result.content.forEach((block, index) => {
          if (block.type === "image") {
            const label = result.content[index - 1].text;
            images.push({ label, block });
          }
        });
        assert.equal(images.length, 2);
        assert.match(images[0].label, /item_id=visual_frame#0 的图片 0/);
        assert.equal(images[0].block.mimeType, "image/jpeg");
        assert.equal(images[0].block.data, small.toString("base64"), "small frames pass through unchanged");
        assert.match(images[1].label, /item_id=visual_frame#1 的图片 0/);
        const shrunk = jpeg.decode(Buffer.from(images[1].block.data, "base64"));
        assert.equal(Math.max(shrunk.width, shrunk.height), 1280);
        assert.equal(shrunk.width, 576);

        // Item JSON keeps image_ref messages but not local paths.
        const itemTexts = result.content.filter((block) => block.type === "text").map((block) => block.text);
        assert.ok(itemTexts.some((value) => value.includes('"item_id": "visual_frame#0"') && value.includes('"image_ref"')));
        assert.ok(!itemTexts.some((value) => value.includes(dir)));
      }, { clientName: "Claude Desktop" });
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("agent_next pages through a round with offset / limit without claiming again", async () => {
  const claimed = round([textItem("a"), textItem("b"), textItem("c")]);
  await withService({
    [`POST ${CLAIM}`]: claimed,
    "POST /api/v1/tasks/:id/agent-heartbeat": { status: 409, json: { detail: "已被其他 Agent 处理" } },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      const first = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all", limit: 2 } });
      assert.deepEqual(first.structuredContent.items.map((item) => item.item_id), ["a", "b"]);
      assert.equal(first.structuredContent.next_offset, 2);
      assert.match(first.content[0].text, /offset=2/);
      const second = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all", offset: 2, limit: 2 } });
      assert.deepEqual(second.structuredContent.items.map((item) => item.item_id), ["c"]);
      assert.equal(second.structuredContent.next_offset, null);
      assert.equal(second.structuredContent.lease_id, "lease-1");
      assert.equal(requestsTo(service, "POST", CLAIM).length, 1);
    });
  });
});

test("agent_next reports pending work and checks scope arguments", async () => {
  await withService({ [`POST ${CLAIM}`]: { task_id: null, pending_tasks: 0 } }, async (service) => {
    await withMcp(service, async (mcp) => {
      const empty = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "task", task_id: "t9" } });
      assert.notEqual(empty.isError, true);
      assert.deepEqual(empty.structuredContent, { task_id: null, pending_tasks: 0 });
      assert.match(empty.content[0].text, /暂无待办/);
      assert.deepEqual(requestsTo(service, "POST", CLAIM).map((r) => r.body), [
        { scope: "task", task_id: "t9", agent: { name: "bilisum-test" } },
      ]);

      const noBatch = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: {} });
      assert.equal(noBatch.isError, true);
      assert.match(noBatch.content[0].text, /batch_id/);
      const laterPage = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all", offset: 3 } });
      assert.equal(laterPage.isError, true);
    });
  });
});

test("agent_next passes a 409 claim refusal through as an error result", async () => {
  await withService({
    [`POST ${CLAIM}`]: { status: 409, json: { detail: "已被其他 Agent 处理" } },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      const result = await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "task", task_id: "t1" } });
      assert.equal(result.isError, true);
      assert.equal(result.content[0].text, "领取失败（409）：已被其他 Agent 处理");
    });
  });
});

// --- heartbeat + submit --------------------------------------------------------

test("the MCP heartbeat has no time cap: it renews for as long as the round is held", async () => {
  const expiresSoon = () => new Date(Date.now() + 3000).toISOString();
  await withService({
    [`POST ${CLAIM}`]: () => round([textItem("summary_full#0")], { lease_expires_at: expiresSoon() }),
    "POST /api/v1/tasks/:id/agent-heartbeat": (req) => ({ task_id: "t1", lease_id: req.body.lease_id, lease_expires_at: expiresSoon() }),
  }, async (service) => {
    // BILISUM_AGENT_MAX_HOLD only caps the CLI's detached heartbeat.
    await withMcp(service, async (mcp) => {
      await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all" } });
      await sleep(3500);
      assert.ok(requestsTo(service, "POST", "/agent-heartbeat").length >= 2, "still renewing past BILISUM_AGENT_MAX_HOLD");
    }, { env: { BILISUM_AGENT_MAX_HOLD: "1" } });
  });
});

test("the lease is renewed in-process until the round is submitted", async () => {
  const expiresSoon = () => new Date(Date.now() + 3000).toISOString();
  await withService({
    [`POST ${CLAIM}`]: () => round([textItem("summary_full#0")], { lease_expires_at: expiresSoon() }),
    "POST /api/v1/tasks/:id/agent-heartbeat": (req) => ({ task_id: "t1", lease_id: req.body.lease_id, lease_expires_at: expiresSoon() }),
    "PUT /api/v1/tasks/:id/agent-work": { status: 202, json: { task_id: "t1", status: "running", round: 1 } },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all", agent_model: "opus" } });
      await sleep(2500);
      const beats = requestsTo(service, "POST", "/agent-heartbeat");
      assert.ok(beats.length >= 1, "heartbeat sent while holding the lease");
      assert.deepEqual(beats[0].body, { lease_id: "lease-1" });

      const submitted = await mcp.client.callTool({
        name: "bilisum_agent_submit",
        arguments: { task_id: "t1", lease_id: "lease-1", results: [{ item_id: "summary_full#0", content: "{\"a\":1}" }] },
      });
      assert.notEqual(submitted.isError, true, JSON.stringify(submitted.content));
      assert.deepEqual(submitted.structuredContent, { task_id: "t1", status: "running", round: 1 });
      const [put] = requestsTo(service, "PUT", "/agent-work");
      assert.deepEqual(put.body, {
        lease_id: "lease-1",
        agent: { name: "bilisum-test", model: "opus" },
        results: [{ item_id: "summary_full#0", content: "{\"a\":1}" }],
      });
      const afterSubmit = requestsTo(service, "POST", "/agent-heartbeat").length;
      await sleep(1500);
      assert.equal(requestsTo(service, "POST", "/agent-heartbeat").length, afterSubmit, "heartbeat stops after submit");
    });
  });
});

test("agent_submit passes 422 item errors and 409 through", async () => {
  let puts = 0;
  await withService({
    "PUT /api/v1/tasks/:id/agent-work": () => {
      puts += 1;
      return puts === 1
        ? { status: 422, json: { detail: { message: "结果校验失败", errors: [{ item_id: "a", error: "不是合法 JSON" }] } } }
        : { status: 409, json: { detail: "已被其他 Agent 处理" } };
    },
  }, async (service) => {
    await withMcp(service, async (mcp) => {
      const args = { task_id: "t1", lease_id: "lease-1", results: [{ item_id: "a", content: "x" }, { item_id: "b", declined: true, reason: "看不到图" }] };
      const invalid = await mcp.client.callTool({ name: "bilisum_agent_submit", arguments: args });
      assert.equal(invalid.isError, true);
      assert.equal(invalid.content[0].text, "提交失败（422）：结果校验失败\n- a：不是合法 JSON");
      assert.deepEqual(invalid.structuredContent, {
        status: 422,
        detail: { message: "结果校验失败", errors: [{ item_id: "a", error: "不是合法 JSON" }] },
      });
      const taken = await mcp.client.callTool({ name: "bilisum_agent_submit", arguments: args });
      assert.equal(taken.isError, true);
      assert.equal(taken.content[0].text, "提交失败（409）：已被其他 Agent 处理");
      assert.deepEqual(requestsTo(service, "PUT", "/agent-work")[0].body.results, args.results);
    });
  });
});

test("stdout stays protocol-only across an Agent round", async () => {
  await withService({
    [`POST ${CLAIM}`]: round([textItem("a")]),
    "POST /api/v1/tasks/:id/agent-heartbeat": { status: 409, json: { detail: "已被其他 Agent 处理" } },
    "PUT /api/v1/tasks/:id/agent-work": { status: 202, json: { task_id: "t1", status: "running", round: 1 } },
  }, async (service) => {
    const mcp = await startMcp(service.connectArgs);
    try {
      await mcp.client.callTool({ name: "bilisum_agent_next", arguments: { scope: "all" } });
      await mcp.client.callTool({
        name: "bilisum_agent_submit",
        arguments: { task_id: "t1", lease_id: "lease-1", results: [{ item_id: "a", content: "ok" }] },
      });
    } finally {
      await mcp.close();
    }
    assert.deepEqual(mcp.parseErrors, []);
    for (const line of mcp.stdout.split("\n").filter(Boolean)) {
      assert.equal(JSON.parse(line).jsonrpc, "2.0", line);
    }
    assert.ok(service.healthChecks > 0, "the first Agent call brings the service up");
  });
});
