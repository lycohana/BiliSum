import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  rectSortingStrategy,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS as DndCSS } from "@dnd-kit/utilities";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent, type PointerEvent as ReactPointerEvent, type SyntheticEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import {
  buildCollectionQueueVideoIds,
  COLLECTION_GROUP_LABELS,
  COLLECTION_QUEUE_ORDER_LABELS,
  COLLECTION_SORT_LABELS,
  COLLECTION_STATUS_LABELS,
  countCollectionStatuses,
  deriveCollectionView,
  flattenCollectionGroups,
  isCollectionItemSummarizable,
  sortBySourcePosition,
  sortCollectionItems,
  type CollectionQueueOrder,
  type CollectionStatusFilter,
  type CollectionViewGroup,
} from "../collectionViewModel";
import { ExternalLinkIcon, RefreshIcon, SearchIcon, TrashIcon } from "../components/AppIcons";
import { VideoCard } from "../components/VideoCard";
import type { CollectionGroupMode, CollectionSortMode, VideoAssetSummary, VideoCollection, VideoCollectionItem } from "../types";

type CollectionDetailPageProps = {
  serviceOnline: boolean;
  onRefreshCollection(collectionId: string): Promise<VideoCollection>;
  onAddCollectionItems(collectionId: string, bvids: string[]): Promise<VideoCollection>;
  onUpdateCollectionSettings(collectionId: string, autoCheckOnOpen: boolean): Promise<VideoCollection>;
  onUpdateCollectionView(
    collectionId: string,
    payload: { view_sort_mode?: CollectionSortMode; view_group_mode?: CollectionGroupMode },
  ): Promise<VideoCollection>;
  onReorderCollectionItems(collectionId: string, orderedBvids: string[]): Promise<VideoCollection>;
  onDeleteCollection(collectionId: string, mode: "detach" | "delete_contents"): Promise<void>;
  onPromoteCollectionItem(collectionId: string, videoId: string): Promise<unknown>;
  onSummarizeCollectionItems(collectionId: string, videoIds: string[]): Promise<void>;
};

type CollectionSelectionDrag = {
  pointerId: number;
  videoId: string;
  select: boolean;
  moved: boolean;
  startX: number;
  startY: number;
  lastVideoId: string | null;
};

const COLLECTION_AUTO_CHECK_LABELS = {
  enabled: "开启自动检查",
  disabled: "关闭自动检查",
} as const;

type CollectionAutoCheckMode = keyof typeof COLLECTION_AUTO_CHECK_LABELS;

function readableCollectionError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message.trim() : "";
  if (!message || /^Internal Server Error$/i.test(message)) return fallback;
  if (/LLM returned no readable message content/i.test(message)) {
    return "LLM 没有返回可读取的内容，已按设置进行重试；请检查模型响应格式。";
  }
  return message;
}

function withCustomCollectionOrder(collection: VideoCollection, orderedBvids: string[]): VideoCollection {
  const positions = new Map(orderedBvids.map((bvid, index) => [bvid, index + 1]));
  return {
    ...collection,
    items: collection.items.map((item) => ({
      ...item,
      custom_position: positions.get(item.bvid) ?? item.custom_position,
    })),
  };
}

