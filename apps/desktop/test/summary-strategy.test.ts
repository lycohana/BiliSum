import assert from "node:assert/strict";

import { getSummaryStrategy, getSummaryStrategyPatch, SUMMARY_STRATEGY_COPY } from "../src/utils/summaryStrategy.ts";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

const cloudDefaults = {
  summary_chunk_concurrency: 2,
  summary_context_mode: "auto",
};

run("detects the speed-first summary strategy", () => {
  assert.equal(getSummaryStrategy(cloudDefaults), "speed");
  assert.equal(SUMMARY_STRATEGY_COPY.speed.contextLabel, "自动上下文");
});

run("detects the cache-first summary strategy", () => {
  assert.equal(
    getSummaryStrategy(
      {
        summary_chunk_concurrency: 1,
        summary_context_mode: "chunked",
      },
    ),
    "cache",
  );
});

run("marks manually mixed settings as custom", () => {
  assert.equal(
    getSummaryStrategy({ ...cloudDefaults, summary_chunk_concurrency: 1 }),
    "custom",
  );
});

run("applies cache-first settings without changing unrelated tuning", () => {
  assert.deepEqual(getSummaryStrategyPatch("cache"), {
    summary_chunk_concurrency: 1,
    summary_context_mode: "chunked",
  });
});

run("keeps task and mindmap concurrency outside strategy presets", () => {
  assert.deepEqual(getSummaryStrategyPatch("speed"), {
    summary_chunk_concurrency: 2,
    summary_context_mode: "auto",
  });
});
