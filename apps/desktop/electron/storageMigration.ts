import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { createReadStream, createWriteStream } from "node:fs";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

export type MigrationExternalKey = "data" | "cache" | "tasks" | "output";
export type MigrationItemKey = "app-root" | MigrationExternalKey;
export const MIGRATION_EXTERNAL_FOLDER = ".bilisum-migrated-external";

export type MigrationLocation = {
  key: MigrationExternalKey;
  label: string;
  path: string;
};

export type MigrationPreviewItem = {
  key: MigrationItemKey;
  label: string;
  path: string;
  exists: boolean;
  sizeBytes: number;
  selected: boolean;
  required: boolean;
  carriedByRoot: boolean;
  selectable: boolean;
  reason: string | null;
  errorDetails: string | null;
  destinationPath: string | null;
  copySourceKey: MigrationItemKey | null;
  includedInLabel: string | null;
};

export type MigrationPreview = {
  sourceRoot: string;
  targetRoot: string;
  environmentManaged: boolean;
  items: MigrationPreviewItem[];
  requiredBytes: number;
  availableBytes: number | null;
  blockers: string[];
  canMigrate: boolean;
  warnings: string[];
  excludedPaths: string[];
};

export type MigrationProgress = {
  phase: "preparing" | "copying" | "verifying" | "switching" | "starting" | "rollback" | "complete" | "failed";
  message: string;
  copiedBytes: number;
  totalBytes: number;
};

export type MigratedFileRecord = {
  sourcePath: string;
  destinationPath: string;
  sourceHash: string;
  destinationHash: string;
  sizeBytes: number;
};

export type MigratedDirectoryRecord = {
  sourcePath: string;
  destinationPath: string;
};

export type MigrationCopyResult = {
  files: MigratedFileRecord[];
  directories: MigratedDirectoryRecord[];
  copiedBytes: number;
};

export type MigrationCleanupResult = {
  deletedFiles: number;
  skippedFiles: number;
  removedDirectories: number;
  retainedPaths: string[];
};

export function rebaseMigrationDestinations(records: MigrationCopyResult, previousRoot: string, finalRoot: string) {
  for (const record of records.files) {
    record.destinationPath = path.resolve(finalRoot, path.relative(previousRoot, record.destinationPath));
  }
  for (const record of records.directories) {
    record.destinationPath = path.resolve(finalRoot, path.relative(previousRoot, record.destinationPath));
  }
}

export async function refreshMigrationDestinationHashes(records: MigrationCopyResult) {
  for (const record of records.files) {
    record.destinationHash = await hashFile(record.destinationPath);
  }
}

type MigrationPreviewInput = {
  sourceRoot: string;
  targetRoot: string;
  locations: MigrationLocation[];
  selectedExternalKeys?: MigrationExternalKey[];
  environmentManaged: boolean;
};

type MigrationPreviewDependencies = {
  availableBytesForPath?: (path: string) => Promise<number | null>;
};

type PathInspection = {
  exists: boolean;
  sizeBytes: number;
  error: string | null;
};

