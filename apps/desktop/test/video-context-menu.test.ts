import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const sharedMenuSource = readFileSync(join(projectRoot, "src/components/VideoContextMenu.tsx"), "utf8");
const libraryPageSource = readFileSync(join(projectRoot, "src/pages/LibraryPage.tsx"), "utf8");
const collectionPageSource = readFileSync(join(projectRoot, "src/pages/CollectionDetailPage.tsx"), "utf8");

run("video library and outer collection items reuse the complete context menu", () => {
  assert.ok(libraryPageSource.includes('from "../components/VideoContextMenu"'));
  assert.ok(collectionPageSource.includes('from "../components/VideoContextMenu"'));
  for (const label of ["打开详情", "跳到所在分组", "设置分组", "收藏", "全局置顶", "文件夹置顶", "删除视频"]) {
    assert.ok(sharedMenuSource.includes(label), `missing shared video action: ${label}`);
  }
});

run("internal collection items expose only detail and promotion actions", () => {
  assert.ok(collectionPageSource.includes("添加到外层视频列表"));
  assert.ok(collectionPageSource.includes("onPromoteCollectionItem"));
  assert.ok(collectionPageSource.includes("showLibraryActions={Boolean(contextMenuItem.is_global_visible)}"));
  assert.ok(collectionPageSource.includes("extraActions={!contextMenuItem.is_global_visible"));
});

run("outer collection items are removed from the library without deleting their data", () => {
  assert.ok(collectionPageSource.includes('deleteLabel="仅删除外层视频"'));
  assert.ok(collectionPageSource.includes("onRemoveCollectionItemFromLibrary"));
  assert.ok(!collectionPageSource.includes("await onDeleteVideo(contextMenuVideo.video_id)"));
});
