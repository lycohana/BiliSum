import type { LibraryFilter } from "./appModel";
import type { VideoAssetSummary, VideoFolder } from "./types";

export type LibraryViewMode = "cover" | "list";
export type LibraryScope = "all" | "unfiled" | "favorite" | string;
export type LibraryFolderTreeNode = VideoFolder & {
  id: string;
  name: string;
  folder: VideoFolder;
  children: LibraryFolderTreeNode[];
};

export const LIBRARY_VIEW_MODE_STORAGE_KEY = "bilisum.libraryViewMode";
export const LIBRARY_ROOT_FOLDER_NODE_ID = "__bilisum_root_folders__";

export function loadLibraryViewMode(storage: Pick<Storage, "getItem"> | undefined = globalThis.window?.localStorage): LibraryViewMode {
  try {
    return storage?.getItem(LIBRARY_VIEW_MODE_STORAGE_KEY) === "list" ? "list" : "cover";
  } catch {
    return "cover";
  }
}

export function saveLibraryViewMode(mode: LibraryViewMode, storage: Pick<Storage, "setItem"> | undefined = globalThis.window?.localStorage) {
  try {
    storage?.setItem(LIBRARY_VIEW_MODE_STORAGE_KEY, mode);
  } catch {
    // Ignore unavailable localStorage in tests or hardened browser contexts.
  }
}

export function canReorderLibraryView(query: string, activeFilter: LibraryFilter) {
  return !query.trim() && activeFilter === "all";
}

export function sortFolders(folders: VideoFolder[]) {
  return [...folders].sort((left, right) => {
    if ((left.parent_id || "") !== (right.parent_id || "")) {
      return (left.parent_id || "").localeCompare(right.parent_id || "");
    }
    return left.position - right.position || left.name.localeCompare(right.name);
  });
}