function normalizedForCompare(value: string) {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

export function isPathWithin(parentPath: string, candidatePath: string) {
  const parent = normalizedForCompare(parentPath);
  const candidate = normalizedForCompare(candidatePath);
  const relative = path.relative(parent, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

export function pathsOverlap(firstPath: string, secondPath: string) {
  return isPathWithin(firstPath, secondPath) || isPathWithin(secondPath, firstPath);
}

export function resolveStorageRoot(environmentRoot: string | undefined, desktopPreferenceRoot: string | undefined, systemDefaultRoot: string) {
  const configuredEnvironmentRoot = String(environmentRoot || "").trim();
  if (configuredEnvironmentRoot) return path.resolve(configuredEnvironmentRoot);
  const configuredDesktopRoot = String(desktopPreferenceRoot || "").trim();
  if (configuredDesktopRoot) return path.resolve(configuredDesktopRoot);
  return path.resolve(systemDefaultRoot);
}

export function getActiveTaskCount(tasks: Array<{ status?: string }>) {
  return tasks.filter((task) => task.status === "queued" || task.status === "running").length;
}

function isExcluded(targetPath: string, excludedPaths: string[]) {
  return excludedPaths.some((excluded) => isPathWithin(excluded, targetPath));
}

async function inspectTree(targetPath: string, excludedPaths: string[] = []): Promise<PathInspection> {
  let rootStats: fs.Stats;
  try {
    rootStats = await fs.promises.lstat(targetPath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { exists: false, sizeBytes: 0, error: null };
    }
    return { exists: false, sizeBytes: 0, error: error instanceof Error ? error.message : "无法读取目录。" };
  }
  if (rootStats.isSymbolicLink()) {
    return { exists: true, sizeBytes: 0, error: "路径本身是符号链接，暂不支持迁移。" };
  }
  if (!rootStats.isDirectory()) {
    return { exists: true, sizeBytes: rootStats.size, error: "存储路径不是目录。" };
  }

  let sizeBytes = 0;
  const stack = [targetPath];
  while (stack.length) {
    const current = stack.pop();
    if (!current) continue;
    let entries: fs.Dirent[];
    try {
      entries = await fs.promises.readdir(current, { withFileTypes: true });
    } catch (error) {
      return { exists: true, sizeBytes, error: `无法读取 ${current}：${error instanceof Error ? error.message : "权限不足。"}` };
    }
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      if (isExcluded(entryPath, excludedPaths)) continue;
      try {
        const stats = await fs.promises.lstat(entryPath);
        if (stats.isSymbolicLink()) {
          return { exists: true, sizeBytes, error: `目录包含符号链接：${entryPath}` };
        }
        if (stats.isDirectory()) {
          stack.push(entryPath);
        } else if (stats.isFile()) {
          sizeBytes += stats.size;
        } else {
          return { exists: true, sizeBytes, error: `目录包含暂不支持的文件类型：${entryPath}` };
        }
      } catch (error) {
        return { exists: true, sizeBytes, error: `无法检查 ${entryPath}：${error instanceof Error ? error.message : "权限不足。"}` };
      }
    }
  }
  return { exists: true, sizeBytes, error: null };
}

async function canonicalizePath(targetPath: string) {
  const resolved = path.resolve(targetPath);
  const missing: string[] = [];
  let existing = resolved;
  while (true) {
    try {
      await fs.promises.lstat(existing);
      break;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      const parent = path.dirname(existing);
      if (parent === existing) throw error;
      missing.unshift(path.basename(existing));
      existing = parent;
    }
  }
  const realExisting = await fs.promises.realpath(existing);
  return path.resolve(realExisting, ...missing);
}

async function inspectTarget(
  targetRoot: string,
  blockers: string[],
  availableBytesForPath?: (path: string) => Promise<number | null>,
) {
  let targetStats: fs.Stats | null = null;
  try {
    targetStats = await fs.promises.lstat(targetRoot);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      blockers.push(`无法检查目标目录：${error instanceof Error ? error.message : "权限不足。"}`);
    }
  }
  if (targetStats?.isSymbolicLink()) {
    blockers.push("目标目录不能是符号链接。 ");
  } else if (targetStats && !targetStats.isDirectory()) {
    blockers.push("目标位置已被文件占用。 ");
  } else if (targetStats?.isDirectory()) {
    try {
      if ((await fs.promises.readdir(targetRoot)).length > 0) {
        blockers.push("目标目录不为空，请选择一个空目录或新目录。 ");
      }
    } catch (error) {
      blockers.push(`无法读取目标目录：${error instanceof Error ? error.message : "权限不足。"}`);
    }
  }

  let writableAncestor = path.dirname(targetRoot);
  while (true) {
    try {
      const stats = await fs.promises.stat(writableAncestor);
      if (!stats.isDirectory()) {
        blockers.push("目标目录的上级路径不是目录。 ");
        return null;
      }
      await fs.promises.access(writableAncestor, fs.constants.W_OK);
      if (availableBytesForPath) {
        const availableBytes = await availableBytesForPath(writableAncestor);
        if (availableBytes === null) blockers.push("无法确定目标磁盘的可用空间。 ");
        return availableBytes;
      }
      const filesystem = await fs.promises.statfs(writableAncestor);
      return Number(filesystem.bavail) * Number(filesystem.bsize);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        const parent = path.dirname(writableAncestor);
        if (parent === writableAncestor) break;
        writableAncestor = parent;
        continue;
      }
      blockers.push(`目标磁盘不可写或无法读取剩余空间：${error instanceof Error ? error.message : "权限不足。"}`);
      return null;
    }
  }
  blockers.push("无法确定目标磁盘的可用空间。 ");
  return null;
}

