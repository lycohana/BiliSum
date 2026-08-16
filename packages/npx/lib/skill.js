"use strict";

const { copyFileSync, existsSync, mkdirSync, statSync } = require("node:fs");
const { homedir } = require("node:os");
const { createInterface } = require("node:readline/promises");
const { dirname, join, resolve } = require("node:path");

const { UsageError } = require("./args");
const { skillSourceDir } = require("./runtime");

const SKILL_NAME = "bilisum-video-understanding";
const MANAGED_FILES = ["SKILL.md", join("agents", "openai.yaml")];

function projectSkillDir(cwd = process.cwd()) {
  return resolve(cwd, ".agents", "skills", SKILL_NAME);
}

function globalSkillDir() {
  const codexHome = String(process.env.CODEX_HOME || "").trim();
  const codexRoot = codexHome || join(homedir(), ".codex");
  return resolve(codexRoot, "skills", SKILL_NAME);
}

function destinationForMode(mode, cwd = process.cwd()) {
  if (mode === "project") {
    return projectSkillDir(cwd);
  }
  if (mode === "global") {
    return globalSkillDir();
  }
  throw new UsageError(`未知 skill 安装范围：${mode}`);
}

function parseInstallArgs(args) {
  const options = {
    mode: "",
    path: "",
    force: false,
    help: false,
  };

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--project") {
      if (options.mode || options.path) {
        throw new UsageError("--project、--global 与 --path 只能选择一个。");
      }
      options.mode = "project";
    } else if (arg === "--global") {
      if (options.mode || options.path) {
        throw new UsageError("--project、--global 与 --path 只能选择一个。");
      }
      options.mode = "global";
    } else if (arg === "--path") {
      const value = args[++index];
      if (!value || value.startsWith("--")) {
        throw new UsageError("--path requires a value");
      }
      if (options.mode || options.path) {
        throw new UsageError("--project、--global 与 --path 只能选择一个。");
      }
      options.path = value;
    } else if (arg === "--force") {
      options.force = true;
    } else if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else {
      throw new UsageError(`未知 skill 安装选项：${arg}`);
    }
  }

  return options;
}

function printSkillHelp() {
  console.log("Usage:");
  console.log("  bilisum skill path");
  console.log("  bilisum skill install [--project | --global | --path <dir>] [--force]");
  console.log("");
  console.log("Commands:");
  console.log("  path       Print the bundled video-understanding skill directory");
  console.log("  install    Install the skill interactively or copy to an explicit target");
  console.log("");
  console.log("Install behavior:");
  console.log("  TTY        Choose project, Codex global, or a custom path interactively");
  console.log("  non-TTY    Preview source and destinations without writing files");
  console.log("  --project  Install to ./.agents/skills/ in the current project");
  console.log("  --global   Install to $CODEX_HOME/skills/ or ~/.codex/skills/");
  console.log("  --path     Install to the exact directory supplied");
  console.log("  --force    Overwrite managed files in an existing skill directory");
}

function assertSkillSource(source) {
  if (!existsSync(join(source, "SKILL.md"))) {
    throw new Error(`Bundled BiliSum skill is missing: ${source}`);
  }
  for (const relativePath of MANAGED_FILES) {
    if (!existsSync(join(source, relativePath))) {
      throw new Error(`Bundled BiliSum skill file is missing: ${join(source, relativePath)}`);
    }
  }
}

function installSkill(target, { source = skillSourceDir(), force = false } = {}) {
  assertSkillSource(source);
  const destination = resolve(target);
  if (existsSync(destination)) {
    if (!statSync(destination).isDirectory()) {
      throw new UsageError(`skill 安装目标不是目录：${destination}`);
    }
    if (!force) {
      throw new UsageError(`skill 安装目标已存在：${destination}。如需更新请加 --force。`);
    }
  }

  for (const relativePath of MANAGED_FILES) {
    const targetPath = join(destination, relativePath);
    mkdirSync(dirname(targetPath), { recursive: true });
    copyFileSync(join(source, relativePath), targetPath);
  }
  return destination;
}

function printInstallPreview(source) {
  console.log(`Bundled skill: ${source}`);
  console.log(`Project target: ${projectSkillDir()}`);
  console.log(`Global target:  ${globalSkillDir()}`);
  console.log("");
  console.log("Non-interactive mode: no files were written.");
  console.log("Use --project, --global, or --path <dir> to install explicitly.");
}

async function chooseInteractiveTarget() {
  const readline = createInterface({ input: process.stdin, output: process.stdout });
  try {
    console.log("选择 BiliSum 视频理解 skill 的安装位置：");
    console.log(`  1) 当前项目  ${projectSkillDir()}`);
    console.log(`  2) Codex 全局 ${globalSkillDir()}`);
    console.log("  3) 自定义目录");
    console.log("  q) 取消");
    const choice = (await readline.question("请输入选项 [1/2/3/q]: ")).trim().toLowerCase();
    if (choice === "1") {
      return projectSkillDir();
    }
    if (choice === "2") {
      return globalSkillDir();
    }
    if (choice === "3") {
      const customPath = (await readline.question("请输入 skill 目标目录: ")).trim();
      if (!customPath) {
        throw new UsageError("自定义 skill 目录不能为空。");
      }
      return resolve(customPath);
    }
    if (choice === "q" || choice === "quit" || choice === "cancel") {
      return null;
    }
    throw new UsageError("无效的 skill 安装选项。");
  } finally {
    readline.close();
  }
}

async function installCommand(args) {
  const options = parseInstallArgs(args);
  if (options.help) {
    printSkillHelp();
    return;
  }

  const source = resolve(skillSourceDir());
  let destination = "";
  if (options.mode) {
    destination = destinationForMode(options.mode);
  } else if (options.path) {
    destination = resolve(options.path);
  } else if (process.stdin.isTTY && process.stdout.isTTY) {
    destination = await chooseInteractiveTarget();
  } else {
    printInstallPreview(source);
    return;
  }

  if (!destination) {
    console.log("已取消 skill 安装。");
    return;
  }

  const installed = installSkill(destination, { source, force: options.force });
  console.log(`BiliSum 视频理解 skill 已安装到：${installed}`);
}

async function runSkillCommand(args) {
  const [subcommand, ...rest] = args;
  if (!subcommand || subcommand === "help" || subcommand === "--help" || subcommand === "-h") {
    printSkillHelp();
    return;
  }
  if (subcommand === "path") {
    if (rest.length > 0) {
      throw new UsageError("用法：bilisum skill path");
    }
    console.log(resolve(skillSourceDir()));
    return;
  }
  if (subcommand === "install") {
    await installCommand(rest);
    return;
  }
  throw new UsageError(`未知 skill 子命令：${subcommand}`);
}

module.exports = {
  SKILL_NAME,
  MANAGED_FILES,
  projectSkillDir,
  globalSkillDir,
  destinationForMode,
  parseInstallArgs,
  installSkill,
  printSkillHelp,
  runSkillCommand,
};
