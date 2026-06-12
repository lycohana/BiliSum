import httpx
from contextlib import asynccontextmanager
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from video_sum_service.auth import is_auth_exempt_path, request_is_authorized, unauthorized_response
from video_sum_core.models.tasks import TaskStatus
from video_sum_infra.db import connect_sqlite
from video_sum_infra.config import normalize_visual_note_mode
from video_sum_infra.runtime import (
    activate_runtime_pythonpath,
    bootstrap_managed_runtime,
    is_frozen,
    prepend_runtime_path,
    repo_root,
    runtime_python_executable,
    write_runtime_metadata,
)

from video_sum_service.context import CACHE_STATIC_DIR, WEB_STATIC_DIR, access_token_manager, app_info, logger, settings_manager
from video_sum_service.integrations import probe_asr_connection, probe_llm_connection
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers.system import router as system_router
from video_sum_service.routers.knowledge import router as knowledge_router
from video_sum_service.routers.tasks import router as tasks_router
from video_sum_service.routers.videos import router as videos_router
from video_sum_service.runtime_support import (
    build_worker,
    clear_environment_probe_cache,
    detect_environment,
    ensure_runtime_channel,
    ensure_runtime_pip,
    install_workspace_packages,
    normalize_runtime_channel,
    pip_install_with_fallbacks,
    replace_task_worker,
    run_command,
    serialize_settings,
    install_knowledge_dependencies as install_knowledge_dependencies_with_worker,
)
from video_sum_service.runtime_startup import (
    get_runtime_startup_state,
    initialize_runtime_startup_state,
    mark_runtime_worker_ready,
    request_runtime_startup_shutdown,
    start_runtime_startup,
    submit_mindmap_or_queue,
)
from video_sum_service.schemas import TaskMindMapResponse, TaskVisualEvidenceResponse
from video_sum_service.settings_manager import SettingsUpdatePayload
from video_sum_service.task_artifacts import cleanup_video_files, load_task_mindmap, load_visual_context, load_visual_note_markdown
import video_sum_service.video_assets as video_assets

probe_video_asset = video_assets.probe_video_asset

_cleanup_video_files = cleanup_video_files


def _uses_current_service_python(runtime_channel: str) -> bool:
    return not is_frozen() and runtime_channel == "base"


def _run_host_command(command: list[str], timeout: int = 3600):
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
        env=env,
        cwd=repo_root(),
    )


def recover_incomplete_tasks(repository: SqliteTaskRepository, task_worker) -> int:
    recoverable = repository.list_recoverable_tasks()
    if not recoverable:
        return 0

    recovered_count = 0
    for record in recoverable:
        if record.status == TaskStatus.RUNNING:
            logger.warning("recover interrupted running task task_id=%s video_id=%s", record.task_id, record.video_id)
        else:
            logger.info("recover queued task task_id=%s video_id=%s", record.task_id, record.video_id)
        task_worker.submit(record)
        recovered_count += 1

    logger.info("recovered incomplete tasks count=%d", recovered_count)
    return recovered_count


@asynccontextmanager
async def lifespan(app: FastAPI):
    current_settings = settings_manager.current
    connection = connect_sqlite(current_settings.database_url)
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    app.state.task_repository = repository
    app.state.db_connection = connection
    app.state.settings_manager = settings_manager
    initialize_runtime_startup_state(app.state, current_settings)
    start_runtime_startup(app.state, repository, current_settings, recover_incomplete_tasks)
    logger.info("application core startup complete database=%s", current_settings.database_url)
    try:
        yield
    finally:
        request_runtime_startup_shutdown(app.state)
        startup_thread = getattr(app.state, "runtime_startup_thread", None)
        if startup_thread is not None and startup_thread.is_alive():
            startup_thread.join(timeout=10)
        task_worker = getattr(app.state, "task_worker", None)
        if task_worker is not None and hasattr(task_worker, "shutdown"):
            task_worker.shutdown(wait=True, timeout=10)
        logger.info("application shutdown")
        connection.close()


