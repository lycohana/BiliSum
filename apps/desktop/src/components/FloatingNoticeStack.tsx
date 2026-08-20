import { useEffect, useMemo, useState } from "react";

type FloatingNoticeTone = "info" | "success" | "error";

export type FloatingNotice = {
  id: string;
  message: string;
  tone?: FloatingNoticeTone;
  durationMs?: number | null;
  version?: number | string;
};

function inferTone(message: string): FloatingNoticeTone {
  if (/失败|错误|不可用|未就绪|停止|关闭/i.test(message)) {
    return "error";
  }
  if (/完成|成功|已复制|已保存|已刷新|已开始|已请求|已切换/i.test(message)) {
    return "success";
  }
  return "info";
}

function resolveAutoDismissDuration(message: string, tone: FloatingNoticeTone, durationMs?: number | null) {
  if (durationMs !== undefined) {
    return durationMs;
  }
  if (/^正在(刷新|导出|复制|重新生成|生成|提交|拉起)|^已发起/i.test(message)) {
    return 3200;
  }
  if (/检测到.+(合集|分 ?P).*(请先|选择)/i.test(message)) {
    return 2800;
  }
  if (/处理中|请先|检测到.+请先/i.test(message)) {
    return null;
  }
  return tone === "error" ? 6500 : 4800;
}

export function FloatingNoticeStack({ notices }: { notices: FloatingNotice[] }) {
  const [dismissedSignatures, setDismissedSignatures] = useState<Record<string, string>>({});
  const normalizedNotices = useMemo(() => (
    notices
      .filter((notice) => notice.message.trim())
      .map((notice) => {
        const tone = notice.tone ?? inferTone(notice.message);
        return {
          ...notice,
          tone,
          signature: `${notice.id}:${notice.version ?? "stable"}:${notice.message}`,
          autoDismissMs: resolveAutoDismissDuration(notice.message, tone, notice.durationMs),
        };
      })
  ), [notices]);
  const visibleNotices = normalizedNotices.filter((notice) => dismissedSignatures[notice.id] !== notice.signature);

  useEffect(() => {
    if (!normalizedNotices.length) {
      return;
    }

    const timers = normalizedNotices
      .filter((notice) => notice.autoDismissMs != null && dismissedSignatures[notice.id] !== notice.signature)
      .map((notice) => window.setTimeout(() => {
        setDismissedSignatures((current) => ({ ...current, [notice.id]: notice.signature }));
      }, notice.autoDismissMs ?? 0));

    return () => {
      for (const timer of timers) {
        window.clearTimeout(timer);
      }
    };
  }, [dismissedSignatures, normalizedNotices]);

  if (!visibleNotices.length) {
    return null;
  }

  return (
    <div className="floating-notice-stack" aria-live="polite" aria-atomic="true">
      {visibleNotices.map((notice) => {
        return (
          <div className={`floating-notice-pill tone-${notice.tone}`} key={notice.signature} role="status">
            <span className="floating-notice-dot" aria-hidden="true" />
            <span className="floating-notice-copy">{notice.message}</span>
            <button
              className="floating-notice-close"
              type="button"
              aria-label="关闭提示"
              onClick={() => {
                setDismissedSignatures((current) => ({ ...current, [notice.id]: notice.signature }));
              }}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        );
      })}
    </div>
  );
}
