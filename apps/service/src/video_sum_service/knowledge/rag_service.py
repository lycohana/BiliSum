from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from fastapi import HTTPException

from video_sum_infra.config import ServiceSettings
from video_sum_service.knowledge.index_service import KnowledgeIndexService, format_anchor_seconds
from video_sum_service.knowledge.local_llm import chat_knowledge_llm, stream_knowledge_llm
from video_sum_service.knowledge.tag_service import TagService
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import KnowledgeAskResponse, KnowledgeChatHistoryItem, KnowledgeSourceRef


KNOWLEDGE_QA_SYSTEM_PROMPT = (
    "你是 VidMind 的本地知识库助手，任务是把用户的视频知识库整理成可信、有人味的学习洞察。"
    "请严格基于给出的知识库片段回答，不要编造片段之外的具体事实、时间线或个人经历。"
    "但只要片段能支持合理归纳，就要主动多回答一点：概括主题、解释为什么、"
    "补充相关分支，并给出可行动的学习建议。"
    "当用户询问“我最近在学什么”“我主要关注什么”等学习画像类问题时，"
    "把“知识库中的视频内容”视为可用证据，直接归纳学习主题；"
    "不要说“暂无您的个人学习记录”“建议提供更多上下文/学习记录”这类没有帮助的话。"
    "如果证据有限，请用温和的限定语，例如“从目前命中的视频看”“更像是”“可以初步判断”，"
    "然后仍然给出最大化有用的答案。"
    "如果确实完全没有相关片段，只需简短说明“这次没有检索到足够相关的知识片段”，"
    "并给出一个可继续追问的方向。"
    "语气要高情商、自然、有陪伴感，同时保持学术表达的清晰和克制；回答使用中文。"
)

EMPTY_KNOWLEDGE_ANSWER = "这次没有检索到足够相关的知识片段。可以换一个关键词，或先到工作台用标签缩小范围。"


@dataclass(frozen=True)
class KnowledgeAgentPlan:
    query: str
    search_query: str
    context_limit: int
    history: list[KnowledgeChatHistoryItem]
    steps: list[str]


@dataclass
class KnowledgeAgentRun:
    plan: KnowledgeAgentPlan
    chunks: list[dict[str, object]] = field(default_factory=list)
    context_blocks: list[str] = field(default_factory=list)
    sources: list[KnowledgeSourceRef] = field(default_factory=list)
    answer: str = ""


def _tool_event(
    tool_id: str,
    label: str,
    status: str,
    detail: str,
    meta: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "id": tool_id,
        "label": label,
        "status": status,
        "detail": detail,
    }
    if meta:
        payload["meta"] = meta
    return "tool", payload


def _normalize_history(history: list[KnowledgeChatHistoryItem] | None, limit: int = 8) -> list[KnowledgeChatHistoryItem]:
    normalized: list[KnowledgeChatHistoryItem] = []
    for item in (history or [])[-limit:]:
        role = "assistant" if str(item.role).strip().lower() == "assistant" else "user"
        content = str(item.content or "").strip()
        if not content:
            continue
        normalized.append(KnowledgeChatHistoryItem(role=role, content=content[:1200]))
    return normalized


def _format_history_for_prompt(history: list[KnowledgeChatHistoryItem]) -> str:
    lines: list[str] = []
    for item in history:
        label = "用户" if item.role == "user" else "助手"
        lines.append(f"{label}：{item.content}")
    return "\n".join(lines)


def _build_contextual_search_query(query: str, history: list[KnowledgeChatHistoryItem]) -> str:
    if not history:
        return query
    recent_user_turns = [item.content for item in history if item.role == "user"][-3:]
    if not recent_user_turns:
        return query
    return "\n".join(["当前问题：", query, "近期追问线索：", *recent_user_turns])


