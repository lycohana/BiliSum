import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  closestCenter,
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  rectSortingStrategy,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Tree, type NodeRendererProps } from "react-arborist";

import type { LibraryFilter, Snapshot } from "../appModel";
import { platformLabel, taskStatusClass } from "../appModel";
import {
  ArrowRightIcon,
  ArrowUpToLineIcon,
  CornerUpLeftIcon,
  ExternalLinkIcon,
  FolderIcon,
  GripVerticalIcon,
  LibraryIcon,
  PinIcon,
  SearchIcon,
  StarIcon,
  TrashIcon,
} from "../components/AppIcons";
import { FloatingNoticeStack } from "../components/FloatingNoticeStack";
import { VideoCard } from "../components/VideoCard";
import {
  buildFolderTree,
  canReorderLibraryView,
  countDirectVideosByFolder,
  filterVideosByScope,
  getFolderAncestorIds,
  getFolderMovePositionFromIndex,
  getSiblingFolderMovePayload,
  getVideoFolderIds,
  isFolderDescendantOf,
  loadLibraryViewMode,
  saveLibraryViewMode,
  sortFolders,
  sortVideosForScope,
  type LibraryFolderTreeNode,
  type LibraryScope,
  type LibraryViewMode,
} from "../libraryModel";
import type { VideoAssetDetail, VideoAssetSummary, VideoFolder } from "../types";
import { formatDateTime, formatDuration, taskStatusLabel } from "../utils";

const COVER_ROWS_PER_PAGE = 5;
const LIST_VIDEOS_PER_PAGE = 15;
const UNFILED_DROP_ID = "folder-drop:unfiled";

function getFolderDropId(folderId: string | null) {
  return folderId ? `folder-drop:${folderId}` : UNFILED_DROP_ID;
}

function buildPaginationItems(currentPage: number, totalPages: number): Array<number | "ellipsis"> {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_item, index) => index + 1);
  if (currentPage <= 3) return [1, 2, 3, 4, "ellipsis", totalPages];
  if (currentPage >= totalPages - 2) return [1, "ellipsis", totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
  return [1, "ellipsis", currentPage - 1, currentPage, currentPage + 1, "ellipsis", totalPages];
}

type LibraryPageProps = {
  snapshot: Snapshot;
  filteredVideos: VideoAssetSummary[];
  libraryCounts: { total: number; completed: number; running: number; withResult: number; favorite: number };
  latestVideo: VideoAssetSummary | null;
  query: string;
  setQuery(value: string): void;
  activeFilter: LibraryFilter;
  setLibraryFilter(value: LibraryFilter): void;
  serviceOnline: boolean;
  runtimeDeviceLabel: string;
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
  onCreateFolder(name: string, parentId?: string | null): Promise<VideoFolder>;
  onUpdateFolder(folderId: string, payload: { name?: string | null; parent_id?: string | null; position?: number | null }): Promise<VideoFolder>;
  onDeleteFolder(folderId: string): Promise<void>;
  onDeleteVideo(videoId: string): Promise<void>;
  onMoveVideo(videoId: string, folderId?: string | null, folderIds?: string[]): Promise<VideoAssetDetail>;
  onSetVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }): Promise<VideoAssetDetail>;
  onReorderVideos(videoIds: string[], folderId?: string | null): Promise<void>;
  onUpdateLibraryPreferences(newVideoPosition: "front" | "back"): Promise<void>;
};

type OptimisticVideoOrder = {
  key: string;
  ids: string[];
} | null;

type VideoActionMenu = {
  videoId: string;
  x: number;
  y: number;
  submenuSide: "left" | "right";
} | null;

type FolderMenuItem = {
  folder: VideoFolder;
  depth: number;
};

