"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");

const { skillSourceDir } = require("../lib/runtime");
const {
  globalSkillDir,
  installSkill,
  parseInstallArgs,
  projectSkillDir,
} = require("../lib/skill");

const CLI_PATH = join(__dirname, "..", "bin", "bilisum.js");

function runCli(args, { cwd, env = {} } = {}) {
  return spawnSync(process.execPath, [CLI_PATH, ...args], {
    cwd,
    encoding: "utf8",
    env: { ...process.env, ...env },
    windowsHide: true,
  });
}

test("skill source resolves from the repository checkout", () => {
  const source = skillSourceDir();
  assert.equal(source, resolve(__dirname, "..", "..", "..", ".agents", "skills", "bilisum-video-understanding"));
  assert.ok(existsSync(join(source, "SKILL.md")));
  assert.ok(existsSync(join(source, "agents", "openai.yaml")));
});

test("parseInstallArgs accepts one destination and force", () => {
  assert.deepEqual(parseInstallArgs(["--project", "--force"]), {
    mode: "project",
    path: "",
    force: true,
    help: false,
  });
  assert.throws(() => parseInstallArgs(["--project", "--global"]), /只能选择一个/);
});

test("globalSkillDir honors CODEX_HOME", () => {
  const previous = process.env.CODEX_HOME;
  const home = mkdtempSync(join(tmpdir(), "bilisum-skill-home-"));
  try {
    process.env.CODEX_HOME = home;
    assert.equal(globalSkillDir(), join(home, "skills", "bilisum-video-understanding"));
  } finally {
    if (previous === undefined) {
      delete process.env.CODEX_HOME;
    } else {
      process.env.CODEX_HOME = previous;
    }
    rmSync(home, { recursive: true, force: true });
  }
});

test("globalSkillDir has a Codex home fallback", () => {
  const previous = process.env.CODEX_HOME;
  try {
    delete process.env.CODEX_HOME;
    assert.match(globalSkillDir(), /[\\/]\.codex[\\/]skills[\\/]bilisum-video-understanding$/);
  } finally {
    if (previous !== undefined) {
      process.env.CODEX_HOME = previous;
    }
  }
});

test("installSkill copies managed files and protects existing destinations", () => {
  const home = mkdtempSync(join(tmpdir(), "bilisum-skill-copy-"));
  const destination = join(home, "installed-skill");
  try {
    installSkill(destination);
    assert.ok(existsSync(join(destination, "SKILL.md")));
    assert.ok(existsSync(join(destination, "agents", "openai.yaml")));

    const extraFile = join(destination, "user-file.txt");
    writeFileSync(extraFile, "preserve me", "utf8");
    assert.throws(() => installSkill(destination), /已存在/);
    installSkill(destination, { force: true });
    assert.equal(readFileSync(extraFile, "utf8"), "preserve me");
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("skill path prints the repository skill directory", () => {
  const result = runCli(["skill", "path"]);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), skillSourceDir());
});

test("non-interactive install previews without writing", () => {
  const cwd = mkdtempSync(join(tmpdir(), "bilisum-skill-preview-"));
  const codexHome = mkdtempSync(join(tmpdir(), "bilisum-skill-preview-global-"));
  try {
    const result = runCli(["skill", "install"], { cwd, env: { CODEX_HOME: codexHome } });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Non-interactive mode/);
    assert.ok(!existsSync(projectSkillDir(cwd)));
    assert.ok(!existsSync(join(codexHome, "skills", "bilisum-video-understanding")));
  } finally {
    rmSync(cwd, { recursive: true, force: true });
    rmSync(codexHome, { recursive: true, force: true });
  }
});

test("explicit project install works in a clean working directory", () => {
  const cwd = mkdtempSync(join(tmpdir(), "bilisum-skill-project-"));
  try {
    const result = runCli(["skill", "install", "--project"], { cwd });
    assert.equal(result.status, 0, result.stderr);
    assert.ok(existsSync(join(projectSkillDir(cwd), "SKILL.md")));
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("explicit global and custom-path installs use the requested destinations", () => {
  const codexHome = mkdtempSync(join(tmpdir(), "bilisum-skill-global-"));
  const customPath = mkdtempSync(join(tmpdir(), "bilisum-skill-custom-"));
  try {
    const globalResult = runCli(["skill", "install", "--global"], { env: { CODEX_HOME: codexHome } });
    assert.equal(globalResult.status, 0, globalResult.stderr);
    assert.ok(existsSync(join(codexHome, "skills", "bilisum-video-understanding", "SKILL.md")));

    const customResult = runCli(["skill", "install", "--path", join(customPath, "video-skill")]);
    assert.equal(customResult.status, 0, customResult.stderr);
    assert.ok(existsSync(join(customPath, "video-skill", "agents", "openai.yaml")));
  } finally {
    rmSync(codexHome, { recursive: true, force: true });
    rmSync(customPath, { recursive: true, force: true });
  }
});
