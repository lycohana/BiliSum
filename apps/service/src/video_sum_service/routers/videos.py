import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from video_sum_core.models.tasks import InputType, TaskInput

from video_sum_service.context import LOCAL_MEDIA_UPLOAD_DIR, logger, settings_manager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.runtime_startup import submit_task_or_queue
from video_sum_service.schemas import (
    AggregateSummaryRequest,
    ResummaryRequest,
    TaskDetailResponse,
    TaskSummaryResponse,
    VideoAssetDetailResponse,
    VideoAssetRecord,
    VideoAssetSummaryResponse,
    VideoFolderCreateRequest,
    VideoFolderResponse,
    VideoFolderUpdateRequest,
    VideoLibraryPreferencesResponse,
    VideoLibraryPreferencesUpdateRequest,
    VideoLibraryResponse,
    VideoMoveRequest,
    VideoPinRequest,
    VideoProbeRequest,
    VideoProbeResponse,
    VideoReorderRequest,
    VideoTaskBatchPageResponse,
    VideoTaskBatchRequest,
    VideoTaskBatchResponse,
    VideoTaskCreateRequest,
)
from video_sum_service.task_artifacts import cleanup_video_files, load_task_segments
from video_sum_service.video_assets import (
    infer_local_input_type,
    is_supported_local_media_file,
    localize_video_cover,
    merge_video_asset_metadata,
    probe_local_video_asset,
    probe_video_asset,
    resolve_video_page,
)

router = APIRouter(prefix="/api/v1/videos")
AGGREGATE_SUMMARY_PAGE_NUMBER = 0
AGGREGATE_SUMMARY_PAGE_TITLE = "全集总结"
AGGREGATE_SUMMARY_TITLE_SUFFIX = "｜全集总结"
AGGREGATE_OVERVIEW_LIMIT = 520
AGGREGATE_KEY_POINT_LIMIT = 160
AGGREGATE_CHAPTER_LIMIT = 180
AGGREGATE_NOTE_LIMIT = 360
AGGREGATE_MAX_KEY_POINTS_PER_PAGE = 8
AGGREGATE_MAX_CHAPTERS_PER_PAGE = 8


