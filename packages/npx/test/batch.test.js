"use strict";

/**
 * F2-1: pages created by one command share one batch. The CLI creates one task
 * per request, so later pages join the batch the first created task got.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const { startFakeService, runCli } = require("./helpers/process-seam");

const URL_SOURCE = "https://www.bilibili.com/video/BV1xx";

function routes() {
  return {
    "POST /api/v1/videos/probe": { video: { video_id: "BV1xx", title: "Demo", pages: [{ page: 1 }, { page: 2 }, { page: 3 }] } },
    "GET /api/v1/videos/:id/reusable-task": { status: 404, json: { detail: "没有可复用的任务。" } },
    "POST /api/v1/videos/:id/tasks": (req) => ({
      task_id: `new${req.body.page_number}`,
      status: "queued",
      title: "Demo",
      batch_id: req.body.batch_id || `batch-of-p${req.body.page_number}`,
    }),
  };
}

test("--all-pages creates every page in the batch of the first created task", async () => {
  const service = await startFakeService(routes());
  let result;
  try {
    result = await runCli(["summarize", URL_SOURCE, "--all-pages", "--no-wait", ...service.connectArgs]);
  } finally {
    await service.close();
  }
  assert.equal(result.status, 0, result.stderr);
  const creates = service.requests.filter((r) => r.method === "POST" && r.path.endsWith("/tasks"));
  assert.deepEqual(
    creates.map((r) => r.body.batch_id),
    [undefined, "batch-of-p1", "batch-of-p1"],
  );
});
