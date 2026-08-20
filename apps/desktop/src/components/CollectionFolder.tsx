import type { VideoCollection } from "../types";
import { formatDuration } from "../utils";

type CollectionFolderProps = {
  collection: VideoCollection;
  className?: string;
  selectable?: boolean;
  selectedBvids?: Set<string>;
  onToggleItem?(bvid: string): void;
};

export function CollectionFolder({
  collection,
  className = "",
  selectable = false,
  selectedBvids = new Set<string>(),
  onToggleItem,
}: CollectionFolderProps) {
  const itemCount = collection.item_count || collection.items.length;

  return (
    <details className={`collection-folder ${className}`.trim()}>
      <summary className="collection-folder-summary">
        <span className="collection-folder-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3.5 7.5a2 2 0 0 1 2-2h5l2 2h6a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />
            <path d="M3.5 9h17" />
          </svg>
        </span>
        <span className="collection-folder-copy">
          <strong>{collection.title}</strong>
          <small>{itemCount} 个视频 · 点击展开查看合集内容</small>
        </span>
        <span className="collection-folder-count">{itemCount}</span>
        <span className="collection-folder-chevron" aria-hidden="true">⌄</span>
      </summary>

      <div className="collection-folder-items" role="list" aria-label={`${collection.title}中的视频`}>
        {collection.items.map((item) => {
          const selected = selectedBvids.has(item.bvid);
          const content = (
            <>
              {selectable ? <span className={`collection-folder-item-check ${selected ? "is-selected" : ""}`} aria-hidden="true">{selected ? "✓" : ""}</span> : null}
              <span className="collection-folder-item-index">{item.position}</span>
              {item.cover_url ? (
                <>
                  <img
                    src={item.cover_url}
                    alt=""
                    loading="lazy"
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                      const fallback = event.currentTarget.nextElementSibling;
                      if (fallback instanceof HTMLElement) {
                        fallback.classList.add("is-visible");
                      }
                    }}
                  />
                  <span className="collection-folder-item-placeholder is-fallback" aria-hidden="true" />
                </>
              ) : (
                <span className="collection-folder-item-placeholder" aria-hidden="true" />
              )}
              <span className="collection-folder-item-copy">
                <strong>{item.title}</strong>
                <small>
                  {formatDuration(item.duration)}
                  {item.is_current ? " · 当前视频" : ""}
                </small>
              </span>
            </>
          );

          return selectable ? (
            <button
              className={`collection-folder-item is-selectable ${item.is_current ? "is-current" : ""} ${selected ? "is-selected" : ""}`}
              key={`${item.bvid}-${item.position}`}
              type="button"
              aria-pressed={selected}
              role="listitem"
              onClick={() => onToggleItem?.(item.bvid)}
            >
              {content}
            </button>
          ) : (
            <a
              className={`collection-folder-item ${item.is_current ? "is-current" : ""}`}
              href={item.source_url}
              key={`${item.bvid}-${item.position}`}
              target="_blank"
              rel="noreferrer"
              role="listitem"
            >
              {content}
            </a>
          );
        })}
      </div>
    </details>
  );
}
