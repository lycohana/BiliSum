import asyncio
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from video_sum_core.models.tasks import InputType, TaskStatus
from video_sum_core.utils import normalize_video_url
from video_sum_infra.config import normalize_visual_note_mode

from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.runtime_startup import submit_mindmap_or_queue, submit_task_or_queue
from video_sum_service.schemas import (
    TaskCreateRequest,
    TaskDetailResponse,
    TaskEventResponse,
    TaskMarkdownExportRequest,
    TaskMarkdownExportResponse,
    TaskMindMapResponse,
    TaskProgressResponse,
    TaskSummaryResponse,
    TaskTranscriptExportRequest,
    TaskVisualEvidenceResponse,
)
from video_sum_service.context import settings_manager
from video_sum_service.task_artifacts import (
    cleanup_task_files,
    load_task_mindmap,
    load_visual_context,
    load_visual_note_markdown,
    resolve_visual_media_path,
)
from video_sum_service.task_exports import (
    export_task_markdown as export_task_markdown_artifact,
    export_task_transcript as export_task_transcript_artifact,
)
from video_sum_service.video_assets import probe_video_asset

router = APIRouter(prefix="/api/v1/tasks")


@router.post("/{task_id}/resume", response_model=TaskDetailResponse)
def resume_task(task_id: str, request: Request) -> TaskDetailResponse:
    store = request.app.state.task_repository
    record = store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if record.status not in {TaskStatus.PAUSED, TaskStatus.FAILED}:
        raise HTTPException(status_code=409, detail="仅暂停或失败任务可以续跑。")
    if record.result is None or not record.result.transcript_text.strip():
        raise HTTPException(status_code=400, detail="当前任务没有已保存的转写。")
    update_event = getattr(request.app.state, "settings_update_event", None)
    if update_event is not None and not update_event.wait(timeout=30):
        raise HTTPException(status_code=503, detail="设置仍在生效，请稍后再次点击仅重跑 LLM。转写已保留。")
    store.update_error(task_id, None, None)
    submit_task_or_queue(request.app.state, store, record)
    return store.get_task(task_id).to_detail()


@router.post("", response_model=TaskDetailResponse, status_code=status.HTTP_201_CREATED)
def create_task(body: TaskCreateRequest, request: Request) -> TaskDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository

    video_id = body.video_id
    if video_id is None and body.input_type is InputType.URL:
        probed, _, _ = probe_video_asset(body.source)
        asset = task_store.upsert_video_asset(probed)
        video_id = asset.video_id

    normalized = normalize_video_url(body.source) if body.input_type is InputType.URL else None
    page_number = normalized.page_number if normalized and normalized.platform == "bilibili" else None

    record = task_store.create_task(
        body,
        video_id=video_id,
        page_number=page_number,
        page_title=body.title,
    )
    submit_task_or_queue(request.app.state, task_store, record)
    refreshed = task_store.get_task(record.task_id)
    assert refreshed is not None
    return refreshed.to_detail()


