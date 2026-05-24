import { Link } from "react-router-dom";
import type { MouseEvent, SyntheticEvent } from "react";

import { platformLabel, taskStatusClass } from "../appModel";
import { GripVerticalIcon, PinIcon } from "./AppIcons";
import type { VideoAssetSummary } from "../types";
import { formatDateTime, formatDuration, taskStatusLabel } from "../utils";

export function VideoCard({
  video,
  folderName,
  canPinInFolder = false,
  onToggleFavorite,
  onToggleGlobalPin,
  onToggleFolderPin,
  onOpenContextMenu,
}: {
  video: VideoAssetSummary;
  folderName?: string;
  canPinInFolder?: boolean;
  onToggleFavorite?: (videoId: string, nextFavorite: boolean) => Promise<void>;
  onToggleGlobalPin?: (videoId: string, nextPinned: boolean) => Promise<void>;
  onToggleFolderPin?: (videoId: string, nextPinned: boolean) => Promise<void>;
  onOpenContextMenu?: (event: MouseEvent, videoId: string) => void;
}) {
  const badgeClass = taskStatusClass(video.latest_status);
  const isMultiPageVideo = video.pages.length > 1;
  const platformClass = video.platform ? `is-${video.platform.toLowerCase()}` : "";

  async function handleFavoriteClick(event: MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
    await onToggleFavorite?.(video.video_id, !video.is_favorite);
  }

  async function handleGlobalPinClick(event: MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
    await onToggleGlobalPin?.(video.video_id, !video.global_pinned);
  }

  async function handleFolderPinClick(event: MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
    await onToggleFolderPin?.(video.video_id, !video.folder_pinned);
  }

  const resultStateLabel = getResultStateLabel(video);

  function handleImageError(event: SyntheticEvent<HTMLImageElement>) {
    const target = event.target as HTMLImageElement;
    target.style.display = "none";
    const placeholder = target.parentElement?.querySelector(".video-card-placeholder");
    if (placeholder) {
      placeholder.classList.remove("is-hidden");
    }
  }

  return (
    <Link className="video-card" to={`/videos/${video.video_id}`} draggable={false} onContextMenu={(event) => onOpenContextMenu?.(event, video.video_id)}>
      <div className="video-card-cover">
        <span className="video-card-drag-handle" title="拖动排序" aria-hidden="true"><GripVerticalIcon /></span>
        {video.cover_url ? (
          <>
            <img src={video.cover_url} alt={video.title} loading="lazy" draggable={false} onError={handleImageError} />
            <div className="video-card-placeholder is-hidden">VIDEO</div>
          </>
        ) : (
          <div className="video-card-placeholder">VIDEO</div>
        )}
        {onToggleFavorite ? (
          <button
            aria-label={video.is_favorite ? "取消收藏" : "收藏视频"}
            className={`video-card-favorite ${video.is_favorite ? "is-active" : ""}`}
            title={video.is_favorite ? "取消收藏" : "收藏视频"}
            type="button"
            onClick={(event) => void handleFavoriteClick(event)}
          >
            <IconFavorite />
          </button>
        ) : null}
        <div className="video-card-pin-actions">
          {canPinInFolder && onToggleFolderPin ? (
            <button
              aria-label={video.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"}
              className={`video-card-pin ${video.folder_pinned ? "is-active" : ""}`}
              title={video.folder_pinned ? "取消文件夹置顶" : "文件夹置顶"}
              type="button"
              onClick={(event) => void handleFolderPinClick(event)}
            >
              <PinIcon />
              <span>文件夹置顶</span>
            </button>
          ) : onToggleGlobalPin ? (
            <button
              aria-label={video.global_pinned ? "取消全局置顶" : "全局置顶"}
              className={`video-card-pin ${video.global_pinned ? "is-active" : ""}`}
              title={video.global_pinned ? "取消全局置顶" : "全局置顶"}
              type="button"
              onClick={(event) => void handleGlobalPinClick(event)}
            >
              <PinIcon />
              <span>全局置顶</span>
            </button>
          ) : null}
        </div>
        <span className="video-duration">{formatDuration(video.duration)}</span>
      </div>
      <div className="video-card-body">
        <div className="video-card-topline">
          <div className="video-card-badges">
            <span className={`video-platform-badge ${platformClass}`.trim()}>{platformLabel(video.platform)}</span>
            {isMultiPageVideo ? <span className="video-page-badge">{video.pages.length}P</span> : null}
          </div>
          <span className={`task-status ${badgeClass}`}>{taskStatusLabel(video.latest_status)}</span>
        </div>
        <h3>{video.title}</h3>
        <div className="video-card-meta">
          <span>{formatDateTime(video.updated_at)}</span>
          <span className="video-card-result-state">{resultStateLabel}</span>
        </div>
        {folderName ? <div className="video-card-folder">{folderName}</div> : null}
      </div>
    </Link>
  );
}

function getResultStateLabel(video: VideoAssetSummary) {
  if (video.has_result) {
    return "摘要已生成";
  }

  switch (video.latest_status) {
    case "running":
      return "正在生成摘要";
    case "queued":
      return "等待开始处理";
    case "failed":
      return "本次生成失败";
    case "cancelled":
      return "已取消生成";
    case "completed":
      return "未产出摘要";
    default:
      return "尚未生成摘要";
  }
}

function IconFavorite() {
  return (
    <svg fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 18.26 4.95 22l1.35-7.84L.6 8.71l7.87-1.14L12 0.5l3.53 7.07 7.87 1.14-5.7 5.45L19.05 22z" />
    </svg>
  );
}