export function buildFolderTree(folders: VideoFolder[]): LibraryFolderTreeNode[] {
  const nodes = new Map<string, LibraryFolderTreeNode>();
  for (const folder of folders) {
    nodes.set(folder.folder_id, {
      ...folder,
      id: folder.folder_id,
      name: folder.name,
      folder,
      children: [],
    });
  }

  const roots: LibraryFolderTreeNode[] = [];
  for (const folder of sortFolders(folders)) {
    const node = nodes.get(folder.folder_id);
    if (!node) continue;
    const parent = folder.parent_id ? nodes.get(folder.parent_id) : null;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

export function isFolderDescendantOf(folders: VideoFolder[], possibleDescendantId: string | null, folderId: string) {
  let currentId = possibleDescendantId;
  const visited = new Set<string>();
  while (currentId) {
    if (currentId === folderId) return true;
    if (visited.has(currentId)) return false;
    visited.add(currentId);
    currentId = folders.find((folder) => folder.folder_id === currentId)?.parent_id ?? null;
  }
  return false;
}

export function countDirectVideosByFolder(videos: VideoAssetSummary[]) {
  const counts = new Map<string, number>();
  for (const video of videos) {
    for (const folderId of getVideoFolderIds(video)) {
      counts.set(folderId, (counts.get(folderId) || 0) + 1);
    }
  }
  return counts;
}

export function getFolderAncestorIds(folders: VideoFolder[], folderId: string | null | undefined) {
  const ancestors: string[] = [];
  let currentId = folderId ?? null;
  const visited = new Set<string>();
  while (currentId) {
    if (visited.has(currentId)) break;
    visited.add(currentId);
    const folder = folders.find((item) => item.folder_id === currentId);
    if (!folder?.parent_id) break;
    ancestors.push(folder.parent_id);
    currentId = folder.parent_id;
  }
  return ancestors;
}

export function sortVideosForScope(videos: VideoAssetSummary[], scope: LibraryScope) {
  return [...videos].sort((left, right) => {
    const globalPinnedDelta = Number(right.global_pinned) - Number(left.global_pinned);
    if (globalPinnedDelta) return globalPinnedDelta;
    if (scope !== "all") {
      const folderPinnedDelta = Number(right.folder_pinned) - Number(left.folder_pinned);
      if (folderPinnedDelta) return folderPinnedDelta;
      return left.folder_order - right.folder_order || newestFirst(left, right);
    }
    return left.global_order - right.global_order || newestFirst(left, right);
  });
}

export function filterVideosByScope(videos: VideoAssetSummary[], scope: LibraryScope) {
  if (scope === "all") return videos;
  if (scope === "unfiled") return videos.filter((video) => getVideoFolderIds(video).length === 0);
  if (scope === "favorite") return videos.filter((video) => video.is_favorite);
  return videos.filter((video) => getVideoFolderIds(video).includes(scope));
}

export function getVideoFolderIds(video: VideoAssetSummary) {
  if (Array.isArray(video.folder_ids)) return video.folder_ids.filter(Boolean);
  return video.folder_id ? [video.folder_id] : [];
}

export function getFolderAppendPosition(folders: VideoFolder[], parentId: string | null, draggedFolderId?: string | null) {
  const siblings = folders.filter((folder) => (folder.parent_id ?? null) === parentId && folder.folder_id !== draggedFolderId);
  const maxPosition = siblings.reduce((max, folder) => Math.max(max, folder.position), 0);
  return maxPosition + 1000;
}

export function getFolderInsertPosition(
  folders: VideoFolder[],
  parentId: string | null,
  targetId: string,
  edge: "before" | "after",
  draggedFolderId?: string | null,
) {
  const siblings = folders.filter((folder) => (folder.parent_id ?? null) === parentId && folder.folder_id !== draggedFolderId);
  const targetIndex = siblings.findIndex((folder) => folder.folder_id === targetId);
  if (targetIndex < 0) return getFolderAppendPosition(folders, parentId, draggedFolderId);
  const target = siblings[targetIndex];
  const neighbor = siblings[edge === "before" ? targetIndex - 1 : targetIndex + 1];
  if (!neighbor) return edge === "before" ? target.position - 1000 : target.position + 1000;
  return (target.position + neighbor.position) / 2;
}

export function getFolderMovePositionFromIndex(
  folders: VideoFolder[],
  parentId: string | null,
  index: number,
  draggedFolderId?: string | null,
) {
  const siblings = folders
    .filter((folder) => (folder.parent_id ?? null) === parentId && folder.folder_id !== draggedFolderId)
    .sort((left, right) => left.position - right.position || left.name.localeCompare(right.name));
  const before = siblings[index - 1];
  const after = siblings[index];
  if (before && after) return (before.position + after.position) / 2;
  if (before) return before.position + 1000;
  if (after) return after.position - 1000;
  return 1000;
}

export function getSiblingFolderMovePayload(
  folders: VideoFolder[],
  folderId: string,
  direction: "up" | "down",
): { parent_id: string | null; position: number } | null {
  const folder = folders.find((item) => item.folder_id === folderId);
  if (!folder) return null;
  const parentId = folder.parent_id ?? null;
  const siblings = folders
    .filter((item) => (item.parent_id ?? null) === parentId)
    .sort((left, right) => left.position - right.position || left.name.localeCompare(right.name));
  const currentIndex = siblings.findIndex((item) => item.folder_id === folderId);
  const nextIndex = direction === "up" ? currentIndex - 1 : currentIndex + 1;
  if (currentIndex < 0 || nextIndex < 0 || nextIndex >= siblings.length) return null;
  return {
    parent_id: parentId,
    position: getFolderMovePositionFromIndex(folders, parentId, nextIndex, folderId),
  };
}

export function resolveFolderMoveParentId(parentId: string | null) {
  return parentId === LIBRARY_ROOT_FOLDER_NODE_ID ? null : parentId;
}

function newestFirst(left: VideoAssetSummary, right: VideoAssetSummary) {
  return new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime();
}