@router.get("", response_model=list[TaskSummaryResponse])
def list_tasks(request: Request) -> list[TaskSummaryResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return [record.to_summary() for record in task_store.list_tasks()]


@router.get("/{task_id}", response_model=TaskDetailResponse)
def get_task(task_id: str, request: Request) -> TaskDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return record.to_detail()


@router.delete("/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, object]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    # 先清理文件，再删除数据库记录：如果文件清理失败，数据库记录仍保留以便重试
    cleanup_task_files(record, settings_manager.current)
    deleted = task_store.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"deleted": True, "task_id": task_id}


@router.get("/{task_id}/result", response_model=TaskDetailResponse)
def get_task_result(task_id: str, request: Request) -> TaskDetailResponse:
    return get_task(task_id, request)


@router.get("/{task_id}/mindmap", response_model=TaskMindMapResponse)
def get_task_mindmap(task_id: str, request: Request) -> TaskMindMapResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    result = record.result
    if result is None:
        return TaskMindMapResponse(task_id=task_id, status="idle")

    mindmap_path = result.mindmap_artifact_path or result.artifacts.get("mindmap_path")
    mindmap = None
    status_value = result.mindmap_status or ("ready" if mindmap_path else "idle")
    error_message = result.mindmap_error_message
    if status_value == "generating":
        return TaskMindMapResponse(
            task_id=task_id,
            status="generating",
            error_message=error_message,
            updated_at=result.mindmap_updated_at,
            mindmap=None,
        )
    if status_value == "failed":
        return TaskMindMapResponse(
            task_id=task_id,
            status="failed",
            error_message=error_message,
            updated_at=result.mindmap_updated_at,
            mindmap=None,
        )
    if mindmap_path:
        try:
            mindmap = load_task_mindmap(mindmap_path)
        except HTTPException:
            status_value = "failed"
            error_message = "思维导图文件缺失或已损坏，请重新生成。"
    if mindmap is not None:
        status_value = "ready"

    return TaskMindMapResponse(
        task_id=task_id,
        status=status_value,
        error_message=error_message,
        updated_at=result.mindmap_updated_at,
        mindmap=mindmap,
    )


@router.post("/{task_id}/mindmap", response_model=TaskMindMapResponse)
def generate_task_mindmap(request: Request, task_id: str, force: bool = False) -> TaskMindMapResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if record.status != TaskStatus.COMPLETED or record.result is None:
        raise HTTPException(status_code=400, detail="仅已完成且有结果的任务可以生成思维导图。")
    if not record.result.knowledge_note_markdown.strip():
        raise HTTPException(status_code=400, detail="当前任务缺少知识笔记，暂时无法生成思维导图。")
    if not record.result.artifacts.get("summary_path"):
        raise HTTPException(status_code=400, detail="当前任务缺少摘要文件，暂时无法生成思维导图。")

    existing_path = record.result.mindmap_artifact_path or record.result.artifacts.get("mindmap_path")
    if record.result.mindmap_status == "generating" and not force:
        return TaskMindMapResponse(
            task_id=task_id,
            status="generating",
            error_message=None,
            updated_at=record.result.mindmap_updated_at,
            mindmap=None,
        )
    if existing_path and record.result.mindmap_status == "ready" and not force:
        try:
            return TaskMindMapResponse(
                task_id=task_id,
                status="ready",
                error_message=None,
                updated_at=record.result.mindmap_updated_at,
                mindmap=load_task_mindmap(existing_path),
            )
        except HTTPException:
            pass

    generating_result = record.result.model_copy(
        update={
            "mindmap_status": "generating",
            "mindmap_error_message": None,
        }
    )
    task_store.save_result(task_id, generating_result)
    submit_mindmap_or_queue(request.app.state, task_store, task_id, force=force)
    refreshed = task_store.get_task(task_id)
    refreshed_result = refreshed.result if refreshed is not None else generating_result
    return TaskMindMapResponse(
        task_id=task_id,
        status=refreshed_result.mindmap_status,
        error_message=refreshed_result.mindmap_error_message,
        updated_at=refreshed_result.mindmap_updated_at,
        mindmap=None,
    )


@router.get("/{task_id}/visual-evidence", response_model=TaskVisualEvidenceResponse)
def get_task_visual_evidence(task_id: str, request: Request) -> TaskVisualEvidenceResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    result = record.result
    if result is None:
        return TaskVisualEvidenceResponse(task_id=task_id, status="idle")

    context_path = result.artifacts.get("visual_context_path")
    context = load_visual_context(context_path) if context_path else None
    note_path = result.visual_enhanced_note_artifact_path or result.visual_note_artifact_path or result.artifacts.get("visual_enhanced_note_path") or result.artifacts.get("visual_note_path")
    return TaskVisualEvidenceResponse(
        task_id=task_id,
        mode=str(context.get("mode") if context else "text"),
        status=result.visual_note_status or (str(context.get("status")) if context else "idle"),
        error_message=result.visual_note_error_message,
        updated_at=result.visual_note_updated_at,
        frame_count=result.visual_frame_count,
        insert_count=int(context.get("insert_count") or 0) if context else 0,
        visual_note_markdown=load_visual_note_markdown(note_path),
        enhanced_note_markdown=load_visual_note_markdown(note_path),
        context=context,
    )


@router.post("/{task_id}/visual-evidence", response_model=TaskVisualEvidenceResponse)
def generate_task_visual_evidence(request: Request, task_id: str, force: bool = False, mode: str | None = None) -> TaskVisualEvidenceResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if record.status != TaskStatus.COMPLETED or record.result is None:
        raise HTTPException(status_code=400, detail="仅已完成且有结果的任务可以生成图文笔记。")

    requested_mode = normalize_visual_note_mode(mode or settings_manager.current.visual_note_mode or "frame_insert", default="frame_insert")
    if requested_mode == "text":
        requested_mode = "frame_insert"
    existing_path = record.result.visual_enhanced_note_artifact_path or record.result.visual_note_artifact_path or record.result.artifacts.get("visual_enhanced_note_path") or record.result.artifacts.get("visual_note_path")
    if record.result.visual_note_status == "generating" and not force:
        return TaskVisualEvidenceResponse(
            task_id=task_id,
            mode=requested_mode,
            status="generating",
            error_message=None,
            updated_at=record.result.visual_note_updated_at,
            frame_count=record.result.visual_frame_count,
        )
    if existing_path and record.result.visual_note_status == "ready" and not force:
        return get_task_visual_evidence(task_id, request)

    generating_result = record.result.model_copy(
        update={
            "visual_note_status": "generating",
            "visual_note_error_message": None,
        }
    )
    task_store.save_result(task_id, generating_result)
    worker = getattr(request.app.state, "task_worker", None)
    if worker is None or not hasattr(worker, "submit_visual_evidence"):
        raise HTTPException(status_code=503, detail="图文笔记后台队列暂不可用。")
    worker.submit_visual_evidence(task_id, force=force, mode=requested_mode)
    refreshed = task_store.get_task(task_id)
    refreshed_result = refreshed.result if refreshed is not None and refreshed.result is not None else generating_result
    return TaskVisualEvidenceResponse(
        task_id=task_id,
        mode=requested_mode,
        status=refreshed_result.visual_note_status,
        error_message=refreshed_result.visual_note_error_message,
        updated_at=refreshed_result.visual_note_updated_at,
        frame_count=refreshed_result.visual_frame_count,
    )


@router.get("/{task_id}/visual-evidence/media/{file_name}")
def get_task_visual_evidence_media(task_id: str, file_name: str, request: Request) -> FileResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None or record.result is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    safe_name = str(file_name or "").strip()
    if not safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="Invalid media file name.")
    target = resolve_visual_media_path(record.result, task_id, settings_manager.current.tasks_dir, safe_name)
    if target is None:
        raise HTTPException(status_code=404, detail="Visual evidence media not found.")
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type, filename=target.name)


