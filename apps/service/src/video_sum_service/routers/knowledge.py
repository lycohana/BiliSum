from __future__ import annotations

import asyncio
import json
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from video_sum_service.context import settings_manager
from video_sum_service.knowledge.conversation_service import (
    get_conversation,
    get_job,
    get_messages,
    get_suggestions,
    list_conversations,
    search_references,
    stream_conversation,
)
from video_sum_service.knowledge.job_coordinator import KnowledgeJobCoordinator
from video_sum_service.runtime_support import detect_environment
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import (
    KnowledgeAskRequest,
    KnowledgeAskResponse,
    KnowledgeAutoTagRequest,
    KnowledgeAutoTagResponse,
    KnowledgeNetworkResponse,
    KnowledgeRebuildResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatsResponse,
    KnowledgeConversationCreateRequest,
    KnowledgeConversationMessagesResponse,
    KnowledgeConversationResponse,
    KnowledgeConversationUpdateRequest,
    KnowledgeJobResponse,
    KnowledgeReferencesResponse,
    KnowledgeSuggestionsResponse,
    KnowledgeTagCreateRequest,
    TagListResponse,
    VideoTagListResponse,
)

if TYPE_CHECKING:
    from video_sum_service.knowledge.index_service import KnowledgeIndexService
    from video_sum_service.knowledge.rag_service import RagService
    from video_sum_service.knowledge.tag_service import TagService

router = APIRouter(prefix="/api/v1/knowledge")
_QUEUE_TIMEOUT = object()
_ASK_STREAM_SEMAPHORE = BoundedSemaphore(2)


def _get_queue_item(event_queue: Queue[object], timeout: float) -> object:
    try:
        return event_queue.get(timeout=timeout)
    except Empty:
        return _QUEUE_TIMEOUT


def _knowledge_settings_signature(settings) -> tuple[object, ...]:
    return (
        bool(getattr(settings, "knowledge_enabled", False)),
        str(getattr(settings, "runtime_channel", "base") or "base"),
        str(getattr(settings, "knowledge_index_auto_rebuild", "disabled") or "disabled"),
        bool(settings.llm_enabled),
        str(settings.llm_base_url or ""),
        str(settings.llm_model or ""),
        str(settings.llm_api_key or ""),
        str(getattr(settings, "knowledge_llm_mode", "same_as_main") or "same_as_main"),
        bool(getattr(settings, "knowledge_llm_enabled", False)),
        str(getattr(settings, "knowledge_llm_provider", "openai-compatible") or "openai-compatible"),
        str(getattr(settings, "knowledge_llm_base_url", "") or ""),
        str(getattr(settings, "knowledge_llm_model", "") or ""),
        str(getattr(settings, "knowledge_llm_api_key", "") or ""),
    )


def _get_services(request: Request) -> tuple[TagService, KnowledgeIndexService, RagService]:
    from video_sum_service.knowledge.index_service import KnowledgeIndexService
    from video_sum_service.knowledge.rag_service import RagService
    from video_sum_service.knowledge.tag_service import TagService

    task_store: SqliteTaskRepository = request.app.state.task_repository
    settings = settings_manager.current
    settings_signature = _knowledge_settings_signature(settings)
    tag_service = getattr(request.app.state, "knowledge_tag_service", None)
    index_service = getattr(request.app.state, "knowledge_index_service", None)
    rag_service = getattr(request.app.state, "knowledge_rag_service", None)
    cached_signature = getattr(request.app.state, "knowledge_settings_signature", None)

    if tag_service is None or index_service is None or rag_service is None or cached_signature != settings_signature:
        tag_service = TagService(task_store, settings)
        index_service = KnowledgeIndexService(task_store, settings)
        rag_service = RagService(task_store, index_service, tag_service, settings)
        request.app.state.knowledge_tag_service = tag_service
        request.app.state.knowledge_index_service = index_service
        request.app.state.knowledge_rag_service = rag_service
        request.app.state.knowledge_settings_signature = settings_signature

    return tag_service, index_service, rag_service


def _knowledge_runtime_ready() -> bool:
    environment = detect_environment(settings_manager.current.runtime_channel)
    return bool(environment.get("knowledgeDependenciesReady"))


