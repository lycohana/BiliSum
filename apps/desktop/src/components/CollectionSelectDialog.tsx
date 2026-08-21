import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import type { VideoAssetSummary, VideoCollection, VideoCollectionItem } from "../types";
import { formatDuration } from "../utils";

type CollectionSelectDialogProps = {
  isOpen: boolean;
  video: VideoAssetSummary | null;
  collection: VideoCollection | null;
  onClose(): void;
  onChoose(scope: "single" | "collection", items?: VideoCollectionItem[]): void;
};

type CollectionSelectView = "grid" | "list";

type CollectionSelectionDrag = {
  pointerId: number;
  bvid: string;
  select: boolean;
  moved: boolean;
  startX: number;
  startY: number;
  lastBvid: string | null;
};

export function CollectionSelectDialog({
  isOpen,
  video,
  collection,
  onClose,
  onChoose,
}: CollectionSelectDialogProps) {
  const [selectedBvids, setSelectedBvids] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<CollectionSelectView>("grid");
  const [heroImageFailed, setHeroImageFailed] = useState(false);
  const [selectionDragActive, setSelectionDragActive] = useState(false);
  const selectionDragRef = useRef<CollectionSelectionDrag | null>(null);
  const suppressNextSelectionClickRef = useRef(false);
  const selectionInitKeyRef = useRef("");

  useEffect(() => {
    if (!isOpen || !collection || !video) {
      selectionInitKeyRef.current = "";
      return;
    }
    const selectionInitKey = `${collection.collection_id}:${video.canonical_id}:${collection.items.length}`;
    if (selectionInitKeyRef.current === selectionInitKey) return;
    selectionInitKeyRef.current = selectionInitKey;
    setSelectedBvids(getInitialSelectionBvids(video, collection));
    setViewMode("grid");
    setHeroImageFailed(false);
    selectionDragRef.current = null;
    setSelectionDragActive(false);
  }, [isOpen, collection, video]);

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const drag = selectionDragRef.current;
      if (!drag || drag.pointerId !== event.pointerId || event.buttons !== 1) return;

      const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
      if (!drag.moved && distance < 4) return;
      drag.moved = true;

      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-collection-selection-id]");
      const bvid = target?.dataset.collectionSelectionId;
      if (!bvid || bvid === drag.lastBvid) return;

      drag.lastBvid = bvid;
      setSelectedState(drag.bvid, drag.select);
      setSelectedState(bvid, drag.select);
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

  if (!isOpen || !video || !collection) {
    return null;
  }

  const collectionItems = collection.items;
  const heroImageUrl = collection.cover_url || video.cover_url;
  const displayedSelectedBvids = selectedBvids.length || selectionInitKeyRef.current
    ? selectedBvids
    : getInitialSelectionBvids(video, collection);
  const selectedBvidSet = new Set(displayedSelectedBvids);
  const selectedItems = collectionItems.filter((item) => selectedBvidSet.has(item.bvid));
  const hasCurrentItem = collectionItems.some((item) => item.is_current);

  function setSelectedState(bvid: string, selected: boolean) {
    setSelectedBvids((current) => {
      const hasItem = current.includes(bvid);
      if (hasItem === selected) return current;
      return selected ? [...current, bvid] : current.filter((item) => item !== bvid);
    });
  }

  function toggleItem(bvid: string) {
    if (suppressNextSelectionClickRef.current) {
      suppressNextSelectionClickRef.current = false;
      return;
    }
    setSelectedBvids((current) => (
      current.includes(bvid)
        ? current.filter((item) => item !== bvid)
        : [...current, bvid]
    ));
  }

  function beginSelectionDrag(bvid: string, event: ReactPointerEvent<HTMLElement>) {
    if (!isOpen || event.button !== 0) return;
    event.stopPropagation();
    suppressNextSelectionClickRef.current = false;
    selectionDragRef.current = {
      pointerId: event.pointerId,
      bvid,
      select: !selectedBvidSet.has(bvid),
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
      lastBvid: null,
    };
    setSelectionDragActive(true);
  }

  function selectAll() {
    setSelectedBvids(collectionItems.map((item) => item.bvid));
  }

  function clearSelection() {
    setSelectedBvids([]);
  }

  return (
    <div className="update-dialog-overlay" onClick={onClose}>
      <div className="update-dialog collection-select-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="update-dialog-header">
          <div>
            <span className="section-kicker">检测到 B 站合集</span>
            <h2>选择要总结的合集视频</h2>
          </div>
          <button className="close-button" type="button" onClick={onClose} aria-label="关闭">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="update-dialog-body collection-select-body">
          <div className="collection-select-hero">
            {heroImageUrl && !heroImageFailed ? (
              <img src={heroImageUrl} alt={collection.title} onError={() => setHeroImageFailed(true)} />
            ) : (
              <div className="collection-select-placeholder" aria-hidden="true">合集</div>
            )}
            <div className="collection-select-copy">
              <strong>{collection.title}</strong>
              <p>当前视频属于这个合集，共 {collection.item_count || collection.items.length} 个视频。</p>
              <span>已预选当前视频，可拖动卡片连续选择其他内容。</span>
            </div>
          </div>

          <div className="collection-select-toolbar">
            <div className="collection-select-toolbar-copy">
              <strong>选择范围</strong>
              <span>已选 {selectedItems.length} / {collection.items.length} 个视频{hasCurrentItem ? " · 已预选当前视频" : ""}</span>
            </div>
            <div className="collection-select-toolbar-actions">
              <span className="collection-select-drag-hint">拖动卡片可连续选择</span>
              <div className="collection-select-view-toggle" role="group" aria-label="内容排列方式">
                <button className={viewMode === "grid" ? "is-active" : ""} type="button" aria-pressed={viewMode === "grid"} onClick={() => setViewMode("grid")}>网格</button>
                <button className={viewMode === "list" ? "is-active" : ""} type="button" aria-pressed={viewMode === "list"} onClick={() => setViewMode("list")}>列表</button>
              </div>
            </div>
          </div>

          <div className={`collection-select-items is-${viewMode}${selectionDragActive ? " is-selection-dragging" : ""}`}>
            {viewMode === "grid" ? collectionItems.map((item) => (
              <CollectionSelectCard
                key={`${item.bvid}-${item.position}`}
                item={item}
                selected={selectedBvidSet.has(item.bvid)}
                onToggle={() => toggleItem(item.bvid)}
                onPointerDown={(event) => beginSelectionDrag(item.bvid, event)}
              />
            )) : collectionItems.map((item) => (
              <CollectionSelectListItem
                key={`${item.bvid}-${item.position}`}
                item={item}
                selected={selectedBvidSet.has(item.bvid)}
                onToggle={() => toggleItem(item.bvid)}
                onPointerDown={(event) => beginSelectionDrag(item.bvid, event)}
              />
            ))}
          </div>
        </div>

        <div className="update-dialog-footer collection-select-footer">
          <button className="secondary-button" type="button" onClick={() => onChoose("single")}>
            仅总结当前视频
          </button>
        </div>
        <div className="collection-select-selection-dock" role="toolbar" aria-label="合集批量选择操作">
          <span>{selectedItems.length ? `已选 ${selectedItems.length} 个` : "请选择视频"}</span>
          <button type="button" onClick={selectAll} disabled={selectedItems.length === collectionItems.length}>全选</button>
          <button type="button" onClick={clearSelection} disabled={!selectedItems.length}>清空</button>
          <button className="is-primary" type="button" disabled={!selectedItems.length} onClick={() => onChoose("collection", selectedItems)}>
            {selectedItems.length ? "开始总结" : "请选择视频"}
          </button>
        </div>
      </div>
    </div>
  );
}