export async function previewStorageMigration(
  input: MigrationPreviewInput,
  dependencies: MigrationPreviewDependencies = {},
): Promise<MigrationPreview> {
  const sourceRoot = path.resolve(input.sourceRoot);
  const targetRoot = path.resolve(input.targetRoot || "");
  const blockers: string[] = [];
  const warnings: string[] = [];
  const selectedKeys = new Set(input.selectedExternalKeys ?? ["data", "cache", "tasks"]);
  const items: MigrationPreviewItem[] = [{
    key: "app-root", label: "应用数据与托管运行时", path: sourceRoot,
    exists: false, sizeBytes: 0, selected: true, required: true,
    carriedByRoot: false, selectable: false, reason: null, errorDetails: null,
    destinationPath: targetRoot, copySourceKey: "app-root", includedInLabel: null,
  }];
  let canonicalSourceRoot = sourceRoot;
  try { canonicalSourceRoot = await canonicalizePath(sourceRoot); }
  catch { blockers.push("无法解析应用根目录，请检查路径和访问权限。"); }
  if (input.environmentManaged) blockers.push("当前存储位置由 VIDEO_SUM_APP_DATA_ROOT 启动环境变量指定，无法从界面迁移。");
  if (!input.targetRoot.trim()) blockers.push("请选择新的存储目录。");

  for (const location of input.locations) {
    const locationPath = path.resolve(location.path);
    let canonicalLocation = locationPath;
    try { canonicalLocation = await canonicalizePath(locationPath); }
    catch { blockers.push(`无法解析${location.label}，请检查路径和访问权限。`); }
    const carriedByRoot = isPathWithin(sourceRoot, locationPath);
    if (isPathWithin(canonicalSourceRoot, canonicalLocation) !== carriedByRoot ||
        normalizedForCompare(canonicalLocation) !== normalizedForCompare(locationPath)) {
      blockers.push(`${location.label}包含路径别名或符号链接，请先改用实际目录。`);
    }
    if (!carriedByRoot && pathsOverlap(canonicalSourceRoot, canonicalLocation)) {
      blockers.push(`${location.label}与应用根目录重叠；请先调整目录设置，再迁移。`);
    }
    const selected = carriedByRoot || selectedKeys.has(location.key);
    items.push({
      key: location.key, label: location.label, path: locationPath, exists: false, sizeBytes: 0,
      selected, required: false, carriedByRoot, selectable: !carriedByRoot,
      reason: null, errorDetails: null, destinationPath: null, copySourceKey: null, includedInLabel: null,
    });
  }

  // runtime_subprocess_env owns this disposable pip/uv temporary tree. Do not
  // read it, copy it or delete it. Model caches and every other subtree remain
  // strict: an unreadable durable file must still stop migration.
  const cache = items.find((item) => item.key === "cache");
  const temporaryPaths = cache ? [path.join(cache.path, "runtime-temp")] : [];
  const unselected = items.filter((item) => !item.selected);
  const excludedPaths = [...temporaryPaths, ...unselected.map((item) => item.path)];
  for (const item of items.filter((entry) => entry.selected)) {
    if (temporaryPaths.some((temporary) => isPathWithin(temporary, item.path))) {
      blockers.push(`${item.label}位于安装临时目录内，请先调整目录设置。`);
    }
    if (unselected.some((other) => normalizedForCompare(other.path) === normalizedForCompare(item.path))) {
      blockers.push(`${item.label}与未选目录使用同一路径，请统一它们的迁移选择。`);
    }
    // A selected child travels with the highest selected ancestor, unless an
    // unselected directory separates them. Each physical file is copied once.
    const ancestors = items.filter((ancestor) => ancestor.selected && ancestor !== item &&
      isPathWithin(ancestor.path, item.path) &&
      (normalizedForCompare(ancestor.path) !== normalizedForCompare(item.path) || items.indexOf(ancestor) < items.indexOf(item)) &&
      !unselected.some((other) => isPathWithin(ancestor.path, other.path) && isPathWithin(other.path, item.path)));
    const owner = ancestors.sort((a, b) => a.path.length - b.path.length)[0] || item;
    item.copySourceKey = owner.key;
    const ownerDestination = owner.key === "app-root" ? targetRoot : path.join(targetRoot, MIGRATION_EXTERNAL_FOLDER, owner.key);
    item.destinationPath = path.join(ownerDestination, path.relative(owner.path, item.path));
    item.includedInLabel = owner === item ? null : owner.label;
  }

  const physicalRoots = items.filter((item) => item.selected && item.copySourceKey === item.key);
  for (const item of items) {
    if (!physicalRoots.includes(item)) {
      // No recursive scan for a child or for a directory staying in place.
      // Its bytes are already included in its physical migration unit.
      try { item.exists = (await fs.promises.lstat(item.path)).isDirectory(); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT" && item.selected) {
          item.reason = "无法检查目录，请检查访问权限。";
          item.errorDetails = error instanceof Error ? error.message : String(error);
          blockers.push(`${item.label}无法迁移，请展开目录详情检查。`);
        }
      }
      continue;
    }
    const exclusions = excludedPaths.filter((excluded) => isPathWithin(item.path, excluded));
    const inspection = await inspectTree(item.path, exclusions);
    item.exists = inspection.exists;
    item.sizeBytes = inspection.sizeBytes;
    if (inspection.error) {
      item.reason = "目录无法完整读取，迁移已暂停。";
      item.errorDetails = inspection.error;
      blockers.push(`${item.label}无法完整读取，请展开目录详情检查。`);
    }
    if (temporaryPaths.some((temporary) => isPathWithin(item.path, temporary))) {
      warnings.push("安装临时文件不参与迁移；新位置需要时会重新创建。旧临时文件会保留。");
    }
  }
  if (unselected.some((item) => physicalRoots.some((root) => isPathWithin(root.path, item.path)))) {
    warnings.push("未勾选的目录会保留在原位置，即使它位于已勾选的数据目录内。");
  }
  if (physicalRoots.some((item) => item.key !== "app-root")) {
    try {
      await fs.promises.lstat(path.join(sourceRoot, MIGRATION_EXTERNAL_FOLDER));
      blockers.push(`应用根目录已包含保留目录 ${MIGRATION_EXTERNAL_FOLDER}，为避免覆盖用户文件，请先调整该目录。`);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") blockers.push("无法检查迁移保留目录，请检查访问权限。");
    }
  }

  let availableBytes: number | null = null;
  if (input.targetRoot.trim()) {
    availableBytes = await inspectTarget(targetRoot, blockers, dependencies.availableBytesForPath);
    try {
      const canonicalSource = await canonicalizePath(sourceRoot);
      const canonicalTarget = await canonicalizePath(targetRoot);
      if (pathsOverlap(canonicalSource, canonicalTarget)) {
        blockers.push("新旧应用数据目录不能相同，也不能互相包含。 ");
      }
      for (const source of input.locations) {
        const canonicalExternal = await canonicalizePath(source.path);
        if (pathsOverlap(canonicalTarget, canonicalExternal)) {
          blockers.push(`目标目录与${source.key === "output" ? "输出" : source.label}目录重叠：${source.path}。`);
        }
      }
    } catch (error) {
      blockers.push(`无法解析迁移路径：${error instanceof Error ? error.message : "路径无效。"}`);
    }
  }

  const requiredBytes = physicalRoots.reduce((sum, item) => sum + item.sizeBytes, 0);
  if (availableBytes !== null && availableBytes < requiredBytes) {
    blockers.push(`目标磁盘空间不足：需要 ${requiredBytes} 字节，可用 ${availableBytes} 字节。`);
  }

  return {
    sourceRoot,
    targetRoot,
    environmentManaged: input.environmentManaged,
    items,
    requiredBytes,
    availableBytes,
    blockers: [...new Set(blockers)],
    canMigrate: blockers.length === 0,
    warnings: [...new Set(warnings)],
    excludedPaths,
  };
}