def _knowledge_enabled() -> bool:
    return bool(getattr(settings_manager.current, "knowledge_enabled", False))


def _index_video_if_ready(index_service: KnowledgeIndexService, video_id: str) -> None:
    if _knowledge_enabled() and _knowledge_runtime_ready():
        index_service.index_video(video_id)


def _require_knowledge_enabled() -> None:
    if not _knowledge_enabled():
        raise HTTPException(
            status_code=400,
            detail="知识库当前未启用。请先在设置中的知识库板块开启知识库。",
        )


def _require_knowledge_runtime() -> None:
    _require_knowledge_enabled()
    if not _knowledge_runtime_ready():
        raise HTTPException(
            status_code=424,
            detail="知识库依赖未安装。请先到设置中的知识库或运行环境板块安装知识库依赖。",
        )


def _get_job_coordinator(request: Request) -> KnowledgeJobCoordinator:
    coordinator = getattr(request.app.state, "knowledge_job_coordinator", None)
    if coordinator is None:
        coordinator = KnowledgeJobCoordinator(request.app.state.task_repository, settings_manager.current)
        coordinator.start()
        request.app.state.knowledge_job_coordinator = coordinator
    return coordinator


@router.get("/conversations", response_model=list[KnowledgeConversationResponse])
def get_knowledge_conversations(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[KnowledgeConversationResponse]:
    return list_conversations(request.app.state.task_repository, limit)


@router.post("/conversations", response_model=KnowledgeConversationResponse, status_code=201)
def create_knowledge_conversation(body: KnowledgeConversationCreateRequest, request: Request) -> KnowledgeConversationResponse:
    repository: SqliteTaskRepository = request.app.state.task_repository
    payload = repository.create_knowledge_conversation(body.title)
    return KnowledgeConversationResponse.model_validate(payload)


@router.get("/conversations/{conversation_id}", response_model=KnowledgeConversationResponse)
def get_knowledge_conversation(conversation_id: str, request: Request) -> KnowledgeConversationResponse:
    return get_conversation(request.app.state.task_repository, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=KnowledgeConversationResponse)
def update_knowledge_conversation(conversation_id: str, body: KnowledgeConversationUpdateRequest, request: Request) -> KnowledgeConversationResponse:
    repository: SqliteTaskRepository = request.app.state.task_repository
    updated = repository.update_knowledge_conversation_title(conversation_id, body.title)
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在或标题不能为空。")
    return KnowledgeConversationResponse.model_validate(updated)


@router.delete("/conversations/{conversation_id}")
def delete_knowledge_conversation(conversation_id: str, request: Request) -> dict[str, object]:
    if not request.app.state.task_repository.delete_knowledge_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在。")
    return {"deleted": True, "conversation_id": conversation_id}


@router.get("/conversations/{conversation_id}/messages", response_model=KnowledgeConversationMessagesResponse)
def get_knowledge_messages(conversation_id: str, request: Request) -> KnowledgeConversationMessagesResponse:
    return get_messages(request.app.state.task_repository, conversation_id)


@router.get("/references", response_model=KnowledgeReferencesResponse)
def get_knowledge_references(request: Request, q: str = Query(default=""), limit: int = Query(default=12, ge=1, le=50)) -> KnowledgeReferencesResponse:
    return search_references(request.app.state.task_repository, q, limit)


@router.get("/suggestions", response_model=KnowledgeSuggestionsResponse)
def get_knowledge_suggestions(request: Request) -> KnowledgeSuggestionsResponse:
    return get_suggestions(request.app.state.task_repository, settings_manager.current)


@router.get("/jobs/{job_id}", response_model=KnowledgeJobResponse)
def get_knowledge_job(job_id: str, request: Request) -> KnowledgeJobResponse:
    return get_job(request.app.state.task_repository, job_id)


@router.post("/jobs/{job_id}/reaggregate", response_model=KnowledgeJobResponse)
def reaggregate_knowledge_job(job_id: str, request: Request) -> KnowledgeJobResponse:
    repository: SqliteTaskRepository = request.app.state.task_repository
    current = repository.get_knowledge_job(job_id)
    if current is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    repository.update_knowledge_job(
        job_id,
        status="running",
        error_message=None,
        completed_at=None,
        agent_round=1,
        agent_state={"phase": "waiting_tasks", "review": None, "rounds": []},
    )
    repository.update_knowledge_message(str(current["assistant_message_id"]), content="正在重新聚合已完成的视频摘要……", status="waiting_tasks")
    _get_job_coordinator(request).submit(job_id)
    return get_job(repository, job_id)


@router.get("/tags", response_model=TagListResponse | VideoTagListResponse)
def get_tags(request: Request, video_id: str | None = None) -> TagListResponse | VideoTagListResponse:
    tag_service, _index_service, _rag_service = _get_services(request)
    if video_id:
        return VideoTagListResponse(video_id=video_id, items=tag_service.get_tags_for_video(video_id))
    return TagListResponse(items=tag_service.get_all_tags())


@router.post("/tags", response_model=VideoTagListResponse)
def create_tag(body: KnowledgeTagCreateRequest, request: Request) -> VideoTagListResponse:
    tag_service, index_service, _rag_service = _get_services(request)
    created = tag_service.add_tag(body.video_id, body.tag)
    if not created:
        raise HTTPException(status_code=404, detail="Video not found.")
    _index_video_if_ready(index_service, body.video_id)
    return VideoTagListResponse(video_id=body.video_id, items=tag_service.get_tags_for_video(body.video_id))


@router.delete("/tags/{video_id}/{tag}", response_model=VideoTagListResponse)
def delete_tag(video_id: str, tag: str, request: Request) -> VideoTagListResponse:
    tag_service, index_service, _rag_service = _get_services(request)
    removed = tag_service.remove_tag(video_id, tag)
    if not removed:
        raise HTTPException(status_code=404, detail="Tag not found.")
    _index_video_if_ready(index_service, video_id)
    return VideoTagListResponse(video_id=video_id, items=tag_service.get_tags_for_video(video_id))


@router.post("/auto-tag", response_model=KnowledgeAutoTagResponse)
def auto_tag(body: KnowledgeAutoTagRequest, request: Request) -> KnowledgeAutoTagResponse:
    tag_service, index_service, _rag_service = _get_services(request)
    response = tag_service.batch_auto_tag(body.video_ids)
    for item in response.items:
        _index_video_if_ready(index_service, item.video_id)
    return response


@router.get("/network", response_model=KnowledgeNetworkResponse)
def get_network(
    request: Request,
    selected_tag: list[str] | None = Query(default=None),
    max_tags: int = 12,
    max_videos: int = 8,
) -> KnowledgeNetworkResponse:
    tag_service, _index_service, _rag_service = _get_services(request)
    selected_values = selected_tag if isinstance(selected_tag, list) else None
    return tag_service.get_network_data(selected_values, max_tags=max_tags, max_videos=max_videos)


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(body: KnowledgeSearchRequest, request: Request) -> KnowledgeSearchResponse:
    _require_knowledge_runtime()
    _tag_service, index_service, _rag_service = _get_services(request)
    filters = body.filters.tags if body.filters is not None else []
    results = index_service.search(body.query, limit=body.limit, tag_filter=filters)
    return KnowledgeSearchResponse(query=body.query, results=results, total=len(results))


@router.post("/ask", response_model=KnowledgeAskResponse)
def ask_knowledge(body: KnowledgeAskRequest, request: Request) -> KnowledgeAskResponse:
    _require_knowledge_runtime()
    _tag_service, _index_service, rag_service = _get_services(request)
    return rag_service.ask(
        body.query,
        context_limit=body.context_limit,
        history=getattr(body, "history", []),
        references=getattr(body, "references", []),
    )


@router.post("/conversations/{conversation_id}/ask/stream")
async def ask_conversation_stream(conversation_id: str, body: KnowledgeAskRequest, request: Request) -> StreamingResponse:
    _require_knowledge_enabled()
    _tag_service, _index_service, rag_service = _get_services(request)
    coordinator = _get_job_coordinator(request)

    async def event_generator():
        event_queue: Queue[object] = Queue()
        cancel_event = Event()

        def produce_events() -> None:
            acquired = _ASK_STREAM_SEMAPHORE.acquire(blocking=False)
            if not acquired:
                event_queue.put(HTTPException(status_code=429, detail="知识库问答任务较多，请等待当前回答结束后再试。"))
                event_queue.put(None)
                return
            try:
                for event in stream_conversation(
                    request.app.state,
                    request.app.state.task_repository,
                    rag_service,
                    conversation_id,
                    body.query,
                    body.context_limit,
                    body.references,
                    cancel_event.is_set,
                    coordinator.submit,
                ):
                    if cancel_event.is_set():
                        return
                    event_queue.put(event)
            except Exception as exc:  # surfaced to the SSE client below
                event_queue.put(exc)
            finally:
                _ASK_STREAM_SEMAPHORE.release()
                event_queue.put(None)

        worker = Thread(target=produce_events, daemon=True, name="knowledge-conversation-stream")
        worker.start()
        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    return
                item = await asyncio.to_thread(_get_queue_item, event_queue, 1.5)
                if item is _QUEUE_TIMEOUT:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                if isinstance(item, HTTPException):
                    raise item
                if isinstance(item, Exception):
                    raise item
                event_name, payload = item
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except HTTPException as exc:
            yield f"event: error\ndata: {json.dumps({'message': str(exc.detail), 'status_code': exc.status_code}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # pragma: no cover - defensive fallback
            yield f"event: error\ndata: {json.dumps({'message': f'知识库问答失败：{exc}'}, ensure_ascii=False)}\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/ask/stream")
