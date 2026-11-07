import type { ReactNode } from "react";

import { getVideoFolderIds, type LibraryFolderTreeNode } from "../libraryModel";
import type { VideoAssetSummary, VideoFolder } from "../types";
import {
  ArrowRightIcon,
  ExternalLinkIcon,
  FolderIcon,
  PinIcon,
  StarIcon,
  TrashIcon,
} from "./AppIcons";

export type VideoContextMenuFolder = {
  folder: VideoFolder;
  depth: number;
};

export function flattenVideoContextMenuFolders(nodes: LibraryFolderTreeNode[], depth = 0): VideoContextMenuFolder[] {
  return nodes.flatMap((node) => [
    { folder: node.folder, depth },
    ...flattenVideoContextMenuFolders(node.children, depth + 1),
  ]);
}

export function VideoContextMenu({
  video,
  x,
  y,
  submenuSide,
  folders,
  headingMeta,
  canPinInFolder,
  busy,
  deleteConfirmOpen,
  extraActions,
  showLibraryActions = true,
  deleteLabel = "删除视频",
  deleteConfirmText = "删除后会移除视频文件与任务记录。",
  deleteConfirmActionLabel = "删除",
  onOpenDetail,
  onRevealFolder,
  onSetFolders,
  onToggleFavorite,
  onToggleGlobalPin,
  onToggleFolderPin,
  onRequestDelete,
  onCancelDelete,
  onDelete,
}: {
  video: VideoAssetSummary;
  x: number;
  y: number;
  submenuSide: "left" | "right";
  folders: VideoContextMenuFolder[];
  headingMeta: string;
  canPinInFolder: boolean;
  busy: boolean;
  deleteConfirmOpen: boolean;
  extraActions?: ReactNode;
  showLibraryActions?: boolean;
  deleteLabel?: string;
  deleteConfirmText?: string;
  deleteConfirmActionLabel?: string;
  onOpenDetail(): void;
  onRevealFolder(): void;
  onSetFolders(folderIds: string[]): void;
  onToggleFavorite(): void;
  onToggleGlobalPin(): void;
  onToggleFolderPin(): void;
  onRequestDelete(): void;
  onCancelDelete(): void;
  onDelete(): void;
}) {
  const selectedFolderIds = getVideoFolderIds(video);
  return (
    <div
      className={`library-video-context-menu ${deleteConfirmOpen ? "is-expanded" : ""} ${submenuSide === "left" ? "is-submenu-left" : ""}`}
      style={{ left: x, top: y }}
      role="menu"
      aria-label={`${video.title} 的操作菜单`}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <div className="library-video-menu-heading">
        <strong>{video.title}</strong>
        <span>{headingMeta}</span>
      </div>
      <button type="button" role="menuitem" disabled={busy} onClick={onOpenDetail}>
        <ExternalLinkIcon />
        <span>打开详情</span>
      </button>
      {showLibraryActions ? <>
        <button type="button" role="menuitem" disabled={busy} onClick={onRevealFolder}>
          <ArrowRightIcon />
          <span>跳到所在分组</span>
        </button>
        <div className="library-video-menu-group">
          <button className="library-video-submenu-trigger" type="button" role="menuitem" disabled={busy}>
            <FolderIcon />
            <span>设置分组</span>
          </button>
          <div className="library-video-submenu" role="menu" aria-label="设置分组">
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={selectedFolderIds.length === 0}
              disabled={busy || selectedFolderIds.length === 0}
              className={selectedFolderIds.length === 0 ? "is-current" : ""}
              onClick={() => onSetFolders([])}
            >
              <span className="library-video-menu-check" aria-hidden="true">{selectedFolderIds.length === 0 ? "✓" : ""}</span>
              <span>未归档</span>
            </button>
            <div className="library-video-folder-options">
              {folders.map(({ folder, depth }) => {
                const selected = selectedFolderIds.includes(folder.folder_id);
                const nextFolderIds = selected
                  ? selectedFolderIds.filter((folderId) => folderId !== folder.folder_id)
                  : [...selectedFolderIds, folder.folder_id];
                return (
                  <button
                    key={folder.folder_id}
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={selected}
                    disabled={busy}
                    className={selected ? "is-current" : ""}
                    style={{ paddingLeft: 10 + depth * 14 }}
                    onClick={() => onSetFolders(nextFolderIds)}
                  >
                    <span className="library-video-menu-check" aria-hidden="true">{selected ? "✓" : ""}</span>
                    <span>{folder.name}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </> : null}
      {extraActions}
      {showLibraryActions ? <>
        <div className="library-video-menu-divider" />
        <button type="button" role="menuitem" disabled={busy} onClick={onToggleFavorite}>
          <StarIcon />
          <span>{video.is_favorite ? "取消收藏" : "收藏"}</span>
        </button>
        <button type="button" role="menuitem" disabled={busy} onClick={onToggleGlobalPin}>
          <PinIcon />
          <span>{video.global_pinned ? "取消全局置顶" : "全局置顶"}</span>
        </button>
        <button type="button" role="menuitem" disabled={busy || !canPinInFolder} onClick={onToggleFolderPin}>
          <PinIcon />
          <span>{video.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"}</span>
        </button>
        {deleteConfirmOpen ? (
          <div className="library-video-delete-confirm">
            <p>{deleteConfirmText}</p>
            <div>
              <button type="button" disabled={busy} onClick={onCancelDelete}>取消</button>
              <button className="is-danger" type="button" disabled={busy} onClick={onDelete}>{deleteConfirmActionLabel}</button>
            </div>
          </div>
        ) : (
          <button className="is-danger" type="button" role="menuitem" disabled={busy} onClick={onRequestDelete}>
            <TrashIcon />
            <span>{deleteLabel}</span>
          </button>
        )}
      </> : null}
    </div>
  );
}

export function resolveVideoContextMenuPosition(clientX: number, clientY: number, menuHeight = 560) {
  const menuWidth = 292;
  const submenuWidth = 248;
  const margin = 12;
  const viewportWidth = globalThis.window?.innerWidth || 1280;
  const viewportHeight = globalThis.window?.innerHeight || 720;
  const submenuSide: "left" | "right" = clientX + menuWidth + submenuWidth + margin <= viewportWidth ? "right" : "left";
  return {
    x: Math.max(margin, Math.min(clientX, viewportWidth - menuWidth - margin)),
    y: Math.max(margin, Math.min(clientY, viewportHeight - menuHeight - margin)),
    submenuSide,
  };
}