async function hashFile(filePath: string) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) {
    hash.update(chunk as Buffer);
  }
  return hash.digest("hex");
}

async function copyTree(
  sourcePath: string,
  destinationPath: string,
  records: MigrationCopyResult,
  onProgress: (copiedBytes: number) => void,
  excludedPaths: string[],
) {
  const stats = await fs.promises.lstat(sourcePath);
  if (stats.isSymbolicLink()) {
    throw new Error(`迁移期间检测到符号链接，已中止：${sourcePath}`);
  }
  if (stats.isDirectory()) {
    await fs.promises.mkdir(destinationPath, { recursive: true, mode: stats.mode });
    records.directories.push({ sourcePath, destinationPath });
    const entries = await fs.promises.readdir(sourcePath, { withFileTypes: true });
    for (const entry of entries) {
      const child = path.join(sourcePath, entry.name);
      if (!isExcluded(child, excludedPaths)) {
        await copyTree(child, path.join(destinationPath, entry.name), records, onProgress, excludedPaths);
      }
    }
    return;
  }
  if (!stats.isFile()) {
    throw new Error(`迁移期间发现不支持的文件类型：${sourcePath}`);
  }

  await fs.promises.mkdir(path.dirname(destinationPath), { recursive: true });
  let streamedBytes = 0;
  const progressTransform = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      streamedBytes += chunk.length;
      onProgress(records.copiedBytes + streamedBytes);
      callback(null, chunk);
    },
  });
  await pipeline(
    createReadStream(sourcePath),
    progressTransform,
    createWriteStream(destinationPath, { flags: "wx", mode: stats.mode }),
  );
  await fs.promises.chmod(destinationPath, stats.mode).catch(() => undefined);
  const sourceHash = await hashFile(sourcePath);
  const destinationHash = await hashFile(destinationPath);
  if (sourceHash !== destinationHash) {
    throw new Error(`文件校验失败：${sourcePath}`);
  }
  records.files.push({ sourcePath, destinationPath, sourceHash, destinationHash, sizeBytes: stats.size });
  records.copiedBytes += stats.size;
  onProgress(records.copiedBytes);
}

