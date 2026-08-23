import assert from "node:assert/strict";

import {
  buildCollectionQueueVideoIds,
  countCollectionStatuses,
  deriveCollectionView,
  filterCollectionItemsByQuery,
  flattenCollectionGroups,
  getCollectionItemStatus,
  groupCollectionItems,
  sortCollectionItems,
} from "../src/collectionViewModel.ts";
import type { VideoCollectionItem } from "../src/types.ts";

function run(name: string, fn: () => Promise<void> | void) {
  Promise.resolve(fn()).then(() => {
    console.log(`ok - ${name}`);
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

function item(
  position: number,
  overrides: Partial<VideoCollectionItem> = {},
): VideoCollectionItem {
  return {
    position,
    bvid: `BV${String(position).padStart(3, "0")}`,
    title: `课程第 ${position} 集`,
    source_url: `https://www.bilibili.com/video/BV${String(position).padStart(3, "0")}`,
    cover_url: "",
    duration: position * 60,
    video_id: `video-${position}`,
    has_result: false,
    latest_status: null,
    source_section_id: position <= 3 ? "opening" : "main",
    source_section_title: position <= 3 ? "序章" : "正篇",
    source_section_position: position <= 3 ? 1 : 2,
    ...overrides,
  };
}

const mixedItems: VideoCollectionItem[] = [
  item(4, { title: "失败案例", latest_status: "failed", custom_position: 2 }),
  item(2, {
    title: "基础概念",
    has_result: true,
    latest_status: "completed",
    last_summary_at: "2026-08-20T12:00:00Z",
    asset_updated_at: "2026-08-19T12:00:00Z",
    custom_position: 1,
  }),
  item(6, {
    title: "课程收尾",
    duration: 240,
    source_section_id: "main",
    source_section_title: "正篇",
    source_section_position: 2,
    custom_position: 5,
  }),
  item(1, { title: "课程导论", custom_position: 3 }),
  item(5, {
    title: "已有摘要但重试失败",
    has_result: true,
    latest_status: "failed",
    last_summary_at: "2026-08-20T12:00:00Z",
    asset_updated_at: "not-a-date",
    custom_position: 4,
  }),
  item(3, {
    title: "正在生成",
    latest_status: "running",
    asset_updated_at: "2026-08-22T12:00:00Z",
    custom_position: 6,
  }),
];

run("collection default view preserves source order across mixed statuses", () => {
  const view = deriveCollectionView(mixedItems, {
    status: "all",
    query: "",
    sort: "source",
    group: "none",
  });

  assert.deepEqual(view.sourceItems.map((entry) => entry.position), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(view.visibleItems.map((entry) => entry.position), [1, 2, 3, 4, 5, 6]);
  assert.equal(view.groups.length, 1);
  assert.deepEqual(view.groups[0]?.items.map((entry) => entry.position), [1, 2, 3, 4, 5, 6]);
});

run("collection statuses prioritize usable results and distinguish active pending and failed", () => {
  const byPosition = new Map(mixedItems.map((entry) => [entry.position, entry]));

  assert.equal(getCollectionItemStatus(byPosition.get(1)!), "pending");
  assert.equal(getCollectionItemStatus(byPosition.get(2)!), "completed");
  assert.equal(getCollectionItemStatus(byPosition.get(3)!), "processing");
  assert.equal(getCollectionItemStatus(byPosition.get(4)!), "failed");
  assert.equal(getCollectionItemStatus(byPosition.get(5)!), "completed");
  assert.deepEqual(countCollectionStatuses(mixedItems), {
    all: 6,
    completed: 2,
    processing: 1,
    pending: 2,
    failed: 1,
  });
});

run("collection filtering and search retain the requested sort order", () => {
  const pendingSearch = deriveCollectionView(mixedItems, {
    status: "pending",
    query: "课程",
    sort: "source_desc",
    group: "none",
  });

  assert.deepEqual(pendingSearch.visibleItems.map((entry) => entry.position), [6, 1]);
});

run("collection search matches title BVID and episode expressions", () => {
  assert.deepEqual(filterCollectionItemsByQuery(mixedItems, "基础概念").map((entry) => entry.position), [2]);
  assert.deepEqual(filterCollectionItemsByQuery(mixedItems, "bv004").map((entry) => entry.position), [4]);
  assert.deepEqual(filterCollectionItemsByQuery(mixedItems, "第3集").map((entry) => entry.position), [3]);
  assert.deepEqual(filterCollectionItemsByQuery(mixedItems, "第 3 集").map((entry) => entry.position), [3]);
});

run("collection date duration and custom sorts use stable source fallbacks", () => {
  assert.deepEqual(
    sortCollectionItems(mixedItems, "recent_summary").map((entry) => entry.position),
    [2, 5, 1, 3, 4, 6],
  );
  assert.deepEqual(
    sortCollectionItems(mixedItems, "recent_update").map((entry) => entry.position),
    [3, 2, 1, 4, 5, 6],
  );
  assert.deepEqual(
    sortCollectionItems([
      item(3, { duration: 600 }),
      item(1, { duration: 600 }),
      item(2, { duration: null }),
    ], "duration").map((entry) => entry.position),
    [1, 3, 2],
  );

  const completeCustomBvidOrder = sortCollectionItems(mixedItems, "custom").map((entry) => entry.bvid);
  assert.deepEqual(completeCustomBvidOrder, ["BV002", "BV004", "BV001", "BV005", "BV006", "BV003"]);
  assert.equal(new Set(completeCustomBvidOrder).size, mixedItems.length);
});

run("collection status and source-section groups preserve visible relative order", () => {
  const visible = sortCollectionItems(mixedItems, "source_desc");
  const statusGroups = groupCollectionItems(visible, "status");
  const completed = statusGroups.find((group) => group.key === "status:completed");
  const pending = statusGroups.find((group) => group.key === "status:pending");

  assert.deepEqual(completed?.items.map((entry) => entry.position), [5, 2]);
  assert.deepEqual(pending?.items.map((entry) => entry.position), [6, 1]);

  const sectionGroups = groupCollectionItems(visible, "source_section");
  assert.deepEqual(sectionGroups.map((group) => group.title), ["序章", "正篇"]);
  assert.deepEqual(sectionGroups[0]?.items.map((entry) => entry.position), [3, 2, 1]);
  assert.deepEqual(sectionGroups[1]?.items.map((entry) => entry.position), [6, 5, 4]);
});

run("collection queue order is independent and retains selected items hidden from the view", () => {
  const sourceItems = [
    item(1),
    item(2, { has_result: true, latest_status: "completed" }),
    item(4, { latest_status: "failed" }),
    item(6),
  ];
  const selectedIds = new Set(["video-1", "video-4", "video-6"]);
  const selectionOrder = ["video-4", "video-6", "video-1"];
  const currentViewItems = [sourceItems[2], sourceItems[0]];
  const build = (order: "source" | "source_desc" | "current_view" | "selection") => (
    buildCollectionQueueVideoIds({ sourceItems, currentViewItems, selectedIds, selectionOrder, order })
  );

  assert.deepEqual(build("source"), ["video-1", "video-4", "video-6"]);
  assert.deepEqual(build("source_desc"), ["video-6", "video-4", "video-1"]);
  assert.deepEqual(build("current_view"), ["video-4", "video-1", "video-6"]);
  assert.deepEqual(build("selection"), ["video-4", "video-6", "video-1"]);
});

run("current-view queue order follows the grouped render order", () => {
  const view = deriveCollectionView([
    item(1, { source_section_id: "later", source_section_position: 2 }),
    item(2, { source_section_id: "first", source_section_position: 1 }),
    item(3, { source_section_id: "later", source_section_position: 2 }),
    item(4, { source_section_id: "first", source_section_position: 1 }),
  ], {
    status: "all",
    query: "",
    sort: "source_desc",
    group: "source_section",
  });
  const renderedItems = flattenCollectionGroups(view.groups);
  const selectedIds = new Set(renderedItems.map((entry) => entry.video_id as string));

  assert.deepEqual(renderedItems.map((entry) => entry.position), [4, 2, 3, 1]);
  assert.deepEqual(buildCollectionQueueVideoIds({
    sourceItems: view.sourceItems,
    currentViewItems: renderedItems,
    selectedIds,
    selectionOrder: [],
    order: "current_view",
  }), ["video-4", "video-2", "video-3", "video-1"]);
});
