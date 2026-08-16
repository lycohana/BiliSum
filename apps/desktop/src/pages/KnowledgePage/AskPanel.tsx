import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { SVGProps } from "react";
import { Link } from "react-router-dom";

import { MarkdownContent } from "../../components/MarkdownContent";
import type { KnowledgeJob, KnowledgeReference, KnowledgeReferenceItem, KnowledgeSourceRef, KnowledgeSuggestion, KnowledgeToolTrace } from "../../types";

export type KnowledgeChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  sources?: KnowledgeSourceRef[];
  tools?: KnowledgeToolTrace[];
  status?: "streaming" | "completed" | "error" | "interrupted" | "waiting_tasks";
  references?: KnowledgeReference[];
  job?: KnowledgeJob;
};

type AskPanelProps = {
  query: string;
  onQueryChange(value: string): void;
  onSubmit(): void;
  onStop(): void;
  onNewConversation(): void;
  onPickSuggestion(value: string): void;
  disabled: boolean;
  loading: boolean;
  hasContext: boolean;
  messages: KnowledgeChatMessage[];
  recentQueries: string[];
  suggestions?: KnowledgeSuggestion[];
  references: KnowledgeReference[];
  referenceOptions: KnowledgeReferenceItem[];
  referenceQuery: string;
  referenceOpen: boolean;
  onReferenceQuery(value: string): void;
  onReferenceSelect(item: KnowledgeReferenceItem): void;
  onReferenceRemove(id: string): void;
  onRetry(message: KnowledgeChatMessage): void;
  suggestionsRefreshing?: boolean;
  onRefreshSuggestions?(): void;
  onReaggregate?(jobId: string): void;
};

const DEFAULT_SUGGESTIONS = [
  "我最近主要在学什么主题？",
  "帮我串一下答题卡识别这条知识线。",
  "有哪些视频反复讲 OpenCV / 深度学习？",
  "把最近内容整理成一份复习提纲。",
];

