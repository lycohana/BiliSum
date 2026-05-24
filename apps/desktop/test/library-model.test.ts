import assert from "node:assert/strict";

import {
  LIBRARY_VIEW_MODE_STORAGE_KEY,
  LIBRARY_ROOT_FOLDER_NODE_ID,
  buildFolderTree,
  canReorderLibraryView,
  countDirectVideosByFolder,
  getFolderAppendPosition,
  getFolderAncestorIds,
  getFolderInsertPosition,
  getFolderMovePositionFromIndex,
  getSiblingFolderMovePayload,
  getVideoFolderIds,
  isFolderDescendantOf,
  loadLibraryViewMode,
  resolveFolderMoveParentId,
  saveLibraryViewMode,
  filterVideosByScope,
  sortVideosForScope,
} from "../src/libraryModel.ts";
import type { VideoAssetSummary } from "../src/types.ts";

function run(name: string, fn: () => Promise<void> | void) {
  Promise.resolve(fn()).then(() => {
    console.log(`ok - ${name}`);
  }).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

function video(id: string, order: number, folderOrder: number, pins?: Partial<VideoAssetSummary>): VideoAssetSummary {
  return {
    video_id: id,
    canonical_id: id,
    platform: "bilibili",
    title: id,
    source_url: `https://example.com/${id}`,
    cover_url: "",
    latest_status: "completed",
    has_result: false,
    is_favorite: false,
    folder_id: "folder",
    global_order: order,
    folder_order: folderOrder,
    global_pinned: false,
    folder_pinned: false,
    pages: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...pins,
  };
}

run("library view mode persists in local storage", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => values.set(key, value),
  };

  assert.equal(loadLibraryViewMode(storage), "cover");
  saveLibraryViewMode("list", storage);
  assert.equal(values.get(LIBRARY_VIEW_MODE_STORAGE_KEY), "list");
  assert.equal(loadLibraryViewMode(storage), "list");
});

run("library sorting respects global and folder pins", () => {
  const sortedAll = sortVideosForScope([
    video("regular", 1000, 1000),
    video("global", 3000, 3000, { global_pinned: true }),
    video("early", 500, 500),
  ], "all");
  assert.deepEqual(sortedAll.map((item) => item.video_id), ["global", "early", "regular"]);

  const sortedFolder = sortVideosForScope([
    video("regular", 1000, 3000),
    video("folder", 3000, 2000, { folder_pinned: true }),
    video("global", 5000, 4000, { global_pinned: true }),
  ], "folder");
  assert.deepEqual(sortedFolder.map((item) => item.video_id), ["global", "folder", "regular"]);
});

run("library reorder is disabled while searching or filtering", () => {
  assert.equal(canReorderLibraryView("", "all"), true);
  assert.equal(canReorderLibraryView("pytest", "all"), false);
  assert.equal(canReorderLibraryView("", "completed"), false);
});

run("folder drop positions support moving between levels", () => {
  const folders = [
    { folder_id: "deep", parent_id: null, name: "深度学习", position: 1000, created_at: "", updated_at: "" },
    { folder_id: "math", parent_id: null, name: "数学", position: 2000, created_at: "", updated_at: "" },
    { folder_id: "freshman", parent_id: "math", name: "大一", position: 1000, created_at: "", updated_at: "" },
  ];

  assert.equal(getFolderInsertPosition(folders, null, "deep", "before", "freshman"), 0);
  assert.equal(getFolderInsertPosition(folders, null, "math", "after", "freshman"), 3000);
  assert.equal(getFolderAppendPosition(folders, null, "freshman"), 3000);
  assert.equal(getFolderAppendPosition(folders, "deep", "freshman"), 1000);
});

