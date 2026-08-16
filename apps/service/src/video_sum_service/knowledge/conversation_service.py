from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator

from fastapi import HTTPException

from video_sum_core.models.tasks import InputType, TaskInput, TaskStatus
from video_sum_core.utils import normalize_video_url
from video_sum_infra.config import ServiceSettings
from video_sum_service.knowledge.local_llm import chat_knowledge_llm, knowledge_llm_available, parse_json_payload
from video_sum_service.knowledge.rag_service import RagService
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.runtime_startup import submit_task_or_queue
from video_sum_service.schemas import (
    KnowledgeConversationMessagesResponse,
    KnowledgeChatHistoryItem,
    KnowledgeConversationResponse,
    KnowledgeJobItemResponse,
    KnowledgeJobResponse,
    KnowledgeMessageResponse,
    KnowledgeReference,
    KnowledgeReferencesResponse,
    KnowledgeSuggestion,
    KnowledgeSuggestionsResponse,
)
from video_sum_service.video_assets import probe_video_asset

_VIDEO_URL_RE = re.compile(r"https?://[^\s<>\u3001\u3002\uff0c\uff1b\uff09]+", re.IGNORECASE)
_MAX_TASK_LINKS = 20
_MAX_REASONING = 4000
logger = logging.getLogger("video_sum_service.knowledge.conversation")


def _model_or_404(repository: SqliteTaskRepository, conversation_id: str) -> KnowledgeConversationResponse:
    payload = repository.get_knowledge_conversation(conversation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return KnowledgeConversationResponse.model_validate(payload)


def list_conversations(repository: SqliteTaskRepository, limit: int = 50) -> list[KnowledgeConversationResponse]:
    return [KnowledgeConversationResponse.model_validate(item) for item in repository.list_knowledge_conversations(limit)]


def get_conversation(repository: SqliteTaskRepository, conversation_id: str) -> KnowledgeConversationResponse:
    return _model_or_404(repository, conversation_id)


def get_messages(repository: SqliteTaskRepository, conversation_id: str) -> KnowledgeConversationMessagesResponse:
    _model_or_404(repository, conversation_id)
    return KnowledgeConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=[KnowledgeMessageResponse.model_validate(item) for item in repository.list_knowledge_messages(conversation_id)],
    )


def _job_item(item: dict[str, object]) -> KnowledgeJobItemResponse:
    raw_status = str(item.get("status") or TaskStatus.FAILED.value)
    try:
        status = TaskStatus(raw_status)
    except ValueError:
        status = TaskStatus.FAILED
    title = ""
    try:
        task_input = json.loads(str(item.get("task_input_json") or "{}"))
        title = str(task_input.get("title") or task_input.get("source") or "")
    except (TypeError, ValueError, AttributeError):
        title = str(item.get("task_id") or "")
    error_message = item.get("error_message") or item.get("task_error_message")
    progress = max(0, min(100, int(item.get("progress") or 0)))
    if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        progress = 100
    return KnowledgeJobItemResponse(
        task_id=str(item.get("task_id") or ""),
        video_id=str(item["video_id"]) if item.get("video_id") else None,
        title=title,
        status=status,
        progress=progress,
        message=str(item.get("task_message") or ""),
        error_message=str(error_message) if error_message else None,
    )