app = FastAPI(
    title="BiliSum Service",
    version=app_info.version,
    description="Local-first backend service for BiliSum video summarization tasks.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=WEB_STATIC_DIR), name="static")
app.mount("/media", StaticFiles(directory=CACHE_STATIC_DIR), name="media")
app.include_router(system_router)
app.include_router(videos_router)
app.include_router(tasks_router)
app.include_router(knowledge_router)


@app.middleware("http")
async def require_api_access_token(request, call_next):
    path = request.url.path
    if path.startswith("/api/v1/") and not is_auth_exempt_path(path, request.method):
        if not request_is_authorized(request, access_token_manager):
            return unauthorized_response()
    return await call_next(request)


def frontend_shell_response() -> FileResponse:
    return FileResponse(
        WEB_STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return frontend_shell_response()


@app.get("/videos/{video_id}", include_in_schema=False)
def video_detail_page(video_id: str) -> FileResponse:
    del video_id
    return frontend_shell_response()


@app.get("/settings", include_in_schema=False)
@app.get("/settings/{subpath:path}", include_in_schema=False)
def settings_page(subpath: str = "") -> FileResponse:
    del subpath
    return frontend_shell_response()


@app.get("/knowledge", include_in_schema=False)
@app.get("/knowledge/{subpath:path}", include_in_schema=False)
def knowledge_page(subpath: str = "") -> FileResponse:
    del subpath
    return frontend_shell_response()


@app.get("/health")
def health() -> dict[str, object]:
    runtime_startup = get_runtime_startup_state(app.state)
    runtime_status = str(runtime_startup.get("status") or "initializing")
    status_value = "ok" if runtime_status == "ready" else "degraded" if runtime_status == "error" else "starting"
    return {
        "status": status_value,
        "service": app_info.name,
        "version": app_info.version,
        "runtime_startup": runtime_startup,
    }


def _run_command(command: list[str], runtime_channel: str, timeout: int = 3600):
    return run_command(command, runtime_channel=runtime_channel, timeout=timeout)


def _ensure_runtime_pip(python_executable, runtime_channel: str) -> None:
    return ensure_runtime_pip(python_executable, runtime_channel)


def _install_workspace_packages(python_executable, runtime_channel: str) -> None:
    if is_frozen():
        return

    _ensure_runtime_pip(python_executable, runtime_channel)
    root = repo_root()
    _run_command(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
            "hatchling>=1.27.0",
        ],
        runtime_channel=runtime_channel,
        timeout=900,
    )
    command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--no-build-isolation",
    ]
    if runtime_channel != "base":
        command.append("--no-deps")
    command.extend(
        [
            str(root / "packages" / "infra"),
            str(root / "packages" / "core"),
            str(root / "apps" / "service"),
        ]
    )
    _run_command(command, runtime_channel=runtime_channel, timeout=1800)


def update_settings(payload: SettingsUpdatePayload) -> dict[str, object]:
    previous_settings = settings_manager.current
    requested_runtime_channel = normalize_runtime_channel(
        payload.runtime_channel or previous_settings.runtime_channel,
        allow_unknown_gpu=True,
    )
    if payload.runtime_channel is not None:
        payload = payload.model_copy(update={"runtime_channel": requested_runtime_channel})
    ensure_runtime_channel(requested_runtime_channel)

    current_settings = settings_manager.save(payload)
    active_runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)
    bootstrap_managed_runtime(active_runtime_channel)
    prepend_runtime_path(active_runtime_channel)
    activate_runtime_pythonpath(active_runtime_channel)
    current_settings.data_dir.mkdir(parents=True, exist_ok=True)
    current_settings.cache_dir.mkdir(parents=True, exist_ok=True)
    current_settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    runtime_channel_changed = previous_settings.runtime_channel != current_settings.runtime_channel
    if runtime_channel_changed:
        clear_environment_probe_cache(previous_settings.runtime_channel)
        clear_environment_probe_cache(current_settings.runtime_channel)
    environment = detect_environment(active_runtime_channel)
    replace_task_worker(
        app.state,
        build_worker(
            app.state.task_repository,
            current_settings,
            environment_info=environment,
        ),
    )
    mark_runtime_worker_ready(app.state, environment, message="Runtime worker refreshed after settings update.")
    return {
        "saved": True,
        "settings": serialize_settings(current_settings, environment_info=environment),
        "message": "设置已保存。涉及服务监听地址的修改将在下次启动后生效。",
    }