def _normalize_batch_page_numbers(video: VideoAssetRecord, page_numbers: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_page_number in page_numbers:
        if raw_page_number in seen:
            continue
        seen.add(raw_page_number)
        normalized.append(int(raw_page_number))

    if not normalized:
        raise HTTPException(status_code=400, detail="至少选择一个分 P。")
    if not video.pages:
        raise HTTPException(status_code=400, detail="当前视频不包含可批量处理的分 P。")

    existing_pages = {page.page for page in video.pages}
    invalid_pages = [page_number for page_number in normalized if page_number not in existing_pages]
    if invalid_pages:
        raise HTTPException(
            status_code=400,
            detail=f"所选分 P 不存在：{', '.join(f'P{page_number}' for page_number in invalid_pages)}。",
        )
    return normalized


def _find_latest_completed_task_for_page(tasks, page_number: int):
    return next(
        (
            task
            for task in tasks
            if (task.page_number if task.page_number is not None else 1) == page_number
            and task.status.value == "completed"
            and task.result is not None
        ),
        None,
    )


def _find_resummary_source_task(
    task_store: SqliteTaskRepository,
    video_id: str,
    body: ResummaryRequest,
):
    source_task = task_store.get_task(body.task_id) if body.task_id else None
    if source_task is None:
        source_task = next(
            (
                task
                for task in task_store.list_tasks_for_video(video_id)
                if (
                    task.result
                    and task.result.transcript_text.strip()
                    and task.result.artifacts.get("summary_path")
                    and (
                        body.page_number is None
                        or _task_page_number(task) == body.page_number
                    )
                )
            ),
            None,
        )
    return source_task


def _task_page_number(task) -> int:
    return task.page_number if task.page_number is not None else 1


def _compact_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _has_aggregate_summary_material(result) -> bool:
    return bool(
        str(result.overview or "").strip()
        or any(str(item).strip() for item in result.key_points)
        or any(str(item).strip() for item in result.segment_summaries)
        or any(isinstance(item, dict) and str(item.get("summary") or "").strip() for item in result.timeline)
        or str(result.knowledge_note_markdown or "").strip()
    )


def _select_aggregate_source_tasks(video: VideoAssetRecord, tasks, page_numbers: list[int] | None):
    requested_pages = set(page_numbers) if page_numbers is not None else None
    page_titles = {page.page: page.title for page in video.pages}
    latest_completed_by_page = {}
    for task in tasks:
        page_number = _task_page_number(task)
        if page_number <= 0:
            continue
        if requested_pages is not None and page_number not in requested_pages:
            continue
        if task.status.value != "completed" or task.result is None:
            continue
        if not _has_aggregate_summary_material(task.result):
            continue
        if page_number not in page_titles:
            continue
        latest_completed_by_page.setdefault(page_number, task)
    return [
        latest_completed_by_page[page_number]
        for page_number in sorted(latest_completed_by_page)
    ]


def _build_aggregate_summary_payload(
    video: VideoAssetRecord,
    source_tasks,
) -> tuple[str, str, list[dict[str, object]]]:
    title = f"{video.title}{AGGREGATE_SUMMARY_TITLE_SUFFIX}"
    transcript_parts: list[str] = []
    segments: list[dict[str, object]] = []

    # 根据总 P 数动态调整每 P 的压缩限制
    page_count = len(source_tasks)
    if page_count <= 20:
        overview_limit = 520
        key_point_limit = 160
        chapter_limit = 180
        note_limit = 360
        max_key_points_per_page = 8
        max_chapters_per_page = 8
    elif page_count <= 40:
        overview_limit = 400
        key_point_limit = 120
        chapter_limit = 140
        note_limit = 280
        max_key_points_per_page = 6
        max_chapters_per_page = 6
    elif page_count <= 60:
        overview_limit = 300
        key_point_limit = 100
        chapter_limit = 100
        note_limit = 200
        max_key_points_per_page = 4
        max_chapters_per_page = 4
    else:
        overview_limit = 200
        key_point_limit = 80
        chapter_limit = 70
        note_limit = 140
        max_key_points_per_page = 3
        max_chapters_per_page = 3

    for task in source_tasks:
        result = task.result
        assert result is not None
        page_number = _task_page_number(task)
        page_title = task.page_title or task.task_input.title or f"P{page_number}"
        key_points = [
            _compact_text(item, key_point_limit)
            for item in result.key_points
            if str(item).strip()
        ][:max_key_points_per_page]
        timeline = [item for item in result.timeline if isinstance(item, dict)]
        segment_summaries = [
            _compact_text(item, chapter_limit)
            for item in result.segment_summaries
            if str(item).strip()
        ]
        note = _compact_text(result.knowledge_note_markdown, note_limit)
        overview = _compact_text(result.overview, overview_limit)

        chapter_fragments: list[str] = []
        for chapter in timeline[:max_chapters_per_page]:
            chapter_title = str(chapter.get("title") or "").strip() or "章节"
            chapter_summary = _compact_text(chapter.get("summary"), chapter_limit)
            if chapter_summary:
                chapter_fragments.append(f"{chapter_title}：{chapter_summary}")

        section_lines = [f"## P{page_number} {page_title}"]
        if overview:
            section_lines.append(f"[重点-概览] {overview}")
        if key_points:
            section_lines.append(f"[重点-要点] {'；'.join(key_points)}")
        if chapter_fragments:
            section_lines.append(f"[重点-章节] {'；'.join(chapter_fragments)}")
        elif segment_summaries:
            section_lines.append(
                f"[重点-片段] {'；'.join(segment_summaries[:max_chapters_per_page])}"
            )
        if note:
            section_lines.append(f"[重点-笔记] {note}")

        section_text = "\n".join(section_lines).strip()
        transcript_parts.append(section_text)
        segments.append(
            {
                "start": float(page_number),
                "end": float(page_number),
                "text": f"P{page_number} {page_title}：{overview or page_title}",
            }
        )

        for chapter in timeline[:max_chapters_per_page]:
            chapter_summary = _compact_text(chapter.get("summary"), chapter_limit)
            if not chapter_summary:
                continue
            chapter_title = str(chapter.get("title") or "").strip() or "章节"
            segments.append(
                {
                    "start": float(page_number),
                    "end": float(page_number),
                    "text": f"P{page_number} {page_title} - {chapter_title}\n{chapter_summary}",
                }
            )

    return title, "\n\n".join(transcript_parts).strip(), segments


def _create_video_task_record(
    *,
    app_state,
    task_store: SqliteTaskRepository,
    video: VideoAssetRecord,
    page_number: int | None = None,
    visual_note_mode: str | None = None,
    prompt_preset_id: str | None = None,
):
    page = resolve_video_page(video, page_number)
    if video.pages and page_number is not None and page is None:
        raise HTTPException(status_code=400, detail="Selected page not found.")

    source_url = page.source_url if page else video.source_url
    title = page.title if page else video.title
    logger.info(
        "create video task video_id=%s page=%s title=%s source=%s visual_note_mode=%s prompt_preset_id=%s",
        video.video_id,
        page.page if page else None,
        title,
        source_url,
        visual_note_mode,
        prompt_preset_id,
    )
    input_type = InputType.URL
    if str(video.platform or "").lower() == "local":
        input_type = infer_local_input_type(source_url)
    task_input = TaskInput(input_type=input_type, source=source_url, title=title, platform_hint=video.platform)
    if visual_note_mode is not None:
        task_input.options.visual_note_mode = visual_note_mode
    if prompt_preset_id is not None:
        task_input.options.prompt_preset_id = prompt_preset_id
    record = task_store.create_task(
        task_input,
        video_id=video.video_id,
        page_number=page.page if page else None,
        page_title=title,
    )
    submit_task_or_queue(app_state, task_store, record)
    refreshed = task_store.get_task(record.task_id)
    assert refreshed is not None
    return refreshed


def _create_resummary_task_record(
    *,
    app_state,
    task_store: SqliteTaskRepository,
    video: VideoAssetRecord,
    source_task,
):
    if source_task.video_id != video.video_id:
        raise HTTPException(status_code=400, detail="所选任务不属于当前视频。")
    if source_task.result is None or not source_task.result.transcript_text.strip():
        raise HTTPException(status_code=400, detail="所选任务还没有可复用的转写文本。")

    summary_path = source_task.result.artifacts.get("summary_path") if source_task.result else None
    if not summary_path:
        raise HTTPException(status_code=400, detail="所选任务缺少可复用的分段文件。")
    segments = load_task_segments(summary_path)

    payload = json.dumps(
        {
            "title": source_task.task_input.title or video.title,
            "transcript": source_task.result.transcript_text,
            "segments": segments,
        },
        ensure_ascii=False,
    )
    logger.info(
        "create video resummary task video_id=%s source_task_id=%s title=%s",
        video.video_id,
        source_task.task_id,
        source_task.task_input.title or video.title,
    )
    record = task_store.create_task(
        TaskInput(
            input_type=InputType.TRANSCRIPT_TEXT,
            source=payload,
            title=source_task.task_input.title or video.title,
            platform_hint=source_task.task_input.platform_hint,
            options=source_task.task_input.options,
        ),
        video_id=video.video_id,
        page_number=source_task.page_number,
        page_title=source_task.page_title or source_task.task_input.title or video.title,
    )
    submit_task_or_queue(app_state, task_store, record)
    refreshed = task_store.get_task(record.task_id)
    assert refreshed is not None
    return refreshed


def normalize_uploaded_media_filename(filename: str) -> tuple[str, str]:
    raw_name = Path(str(filename or "").strip()).name
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少有效文件名。")
    suffix = Path(raw_name).suffix.lower()
    if not suffix or not is_supported_local_media_file(Path(raw_name)):
        raise HTTPException(status_code=400, detail="当前仅支持导入常见本地视频或音频文件。")
    title = Path(raw_name).stem.strip() or "本地媒体"
    return title, suffix


async def _cache_uploaded_media_chunks(
    filename: str,
    chunks: AsyncIterator[bytes | str],
) -> tuple[Path, str, str]:
    title, suffix = normalize_uploaded_media_filename(filename)
    LOCAL_MEDIA_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = LOCAL_MEDIA_UPLOAD_DIR / f"{uuid4().hex}.upload"
    digest = hashlib.sha1()
    total_bytes = 0
    try:
        with temp_path.open("wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                handle.write(chunk)
                digest.update(chunk)
                total_bytes += len(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="保存上传视频时失败。") from exc

    if total_bytes <= 0:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove empty upload temp file: %s", temp_path, exc_info=True)
        raise HTTPException(status_code=400, detail="上传文件为空。")

    content_hash = digest.hexdigest()
    final_path = LOCAL_MEDIA_UPLOAD_DIR / f"{content_hash}{suffix}"
    if final_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            logger.warning("failed to remove duplicate upload temp file: %s", temp_path, exc_info=True)
    else:
        try:
            temp_path.replace(final_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="整理上传视频文件时失败。") from exc
    return final_path.resolve(), title, content_hash


async def cache_uploaded_media_file(request: Request, filename: str) -> tuple[Path, str, str]:
    return await _cache_uploaded_media_chunks(filename, request.stream())


async def _iter_upload_form_file(upload_file) -> AsyncIterator[bytes]:
    while True:
        chunk = await upload_file.read(1024 * 1024)
        if not chunk:
            break
        yield chunk


def _upsert_local_upload_response(
    task_store: SqliteTaskRepository,
    saved_path: Path,
    title: str,
    content_hash: str,
) -> VideoProbeResponse:
    probed = probe_local_video_asset(
        saved_path,
        title_override=title,
        canonical_id_override=f"local-upload-{content_hash[:24]}",
    )
    existing = task_store.get_video_asset_by_canonical_id(probed.canonical_id)
    cached = existing is not None
    asset = existing if cached else task_store.upsert_video_asset(probed)
    asset = localize_video_cover(task_store, asset)
    return VideoProbeResponse(
        video=asset.to_summary(),
        cached=cached,
        requires_selection=False,
        pages=[],
    )


@router.post("/{video_id}/favorite", response_model=VideoAssetDetailResponse)
def set_video_favorite(video_id: str, payload: dict[str, bool], request: Request) -> VideoAssetDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    updated = task_store.set_video_favorite(video_id, bool(payload.get("is_favorite")))
    if updated is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    return localize_video_cover(task_store, updated).to_detail()


@router.post("/probe", response_model=VideoProbeResponse)
def probe_video(request: VideoProbeRequest, app_request: Request) -> VideoProbeResponse:
    task_store: SqliteTaskRepository = app_request.app.state.task_repository
    logger.info("probe video url=%s force_refresh=%s", request.url, request.force_refresh)
    probed, pages, requires_selection = probe_video_asset(request.url, request.force_refresh)
    existing = task_store.get_video_asset_by_canonical_id(probed.canonical_id)
    cached = existing is not None and not request.force_refresh
    asset = existing if cached else task_store.upsert_video_asset(merge_video_asset_metadata(existing, probed) if existing else probed)
    asset = localize_video_cover(task_store, asset)
    return VideoProbeResponse(
        video=asset.to_summary(),
        cached=cached,
        requires_selection=requires_selection,
        pages=asset.pages or pages,
    )


@router.post("/upload", response_model=VideoProbeResponse)
async def upload_local_video(
    request: Request,
    filename: str = Query(..., min_length=1),
) -> VideoProbeResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    saved_path, title, content_hash = await cache_uploaded_media_file(request, filename)
    logger.info("upload local media filename=%s saved_path=%s", filename, saved_path)
    return _upsert_local_upload_response(task_store, saved_path, title, content_hash)


@router.post("/upload/batch", response_model=list[VideoProbeResponse])
async def upload_local_videos_batch(request: Request) -> list[VideoProbeResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    form = await request.form()
    upload_files = [
        item
        for item in form.values()
        if hasattr(item, "filename") and hasattr(item, "read")
    ]
    if not upload_files:
        raise HTTPException(status_code=400, detail="至少上传一个本地视频或音频文件。")

    responses: list[VideoProbeResponse] = []
    for upload_file in upload_files:
        filename = str(getattr(upload_file, "filename", "") or "")
        saved_path, title, content_hash = await _cache_uploaded_media_chunks(
            filename,
            _iter_upload_form_file(upload_file),
        )
        logger.info(
            "batch upload local media filename=%s saved_path=%s",
            filename,
            saved_path,
        )
        responses.append(
            _upsert_local_upload_response(task_store, saved_path, title, content_hash)
        )
    return responses


@router.get("", response_model=list[VideoAssetSummaryResponse])
def list_videos(request: Request) -> list[VideoAssetSummaryResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return [localize_video_cover(task_store, video).to_summary() for video in task_store.list_video_assets()]


@router.get("/library", response_model=VideoLibraryResponse)
def get_video_library(request: Request) -> VideoLibraryResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return VideoLibraryResponse(
        videos=[localize_video_cover(task_store, video).to_summary() for video in task_store.list_video_assets()],
        folders=task_store.list_video_folders(),
        preferences=task_store.get_library_preferences(),
    )


@router.put("/library/preferences", response_model=VideoLibraryPreferencesResponse)
def update_video_library_preferences(
    payload: VideoLibraryPreferencesUpdateRequest,
    request: Request,
) -> VideoLibraryPreferencesResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return task_store.update_library_preferences(new_video_position=payload.new_video_position)


@router.post("/folders", response_model=VideoFolderResponse, status_code=status.HTTP_201_CREATED)
def create_video_folder(payload: VideoFolderCreateRequest, request: Request) -> VideoFolderResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    folder = task_store.create_video_folder(payload.name, payload.parent_id)
    if folder is None:
        raise HTTPException(status_code=400, detail="Folder name or parent is invalid.")
    return folder


@router.patch("/folders/{folder_id}", response_model=VideoFolderResponse)
def update_video_folder(folder_id: str, payload: VideoFolderUpdateRequest, request: Request) -> VideoFolderResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    try:
        folder = task_store.update_video_folder(
            folder_id,
            name=payload.name,
            **({"parent_id": payload.parent_id} if "parent_id" in payload.model_fields_set else {}),
            position=payload.position,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found.")
    return folder


@router.delete("/folders/{folder_id}")
def delete_video_folder(folder_id: str, request: Request) -> dict[str, object]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    deleted = task_store.delete_video_folder(folder_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Folder not found.")
    return {"deleted": True, "folder_id": folder_id}


@router.post("/reorder", response_model=list[VideoAssetSummaryResponse])
def reorder_videos(payload: VideoReorderRequest, request: Request) -> list[VideoAssetSummaryResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    folder_id = "__global__" if payload.folder_id == "__global__" else payload.folder_id
    try:
        videos = task_store.reorder_videos(payload.video_ids, folder_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        localize_video_cover(task_store, video).to_summary()
        for video in videos
    ]


@router.patch("/{video_id}/move", response_model=VideoAssetDetailResponse)
def move_video_to_folder(video_id: str, payload: VideoMoveRequest, request: Request) -> VideoAssetDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    if payload.folder_ids is not None:
        updated = task_store.set_video_folders(video_id, payload.folder_ids)
    else:
        updated = task_store.move_video_to_folder(video_id, payload.folder_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Video or folder not found.")
    return localize_video_cover(task_store, updated).to_detail()


@router.patch("/{video_id}/pin", response_model=VideoAssetDetailResponse)
def set_video_pin(video_id: str, payload: VideoPinRequest, request: Request) -> VideoAssetDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    updated = task_store.set_video_pin(
        video_id,
        global_pinned=payload.global_pinned,
        folder_pinned=payload.folder_pinned,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    return localize_video_cover(task_store, updated).to_detail()


@router.get("/{video_id}", response_model=VideoAssetDetailResponse)
def get_video(video_id: str, request: Request) -> VideoAssetDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    return localize_video_cover(task_store, video).to_detail()


@router.get("/{video_id}/media")
def get_local_video_media(video_id: str, request: Request) -> FileResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    if str(video.platform or "").lower() != "local":
        raise HTTPException(status_code=400, detail="当前视频不支持本地播放器。")

    source_path = Path(str(video.source_url or "")).expanduser()
    try:
        resolved_path = source_path.resolve()
    except OSError:
        resolved_path = source_path
    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="本地视频文件不存在或已被移动。")
    if not is_supported_local_media_file(resolved_path):
        raise HTTPException(status_code=400, detail="当前文件类型不支持本地播放器。")

    media_type = None
    suffix = resolved_path.suffix.lower()
    if suffix == ".mp4":
        media_type = "video/mp4"
    elif suffix == ".webm":
        media_type = "video/webm"
    elif suffix in {".mov", ".m4v"}:
        media_type = "video/quicktime"
    elif suffix == ".ogg":
        media_type = "audio/ogg"
    elif suffix == ".mp3":
        media_type = "audio/mpeg"
    return FileResponse(resolved_path, media_type=media_type, filename=resolved_path.name)


@router.delete("/{video_id}")
def delete_video(video_id: str, request: Request) -> dict[str, object]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    tasks = task_store.list_tasks_for_video(video_id)
    deleted = task_store.delete_video_asset(video_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Video not found.")
    cleanup_video_files(video, tasks, settings_manager.current)
    logger.info("delete video video_id=%s", video_id)
    return {"deleted": True, "video_id": video_id}


@router.get("/{video_id}/tasks", response_model=list[TaskSummaryResponse])
def get_video_tasks(video_id: str, request: Request) -> list[TaskSummaryResponse]:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    return [task.to_summary() for task in task_store.list_tasks_for_video(video_id)]


@router.post("/{video_id}/tasks", response_model=TaskDetailResponse, status_code=status.HTTP_201_CREATED)
def create_video_task(
    request: Request,
    video_id: str,
    request_body: VideoTaskCreateRequest | None = None,
) -> TaskDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    refreshed = _create_video_task_record(
        app_state=request.app.state,
        task_store=task_store,
        video=video,
        page_number=getattr(request_body, "page_number", None) if request_body else None,
        visual_note_mode=getattr(request_body, "visual_note_mode", None) if request_body else None,
        prompt_preset_id=getattr(request_body, "prompt_preset_id", None) if request_body else None,
    )
    return refreshed.to_detail()


@router.post("/{video_id}/tasks/resummary", response_model=TaskDetailResponse, status_code=status.HTTP_201_CREATED)
def create_video_resummary_task(video_id: str, body: ResummaryRequest, request: Request) -> TaskDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")

    source_task = _find_resummary_source_task(task_store, video_id, body)
    if source_task is None:
        raise HTTPException(status_code=400, detail="当前视频还没有可复用的转写结果。")

    refreshed = _create_resummary_task_record(
        app_state=request.app.state,
        task_store=task_store,
        video=video,
        source_task=source_task,
    )
    return refreshed.to_detail()


@router.post(
    "/{video_id}/tasks/aggregate-summary",
    response_model=TaskDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_video_aggregate_summary_task(
    video_id: str,
    request: Request,
    body: AggregateSummaryRequest | None = None,
) -> TaskDetailResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    if not video.pages:
        raise HTTPException(status_code=400, detail="当前视频不是多 P 视频，无法生成全集总结。")

    requested_pages = body.page_numbers if body and body.page_numbers is not None else None
    if requested_pages is not None:
        _normalize_batch_page_numbers(video, requested_pages)

    source_tasks = _select_aggregate_source_tasks(
        video,
        task_store.list_tasks_for_video(video_id),
        requested_pages,
    )
    if not source_tasks:
        raise HTTPException(status_code=400, detail="还没有可汇总的分 P 摘要，请先生成至少一个分 P 摘要。")

    title, transcript, segments = _build_aggregate_summary_payload(video, source_tasks)
    payload = json.dumps(
        {
            "title": title,
            "source_kind": "aggregate_series",
            "transcript": transcript,
            "segments": segments,
        },
        ensure_ascii=False,
    )
    record = task_store.create_task(
        TaskInput(
            input_type=InputType.TRANSCRIPT_TEXT,
            source=payload,
            title=title,
            platform_hint=video.platform,
        ),
        video_id=video.video_id,
        page_number=AGGREGATE_SUMMARY_PAGE_NUMBER,
        page_title=AGGREGATE_SUMMARY_PAGE_TITLE,
    )
    submit_task_or_queue(request.app.state, task_store, record)
    refreshed = task_store.get_task(record.task_id)
    assert refreshed is not None
    return refreshed.to_detail()


@router.post("/{video_id}/tasks/batch", response_model=VideoTaskBatchResponse, status_code=status.HTTP_201_CREATED)
def create_video_tasks_batch(video_id: str, body: VideoTaskBatchRequest, request: Request) -> VideoTaskBatchResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")

    page_numbers = _normalize_batch_page_numbers(video, body.page_numbers)
    tasks = task_store.list_tasks_for_video(video_id)

    conflict_pages: list[VideoTaskBatchPageResponse] = []
    skipped_pages: list[VideoTaskBatchPageResponse] = []
    created_tasks: list[TaskDetailResponse] = []
    creatable_page_numbers: list[int] = []

    for page_number in page_numbers:
        page = resolve_video_page(video, page_number)
        assert page is not None
        existing_completed_task = _find_latest_completed_task_for_page(tasks, page_number)
        if existing_completed_task is None:
            creatable_page_numbers.append(page_number)
            continue

        conflict_pages.append(
            VideoTaskBatchPageResponse(
                page_number=page_number,
                page_title=page.title,
                action="skip",
                reason="该分 P 已有成功摘要，批量生成将默认跳过。",
                existing_task_id=existing_completed_task.task_id,
                existing_status=existing_completed_task.status,
                has_existing_result=True,
            )
        )

    if conflict_pages and not body.confirm:
        return VideoTaskBatchResponse(
            operation="create",
            requested_page_numbers=page_numbers,
            requires_confirmation=True,
            created_tasks=[],
            skipped_pages=[],
            conflict_pages=conflict_pages,
        )

    if conflict_pages:
        skipped_pages.extend(conflict_pages)

    prompt_preset_id = getattr(body, "prompt_preset_id", None)
    for page_number in creatable_page_numbers:
        refreshed = _create_video_task_record(
            app_state=request.app.state,
            task_store=task_store,
            video=video,
            page_number=page_number,
            prompt_preset_id=prompt_preset_id,
        )
        created_tasks.append(refreshed.to_detail())

    return VideoTaskBatchResponse(
        operation="create",
        requested_page_numbers=page_numbers,
        requires_confirmation=False,
        created_tasks=created_tasks,
        skipped_pages=skipped_pages,
        conflict_pages=[],
    )


@router.post("/{video_id}/tasks/resummary/batch", response_model=VideoTaskBatchResponse, status_code=status.HTTP_201_CREATED)
def create_video_resummary_tasks_batch(video_id: str, body: VideoTaskBatchRequest, request: Request) -> VideoTaskBatchResponse:
    task_store: SqliteTaskRepository = request.app.state.task_repository
    video = task_store.get_video_asset(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")

    page_numbers = _normalize_batch_page_numbers(video, body.page_numbers)
    conflict_pages: list[VideoTaskBatchPageResponse] = []
    skipped_pages: list[VideoTaskBatchPageResponse] = []
    created_tasks: list[TaskDetailResponse] = []

    source_tasks_by_page = {
        page_number: _find_resummary_source_task(
            task_store,
            video_id,
            ResummaryRequest(page_number=page_number),
        )
        for page_number in page_numbers
    }

    for page_number in page_numbers:
        page = resolve_video_page(video, page_number)
        assert page is not None
        source_task = source_tasks_by_page[page_number]
        if source_task is None:
            skipped_pages.append(
                VideoTaskBatchPageResponse(
                    page_number=page_number,
                    page_title=page.title,
                    action="skip",
                    reason="该分 P 暂无可复用的转写与摘要结果。",
                    has_existing_result=False,
                )
            )
            continue

        conflict_pages.append(
            VideoTaskBatchPageResponse(
                page_number=page_number,
                page_title=page.title,
                action="rerun",
                reason="该分 P 已有成功摘要，确认后将复用转写重新生成摘要。",
                existing_task_id=source_task.task_id,
                existing_status=source_task.status,
                has_existing_result=True,
            )
        )

    if conflict_pages and not body.confirm:
        return VideoTaskBatchResponse(
            operation="resummary",
            requested_page_numbers=page_numbers,
            requires_confirmation=True,
            created_tasks=[],
            skipped_pages=skipped_pages,
            conflict_pages=conflict_pages,
        )

    for page_number in page_numbers:
        source_task = source_tasks_by_page[page_number]
        if source_task is None:
            continue
        refreshed = _create_resummary_task_record(
            app_state=request.app.state,
            task_store=task_store,
            video=video,
            source_task=source_task,
        )
        created_tasks.append(refreshed.to_detail())

    return VideoTaskBatchResponse(
        operation="resummary",
        requested_page_numbers=page_numbers,
        requires_confirmation=False,
        created_tasks=created_tasks,
        skipped_pages=skipped_pages,
        conflict_pages=[],
    )
