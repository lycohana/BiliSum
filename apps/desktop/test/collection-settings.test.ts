import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const detailSource = readFileSync(join(projectRoot, "src/pages/CollectionDetailPage.tsx"), "utf8");
const dialogSource = readFileSync(join(projectRoot, "src/components/CollectionSettingsDialog.tsx"), "utf8");
const cardSource = readFileSync(join(projectRoot, "src/components/CollectionCard.tsx"), "utf8");
const apiSource = readFileSync(join(projectRoot, "src/api.ts"), "utf8");

run("collection settings button sits between refresh and delete controls", () => {
  const refreshIndex = detailSource.indexOf('className="collection-refresh-control"');
  const settingsIndex = detailSource.indexOf('aria-label="合集设置"');
  const deleteIndex = detailSource.indexOf('className="collection-delete-anchor"');
  assert.ok(refreshIndex >= 0 && settingsIndex > refreshIndex && deleteIndex > settingsIndex);
  assert.ok(detailSource.includes("<SettingsIcon"));
});

run("collection cover dialog provides preview upload validation and source reset", () => {
  for (const content of ["合集封面", "选择图片", "保存封面", "恢复原封面", "JPEG、PNG、WebP", "10MB"]) {
    assert.ok(dialogSource.includes(content), `missing collection cover control: ${content}`);
  }
  assert.ok(dialogSource.includes("URL.createObjectURL"));
  assert.ok(dialogSource.includes("URL.revokeObjectURL"));
});

run("collection cover API supports upload and reset", () => {
  assert.ok(apiSource.includes("uploadVideoCollectionCover"));
  assert.ok(apiSource.includes("resetVideoCollectionCover"));
  assert.ok(apiSource.includes('method: "PUT"'));
  assert.ok(apiSource.includes('method: "DELETE"'));
});

run("collection cards retry image loading when the effective cover changes", () => {
  assert.ok(cardSource.includes("useEffect(() => setFailed(false), [coverUrl])"));
  assert.ok(cardSource.includes("<CollectionCoverImage"));
});