export function LibraryPage({
  snapshot,
  filteredVideos,
  libraryCounts,
  latestVideo,
  query,
  setQuery,
  activeFilter,
  setLibraryFilter,
  serviceOnline,
  runtimeDeviceLabel,
  onToggleFavorite,
  onCreateFolder,
  onUpdateFolder,
  onDeleteFolder,
  onDeleteVideo,
  onMoveVideo,
  onSetVideoPin,
  onReorderVideos,
  onUpdateLibraryPreferences,
}: LibraryPageProps) {
  const navigate = useNavigate();
  const [currentPage, setCurrentPage] = useState(1);
  const [activeScope, setActiveScope] = useState<LibraryScope>("all");
  const [viewMode, setViewMode] = useState<LibraryViewMode>(() => loadLibraryViewMode());
  const [activeVideoDragId, setActiveVideoDragId] = useState<string | null>(null);
  const [optimisticVideoOrder, setOptimisticVideoOrder] = useState<OptimisticVideoOrder>(null);
  const [folderDraftParentId, setFolderDraftParentId] = useState<string | null | undefined>(undefined);
  const [folderDraftName, setFolderDraftName] = useState("");
  const [folderDraftBusy, setFolderDraftBusy] = useState(false);
  const [collapsedFolderIds, setCollapsedFolderIds] = useState<Set<string>>(() => new Set());
  const [folderActionId, setFolderActionId] = useState<string | null>(null);
  const [folderRenameId, setFolderRenameId] = useState<string | null>(null);
  const [folderRenameName, setFolderRenameName] = useState("");
  const [folderDeleteConfirmId, setFolderDeleteConfirmId] = useState<string | null>(null);
  const [videoActionMenu, setVideoActionMenu] = useState<VideoActionMenu>(null);
  const [videoDeleteConfirmId, setVideoDeleteConfirmId] = useState<string | null>(null);
  const [videoActionBusy, setVideoActionBusy] = useState(false);
  const [libraryNotice, setLibraryNotice] = useState<{ message: string; tone: "info" | "success" | "error"; version: number }>({ message: "", tone: "info", version: 0 });
  const [newVideoSettingsOpen, setNewVideoSettingsOpen] = useState(false);
  const videoMoveBusyRef = useRef(false);
  const coverGridRef = useRef<HTMLDivElement | null>(null);
  const [coverColumnCount, setCoverColumnCount] = useState(3);
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const folders = useMemo(() => sortFolders(snapshot.folders), [snapshot.folders]);
  const folderMap = useMemo(() => new Map(folders.map((folder) => [folder.folder_id, folder])), [folders]);
  const folderTreeData = useMemo(() => buildFolderTree(folders), [folders]);
  const folderMenuItems = useMemo(() => flattenFolderMenuItems(folderTreeData), [folderTreeData]);
  const directVideoCounts = useMemo(() => countDirectVideosByFolder(snapshot.videos), [snapshot.videos]);
  const initialOpenState = useMemo(() => Object.fromEntries(
    folders
      .map((folder) => [folder.folder_id, !collapsedFolderIds.has(folder.folder_id)]),
  ), [collapsedFolderIds, folders]);
  const visibleOrderKey = `${String(activeScope)}:${activeFilter}:${query}`;
  const rawVisibleBaseVideos = useMemo(() => {
    const scoped = filterVideosByScope(filteredVideos, activeScope);
    return sortVideosForScope(scoped, activeScope);
  }, [activeScope, filteredVideos]);
  const visibleBaseVideos = useMemo(() => {
    if (!optimisticVideoOrder || optimisticVideoOrder.key !== visibleOrderKey) return rawVisibleBaseVideos;
    const videoById = new Map(rawVisibleBaseVideos.map((video) => [video.video_id, video]));
    const hasSameVideos = optimisticVideoOrder.ids.length === rawVisibleBaseVideos.length
      && optimisticVideoOrder.ids.every((videoId) => videoById.has(videoId));
    if (!hasSameVideos) return rawVisibleBaseVideos;
    return optimisticVideoOrder.ids.map((videoId) => videoById.get(videoId)).filter((video): video is VideoAssetSummary => Boolean(video));
  }, [optimisticVideoOrder, rawVisibleBaseVideos, visibleOrderKey]);
  const activeFolder = typeof activeScope === "string" && !["all", "unfiled", "favorite"].includes(activeScope)
    ? folderMap.get(activeScope) || null
    : null;
  const isFolderScope = Boolean(activeFolder);
  const canReorder = canReorderLibraryView(query, activeFilter) && activeScope !== "favorite";
  const videosPerPage = viewMode === "cover" ? Math.max(1, coverColumnCount) * COVER_ROWS_PER_PAGE : LIST_VIDEOS_PER_PAGE;
  const totalPages = Math.max(1, Math.ceil(visibleBaseVideos.length / videosPerPage));
  const safeCurrentPage = Math.min(currentPage, totalPages);
  const pageStartIndex = (safeCurrentPage - 1) * videosPerPage;
  const pagedVideos = visibleBaseVideos.slice(pageStartIndex, pageStartIndex + videosPerPage);
  const pagedVideoIds = useMemo(() => pagedVideos.map((video) => video.video_id), [pagedVideos]);
  const paginationItems = buildPaginationItems(safeCurrentPage, totalPages);
  const folderTreeHeight = folders.length ? folders.length * 42 + 12 : 0;
  const filters: Array<{ id: LibraryFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: libraryCounts.total },
    { id: "favorite", label: "收藏", count: libraryCounts.favorite },
    { id: "completed", label: "已完成", count: libraryCounts.completed },
    { id: "running", label: "处理中", count: libraryCounts.running },
    ...(libraryCounts.withResult !== libraryCounts.completed ? [{ id: "with-result" as const, label: "有结果", count: libraryCounts.withResult }] : []),
  ];
  const activeFilterLabel = filters.find((filter) => filter.id === activeFilter)?.label || "全部";
  const summaryText = latestVideo ? `最近更新：${latestVideo.title}` : "输入链接后，视频会自动进入这里统一管理。";
  const draftParentFolder = folderDraftParentId ? folderMap.get(folderDraftParentId) || null : null;
  const scopeTitle = activeScope === "all" ? "全部视频" : activeScope === "unfiled" ? "未归档" : activeScope === "favorite" ? "收藏" : activeFolder?.name || "文件夹";
  const activeDraggedVideo = activeVideoDragId ? snapshot.videos.find((video) => video.video_id === activeVideoDragId) || null : null;
  const videoMenuVideo = videoActionMenu ? snapshot.videos.find((video) => video.video_id === videoActionMenu.videoId) || null : null;

  function getFolderLabel(video: VideoAssetSummary) {
    const names = getVideoFolderIds(video).map((folderId) => folderMap.get(folderId)?.name).filter((name): name is string => Boolean(name));
    if (!names.length) return "未归档";
    if (names.length === 1) return names[0];
    return `${names[0]} +${names.length - 1}`;
  }

  useEffect(() => {
    setCurrentPage(1);
    setOptimisticVideoOrder(null);
  }, [activeFilter, query, activeScope]);

  useEffect(() => {
    if (currentPage > totalPages) setCurrentPage(totalPages);
  }, [currentPage, totalPages]);

  useEffect(() => {
    saveLibraryViewMode(viewMode);
  }, [viewMode]);

  useEffect(() => {
    if (viewMode !== "cover") return;
    const grid = coverGridRef.current;
    if (!grid) return;

    const gridElement = grid;

    function updateCoverColumnCount() {
      const columns = window.getComputedStyle(gridElement).gridTemplateColumns.split(" ").filter(Boolean).length;
      setCoverColumnCount(Math.max(1, columns || 1));
    }

    updateCoverColumnCount();
    const observer = new ResizeObserver(updateCoverColumnCount);
    observer.observe(gridElement);
    window.addEventListener("resize", updateCoverColumnCount);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateCoverColumnCount);
    };
  }, [viewMode]);

  useEffect(() => {
    if (!newVideoSettingsOpen) return;

    function closeNewVideoSettings() {
      setNewVideoSettingsOpen(false);
    }

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setNewVideoSettingsOpen(false);
      }
    }

    document.addEventListener("pointerdown", closeNewVideoSettings);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeNewVideoSettings);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [newVideoSettingsOpen]);

  useEffect(() => {
    if (!folderActionId) return;

    function closeFolderActions() {
      setFolderActionId(null);
    }

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setFolderActionId(null);
      }
    }

    document.addEventListener("pointerdown", closeFolderActions);
    document.addEventListener("scroll", closeFolderActions, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeFolderActions);
      document.removeEventListener("scroll", closeFolderActions, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [folderActionId]);

  useEffect(() => {
    if (!videoActionMenu) return;

    function closeVideoActions(event?: Event) {
      const target = event?.target as HTMLElement | null;
      if (target?.closest(".library-video-context-menu")) return;
      setVideoActionMenu(null);
      setVideoDeleteConfirmId(null);
      setVideoActionBusy(false);
    }

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeVideoActions();
      }
    }

    document.addEventListener("pointerdown", closeVideoActions);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeVideoActions);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [videoActionMenu]);

  function beginCreateFolder(parentId?: string | null) {
    setFolderDraftParentId(parentId ?? null);
    setFolderDraftName("");
    setFolderActionId(null);
    setFolderRenameId(null);
    setFolderDeleteConfirmId(null);
  }

  async function submitFolderDraft() {
    const name = folderDraftName.trim();
    if (!name) return;
    setFolderDraftBusy(true);
    try {
      const folder = await onCreateFolder(name, folderDraftParentId ?? null);
      setActiveScope(folder.folder_id);
      setFolderDraftParentId(undefined);
      setFolderDraftName("");
    } catch (error) {
      showLibraryNotice(error instanceof Error ? error.message : "新建文件夹失败", "error");
    } finally {
      setFolderDraftBusy(false);
    }
  }

  function beginRenameFolder(folder: VideoFolder) {
    setFolderRenameId(folder.folder_id);
    setFolderRenameName(folder.name);
    setFolderDeleteConfirmId(null);
  }

  async function submitRenameFolder(folder: VideoFolder) {
    const name = folderRenameName.trim();
    if (!name || name === folder.name) return;
    try {
      await onUpdateFolder(folder.folder_id, { name });
      setFolderActionId(null);
      setFolderRenameId(null);
      setFolderRenameName("");
    } catch (error) {
      showLibraryNotice(error instanceof Error ? error.message : "重命名失败", "error");
    }
  }

  async function deleteFolder(folder: VideoFolder) {
    try {
      await onDeleteFolder(folder.folder_id);
      setFolderActionId(null);
      setFolderDeleteConfirmId(null);
      setFolderRenameId(null);
      setActiveScope("unfiled");
    } catch (error) {
      showLibraryNotice(error instanceof Error ? error.message : "删除文件夹失败", "error");
    }
  }

  function showLibraryNotice(message: string, tone: "info" | "success" | "error" = "info") {
    setLibraryNotice((current) => ({ message, tone, version: current.version + 1 }));
  }

  function openVideoActions(event: MouseEvent, videoId: string) {
    event.preventDefault();
    event.stopPropagation();
    setVideoDeleteConfirmId(null);
    setVideoActionBusy(false);
    setVideoActionMenu({ videoId, ...resolveContextMenuPosition(event.clientX, event.clientY) });
  }

  async function runVideoAction(action: () => Promise<void>, successMessage?: string) {
    if (videoActionBusy) return;
    setVideoActionBusy(true);
    try {
      await action();
      setVideoActionMenu(null);
      setVideoDeleteConfirmId(null);
      if (successMessage) showLibraryNotice(successMessage, "success");
    } catch (error) {
      showLibraryNotice(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setVideoActionBusy(false);
    }
  }

  function closeVideoActions() {
    setVideoActionMenu(null);
    setVideoDeleteConfirmId(null);
    setVideoActionBusy(false);
  }

  function handleVideoDragStart(event: DragStartEvent) {
    setActiveVideoDragId(String(event.active.id));
  }

  async function handleVideoDragEnd(event: DragEndEvent) {
    const sourceVideoId = String(event.active.id);
    const over = event.over;
    setActiveVideoDragId(null);
    if (!over || videoMoveBusyRef.current) return;

    const overData = over.data.current;
    if (overData?.type === "folder-drop") {
      const folderId = overData.folderId as string | null;
      videoMoveBusyRef.current = true;
      try {
        await onMoveVideo(sourceVideoId, folderId);
      } finally {
        videoMoveBusyRef.current = false;
      }
      return;
    }

    const targetVideoId = String(over.id);
    if (!canReorder || sourceVideoId === targetVideoId || !visibleBaseVideos.some((video) => video.video_id === targetVideoId)) return;
    const ids = visibleBaseVideos.map((video) => video.video_id);
    const fromIndex = ids.indexOf(sourceVideoId);
    const toIndex = ids.indexOf(targetVideoId);
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return;
    const nextIds = arrayMove(ids, fromIndex, toIndex);
    setOptimisticVideoOrder({ key: visibleOrderKey, ids: nextIds });
    try {
      await onReorderVideos(nextIds, activeScope === "all" ? "__global__" : activeScope === "unfiled" ? null : activeScope);
    } catch (error) {
      setOptimisticVideoOrder(null);
      showLibraryNotice(error instanceof Error ? error.message : "保存顺序失败", "error");
    }
  }

  function handleVideoDragCancel() {
    setActiveVideoDragId(null);
  }

  async function promoteFolder(folder: VideoFolder, mode: "parent" | "root") {
    const currentParent = folder.parent_id ? folderMap.get(folder.parent_id) || null : null;
    const nextParentId = mode === "root" ? null : currentParent?.parent_id ?? null;
    if ((folder.parent_id ?? null) === nextParentId) return;
    await onUpdateFolder(folder.folder_id, {
      parent_id: nextParentId,
      position: getFolderMovePositionFromIndex(folders, nextParentId, 0, folder.folder_id),
    });
    setFolderActionId(null);
  }

  async function moveFolderSibling(folder: VideoFolder, direction: "up" | "down") {
    const payload = getSiblingFolderMovePayload(folders, folder.folder_id, direction);
    if (!payload) return;
    await onUpdateFolder(folder.folder_id, payload);
    setFolderActionId(null);
  }

  async function handleFolderTreeMove({ dragIds, parentId, index }: { dragIds: string[]; parentId: string | null; index: number }) {
    const folderId = dragIds[0];
    if (!folderId) return;
    const nextParentId = parentId ?? null;
    if (nextParentId === folderId) return;
    if (isFolderDescendantOf(folders, nextParentId, folderId)) return;
    await onUpdateFolder(folderId, {
      parent_id: nextParentId,
      position: getFolderMovePositionFromIndex(folders, nextParentId, index, folderId),
    });
  }

  return (
    <section className="library-page">
      <section className="library-hero">
        <div className="library-hero-copy">
          <span className="library-kicker">Library</span>
          <h2>视频库</h2>
          <p>{summaryText}</p>
        </div>
        <div className="library-hero-status">
          <span className={`helper-chip ${serviceOnline ? "status-success" : "status-pending"}`}>{serviceOnline ? "服务在线" : "服务离线"}</span>
          <span className="helper-chip">{runtimeDeviceLabel}</span>
          <span className="helper-chip">筛选：{activeFilterLabel}</span>
        </div>
      </section>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleVideoDragStart}
        onDragEnd={(event) => void handleVideoDragEnd(event)}
        onDragCancel={handleVideoDragCancel}
      >
      <section className="library-workbench">
        <aside className={`library-sidebar-panel ${activeVideoDragId ? "is-video-dragging" : ""}`}>
          <div className="library-sidebar-head">
            <strong>文件夹</strong>
            <button className="library-icon-button" type="button" title="新建文件夹" onClick={() => beginCreateFolder(null)}>+</button>
          </div>
          <div className="library-folder-list">
            <ScopeButton icon={<LibraryIcon />} active={activeScope === "all"} label="全部视频" count={snapshot.videos.length} onClick={() => setActiveScope("all")} />
            <ScopeButton
              icon={<FolderIcon />}
              active={activeScope === "unfiled"}
              label="未归档"
              count={snapshot.videos.filter((video) => getVideoFolderIds(video).length === 0).length}
              onClick={() => setActiveScope("unfiled")}
              droppableId={UNFILED_DROP_ID}
              folderId={null}
            />
            <ScopeButton icon={<StarIcon />} active={activeScope === "favorite"} label="收藏" count={libraryCounts.favorite} onClick={() => setActiveScope("favorite")} />
            {folderDraftParentId === null ? (
              <FolderDraftInput
                value={folderDraftName}
                busy={folderDraftBusy}
                onChange={setFolderDraftName}
                onSubmit={submitFolderDraft}
                onCancel={() => setFolderDraftParentId(undefined)}
              />
            ) : null}
            <div className="library-folder-tree-shell">
              <Tree<LibraryFolderTreeNode>
                key={folders.map((folder) => `${folder.folder_id}:${folder.parent_id ?? "root"}:${collapsedFolderIds.has(folder.folder_id) ? "closed" : "open"}`).join("|")}
                data={folderTreeData}
                className="library-arborist-tree"
                rowHeight={42}
                height={folderTreeHeight}
                width="100%"
                indent={18}
                overscanCount={4}
                paddingTop={4}
                paddingBottom={8}
                selection={typeof activeScope === "string" && folderMap.has(activeScope) ? activeScope : undefined}
                initialOpenState={initialOpenState}
                openByDefault={false}
                onMove={(args) => void handleFolderTreeMove(args)}
                onToggle={(id) => {
                  setCollapsedFolderIds((current) => {
                    const next = new Set(current);
                    if (next.has(id)) next.delete(id);
                    else next.add(id);
                    return next;
                  });
                }}
                onActivate={(node) => setActiveScope(node.data.folder.folder_id)}
                disableMultiSelection
                disableEdit
                disableDrop={({ parentNode, dragNodes }) => {
                  const folderId = dragNodes[0]?.data.folder.folder_id;
                  const nextParentId = parentNode?.isRoot ? null : parentNode?.data.folder.folder_id ?? null;
                  return !folderId || nextParentId === folderId || isFolderDescendantOf(folders, nextParentId, folderId);
                }}
              >
                {(props) => (
                  <FolderTreeNode
                    {...props}
                    activeScope={activeScope}
                    actionOpen={folderActionId === props.node.data.id}
                    videoCount={directVideoCounts.get(props.node.data.id) || 0}
                    onActivate={(folderId) => setActiveScope(folderId)}
                    collapsed={collapsedFolderIds.has(props.node.data.id)}
                    onBeginCreate={beginCreateFolder}
                    renameOpen={folderRenameId === props.node.data.id}
                    renameValue={folderRenameName}
                    onRenameChange={setFolderRenameName}
                    onBeginRename={beginRenameFolder}
                    onSubmitRename={submitRenameFolder}
                    onCancelRename={() => {
                      setFolderRenameId(null);
                      setFolderRenameName("");
                    }}
                    onDelete={deleteFolder}
                    deleteConfirmOpen={folderDeleteConfirmId === props.node.data.id}
                    onRequestDelete={(folderId) => {
                      setFolderDeleteConfirmId(folderId);
                      setFolderRenameId(null);
                    }}
                    onCancelDelete={() => setFolderDeleteConfirmId(null)}
                    onPromote={promoteFolder}
                    onMoveSibling={moveFolderSibling}
                    onToggleActions={(folderId) => {
                      setFolderActionId((current) => current === folderId ? null : folderId);
                      setFolderRenameId(null);
                      setFolderDeleteConfirmId(null);
                    }}
                  />
                )}
              </Tree>
            </div>
            {folderDraftParentId && draftParentFolder ? (
              <div className="library-folder-context-draft">
                <span className="library-folder-draft-label">在“{draftParentFolder.name}”中新建</span>
                <FolderDraftInput
                  value={folderDraftName}
                  busy={folderDraftBusy}
                  onChange={setFolderDraftName}
                  onSubmit={submitFolderDraft}
                  onCancel={() => setFolderDraftParentId(undefined)}
                />
              </div>
            ) : null}
          </div>
        </aside>

        <section className="library-collection">
          <div className="library-toolbar">
            <div className="library-toolbar-copy">
              <span className="library-breadcrumb">视频库 / {scopeTitle}</span>
              <h3>{scopeTitle}</h3>
              <p>共 {visibleBaseVideos.length} 个视频，当前第 {safeCurrentPage} / {totalPages} 页</p>
            </div>
            <label className="search-field library-search-field">
              <span className="search-icon" aria-hidden="true"><SearchIcon /></span>
              <input className="input-field input-field-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或来源链接..." />
            </label>
          </div>

          <div className="library-control-row">
            <div className="filter-pill-row library-filter-row">
              {filters.map((filter) => (
                <button key={filter.id} className={`filter-pill ${activeFilter === filter.id ? "active" : ""}`} type="button" onClick={() => setLibraryFilter(filter.id)}>
                  <span>{filter.label}</span>
                  <strong>{filter.count}</strong>
                </button>
              ))}
            </div>
            <div className="library-view-controls">
              <button className={`library-segment ${viewMode === "cover" ? "is-active" : ""}`} type="button" onClick={() => setViewMode("cover")}>封面</button>
              <button className={`library-segment ${viewMode === "list" ? "is-active" : ""}`} type="button" onClick={() => setViewMode("list")}>列表</button>
              <div className="library-setting-popover-wrap" onPointerDown={(event) => event.stopPropagation()}>
                <button
                  className={`library-segment ${newVideoSettingsOpen ? "is-active" : ""}`}
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={newVideoSettingsOpen}
                  onClick={() => setNewVideoSettingsOpen((open) => !open)}
                >
                  设置
                </button>
                <div className={`library-setting-popover ${newVideoSettingsOpen ? "is-open" : ""}`} role="menu" aria-label="新视频位置设置">
                  <span className="library-setting-popover-label">新视频加入位置</span>
                  <button
                    className={snapshot.libraryPreferences.new_video_position === "front" ? "is-active" : ""}
                    type="button"
                    role="menuitemradio"
                    aria-checked={snapshot.libraryPreferences.new_video_position === "front"}
                    onClick={() => {
                      setNewVideoSettingsOpen(false);
                      void onUpdateLibraryPreferences("front");
                    }}
                  >
                    <span>置前</span>
                    <strong>放在列表最前</strong>
                  </button>
                  <button
                    className={snapshot.libraryPreferences.new_video_position === "back" ? "is-active" : ""}
                    type="button"
                    role="menuitemradio"
                    aria-checked={snapshot.libraryPreferences.new_video_position === "back"}
                    onClick={() => {
                      setNewVideoSettingsOpen(false);
                      void onUpdateLibraryPreferences("back");
                    }}
                  >
                    <span>置后</span>
                    <strong>放在列表最后</strong>
                  </button>
                </div>
              </div>
            </div>
          </div>

          {!canReorder ? <p className="library-reorder-note">搜索或状态筛选中可拖到文件夹归档，列表内排序暂时锁定。</p> : null}

          {visibleBaseVideos.length ? (
            <>
              {viewMode === "cover" ? (
                <SortableContext items={pagedVideoIds} strategy={rectSortingStrategy}>
                <div ref={coverGridRef} className="video-grid library-sortable-grid">
                  {pagedVideos.map((video) => (
                      <SortableVideoCard
                        key={video.video_id}
                        video={video}
                        folderName={getFolderLabel(video)}
                        canPinInFolder={isFolderScope}
                        onToggleFavorite={onToggleFavorite}
                        onSetVideoPin={onSetVideoPin}
                        onOpenContextMenu={openVideoActions}
                      />
                  ))}
                </div>
                </SortableContext>
              ) : (
                <SortableContext items={pagedVideoIds} strategy={verticalListSortingStrategy}>
                <div className="library-list-view">
                  {pagedVideos.map((video) => (
                      <SortableVideoListItem
                        key={video.video_id}
                        video={video}
                        folderName={getFolderLabel(video)}
                        canPinInFolder={isFolderScope}
                        onToggleFavorite={onToggleFavorite}
                        onSetVideoPin={onSetVideoPin}
                        onOpenContextMenu={openVideoActions}
                      />
                  ))}
                </div>
                </SortableContext>
              )}

              {totalPages > 1 ? (
                <div className="library-pagination" aria-label="视频库分页">
                  <div className="library-pagination-summary">显示第 {pageStartIndex + 1}-{Math.min(pageStartIndex + pagedVideos.length, visibleBaseVideos.length)} 条，共 {visibleBaseVideos.length} 条</div>
                  <div className="library-pagination-actions">
                    <button className="library-pagination-button" type="button" disabled={safeCurrentPage === 1} onClick={() => setCurrentPage((page) => Math.max(1, page - 1))}>上一页</button>
                    {paginationItems.map((item, index) => item === "ellipsis"
                      ? <span key={`ellipsis-${index}`} className="library-pagination-ellipsis">...</span>
                      : (
                        <span key={item} className="library-pagination-slot">
                          <button className={`library-pagination-button ${item === safeCurrentPage ? "is-active" : ""}`} type="button" onClick={() => setCurrentPage(item)} aria-current={item === safeCurrentPage ? "page" : undefined}>{item}</button>
                        </span>
                      ))}
                    <button className="library-pagination-button" type="button" disabled={safeCurrentPage === totalPages} onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))}>下一页</button>
                  </div>
                </div>
              ) : null}
            </>
          ) : (
            <div className="library-empty-state">
              <div className="library-empty-visual" aria-hidden="true"><SearchIcon width={34} height={34} /></div>
              <div className="library-empty-copy">
                <h4>当前范围还没有视频</h4>
                <p>可以调整筛选条件，或者把视频拖到左侧文件夹完成归档。</p>
              </div>
            </div>
          )}
        </section>
      </section>
      <DragOverlay dropAnimation={{ duration: 180, easing: "cubic-bezier(0.22, 1, 0.36, 1)" }}>
        {activeDraggedVideo ? (
          <div className={`library-drag-overlay ${viewMode === "cover" ? "is-cover" : "is-list"}`}>
            {viewMode === "cover" ? (
              <VideoCard
                video={activeDraggedVideo}
                folderName={getFolderLabel(activeDraggedVideo)}
                canPinInFolder={isFolderScope}
                onToggleFavorite={onToggleFavorite}
                onToggleGlobalPin={async (videoId, nextPinned) => {
                  await onSetVideoPin(videoId, { global_pinned: nextPinned });
                }}
                onToggleFolderPin={async (videoId, nextPinned) => {
                  await onSetVideoPin(videoId, { folder_pinned: nextPinned });
                }}
              />
            ) : (
              <VideoListItem
                video={activeDraggedVideo}
                folderName={getFolderLabel(activeDraggedVideo)}
                canPinInFolder={isFolderScope}
                onToggleFavorite={onToggleFavorite}
                onSetVideoPin={onSetVideoPin}
                onOpenContextMenu={openVideoActions}
              />
            )}
          </div>
        ) : null}
      </DragOverlay>
      </DndContext>
      {videoActionMenu && videoMenuVideo ? (
        <VideoContextMenu
          video={videoMenuVideo}
          x={videoActionMenu.x}
          y={videoActionMenu.y}
          submenuSide={videoActionMenu.submenuSide}
          folders={folderMenuItems}
          currentFolderName={getFolderLabel(videoMenuVideo)}
          canPinInFolder={getVideoFolderIds(videoMenuVideo).length > 0}
          busy={videoActionBusy}
          deleteConfirmOpen={videoDeleteConfirmId === videoMenuVideo.video_id}
          onOpenDetail={() => {
            closeVideoActions();
            navigate(`/videos/${videoMenuVideo.video_id}`);
          }}
          onRevealFolder={() => {
            const primaryFolderId = getVideoFolderIds(videoMenuVideo)[0] || null;
            if (!primaryFolderId) {
              setActiveScope("unfiled");
            } else {
              setActiveScope(primaryFolderId);
              const ancestorIds = getFolderAncestorIds(folders, primaryFolderId);
              if (ancestorIds.length) {
                setCollapsedFolderIds((current) => {
                  const next = new Set(current);
                  for (const folderId of ancestorIds) next.delete(folderId);
                  return next;
                });
              }
            }
            closeVideoActions();
          }}
          onMove={(folderId) => runVideoAction(
            async () => {
              await onMoveVideo(videoMenuVideo.video_id, folderId);
            },
            folderId ? "已移动到分组" : "已移到未归档",
          )}
          onSetFolders={(folderIds) => runVideoAction(
            async () => {
              await onMoveVideo(videoMenuVideo.video_id, undefined, folderIds);
            },
            folderIds.length ? "分组已更新" : "已移到未归档",
          )}
          onToggleFavorite={() => runVideoAction(
            async () => {
              await onToggleFavorite(videoMenuVideo.video_id, !videoMenuVideo.is_favorite);
            },
            videoMenuVideo.is_favorite ? "已取消收藏" : "已收藏",
          )}
          onToggleGlobalPin={() => runVideoAction(
            async () => {
              await onSetVideoPin(videoMenuVideo.video_id, { global_pinned: !videoMenuVideo.global_pinned });
            },
            videoMenuVideo.global_pinned ? "已取消全局置顶" : "已全局置顶",
          )}
          onToggleFolderPin={() => runVideoAction(
            async () => {
              await onSetVideoPin(videoMenuVideo.video_id, { folder_pinned: !videoMenuVideo.folder_pinned });
            },
            videoMenuVideo.folder_pinned ? "已取消文件夹置顶" : "已文件夹置顶",
          )}
          onRequestDelete={() => setVideoDeleteConfirmId(videoMenuVideo.video_id)}
          onCancelDelete={() => setVideoDeleteConfirmId(null)}
          onDelete={() => runVideoAction(
            async () => {
              await onDeleteVideo(videoMenuVideo.video_id);
            },
            "视频已删除",
          )}
        />
      ) : null}
      <FloatingNoticeStack notices={[{ id: "library-action-status", message: libraryNotice.message, tone: libraryNotice.tone, version: libraryNotice.version }]} />
    </section>
  );
}

