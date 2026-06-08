import { useCallback, useEffect, useRef, useState } from "react";

type TourStep = {
  targetSelector: string;
  title: string;
  description: string;
  placement: "top" | "bottom" | "left" | "right";
};

const TOUR_STEPS: TourStep[] = [
  {
    targetSelector: "[data-home-tour='input']",
    title: "粘贴视频链接",
    description: "在这里粘贴 Bilibili / YouTube 视频链接，或直接输入 BV 号即可开始。",
    placement: "bottom",
  },
  {
    targetSelector: "[data-home-tour='local-video']",
    title: "导入本地视频",
    description: "点击此按钮可以选择本地视频文件进行总结。",
    placement: "bottom",
  },
  {
    targetSelector: "[data-home-tour='submit']",
    title: "开始总结 & 笔记偏好",
    description: "点击「开始总结」提交任务。右侧 ▼ 可切换笔记形式：纯文字笔记或带截图的图文笔记。",
    placement: "bottom",
  },
];

const TOUR_SEEN_KEY = "vidmind.homeTourSeen";

function getElementRect(selector: string): DOMRect | null {
  const el = document.querySelector(selector);
  if (!el) return null;
  return el.getBoundingClientRect();
}

type TourCardPosition = {
  top: number;
  left: number;
  arrowTop?: number;
  arrowLeft?: number;
  arrowDirection: "up" | "down" | "left" | "right";
};

function calcPosition(
  targetRect: DOMRect,
  placement: TourStep["placement"],
): TourCardPosition {
  const cardWidth = 320;
  const gap = 16;
  const arrowSize = 10;

  const targetCenterX = targetRect.left + targetRect.width / 2;

  let top: number;
  let left: number;
  let arrowLeft: number | undefined;
  let arrowDirection: TourCardPosition["arrowDirection"];

  switch (placement) {
    case "bottom":
      top = targetRect.bottom + gap + arrowSize;
      left = targetCenterX - cardWidth / 2;
      arrowDirection = "up";
      arrowLeft = targetCenterX - left;
      break;
    case "top":
      top = targetRect.top - gap - arrowSize;
      left = targetCenterX - cardWidth / 2;
      arrowDirection = "down";
      arrowLeft = targetCenterX - left;
      break;
    default:
      top = targetRect.bottom + gap + arrowSize;
      left = targetCenterX - cardWidth / 2;
      arrowDirection = "up";
      arrowLeft = targetCenterX - left;
  }

  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const margin = 12;

  left = Math.max(margin, Math.min(left, viewportWidth - cardWidth - margin));
  top = Math.max(margin, Math.min(top, viewportHeight - 120 - margin));

  if (arrowLeft !== undefined) {
    arrowLeft = Math.max(24, Math.min(arrowLeft, cardWidth - 24));
  }

  return { top, left, arrowDirection, arrowLeft };
}

export function HomeTour({ onClose }: { onClose: () => void }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [position, setPosition] = useState<TourCardPosition | null>(null);
  const [targetRect, setTargetRect] = useState<DOMRect | null>(null);
  const [visible, setVisible] = useState(false);
  const rafRef = useRef<number>(0);
  const availableSteps = useRef(TOUR_STEPS.filter((s) => typeof document !== "undefined" && document.querySelector(s.targetSelector)));

  const totalSteps = availableSteps.current.length;

  const finishTour = useCallback(() => {
    setVisible(false);
    try {
      window.localStorage.setItem(TOUR_SEEN_KEY, "1");
    } catch { /* ignore */ }
    onClose();
  }, [onClose]);

  if (totalSteps === 0) {
    finishTour();
    return null;
  }

  const step = availableSteps.current[stepIndex];

  const updatePosition = useCallback(() => {
    const rect = getElementRect(step.targetSelector);
    if (!rect) {
      setPosition(null);
      setTargetRect(null);
      return;
    }
    setTargetRect(rect);
    setPosition(calcPosition(rect, step.placement));
  }, [step]);

  useEffect(() => {
    cancelAnimationFrame(rafRef.current);
    setVisible(false);
    setPosition(null);
    setTargetRect(null);

    const frame = requestAnimationFrame(() => {
      updatePosition();
      requestAnimationFrame(() => {
        setVisible(true);
      });
    });
    rafRef.current = frame;

    const handleResize = () => updatePosition();
    const handleScroll = () => updatePosition();
    window.addEventListener("resize", handleResize);
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("scroll", handleScroll);
    };
  }, [step, updatePosition]);

  function goNext() {
    if (stepIndex < totalSteps - 1) {
      setStepIndex((i) => i + 1);
    } else {
      finishTour();
    }
  }

  if (!position || !targetRect) return null;

  const highlightStyle: React.CSSProperties = {
    position: "fixed",
    top: targetRect.top - 6,
    left: targetRect.left - 6,
    width: targetRect.width + 12,
    height: targetRect.height + 12,
    borderRadius: 10,
    boxShadow: "0 0 0 9999px rgba(0, 0, 0, 0.45)",
    pointerEvents: "none",
    zIndex: 1000,
  };

  const arrowStyle: React.CSSProperties = {
    position: "absolute",
    width: 0,
    height: 0,
    ...(position.arrowDirection === "up" && {
      top: -10,
      left: position.arrowLeft ?? "50%",
      marginLeft: -10,
      borderLeft: "10px solid transparent",
      borderRight: "10px solid transparent",
      borderBottom: "10px solid var(--bg-elevated, #fff)",
    }),
    ...(position.arrowDirection === "down" && {
      bottom: -10,
      left: position.arrowLeft ?? "50%",
      marginLeft: -10,
      borderLeft: "10px solid transparent",
      borderRight: "10px solid transparent",
      borderTop: "10px solid var(--bg-elevated, #fff)",
    }),
  };

  return (
    <div className="home-tour-overlay" style={{ zIndex: 1000 }}>
      <div style={highlightStyle} />
      <div
        className={`home-tour-card ${visible ? "is-visible" : ""}`}
        style={{
          position: "fixed",
          top: position.top,
          left: position.left,
          zIndex: 1002,
          maxWidth: 320,
        }}
        role="dialog"
        aria-label={step.title}
      >
        <div style={arrowStyle} />
        <div className="home-tour-card-body">
          <strong className="home-tour-card-title">{step.title}</strong>
          <p className="home-tour-card-desc">{step.description}</p>
        </div>
        <div className="home-tour-card-footer">
          <span className="home-tour-step-indicator">
            {stepIndex + 1} / {totalSteps}
          </span>
          <div className="home-tour-card-actions">
            <button className="home-tour-skip" type="button" onClick={finishTour}>
              跳过
            </button>
            <button className="home-tour-next primary-button" type="button" onClick={goNext}>
              {stepIndex < totalSteps - 1 ? "下一步" : "知道了"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function isHomeTourSeen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(TOUR_SEEN_KEY) === "1";
  } catch {
    return true;
  }
}
