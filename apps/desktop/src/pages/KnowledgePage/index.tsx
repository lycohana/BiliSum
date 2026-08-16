import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../api";
import { AskPanel, type KnowledgeChatMessage } from "./AskPanel";
import { SearchPanel } from "./SearchPanel";
import { TagManager } from "./TagManager";
import { TagNetwork } from "./TagNetwork";
import type {
  KnowledgeChatHistoryItem,
  KnowledgeAskResponse,
  KnowledgeConversation,
  KnowledgeJob,
  KnowledgeMessage,
  KnowledgeReference,
  KnowledgeReferenceItem,
  KnowledgeSuggestionsResponse,
  KnowledgeNetworkResponse,
  KnowledgeSearchResult,
  KnowledgeStatsResponse,
  KnowledgeTagItem,
} from "../../types";

type KnowledgeView = "chat" | "workspace";

function createMessage(
  role: "user" | "assistant",
  content: string,
  options: Partial<Pick<KnowledgeChatMessage, "reasoning" | "sources" | "tools" | "status" | "references" | "job">> = {},
) {
  return {
    id: `${role}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role,
    content,
    reasoning: options.reasoning || "",
    sources: options.sources || [],
    tools: options.tools || [],
    status: options.status,
    references: options.references || [],
    job: options.job,
  } satisfies KnowledgeChatMessage;
}

function fromKnowledgeMessage(message: KnowledgeMessage): KnowledgeChatMessage {
  return {
    id: message.message_id,
    role: message.role === "user" ? "user" : "assistant",
    content: message.content,
    reasoning: message.reasoning,
    sources: message.sources,
    tools: message.tools,
    status: message.status,
    references: message.references,
    job: message.job_id ? {
      job_id: message.job_id,
      conversation_id: message.conversation_id,
      assistant_message_id: message.message_id,
      kind: "multi_video_summary",
      status: message.status,
      query: "",
      progress: 0,
      items: [],
      agent_round: 1,
      agent_phase: "waiting_tasks",
      created_at: message.created_at,
      updated_at: message.updated_at,
    } : undefined,
  };
}

export function KnowledgePage() {
  const navigate = useNavigate();
  const askAbortRef = useRef<AbortController | null>(null);
  const activeConversationIdRef = useRef<string | null>(null);
  const [activeView, setActiveView] = useState<KnowledgeView>("chat");
  const [stats, setStats] = useState<KnowledgeStatsResponse | null>(null);
  const [tags, setTags] = useState<KnowledgeTagItem[]>([]);
  const [network, setNetwork] = useState<KnowledgeNetworkResponse>({ nodes: [], links: [], selected_tags: [] });
  const [networkExpanded, setNetworkExpanded] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeSearchResult[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [askQuery, setAskQuery] = useState("");
  const [chatMessages, setChatMessages] = useState<KnowledgeChatMessage[]>([]);
  const [conversations, setConversations] = useState<KnowledgeConversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [conversationMenu, setConversationMenu] = useState<{ conversationId: string; x: number; y: number } | null>(null);
  const [suggestions, setSuggestions] = useState<KnowledgeSuggestionsResponse["suggestions"]>([]);
  const [suggestionsRefreshing, setSuggestionsRefreshing] = useState(false);
  const [references, setReferences] = useState<KnowledgeReference[]>([]);
  const [referenceOptions, setReferenceOptions] = useState<KnowledgeReferenceItem[]>([]);
  const [referenceQuery, setReferenceQuery] = useState("");
  const [referenceOpen, setReferenceOpen] = useState(false);
  const [recentQueries, setRecentQueries] = useState<string[]>([]);
  const [asking, setAsking] = useState(false);
  const [askingConversationId, setAskingConversationId] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [managingVideoId, setManagingVideoId] = useState<string | null>(null);
  const [maintenanceMenuOpen, setMaintenanceMenuOpen] = useState(false);
  const [maintenanceBusy, setMaintenanceBusy] = useState(false);
  const maintenanceMenuRef = useRef<HTMLDivElement | null>(null);
  const conversationMenuRef = useRef<HTMLDivElement | null>(null);
  const conversationMenuFirstItemRef = useRef<HTMLButtonElement | null>(null);
  const pendingDeltaRef = useRef<{ messageId: string; text: string }>({ messageId: "", text: "" });
  const pendingDeltaTimerRef = useRef<number | null>(null);

  const videoTitleMap = useMemo(() => {
    return Object.fromEntries(
      network.nodes
        .filter((node) => node.type === "video")
        .map((node) => [String(node.id).replace(/^video_/, ""), node.label]),
    );
  }, [network.nodes]);

  const statsSummary = useMemo(() => ([
    { label: "视频", value: stats?.video_count ?? 0 },
    { label: "标签", value: stats?.tag_count ?? 0 },
    { label: "索引片段", value: stats?.indexed_chunk_count ?? 0 },
    { label: "未标记", value: stats?.untagged_video_count ?? 0 },
  ]), [stats]);

  const activeConversation = useMemo(
    () => conversations.find((item) => item.conversation_id === activeConversationId) || null,
    [activeConversationId, conversations],
  );

  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);
  const statusTone = /失败|错误|异常|HTTP\s*[45]\d\d/i.test(status)
    ? "danger"
    : /正在|处理中|维护中/.test(status)
      ? "progress"
      : "success";

  async function refreshBase(nextSelectedTags: string[] = selectedTags, expanded: boolean = networkExpanded) {
    const [tagPayload, networkPayload, statsPayload] = await Promise.all([
      api.getKnowledgeTags(),
      api.getKnowledgeNetwork({
        selectedTags: nextSelectedTags,
        maxTags: expanded ? 20 : 12,
        maxVideos: expanded ? 10 : 8,
      }),
      api.getKnowledgeStats(),
    ]);
    if ("items" in tagPayload) {
      setTags(tagPayload.items as KnowledgeTagItem[]);
    }
    setNetwork(networkPayload);
    setStats(statsPayload);
    setStatus("");
  }

  async function refreshSuggestions() {
    if (suggestionsRefreshing) {
      return;
    }
    setSuggestionsRefreshing(true);
    try {
      const [payload] = await Promise.all([
        api.getKnowledgeSuggestions(),
        new Promise<void>((resolve) => window.setTimeout(resolve, 360)),
      ]);
      setSuggestions(payload.suggestions);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "刷新推荐问题失败");
    } finally {
      setSuggestionsRefreshing(false);
    }
  }

  useEffect(() => {
    void refreshBase().catch((error) => {
      setStatus(error instanceof Error ? error.message : "知识库初始化失败");
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    void api.getKnowledgeSuggestions().then((suggestionPayload) => {
      if (!cancelled) {
        setSuggestions(suggestionPayload.suggestions);
      }
    }).catch(() => undefined);
    void api.listKnowledgeConversations().then(async (conversationPayload) => {
      if (cancelled) {
        return;
      }
      setConversations(conversationPayload);
      let target = conversationPayload[0];
      if (!target) {
        target = await api.createKnowledgeConversation();
        if (cancelled) {
          return;
        }
        setConversations([target]);
      }
      setActiveConversationId(target.conversation_id);
    }).catch((error) => {
      if (!cancelled) {
        setStatus(error instanceof Error ? error.message : "会话初始化失败");
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!activeConversationId) {
      return;
    }
    let cancelled = false;
    void api.getKnowledgeMessages(activeConversationId).then((payload) => {
      if (!cancelled) {
        setChatMessages(payload.messages.filter((message) => message.role !== "system").map(fromKnowledgeMessage));
      }
    }).catch((error) => {
      if (!cancelled) {
        setStatus(error instanceof Error ? error.message : "加载会话失败");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeConversationId]);

  useEffect(() => {
    if (!referenceOpen) {
      return;
    }
    const timer = window.setTimeout(() => {
      void api.getKnowledgeReferences(referenceQuery).then((payload) => setReferenceOptions(payload.items)).catch(() => setReferenceOptions([]));
    }, 120);
    return () => window.clearTimeout(timer);
  }, [referenceOpen, referenceQuery]);

  useEffect(() => {
    const jobIds = chatMessages.map((message) => message.job?.job_id).filter((jobId): jobId is string => Boolean(jobId));
    if (!jobIds.length) {
      return;
    }
    let cancelled = false;
    const poll = () => {
      void Promise.all(jobIds.map((jobId) => api.getKnowledgeJob(jobId))).then((jobs) => {
        if (cancelled) {
          return;
        }
        setChatMessages((current) => current.map((message) => {
          const currentJob = message.job;
          const job = currentJob ? jobs.find((item) => item.job_id === currentJob.job_id) : undefined;
          if (!job) {
            return message;
          }
          return {
            ...message,
            job,
            status: job.status === "completed" ? "completed" : message.status === "error" ? "error" : "waiting_tasks",
          };
        }));
        if (jobs.some((job) => job.status === "completed")) {
          const jobsById = new Map(jobs.map((job) => [job.job_id, job]));
          void api.getKnowledgeMessages(activeConversationId || "").then((payload) => {
            if (!cancelled) {
              const loadedMessages = payload.messages.filter((message) => message.role !== "system").map(fromKnowledgeMessage);
              setChatMessages((current) => loadedMessages.map((message) => {
                const existing = current.find((item) => item.id === message.id);
                const jobId = message.job?.job_id || existing?.job?.job_id;
                const job = jobId ? jobsById.get(jobId) : undefined;
                return job ? { ...message, job } : message;
              }));
            }
          });
        }
      }).catch(() => undefined);
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeConversationId, chatMessages.map((message) => message.job?.job_id).join(",")]);

  useEffect(() => {
    void refreshBase(selectedTags, networkExpanded).catch((error) => {
      setStatus(error instanceof Error ? error.message : "刷新知识库概览失败");
    });
  }, [selectedTags, networkExpanded]);

  useEffect(() => {
    if (activeView !== "workspace") {
      return;
    }
    const trimmed = query.trim();
    const timer = window.setTimeout(() => {
      if (!trimmed) {
        setResults([]);
        setSearching(false);
        return;
      }
      setSearching(true);
      void api.searchKnowledge({
        query: trimmed,
        limit: 12,
        filters: selectedTags.length ? { tags: selectedTags } : undefined,
      }).then((payload) => {
        setResults(payload.results);
      }).catch((error) => {
        setStatus(error instanceof Error ? error.message : "搜索失败");
      }).finally(() => {
        setSearching(false);
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [activeView, query, selectedTags]);

  useEffect(() => {
    if (!maintenanceMenuOpen) {
      return;
    }
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && maintenanceMenuRef.current?.contains(target)) {
        return;
      }
      setMaintenanceMenuOpen(false);
    };
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [maintenanceMenuOpen]);

  useEffect(() => {
    if (!conversationMenu) {
      return;
    }
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && conversationMenuRef.current?.contains(target)) {
        return;
      }
      setConversationMenu(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setConversationMenu(null);
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.requestAnimationFrame(() => conversationMenuFirstItemRef.current?.focus());
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [conversationMenu]);

  useEffect(() => {
    return () => {
      if (pendingDeltaTimerRef.current !== null) {
        window.clearTimeout(pendingDeltaTimerRef.current);
      }
    };
  }, []);

  function toggleTag(tag: string) {
    setSelectedTags((current) => current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag]);
  }

  function pushRecentQuery(value: string) {
    setRecentQueries((current) => [value, ...current.filter((item) => item !== value)].slice(0, 6));
  }

  function updateMessage(messageId: string, updater: (message: KnowledgeChatMessage) => KnowledgeChatMessage) {
    setChatMessages((current) => current.map((message) => message.id === messageId ? updater(message) : message));
  }

  function flushPendingAssistantDelta() {
    if (pendingDeltaTimerRef.current !== null) {
      window.clearTimeout(pendingDeltaTimerRef.current);
      pendingDeltaTimerRef.current = null;
    }
    const pending = pendingDeltaRef.current;
    if (!pending.messageId || !pending.text) {
      return;
    }
    const { messageId, text } = pending;
    pendingDeltaRef.current = { messageId, text: "" };
    updateMessage(messageId, (message) => ({ ...message, content: `${message.content}${text}` }));
  }

  function enqueueAssistantDelta(messageId: string, delta: string) {
    if (!delta) {
      return;
    }
    const pending = pendingDeltaRef.current;
    if (pending.messageId !== messageId) {
      flushPendingAssistantDelta();
      pendingDeltaRef.current = { messageId, text: "" };
    }
    pendingDeltaRef.current.text += delta;
    if (pendingDeltaTimerRef.current === null) {
      pendingDeltaTimerRef.current = window.setTimeout(flushPendingAssistantDelta, 48);
    }
  }

  function buildAskHistory(): KnowledgeChatHistoryItem[] {
    return chatMessages
      .filter((message) => message.content.trim() && message.status !== "streaming")
      .slice(-8)
      .map((message) => ({
        role: message.role,
        content: message.content.trim().slice(0, 1200),
      }));
  }

  async function handleNewConversation() {
    flushPendingAssistantDelta();
    try {
      const conversation = await api.createKnowledgeConversation();
      setConversations((current) => [conversation, ...current]);
      setChatMessages([]);
      setActiveConversationId(conversation.conversation_id);
      setAskQuery("");
      setReferences([]);
      setStatus("");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "新建会话失败");
    }
  }

  function handleSelectConversation(conversationId: string, closeHistory = true) {
    flushPendingAssistantDelta();
    setActiveConversationId(conversationId);
    setChatMessages([]);
    setAskQuery("");
    setReferences([]);
    setReferenceOpen(false);
    setConversationMenu(null);
    if (closeHistory) {
      setHistoryOpen(false);
    }
  }

  function handleConversationContextMenu(event: MouseEvent<HTMLButtonElement>, conversationId: string) {
    event.preventDefault();
    setConversationMenu({
      conversationId,
      x: Math.min(event.clientX, Math.max(8, window.innerWidth - 168)),
      y: Math.min(event.clientY, Math.max(8, window.innerHeight - 108)),
    });
  }

  function handleRenameConversation(conversationId: string) {
    const conversation = conversations.find((item) => item.conversation_id === conversationId);
    const title = window.prompt("重命名会话", conversation?.title || "新会话");
    setConversationMenu(null);
    if (!title?.trim()) {
      return;
    }
    void api.renameKnowledgeConversation(conversationId, title.trim()).then((next) => {
      setConversations((items) => items.map((item) => item.conversation_id === next.conversation_id ? next : item));
    }).catch((error) => {
      setStatus(error instanceof Error ? error.message : "重命名会话失败");
    });
  }

  function handleDeleteConversation(conversationId: string) {
    const conversation = conversations.find((item) => item.conversation_id === conversationId);
    if (!window.confirm(`删除“${conversation?.title || "这个会话"}”？消息将一并删除。`)) {
      return;
    }
    setConversationMenu(null);
    void api.deleteKnowledgeConversation(conversationId).then(() => {
      const remaining = conversations.filter((item) => item.conversation_id !== conversationId);
      setConversations(remaining);
      if (conversationId !== activeConversationId) {
        return;
      }
      if (remaining[0]) {
        handleSelectConversation(remaining[0].conversation_id, false);
      } else {
        void handleNewConversation();
      }
    }).catch((error) => {
      setStatus(error instanceof Error ? error.message : "删除会话失败");
    });
  }

  function handleQueryChange(value: string) {
    setAskQuery(value);
    const match = value.match(/(?:^|\s)@([^\s@]*)$/);
    if (match) {
      setReferenceQuery(match[1] || "");
      setReferenceOpen(true);
    } else {
      setReferenceOpen(false);
    }
  }

  function handleReferenceSelect(item: KnowledgeReferenceItem) {
    setReferences((current) => current.some((reference) => reference.id === item.id) ? current : [...current, { kind: item.kind, id: item.id, label: item.label }]);
    setAskQuery((current) => current.replace(/(?:^|\s)@[^\s@]*$/, "").trimEnd());
    setReferenceQuery("");
    setReferenceOpen(false);
  }

  async function handleAsk(seedQuery?: string) {
    const effectiveQuery = String(seedQuery ?? askQuery).trim();
    if (!effectiveQuery || asking || !activeConversationId) {
      return;
    }
    const conversationId = activeConversationId;
    const history = buildAskHistory();
    const selectedReferences = [...references];
    setAsking(true);
    setAskingConversationId(conversationId);
    setAskQuery("");
    setStatus("");
    const controller = new AbortController();
    askAbortRef.current = controller;
    const userMessage = createMessage("user", effectiveQuery);
    const assistantMessage = createMessage("assistant", "", {
      status: "streaming",
      tools: [],
      sources: [],
      references: selectedReferences,
    });
    setChatMessages((current) => [...current, userMessage, assistantMessage]);
    pushRecentQuery(effectiveQuery);
    try {
      await api.streamKnowledgeAsk(
        { query: effectiveQuery, context_limit: 5, history, references: selectedReferences },
        {
          onTool: (tool) => {
            updateMessage(assistantMessage.id, (message) => {
              const existing = message.tools || [];
              const nextTools = existing.some((item) => item.id === tool.id)
                ? existing.map((item) => item.id === tool.id ? { ...item, ...tool } : item)
                : [...existing, tool];
              return { ...message, tools: nextTools };
            });
          },
          onTextDelta: (delta) => {
            enqueueAssistantDelta(assistantMessage.id, delta);
          },
          onReasoningDelta: (delta) => {
            updateMessage(assistantMessage.id, (message) => ({
              ...message,
              reasoning: `${message.reasoning || ""}${delta}`,
            }));
          },
          onSources: (sources) => {
            flushPendingAssistantDelta();
            updateMessage(assistantMessage.id, (message) => ({ ...message, sources }));
          },
          onDone: (payload) => {
            flushPendingAssistantDelta();
            updateMessage(assistantMessage.id, (message) => ({
              ...message,
              content: payload.answer || message.content,
              sources: payload.sources,
              status: (payload as KnowledgeAskResponse & { job_id?: string }).job_id ? "waiting_tasks" : "completed",
            }));
          },
          onJob: (job) => {
            updateMessage(assistantMessage.id, (message) => ({ ...message, job, status: job.status === "completed" ? "completed" : "waiting_tasks" }));
          },
          onError: (message) => {
            flushPendingAssistantDelta();
            if (activeConversationIdRef.current === conversationId) {
              setStatus(message);
            }
            updateMessage(assistantMessage.id, (current) => ({
              ...current,
              content: current.content || `知识库助手暂时没有顺利完成回答。\n\n${message}`,
              status: "error",
            }));
          },
        },
        { signal: controller.signal, conversationId },
      );
    } catch (error) {
      if (controller.signal.aborted) {
        flushPendingAssistantDelta();
        updateMessage(assistantMessage.id, (message) => ({
          ...message,
          content: message.content || "已停止本轮回答。",
          status: "completed",
        }));
        return;
      }
      const detail = error instanceof Error ? error.message : "问答失败";
      flushPendingAssistantDelta();
      if (activeConversationIdRef.current === conversationId) {
        setStatus(detail);
      }
      updateMessage(assistantMessage.id, (message) => ({
        ...message,
        content: message.content || `知识库助手暂时没有顺利完成回答。\n\n${detail}`,
        status: "error",
      }));
    } finally {
      flushPendingAssistantDelta();
      askAbortRef.current = null;
      setAsking(false);
      setAskingConversationId((current) => current === conversationId ? null : current);
      if (activeConversationIdRef.current === conversationId) {
        setReferences([]);
      }
      void api.listKnowledgeConversations().then(setConversations).catch(() => undefined);
    }
  }

  function handleStopAsk() {
    askAbortRef.current?.abort();
    askAbortRef.current = null;
    setAsking(false);
  }

  function handleRetry(message: KnowledgeChatMessage) {
    const index = chatMessages.findIndex((item) => item.id === message.id);
    const previous = index > 0 ? chatMessages.slice(0, index).reverse().find((item) => item.role === "user") : null;
    if (previous) {
      void handleAsk(previous.content);
    }
  }

  async function handleAutoTag() {
    if (maintenanceBusy) {
      return;
    }
    setStatus("正在为未标记视频自动打标...");
    try {
      setMaintenanceBusy(true);
      const payload = await api.autoTagKnowledge();
      setStatus(`已处理 ${payload.items.length} 个视频`);
      await refreshBase();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "自动打标失败");
    } finally {
      setMaintenanceBusy(false);
    }
  }

  async function handleRebuild() {
    if (maintenanceBusy) {
      return;
    }
    setStatus("正在重建知识库索引...");
    try {
      setMaintenanceBusy(true);
      const payload = await api.rebuildKnowledgeIndex();
      setStatus(`索引已重建，共处理 ${payload.indexed_videos} 个视频`);
      await refreshBase();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "索引重建失败");
    } finally {
      setMaintenanceBusy(false);
    }
  }

  async function handleAutoTagAndRebuild() {
    if (maintenanceBusy) {
      return;
    }
    setStatus("正在自动打标并重建知识库索引...");
    try {
      setMaintenanceBusy(true);
      const tagPayload = await api.autoTagKnowledge();
      const indexPayload = await api.rebuildKnowledgeIndex();
      setStatus(`已处理 ${tagPayload.items.length} 个标签任务，索引已重建 ${indexPayload.indexed_videos} 个视频`);
      await refreshBase();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "知识库维护失败");
    } finally {
      setMaintenanceBusy(false);
    }
  }

  return (
    <section className="knowledge-page knowledge-page-refined">
      <div className="knowledge-topbar">
        <div className="knowledge-topbar-leading">
          <div className="knowledge-view-tabs">
            <button className={`knowledge-view-tab ${activeView === "chat" ? "is-active" : ""}`} type="button" onClick={() => setActiveView("chat")}>
              AI 问答
            </button>
            <button className={`knowledge-view-tab ${activeView === "workspace" ? "is-active" : ""}`} type="button" onClick={() => setActiveView("workspace")}>
              工作台
            </button>
          </div>
          {activeView === "chat" ? (
            <button className="knowledge-history-trigger" type="button" onClick={() => setHistoryOpen(true)} aria-haspopup="dialog" aria-expanded={historyOpen}>
              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><path d="M4 5.5h12M4 10h12M4 14.5h8" /></svg>
              <span>{activeConversation?.title || "对话历史"}</span>
            </button>
          ) : null}
        </div>
        <div className="knowledge-topbar-actions">
          {statsSummary.map((item) => (
            <span key={item.label} className="helper-chip">{item.label} {item.value}</span>
          ))}
          <div className={`knowledge-maintenance-menu ${maintenanceMenuOpen ? "is-open" : ""}`} ref={maintenanceMenuRef}>
            <button
              className="secondary-button knowledge-maintenance-trigger"
              type="button"
              aria-haspopup="menu"
              aria-expanded={maintenanceMenuOpen}
              disabled={maintenanceBusy}
              onClick={() => setMaintenanceMenuOpen((current) => !current)}
            >
              {maintenanceBusy ? "维护中..." : "知识库维护"}
              <span className="knowledge-maintenance-caret" aria-hidden="true" />
            </button>
            {maintenanceMenuOpen ? (
              <div className="knowledge-maintenance-popover" role="menu" aria-label="知识库维护设置">
                <button
                  className="knowledge-maintenance-item"
                  type="button"
                  role="menuitem"
                  disabled={maintenanceBusy}
                  onClick={() => {
                    setMaintenanceMenuOpen(false);
                    void handleRebuild();
                  }}
                >
                  <strong>重建索引</strong>
                  <span>重新扫描已有结果并刷新检索索引。</span>
                </button>
                <button
                  className="knowledge-maintenance-item"
                  type="button"
                  role="menuitem"
                  disabled={maintenanceBusy || !stats?.knowledge_llm_available}
                  onClick={() => {
                    setMaintenanceMenuOpen(false);
                    void handleAutoTag();
                  }}
                >
                  <strong>自动打标未标记视频</strong>
                  <span>使用知识库 LLM 为未标记内容补充标签。</span>
                </button>
                <button
                  className="knowledge-maintenance-item"
                  type="button"
                  role="menuitem"
                  disabled={maintenanceBusy || !stats?.knowledge_llm_available}
                  onClick={() => {
                    setMaintenanceMenuOpen(false);
                    void handleAutoTagAndRebuild();
                  }}
                >
                  <strong>打标后重建索引</strong>
                  <span>先补齐标签，再一次性刷新索引。</span>
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {status ? <div className={`knowledge-status-banner ${statusTone}`} role={statusTone === "danger" ? "alert" : "status"}>{status}</div> : null}
      {!stats?.knowledge_llm_available ? <div className="knowledge-status-banner warning">知识库 LLM 未就绪；系统信息问答和本地推荐仍可用，内容问答与自动打标需要先配置模型。</div> : null}

      {activeView === "chat" ? (
        <div className="knowledge-chat-layout">
          {historyOpen ? (
            <>
              <button className="knowledge-history-backdrop" type="button" aria-label="关闭对话历史" onClick={() => { setConversationMenu(null); setHistoryOpen(false); }} />
              <aside className="knowledge-conversation-sidebar" aria-label="对话历史" role="dialog" aria-modal="true">
                <div className="knowledge-conversation-sidebar-head">
                  <div><strong>对话历史</strong><span>{conversations.length} 个会话</span></div>
                  <div className="knowledge-conversation-head-actions">
                    <button type="button" aria-label="新建会话" title="新建会话" onClick={() => { void handleNewConversation(); setHistoryOpen(false); }}>＋</button>
                    <button type="button" aria-label="关闭对话历史" title="关闭" onClick={() => { setConversationMenu(null); setHistoryOpen(false); }}>×</button>
                  </div>
                </div>
                <div className="knowledge-conversation-list">
                  {conversations.map((conversation) => (
                    <button key={conversation.conversation_id} type="button" className={`knowledge-conversation-item ${conversation.conversation_id === activeConversationId ? "is-active" : ""}`} aria-haspopup="menu" onClick={() => handleSelectConversation(conversation.conversation_id)} onContextMenu={(event) => handleConversationContextMenu(event, conversation.conversation_id)}>
                      <strong>{conversation.title}</strong>
                      <span>{conversation.preview || "还没有消息"}</span>
                    </button>
                  ))}
                </div>
                {conversationMenu ? (
                  <div ref={conversationMenuRef} className="knowledge-conversation-context-menu" role="menu" aria-label="会话操作" style={{ left: conversationMenu.x, top: conversationMenu.y }}>
                    <button ref={conversationMenuFirstItemRef} type="button" role="menuitem" onClick={() => handleRenameConversation(conversationMenu.conversationId)}>重命名</button>
                    <button type="button" role="menuitem" className="is-danger" onClick={() => handleDeleteConversation(conversationMenu.conversationId)}>删除</button>
                  </div>
                ) : null}
              </aside>
            </>
          ) : null}
          <AskPanel
            query={askQuery}
            onQueryChange={handleQueryChange}
            onSubmit={() => void handleAsk()}
            onStop={handleStopAsk}
            onNewConversation={() => void handleNewConversation()}
            onPickSuggestion={(value) => {
              handleQueryChange(value);
              void handleAsk(value);
            }}
            disabled={!activeConversationId || (asking && askingConversationId !== activeConversationId)}
            loading={asking && askingConversationId === activeConversationId}
            hasContext={chatMessages.length > 0 || Boolean(askQuery.trim())}
            messages={chatMessages}
            recentQueries={recentQueries}
            suggestions={suggestions}
            references={references}
            referenceOptions={referenceOptions}
            referenceQuery={referenceQuery}
            referenceOpen={referenceOpen}
            onReferenceQuery={setReferenceQuery}
            onReferenceSelect={handleReferenceSelect}
            onReferenceRemove={(id) => setReferences((current) => current.filter((reference) => reference.id !== id))}
            onRetry={handleRetry}
            suggestionsRefreshing={suggestionsRefreshing}
            onRefreshSuggestions={() => void refreshSuggestions()}
            onReaggregate={(jobId) => {
              void api.reaggregateKnowledgeJob(jobId).then((job) => {
                setChatMessages((current) => current.map((message) => message.job?.job_id === job.job_id ? { ...message, job, status: "waiting_tasks" } : message));
              }).catch((error) => setStatus(error instanceof Error ? error.message : "重新聚合失败"));
            }}
          />
        </div>
      ) : (
        <div className="knowledge-workbench-shell">
          <SearchPanel
            query={query}
            onQueryChange={setQuery}
            results={results}
            selectedTags={selectedTags}
            allTags={tags}
            onToggleTag={toggleTag}
            onManageTags={setManagingVideoId}
            searching={searching}
            statsSummary={statsSummary}
          />
          <TagNetwork
            network={network}
            selectedTags={selectedTags}
            expanded={networkExpanded}
            onToggleExpanded={() => setNetworkExpanded((current) => !current)}
            onToggleTag={toggleTag}
            onSelectVideo={(videoId) => navigate(`/videos/${videoId}`)}
          />
        </div>
      )}

      <TagManager
        videoId={managingVideoId}
        title={managingVideoId ? (videoTitleMap[managingVideoId] || managingVideoId) : ""}
        onClose={() => setManagingVideoId(null)}
        onUpdated={() => void refreshBase()}
      />
    </section>
  );
}