function ScopeButton({
  icon,
  active,
  label,
  count,
  onClick,
  droppableId,
  folderId,
}: {
  icon: ReactNode;
  active: boolean;
  label: string;
  count: number;
  onClick(): void;
  droppableId?: string;
  folderId?: string | null;
}) {
  const { isOver, setNodeRef } = useDroppable({
    id: droppableId || `scope:${label}`,
    disabled: !droppableId,
    data: { type: "folder-drop", folderId: folderId ?? null },
  });
  return (
    <button
      ref={setNodeRef}
      className={`library-scope-row ${active ? "is-active" : ""} ${isOver ? "is-video-over" : ""}`}
      type="button"
      onClick={onClick}
    >
      <span className="library-folder-icon" aria-hidden="true">{icon}</span>
      <span>{label}</span>
      <strong>{count}</strong>
    </button>
  );
}

function VideoContextMenu({
  video,
  x,
  y,
  submenuSide,
  folders,
  currentFolderName,
  canPinInFolder,
  busy,
  deleteConfirmOpen,
  onOpenDetail,
  onRevealFolder,
  onMove,
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
  folders: FolderMenuItem[];
  currentFolderName: string;
  canPinInFolder: boolean;
  busy: boolean;
  deleteConfirmOpen: boolean;
  onOpenDetail(): void;
  onRevealFolder(): void;
  onMove(folderId: string | null): void;
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
        <span>{currentFolderName}</span>
      </div>
      <button type="button" role="menuitem" disabled={busy} onClick={onOpenDetail}>
        <ExternalLinkIcon />
        <span>打开详情</span>
      </button>
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
          <p>删除后会移除视频文件与任务记录。</p>
          <div>
            <button type="button" disabled={busy} onClick={onCancelDelete}>取消</button>
            <button className="is-danger" type="button" disabled={busy} onClick={onDelete}>删除</button>
          </div>
        </div>
      ) : (
        <button className="is-danger" type="button" role="menuitem" disabled={busy} onClick={onRequestDelete}>
          <TrashIcon />
          <span>删除视频</span>
        </button>
      )}
    </div>
  );
}