def _build_knowledge_user_prompt(
    query: str,
    context_blocks: list[str],
    history: list[KnowledgeChatHistoryItem] | None = None,
) -> str:
    history_text = _format_history_for_prompt(_normalize_history(history))
    history_block = f"本轮会话上下文：\n---\n{history_text}\n---\n\n" if history_text else ""
    return (
        f"用户问题：{query}\n\n"
        + history_block
        + "相关视频内容：\n---\n"
        + "\n\n".join(context_blocks)
        + "\n---\n\n"
        + "请按下面原则组织回答：\n"
        "1. 先直接回答问题，不要先道歉或免责声明。\n"
        "2. 如果适合，按主题分组，并说明每组主题背后的依据。\n"
        "3. 可以给出“下一步学习建议”或“知识结构判断”，但必须和片段内容相关。\n"
        "4. 可以利用会话上下文理解代词、追问和用户偏好，但知识事实仍以视频片段为准。\n"
        "5. 不要输出“暂无个人学习记录”“请提供更多上下文或学习记录”这类空泛句子。"
    )


class KnowledgeAgent:
    def __init__(
        self,
        repository: SqliteTaskRepository,
        index_service: KnowledgeIndexService,
        settings: ServiceSettings,
    ) -> None:
        self._repository = repository
        self._index_service = index_service
        self._settings = settings
        self._tool_handlers = {
            "conversation_context": self._prepare_context,
            "semantic_search": self._run_semantic_search,
            "context_builder": self._build_answer_context,
        }

    def make_plan(
        self,
        query: str,
        context_limit: int = 5,
        history: list[KnowledgeChatHistoryItem] | None = None,
    ) -> KnowledgeAgentPlan:
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            raise HTTPException(status_code=400, detail="问题不能为空。")

        normalized_history = _normalize_history(history)
        steps = ["conversation_context", "semantic_search", "context_builder", "knowledge_llm"]
        return KnowledgeAgentPlan(
            query=cleaned_query,
            search_query=_build_contextual_search_query(cleaned_query, normalized_history),
            context_limit=max(1, context_limit),
            history=normalized_history,
            steps=steps,
        )

    def execute(
        self,
        query: str,
        context_limit: int = 5,
        history: list[KnowledgeChatHistoryItem] | None = None,
    ) -> KnowledgeAgentRun:
        run = KnowledgeAgentRun(plan=self.make_plan(query, context_limit, history))
        for step in run.plan.steps:
            if step == "knowledge_llm":
                run.answer = self._answer(run)
                break
            self._tool_handlers[step](run)
        return run

    def stream(
        self,
        query: str,
        context_limit: int = 5,
        history: list[KnowledgeChatHistoryItem] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[str, dict[str, object]]]:
        run = KnowledgeAgentRun(plan=self.make_plan(query, context_limit, history))
        should_stop = should_cancel or (lambda: False)
        yield _tool_event(
            "agent_plan",
            "Agent 计划",
            "completed",
            "已整理本轮问答链路：理解上下文、检索知识库、拼装证据、生成回答。",
            {"step_count": len(run.plan.steps)},
        )

        for step in run.plan.steps:
            if should_stop():
                return
            if step == "knowledge_llm":
                yield from self._stream_answer(run, should_cancel=should_stop)
                return
            yield from self._stream_tool(run, step)
            if step == "semantic_search" and not run.chunks:
                yield ("text_delta", {"delta": EMPTY_KNOWLEDGE_ANSWER})
                yield ("sources", {"sources": []})
                yield ("done", {"query": run.plan.query, "answer": EMPTY_KNOWLEDGE_ANSWER, "sources": []})
                return

    def _stream_tool(self, run: KnowledgeAgentRun, tool_name: str) -> Iterator[tuple[str, dict[str, object]]]:
        if tool_name == "conversation_context" and not run.plan.history:
            return
        yield self._tool_started(run, tool_name)
        self._tool_handlers[tool_name](run)
        yield self._tool_completed(run, tool_name)

    def _prepare_context(self, run: KnowledgeAgentRun) -> None:
        return None

    def _run_semantic_search(self, run: KnowledgeAgentRun) -> None:
        run.chunks = self._index_service.search_chunks(run.plan.search_query, limit=run.plan.context_limit)

    def _build_answer_context(self, run: KnowledgeAgentRun) -> None:
        if not run.chunks:
            return

        context_blocks: list[str] = []
        sources: list[KnowledgeSourceRef] = []
        seen_sources: set[tuple[str, str | None]] = set()
        for item in run.chunks:
            video_id = str(item["video_id"])
            metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
            asset = self._repository.get_video_asset(video_id)
            video_title = asset.title if asset is not None else str(metadata.get("title") or "未知视频")
            page_title = str(metadata.get("page_title") or metadata.get("display_title") or "").strip()
            if page_title == video_title:
                page_title = ""
            title = page_title or video_title
            page_number_raw = metadata.get("page_number")
            page_number = int(page_number_raw) if isinstance(page_number_raw, (int, float)) and int(page_number_raw) > 0 else None
            anchor_seconds = (
                float(metadata["anchor_seconds"])
                if metadata.get("anchor_seconds") not in {None, "", -1, -1.0}
                else None
            )
            timestamp = format_anchor_seconds(anchor_seconds)
            context_blocks.append(
                "\n".join(
                    line
                    for line in [
                        f"[视频：{title}]",
                        f"[总标题：{video_title}]" if page_title else "",
                        f"[时间：{timestamp or '未标注'}]",
                        str(item["document"]).strip(),
                    ]
                    if line
                )
            )
            key = (video_id, timestamp)
            if key not in seen_sources:
                seen_sources.add(key)
                sources.append(
                    KnowledgeSourceRef(
                        video_id=video_id,
                        title=title,
                        relevance_score=float(item["relevance_score"]),
                        timestamp=timestamp,
                        video_title=video_title,
                        page_title=page_title or None,
                        page_number=page_number,
                    )
                )

        run.context_blocks = context_blocks
        run.sources = sources

    def _answer(self, run: KnowledgeAgentRun) -> str:
        if not run.chunks:
            return EMPTY_KNOWLEDGE_ANSWER
        answer, _body = chat_knowledge_llm(
            self._settings,
            system_prompt=KNOWLEDGE_QA_SYSTEM_PROMPT,
            user_prompt=_build_knowledge_user_prompt(run.plan.query, run.context_blocks, run.plan.history),
            max_tokens=1100,
            temperature=0.28,
        )
        return answer.strip()

    def _stream_answer(
        self,
        run: KnowledgeAgentRun,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[str, dict[str, object]]]:
        should_stop = should_cancel or (lambda: False)
        if not run.chunks:
            yield ("text_delta", {"delta": EMPTY_KNOWLEDGE_ANSWER})
            yield ("sources", {"sources": []})
            yield ("done", {"query": run.plan.query, "answer": EMPTY_KNOWLEDGE_ANSWER, "sources": []})
            return

        yield _tool_event("knowledge_llm", "知识库 LLM", "running", "正在根据证据片段与会话上下文生成回答。")
        answer_parts: list[str] = []
        reasoning_character_count = 0
        last_reasoning_notice_at = 0.0
        try:
            for event in stream_knowledge_llm(
                self._settings,
                system_prompt=KNOWLEDGE_QA_SYSTEM_PROMPT,
                user_prompt=_build_knowledge_user_prompt(run.plan.query, run.context_blocks, run.plan.history),
                max_tokens=1100,
                temperature=0.28,
                should_cancel=should_stop,
            ):
                if should_stop():
                    return
                if isinstance(event, str):
                    delta = event
                    kind = "content"
                else:
                    delta = event.delta
                    kind = event.kind
                if kind == "reasoning":
                    reasoning_character_count += len(delta)
                    yield ("reasoning_delta", {"delta": delta})
                    now = time.monotonic()
                    if reasoning_character_count and (last_reasoning_notice_at == 0.0 or now - last_reasoning_notice_at > 2.0):
                        last_reasoning_notice_at = now
                        yield _tool_event(
                            "knowledge_llm",
                            "知识库 LLM",
                            "running",
                            "已收到模型推理流，正在等待最终回答正文。",
                            {"reasoning_character_count": reasoning_character_count},
                        )
                    continue
                answer_parts.append(delta)
                yield ("text_delta", {"delta": delta})
        except HTTPException as exc:
            if exc.status_code in {502, 503, 504}:
                message = str(exc.detail)
                yield _tool_event(
                    "knowledge_llm",
                    "知识库 LLM",
                    "error",
                    message,
                    {"status_code": exc.status_code},
                )
                yield ("error", {"message": message, "status_code": exc.status_code})
                return
            yield _tool_event(
                "knowledge_llm",
                "知识库 LLM",
                "running",
                "流式输出没有顺利返回，正在切换为普通问答调用。",
                {"fallback": "non_streaming", "status_code": exc.status_code},
            )
            try:
                run.answer = self._answer(run)
            except HTTPException as fallback_exc:
                message = str(fallback_exc.detail)
                yield _tool_event(
                    "knowledge_llm",
                    "知识库 LLM",
                    "error",
                    message,
                    {"status_code": fallback_exc.status_code},
                )
                yield ("error", {"message": message, "status_code": fallback_exc.status_code})
                return
            yield ("text_delta", {"delta": run.answer})

        if not answer_parts and not run.answer:
            yield _tool_event(
                "knowledge_llm",
                "知识库 LLM",
                "running",
                "流式接口已结束但没有正文，正在切换为普通问答调用。",
                {"fallback": "non_streaming"},
            )
            try:
                run.answer = self._answer(run)
            except HTTPException as exc:
                message = str(exc.detail)
                yield _tool_event(
                    "knowledge_llm",
                    "知识库 LLM",
                    "error",
                    message,
                    {"status_code": exc.status_code},
                )
                yield ("error", {"message": message, "status_code": exc.status_code})
                return
            yield ("text_delta", {"delta": run.answer})

        if not run.answer:
            run.answer = "".join(answer_parts).strip() or EMPTY_KNOWLEDGE_ANSWER
        yield _tool_event(
            "knowledge_llm",
            "知识库 LLM",
            "completed",
            "回答生成完成。",
            {"character_count": len(run.answer)},
        )
        yield ("sources", {"sources": [source.model_dump(mode="json") for source in run.sources]})
        yield (
            "done",
            {
                "query": run.plan.query,
                "answer": run.answer,
                "sources": [source.model_dump(mode="json") for source in run.sources],
            },
        )

    def _tool_started(self, run: KnowledgeAgentRun, tool_name: str) -> tuple[str, dict[str, object]]:
        details = {
            "conversation_context": f"正在整理最近 {len(run.plan.history)} 条会话上下文。",
            "semantic_search": "正在检索与你问题最相关的知识片段。",
            "context_builder": "正在整理片段、时间点和来源引用。",
        }
        labels = {
            "conversation_context": "上下文整理",
            "semantic_search": "语义检索",
            "context_builder": "上下文拼装",
        }
        return _tool_event(tool_name, labels[tool_name], "running", details[tool_name])

    def _tool_completed(self, run: KnowledgeAgentRun, tool_name: str) -> tuple[str, dict[str, object]]:
        if tool_name == "conversation_context":
            return _tool_event(
                tool_name,
                "上下文整理",
                "completed",
                f"已带入最近 {len(run.plan.history)} 条会话上下文，用于理解追问与指代。",
                {"turn_count": len(run.plan.history)},
            )
        if tool_name == "semantic_search":
            matched_videos = len({str(item["video_id"]) for item in run.chunks})
            detail = (
                f"命中 {len(run.chunks)} 个片段，来自 {matched_videos} 个视频。"
                if run.chunks
                else "没有检索到相关知识片段。"
            )
            return _tool_event(
                tool_name,
                "语义检索",
                "completed",
                detail,
                {"chunk_count": len(run.chunks), "video_count": matched_videos},
            )

        return _tool_event(
            tool_name,
            "上下文拼装",
            "completed",
            "已生成回答上下文，并保留来源视频与时间点。",
            {
                "sources": [
                    {
                        "title": source.title,
                        "timestamp": source.timestamp,
                        "score": round(source.relevance_score, 3),
                    }
                    for source in run.sources[:4]
                ],
            },
        )


class RagService:
    def __init__(
        self,
        repository: SqliteTaskRepository,
        index_service: KnowledgeIndexService,
        tag_service: TagService,
        settings: ServiceSettings,
    ) -> None:
        self._tag_service = tag_service
        self._agent = KnowledgeAgent(repository, index_service, settings)

    def ask(
        self,
        query: str,
        context_limit: int = 5,
        history: list[KnowledgeChatHistoryItem] | None = None,
    ) -> KnowledgeAskResponse:
        run = self._agent.execute(query, context_limit=context_limit, history=history)
        return KnowledgeAskResponse(query=run.plan.query, answer=run.answer, sources=run.sources)

    def ask_stream(
        self,
        query: str,
        context_limit: int = 5,
        history: list[KnowledgeChatHistoryItem] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[str, dict[str, object]]]:
        yield from self._agent.stream(
            query,
            context_limit=context_limit,
            history=history,
            should_cancel=should_cancel,
        )
