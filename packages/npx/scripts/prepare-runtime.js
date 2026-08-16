const { copyFileSync, cpSync, existsSync, mkdirSync, readdirSync, rmSync, readFileSync } = require("node:fs");
const { basename, join, resolve } = require("node:path");

const packageRoot = resolve(__dirname, "..");
const repoRoot = resolve(packageRoot, "..", "..");
const runtimeRoot = join(packageRoot, "runtime");
const skillRoot = join(packageRoot, "skill");

function copyDir(from, to) {
  if (!existsSync(from)) {
    throw new Error(`Missing runtime source: ${from}`);
  }
  mkdirSync(to, { recursive: true });
  cpSync(from, to, {
    recursive: true,
    force: true,
    filter: (source) => {
      const name = basename(source);
      return !["__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"].includes(name);
    },
  });
}

function copyPackage(relativePath) {
  const source = join(repoRoot, relativePath);
  const target = join(runtimeRoot, relativePath);
  mkdirSync(target, { recursive: true });
  copyFileSync(join(source, "pyproject.toml"), join(target, "pyproject.toml"));
  copyDir(join(source, "src"), join(target, "src"));
}

function copyWebStatic() {
  // Web UI (settings page) is optional for the CLI: it is only needed for
  // `bilisum --setting`. Skip gracefully when the static build is absent.
  const source = join(repoRoot, "apps", "web", "static");
  if (!existsSync(source)) {
    console.log("apps/web/static not found; CLI --setting page will be unavailable in this package.");
    return;
  }
  const target = join(runtimeRoot, "apps", "web", "static");
  copyDir(source, target);

  const indexHtml = readFileSync(join(target, "index.html"), "utf8");
  const referencedAssets = new Set([...indexHtml.matchAll(/\/static\/assets\/([^"']+)/g)].map((match) => match[1]));
  const assetsDir = join(target, "assets");
  if (!existsSync(assetsDir)) {
    return;
  }

  for (const entry of readdirSync(assetsDir, { withFileTypes: true })) {
    if (!entry.isFile()) {
      continue;
    }
    const name = entry.name;
    const isBuildEntry = /^index-.*\.(js|css)$/.test(name);
    if (isBuildEntry && !referencedAssets.has(name)) {
      rmSync(join(assetsDir, name), { force: true });
    }
  }
}

function copyAgentSkill() {
  const source = join(repoRoot, ".agents", "skills", "bilisum-video-understanding");
  const target = join(skillRoot, "bilisum-video-understanding");
  copyDir(source, target);
}

rmSync(runtimeRoot, { recursive: true, force: true });
rmSync(skillRoot, { recursive: true, force: true });
mkdirSync(runtimeRoot, { recursive: true });

copyPackage(join("packages", "infra"));
copyPackage(join("packages", "core"));
copyPackage(join("apps", "service"));
copyWebStatic();
copyAgentSkill();
if (existsSync(join(repoRoot, "VERSION"))) {
  copyFileSync(join(repoRoot, "VERSION"), join(runtimeRoot, "VERSION"));
}

console.log(`Prepared BiliSum npx runtime at ${runtimeRoot}`);