function flattenFolderMenuItems(nodes: LibraryFolderTreeNode[], depth = 0): FolderMenuItem[] {
  return nodes.flatMap((node) => [
    { folder: node.folder, depth },
    ...flattenFolderMenuItems(node.children, depth + 1),
  ]);
}

function resolveContextMenuPosition(clientX: number, clientY: number) {
  const menuWidth = 292;
  const submenuWidth = 248;
  const menuHeight = 560;
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

function VideoListItem({
  video,
  folderName,
  canPinInFolder,
  onToggleFavorite,
  onSetVideoPin,
  onOpenContextMenu,
  className = "",
  dragHandleProps,
  style,
  setNodeRef,
}: {
  video: VideoAssetSummary;
  folderName?: string;
  canPinInFolder: boolean;
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
  onSetVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }): Promise<VideoAssetDetail>;
  onOpenContextMenu?: (event: MouseEvent, videoId: string) => void;
  className?: string;
  dragHandleProps?: Record<string, unknown>;
  style?: React.CSSProperties;
  setNodeRef?: (node: HTMLDivElement | null) => void;
}) {
  return (
    <div ref={setNodeRef} style={style} className={`library-list-item ${className}`.trim()} onContextMenu={(event) => onOpenContextMenu?.(event, video.video_id)}>
      <span className="library-drag-handle" title="拖动排序" {...dragHandleProps}><GripVerticalIcon /></span>
      <Link className="library-list-cover" to={`/videos/${video.video_id}`} draggable={false}>
        {video.cover_url ? <img src={video.cover_url} alt={video.title} loading="lazy" draggable={false} /> : <span>VIDEO</span>}
      </Link>
      <Link className="library-list-main" to={`/videos/${video.video_id}`} draggable={false}>
        <strong>{video.title}</strong>
        <span>{platformLabel(video.platform)} · {formatDuration(video.duration)} · {folderName || "未归档"}</span>
      </Link>
      <span className={`task-status ${taskStatusClass(video.latest_status)}`}>{taskStatusLabel(video.latest_status)}</span>
      <span className="library-list-date">{formatDateTime(video.updated_at)}</span>
      <div className="library-list-actions">
        <button type="button" title={video.is_favorite ? "取消收藏" : "收藏"} onClick={() => void onToggleFavorite(video.video_id, !video.is_favorite)}>{video.is_favorite ? "★" : "☆"}</button>
        {canPinInFolder ? (
          <button className={`library-pin-button ${video.folder_pinned ? "is-active" : ""}`} type="button" title={video.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"} onClick={() => void onSetVideoPin(video.video_id, { folder_pinned: !video.folder_pinned })}>
            <PinIcon />
            <span>文件夹置顶</span>
          </button>
        ) : (
          <button className={`library-pin-button ${video.global_pinned ? "is-active" : ""}`} type="button" title={video.global_pinned ? "取消全局置顶" : "全局置顶"} onClick={() => void onSetVideoPin(video.video_id, { global_pinned: !video.global_pinned })}>
            <PinIcon />
            <span>全局置顶</span>
          </button>
        )}
      </div>
    </div>
  );
}

function SortableVideoCard({
  video,
  folderName,
  canPinInFolder,
  onToggleFavorite,
  onSetVideoPin,
  onOpenContextMenu,
}: {
  video: VideoAssetSummary;
  folderName?: string;
  canPinInFolder: boolean;
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
  onSetVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }): Promise<VideoAssetDetail>;
  onOpenContextMenu?: (event: MouseEvent, videoId: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: video.video_id,
    data: { type: "video", videoId: video.video_id },
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div ref={setNodeRef} style={style} className={`library-video-shell ${isDragging ? "is-dragging" : ""}`}>
      <div className="library-card-drag-surface" {...attributes} {...listeners}>
        <VideoCard
          video={video}
          folderName={folderName}
          canPinInFolder={canPinInFolder}
          onToggleFavorite={onToggleFavorite}
          onToggleGlobalPin={async (videoId, nextPinned) => {
            await onSetVideoPin(videoId, { global_pinned: nextPinned });
          }}
          onToggleFolderPin={async (videoId, nextPinned) => {
            await onSetVideoPin(videoId, { folder_pinned: nextPinned });
          }}
          onOpenContextMenu={onOpenContextMenu}
        />
      </div>
    </div>
  );
}

