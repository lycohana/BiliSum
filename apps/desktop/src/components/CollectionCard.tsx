import { Link } from "react-router-dom";
import type { CSSProperties, MouseEvent, SyntheticEvent } from "react";

import { platformLabel } from "../appModel";
import { GripVerticalIcon, PinIcon, StarIcon } from "./AppIcons";
import type { VideoCollection } from "../types";
import { formatDateTime } from "../utils";

export function CollectionCard({
  collection,
  canPinInFolder = false,
  onOpenContextMenu,
  onToggleFavorite,
  onToggleGlobalPin,
  onToggleFolderPin,
}: {
  collection: VideoCollection;
  canPinInFolder?: boolean;
  onOpenContextMenu?: (event: MouseEvent, collectionId: string) => void;
  onToggleFavorite?: (collectionId: string, nextFavorite: boolean) => Promise<unknown>;
  onToggleGlobalPin?: (collectionId: string, nextPinned: boolean) => Promise<unknown>;
  onToggleFolderPin?: (collectionId: string, nextPinned: boolean) => Promise<unknown>;
}) {
  function handleImageError(event: SyntheticEvent<HTMLImageElement>) {
    const target = event.target as HTMLImageElement;
    target.style.display = "none";
    target.parentElement?.querySelector(".video-card-placeholder")?.classList.remove("is-hidden");
  }

  const summarizedCount = collection.summarized_count || 0;
  const unsummarizedCount = collection.unsummarized_count ?? Math.max(0, collection.item_count - summarizedCount);
  const latestActivityAt = collection.latest_video_updated_at || collection.last_checked_at;

  function stop(event: MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
  }

  return (
    <Link className="video-card collection-video-card" to={`/collections/${encodeURIComponent(collection.collection_id)}`} draggable={false} onContextMenu={(event) => onOpenContextMenu?.(event, collection.collection_id)}>
      <div className="video-card-cover">
        {collection.cover_url ? (
          <>
            <img src={collection.cover_url} alt={collection.title} loading="lazy" draggable={false} onError={handleImageError} />
            <div className="video-card-placeholder is-hidden">合集</div>
          </>
        ) : <div className="video-card-placeholder">合集</div>}
        <div className="collection-count-badge" aria-label={`${collection.item_count} 个视频`}>
          <strong>{collection.item_count}</strong>
          <span>集</span>
        </div>
        {onToggleFavorite ? <button type="button" className={`video-card-favorite ${collection.is_favorite ? "is-active" : ""}`} aria-label={collection.is_favorite ? "取消收藏合集" : "收藏合集"} onClick={(event) => { stop(event); void onToggleFavorite(collection.collection_id, !collection.is_favorite); }}><StarIcon /></button> : null}
        <div className="video-card-pin-actions">
          {canPinInFolder && onToggleFolderPin ? <button type="button" className={`video-card-pin ${collection.folder_pinned ? "is-active" : ""}`} aria-label={collection.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"} onClick={(event) => { stop(event); void onToggleFolderPin(collection.collection_id, !collection.folder_pinned); }}><PinIcon /><span>文件夹置顶</span></button> : onToggleGlobalPin ? <button type="button" className={`video-card-pin ${collection.global_pinned ? "is-active" : ""}`} aria-label={collection.global_pinned ? "取消全局置顶" : "全局置顶"} onClick={(event) => { stop(event); void onToggleGlobalPin(collection.collection_id, !collection.global_pinned); }}><PinIcon /><span>全局置顶</span></button> : null}
        </div>
      </div>
      <div className="video-card-body">
        <div className="video-card-topline">
          <div className="video-card-badges">
            <span className="video-platform-badge is-bilibili">{platformLabel("bilibili")}</span>
            <span className="video-page-badge">合集</span>
          </div>
          <span className="task-status status-pending">合集</span>
        </div>
        <h3>{collection.title}</h3>
        <div className="video-card-meta">
          <span>{formatDateTime(latestActivityAt)}</span>
          <span className="video-card-result-state">已总结 {summarizedCount} · 未总结 {unsummarizedCount}</span>
        </div>
        <div className="video-card-folder">合集 · {collection.item_count} 个视频</div>
      </div>
    </Link>
  );
}

export function CollectionListItem({
  collection,
  onOpenContextMenu,
  className = "",
  dragHandleProps,
  style,
  setNodeRef,
  canPinInFolder = false,
  onToggleFavorite,
  onToggleGlobalPin,
  onToggleFolderPin,
}: {
  collection: VideoCollection;
  onOpenContextMenu?: (event: MouseEvent, collectionId: string) => void;
  className?: string;
  dragHandleProps?: Record<string, unknown>;
  style?: CSSProperties;
  setNodeRef?: (node: HTMLDivElement | null) => void;
  canPinInFolder?: boolean;
  onToggleFavorite?: (collectionId: string, nextFavorite: boolean) => Promise<unknown>;
  onToggleGlobalPin?: (collectionId: string, nextPinned: boolean) => Promise<unknown>;
  onToggleFolderPin?: (collectionId: string, nextPinned: boolean) => Promise<unknown>;
}) {
  const summarizedCount = collection.summarized_count || 0;
  const unsummarizedCount = collection.unsummarized_count ?? Math.max(0, collection.item_count - summarizedCount);
  const latestActivityAt = collection.latest_video_updated_at || collection.last_checked_at;
  return (
    <div ref={setNodeRef} style={style} className={`library-list-item collection-list-item ${className}`.trim()} onContextMenu={(event) => onOpenContextMenu?.(event, collection.collection_id)}>
      <span className="library-drag-handle" title="拖动排序" {...dragHandleProps}><GripVerticalIcon /></span>
      <Link className="library-list-cover" to={`/collections/${encodeURIComponent(collection.collection_id)}`} draggable={false}>
        {collection.cover_url ? <img src={collection.cover_url} alt={collection.title} loading="lazy" draggable={false} /> : <span>合集</span>}
      </Link>
      <Link className="library-list-main" to={`/collections/${encodeURIComponent(collection.collection_id)}`} draggable={false}>
        <strong>{collection.title}</strong>
        <span>Bilibili · {collection.item_count} 个视频 · 已总结 {summarizedCount} · 未总结 {unsummarizedCount}</span>
      </Link>
      <span className="task-status status-pending">合集</span>
      <span className="library-list-date">{formatDateTime(latestActivityAt)}</span>
      <div className="library-list-actions collection-list-actions">
        {onToggleFavorite ? <button type="button" title={collection.is_favorite ? "取消收藏" : "收藏"} onClick={(event) => { event.preventDefault(); event.stopPropagation(); void onToggleFavorite(collection.collection_id, !collection.is_favorite); }}>{collection.is_favorite ? "★" : "☆"}</button> : null}
        {canPinInFolder && onToggleFolderPin ? <button className={`library-pin-button ${collection.folder_pinned ? "is-active" : ""}`} type="button" title={collection.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"} onClick={(event) => { event.preventDefault(); event.stopPropagation(); void onToggleFolderPin(collection.collection_id, !collection.folder_pinned); }}><PinIcon /><span>文件夹置顶</span></button> : onToggleGlobalPin ? <button className={`library-pin-button ${collection.global_pinned ? "is-active" : ""}`} type="button" title={collection.global_pinned ? "取消全局置顶" : "全局置顶"} onClick={(event) => { event.preventDefault(); event.stopPropagation(); void onToggleGlobalPin(collection.collection_id, !collection.global_pinned); }}><PinIcon /><span>全局置顶</span></button> : null}
      </div>
    </div>
  );
}
