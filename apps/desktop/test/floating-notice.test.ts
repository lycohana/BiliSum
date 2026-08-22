import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function run(name: string, fn: () => void) {
  fn();
  console.log(`ok - ${name}`);
}

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const componentSource = readFileSync(join(projectRoot, "src/components/FloatingNoticeStack.tsx"), "utf8");
const stylesSource = readFileSync(join(projectRoot, "src/styles/06-desktop.css"), "utf8");

run("global notices use a five-second default lifetime", () => {
  assert.ok(componentSource.includes("DEFAULT_NOTICE_DURATION_MS = 5000"));
  assert.ok(componentSource.includes("return DEFAULT_NOTICE_DURATION_MS"));
});

run("global notices animate out before unmounting", () => {
  assert.ok(componentSource.includes("is-exiting"), "notice should have an exit state");
  assert.ok(componentSource.includes("NOTICE_EXIT_ANIMATION_MS"), "notice should delay unmount until exit completes");
  assert.ok(stylesSource.includes("@keyframes floatingNoticeOut"), "notice should define an exit animation");
  assert.ok(stylesSource.includes(".floating-notice-pill.is-exiting"), "notice should apply the exit animation state");
});

run("global notice motion respects reduced-motion preferences", () => {
  assert.ok(stylesSource.includes("@media (prefers-reduced-motion: reduce)"));
  assert.ok(stylesSource.includes(".floating-notice-pill.is-exiting"));
});