async def ask_knowledge_stream(body: KnowledgeAskRequest, request: Request) -> StreamingResponse:
    _require_knowledge_runtime()
    _tag_service, _index_service, rag_service = _get_services(request)

    async def event_generator():
        event_queue: Queue[object] = Queue()
        cancel_event = Event()

        def produce_events() -> None:
            acquired = _ASK_STREAM_SEMAPHORE.acquire(blocking=False)
            if not acquired:
                event_queue.put(
                    HTTPException(
                        status_code=429,
                        detail="知识库问答任务较多，请等待当前回答结束后再试。",
                    )
                )
                event_queue.put(None)
                return
            try:
                for event in rag_service.ask_stream(
                    body.query,
                    context_limit=body.context_limit,
                    history=getattr(body, "history", []),
                    should_cancel=cancel_event.is_set,
                ):
                    if cancel_event.is_set():
                        return
                    event_queue.put(event)
            except Exception as exc:  # pragma: no cover - surfaced to the SSE client below
                event_queue.put(exc)
            finally:
                _ASK_STREAM_SEMAPHORE.release()
                event_queue.put(None)

        worker = Thread(target=produce_events, daemon=True, name="knowledge-ask-stream")
        worker.start()

        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    return
                item = await asyncio.to_thread(_get_queue_item, event_queue, 1.5)
                if item is _QUEUE_TIMEOUT:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                if isinstance(item, HTTPException):
                    raise item
                if isinstance(item, Exception):
                    raise item
                event_name, payload = item
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except HTTPException as exc:
            yield (
                "event: error\n"
                f"data: {json.dumps({'message': str(exc.detail), 'status_code': exc.status_code}, ensure_ascii=False)}\n\n"
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            yield f"event: error\ndata: {json.dumps({'message': f'知识库问答失败：{exc}'}, ensure_ascii=False)}\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/stats", response_model=KnowledgeStatsResponse)
def get_knowledge_stats(request: Request) -> KnowledgeStatsResponse:
    from video_sum_service.knowledge.local_llm import knowledge_llm_available

    tag_service, index_service, _rag_service = _get_services(request)
    task_store: SqliteTaskRepository = request.app.state.task_repository
    settings = settings_manager.current
    return KnowledgeStatsResponse(
        video_count=len(task_store.list_video_assets()),
        indexed_chunk_count=task_store.get_knowledge_chunk_count(),
        tag_count=len(tag_service.get_all_tags()),
        untagged_video_count=len(task_store.list_untagged_video_ids()),
        knowledge_llm_available=bool(settings.knowledge_enabled and knowledge_llm_available(settings)),
    )


@router.post("/rebuild-index", response_model=KnowledgeRebuildResponse)
def rebuild_index(request: Request) -> KnowledgeRebuildResponse:
    _require_knowledge_runtime()
    _tag_service, index_service, _rag_service = _get_services(request)
    return KnowledgeRebuildResponse(indexed_videos=index_service.rebuild_index(force=True))