export type MigrationPathMapping = { source: string; target: string };

export function getMigrationPathMappings(preview: MigrationPreview): MigrationPathMapping[] {
  // Identity entries shield unselected child paths from a moved parent prefix.
  return preview.items.map((item) => ({ source: item.path, target: item.destinationPath || item.path }))
    .sort((a, b) => b.source.length - a.source.length);
}

function rewriteObjectPaths(value: unknown, mappings: MigrationPathMapping[]): unknown {
  if (typeof value === "string") {
    for (const mapping of [...mappings].sort((a, b) => b.source.length - a.source.length)) {
      for (const [source, target] of [[mapping.source, mapping.target],
        [mapping.source.split(path.sep).join("/"), mapping.target.split(path.sep).join("/")]]) {
        for (const prefix of ["", "sqlite:///"]) {
          const old = prefix + source;
          const matches = process.platform === "win32"
            ? value.slice(0, old.length).toLowerCase() === old.toLowerCase()
            : value.startsWith(old);
          if (matches && (value.length === old.length || value[old.length] === "/" || value[old.length] === "\\")) {
            return prefix + target + value.slice(old.length);
          }
        }
      }
    }
    return value;
  }
  if (Array.isArray(value)) return value.map((entry) => rewriteObjectPaths(entry, mappings));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, entry]) => [key, rewriteObjectPaths(entry, mappings)]));
  }
  return value;
}