export function CollectionDetailPage({
  serviceOnline,
  onRefreshCollection,
  onAddCollectionItems,
  onUpdateCollectionSettings,
  onUpdateCollectionView,
  onReorderCollectionItems,
  onDeleteCollection,
  onPromoteCollectionItem,
  onSummarizeCollectionItems,
}: CollectionDetailPageProps) {
  const { collectionId = "" } = useParams();
  const navigate = useNavigate();
  const [collection, setCollection] = useState<VideoCollection | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [selectionOrder, setSelectionOrder] = useState<string[]>([]);
  const [queueOrder, setQueueOrder] = useState<CollectionQueueOrder>("source");
  const [statusFilter, setStatusFilter] = useState<CollectionStatusFilter>("all");
  const [query, setQuery] = useState("");
  const [sortMode, setSortMode] = useState<CollectionSortMode>("source");
  const [groupMode, setGroupMode] = useState<CollectionGroupMode>("none");
  const [ownerFaceFailed, setOwnerFaceFailed] = useState(false);
  const [newNoticeDismissed, setNewNoticeDismissed] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; item: VideoCollectionItem } | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [selectionDragActive, setSelectionDragActive] = useState(false);
  const [toolbarStuck, setToolbarStuck] = useState(false);
  const selectionDragRef = useRef<CollectionSelectionDrag | null>(null);
  const suppressNextSelectionClickRef = useRef(false);
  const initializedViewRef = useRef("");
  const toolbarRef = useRef<HTMLElement | null>(null);
  const toolbarSentinelRef = useRef<HTMLDivElement | null>(null);
  const viewSaveGenerationRef = useRef(0);
  const viewSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const desiredViewRef = useRef<{ sort: CollectionSortMode; group: CollectionGroupMode }>({ sort: "source", group: "none" });
  const persistedViewRef = useRef<{ sort: CollectionSortMode; group: CollectionGroupMode }>({ sort: "source", group: "none" });
  const reorderGenerationRef = useRef(0);
  const reorderSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const persistedCustomOrderRef = useRef<string[]>([]);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const view = useMemo(() => deriveCollectionView(collection?.items || [], {
    status: statusFilter,
    query,
    sort: sortMode,
    group: groupMode,
  }), [collection?.items, statusFilter, query, sortMode, groupMode]);
  const renderedItems = useMemo(() => flattenCollectionGroups(view.groups), [view.groups]);
  const statusCounts = useMemo(() => countCollectionStatuses(collection?.items || []), [collection?.items]);
  const selectableItems = useMemo(() => view.sourceItems.filter(isCollectionItemSummarizable), [view.sourceItems]);
  const visibleSelectedIds = useMemo(() => new Set(
    view.visibleItems.map((item) => item.video_id).filter((videoId): videoId is string => Boolean(videoId)),
  ), [view.visibleItems]);
  const hiddenSelectedCount = useMemo(
    () => [...selectedIds].filter((videoId) => !visibleSelectedIds.has(videoId)).length,
    [selectedIds, visibleSelectedIds],
  );
  const newItems = collection?.new_items || [];
  const ownerUrl = collection?.owner_url || (collection?.owner_mid ? `https://space.bilibili.com/${collection.owner_mid}` : "");
  const canCustomReorder = (
    sortMode === "custom"
    && groupMode === "none"
    && statusFilter === "all"
    && !query.trim()
  );

  useEffect(() => {
    let cancelled = false;
    initializedViewRef.current = "";
    viewSaveGenerationRef.current += 1;
    desiredViewRef.current = { sort: "source", group: "none" };
    persistedViewRef.current = { sort: "source", group: "none" };
    reorderGenerationRef.current += 1;
    persistedCustomOrderRef.current = [];
    setCollection(null);
    setLoading(true);
    setErrorMessage("");
    setStatusFilter("all");
    setQuery("");
    setSortMode("source");
    setGroupMode("none");

    function applyLoadedCollection(response: VideoCollection) {
      setOwnerFaceFailed(false);
      setCollection(response);
      persistedCustomOrderRef.current = sortCollectionItems(response.items, "custom").map((item) => item.bvid);
      if (initializedViewRef.current !== collectionId) {
        const loadedView = {
          sort: response.view_sort_mode || "source",
          group: response.view_group_mode || "none",
        } satisfies { sort: CollectionSortMode; group: CollectionGroupMode };
        setSortMode(loadedView.sort);
        setGroupMode(loadedView.group);
        desiredViewRef.current = loadedView;
        persistedViewRef.current = loadedView;
        initializedViewRef.current = collectionId;
      }
    }

    async function load() {
      try {
        const response = await api.getVideoCollection(collectionId);
        if (cancelled) return;
        applyLoadedCollection(response);
        setLoading(false);
        if (response.auto_check_on_open !== false) {
          setRefreshing(true);
          try {
            const refreshed = await onRefreshCollection(collectionId);
            if (!cancelled) applyLoadedCollection(refreshed);
          } catch (error) {
            if (!cancelled) setErrorMessage(readableCollectionError(error, "合集刷新失败，请稍后重试。"));
          } finally {
            if (!cancelled) setRefreshing(false);
          }
        }
      } catch (error) {
        if (!cancelled) {
          setErrorMessage(readableCollectionError(error, "合集读取失败，请稍后重试。"));
          setLoading(false);
        }
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [collectionId]);

  useEffect(() => {
    setSelectedIds(new Set());
    setSelectionOrder([]);
    setQueueOrder("source");
    setSelectionMode(false);
    selectionDragRef.current = null;
    setSelectionDragActive(false);
  }, [collectionId]);

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const drag = selectionDragRef.current;
      if (!drag || drag.pointerId !== event.pointerId || event.buttons !== 1) return;
      const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
      if (!drag.moved && distance < 4) return;
      drag.moved = true;
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-collection-selection-id]");
      const videoId = target?.dataset.collectionSelectionId;
      if (!videoId || videoId === drag.lastVideoId) return;
      drag.lastVideoId = videoId;
      setSelectedState(drag.videoId, drag.select);
      setSelectedState(videoId, drag.select);
      event.preventDefault();
    }
    function finishSelectionDrag() {
      const drag = selectionDragRef.current;
      suppressNextSelectionClickRef.current = Boolean(drag?.moved);
      selectionDragRef.current = null;
      setSelectionDragActive(false);
    }
    window.addEventListener("pointermove", handlePointerMove, { passive: false });
    window.addEventListener("pointerup", finishSelectionDrag);
    window.addEventListener("pointercancel", finishSelectionDrag);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishSelectionDrag);
      window.removeEventListener("pointercancel", finishSelectionDrag);
    };
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("blur", close);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (loading) return;
    const toolbar = toolbarRef.current;
    const sentinel = toolbarSentinelRef.current;
    const scrollRoot = toolbar?.closest<HTMLElement>(".content");
    if (!toolbar || !sentinel || !scrollRoot) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      const stickyViewportTop = Number.parseFloat(
        getComputedStyle(toolbar).getPropertyValue("--collection-sticky-viewport-top"),
      ) || 0;
      setToolbarStuck(sentinel.getBoundingClientRect().top <= stickyViewportTop + 1);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    scrollRoot.addEventListener("scroll", schedule, { passive: true });
    document.body.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      scrollRoot.removeEventListener("scroll", schedule);
      document.body.removeEventListener("scroll", schedule);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [collectionId, loading]);

  useEffect(() => {
    if (!deleteConfirmOpen) return;
    function closeDeletePopover(event?: Event) {
      const target = event?.target as HTMLElement | null;
      if (target?.closest(".collection-delete-anchor")) return;
      setDeleteConfirmOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") closeDeletePopover();
    }
    window.addEventListener("pointerdown", closeDeletePopover, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeDeletePopover, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [deleteConfirmOpen]);

  async function refreshCollection() {
    setRefreshing(true);
    setErrorMessage("");
    try {
      setOwnerFaceFailed(false);
      setCollection(await onRefreshCollection(collectionId));
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "合集刷新失败，请稍后重试。"));
    } finally {
      setRefreshing(false);
    }
  }

  function setSelectedState(videoId: string, selected: boolean) {
    setSelectedIds((current) => {
      if (current.has(videoId) === selected) return current;
      const next = new Set(current);
      if (selected) next.add(videoId);
      else next.delete(videoId);
      return next;
    });
    setSelectionOrder((current) => selected
      ? (current.includes(videoId) ? current : [...current, videoId])
      : current.filter((entry) => entry !== videoId));
  }

  function toggleSelected(videoId: string) {
    if (suppressNextSelectionClickRef.current) {
      suppressNextSelectionClickRef.current = false;
      return;
    }
    setSelectedState(videoId, !selectedIds.has(videoId));
  }

  function beginSelectionDrag(videoId: string, event: ReactPointerEvent<HTMLElement>) {
    if (!selectionMode || event.button !== 0) return;
    event.stopPropagation();
    suppressNextSelectionClickRef.current = false;
    selectionDragRef.current = {
      pointerId: event.pointerId,
      videoId,
      select: !selectedIds.has(videoId),
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
      lastVideoId: null,
    };
    setSelectionDragActive(true);
  }

  function selectAllSummarizable() {
    const videoIds = selectableItems.map((item) => item.video_id).filter((videoId): videoId is string => Boolean(videoId));
    setSelectionMode(true);
    setSelectedIds(new Set(videoIds));
    setSelectionOrder(videoIds);
  }

  function enterSelectionMode() {
    setSelectionMode(true);
    selectionDragRef.current = null;
    setSelectionDragActive(false);
  }

  function exitSelectionMode() {
    setSelectionMode(false);
    setSelectedIds(new Set());
    setSelectionOrder([]);
  }

  async function summarizeSelected() {
    const videoIds = buildCollectionQueueVideoIds({
      sourceItems: view.sourceItems,
      currentViewItems: renderedItems,
      selectedIds,
      selectionOrder,
      order: queueOrder,
    });
    if (!videoIds.length) return;
    setBusy(true);
    setErrorMessage("");
    try {
      await onSummarizeCollectionItems(collectionId, [...videoIds]);
      setSelectedIds(new Set());
      setSelectionOrder([]);
      setCollection(await api.getVideoCollection(collectionId));
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "批量总结任务创建失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  function updateView(nextSort: CollectionSortMode, nextGroup: CollectionGroupMode) {
    const generation = viewSaveGenerationRef.current + 1;
    viewSaveGenerationRef.current = generation;
    desiredViewRef.current = { sort: nextSort, group: nextGroup };
    setSortMode(nextSort);
    setGroupMode(nextGroup);
    setErrorMessage("");
    viewSaveQueueRef.current = viewSaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        try {
          const updated = await onUpdateCollectionView(collectionId, {
            view_sort_mode: nextSort,
            view_group_mode: nextGroup,
          });
          if (initializedViewRef.current !== collectionId) return;
          persistedViewRef.current = { sort: nextSort, group: nextGroup };
          if (generation === viewSaveGenerationRef.current) setCollection(updated);
        } catch (error) {
          if (generation !== viewSaveGenerationRef.current) return;
          const persistedView = persistedViewRef.current;
          desiredViewRef.current = persistedView;
          setSortMode(persistedView.sort);
          setGroupMode(persistedView.group);
          setErrorMessage(readableCollectionError(error, "合集视图设置保存失败。"));
        }
      });
  }

  function changeSort(nextSort: CollectionSortMode) {
    updateView(nextSort, desiredViewRef.current.group);
  }

  function changeGroup(nextGroup: CollectionGroupMode) {
    updateView(desiredViewRef.current.sort, nextGroup);
  }

  function persistCustomOrder(orderedBvids: string[], failureMessage: string): Promise<void> {
    const generation = reorderGenerationRef.current + 1;
    reorderGenerationRef.current = generation;
    setCollection((current) => current ? withCustomCollectionOrder(current, orderedBvids) : current);
    setErrorMessage("");
    reorderSaveQueueRef.current = reorderSaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        try {
          const updated = await onReorderCollectionItems(collectionId, orderedBvids);
          if (initializedViewRef.current !== collectionId) return;
          persistedCustomOrderRef.current = sortCollectionItems(updated.items, "custom").map((item) => item.bvid);
          if (generation === reorderGenerationRef.current) setCollection(updated);
        } catch (error) {
          if (generation !== reorderGenerationRef.current) return;
          const persistedOrder = persistedCustomOrderRef.current;
          setCollection((current) => current ? withCustomCollectionOrder(current, persistedOrder) : current);
          setErrorMessage(readableCollectionError(error, failureMessage));
        }
      });
    return reorderSaveQueueRef.current;
  }

  function handleCustomDragEnd(event: DragEndEvent) {
    if (!collection || !canCustomReorder || !event.over) return;
    const currentOrder = view.visibleItems.map((item) => item.bvid);
    const fromIndex = currentOrder.indexOf(String(event.active.id));
    const toIndex = currentOrder.indexOf(String(event.over.id));
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return;
    const orderedBvids = arrayMove(currentOrder, fromIndex, toIndex);
    void persistCustomOrder(orderedBvids, "自定义顺序保存失败，请稍后重试。");
  }

  async function restoreSourceOrder() {
    if (!collection) return;
    setBusy(true);
    setErrorMessage("");
    try {
      await persistCustomOrder(
        sortBySourcePosition(collection.items).map((item) => item.bvid),
        "恢复原合集顺序失败。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function addNewItems() {
    setBusy(true);
    setErrorMessage("");
    try {
      setCollection(await onAddCollectionItems(collectionId, newItems.map((item) => item.bvid)));
      setNewNoticeDismissed(false);
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "新增视频加入失败。"));
    } finally {
      setBusy(false);
    }
  }

  async function updateAutoCheck(enabled: boolean) {
    if (!collection) return;
    const currentAutoCheck = collection.auto_check_on_open !== false;
    if (currentAutoCheck === enabled) return;
    setBusy(true);
    setErrorMessage("");
    try {
      setCollection(await onUpdateCollectionSettings(collectionId, enabled));
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "合集设置保存失败。"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteCollection(mode: "detach" | "delete_contents") {
    setBusy(true);
    setErrorMessage("");
    try {
      await onDeleteCollection(collectionId, mode);
      navigate("/library");
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "合集删除失败，请稍后重试。"));
    } finally {
      setBusy(false);
      setDeleteConfirmOpen(false);
    }
  }

  async function promoteItem(item: VideoCollectionItem) {
    if (!item.video_id) return;
    setBusy(true);
    setErrorMessage("");
    try {
      await onPromoteCollectionItem(collectionId, item.video_id);
      setCollection((current) => current ? {
        ...current,
        items: current.items.map((entry) => entry.video_id === item.video_id ? { ...entry, is_global_visible: true } : entry),
      } : current);
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "添加到外层视频库失败。"));
    } finally {
      setBusy(false);
      setContextMenu(null);
    }
  }

  function handleImageError(event: SyntheticEvent<HTMLImageElement>) {
    const target = event.target as HTMLImageElement;
    target.style.display = "none";
    target.parentElement?.querySelector(".collection-detail-placeholder")?.classList.remove("is-hidden");
  }

  if (loading) {
    return <section className="collection-detail-page collection-detail-state"><strong>正在读取合集…</strong><span>{serviceOnline ? "正在同步合集内容" : "等待服务恢复"}</span></section>;
  }
  if (!collection) {
    return <section className="collection-detail-page collection-detail-state"><strong>合集不可用</strong><span>{errorMessage || "这个合集可能已经被移除。"}</span><button className="secondary-button" type="button" onClick={() => navigate("/library")}>返回视频库</button></section>;
  }

  const hasViewConstraint = statusFilter !== "all" || Boolean(query.trim());

  return (
    <section className={`collection-detail-page detail-page-shell${selectionMode ? " is-selection-mode" : ""}`}>
      <div className="detail-page-toolbar collection-detail-toolbar-top">
        <Link className="detail-back-button" to="/library">
          <svg className="detail-back-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M10.5 3 5.5 8l5 5M6 8h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
          返回视频库
        </Link>
        <span className="collection-detail-auto-status">{refreshing ? "正在检查合集更新…" : collection.auto_check_on_open === false ? "自动检查已关闭" : "打开时自动检查"}</span>
      </div>

      <header className="collection-detail-hero video-detail-hero">
        <div className="collection-detail-hero-cover video-detail-cover">
          {collection.cover_url ? <><img src={collection.cover_url} alt={collection.title} onError={handleImageError} /><div className="collection-detail-placeholder is-hidden">合集</div></> : <div className="collection-detail-placeholder">合集</div>}
          <div className="collection-count-badge" aria-label={`${collection.item_count} 个视频`}><strong>{collection.item_count}</strong><span>集</span></div>
        </div>
        <div className="collection-detail-hero-copy video-detail-copy">
          <div className="video-card-badges"><span className="video-platform-badge is-bilibili">Bilibili</span><span className="video-page-badge">合集</span></div>
          <h1>{collection.title}</h1>
          <p>{collection.item_count} 个视频 · 已总结 {statusCounts.completed} · 未总结 {statusCounts.all - statusCounts.completed}</p>
          {ownerUrl || collection.owner_name ? <CollectionOwner collection={collection} ownerUrl={ownerUrl} ownerFaceFailed={ownerFaceFailed} onOwnerFaceError={() => setOwnerFaceFailed(true)} /> : null}
          <div className="collection-detail-actions">
            <div className="collection-refresh-control" role="group" aria-label="刷新与自动检查">
              <button className="secondary-button compact-button collection-icon-button" type="button" aria-label="刷新列表" title="刷新列表" disabled={refreshing} onClick={() => void refreshCollection()}><RefreshIcon aria-hidden="true" /></button>
              <div className="collection-auto-check-control">
                <CollectionToolbarSelect<CollectionAutoCheckMode>
                  ariaLabel="自动检查"
                  value={collection.auto_check_on_open === false ? "disabled" : "enabled"}
                  options={COLLECTION_AUTO_CHECK_LABELS}
                  iconOnly
                  disabled={busy}
                  onChange={(mode) => void updateAutoCheck(mode === "enabled")}
                />
              </div>
            </div>
            <div className="collection-delete-anchor">
              <button className="secondary-button compact-button danger-button collection-icon-button" type="button" aria-label="删除合集" title="删除合集" aria-expanded={deleteConfirmOpen} aria-controls="collection-delete-popover" disabled={busy} onClick={() => setDeleteConfirmOpen((open) => !open)}><TrashIcon aria-hidden="true" /></button>
              {deleteConfirmOpen ? <div className="collection-delete-panel" id="collection-delete-popover" role="dialog" aria-label="删除合集"><strong>删除这个合集</strong><p>仅移除合集会把内部视频恢复到总视频库；删除全部内容会同时移除视频、任务和摘要。</p><div><button className="secondary-button compact-button" type="button" disabled={busy} onClick={() => void deleteCollection("detach")}>仅移除合集</button><button className="primary-button compact-button danger-button" type="button" disabled={busy} onClick={() => void deleteCollection("delete_contents")}>删除全部内容</button></div></div> : null}
            </div>
          </div>
        </div>
      </header>

      {newItems.length && !newNoticeDismissed ? <div className="collection-detail-new-notice" role="status"><span>发现 {newItems.length} 个新视频，是否添加到这个合集？</span><div><button type="button" disabled={busy} onClick={() => void addNewItems()}>添加</button><button type="button" className="is-quiet" onClick={() => setNewNoticeDismissed(true)}>稍后</button></div></div> : null}
      {errorMessage ? <p className="collection-detail-error" role="alert">{errorMessage}</p> : null}

      <div className="collection-view-toolbar-sentinel" ref={toolbarSentinelRef} aria-hidden="true" />
      <section ref={toolbarRef} className={`collection-view-toolbar collection-detail-toolbar${toolbarStuck ? " is-stuck" : ""}`} aria-label="合集内容工具栏">
        <div className="collection-view-toolbar-heading"><strong>合集内容</strong></div>
        <div className="collection-view-toolbar-row">
          <div className="collection-status-tabs" role="group" aria-label="按总结状态筛选">
            {(Object.keys(COLLECTION_STATUS_LABELS) as CollectionStatusFilter[]).map((status) => <button key={status} className={statusFilter === status ? "is-active" : ""} type="button" aria-pressed={statusFilter === status} onClick={() => setStatusFilter(status)}>{COLLECTION_STATUS_LABELS[status]} <strong>{statusCounts[status]}</strong></button>)}
          </div>
          <div className="collection-view-controls">
            <label className="collection-search-control"><SearchIcon aria-hidden="true" /><input type="search" aria-label="搜索合集视频" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、BV号或第几集" /></label>
            <div className="collection-select-control"><span>排序</span><CollectionToolbarSelect ariaLabel="排序" value={sortMode} options={COLLECTION_SORT_LABELS} onChange={changeSort} /></div>
            <div className="collection-select-control"><span>分组</span><CollectionToolbarSelect ariaLabel="分组" value={groupMode} options={COLLECTION_GROUP_LABELS} onChange={changeGroup} /></div>
            {canCustomReorder ? <button className="secondary-button compact-button" type="button" disabled={busy} onClick={() => void restoreSourceOrder()}>恢复原顺序</button> : null}
            <button className="secondary-button compact-button" type="button" onClick={selectionMode ? exitSelectionMode : enterSelectionMode}>{selectionMode ? "退出选择" : "选择"}</button>
          </div>
        </div>
      </section>

      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={(event) => void handleCustomDragEnd(event)}>
        <SortableContext items={canCustomReorder ? view.visibleItems.map((item) => item.bvid) : []} strategy={rectSortingStrategy}>
          {view.visibleItems.length ? view.groups.map((group) => <CollectionGroupSection
            key={group.key}
            group={group}
            showHeading={groupMode !== "none"}
            collectionTimestamp={collection.last_checked_at}
            canCustomReorder={canCustomReorder}
            selectionMode={selectionMode}
            selectionDragActive={selectionDragActive}
            selectedIds={selectedIds}
            onToggleSelected={toggleSelected}
            onSelectionPointerDown={beginSelectionDrag}
            onContextMenu={(event, item) => { event.preventDefault(); setContextMenu({ x: event.clientX, y: event.clientY, item }); }}
          />) : <div className="collection-detail-empty collection-view-empty"><strong>没有符合条件的视频</strong><span>{hasViewConstraint ? "尝试清除搜索或状态筛选。" : "这个合集暂时没有可显示的视频。"}</span>{hasViewConstraint ? <button className="secondary-button compact-button" type="button" onClick={() => { setStatusFilter("all"); setQuery(""); }}>清除筛选</button> : null}</div>}
        </SortableContext>
      </DndContext>

      {selectionMode ? <div className="collection-selection-dock" role="toolbar" aria-label="合集批量操作">
        <span className="collection-selection-count">已选 {selectedIds.size} 个{hiddenSelectedCount ? ` · 隐藏 ${hiddenSelectedCount}` : ""}</span>
        <div className="collection-queue-order"><span>队列顺序</span><CollectionToolbarSelect ariaLabel="任务队列顺序" value={queueOrder} options={COLLECTION_QUEUE_ORDER_LABELS} onChange={setQueueOrder} /></div>
        <button type="button" onClick={selectAllSummarizable} disabled={busy}>全选可总结</button>
        <button type="button" onClick={() => { setSelectedIds(new Set()); setSelectionOrder([]); }} disabled={busy}>清空</button>
        <button className="is-primary" type="button" disabled={busy || selectedIds.size === 0} onClick={() => void summarizeSelected()}>{busy ? "正在创建…" : "一键总结已选"}</button>
      </div> : null}

      {contextMenu ? <div className="library-video-context-menu collection-detail-context-menu" style={{ left: contextMenu.x, top: contextMenu.y }} role="menu" aria-label={`${contextMenu.item.title} 的操作菜单`} onClick={(event) => event.stopPropagation()} onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); }}>
        <div className="library-video-menu-heading"><strong>{contextMenu.item.title}</strong><span>合集内 · 第 {contextMenu.item.position} 集</span></div>
        {contextMenu.item.video_id ? <button type="button" role="menuitem" disabled={busy} onClick={() => void promoteItem(contextMenu.item)}>添加到外层视频列表</button> : <span className="collection-detail-context-empty">该视频还未进入任务库</span>}
      </div> : null}
    </section>
  );
}