run("folder tree move positions support root and empty-folder drops", () => {
  const folders = [
    { folder_id: "deep", parent_id: null, name: "深度学习", position: 1000, created_at: "", updated_at: "" },
    { folder_id: "math", parent_id: null, name: "数学", position: 2000, created_at: "", updated_at: "" },
    { folder_id: "freshman", parent_id: "math", name: "大一", position: 1000, created_at: "", updated_at: "" },
  ];

  const tree = buildFolderTree(folders);
  assert.deepEqual(tree.map((folder) => folder.folder_id), ["deep", "math"]);
  assert.deepEqual(tree[1].children.map((folder) => folder.folder_id), ["freshman"]);
  assert.equal(getFolderMovePositionFromIndex(folders, null, 0, "freshman"), 0);
  assert.equal(getFolderMovePositionFromIndex(folders, "deep", 0, "freshman"), 1000);
  assert.equal(resolveFolderMoveParentId(LIBRARY_ROOT_FOLDER_NODE_ID), null);
  assert.equal(resolveFolderMoveParentId("deep"), "deep");
});

run("folder tree helpers detect ancestry and direct video counts", () => {
  const folders = [
    { folder_id: "root", parent_id: null, name: "课程", position: 1000, created_at: "", updated_at: "" },
    { folder_id: "chapter", parent_id: "root", name: "章节", position: 1000, created_at: "", updated_at: "" },
    { folder_id: "lesson", parent_id: "chapter", name: "课时", position: 1000, created_at: "", updated_at: "" },
  ];
  const videos = [
    video("a", 1000, 1000, { folder_id: "root" }),
    video("b", 2000, 2000, { folder_id: "chapter" }),
    video("c", 3000, 3000, { folder_id: "chapter" }),
    video("d", 4000, 4000, { folder_id: null }),
  ];

  assert.equal(isFolderDescendantOf(folders, "lesson", "root"), true);
  assert.equal(isFolderDescendantOf(folders, "root", "lesson"), false);
  assert.deepEqual(getFolderAncestorIds(folders, "lesson"), ["chapter", "root"]);

  const counts = countDirectVideosByFolder(videos);
  assert.equal(counts.get("root"), 1);
  assert.equal(counts.get("chapter"), 2);
  assert.equal(counts.has("lesson"), false);
});

run("folder sibling move payloads preserve parent scope", () => {
  const folders = [
    { folder_id: "first", parent_id: null, name: "A", position: 1000, created_at: "", updated_at: "" },
    { folder_id: "second", parent_id: null, name: "B", position: 2000, created_at: "", updated_at: "" },
    { folder_id: "third", parent_id: null, name: "C", position: 3000, created_at: "", updated_at: "" },
    { folder_id: "child", parent_id: "second", name: "B1", position: 1000, created_at: "", updated_at: "" },
  ];

  assert.deepEqual(getSiblingFolderMovePayload(folders, "second", "up"), { parent_id: null, position: 0 });
  assert.deepEqual(getSiblingFolderMovePayload(folders, "second", "down"), { parent_id: null, position: 4000 });
  assert.equal(getSiblingFolderMovePayload(folders, "first", "up"), null);
  assert.equal(getSiblingFolderMovePayload(folders, "child", "down"), null);
});

run("library scope filters include unfiled favorite and folders", () => {
  const videos = [
    video("a", 1000, 1000, { folder_id: "folder", folder_ids: ["folder", "other"], is_favorite: true }),
    video("b", 2000, 2000, { folder_id: null }),
    video("c", 3000, 3000, { folder_id: "other" }),
  ];

  assert.deepEqual(filterVideosByScope(videos, "all").map((item) => item.video_id), ["a", "b", "c"]);
  assert.deepEqual(filterVideosByScope(videos, "unfiled").map((item) => item.video_id), ["b"]);
  assert.deepEqual(filterVideosByScope(videos, "favorite").map((item) => item.video_id), ["a"]);
  assert.deepEqual(filterVideosByScope(videos, "folder").map((item) => item.video_id), ["a"]);
  assert.deepEqual(filterVideosByScope(videos, "other").map((item) => item.video_id), ["a", "c"]);
  assert.deepEqual(getVideoFolderIds(videos[0]), ["folder", "other"]);
});
