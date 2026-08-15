const { rmSync } = require("node:fs");
const { join, resolve } = require("node:path");

const packageRoot = resolve(__dirname, "..");
rmSync(join(packageRoot, "runtime"), { recursive: true, force: true });
