import { useEffect, useMemo, useState } from "react";

import type {
  PageAggregateStatus,
  VideoAssetSummary,
  VideoPageBatchOption,
  VideoTaskBatchResponse,
} from "../types";
import { taskStatusLabel } from "../taskStatus";
import { formatDateTime, formatDuration } from "../utils";

type MultiPageSelectDialogProps = {
  isOpen: boolean;
  mode: "create" | "resummary";
  video: VideoAssetSummary | null;
  pages: VideoPageBatchOption[];
  onClose(): void;
  onSubmit(input: { pageNumbers: number[]; confirm: boolean }): Promise<VideoTaskBatchResponse>;
};

function aggregateStatusLabel(aggregateStatus?: PageAggregateStatus) {
  if (aggregateStatus === "completed") return "已完成";
  if (aggregateStatus === "in_progress") return "进行中";
  if (aggregateStatus === "failed") return "失败";
  return "未开始";
}

function aggregateStatusClass(aggregateStatus?: PageAggregateStatus) {
  if (aggregateStatus === "completed") return "status-success";
  if (aggregateStatus === "in_progress") return "status-running";
  if (aggregateStatus === "failed") return "status-failed";
  return "status-pending";
}

export function MultiPageSelectDialog({
  isOpen,
  mode,
  video,
  pages,
  onClose,
  onSubmit,
}: MultiPageSelectDialogProps) {
  const [selectedPages, setSelectedPages] = useState<number[]>([]);
  const [confirmation, setConfirmation] = useState<VideoTaskBatchResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isOpen) {
      setSelectedPages([]);
      setConfirmation(null);
      setSubmitting(false);
    }
  }, [isOpen]);

  const selectedPageSet = useMemo(() => new Set(selectedPages), [selectedPages]);
  const selectedCount = selectedPages.length;
  const selectedPreview = useMemo(
    () => pages.filter((page) => selectedPageSet.has(page.page)),
    [pages, selectedPageSet],
  );
  const heroCover = selectedPreview[0]?.cover_url || pages[0]?.cover_url || video?.cover_url || "";
  const totalConflictCount = confirmation?.conflict_pages.length ?? 0;
  const totalSkipCount = confirmation?.skipped_pages.length ?? 0;

  if (!isOpen || !video) {
    return null;
  }

  function togglePage(pageNumber: number) {
    setConfirmation(null);
    setSelectedPages((current) => (
      current.includes(pageNumber)
        ? current.filter((item) => item !== pageNumber)
        : [...current, pageNumber].sort((left, right) => left - right)
    ));
  }

  async function handleSubmit(confirm: boolean) {
    if (!selectedPages.length) {
      return;
    }
    setSubmitting(true);
    try {
      const response = await onSubmit({ pageNumbers: selectedPages, confirm });
      if (response.requires_confirmation) {
        setConfirmation(response);
      }
    } finally {
      setSubmitting(false);
    }
  }

  const title = mode === "create" ? "批量选择要生成的分 P" : "批量选择要重生成的分 P";
  const description = mode === "create"
    ? `共 ${pages.length} 个分 P。默认不勾选任何内容，请手动选择后再开始批量生成。`
    : `共 ${pages.length} 个分 P。请选择要复用转写重新生成摘要的内容。`;
  const submitLabel = mode === "create" ? "批量生成摘要" : "批量重生成摘要";

  return (
    <div className="update-dialog-overlay" onClick={() => !submitting && onClose()}>
      <div className="update-dialog multi-page-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="update-dialog-header">
          <h2>{confirmation ? "确认批量处理" : title}</h2>
          <button className="close-button" onClick={onClose} aria-label="关闭" disabled={submitting}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="update-dialog-body multi-page-dialog-body">
          <div className="multi-page-dialog-hero">
            {heroCover ? (
              <img src={heroCover} alt={video.title} />
            ) : (
              <div className="multi-page-dialog-placeholder" aria-hidden="true">P</div>
            )}
            <div className="multi-page-dialog-copy">
              <span className="section-kicker">{mode === "create" ? "批量生成" : "批量重生成"}</span>
              <strong>{video.title}</strong>
              <small>{confirmation ? "请确认这次批量操作的执行结果。" : description}</small>
            </div>
          </div>

          {confirmation ? (
            <div className="multi-page-confirmation">
              <div className="multi-page-confirmation-summary">
                <span className="helper-chip">已选 {selectedCount} 个分 P</span>
                {totalConflictCount ? <span className="helper-chip">{mode === "create" ? `将跳过 ${totalConflictCount} 个` : `将重跑 ${totalConflictCount} 个`}</span> : null}
                {totalSkipCount ? <span className="helper-chip">无法处理 {totalSkipCount} 个</span> : null}
              </div>

              {confirmation.conflict_pages.length ? (
                <div className="multi-page-confirmation-list">
                  {confirmation.conflict_pages.map((item) => (
                    <article className="multi-page-confirmation-item" key={`conflict-${item.page_number}`}>
                      <div className="multi-page-confirmation-head">
                        <strong>{item.page_title || `P${item.page_number}`}</strong>
                        <span className={`task-status ${item.action === "rerun" ? "status-running" : "status-pending"}`}>
                          {item.action === "rerun" ? "确认重跑" : "确认跳过"}
                        </span>
                      </div>
                      <small>{item.reason || "-"}</small>
                      {item.existing_status ? (
                        <span className="multi-page-confirmation-meta">
                          当前状态：{taskStatusLabel(item.existing_status)}{item.existing_task_id ? ` · 任务 ${item.existing_task_id.slice(0, 8)}` : ""}
                        </span>
                      ) : null}
                    </article>
                  ))}
                </div>
              ) : null}

              {confirmation.skipped_pages.length ? (
                <div className="multi-page-confirmation-list">
                  {confirmation.skipped_pages.map((item) => (
                    <article className="multi-page-confirmation-item is-muted" key={`skipped-${item.page_number}`}>
                      <div className="multi-page-confirmation-head">
                        <strong>{item.page_title || `P${item.page_number}`}</strong>
                        <span className="task-status status-pending">将跳过</span>
                      </div>
                      <small>{item.reason || "-"}</small>
                    </article>
                  ))}
                </div>
              ) : null}
            </div>
          ) : (
            <>
              <div className="multi-page-toolbar">
                <span className="multi-page-toolbar-copy">已选 {selectedCount} / {pages.length}</span>
                <div className="multi-page-toolbar-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={submitting || selectedCount === pages.length}
                    onClick={() => {
                      setConfirmation(null);
                      setSelectedPages(pages.map((page) => page.page));
                    }}
                  >
                    全选
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={submitting || selectedCount === 0}
                    onClick={() => {
                      setConfirmation(null);
                      setSelectedPages([]);
                    }}
                  >
                    清空
                  </button>
                </div>
              </div>

              <div className="multi-page-list" role="listbox" aria-label="视频分 P 列表" aria-multiselectable="true">
                {pages.map((page) => {
                  const selected = selectedPageSet.has(page.page);
                  const statusClass = aggregateStatusClass(page.aggregate_status);
                  return (
                    <button
                      key={page.page}
                      type="button"
                      className={`multi-page-item ${selected ? "is-selected" : ""}`}
                      aria-selected={selected}
                      onClick={() => togglePage(page.page)}
                    >
                      <span className={`multi-page-item-check ${selected ? "is-selected" : ""}`} aria-hidden="true">
                        {selected ? "✓" : ""}
                      </span>
                      <span className="multi-page-item-copy">
                        <span className="multi-page-item-topline">
                          <strong>{page.title}</strong>
                          <span className={`task-status ${statusClass}`}>{aggregateStatusLabel(page.aggregate_status)}</span>
                        </span>
                        <small>
                          P{page.page} · {formatDuration(page.duration)}
                          {page.latest_task_status ? ` · ${taskStatusLabel(page.latest_task_status)}` : ""}
                          {page.latest_task_updated_at ? ` · ${formatDateTime(page.latest_task_updated_at)}` : ""}
                        </small>
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>

        <div className="update-dialog-footer">
          {confirmation ? (
            <>
              <button className="secondary-button" type="button" onClick={() => setConfirmation(null)} disabled={submitting}>返回修改</button>
              <button className="primary-button" type="button" onClick={() => void handleSubmit(true)} disabled={submitting}>
                {submitting ? "正在提交..." : (mode === "create" ? "确认继续创建" : "确认继续重生成")}
              </button>
            </>
          ) : (
            <>
              <button className="secondary-button" type="button" onClick={onClose} disabled={submitting}>取消</button>
              <button className="primary-button" type="button" onClick={() => void handleSubmit(false)} disabled={!selectedCount || submitting}>
                {submitting ? "正在提交..." : `${submitLabel}${selectedCount ? ` (${selectedCount})` : ""}`}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
