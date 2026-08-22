import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type FloatingNoticeTone = "info" | "success" | "error";

const DEFAULT_NOTICE_DURATION_MS = 5000;
const NOTICE_EXIT_ANIMATION_MS = 180;

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

function resolveAutoDismissDuration(durationMs?: number | null) {
  if (durationMs !== undefined) {
    return durationMs;
  }
  return DEFAULT_NOTICE_DURATION_MS;
}

export function FloatingNoticeStack({ notices }: { notices: FloatingNotice[] }) {
  const [dismissedSignatures, setDismissedSignatures] = useState<Record<string, string>>({});
  const [exitingSignatures, setExitingSignatures] = useState<Record<string, string>>({});
  const exitTimersRef = useRef<Record<string, number>>({});
  const normalizedNotices = useMemo(() => (
    notices
      .filter((notice) => notice.message.trim())
      .map((notice) => {
        const tone = notice.tone ?? inferTone(notice.message);
        return {
          ...notice,
          tone,
          signature: `${notice.id}:${notice.version ?? "stable"}:${notice.message}`,
          autoDismissMs: resolveAutoDismissDuration(notice.durationMs),
        };
      })
  ), [notices]);
  const visibleNotices = normalizedNotices.filter((notice) => dismissedSignatures[notice.id] !== notice.signature);

  const dismissNotice = useCallback((id: string, signature: string) => {
    setExitingSignatures((current) => ({ ...current, [id]: signature }));
    const previousTimer = exitTimersRef.current[id];
    if (previousTimer) {
      window.clearTimeout(previousTimer);
    }
    const timer = window.setTimeout(() => {
      setDismissedSignatures((current) => ({ ...current, [id]: signature }));
      setExitingSignatures((current) => {
        if (current[id] !== signature) {
          return current;
        }
        const next = { ...current };
        delete next[id];
        return next;
      });
      if (exitTimersRef.current[id] === timer) {
        delete exitTimersRef.current[id];
      }
    }, NOTICE_EXIT_ANIMATION_MS);
    exitTimersRef.current[id] = timer;
  }, []);

  useEffect(() => () => {
    for (const timer of Object.values(exitTimersRef.current)) {
      window.clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    if (!normalizedNotices.length) {
      return;
    }

    const timers = normalizedNotices
      .filter((notice) => notice.autoDismissMs != null && dismissedSignatures[notice.id] !== notice.signature)
      .map((notice) => window.setTimeout(() => {
        dismissNotice(notice.id, notice.signature);
      }, notice.autoDismissMs ?? 0));

    return () => {
      for (const timer of timers) {
        window.clearTimeout(timer);
      }
    };
  }, [dismissedSignatures, dismissNotice, normalizedNotices]);

  if (!visibleNotices.length) {
    return null;
  }

  return (
    <div className="floating-notice-stack" aria-live="polite" aria-atomic="true">
      {visibleNotices.map((notice) => {
        return (
          <div
            className={`floating-notice-pill tone-${notice.tone}${exitingSignatures[notice.id] === notice.signature ? " is-exiting" : ""}`}
            key={notice.signature}
            role="status"
          >
            <span className="floating-notice-dot" aria-hidden="true" />
            <span className="floating-notice-copy">{notice.message}</span>
            <button
              className="floating-notice-close"
              type="button"
              aria-label="关闭提示"
              onClick={() => {
                dismissNotice(notice.id, notice.signature);
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