function CollectionSelectCard({
  item,
  selected,
  onToggle,
  onPointerDown,
}: {
  item: VideoCollectionItem;
  selected: boolean;
  onToggle(): void;
  onPointerDown(event: ReactPointerEvent<HTMLElement>): void;
}) {
  return (
    <button
      className={`collection-select-card${selected ? " is-selected" : ""}${item.is_current ? " is-current" : ""}`}
      data-collection-selection-id={item.bvid}
      type="button"
      aria-pressed={selected}
      onPointerDown={onPointerDown}
      onClick={onToggle}
    >
      <span className="collection-select-card-cover">
        <span className={`collection-select-card-check${selected ? " is-checked" : ""}`} aria-hidden="true">{selected ? "✓" : ""}</span>
        <span className="collection-select-card-fallback" aria-hidden="true">B站</span>
        {item.cover_url ? <img src={item.cover_url} alt="" loading="lazy" onError={(event) => { event.currentTarget.style.opacity = "0"; }} /> : null}
        {item.duration != null ? <span className="collection-select-card-duration">{formatDuration(item.duration)}</span> : null}
      </span>
      <span className="collection-select-card-body">
        <span className="collection-select-card-topline">
          <span className="collection-select-card-badge">Bilibili</span>
          <span className={`collection-select-card-status ${item.has_result ? "is-done" : "is-pending"}`}>{item.has_result ? "已总结" : "未总结"}</span>
        </span>
        <strong>{item.title}</strong>
      </span>
      <span className="collection-select-card-episode">第 {item.position} 集{item.is_current ? " · 当前视频" : ""}</span>
    </button>
  );
}

function CollectionSelectListItem({
  item,
  selected,
  onToggle,
  onPointerDown,
}: {
  item: VideoCollectionItem;
  selected: boolean;
  onToggle(): void;
  onPointerDown(event: ReactPointerEvent<HTMLElement>): void;
}) {
  return (
    <button
      className={`collection-select-list-item${selected ? " is-selected" : ""}${item.is_current ? " is-current" : ""}`}
      data-collection-selection-id={item.bvid}
      type="button"
      aria-pressed={selected}
      onPointerDown={onPointerDown}
      onClick={onToggle}
    >
      <input
        className="collection-select-list-check"
        type="checkbox"
        checked={selected}
        aria-label={`选择 ${item.title}`}
        onPointerDown={(event) => {
          event.stopPropagation();
          onPointerDown(event);
        }}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onToggle();
        }}
        onChange={() => undefined}
      />
      <span className="collection-select-list-thumb">
        <span aria-hidden="true">B</span>
        {item.cover_url ? <img src={item.cover_url} alt="" loading="lazy" onError={(event) => { event.currentTarget.style.opacity = "0"; }} /> : null}
      </span>
      <span className="collection-select-list-copy">
        <strong>{item.title}</strong>
        <small>第 {item.position} 集 · {formatDuration(item.duration)}{item.is_current ? " · 当前视频" : ""}</small>
      </span>
      <span className={`collection-select-list-status ${item.has_result ? "is-done" : "is-pending"}`}>{item.has_result ? "已总结" : "未总结"}</span>
    </button>
  );
}

function getInitialSelectionBvids(video: VideoAssetSummary, collection: VideoCollection) {
  const currentItems = collection.items.filter((item) => item.is_current);
  if (currentItems.length) {
    return currentItems.map((item) => item.bvid);
  }
  const currentVideoItem = collection.items.find((item) => (
    item.bvid === video.canonical_id
    || item.source_url === video.source_url
    || item.title === video.title
  ));
  return currentVideoItem ? [currentVideoItem.bvid] : [];
}