export async function copyStorageMigration(
  preview: MigrationPreview,
  stageRoot: string,
  onProgress: (progress: MigrationProgress) => void,
): Promise<MigrationCopyResult> {
  const records: MigrationCopyResult = { files: [], directories: [], copiedBytes: 0 };
  const totalBytes = preview.requiredBytes;
  let lastProgressAt = 0;
  await fs.promises.mkdir(stageRoot, { recursive: false });
  onProgress({ phase: "copying", message: "正在复制应用数据与托管运行时…", copiedBytes: 0, totalBytes });

  if (!preview.canMigrate) throw new Error("迁移预览未通过，请先解决提示的问题。");
  for (const item of preview.items) {
    if (!item.selected || item.copySourceKey !== item.key || !item.destinationPath) continue;
    const destination = path.join(stageRoot, path.relative(preview.targetRoot, item.destinationPath));
    const exclusions = preview.excludedPaths.filter((excluded) => isPathWithin(item.path, excluded));
    if (item.exists) {
      await copyTree(item.path, destination, records, (copiedBytes) => {
        if (Date.now() - lastProgressAt < 100 && copiedBytes < totalBytes) return;
        lastProgressAt = Date.now();
        onProgress({ phase: "copying", message: `正在复制${item.label}…`, copiedBytes, totalBytes });
      }, exclusions);
    } else {
      await fs.promises.mkdir(destination, { recursive: true });
    }
  }

  onProgress({ phase: "verifying", message: "文件校验完成，正在更新迁移后的目录设置…", copiedBytes: records.copiedBytes, totalBytes });
  const settingsPath = path.join(stageRoot, "data", "settings.json");
  try {
    const raw = await fs.promises.readFile(settingsPath, "utf-8");
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const rewritten = rewriteObjectPaths(parsed, getMigrationPathMappings(preview));
    await fs.promises.writeFile(settingsPath, `${JSON.stringify(rewritten, null, 2)}\n`, "utf-8");
    const record = records.files.find((file) => path.resolve(file.destinationPath) === path.resolve(settingsPath));
    if (record) {
      record.destinationHash = await hashFile(settingsPath);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new Error(`无法更新迁移后的设置文件：${error instanceof Error ? error.message : "设置格式无效。"}`);
    }
  }
  return records;
}

export async function cleanupMigratedSource(records: MigrationCopyResult): Promise<MigrationCleanupResult> {
  let deletedFiles = 0;
  let skippedFiles = 0;
  let removedDirectories = 0;
  const retainedPaths: string[] = [];

  for (const file of [...records.files].reverse()) {
    try {
      const sourceStats = await fs.promises.lstat(file.sourcePath);
      const destinationStats = await fs.promises.lstat(file.destinationPath);
      if (!sourceStats.isFile() || !destinationStats.isFile()) {
        skippedFiles += 1;
        retainedPaths.push(file.sourcePath);
        continue;
      }
      const [currentSourceHash, currentDestinationHash] = await Promise.all([
        hashFile(file.sourcePath),
        hashFile(file.destinationPath),
      ]);
      if (currentSourceHash !== file.sourceHash || currentDestinationHash !== file.destinationHash) {
        skippedFiles += 1;
        retainedPaths.push(file.sourcePath);
        continue;
      }
      await fs.promises.unlink(file.sourcePath);
      deletedFiles += 1;
    } catch {
      skippedFiles += 1;
      retainedPaths.push(file.sourcePath);
    }
  }

  const orderedDirectories = [...records.directories].sort((first, second) => second.sourcePath.length - first.sourcePath.length);
  for (const directory of orderedDirectories) {
    try {
      await fs.promises.rmdir(directory.sourcePath);
      removedDirectories += 1;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "ENOENT" && code !== "ENOTEMPTY" && code !== "EEXIST") {
        retainedPaths.push(directory.sourcePath);
      }
    }
  }

  return { deletedFiles, skippedFiles, removedDirectories, retainedPaths: [...new Set(retainedPaths)] };
}