function renderToolMeta(meta: KnowledgeToolTrace["meta"]) {
  if (!meta) {
    return null;
  }
  const sourceItems = Array.isArray(meta.sources) ? meta.sources : [];
  const pairs = Object.entries(meta).filter(([key]) => key !== "sources" && key !== "reasoning_character_count");
  const reasoningCharacterCount = Number(meta.reasoning_character_count || 0);
  return (
    <div className="knowledge-tool-meta">
      {reasoningCharacterCount > 0 ? (
        <span className="helper-chip">已收到推理流</span>
      ) : null}
      {pairs.map(([key, value]) => (
        <span key={key} className="helper-chip">
          {key.replace(/_/g, " ")} {String(value)}
        </span>
      ))}
      {sourceItems.length ? (
        <div className="knowledge-tool-source-preview">
          {sourceItems.slice(0, 3).map((item, index) => {
            if (!item || typeof item !== "object") {
              return null;
            }
            const label = String((item as { title?: string }).title || `来源 ${index + 1}`);
            const timestamp = String((item as { timestamp?: string }).timestamp || "").trim();
            return (
              <span key={`${label}-${timestamp || index}`} className="knowledge-tool-source-chip">
                {label}{timestamp ? ` · ${timestamp}` : ""}
              </span>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function SummaryToolIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
      <path className="knowledge-tool-summary-icon-page" d="M6.5 4.5h5.25L15 7.75v7.75H6.5a1.5 1.5 0 0 1-1.5-1.5v-8a1.5 1.5 0 0 1 1.5-1.5Z" />
      <path className="knowledge-tool-summary-icon-fold" d="M11.75 4.5v3.25H15" />
      <path d="M8 10.25h4.25" />
      <path d="M8 12.75h3" />
    </svg>
  );
}

function MessageActionIcon({ kind }: { kind: "copy" | "retry" | "save" }) {
  if (kind === "retry") {
    return <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><path d="M4 8a6 6 0 1 1 1.6 6.7" /><path d="M4 4.5V8h3.5" /></svg>;
  }
  if (kind === "save") {
    return <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 3.5h7l3 3v10H5z" /><path d="M8 3.5v4h5" /><path d="M8 12.5h4" /></svg>;
  }
  return <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="6.5" y="6.5" width="8" height="9" rx="1.5" /><path d="M5 12.5H4.5A1.5 1.5 0 0 1 3 11V5.5A1.5 1.5 0 0 1 4.5 4h5.5A1.5 1.5 0 0 1 11.5 5v1.5" /></svg>;
}

function formatSourceMeta(source: KnowledgeSourceRef) {
  const parts: string[] = [];
  if (source.page_number) {
    parts.push(`P${source.page_number}`);
  }
  if (source.video_title && source.video_title !== source.title) {
    parts.push(source.video_title);
  }
  parts.push(`${Math.round(source.relevance_score * 100)}%`);
  if (source.timestamp) {
    parts.push(source.timestamp);
  }
  return parts.join(" · ");
}

function buildSourceCards(sources: KnowledgeSourceRef[] = []) {
  const sourceMap = new Map<string, KnowledgeSourceRef & { timestamps: string[] }>();
  sources.forEach((source) => {
    const key = source.video_id || source.title;
    const existing = sourceMap.get(key);
    const timestamps = source.timestamp ? [source.timestamp] : [];
    if (!existing) {
      sourceMap.set(key, { ...source, timestamps });
      return;
    }
    const nextTimestamps = [...existing.timestamps];
    timestamps.forEach((timestamp) => {
      if (!nextTimestamps.includes(timestamp)) {
        nextTimestamps.push(timestamp);
      }
    });
    if (source.relevance_score > existing.relevance_score) {
      sourceMap.set(key, { ...source, timestamps: nextTimestamps });
      return;
    }
    existing.timestamps = nextTimestamps;
  });
  return [...sourceMap.values()].slice(0, 4);
}

function ToolTimeline({ tools = [] }: { tools?: KnowledgeToolTrace[] }) {
  if (!tools.length) {
    return null;
  }
  const runningCount = tools.filter((tool) => tool.status === "running").length;
  const errorCount = tools.filter((tool) => tool.status === "error").length;
  const completedCount = tools.filter((tool) => tool.status === "completed").length;
  const runningTool = tools.find((tool) => tool.status === "running") || null;
  const runningToolIndex = runningTool ? tools.findIndex((tool) => tool.id === runningTool.id) : -1;
  const traceStatus = runningCount ? "running" : errorCount ? "error" : "completed";
  const titleText = runningTool
    ? `正在调用：${runningTool.label}`
    : errorCount
      ? "工具链需要注意"
      : "工具链";
  const statusText = runningCount
    ? `第 ${runningToolIndex + 1 || completedCount + 1}/${tools.length} 步`
    : errorCount
      ? `${errorCount} 个工具失败`
      : `已完成 ${completedCount} 个工具`;
  return (
    <details className={`knowledge-tool-trace is-${traceStatus}`}>
      <summary>
        <span className="knowledge-tool-summary-main">
          <span className="knowledge-tool-summary-icon">
            <SummaryToolIcon />
          </span>
          <span className="knowledge-tool-summary-title">{titleText}</span>
          {runningTool ? (
            <span className="knowledge-tool-summary-progress" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
          ) : null}
        </span>
        <span className="knowledge-tool-summary-status" aria-live="polite">{statusText}</span>
      </summary>
      <div className="knowledge-tool-list">
        {tools.map((tool, index) => (
          <article key={tool.id} className={`knowledge-tool-card is-${tool.status}`}>
            <div className="knowledge-tool-head">
              <div className="knowledge-tool-status">
                <span className="knowledge-tool-status-dot" />
                <span className="knowledge-tool-step-index">{index + 1}</span>
                <strong>{tool.label}</strong>
              </div>
              <span className="knowledge-tool-status-text">
                {tool.status === "running" ? "运行中" : tool.status === "error" ? "失败" : "完成"}
              </span>
            </div>
            {tool.detail ? <p>{tool.detail}</p> : null}
            {renderToolMeta(tool.meta)}
          </article>
        ))}
      </div>
    </details>
  );
}

export function AskPanel({
  query,
  onQueryChange,
  onSubmit,
  onStop,
  onNewConversation,
  onPickSuggestion,
  disabled,
  loading,
  hasContext,
  messages,
  recentQueries,
  suggestions = [],
  references,
  referenceOptions,
  referenceQuery,
  referenceOpen,
  onReferenceQuery,
  onReferenceSelect,
  onReferenceRemove,
  onRetry,
  suggestionsRefreshing = false,
  onRefreshSuggestions,
  onReaggregate,
}: AskPanelProps) {
  const threadRef = useRef<HTMLDivElement | null>(null);
  const threadContentRef = useRef<HTMLDivElement | null>(null);
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const previousMessageCountRef = useRef(0);
  const [showScrollBottom, setShowScrollBottom] = useState(false);

  const suggestionItems = useMemo(() => {
    const seen = new Set<string>();
    const merged = [...suggestions.map((item) => item.text), ...recentQueries, ...DEFAULT_SUGGESTIONS];
    return merged.filter((item) => {
      const cleaned = item.trim();
      if (!cleaned || seen.has(cleaned)) {
        return false;
      }
      seen.add(cleaned);
      return true;
    }).slice(0, 6);
  }, [recentQueries, suggestions]);

  const hasMessages = messages.length > 0;
  const lastMessage = hasMessages ? messages[messages.length - 1] : null;
  const lastToolSignature = useMemo(() => {
    return (lastMessage?.tools || [])
      .map((tool) => `${tool.id}:${tool.status}:${tool.detail || ""}`)
      .join("|");
  }, [lastMessage?.tools]);
  const latestAssistantIsStreaming = lastMessage?.role === "assistant" && lastMessage.status === "streaming";

  const updateScrollState = useCallback(() => {
    const element = threadRef.current;
    if (!element) {
      return;
    }
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    const isNearBottom = distanceFromBottom < 160;
    shouldAutoScrollRef.current = isNearBottom;
    setShowScrollBottom(hasMessages && !isNearBottom);
  }, [hasMessages]);

  const scrollThreadToBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    const element = threadRef.current;
    if (!element) {
      return;
    }
    element.scrollTo({ top: element.scrollHeight, behavior });
    shouldAutoScrollRef.current = true;
    setShowScrollBottom(false);
  }, []);

  useLayoutEffect(() => {
    if (!hasMessages) {
      previousMessageCountRef.current = 0;
      return;
    }
    const hasNewMessage = messages.length > previousMessageCountRef.current;
    previousMessageCountRef.current = messages.length;
    if (hasNewMessage) {
      shouldAutoScrollRef.current = true;
    }
    if (shouldAutoScrollRef.current || hasNewMessage) {
      scrollThreadToBottom(latestAssistantIsStreaming ? "auto" : "smooth");
    } else {
      updateScrollState();
    }
  }, [
    hasMessages,
    latestAssistantIsStreaming,
    lastMessage?.id,
    lastMessage?.content,
    lastMessage?.reasoning,
    lastMessage?.sources?.length,
    lastMessage?.status,
    lastMessage?.role,
    lastToolSignature,
    messages.length,
    scrollThreadToBottom,
    updateScrollState,
  ]);

  useEffect(() => {
    if (!latestAssistantIsStreaming) {
      return;
    }
    const contentElement = threadContentRef.current;
    if (!contentElement) {
      return;
    }
    let frameId = 0;
    const scheduleScroll = () => {
      if (!shouldAutoScrollRef.current) {
        updateScrollState();
        return;
      }
      window.cancelAnimationFrame(frameId);
      frameId = window.requestAnimationFrame(() => {
        scrollThreadToBottom("auto");
        updateScrollState();
      });
    };
    scheduleScroll();
    const observer = new ResizeObserver(scheduleScroll);
    observer.observe(contentElement);
    return () => {
      window.cancelAnimationFrame(frameId);
      observer.disconnect();
    };
  }, [latestAssistantIsStreaming, lastMessage?.id, scrollThreadToBottom, updateScrollState]);

  return (
    <div className={`knowledge-chat-shell knowledge-chat-shell-gpt ${hasMessages ? "has-thread" : "is-empty"}`}>
      <div ref={threadRef} className="knowledge-chat-scroll" onScroll={updateScrollState}>
        {!hasMessages ? (
          <div className="knowledge-chat-welcome">
            <span className="library-kicker">知识库助手</span>
            <h2>今天想从哪里继续？</h2>
            <p>问一段视频、梳理最近所学，或直接粘贴链接创建总结任务。</p>
            <div className="knowledge-chat-suggestion-heading">
              <span>基于你的最近内容</span>
              {onRefreshSuggestions ? (
                <button
                  type="button"
                  className={suggestionsRefreshing ? "is-refreshing" : undefined}
                  aria-label={suggestionsRefreshing ? "正在刷新推荐问题" : "刷新推荐问题"}
                  aria-busy={suggestionsRefreshing}
                  title={suggestionsRefreshing ? "正在刷新" : "刷新推荐问题"}
                  disabled={suggestionsRefreshing}
                  onClick={onRefreshSuggestions}
                >
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><path d="M16 8a6 6 0 1 0 1 4" /><path d="M16 4v4h-4" /></svg>
                </button>
              ) : null}
            </div>
            <div className="knowledge-chat-suggestion-grid">
              {suggestionItems.map((item) => (
                <button key={item} className="knowledge-chat-suggestion-card" type="button" onClick={() => onPickSuggestion(item)}>
                  {item}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div ref={threadContentRef} className="knowledge-chat-thread">
            {messages.map((message) => (
              <article key={message.id} className={`knowledge-chat-message-row ${message.role === "user" ? "is-user" : "is-assistant"}`}>
                <div className={`knowledge-chat-bubble ${message.role === "user" ? "is-user" : "is-assistant"}`}>
                  {message.role === "assistant" ? (
                    <div className="knowledge-chat-message-meta">
                      <strong>知识库助手</strong>
                      {message.status === "streaming" ? (
                        <span className="knowledge-streaming-indicator">正在输出</span>
                      ) : null}
                    </div>
                  ) : null}
                  {message.role === "assistant" ? (
                    <>
                      <div className={`knowledge-chat-answer-card ${message.status === "error" ? "is-error" : ""}`}>
                        <ToolTimeline tools={message.tools} />
                        {message.reasoning ? (
                          <details className="knowledge-chat-reasoning" open={message.status === "streaming"}>
                            <summary>
                              <span>思考</span>
                              <small>{message.status === "streaming" ? "正在更新" : "已完成"}</small>
                            </summary>
                            <pre>{message.reasoning}</pre>
                          </details>
                        ) : null}
                        <MarkdownContent className="knowledge-chat-markdown" content={message.content || (message.status === "streaming" ? "正在整理回答..." : "")} />
                        {message.job ? (
                          <div className="knowledge-chat-job-card" aria-live="polite">
                            <div className="knowledge-chat-job-head"><strong>视频总结任务</strong><span>{message.job.progress}% · Agent 第 {message.job.agent_round} 轮：{message.job.agent_phase === "agent_review" ? "审阅中" : message.job.agent_phase === "agent_writing" ? "写作中" : message.job.agent_phase === "completed" ? "已完成" : "等待视频任务"} {message.job.status === "completed" && onReaggregate ? <button type="button" aria-label="重新聚合" title="重新聚合" onClick={() => onReaggregate(message.job?.job_id || "")}><MessageActionIcon kind="retry" /></button> : null}</span></div>
                            <div className="knowledge-chat-job-progress"><i style={{ width: `${message.job.progress}%` }} /></div>
                            <div className="knowledge-chat-job-items">
                              {message.job.items.map((item) => (
                                item.video_id ? (
                                  <Link key={item.task_id} className={`is-${item.status}`} to={`/videos/${item.video_id}`} title={item.error_message || item.message}>{item.status === "completed" ? "✓" : item.status === "failed" ? "!" : "·"} {item.title}</Link>
                                ) : (
                                  <span key={item.task_id} className={`is-${item.status}`} title={item.error_message || item.message}>{item.status === "completed" ? "✓" : item.status === "failed" ? "!" : "·"} {item.title}</span>
                                )
                              ))}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    </>
                  ) : (
                    <>
                      {message.references?.length ? (
                        <div className="knowledge-chat-message-reference-row">
                          {message.references.map((reference) => <span key={`${reference.kind}-${reference.id}`}>@{reference.label}</span>)}
                        </div>
                      ) : null}
                      <p className="knowledge-chat-user-text">{message.content}</p>
                    </>
                  )}
                  {message.role === "assistant" && message.sources?.length ? (
                    <details className="knowledge-chat-source-drawer">
                      <summary>
                        <span>参考来源</span>
                        <strong>{buildSourceCards(message.sources).length}</strong>
                      </summary>
                      <div className="knowledge-chat-sources">
                        {buildSourceCards(message.sources).map((source) => (
                          <Link key={source.video_id || source.title} className="knowledge-chat-source-card" to={`/videos/${source.video_id}`}>
                            <strong>{source.title}</strong>
                            <span>{formatSourceMeta(source)}</span>
                            {source.timestamps.length ? <small>{source.timestamps.slice(0, 3).join(" / ")}</small> : null}
                          </Link>
                        ))}
                      </div>
                    </details>
                  ) : null}
                  {message.role === "assistant" && message.status !== "streaming" ? (
                    <div className="knowledge-chat-message-actions">
                      <button type="button" aria-label="复制 Markdown" title="复制 Markdown" onClick={() => void navigator.clipboard?.writeText(message.content)}><MessageActionIcon kind="copy" /></button>
                      <button type="button" aria-label="重新回答" title="重新回答" onClick={() => onRetry(message)}><MessageActionIcon kind="retry" /></button>
                      <button type="button" aria-label="保存 Markdown" title="保存 Markdown" onClick={() => {
                        const blob = new Blob([message.content], { type: "text/markdown;charset=utf-8" });
                        const url = URL.createObjectURL(blob);
                        const anchor = document.createElement("a");
                        anchor.href = url;
                        anchor.download = "bilisum-answer.md";
                        anchor.click();
                        URL.revokeObjectURL(url);
                      }}><MessageActionIcon kind="save" /></button>
                    </div>
                  ) : null}
                </div>
              </article>
            ))}
            <div ref={threadEndRef} className="knowledge-chat-thread-end" aria-hidden="true" />
          </div>
        )}
      </div>

      {showScrollBottom ? (
        <button
          className="knowledge-chat-scroll-bottom"
          type="button"
          onClick={() => scrollThreadToBottom("smooth")}
          aria-label="滚动到底部"
          title="滚动到底部"
        >
          <span aria-hidden="true">↓</span>
        </button>
      ) : null}

      <div className="knowledge-chat-composer-dock">
        <div className="knowledge-chat-composer-surface">
          {references.length ? (
            <div className="knowledge-chat-reference-chips" aria-label="已引用内容">
              {references.map((reference) => (
                <button key={`${reference.kind}-${reference.id}`} type="button" className="knowledge-chat-reference-chip" onClick={() => onReferenceRemove(reference.id)} title="移除引用">
                  @{reference.label}<span aria-hidden="true">×</span>
                </button>
              ))}
            </div>
          ) : null}
          <textarea
            className="textarea-field knowledge-chat-textarea"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="问视频内容、学习主题，或粘贴链接开始总结"
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                onSubmit();
              }
            }}
          />
          {referenceOpen ? (
            <div className="knowledge-reference-popover" role="listbox" aria-label="引用视频或收藏夹">
              <input className="knowledge-reference-search" value={referenceQuery} onChange={(event) => onReferenceQuery(event.target.value)} placeholder="搜索视频或收藏夹" autoFocus />
              {referenceOptions.map((item) => (
                <button key={`${item.kind}-${item.id}`} type="button" role="option" onClick={() => onReferenceSelect(item)}>
                  <strong>@{item.label}</strong><span>{item.kind === "folder" ? "收藏夹" : "视频"} · {item.subtitle}</span>
                </button>
              ))}
            </div>
          ) : null}
          <div className="knowledge-chat-composer-footer">
            <div className="knowledge-chat-composer-hint">
              Enter 发送，Shift + Enter 换行
            </div>
            <div className="knowledge-chat-action-row">
              <button
                className="secondary-button knowledge-chat-new-session"
                type="button"
                onClick={onNewConversation}
                disabled={!hasContext}
                aria-label="新建会话"
                title="新建会话"
              >
                <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 3.5h7l3 3v10H5z" /><path d="M12 3.5v3h3M8 11h4M10 9v4" /></svg>
              </button>
              {loading ? (
                <button className="secondary-button" type="button" onClick={onStop}>
                  停止回答
                </button>
              ) : null}
              <button className="primary-button knowledge-chat-submit" type="button" onClick={onSubmit} disabled={disabled || loading}>
                {loading ? "回答中..." : "发送"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