function CollectionToolbarSelect<T extends string>({ ariaLabel, value, options, iconOnly = false, disabled = false, onChange }: {
  ariaLabel: string;
  value: T;
  options: Record<T, string>;
  iconOnly?: boolean;
  disabled?: boolean;
  onChange(value: T): void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const openFocusIndexRef = useRef<number | null>(null);
  const values = Object.keys(options) as T[];

  useEffect(() => {
    if (!open) return;
    const close = (event: globalThis.MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const selectedIndex = openFocusIndexRef.current ?? Math.max(0, values.indexOf(value));
    openFocusIndexRef.current = null;
    const frame = window.requestAnimationFrame(() => optionRefs.current[selectedIndex]?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [open, value]);

  function openFromKeyboard(event: ReactKeyboardEvent<HTMLButtonElement>, edge: "first" | "last") {
    event.preventDefault();
    openFocusIndexRef.current = edge === "first" ? 0 : values.length - 1;
    setOpen(true);
  }

  function closeAndRestoreFocus() {
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }

  function moveOptionFocus(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (index + 1) % values.length;
    else if (event.key === "ArrowUp") nextIndex = (index - 1 + values.length) % values.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = values.length - 1;
    else if (event.key === "Escape") {
      event.preventDefault();
      closeAndRestoreFocus();
      return;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    optionRefs.current[nextIndex]?.focus();
  }

  return <div ref={rootRef} className={`collection-toolbar-select${open ? " is-open" : ""}`}>
    <button
      ref={triggerRef}
      className={`collection-toolbar-select-trigger${iconOnly ? " is-icon-only" : ""}`}
      type="button"
      aria-label={iconOnly ? `${ariaLabel}：${options[value]}` : ariaLabel}
      title={iconOnly ? options[value] : undefined}
      aria-haspopup="menu"
      aria-expanded={open}
      disabled={disabled}
      onClick={() => setOpen((current) => !current)}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown") openFromKeyboard(event, "first");
        else if (event.key === "ArrowUp") openFromKeyboard(event, "last");
        else if (event.key === "Escape") setOpen(false);
      }}
    >
      {iconOnly ? null : <span>{options[value]}</span>}
      <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4.5 6.25 3.5 3.5 3.5-3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </button>
    {open ? <div className="collection-toolbar-select-menu" role="menu" aria-label={ariaLabel}>
      {values.map((optionValue, index) => <button
        ref={(element) => { optionRefs.current[index] = element; }}
        key={optionValue}
        type="button"
        role="menuitemradio"
        aria-checked={optionValue === value}
        className={optionValue === value ? "is-selected" : ""}
        onKeyDown={(event) => moveOptionFocus(event, index)}
        onClick={() => { onChange(optionValue); closeAndRestoreFocus(); }}
      >
        <span>{options[optionValue]}</span>
        {optionValue === value ? <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3 8.25 3.1 3.1L13 4.75" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg> : null}
      </button>)}
    </div> : null}
  </div>;
}

function CollectionOwner({ collection, ownerUrl, ownerFaceFailed, onOwnerFaceError }: { collection: VideoCollection; ownerUrl: string; ownerFaceFailed: boolean; onOwnerFaceError(): void }) {
  const identity = <>{collection.owner_face && !ownerFaceFailed ? <img src={collection.owner_face} alt="" loading="lazy" onError={onOwnerFaceError} /> : <span className="collection-owner-avatar" aria-hidden="true">{(collection.owner_name || "UP").slice(0, 1)}</span>}<span className="collection-owner-copy"><small>UP主</small><strong>{collection.owner_name || `UID ${collection.owner_mid || "未知"}`}</strong></span>{ownerUrl ? <ExternalLinkIcon /> : null}</>;
  return ownerUrl ? <a className="collection-owner-link" href={ownerUrl} target="_blank" rel="noreferrer" aria-label={`打开 ${collection.owner_name || "UP主"} 的空间`}>{identity}</a> : <div className="collection-owner-static">{identity}</div>;
}

function CollectionGroupSection({
  group,
  showHeading,
  collectionTimestamp,
  canCustomReorder,
  selectionMode,
  selectionDragActive,
  selectedIds,
  onToggleSelected,
  onSelectionPointerDown,
  onContextMenu,
}: {
  group: CollectionViewGroup;
  showHeading: boolean;
  collectionTimestamp?: string | null;
  canCustomReorder: boolean;
  selectionMode: boolean;
  selectionDragActive: boolean;
  selectedIds: Set<string>;
  onToggleSelected(videoId: string): void;
  onSelectionPointerDown(videoId: string, event: ReactPointerEvent<HTMLElement>): void;
  onContextMenu(event: MouseEvent, item: VideoCollectionItem): void;
}) {
  return <section className="collection-detail-bucket">
    {showHeading ? <div className="collection-detail-bucket-head"><strong>{group.title}</strong><div className="collection-detail-bucket-head-actions"><span>{group.items.length}</span></div></div> : null}
    <div className={`video-grid collection-detail-grid${selectionDragActive ? " is-selection-dragging" : ""}`}>
      {group.items.map((item) => <SortableCollectionCard
        key={item.bvid}
        item={item}
        collectionTimestamp={collectionTimestamp}
        canCustomReorder={canCustomReorder}
        selectionMode={selectionMode}
        selectedIds={selectedIds}
        onToggleSelected={onToggleSelected}
        onSelectionPointerDown={onSelectionPointerDown}
        onContextMenu={onContextMenu}
      />)}
    </div>
  </section>;
}

function SortableCollectionCard({ item, collectionTimestamp, canCustomReorder, selectionMode, selectedIds, onToggleSelected, onSelectionPointerDown, onContextMenu }: {
  item: VideoCollectionItem;
  collectionTimestamp?: string | null;
  canCustomReorder: boolean;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected(videoId: string): void;
  onSelectionPointerDown(videoId: string, event: ReactPointerEvent<HTMLElement>): void;
  onContextMenu(event: MouseEvent, item: VideoCollectionItem): void;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id: item.bvid, disabled: !canCustomReorder });
  const style = { transform: DndCSS.Transform.toString(transform), transition };
  const video = item.video_id ? buildCollectionVideo(item, collectionTimestamp) : null;
  const selectable = Boolean(item.video_id) && isCollectionItemSummarizable(item);
  return <div ref={setNodeRef} style={style} className={`collection-sortable-card${isDragging ? " is-dragging" : ""}${item.is_new ? " is-new" : ""}`}>
    {canCustomReorder ? <button ref={setActivatorNodeRef} className="collection-custom-drag-handle" type="button" title="拖动调整自定义顺序" aria-label={`拖动 ${item.title} 调整顺序`} {...attributes} {...listeners}><svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="5" cy="4" r="1" /><circle cx="11" cy="4" r="1" /><circle cx="5" cy="8" r="1" /><circle cx="11" cy="8" r="1" /><circle cx="5" cy="12" r="1" /><circle cx="11" cy="12" r="1" /></svg></button> : null}
    {item.is_new ? <span className="collection-new-item-badge">新</span> : null}
    {video ? <VideoCard
      video={video}
      metaLabel={`第 ${item.position} 集`}
      selection={selectable && selectionMode && item.video_id ? {
        id: item.video_id,
        checked: selectedIds.has(item.video_id),
        disabled: false,
        onChange: () => undefined,
        onToggle: () => onToggleSelected(item.video_id as string),
        onPointerDown: (event) => onSelectionPointerDown(item.video_id as string, event),
      } : undefined}
      onOpenContextMenu={(event) => onContextMenu(event, item)}
    /> : <article className="video-card collection-unresolved-card" onContextMenu={(event) => onContextMenu(event, item)}><div className="video-card-cover">{item.cover_url ? <img src={item.cover_url} alt="" loading="lazy" /> : <div className="video-card-placeholder">待加入</div>}</div><div className="video-card-body"><div className="video-card-topline"><div className="video-card-badges"><span className="video-platform-badge is-bilibili">Bilibili</span></div><span className="task-status status-pending">未入库</span></div><h3>{item.title}</h3><div className="video-card-meta"><span>第 {item.position} 集</span><span className="video-card-result-state">等待加入合集</span></div><div className="video-card-folder">刷新后可添加</div></div></article>}
  </div>;
}

function buildCollectionVideo(item: VideoCollectionItem, collectionTimestamp?: string | null): VideoAssetSummary {
  const timestamp = item.asset_updated_at || collectionTimestamp || "";
  return {
    video_id: item.video_id || item.bvid,
    canonical_id: item.bvid,
    platform: "bilibili",
    title: item.title,
    source_url: item.source_url,
    cover_url: item.cover_url,
    duration: item.duration,
    latest_task_id: null,
    latest_status: item.latest_status || null,
    latest_stage: null,
    has_result: Boolean(item.has_result),
    is_favorite: false,
    favorite_updated_at: null,
    folder_id: null,
    folder_ids: [],
    global_order: 0,
    folder_order: 0,
    global_pinned: false,
    folder_pinned: false,
    pages: [],
    created_at: item.last_summary_at || timestamp,
    updated_at: timestamp,
  };
}
