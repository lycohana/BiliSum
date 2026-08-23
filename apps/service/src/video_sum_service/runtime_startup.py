from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from threading import Lock, Thread
from typing import Any

from fastapi import HTTPException

from video_sum_core.models.tasks import TaskStatus
from video_sum_infra.config import ServiceSettings
from video_sum_service.context import logger
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.runtime_support import build_worker, detect_environment, replace_task_worker
from video_sum_service.schemas import TaskRecord


RuntimeRecover = Callable[[SqliteTaskRepository, Any], int]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minimal_environment_snapshot(current_settings: ServiceSettings) -> dict[str, object]:
    return {
        "runtimeChannel": current_settings.runtime_channel,
        "runtimeReady": False,
    }


def initialize_runtime_startup_state(app_state: Any, current_settings: ServiceSettings) -> None:
    app_state.runtime_startup_lock = Lock()
    app_state.runtime_startup_shutdown = False
    app_state.pending_mindmap_jobs: list[dict[str, object]] = []
    app_state.runtime_startup = {
        "status": "initializing",
        "message": "Runtime worker is initializing.",
        "started_at": _now_iso(),
        "ready_at": None,
        "error_at": None,
        "environment": _minimal_environment_snapshot(current_settings),
    }


def get_runtime_startup_state(app_state: Any) -> dict[str, object]:
    state = getattr(app_state, "runtime_startup", None)
    if not isinstance(state, dict):
        return {
            "status": "initializing",
            "message": "Runtime worker is initializing.",
            "started_at": None,
            "ready_at": None,
            "error_at": None,
            "environment": {},
        }
    return {
        **state,
        "environment": dict(state.get("environment") or {}),
    }


def runtime_worker_ready(app_state: Any) -> bool:
    state = getattr(app_state, "runtime_startup", None)
    if not isinstance(state, dict):
        return getattr(app_state, "task_worker", None) is not None
    return state.get("status") == "ready" and getattr(app_state, "task_worker", None) is not None


def request_runtime_startup_shutdown(app_state: Any) -> None:
    lock = getattr(app_state, "runtime_startup_lock", None)
    if lock is None:
        app_state.runtime_startup_shutdown = True
        return
    with lock:
        app_state.runtime_startup_shutdown = True


def runtime_startup_shutdown_requested(app_state: Any) -> bool:
    lock = getattr(app_state, "runtime_startup_lock", None)
    if lock is None:
        return bool(getattr(app_state, "runtime_startup_shutdown", False))
    with lock:
        return bool(getattr(app_state, "runtime_startup_shutdown", False))


def _runtime_startup_error_detail(app_state: Any) -> str | None:
    state = getattr(app_state, "runtime_startup", None)
    if not isinstance(state, dict) or state.get("status") != "error":
        return None
    message = str(state.get("message") or "").strip()
    return message or "运行环境初始化失败，请检查运行环境设置后重启服务。"


def _raise_runtime_startup_error(app_state: Any) -> None:
    detail = _runtime_startup_error_detail(app_state)
    if detail is not None:
        raise HTTPException(status_code=503, detail=detail)


def mark_runtime_worker_recovering(
    app_state: Any,
    environment: dict[str, object],
    message: str = "Runtime worker is recovering queued work.",
) -> None:
    lock = getattr(app_state, "runtime_startup_lock", None)
    if lock is None:
        return
    with lock:
        state = getattr(app_state, "runtime_startup", None)
        if not isinstance(state, dict):
            return
        state.update(
            {
                "status": "recovering",
                "message": message,
                "ready_at": None,
                "error_at": None,
                "environment": dict(environment),
            }
        )


def queue_task_for_runtime_startup(repository: SqliteTaskRepository, record: TaskRecord) -> None:
    repository.update_status(record.task_id, TaskStatus.QUEUED)
    repository.append_event(
        task_id=record.task_id,
        stage="queued",
        progress=0,
        message="任务已保存，等待运行环境初始化完成后自动执行",
        payload={"runtime_startup": True},
    )


def submit_task_or_queue(app_state: Any, repository: SqliteTaskRepository, record: TaskRecord) -> None:
    try:
        _raise_runtime_startup_error(app_state)
    except HTTPException as exc:
        repository.update_status(record.task_id, TaskStatus.FAILED)
        repository.update_error(record.task_id, "runtime_startup_failed", str(exc.detail))
        repository.append_event(
            task_id=record.task_id,
            stage="failed",
            progress=0,
            message="运行环境初始化失败，任务无法开始",
            payload={"runtime_startup": True, "error": str(exc.detail)},
        )
        raise

    task_worker = getattr(app_state, "task_worker", None)
    if runtime_worker_ready(app_state) and task_worker is not None:
        submit_result = task_worker.submit(record)
        if submit_result == "rejected":
            message = "后台任务队列正在关闭，暂时无法接收新任务。"
            repository.update_status(record.task_id, TaskStatus.FAILED)
            repository.update_error(record.task_id, "task_dispatch_rejected", message)
            repository.append_event(
                task_id=record.task_id,
                stage="failed",
                progress=0,
                message=message,
                payload={"dispatch_rejected": True},
            )
            raise RuntimeError(message)
        return
    queue_task_for_runtime_startup(repository, record)


