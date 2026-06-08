import { useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";

import type { ConfigHealth } from "../appModel";
import { platformLabel } from "../appModel";
import { LinkIcon, LocalVideoIcon } from "../components/AppIcons";
import { FloatingNoticeStack } from "../components/FloatingNoticeStack";
import { VideoCard } from "../components/VideoCard";
import type { VideoAssetSummary } from "../types";
import { formatDuration } from "../utils";

type HomePageProps = {
  configHealth: ConfigHealth;
  probePreview: VideoAssetSummary | null;
  probeUrl: string;
  setProbeUrl(value: string): void;
  submitStatus: string;
  onProbe(event: FormEvent): Promise<void>;
  onImportLocalVideo(): Promise<void>;
  canImportLocalVideo: boolean;
  onOpenSetupAssistant(issueKey?: string): void;
  onOpenConfigIssue(issueKey: string): void;
  favoriteVideos: VideoAssetSummary[];
  recentVideos: VideoAssetSummary[];
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
};

type SummaryPreference = {
  noteMode: "text" | "visual";
  generateMindmap: boolean;
};

const SUMMARY_PREFERENCE_STORAGE_KEY = "vidmind.summaryPreference";
const PREFERENCE_HINT_SEEN_KEY = "vidmind.summaryPreferenceHintSeen";
const DEFAULT_SUMMARY_PREFERENCE: SummaryPreference = {
  noteMode: "text",
  generateMindmap: false,
};

function loadSummaryPreference(): SummaryPreference {
  if (typeof window === "undefined") {
    return DEFAULT_SUMMARY_PREFERENCE;
  }
  try {
    const rawValue = window.localStorage.getItem(SUMMARY_PREFERENCE_STORAGE_KEY);
    if (!rawValue) {
      return DEFAULT_SUMMARY_PREFERENCE;
    }
    const parsed = JSON.parse(rawValue) as Partial<SummaryPreference>;
    return {
      noteMode: parsed.noteMode === "visual" ? "visual" : "text",
      generateMindmap: Boolean(parsed.generateMindmap),
    };
  } catch {
    return DEFAULT_SUMMARY_PREFERENCE;
  }
}

export function HomePage({
  configHealth,
  probePreview,
  probeUrl,
  setProbeUrl,
  submitStatus,
  onProbe,
  onImportLocalVideo,
  canImportLocalVideo,
  onOpenSetupAssistant,
  onOpenConfigIssue,
  favoriteVideos,
  recentVideos,
  onToggleFavorite,
}: HomePageProps) {
  const preferenceMenuRef = useRef<HTMLDivElement | null>(null);
  const [summaryPreference, setSummaryPreference] = useState<SummaryPreference>(() => loadSummaryPreference());
  const [preferenceMenuOpen, setPreferenceMenuOpen] = useState(false);
  const [preferenceHintDismissed, setPreferenceHintDismissed] = useState(() => {
    if (typeof window !== "undefined") {
      return window.localStorage.getItem(PREFERENCE_HINT_SEEN_KEY) === "1";
    }
    return true;
  });

  useEffect(() => {
    window.localStorage.setItem(SUMMARY_PREFERENCE_STORAGE_KEY, JSON.stringify(summaryPreference));
  }, [summaryPreference]);

  const showPreferenceHint = preferenceMenuOpen && !preferenceHintDismissed;

  function dismissPreferenceHint() {
    setPreferenceHintDismissed(true);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(PREFERENCE_HINT_SEEN_KEY, "1");
    }
  }

  useEffect(() => {
    if (!preferenceMenuOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (preferenceMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      setPreferenceMenuOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPreferenceMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [preferenceMenuOpen]);

  function updateSummaryPreference(nextPreference: SummaryPreference) {
    setSummaryPreference(nextPreference);
  }

  return (
    <section className="home-page">
      <FloatingNoticeStack notices={[{ id: "home-submit-status", message: submitStatus }]} />
      <div className="section">
        <h2 className="section-title">开始总结</h2>
        <form className="task-form" onSubmit={onProbe}>
          <div className="task-form-row">
            <label className="input-row input-row-hero" style={{ flex: 1 }}>
              <div className="input-with-icon" style={{ flex: 1 }} data-home-tour="input">
                <span className="input-icon" aria-hidden="true"><LinkIcon /></span>
                <input
                  className={`input-field input-field-hero ${canImportLocalVideo ? "input-field-with-action" : ""}`.trim()}
                  type="text"
                  value={probeUrl}
                  onChange={(event) => setProbeUrl(event.target.value)}
                  placeholder="粘贴 Bilibili / YouTube 视频链接，或直接输入 BV 号"
                  required
                />
                {canImportLocalVideo ? (
                  <button
                    className="input-inline-action"
                    type="button"
                    aria-label="导入本地视频"
                    title="导入本地视频"
                    data-home-tour="local-video"
                    onClick={() => void onImportLocalVideo()}
                  >
                    <LocalVideoIcon />
                  </button>
                ) : null}
              </div>
            </label>
            <div className="summary-submit-control" ref={preferenceMenuRef} data-home-tour="submit">
              <div className="summary-split-button">
                <button className="primary-button primary-button-hero summary-submit-main" type="submit">
                  开始总结
                </button>
                <button
                  className="summary-submit-menu-button"
                  type="button"
                  aria-label="打开生成偏好"
                  aria-expanded={preferenceMenuOpen}
                  aria-controls="summary-preference-menu"
                  title="生成偏好"
                  onClick={() => setPreferenceMenuOpen((open) => !open)}
                >
                  <IconChevronDown />
                </button>
              </div>
              {preferenceMenuOpen ? (
                <div className="summary-preference-menu" id="summary-preference-menu" role="menu">
                  {showPreferenceHint ? (
                    <div className="summary-preference-hint">
                      <p>本次生成的笔记形式，切换到"图文笔记"后会为本次任务生成带截图的图文笔记。</p>
                      <button
                        className="summary-preference-hint-close"
                        type="button"
                        aria-label="知道了"
                        onClick={dismissPreferenceHint}
                      >
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                          <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        </svg>
                      </button>
                    </div>
                  ) : null}
                  <div className="summary-preference-group">
                    <span className="summary-preference-label">笔记形式</span>
                    <label className={`summary-preference-option ${summaryPreference.noteMode === "text" ? "is-selected" : ""}`}>
                      <input
                        type="radio"
                        name="summary-note-mode"
                        checked={summaryPreference.noteMode === "text"}
                        onChange={() => { updateSummaryPreference({ ...summaryPreference, noteMode: "text" }); dismissPreferenceHint(); }}
                      />
                      <span>文字笔记</span>
                    </label>
                    <label className={`summary-preference-option ${summaryPreference.noteMode === "visual" ? "is-selected" : ""}`}>
                      <input
                        type="radio"
                        name="summary-note-mode"
                        checked={summaryPreference.noteMode === "visual"}
                        onChange={() => { updateSummaryPreference({ ...summaryPreference, noteMode: "visual" }); dismissPreferenceHint(); }}
                      />
                      <span>图文笔记</span>
                    </label>
                  </div>
                  <div className="summary-preference-divider" />
                  <label className={`summary-preference-option summary-preference-toggle ${summaryPreference.generateMindmap ? "is-selected" : ""}`}>
                    <input
                      type="checkbox"
                      checked={summaryPreference.generateMindmap}
                      onChange={(event) => { updateSummaryPreference({ ...summaryPreference, generateMindmap: event.target.checked }); dismissPreferenceHint(); }}
                    />
                    <span>生成导图</span>
                  </label>
                </div>
              ) : null}
            </div>
          </div>
        </form>

        {configHealth.checked && !configHealth.isConfigured ? (
          <article className={`config-alert-card tone-${configHealth.state}`}>
            <div className="config-alert-copy">
              <span className="section-kicker">运行配置提醒</span>
              <strong>{configHealth.hasBlockingIssues ? "当前缺少开始总结所需配置" : "当前有增强配置待补全"}</strong>
              <p>{configHealth.summary}</p>
              <div className="config-alert-list">
                {configHealth.issues.map((issue) => (
                  <button className="config-alert-item" type="button" key={issue.key} onClick={() => onOpenConfigIssue(issue.key)}>
                    <strong>{issue.title}</strong>
                    <span>{issue.description}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="config-alert-actions">
              <button
                className={configHealth.hasBlockingIssues ? "primary-button danger-button" : "secondary-button"}
                type="button"
                onClick={() => onOpenSetupAssistant(configHealth.blockingIssues[0]?.key || configHealth.issues[0]?.key)}
              >
                {configHealth.actionText}
              </button>
            </div>
          </article>
        ) : null}

        {probePreview && (
          <article className="probe-preview">
            <img src={probePreview.cover_url} alt={probePreview.title} />
            <div className="probe-preview-copy">
              <span className="section-kicker">即将加入视频库</span>
              <strong>{probePreview.title}</strong>
              <small>{formatDuration(probePreview.duration)} · {platformLabel(probePreview.platform)}</small>
            </div>
          </article>
        )}
      </div>

      {favoriteVideos.length > 0 ? (
        <div className="section">
          <VideoSection
            title="收藏视频"
            videos={favoriteVideos}
            onToggleFavorite={onToggleFavorite}
          />
        </div>
      ) : null}

      <div className="section">
        {recentVideos.length > 0 ? (
          <VideoSection
            title="最近视频"
            videos={recentVideos}
            onToggleFavorite={onToggleFavorite}
          />
        ) : (
          <div className="empty-placeholder">
            还没有视频，先输入一个链接开始总结吧。
          </div>
        )}
      </div>
    </section>
  );
}

function IconChevronDown() {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.9" viewBox="0 0 24 24" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function VideoSection({
  title,
  videos,
  onToggleFavorite,
}: {
  title: string;
  videos: VideoAssetSummary[];
  onToggleFavorite(videoId: string, nextFavorite: boolean): Promise<void>;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(videos.length > 1);
  const [isScrollable, setIsScrollable] = useState(false);
  const [cardsPerView, setCardsPerView] = useState(() => getCardsPerView(typeof window === "undefined" ? 1180 : window.innerWidth));
  const [cardWidth, setCardWidth] = useState(0);
  const pageStep = Math.max(1, cardsPerView * (cardWidth + 16));
  const totalPages = Math.max(1, Math.ceil(videos.length / Math.max(1, cardsPerView)));
  const currentPage = isScrollable && cardWidth > 0
    ? Math.min(totalPages, Math.max(1, Math.round((viewportRef.current?.scrollLeft ?? 0) / pageStep) + 1))
    : 1;

  function getCardsPerView(width: number) {
    if (width >= 1240) {
      return 4;
    }
    if (width >= 820) {
      return 3;
    }
    if (width >= 560) {
      return 2;
    }
    return 1;
  }

  function updateLayout() {
    const viewport = viewportRef.current;
    if (!viewport) {
      setCardsPerView(getCardsPerView(typeof window === "undefined" ? 1180 : window.innerWidth));
      setIsScrollable(videos.length > 1);
      setCanScrollLeft(false);
      setCanScrollRight(videos.length > 1);
      return;
    }

    const nextCardsPerView = getCardsPerView(viewport.clientWidth);
    const gap = 16;
    setCardsPerView(nextCardsPerView);
    setCardWidth(Math.max(176, (viewport.clientWidth - gap * (nextCardsPerView - 1)) / nextCardsPerView));

    const maxScrollLeft = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    const nextScrollable = maxScrollLeft > 8;
    setIsScrollable(nextScrollable);
    setCanScrollLeft(viewport.scrollLeft > 8);
    setCanScrollRight(nextScrollable && viewport.scrollLeft < maxScrollLeft - 8);
  }

  useEffect(() => {
    updateLayout();
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }

    const handleScroll = () => updateLayout();
    const handleResize = () => updateLayout();
    const resizeObserver = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => updateLayout())
      : null;

    viewport.addEventListener("scroll", handleScroll, { passive: true });
    window.addEventListener("resize", handleResize);
    resizeObserver?.observe(viewport);

    return () => {
      viewport.removeEventListener("scroll", handleScroll);
      window.removeEventListener("resize", handleResize);
      resizeObserver?.disconnect();
    };
  }, [videos.length]);

  function scrollByPage(direction: "left" | "right") {
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }

    const firstCard = viewport.querySelector<HTMLElement>(".home-carousel-item");
    const cardWidth = firstCard?.offsetWidth ?? viewport.clientWidth;
    const gap = 16;
    const nextOffset = (cardWidth + gap) * cardsPerView * (direction === "left" ? -1 : 1);

    viewport.scrollBy({
      left: nextOffset,
      behavior: "smooth",
    });
  }

  return (
    <>
      <div className="home-carousel-head">
        <h2 className="section-title home-carousel-title">{title}</h2>
        <div className="home-carousel-controls">
          <span className="helper-chip">{videos.length} 个视频</span>
        </div>
      </div>

      {videos.length > 0 ? (
        <div className="home-carousel-shell">
          <div className="home-carousel-stage" ref={viewportRef}>
            <div className="home-carousel-track">
              {videos.map((video) => (
                <div
                  className="home-carousel-item"
                  key={video.video_id}
                  style={cardWidth > 0 ? ({ width: `${cardWidth}px`, flexBasis: `${cardWidth}px` } as CSSProperties) : undefined}
                >
                  <VideoCard
                    video={video}
                    onToggleFavorite={onToggleFavorite}
                  />
                </div>
              ))}
            </div>
          </div>
          {isScrollable ? (
            <div className="home-carousel-footer">
              <div className="home-carousel-nav-row">
                <span className="home-carousel-page-indicator">
                  {currentPage} / {totalPages}
                </span>
                <div className="home-carousel-nav-group">
                  <button
                    className="home-carousel-button home-carousel-button-left"
                    type="button"
                    onClick={() => scrollByPage("left")}
                    disabled={!canScrollLeft}
                    aria-label={`${title}上一页`}
                  >
                    <IconChevron direction="left" />
                  </button>
                  <button
                    className="home-carousel-button home-carousel-button-right"
                    type="button"
                    onClick={() => scrollByPage("right")}
                    disabled={!canScrollRight}
                    aria-label={`${title}下一页`}
                  >
                    <IconChevron direction="right" />
                  </button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function IconChevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" viewBox="0 0 24 24" aria-hidden="true">
      {direction === "left" ? <path d="m15 18-6-6 6-6" /> : <path d="m9 6 6 6-6 6" />}
    </svg>
  );
}
