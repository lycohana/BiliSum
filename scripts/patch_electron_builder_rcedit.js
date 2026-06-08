#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const targetPath = path.join(
  repoRoot,
  "apps",
  "desktop",
  "node_modules",
  "app-builder-lib",
  "out",
  "winPackager.js",
);

const originalSnippet = `        // rcedit crashed of executed using wine, resourcehacker works
        if (process.platform === "win32" || process.platform === "darwin") {
            await (0, builder_util_1.executeAppBuilder)(["rcedit", "--args", JSON.stringify(args)], undefined /* child-process */, {}, 3 /* retry three times */);
        }
        else if (this.info.framework.name === "electron") {
            const vendorPath = await (0, windowsSignToolManager_1.getSignVendorPath)();
            await (0, wine_1.execWine)(path.join(vendorPath, "rcedit-ia32.exe"), path.join(vendorPath, "rcedit-x64.exe"), args);
        }`;

const patchedSnippet = `        // rcedit crashed of executed using wine, resourcehacker works
        if (process.platform === "win32") {
            const localRcedit = process.env.VIDMIND_RCEDIT_PATH || process.env.BRIEFVID_RCEDIT_PATH;
            if (localRcedit) {
                await (0, builder_util_1.exec)(localRcedit, args);
            }
            else {
                await (0, builder_util_1.executeAppBuilder)(["rcedit", "--args", JSON.stringify(args)], undefined /* child-process */, {}, 3 /* retry three times */);
            }
        }
        else if (process.platform === "darwin") {
            await (0, builder_util_1.executeAppBuilder)(["rcedit", "--args", JSON.stringify(args)], undefined /* child-process */, {}, 3 /* retry three times */);
        }
        else if (this.info.framework.name === "electron") {
            const vendorPath = await (0, windowsSignToolManager_1.getSignVendorPath)();
            await (0, wine_1.execWine)(path.join(vendorPath, "rcedit-ia32.exe"), path.join(vendorPath, "rcedit-x64.exe"), args);
        }`;

if (!fs.existsSync(targetPath)) {
  console.error(`Target file not found: ${targetPath}`);
  process.exit(1);
}

const source = fs.readFileSync(targetPath, "utf8");

if (source.includes("const localRcedit = process.env.VIDMIND_RCEDIT_PATH || process.env.BRIEFVID_RCEDIT_PATH;")) {
  console.log(`Already patched: ${targetPath}`);
  process.exit(0);
}

if (source.includes("const localRcedit = process.env.VIDMIND_RCEDIT_PATH;")) {
  fs.writeFileSync(
    targetPath,
    source.replace(
      "const localRcedit = process.env.VIDMIND_RCEDIT_PATH;",
      "const localRcedit = process.env.VIDMIND_RCEDIT_PATH || process.env.BRIEFVID_RCEDIT_PATH;",
    ),
    "utf8",
  );
  console.log(`Updated electron-builder rcedit fallback in: ${targetPath}`);
  process.exit(0);
}

if (!source.includes(originalSnippet)) {
  console.error("Expected electron-builder snippet was not found. The installed version may have changed.");
  process.exit(1);
}

fs.writeFileSync(targetPath, source.replace(originalSnippet, patchedSnippet), "utf8");
console.log(`Patched electron-builder rcedit fallback in: ${targetPath}`);
