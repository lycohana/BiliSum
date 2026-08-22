import { useEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent as ReactPointerEvent, type SyntheticEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { ExternalLinkIcon } from "../components/AppIcons";
import { VideoCard } from "../components/VideoCard";
import type { VideoAssetSummary, VideoCollection, VideoCollectionItem } from "../types";

type CollectionDetailPageProps = {
  serviceOnline: boolean;
  onRefreshCollection(collectionId: string): Promise<VideoCollection>;
  onAddCollectionItems(collectionId: string, bvids: string[]): Promise<VideoCollection>;
  onUpdateCollectionSettings(collectionId: string, autoCheckOnOpen: boolean): Promise<VideoCollection>;
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

function readableCollectionError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message.trim() : "";
  if (!message || /^Internal Server Error$/i.test(message)) {
    return fallback;
  }
  if (/LLM returned no readable message content/i.test(message)) {
    return "LLM 没有返回可读取的内容，已按设置进行重试；请检查模型响应格式。";
  }
  return message;
}

export function CollectionDetailPage({
  serviceOnline,
  onRefreshCollection,
  onAddCollectionItems,
  onUpdateCollectionSettings,
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
  const [ownerFaceFailed, setOwnerFaceFailed] = useState(false);
  const [newNoticeDismissed, setNewNoticeDismissed] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; item: VideoCollectionItem } | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [selectionDragActive, setSelectionDragActive] = useState(false);
  const selectionDragRef = useRef<CollectionSelectionDrag | null>(null);
  const suppressNextSelectionClickRef = useRef(false);

  const summarizedItems = useMemo(() => collection?.items.filter((item) => (
    item.has_result || item.latest_status === "queued" || item.latest_status === "running"
  )) || [], [collection]);
  const unsummarizedItems = useMemo(() => collection?.items.filter((item) => (
    !item.has_result && item.latest_status !== "queued" && item.latest_status !== "running"
  )) || [], [collection]);
  const newItems = collection?.new_items || [];
  const ownerUrl = collection?.owner_url || (collection?.owner_mid ? `https://space.bilibili.com/${collection.owner_mid}` : "");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErrorMessage("");
    async function load() {
      try {
        const response = await api.getVideoCollection(collectionId);
        if (cancelled) return;
        setOwnerFaceFailed(false);
        setCollection(response);
        setLoading(false);
        if (response.auto_check_on_open !== false) {
          setRefreshing(true);
          try {
            const refreshed = await onRefreshCollection(collectionId);
            if (!cancelled) {
              setOwnerFaceFailed(false);
              setCollection(refreshed);
            }
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
    return () => {
      cancelled = true;
    };
  }, [collectionId]);

  useEffect(() => {
    setSelectedIds(new Set());
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
    window.addEventListener("click", closeDeletePopover, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeDeletePopover, true);
      window.removeEventListener("click", closeDeletePopover, true);
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
  }

  function toggleSelected(videoId: string) {
    if (suppressNextSelectionClickRef.current) {
      suppressNextSelectionClickRef.current = false;
      return;
    }
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(videoId)) next.delete(videoId);
      else next.add(videoId);
      return next;
    });
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

  function selectAllUnsummarized() {
    setSelectionMode(true);
    setSelectedIds(new Set(unsummarizedItems.map((item) => item.video_id).filter((id): id is string => Boolean(id))));
  }

  function enterSelectionMode() {
    setSelectionMode(true);
    setSelectedIds(new Set());
    selectionDragRef.current = null;
    setSelectionDragActive(false);
  }

  function exitSelectionMode() {
    setSelectionMode(false);
    setSelectedIds(new Set());
  }

  async function summarizeSelected() {
    const validIds = new Set(unsummarizedItems.map((item) => item.video_id).filter((id): id is string => Boolean(id)));
    const videoIds = [...selectedIds].filter((id) => validIds.has(id));
    if (!videoIds.length) return;
    setBusy(true);
    setErrorMessage("");
    try {
      await onSummarizeCollectionItems(collectionId, videoIds);
      setSelectedIds(new Set());
      const refreshed = await api.getVideoCollection(collectionId);
      setCollection(refreshed);
    } catch (error) {
      setErrorMessage(readableCollectionError(error, "批量总结任务创建失败，请稍后重试。"));
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

  async function toggleAutoCheck() {
    if (!collection) return;
    setBusy(true);
    setErrorMessage("");
    try {
      setCollection(await onUpdateCollectionSettings(collectionId, collection.auto_check_on_open === false));
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

  return (
    <section className={`collection-detail-page detail-page-shell${selectionMode ? " is-selection-mode" : ""}`}>
      <div className="detail-page-toolbar collection-detail-toolbar-top">
        <Link className="detail-back-button" to="/library">
          <svg className="detail-back-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M10.5 3 5.5 8l5 5M6 8h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          返回视频库
        </Link>
        <span className="collection-detail-auto-status">{refreshing ? "正在检查合集更新…" : collection.auto_check_on_open === false ? "自动检查已关闭" : "打开时自动检查"}</span>
      </div>
      <header className="collection-detail-hero video-detail-hero">
        <div className="collection-detail-hero-cover video-detail-cover">
          {collection.cover_url ? (
            <>
              <img src={collection.cover_url} alt={collection.title} onError={handleImageError} />
              <div className="collection-detail-placeholder is-hidden">合集</div>
            </>
          ) : <div className="collection-detail-placeholder">合集</div>}
          <div className="collection-count-badge" aria-label={`${collection.item_count} 个视频`}><strong>{collection.item_count}</strong><span>集</span></div>
        </div>
        <div className="collection-detail-hero-copy video-detail-copy">
          <div className="video-card-badges"><span className="video-platform-badge is-bilibili">Bilibili</span><span className="video-page-badge">合集</span></div>
          <h1>{collection.title}</h1>
          <p>{collection.item_count} 个视频 · 已总结 {summarizedItems.length} · 未总结 {unsummarizedItems.length}</p>
          {ownerUrl || collection.owner_name ? <CollectionOwner collection={collection} ownerUrl={ownerUrl} ownerFaceFailed={ownerFaceFailed} onOwnerFaceError={() => setOwnerFaceFailed(true)} /> : null}
          <div className="collection-delete-anchor">
            <div className="collection-detail-actions">
              <button className="secondary-button compact-button" type="button" disabled={refreshing} onClick={() => void refreshCollection()}>刷新列表</button>
              <button className="secondary-button compact-button" type="button" disabled={busy} onClick={() => void toggleAutoCheck()}>{collection.auto_check_on_open === false ? "开启自动检查" : "关闭自动检查"}</button>
              <button className="secondary-button compact-button danger-button" type="button" aria-expanded={deleteConfirmOpen} aria-controls="collection-delete-popover" disabled={busy} onClick={() => setDeleteConfirmOpen((open) => !open)}>删除合集</button>
            </div>
            {deleteConfirmOpen ? (
              <div className="collection-delete-panel" id="collection-delete-popover" role="dialog" aria-label="删除合集">
                <strong>删除这个合集</strong>
                <p>仅移除合集会把内部视频恢复到总视频库；删除全部内容会同时移除视频、任务和摘要。</p>
                <div><button className="secondary-button compact-button" type="button" disabled={busy} onClick={() => void deleteCollection("detach")}>仅移除合集</button><button className="primary-button compact-button danger-button" type="button" disabled={busy} onClick={() => void deleteCollection("delete_contents")}>删除全部内容</button></div>
              </div>
            ) : null}
          </div>
        </div>
      </header>

      {newItems.length && !newNoticeDismissed ? (
        <div className="collection-detail-new-notice" role="status">
          <span>发现 {newItems.length} 个新视频，是否添加到这个合集？</span>
          <div><button type="button" disabled={busy} onClick={() => void addNewItems()}>添加</button><button type="button" className="is-quiet" onClick={() => setNewNoticeDismissed(true)}>稍后</button></div>
        </div>
      ) : null}
      {errorMessage ? <p className="collection-detail-error" role="alert">{errorMessage}</p> : null}

      <div className="collection-detail-toolbar">
        <div><strong>合集内容</strong><span>视频默认只在合集内显示，可右键提升到外层视频库。</span></div>
        <div className="collection-detail-selection-entry">
          <span>{collection.item_count} 个视频 · 已总结优先</span>
          {selectionMode ? <button className="secondary-button compact-button" type="button" onClick={exitSelectionMode}>退出选择</button> : null}
        </div>
      </div>

      <CollectionBucket title="已总结" tone="done" items={summarizedItems} collectionTimestamp={collection.last_checked_at} selectionMode={false} selectable={false} selectionDragActive={false} showSelectionAction={false} selectedIds={selectedIds} onToggleSelected={toggleSelected} onEnterSelectionMode={enterSelectionMode} onSelectionPointerDown={beginSelectionDrag} onContextMenu={(event, item) => { event.preventDefault(); setContextMenu({ x: event.clientX, y: event.clientY, item }); }} />
      <CollectionBucket title="未总结" tone="pending" items={unsummarizedItems} collectionTimestamp={collection.last_checked_at} selectionMode={selectionMode} selectable selectionDragActive={selectionDragActive} showSelectionAction={!selectionMode && unsummarizedItems.length > 0} selectedIds={selectedIds} onToggleSelected={toggleSelected} onEnterSelectionMode={enterSelectionMode} onSelectionPointerDown={beginSelectionDrag} onContextMenu={(event, item) => { event.preventDefault(); setContextMenu({ x: event.clientX, y: event.clientY, item }); }} />

      {selectionMode ? (
        <div className="collection-selection-dock" role="toolbar" aria-label="合集批量操作">
          <span className="collection-selection-count">已选 {selectedIds.size} 个</span>
          <button type="button" onClick={selectAllUnsummarized} disabled={busy}>全选未总结</button>
          <button type="button" onClick={() => setSelectedIds(new Set())} disabled={busy}>清空</button>
          <button className="is-primary" type="button" disabled={busy || selectedIds.size === 0} onClick={() => void summarizeSelected()}>{busy ? "正在创建…" : "一键总结已选"}</button>
        </div>
      ) : null}

      {contextMenu ? (
        <div
          className="library-video-context-menu collection-detail-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          role="menu"
          aria-label={`${contextMenu.item.title} 的操作菜单`}
          onClick={(event) => event.stopPropagation()}
          onContextMenu={(event) => {
            event.preventDefault();
            event.stopPropagation();
          }}
        >
          <div className="library-video-menu-heading">
            <strong>{contextMenu.item.title}</strong>
            <span>合集内 · 第 {contextMenu.item.position} 集</span>
          </div>
          {contextMenu.item.video_id ? (
            <button type="button" role="menuitem" disabled={busy} onClick={() => void promoteItem(contextMenu.item)}>添加到外层视频列表</button>
          ) : <span className="collection-detail-context-empty">该视频还未进入任务库</span>}
        </div>
      ) : null}
    </section>
  );
}

function CollectionOwner({
  collection,
  ownerUrl,
  ownerFaceFailed,
  onOwnerFaceError,
}: {
  collection: VideoCollection;
  ownerUrl: string;
  ownerFaceFailed: boolean;
  onOwnerFaceError(): void;
}) {
  const identity = (
    <>
      {collection.owner_face && !ownerFaceFailed ? (
        <img src={collection.owner_face} alt="" loading="lazy" onError={onOwnerFaceError} />
      ) : (
        <span className="collection-owner-avatar" aria-hidden="true">{(collection.owner_name || "UP").slice(0, 1)}</span>
      )}
      <span className="collection-owner-copy"><small>UP主</small><strong>{collection.owner_name || `UID ${collection.owner_mid || "未知"}`}</strong></span>
      {ownerUrl ? <ExternalLinkIcon /> : null}
    </>
  );

  return ownerUrl ? (
    <a className="collection-owner-link" href={ownerUrl} target="_blank" rel="noreferrer" aria-label={`打开 ${collection.owner_name || "UP主"} 的空间`}>
      {identity}
    </a>
  ) : (
    <div className="collection-owner-static">{identity}</div>
  );
}

function CollectionBucket({
  title,
  tone,
  items,
  collectionTimestamp,
  selectionMode,
  selectable,
  selectionDragActive,
  showSelectionAction,
  selectedIds,
  onToggleSelected,
  onEnterSelectionMode,
  onSelectionPointerDown,
  onContextMenu,
}: {
  title: string;
  tone: "pending" | "done";
  items: VideoCollectionItem[];
  collectionTimestamp?: string | null;
  selectionMode: boolean;
  selectable: boolean;
  selectionDragActive: boolean;
  showSelectionAction: boolean;
  selectedIds: Set<string>;
  onToggleSelected(videoId: string): void;
  onEnterSelectionMode(): void;
  onSelectionPointerDown(videoId: string, event: ReactPointerEvent<HTMLElement>): void;
  onContextMenu(event: MouseEvent, item: VideoCollectionItem): void;
}) {
  return (
    <section className={`collection-detail-bucket is-${tone}`}>
      <div className="collection-detail-bucket-head">
        <strong>{title}</strong>
        <div className="collection-detail-bucket-head-actions">
          {showSelectionAction ? <button className="collection-bucket-select-button" type="button" onClick={onEnterSelectionMode}>选择视频</button> : null}
          <span>{items.length}</span>
        </div>
      </div>
      {items.length ? (
        <div className={`video-grid collection-detail-grid ${selectionDragActive ? "is-selection-dragging" : ""}`.trim()}>
          {items.map((item) => {
            if (!item.video_id) return null;
            const videoId = item.video_id;
            const video = buildCollectionVideo(item, collectionTimestamp);
            return video ? (
              <VideoCard
                key={`${item.bvid}-${item.position}`}
                video={video}
                folderName={`合集内 · 第 ${item.position} 集`}
                metaLabel={`第 ${item.position} 集`}
                selection={selectable && selectionMode ? {
                  id: videoId,
                  checked: selectedIds.has(videoId),
                  disabled: false,
                  onChange: () => undefined,
                  onToggle: () => onToggleSelected(videoId),
                  onPointerDown: (event) => onSelectionPointerDown(videoId, event),
                } : undefined}
                onOpenContextMenu={(event) => onContextMenu(event, item)}
              />
            ) : (
              <article className="video-card collection-unresolved-card" key={`${item.bvid}-${item.position}`} onContextMenu={(event) => onContextMenu(event, item)}>
                <div className="video-card-cover"><div className="video-card-placeholder">待加入</div></div>
                <div className="video-card-body">
                  <div className="video-card-topline"><div className="video-card-badges"><span className="video-platform-badge is-bilibili">Bilibili</span></div><span className="task-status status-pending">未入库</span></div>
                  <h3>{item.title}</h3>
                  <div className="video-card-meta"><span>第 {item.position} 集</span><span className="video-card-result-state">等待加入合集</span></div>
                  <div className="video-card-folder">刷新后可添加</div>
                </div>
              </article>
            );
          })}
        </div>
      ) : <p className="collection-detail-empty">这里暂时没有视频。</p>}
    </section>
  );
}

function buildCollectionVideo(item: VideoCollectionItem, collectionTimestamp?: string | null): VideoAssetSummary {
  const timestamp = collectionTimestamp || "";
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
    created_at: timestamp,
    updated_at: timestamp,
  };
}