def get_job(repository: SqliteTaskRepository, job_id: str) -> KnowledgeJobResponse:
    payload = repository.get_knowledge_job(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    items = [_job_item(item) for item in payload.get("items", []) if isinstance(item, dict)]
    completed = sum(1 for item in items if item.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
    progress = round(completed / len(items) * 100) if items else 0
    try:
        agent_state = json.loads(str(payload.get("agent_state_json") or "{}"))
    except (TypeError, ValueError):
        agent_state = {}
    return KnowledgeJobResponse(
        job_id=str(payload["job_id"]),
        conversation_id=str(payload["conversation_id"]),
        assistant_message_id=str(payload["assistant_message_id"]),
        kind=str(payload["kind"]),
        status=str(payload["status"]),
        query=str(payload["query"]),
        progress=progress,
        error_message=str(payload["error_message"]) if payload.get("error_message") else None,
        items=items,
        agent_round=max(1, int(payload.get("agent_round") or 1)),
        agent_phase=str(agent_state.get("phase") or "waiting_tasks"),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        completed_at=payload["completed_at"],
    )


def search_references(repository: SqliteTaskRepository, query: str = "", limit: int = 12) -> KnowledgeReferencesResponse:
    cleaned = str(query or "").strip().lower()
    items = []
    for video in repository.list_video_assets():
        if cleaned and cleaned not in video.title.lower():
            continue
        items.append({
            "kind": "video",
            "id": video.video_id,
            "label": video.title,
            "subtitle": video.platform,
            "cover_url": video.cover_url,
            "updated_at": video.last_summary_at or video.updated_at,
        })
    for folder in repository.list_video_folders():
        if cleaned and cleaned not in folder.name.lower():
            continue
        items.append({
            "kind": "folder",
            "id": folder.folder_id,
            "label": folder.name,
            "subtitle": f"{len(repository.list_video_ids_in_folder(folder.folder_id))} 个视频",
            "updated_at": folder.updated_at,
        })
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    from video_sum_service.schemas import KnowledgeReferenceItem

    return KnowledgeReferencesResponse(items=[KnowledgeReferenceItem.model_validate(item) for item in items[: max(1, min(limit, 50))]])


def _extract_urls(query: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for raw in _VIDEO_URL_RE.findall(query):
        url = raw.rstrip(".,;!?，。；！？）")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:_MAX_TASK_LINKS]


def _is_task_request(query: str, references: list[KnowledgeReference]) -> bool:
    markers = ("总结", "汇总", "整理成文档", "整理为文档", "做成文档", "生成文档")
    return bool(_extract_urls(query) or references) and any(marker in query for marker in markers)


def _history(repository: SqliteTaskRepository, conversation_id: str) -> list[dict[str, str]]:
    history = []
    for item in repository.list_knowledge_messages(conversation_id)[-8:]:
        if item["role"] in {"user", "assistant"} and item["content"] and item["status"] not in {"streaming", "interrupted"}:
            history.append({"role": str(item["role"]), "content": str(item["content"])[:1200]})
    return history


def _create_video_tasks(
    app_state: object,
    repository: SqliteTaskRepository,
    query: str,
    references: list[KnowledgeReference],
) -> tuple[list[tuple[str, str | None, int]], list[str]]:
    urls = _extract_urls(query)
    for reference in references:
        if reference.kind == "video":
            video = repository.get_video_asset(reference.id)
            if video is not None and video.source_url not in urls:
                urls.append(video.source_url)
        elif reference.kind == "folder":
            for video_id in repository.list_video_ids_in_folder(reference.id):
                video = repository.get_video_asset(video_id)
                if video is not None and video.source_url not in urls:
                    urls.append(video.source_url)
    task_items: list[tuple[str, str | None, int]] = []
    errors: list[str] = []
    for position, url in enumerate(urls[:_MAX_TASK_LINKS]):
        try:
            probed, _pages, _requires_selection = probe_video_asset(url)
            asset = repository.upsert_video_asset(probed)
            normalized = normalize_video_url(url)
            record = repository.create_task(
                TaskInput(input_type=InputType.URL, source=normalized.normalized_url, title=asset.title, platform_hint=asset.platform),
                video_id=asset.video_id,
                page_number=normalized.page_number if normalized.platform == "bilibili" else None,
                page_title=asset.title,
            )
            submit_task_or_queue(app_state, repository, record)
            task_items.append((record.task_id, asset.video_id, position))
        except Exception as exc:  # keep one bad URL from blocking the other items
            errors.append(f"{url}：{exc}")
    return task_items, errors


def _suggestion_facts(repository: SqliteTaskRepository) -> tuple[str, list[str]]:
    videos = repository.list_video_assets()
    recent = sorted(
        [video for video in videos if video.last_summary_at is not None],
        key=lambda video: video.last_summary_at or video.updated_at,
        reverse=True,
    )[:6]
    folders = repository.list_video_folders()[:8]
    tags = [str(tag.get("tag") or "").strip() for tag in repository.list_all_tags()[:12]]
    tags = [tag for tag in tags if tag]
    facts = "\n".join(
        [
            "最近总结视频：" + "、".join(video.title for video in recent),
            "收藏视频：" + "、".join(video.title for video in videos if video.is_favorite)[:600],
            "收藏夹：" + "、".join(f"{folder.name}({len(repository.list_video_ids_in_folder(folder.folder_id))})" for folder in folders),
            "标签：" + "、".join(tags),
        ]
    )
    based_on = [video.title for video in recent[:3]] + [folder.name for folder in folders[:2]]
    return facts, based_on


def get_suggestions(repository: SqliteTaskRepository, settings: ServiceSettings) -> KnowledgeSuggestionsResponse:
    try:
        facts, based_on = _suggestion_facts(repository)
    except Exception:
        logger.exception("knowledge suggestion facts collection failed; using local fallback")
        facts, based_on = "当前没有可读取的个性化推荐上下文。", []
    fallback_titles = based_on[:3]
    fallback = [
        KnowledgeSuggestion(text="我最近在学什么主题？", reason="根据最近完成的总结"),
        KnowledgeSuggestion(text="把最近的总结串成一份复习提纲。", reason="根据最近总结视频"),
        KnowledgeSuggestion(text=f"总结一下收藏夹里的重点。", reason="根据收藏夹内容"),
        KnowledgeSuggestion(text=(f"比较「{fallback_titles[0]}」和最近内容的共同主题。" if fallback_titles else "我的知识库里有哪些值得继续学习的主题？"), reason="根据已有知识库内容"),
    ]
    if not knowledge_llm_available(settings):
        return KnowledgeSuggestionsResponse(suggestions=fallback[:4], based_on=based_on)
    try:
        text, _body = chat_knowledge_llm(
            settings,
            system_prompt="你是知识库首页推荐问题生成器。只返回 JSON：{\"suggestions\":[{\"text\":\"...\",\"reason\":\"...\"}]}。生成 3 到 6 个自然、具体、可回答的问题，每条不超过 60 个汉字。",
            user_prompt=facts,
            max_tokens=350,
            temperature=0.35,
            require_json=True,
        )
        payload = parse_json_payload(text)
        raw = payload.get("suggestions")
        suggestions = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("text") or "").strip()[:80]
                if label:
                    suggestions.append(KnowledgeSuggestion(text=label, reason=str(item.get("reason") or "")[:80]))
        if 3 <= len(suggestions) <= 6:
            return KnowledgeSuggestionsResponse(suggestions=suggestions, based_on=based_on)
    except Exception as exc:
        logger.warning("knowledge suggestion generation failed; using local fallback error=%s", exc)
    return KnowledgeSuggestionsResponse(suggestions=fallback[:4], based_on=based_on)


def stream_conversation(
    app_state: object,
    repository: SqliteTaskRepository,
    rag_service: RagService,
    conversation_id: str,
    query: str,
    context_limit: int,
    references: list[KnowledgeReference],
    should_cancel: Callable[[], bool],
    coordinator_submit: Callable[[str], None],
) -> Iterator[tuple[str, dict[str, object]]]:
    conversation = _model_or_404(repository, conversation_id)
    clean_query = str(query or "").strip()
    if not clean_query:
        raise HTTPException(status_code=400, detail="问题不能为空。")
    if conversation.title == "新会话":
        repository.update_knowledge_conversation_title(conversation_id, clean_query[:36])
    prior_history = _history(repository, conversation_id)
    user = repository.create_knowledge_message(
        conversation_id,
        "user",
        clean_query,
        status="completed",
        references=[reference.model_dump(mode="json") for reference in references],
    )
    assistant = repository.create_knowledge_message(
        conversation_id,
        "assistant",
        "",
        status="streaming",
        references=[reference.model_dump(mode="json") for reference in references],
    )
    if user is None or assistant is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    assistant_id = str(assistant["message_id"])
    yield ("message", {"user_message_id": user["message_id"], "assistant_message_id": assistant_id})
    if _is_task_request(clean_query, references):
        yield ("tool", {"id": "task_router", "label": "任务路由", "status": "running", "detail": "已识别为多视频总结任务，正在创建子任务。"})
        items, errors = _create_video_tasks(app_state, repository, clean_query, references)
        if not items:
            detail = "；".join(errors) or "没有识别到可执行的视频链接。"
            repository.update_knowledge_message(assistant_id, content=f"视频任务创建失败：{detail}", status="error", tools=[{"id": "task_router", "label": "任务路由", "status": "error", "detail": detail}])
            yield ("error", {"message": detail, "status_code": 400})
            return
        job = repository.create_knowledge_job(
            conversation_id,
            assistant_id,
            "multi_video_summary",
            "queued",
            clean_query,
            agent_round=1,
            agent_state={
                "phase": "waiting_tasks",
                "review": None,
                "rounds": [{"round": 1, "name": "task_planning", "status": "completed"}],
            },
        )
        if job is None:
            raise HTTPException(status_code=500, detail="无法创建知识库任务。")
        repository.add_knowledge_job_items(str(job["job_id"]), items)
        repository.update_knowledge_message(
            assistant_id,
            content="任务已创建。所有视频完成后，我会继续生成聚合 Markdown 文档。",
            status="waiting_tasks",
            job_id=str(job["job_id"]),
            tools=[{"id": "task_router", "label": "任务路由", "status": "completed", "detail": f"已创建 {len(items)} 个视频总结子任务。"}],
        )
        coordinator_submit(str(job["job_id"]))
        if errors:
            repository.update_knowledge_job(str(job["job_id"]), status="running", error_message="；".join(errors))
        yield ("tool", {"id": "task_router", "label": "任务路由", "status": "completed", "detail": f"已创建 {len(items)} 个视频总结子任务。"})
        yield ("job", {"job": get_job(repository, str(job["job_id"])).model_dump(mode="json")})
        yield ("done", {"query": clean_query, "answer": "任务已创建。所有视频完成后，我会继续生成聚合 Markdown 文档。", "sources": [], "job_id": job["job_id"]})
        return

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tools: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    error_message = ""
    finished = False
    try:
        for event_name, payload in rag_service.ask_stream(
            clean_query,
            context_limit=context_limit,
            history=[KnowledgeChatHistoryItem.model_validate(item) for item in prior_history],
            references=references,
            should_cancel=should_cancel,
        ):
            if event_name == "text_delta":
                content_parts.append(str(payload.get("delta") or ""))
            elif event_name == "reasoning_delta":
                if sum(len(item) for item in reasoning_parts) < _MAX_REASONING:
                    remaining = max(0, _MAX_REASONING - sum(len(item) for item in reasoning_parts))
                    reasoning_parts.append(str(payload.get("delta") or "")[:remaining])
            elif event_name == "tool":
                tool = dict(payload)
                tool_id = str(tool.get("id") or "")
                tools[:] = [item for item in tools if str(item.get("id")) != tool_id]
                tools.append(tool)
            elif event_name == "sources":
                sources = list(payload.get("sources") or [])
            elif event_name == "done":
                finished = True
            elif event_name == "error":
                error_message = str(payload.get("message") or "流式回答失败")
            yield (event_name, payload)
        if finished:
            content = "".join(content_parts).strip()
            repository.update_knowledge_message(
                assistant_id,
                content=content or "这次没有生成可读取的回答正文。",
                status="completed",
                reasoning="".join(reasoning_parts),
                sources=sources,
                tools=tools,
            )
        elif should_cancel():
            repository.update_knowledge_message(assistant_id, content="本轮回答已中断，可点击重新回答。", status="interrupted", reasoning="".join(reasoning_parts), sources=sources, tools=tools)
        elif error_message:
            repository.update_knowledge_message(assistant_id, content=f"知识库助手暂时没有完成回答。\n\n{error_message}", status="error", reasoning="".join(reasoning_parts), sources=sources, tools=tools)
        else:
            repository.update_knowledge_message(assistant_id, content="流式回答提前结束，可点击重新回答。", status="error", reasoning="".join(reasoning_parts), sources=sources, tools=tools)
    except Exception as exc:
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        repository.update_knowledge_message(assistant_id, content=f"知识库助手暂时没有完成回答。\n\n{detail}", status="error", reasoning="".join(reasoning_parts), sources=sources, tools=tools)
        yield ("error", {"message": detail, "status_code": getattr(exc, "status_code", 500)})
    finally:
        if should_cancel() and not finished:
            current = repository.get_knowledge_message(assistant_id)
            if current is not None and current.get("status") == "streaming":
                repository.update_knowledge_message(
                    assistant_id,
                    content="本轮回答已中断，可点击重新回答。",
                    status="interrupted",
                    reasoning="".join(reasoning_parts),
                    sources=sources,
                    tools=tools,
                )