def install_local_asr(reinstall: bool = False) -> dict[str, object]:
    current_settings = settings_manager.current
    runtime_channel = current_settings.runtime_channel
    runtime_dir = ensure_runtime_channel(runtime_channel)
    python_executable = runtime_python_executable(runtime_channel)
    if runtime_dir is None or python_executable is None:
        raise HTTPException(status_code=500, detail="Managed runtime is unavailable.")

    try:
        _install_workspace_packages(python_executable, runtime_channel=runtime_channel)
        _ensure_runtime_pip(python_executable, runtime_channel)
        result = pip_install_with_fallbacks(
            python_executable,
            runtime_channel,
            ["faster-whisper>=1.1.1"],
            reinstall=reinstall,
            timeout=1800,
            runner=_run_command,
        )
    except Exception as exc:
        clear_environment_probe_cache(runtime_channel)
        if isinstance(exc, HTTPException):
            raise
        detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
        raise HTTPException(status_code=500, detail=str(detail)[-1500:]) from exc

    clear_environment_probe_cache(runtime_channel)
    environment = detect_environment(runtime_channel)
    replace_task_worker(
        app.state,
        build_worker(
            app.state.task_repository,
            current_settings,
            environment_info=environment,
        ),
    )
    mark_runtime_worker_ready(app.state, environment, message="Runtime worker refreshed after ASR install.")
    write_runtime_metadata(
        runtime_channel,
        {
            "runtimeChannel": runtime_channel,
            "python": str(python_executable),
            "localAsrInstalled": bool(environment.get("localAsrInstalled")),
            "localAsrVersion": str(environment.get("localAsrVersion") or ""),
        },
    )
    return {
        "installed": bool(environment.get("localAsrInstalled")),
        "runtimeChannel": runtime_channel,
        "stdoutTail": ((getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")).strip()[-1500:],
        "environment": environment,
    }


def install_knowledge_dependencies(
    reinstall: bool = False,
    runtime_channel: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    result, worker = install_knowledge_dependencies_with_worker(
        reinstall=reinstall,
        repository=app.state.task_repository,
        runtime_channel=runtime_channel,
        session_id=session_id,
    )
    if worker is not None:
        replace_task_worker(app.state, worker)
    if worker is not None and isinstance(result.get("environment"), dict):
        mark_runtime_worker_ready(app.state, result["environment"], message="Runtime worker refreshed after knowledge install.")
    return result


def get_task_mindmap(task_id: str) -> TaskMindMapResponse:
    task_store: SqliteTaskRepository = app.state.task_repository
    record = task_store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    result = record.result
    if result is None:
        return TaskMindMapResponse(task_id=task_id, status="idle")

    mindmap_path = result.mindmap_artifact_path or result.artifacts.get("mindmap_path")
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

    mindmap = None
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


def generate_task_mindmap(task_id: str, force: bool = False) -> TaskMindMapResponse:
    task_store: SqliteTaskRepository = app.state.task_repository
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
    submit_mindmap_or_queue(app.state, task_store, task_id, force=force)
    refreshed = task_store.get_task(task_id)
    refreshed_result = refreshed.result if refreshed is not None else generating_result
    return TaskMindMapResponse(
        task_id=task_id,
        status=refreshed_result.mindmap_status,
        error_message=refreshed_result.mindmap_error_message,
        updated_at=refreshed_result.mindmap_updated_at,
        mindmap=None,
    )


def get_task_visual_evidence(task_id: str) -> TaskVisualEvidenceResponse:
    task_store: SqliteTaskRepository = app.state.task_repository
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


def generate_task_visual_evidence(task_id: str, force: bool = False, mode: str | None = None) -> TaskVisualEvidenceResponse:
    task_store: SqliteTaskRepository = app.state.task_repository
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
        return get_task_visual_evidence(task_id)

    generating_result = record.result.model_copy(
        update={
            "visual_note_status": "generating",
            "visual_note_error_message": None,
        }
    )
    task_store.save_result(task_id, generating_result)
    worker = getattr(app.state, "task_worker", None)
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