function SortableVideoListItem({
  video,
  folderName,
  canPinInFolder,
  onToggleFavorite,
  onSetVideoPin,
  onOpenContextMenu,
}: {
  video: VideoAssetSummary;
  folderName?: string;
  canPinInFolder: boolean;
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
  onSetVideoPin(videoId: string, payload: { global_pinned?: boolean | null; folder_pinned?: boolean | null }): Promise<VideoAssetDetail>;
  onOpenContextMenu?: (event: MouseEvent, videoId: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: video.video_id,
    data: { type: "video", videoId: video.video_id },
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <VideoListItem
      setNodeRef={setNodeRef}
      style={style}
      className={isDragging ? "is-dragging" : ""}
      video={video}
      folderName={folderName}
      canPinInFolder={canPinInFolder}
      onToggleFavorite={onToggleFavorite}
      onSetVideoPin={onSetVideoPin}
      onOpenContextMenu={onOpenContextMenu}
      dragHandleProps={{ ...attributes, ...listeners }}
    />
  );
}

function FolderTreeNode({
  node,
  style,
  dragHandle,
  activeScope,
  actionOpen,
  videoCount,
  onActivate,
  collapsed,
  onBeginCreate,
  renameOpen,
  renameValue,
  onRenameChange,
  onBeginRename,
  onSubmitRename,
  onCancelRename,
  onDelete,
  deleteConfirmOpen,
  onRequestDelete,
  onCancelDelete,
  onPromote,
  onMoveSibling,
  onToggleActions,
}: {
  node: NodeRendererProps<LibraryFolderTreeNode>["node"];
  style: NodeRendererProps<LibraryFolderTreeNode>["style"];
  dragHandle?: NodeRendererProps<LibraryFolderTreeNode>["dragHandle"];
  activeScope: LibraryScope;
  actionOpen: boolean;
  collapsed: boolean;
  videoCount: number;
  onActivate(folderId: string): void;
  onBeginCreate(parentId: string): void;
  renameOpen: boolean;
  renameValue: string;
  onRenameChange(value: string): void;
  onBeginRename(folder: VideoFolder): void;
  onSubmitRename(folder: VideoFolder): Promise<void>;
  onCancelRename(): void;
  onDelete(folder: VideoFolder): Promise<void>;
  deleteConfirmOpen: boolean;
  onRequestDelete(folderId: string): void;
  onCancelDelete(): void;
  onPromote(folder: VideoFolder, mode: "parent" | "root"): Promise<void>;
  onMoveSibling(folder: VideoFolder, direction: "up" | "down"): Promise<void>;
  onToggleActions(folderId: string): void;
}) {
  const folder = node.data.folder;
  const hasChildren = Boolean(node.children?.length);
  const { isOver, setNodeRef } = useDroppable({
    id: getFolderDropId(folder.folder_id),
    data: { type: "folder-drop", folderId: folder.folder_id },
  });

  function toggleChildren() {
    if (!hasChildren) return;
    node.toggle();
  }

  return (
    <div style={style} className={`library-folder-tree-node ${node.isDragging ? "is-dragging" : ""}`}>
      <div
        ref={dragHandle}
        className="library-folder-tree-row-wrap"
        title="拖动调整文件夹位置和层级"
      >
        <button
          ref={setNodeRef}
          type="button"
          className={`library-folder-row ${activeScope === folder.folder_id ? "is-active" : ""} ${node.willReceiveDrop ? "is-drop-into" : ""} ${isOver ? "is-video-over" : ""}`}
          onClick={(event) => {
            event.stopPropagation();
            onActivate(folder.folder_id);
            toggleChildren();
          }}
          onPointerDown={(event) => {
            const target = event.target as HTMLElement;
            if (target.closest(".library-folder-actions")) event.stopPropagation();
          }}
          onContextMenu={(event) => {
            event.preventDefault();
            event.stopPropagation();
            onToggleActions(folder.folder_id);
          }}
          onKeyDown={(event) => {
            if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
              event.preventDefault();
              onToggleActions(folder.folder_id);
              return;
            }
            if ((event.key === "Enter" || event.key === " ") && hasChildren) {
              event.preventDefault();
              onActivate(folder.folder_id);
              toggleChildren();
            }
          }}
        >
          <span className="library-folder-toggle" aria-hidden="true" />
          <span className="library-folder-icon" aria-hidden="true"><FolderIcon /></span>
          <span>{folder.name}</span>
          <strong>{videoCount}</strong>
        </button>
      </div>
      <div
        className={`library-folder-actions ${actionOpen ? "is-open" : ""} ${renameOpen || deleteConfirmOpen ? "is-expanded" : ""}`}
        role="menu"
        aria-label={`${folder.name}管理`}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          onBeginCreate(folder.folder_id);
        }}><span aria-hidden="true">+</span> 新建子文件夹</button>
        <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          void onMoveSibling(folder, "up");
        }}><span aria-hidden="true">↑</span> 上移</button>
        <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          void onMoveSibling(folder, "down");
        }}><span aria-hidden="true">↓</span> 下移</button>
        <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          onBeginRename(folder);
        }}><span aria-hidden="true">Aa</span> 重命名</button>
        {renameOpen ? (
          <form
            className="library-folder-inline-panel"
            onSubmit={(event) => {
              event.preventDefault();
              void onSubmitRename(folder);
            }}
          >
            <input
              autoFocus
              value={renameValue}
              placeholder="文件夹名称"
              onChange={(event) => onRenameChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") onCancelRename();
              }}
            />
            <div className="library-folder-inline-actions">
              <button type="submit" disabled={!renameValue.trim() || renameValue.trim() === folder.name}>保存</button>
              <button type="button" onClick={onCancelRename}>取消</button>
            </div>
          </form>
        ) : null}
        {folder.parent_id ? (
          <>
            <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
              event.stopPropagation();
              void onPromote(folder, "parent");
            }}><CornerUpLeftIcon /> 上移一级</button>
            <button type="button" role="menuitem" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
              event.stopPropagation();
              void onPromote(folder, "root");
            }}><ArrowUpToLineIcon /> 移到顶层</button>
          </>
        ) : null}
        <button type="button" role="menuitem" className="is-danger" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          onRequestDelete(folder.folder_id);
        }}><span aria-hidden="true">×</span> 删除</button>
        {deleteConfirmOpen ? (
          <div className="library-folder-inline-panel is-danger-confirm" role="group" aria-label={`确认删除${folder.name}`}>
            <p>删除“{folder.name}”？子文件夹会一并删除，视频移到未归档。</p>
            <div className="library-folder-inline-actions">
              <button type="button" className="is-danger" onClick={() => void onDelete(folder)}>确认删除</button>
              <button type="button" onClick={onCancelDelete}>取消</button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FolderDraftInput({
  value,
  busy,
  onChange,
  onSubmit,
  onCancel,
}: {
  value: string;
  busy: boolean;
  onChange(value: string): void;
  onSubmit(): Promise<void>;
  onCancel(): void;
}) {
  return (
    <form
      className="library-folder-draft"
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit();
      }}
    >
      <span className="library-folder-icon" aria-hidden="true"><FolderIcon /></span>
      <input
        autoFocus
        value={value}
        disabled={busy}
        placeholder="文件夹名称"
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
      />
      <button type="submit" disabled={busy || !value.trim()} title="创建">✓</button>
      <button type="button" disabled={busy} title="取消" onClick={onCancel}>×</button>
    </form>
  );
}