def submit_mindmap_or_queue(
    app_state: Any,
    repository: SqliteTaskRepository,
    task_id: str,
    *,
    force: bool = False,
) -> None:
    try:
        _raise_runtime_startup_error(app_state)
    except HTTPException as exc:
        record = repository.get_task(task_id)
        if record is not None and record.result is not None:
            failed_result = record.result.model_copy(
                update={
                    "mindmap_status": "failed",
                    "mindmap_error_message": str(exc.detail),
                    "mindmap_updated_at": datetime.now(timezone.utc),
                }
            )
            repository.save_result(task_id, failed_result)
        repository.append_event(
            task_id=task_id,
            stage="mindmap_failed",
            progress=100,
            message="运行环境初始化失败，思维导图无法开始",
            payload={"force": force, "runtime_startup": True, "error": str(exc.detail)},
        )
        raise

    task_worker = getattr(app_state, "task_worker", None)
    if runtime_worker_ready(app_state) and task_worker is not None:
        task_worker.submit_mindmap(task_id, force=force)
        return

    lock = getattr(app_state, "runtime_startup_lock", None)
    if lock is None:
        raise HTTPException(status_code=503, detail="运行环境尚未初始化，暂时无法生成思维导图。")
    with lock:
        pending_jobs = getattr(app_state, "pending_mindmap_jobs", None)
        if pending_jobs is None:
            app_state.pending_mindmap_jobs = []
            pending_jobs = app_state.pending_mindmap_jobs
        pending_jobs.append({"task_id": task_id, "force": force})
    repository.append_event(
        task_id=task_id,
        stage="mindmap_queued",
        progress=100,
        message="思维导图已保存，等待运行环境初始化完成后自动执行",
        payload={"force": force, "runtime_startup": True},
    )


def mark_runtime_worker_ready(
    app_state: Any,
    environment: dict[str, object],
    message: str = "Runtime worker is ready.",
) -> None:
    lock = getattr(app_state, "runtime_startup_lock", None)
    if lock is None:
        return
    with lock:
        state = getattr(app_state, "runtime_startup", None)
        if not isinstance(state, dict):
            return
        state.update(
            {
                "status": "ready",
                "message": message,
                "ready_at": state.get("ready_at") or _now_iso(),
                "error_at": None,
                "environment": dict(environment),
            }
        )


def start_runtime_startup(
    app_state: Any,
    repository: SqliteTaskRepository,
    current_settings: ServiceSettings,
    recover_incomplete_tasks: RuntimeRecover,
) -> Thread:
    def run() -> None:
        try:
            # Update status so the UI shows what's happening during the
            # potentially slow first environment probe after an update.
            with app_state.runtime_startup_lock:
                app_state.runtime_startup["message"] = "正在检测运行环境（首次可能较慢，需导入 torch 等依赖）..."
            environment = detect_environment(current_settings.runtime_channel)
            if not environment.get("runtimeReady"):
                raise RuntimeError(str(environment.get("runtimeError") or "Runtime environment is not ready."))

            with app_state.runtime_startup_lock:
                app_state.runtime_startup["message"] = "正在构建任务处理器..."
            task_worker = build_worker(
                repository=repository,
                current_settings=current_settings,
                environment_info=environment,
            )
            if runtime_startup_shutdown_requested(app_state):
                task_worker.shutdown(wait=True, timeout=5)
                logger.info("runtime startup skipped because application is shutting down")
                return
            replace_task_worker(app_state, task_worker)
            mark_runtime_worker_recovering(app_state, environment)
            if runtime_startup_shutdown_requested(app_state):
                task_worker.shutdown(wait=True, timeout=5)
                logger.info("runtime recovery skipped because application is shutting down")
                return
            recovered_count = recover_incomplete_tasks(repository, task_worker)
            _submit_pending_mindmap_jobs(app_state, task_worker)
            mark_runtime_worker_ready(
                app_state,
                environment,
                message=f"Runtime worker is ready. Recovered {recovered_count} queued task(s).",
            )
            logger.info("runtime startup complete recovered_tasks=%d", recovered_count)
        except Exception as exc:
            logger.exception("runtime startup failed error=%s", exc)
            with app_state.runtime_startup_lock:
                app_state.runtime_startup.update(
                    {
                        "status": "error",
                        "message": str(exc),
                        "ready_at": None,
                        "error_at": _now_iso(),
                    }
                )

    thread = Thread(target=run, name="runtime-startup", daemon=True)
    app_state.runtime_startup_thread = thread
    thread.start()
    return thread


def _submit_pending_mindmap_jobs(app_state: Any, task_worker: Any) -> None:
    with app_state.runtime_startup_lock:
        pending_jobs = list(getattr(app_state, "pending_mindmap_jobs", []))
        app_state.pending_mindmap_jobs = []
    for job in pending_jobs:
        task_worker.submit_mindmap(str(job["task_id"]), force=bool(job.get("force")))