@router.post("/{task_id}/exports/markdown", response_model=TaskMarkdownExportResponse)
def export_task_markdown(request: Request, task_id: str, body: TaskMarkdownExportRequest) -> TaskMarkdownExportResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return export_task_markdown_artifact(
        task_store,
        settings_manager.current,
        task_id,
        target=body.target,
        include_transcript=body.include_transcript,
        output_dir=body.output_dir,
    )


@router.get("/{task_id}/exports/download")
def download_task_markdown(request: Request, task_id: str, target: str = "markdown", include_transcript: bool = False):
    directory = Path(tempfile.mkdtemp(prefix="bilisum-export-"))
    try:
        exported = export_task_markdown_artifact(
            request.app.state.task_repository, settings_manager.current, task_id,
            target=target, include_transcript=include_transcript,
            output_dir=str(directory), persist_artifact=False,
        )
        markdown = Path(exported.path)
        files = [file for file in directory.rglob("*") if file.is_file()]
        if len(files) > 1:
            result = directory / f"{markdown.stem}.zip"
            with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    archive.write(file, file.relative_to(directory).as_posix())
            media_type = "application/zip"
        else:
            result = markdown
            media_type = "text/markdown; charset=utf-8"
        return FileResponse(result, filename=result.name, media_type=media_type,
                            background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@router.post("/{task_id}/exports/transcript", response_model=TaskMarkdownExportResponse)
def export_task_transcript(
    request: Request,
    task_id: str,
    body: TaskTranscriptExportRequest,
) -> TaskMarkdownExportResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return export_task_transcript_artifact(
        task_store,
        settings_manager.current,
        task_id,
        output_dir=body.output_dir,
    )


@router.get("/{task_id}/events", response_model=list[TaskEventResponse])
def get_task_events(task_id: str, request: Request) -> list[TaskEventResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return [event.to_response() for event in task_store.list_events(task_id)]


@router.get("/{task_id}/events/stream")
async def stream_task_events(request: Request, task_id: str, after: str | None = None) -> StreamingResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    async def event_generator():
        last_seen = after
        idle_ticks = 0
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED}

        while True:
            if await request.is_disconnected():
                return

            current_record = task_store.get_task(task_id)
            if current_record is None:
                yield "event: error\ndata: {\"message\":\"Task not found.\"}\n\n"
                return

            events = task_store.list_events_after(task_id, last_seen)
            if events:
                idle_ticks = 0
                for event in events:
                    if await request.is_disconnected():
                        return
                    last_seen = event.created_at.isoformat()
                    payload = {
                        "event": event.to_response().model_dump(mode="json"),
                        "status": current_record.status.value,
                        "updated_at": current_record.updated_at.isoformat(),
                        "result": current_record.result.model_dump(mode="json") if current_record.result is not None else None,
                    }
                    yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                idle_ticks += 1

            current_record = task_store.get_task(task_id)
            if current_record is None or current_record.status in terminal_statuses:
                if idle_ticks >= 2:
                    return

            if idle_ticks >= 20:
                if await request.is_disconnected():
                    return
                yield "event: heartbeat\ndata: {}\n\n"
                idle_ticks = 0

            if await request.is_disconnected():
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}/progress", response_model=TaskProgressResponse)
def get_task_progress(task_id: str, request: Request) -> TaskProgressResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    latest_event = task_store.get_latest_event(task_id)
    return TaskProgressResponse(
        task_id=record.task_id,
        status=record.status,
        progress=int(latest_event.progress) if latest_event is not None else 0,
        latest_stage=latest_event.stage if latest_event is not None else None,
        latest_message=latest_event.message if latest_event is not None else None,
        updated_at=record.updated_at,
    )
