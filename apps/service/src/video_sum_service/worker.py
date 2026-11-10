import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock, Thread, current_thread
from typing import Literal

from video_sum_core.models.tasks import (
    AgentIdentity,
    InputType,
    TaskInput,
    TaskResult,
    TaskStatus,
    merge_llm_usage_records,
)
from video_sum_core.pipeline.base import (
    TRANSCRIPTION_FILENAME,
    PipelineContext,
    PipelineRunner,
    PipelineTranscription,
)
from video_sum_core.pipeline.model_steps import (
    VISUAL_STEPS,
    AgentContentRejected,
    AgentSignal,
    AgentWorkRequired,
    PendingItem,
    agent_session,
    record_routes,
)
from video_sum_infra.config import ServiceSettings, normalize_visual_note_mode

from video_sum_service import agent_work
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import TaskRecord
from video_sum_service.video_assets import resolve_video_page

logger = logging.getLogger("video_sum_service.worker")

AfterCompletedPolicy = Literal["async", "inline"]
"""``async``: mindmap/visual evidence are queued after completion (default mode).
``inline``: the mindmap runs inside the summary unit before completion (Agent mode)."""

# Exceptions that must cross every broad ``except`` inside the summary unit and
# reach ``_run_llm_job`` untouched.
SUMMARY_UNIT_SIGNALS: tuple[type[BaseException], ...] = (AgentSignal,)


@dataclass(frozen=True)
class _MindmapJob:
    task_id: str
    force: bool


@dataclass(frozen=True)
class _VisualEvidenceJob:
    task_id: str
    force: bool
    mode: str | None = None


@dataclass(frozen=True)
class _LLMJob:
    task_id: str


@dataclass
class _SummaryBatchState:
    completed_tasks: int = 0
    total_tasks: int = 0
    total_tokens: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    started_monotonic: float | None = None
    completed_monotonic: float | None = None
    items: dict[str, dict[str, object]] = field(default_factory=dict)


class _TaskQueueState:
    def __init__(self, name: str, concurrency: int) -> None:
        self.name = name
        self.concurrency = max(1, int(concurrency))
        self.pending: deque[object] = deque()
        self.pending_ids: set[str] = set()
        self.running_ids: set[str] = set()

    def job_id_for(self, job: object) -> str:
        if isinstance(job, TaskRecord):
            return job.task_id
        if isinstance(job, _MindmapJob):
            return job.task_id
        if isinstance(job, _VisualEvidenceJob):
            return job.task_id
        if isinstance(job, _LLMJob):
            return job.task_id
        raise TypeError(f"Unsupported job type: {type(job)!r}")

    def clear_pending(self) -> int:
        count = len(self.pending)
        self.pending.clear()
        self.pending_ids.clear()
        return count


class TaskWorker:
    """Background worker with independent transcription, LLM, and artifact queues."""

    def __init__(
        self,
        repository: SqliteTaskRepository,
        pipeline_runner: PipelineRunner,
        *,
        auto_generate_mindmap: bool = False,
        auto_generate_visual_evidence: bool = False,
        knowledge_index_auto_rebuild: str = "disabled",
        knowledge_index_settings: ServiceSettings | None = None,
        task_concurrency: int | None = None,
        transcription_concurrency: int | None = None,
        llm_concurrency: int | None = None,
        mindmap_concurrency: int = 1,
    ) -> None:
        self._repository = repository
        self._pipeline_runner = pipeline_runner
        self._auto_generate_mindmap = auto_generate_mindmap
        self._auto_generate_visual_evidence = auto_generate_visual_evidence
        self._knowledge_index_auto_rebuild = str(knowledge_index_auto_rebuild or "disabled")
        self._knowledge_index_settings = knowledge_index_settings or ServiceSettings(_env_file=None)
        legacy_concurrency = max(1, int(task_concurrency or 1))
        self._transcription_state = _TaskQueueState(
            "transcription",
            transcription_concurrency if transcription_concurrency is not None else legacy_concurrency,
        )
        # Keep the old private name for integrations/tests that used it before
        # the queue split. It now points at the transcription queue.
        self._task_state = self._transcription_state
        self._llm_state = _TaskQueueState(
            "llm",
            llm_concurrency if llm_concurrency is not None else legacy_concurrency,
        )
        self._mindmap_state = _TaskQueueState("mindmap", mindmap_concurrency)
        self._visual_state = _TaskQueueState("visual", 1)
        self._staged_transcriptions: dict[str, PipelineTranscription] = {}
        # task_id -> highest progress recorded; set only while an Agent task's LLM job runs.
        self._progress_floor: dict[str, int] = {}
        self._active_runner: object = None
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._summary_batch: _SummaryBatchState | None = None
        self._accept_new_work = True
        self._shutdown_requested = False
        self._job_threads: set[Thread] = set()
        self._dispatch_threads = [
            Thread(target=self._dispatch_loop, args=(self._transcription_state, self._run_transcription_job), daemon=True),
            Thread(target=self._dispatch_loop, args=(self._llm_state, self._run_llm_job), daemon=True),
            Thread(target=self._dispatch_loop, args=(self._mindmap_state, self._run_mindmap_job), daemon=True),
            Thread(target=self._dispatch_loop, args=(self._visual_state, self._run_visual_evidence_job), daemon=True),
        ]
        for thread in self._dispatch_threads:
            thread.start()

    def submit(self, task: TaskRecord) -> Literal["accepted", "already_queued", "rejected"]:
        logger.info(
            "queue summary task task_id=%s video_id=%s input_type=%s source=%s",
            task.task_id,
            task.video_id,
            task.task_input.input_type.value,
            task.task_input.source,
        )
        result = self._enqueue_summary(task)
        return result

    def recover(self, task: TaskRecord) -> Literal["accepted", "already_queued", "rejected"]:
        """Re-queue a task found incomplete at startup.

        A ``running`` task whose transcription was already persisted goes
        straight to the LLM queue, so a restart between the transcription and
        summary stages does not download or transcribe again. Everything else
        restarts from the transcription queue via :meth:`submit`.
        """

        if (
            task.status == TaskStatus.RUNNING
            and self._load_persisted_transcription(task.task_id) is not None
        ):
            logger.info(
                "recover task into LLM queue from persisted transcription task_id=%s", task.task_id
            )
            result = self._enqueue_summary(task, resume_llm=True)
            if result == "accepted":
                self._repository.append_event(
                    task_id=task.task_id,
                    stage="llm_queued",
                    progress=87,
                    message="已恢复转写结果，进入 LLM 队列",
                    payload={
                        "recovered": True,
                        "transcription_concurrency": self._transcription_state.concurrency,
                        "llm_concurrency": self._llm_state.concurrency,
                    },
                )
            return result
        return self.submit(task)

    def submit_mindmap(self, task_id: str, *, force: bool = False) -> None:
        logger.info("queue mindmap generation task_id=%s force=%s", task_id, force)
        enqueued = self._enqueue(self._mindmap_state, _MindmapJob(task_id=task_id, force=force))
        if enqueued:
            self._repository.append_event(
                task_id=task_id,
                stage="mindmap_queued",
                progress=100,
                message="思维导图已进入后台队列",
                payload={"force": force},
            )

    def submit_visual_evidence(self, task_id: str, *, force: bool = False, mode: str | None = None) -> None:
        logger.info("queue visual evidence generation task_id=%s force=%s mode=%s", task_id, force, mode)
        enqueued = self._enqueue(self._visual_state, _VisualEvidenceJob(task_id=task_id, force=force, mode=mode))
        if enqueued:
            self._repository.append_event(
                task_id=task_id,
                stage="visual_queued",
                progress=100,
                message="图文笔记已进入后台队列",
                payload={"force": force, "mode": normalize_visual_note_mode(mode, default="frame_insert") if mode else mode},
            )

    def close_for_new_work(self) -> None:
        with self._condition:
            self._accept_new_work = False
            self._condition.notify_all()

    def shutdown(self, wait: bool = False, timeout: float | None = None, *, cancel_pending: bool = True) -> None:
        with self._condition:
            self._accept_new_work = False
            self._shutdown_requested = True
            if cancel_pending:
                dropped = (
                    self._task_state.clear_pending()
                    + self._llm_state.clear_pending()
                    + self._mindmap_state.clear_pending()
                    + self._visual_state.clear_pending()
                )
                if dropped:
                    logger.info("dropped pending jobs during worker shutdown count=%d", dropped)
            self._condition.notify_all()
        # C2/C7: cancel active pipeline runner
        active_runner = self._active_runner
        if active_runner is not None and hasattr(active_runner, 'cancel'):
            active_runner.cancel()
        if wait:
            deadline = None if timeout is None else datetime.now(timezone.utc).timestamp() + timeout
            for thread in self._dispatch_threads:
                join_timeout = None if deadline is None else max(0, deadline - datetime.now(timezone.utc).timestamp())
                thread.join(join_timeout)
            while True:
                with self._condition:
                    job_threads = [thread for thread in self._job_threads if thread.is_alive()]
                    self._job_threads = set(job_threads)
                if not job_threads:
                    break
                join_timeout = None if deadline is None else max(0, deadline - datetime.now(timezone.utc).timestamp())
                if join_timeout == 0:
                    break
                job_threads[0].join(join_timeout)
            # W7: extra timeout for orphan subprocesses
            orphan_deadline = time.monotonic() + 10
            while any(t.is_alive() for t in self._job_threads) and time.monotonic() < orphan_deadline:
                time.sleep(0.5)
            # force cancel remaining active runners
            active_runner = self._active_runner
            if active_runner is not None and hasattr(active_runner, 'cancel'):
                active_runner.cancel()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "accept_new_work": self._accept_new_work,
                "shutdown_requested": self._shutdown_requested,
                "dispatch_threads": sum(1 for thread in self._dispatch_threads if thread.is_alive()),
                "job_threads": sum(1 for thread in self._job_threads if thread.is_alive()),
                "queues": {
                    self._task_state.name: self._queue_snapshot(self._task_state),
                    # ``task`` remains an alias for clients that displayed
                    # the pre-split queue name.
                    "task": self._queue_snapshot(self._task_state),
                    self._llm_state.name: self._queue_snapshot(self._llm_state),
                    self._mindmap_state.name: self._queue_snapshot(self._mindmap_state),
                    self._visual_state.name: self._queue_snapshot(self._visual_state),
                },
                "batch": self._summary_batch_snapshot_locked(),
            }

    def _summary_batch_snapshot_locked(self) -> dict[str, object]:
        batch = self._summary_batch
        if batch is None:
            return {
                "completed_tasks": 0,
                "total_tasks": 0,
                "pending_tasks": 0,
                "running_tasks": 0,
                "concurrency": self._transcription_state.concurrency,
                "transcription_concurrency": self._transcription_state.concurrency,
                "llm_concurrency": self._llm_state.concurrency,
                "mode": "parallel"
                if max(self._transcription_state.concurrency, self._llm_state.concurrency) > 1
                else "serial",
                "total_tokens": 0,
                "runtime_seconds": 0.0,
                "started_at": None,
                "completed_at": None,
                "state": "idle",
                "visible": False,
                "items": [],
            }

        pending_tasks = len(self._task_state.pending) + len(self._llm_state.pending)
        running_tasks = len(self._task_state.running_ids) + len(self._llm_state.running_ids)
        is_complete = (
            batch.total_tasks > 0
            and pending_tasks == 0
            and running_tasks == 0
            and batch.completed_tasks >= batch.total_tasks
        )
        state = "completed" if is_complete else "active"
        if batch.started_monotonic is None:
            runtime_seconds = 0.0
        else:
            end_monotonic = batch.completed_monotonic if is_complete else time.monotonic()
            runtime_seconds = max(0.0, end_monotonic - batch.started_monotonic)

        return {
            "completed_tasks": batch.completed_tasks,
            "total_tasks": batch.total_tasks,
            "pending_tasks": pending_tasks,
            "running_tasks": running_tasks,
            "concurrency": self._transcription_state.concurrency,
            "transcription_concurrency": self._transcription_state.concurrency,
            "llm_concurrency": self._llm_state.concurrency,
            "transcription_pending_tasks": len(self._transcription_state.pending),
            "transcription_running_tasks": len(self._transcription_state.running_ids),
            "llm_pending_tasks": len(self._llm_state.pending),
            "llm_running_tasks": len(self._llm_state.running_ids),
            "mode": "parallel"
            if max(self._transcription_state.concurrency, self._llm_state.concurrency) > 1
            else "serial",
            "total_tokens": batch.total_tokens,
            "runtime_seconds": runtime_seconds,
            "started_at": batch.started_at.isoformat() if batch.started_at else None,
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
            "state": state,
            "visible": True,
            "items": [dict(item) for item in batch.items.values()],
        }

    def _queue_snapshot(self, state: _TaskQueueState) -> dict[str, object]:
        return {
            "concurrency": state.concurrency,
            "pending": len(state.pending),
            "running": len(state.running_ids),
        }

    def _enqueue_summary(
        self,
        task: TaskRecord,
        *,
        resume_llm: bool = False,
    ) -> Literal["accepted", "already_queued", "rejected"]:
        job_id = self._task_state.job_id_for(task)
        with self._condition:
            if not self._accept_new_work:
                logger.info("reject new summary job because worker is closed task_id=%s", job_id)
                return "rejected"
            if (
                job_id in self._task_state.pending_ids
                or job_id in self._task_state.running_ids
                or job_id in self._llm_state.pending_ids
                or job_id in self._llm_state.running_ids
            ):
                logger.info("skip duplicate summary job task_id=%s", job_id)
                return "already_queued"

            if self._summary_batch is None or (
                not self._task_state.pending
                and not self._task_state.running_ids
                and not self._llm_state.pending
                and not self._llm_state.running_ids
            ):
                now = datetime.now(timezone.utc)
                self._summary_batch = _SummaryBatchState(
                    started_at=now,
                    started_monotonic=time.monotonic(),
                )

            if resume_llm:
                # Restart recovery: the transcription is already on disk, so the
                # task skips the transcription queue (and any schedule).
                self._llm_state.pending.append(_LLMJob(task_id=job_id))
                self._llm_state.pending_ids.add(job_id)
            else:
                self._repository.update_status(task.task_id, TaskStatus.QUEUED)
                self._repository.append_event(
                    task_id=task.task_id,
                    stage="queued",
                    progress=0,
                    message=(
                        f"定时任务：将在 {task.task_input.options.scheduled_at.isoformat()} 开始；"
                        "需保持服务运行"
                        if task.task_input.options.scheduled_at
                        else "任务已进入后台队列"
                    ),
                )
                self._task_state.pending.append(task)
                self._task_state.pending_ids.add(job_id)
            self._summary_batch.total_tasks += 1
            self._summary_batch.items[job_id] = {
                "task_id": job_id,
                "title": task.page_title or task.task_input.title or job_id,
                "status": TaskStatus.RUNNING.value if resume_llm else TaskStatus.QUEUED.value,
                "progress": 87 if resume_llm else 0,
                "message": "已恢复转写结果，等待 LLM 并发槽位"
                if resume_llm
                else "任务已进入后台队列",
            }
            self._condition.notify_all()
            return "accepted"

    def _enqueue_llm_locked(self, task_id: str) -> bool:
        """Move a successfully transcribed task into the LLM queue.

        The caller holds ``self._condition``. This internal handoff is allowed
        after ``close_for_new_work`` so an already accepted task can finish;
        shutdown still prevents new LLM work from being started.
        """

        if self._shutdown_requested:
            return False
        if task_id in self._llm_state.pending_ids or task_id in self._llm_state.running_ids:
            return False
        self._llm_state.pending.append(_LLMJob(task_id=task_id))
        self._llm_state.pending_ids.add(task_id)
        return True

    def _enqueue(self, state: _TaskQueueState, job: object) -> bool:
        job_id = state.job_id_for(job)
        with self._condition:
            if not self._accept_new_work:
                logger.info("reject new %s job because worker is closed task_id=%s", state.name, job_id)
                return False
            if job_id in state.pending_ids or job_id in state.running_ids:
                logger.info("skip duplicate %s job task_id=%s", state.name, job_id)
                return False
            state.pending.append(job)
            state.pending_ids.add(job_id)
            self._condition.notify_all()
            return True

    def _dispatch_loop(self, state: _TaskQueueState, runner) -> None:
        while True:
            job = self._wait_for_available_job(state)
            if job is None:
                return
            job_id = state.job_id_for(job)
            thread = Thread(target=self._execute_job, args=(state, job, runner), daemon=True, name=f"{state.name}-{job_id}")
            with self._condition:
                self._job_threads.add(thread)
            thread.start()

    def _wait_for_available_job(self, state: _TaskQueueState) -> object | None:
        with self._condition:
            while True:
                if self._shutdown_requested and not state.pending and not state.running_ids:
                    return None
                if self._shutdown_requested and state.pending:
                    dropped = state.clear_pending()
                    if dropped:
                        logger.info("dropped pending %s jobs during worker shutdown count=%d", state.name, dropped)
                    if not state.running_ids:
                        return None
                if not self._accept_new_work and not state.pending and not state.running_ids:
                    return None
                if state.pending and len(state.running_ids) < state.concurrency:
                    now = datetime.now(timezone.utc)
                    job = next((candidate for candidate in state.pending
                                if not isinstance(candidate, TaskRecord)
                                or candidate.task_input.options.scheduled_at is None
                                or candidate.task_input.options.scheduled_at <= now), None)
                    if job is None:
                        delay = min((candidate.task_input.options.scheduled_at - now).total_seconds()
                                    for candidate in state.pending)
                        self._condition.wait(timeout=max(0.01, min(delay, 30)))
                        continue
                    state.pending.remove(job)
                    job_id = state.job_id_for(job)
                    state.pending_ids.discard(job_id)
                    state.running_ids.add(job_id)
                    if state is self._task_state:
                        self._update_summary_item_locked(
                            job_id,
                            status=TaskStatus.RUNNING.value,
                        )
                    return job
                self._condition.wait()

    def _execute_job(self, state: _TaskQueueState, job: object, runner) -> None:
        job_id = state.job_id_for(job)
        handed_off = False
        try:
            runner_result = runner(job)
            handed_off = bool(runner_result) if state is self._task_state else False
        finally:
            task_status, task_message, task_tokens = (
                self._summary_task_outcome(job_id)
                if state is self._task_state or state is self._llm_state
                else (None, None, 0)
            )
            with self._condition:
                was_running = job_id in state.running_ids
                state.running_ids.discard(job_id)
                if state is self._task_state and was_running and handed_off:
                    if self._enqueue_llm_locked(job_id):
                        self._repository.append_event(
                            task_id=job_id,
                            stage="llm_queued",
                            progress=87,
                            message="转写完成，已进入 LLM 队列",
                            payload={
                                "transcription_concurrency": self._transcription_state.concurrency,
                                "llm_concurrency": self._llm_state.concurrency,
                            },
                        )
                        self._update_summary_item_locked(
                            job_id,
                            status=TaskStatus.RUNNING.value,
                            progress=87,
                            message="转写完成，等待 LLM 并发槽位",
                        )
                elif (
                    (state is self._task_state or state is self._llm_state)
                    and was_running
                ):
                    self._complete_summary_task_locked(job_id, task_status, task_message, task_tokens)
                self._job_threads.discard(current_thread())
                self._condition.notify_all()

    def _update_summary_item(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._condition:
            self._update_summary_item_locked(task_id, status=status, progress=progress, message=message)

    def _update_summary_item_locked(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
    ) -> None:
        batch = self._summary_batch
        if batch is None:
            return
        item = batch.items.get(task_id)
        if item is None:
            return
        if status is not None:
            item["status"] = status
        if progress is not None:
            item["progress"] = max(0, min(100, int(progress)))
        if message is not None:
            item["message"] = message

    def _summary_task_outcome(self, task_id: str) -> tuple[str, str, int]:
        try:
            record = self._repository.get_task(task_id)
        except Exception:
            logger.exception("failed to read summary task usage task_id=%s", task_id)
            return TaskStatus.FAILED.value, "任务执行失败", 0
        if record is None or record.result is None:
            if record is None:
                return TaskStatus.FAILED.value, "任务执行失败", 0
            task_tokens = 0
        else:
            try:
                task_tokens = max(0, int(record.result.llm_total_tokens or 0))
            except (TypeError, ValueError):
                task_tokens = 0

        status = record.status.value if record is not None else TaskStatus.FAILED.value
        if status == TaskStatus.COMPLETED.value:
            return status, "任务已完成", task_tokens
        if status == TaskStatus.PAUSED.value:
            return status, "转写已保存，等待续跑", task_tokens
        if status == TaskStatus.CANCELLED.value:
            return status, "任务已取消", task_tokens
        if status == TaskStatus.AGENT_SUMMARIZING.value:
            return status, "等待 Agent 处理", task_tokens
        if status == TaskStatus.FAILED.value:
            return status, record.error_message or "任务执行失败", task_tokens
        return TaskStatus.FAILED.value, "任务执行失败", task_tokens

    def _complete_summary_task_locked(
        self,
        task_id: str,
        task_status: str | None,
        task_message: str | None,
        task_tokens: int,
    ) -> None:
        batch = self._summary_batch
        if batch is None:
            return
        final_status = task_status or TaskStatus.FAILED.value
        final_progress = 100 if final_status == TaskStatus.COMPLETED.value else 0
        self._update_summary_item_locked(
            task_id,
            status=final_status,
            progress=final_progress,
            message=task_message or "任务执行失败",
        )
        batch.completed_tasks += 1
        batch.total_tokens += task_tokens
        if (
            not self._task_state.pending
            and not self._task_state.running_ids
            and not self._llm_state.pending
            and not self._llm_state.running_ids
        ):
            batch.completed_at = datetime.now(timezone.utc)
            batch.completed_monotonic = time.monotonic()

    def _persisted_task_dir(self, task_id: str) -> Path | None:
        if not self._supports_staged_execution():
            return None
        resolve = getattr(self._pipeline_runner, "task_dir", None)
        if not callable(resolve):
            return None
        try:
            task_dir = resolve(task_id)
        except Exception:
            logger.exception("failed to resolve task directory task_id=%s", task_id)
            return None
        return Path(task_dir) if task_dir is not None else None

    def _load_persisted_transcription(self, task_id: str) -> PipelineTranscription | None:
        task_dir = self._persisted_task_dir(task_id)
        if task_dir is None:
            return None
        return PipelineTranscription.load(task_dir)

    def _discard_persisted_transcription(self, task_id: str) -> None:
        task_dir = self._persisted_task_dir(task_id)
        if task_dir is None:
            return
        try:
            (task_dir / TRANSCRIPTION_FILENAME).unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to discard stale transcription task_id=%s", task_id)

    def _discard_agent_state(self, task_id: str) -> None:
        task_dir = self._persisted_task_dir(task_id)
        if task_dir is None:
            return
        try:
            agent_work.clear_agent_state(task_dir)
        except OSError:
            logger.exception("failed to discard stale agent results task_id=%s", task_id)

    def _supports_staged_execution(self) -> bool:
        supports = getattr(self._pipeline_runner, "supports_staged_execution", None)
        if not callable(supports):
            return False
        try:
            return bool(supports())
        except Exception:
            logger.exception("failed to inspect staged pipeline support")
            return False

    def _pipeline_event_handler(self, task_id: str):
        def handle_pipeline_event(event) -> None:
            payload = dict(event.payload or {})
            result_payload = payload.get("result")
            if isinstance(result_payload, dict):
                try:
                    partial_result = TaskResult.model_validate(result_payload)
                except Exception:
                    logger.warning(
                        "skip invalid partial result task_id=%s stage=%s",
                        task_id,
                        event.stage,
                    )
                else:
                    self._repository.save_result(task_id, partial_result)

            self._append_progress_event(
                task_id,
                stage=event.stage,
                progress=event.progress,
                message=event.message,
                payload=payload,
                before_append=lambda: self._update_summary_item(
                    task_id,
                    progress=event.progress,
                    message=event.message,
                ),
            )

        return handle_pipeline_event

    def _mark_task_failed(self, task_id: str, exc: Exception) -> None:
        record = self._repository.get_task(task_id)
        resumable = bool(record and record.result and record.result.transcript_text.strip())
        status = TaskStatus.PAUSED if resumable else TaskStatus.FAILED
        from video_sum_core.errors import LLMAuthenticationError

        message = str(exc)
        if resumable:
            message += (" 请更新 API Key 后，点击「仅重跑 LLM」。" if isinstance(exc, LLMAuthenticationError)
                        else " 转写已保存，可点击「仅重跑 LLM」继续。")
        self._repository.update_error(task_id, "LLM_RESUMABLE" if resumable else "TASK_EXECUTION_FAILED", message)
        self._repository.update_status(task_id, status)
        self._update_summary_item(
            task_id,
            status=status.value,
            progress=0,
            message=message or "任务执行失败",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="paused" if resumable else "failed",
            progress=0,
            message=message or "任务执行失败",
            payload={"error": str(exc)},
        )

    def _summary_policy(self, record: TaskRecord) -> AfterCompletedPolicy:
        """Pick how a task's optional artifacts run: Agent tasks use ``inline``."""

        return "inline" if record.task_input.options.routing.is_agent else "async"

    def _run_summary_unit(
        self,
        record: TaskRecord,
        context: PipelineContext,
        transcription: PipelineTranscription,
        *,
        on_event,
        policy: AfterCompletedPolicy,
    ) -> TaskResult:
        """Run the replayable summary unit: ``run_summary`` (with its inline
        visual note) and, under the ``inline`` policy, the optional mindmap.

        The unit has no side effects outside the pipeline's own task directory
        and the task's result row, so it can be run again from the top; any
        signal it raises reaches the single ``except`` in ``_run_llm_job``.
        """

        result = self._pipeline_runner.run_summary(  # type: ignore[attr-defined]
            context,
            transcription,
            on_event=on_event,
        )
        final_result = result if isinstance(result, TaskResult) else TaskResult()
        if policy == "inline" and self._should_auto_generate_mindmap(final_result):
            self._repository.save_result(record.task_id, final_result)
            mindmap_result = self._generate_mindmap(record, force=False)
            if mindmap_result is not None:
                final_result = mindmap_result
        return final_result

    def _finalize_summary_task(
        self,
        record: TaskRecord,
        result: TaskResult,
        *,
        policy: AfterCompletedPolicy = "async",
    ) -> None:
        task_id = record.task_id
        final_result = result if isinstance(result, TaskResult) else TaskResult()
        self._repository.save_result(task_id, final_result)
        self._repository.update_status(task_id, TaskStatus.COMPLETED)
        self._update_summary_item(
            task_id,
            status=TaskStatus.COMPLETED.value,
            progress=100,
            message="任务已完成",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="completed",
            progress=100,
            message="任务已完成",
            payload={"completed_at": datetime.now(timezone.utc).isoformat()},
        )
        logger.info(
            "task completed task_id=%s key_points=%d timeline=%d transcript_chars=%d",
            task_id,
            len(final_result.key_points),
            len(final_result.timeline),
            len(final_result.transcript_text or ""),
        )
        self._after_completed(record, final_result, policy)

    def _after_completed(
        self, record: TaskRecord, result: TaskResult, policy: AfterCompletedPolicy
    ) -> None:
        """Automatic follow-ups once a task is ``completed``.

        ``async``: mindmap and visual evidence go to their own queues (default mode).
        ``inline``: they already ran inside the summary unit, so nothing is queued.
        Knowledge indexing runs under both policies.
        """

        task_id = record.task_id
        if policy == "async":
            if self._should_auto_generate_mindmap(result):
                self.submit_mindmap(task_id)
            if (
                self._auto_generate_visual_evidence
                and result.visual_note_status
                not in {"generating", "ready", "partial", "unsupported", "failed"}
                and self._can_generate_visual_evidence(record, result)
            ):
                self.submit_visual_evidence(task_id)
        # Indexing embeds the note (a model call); transcript_only tasks make none.
        if (
            self._knowledge_index_auto_rebuild == "on_task_completed"
            and not record.task_input.options.transcript_only
        ):
            self._index_completed_task(record.video_id, task_id)

    def _should_auto_generate_mindmap(self, result: TaskResult) -> bool:
        return self._auto_generate_mindmap and self._can_auto_generate_mindmap(result)

    def _run_transcription_job(self, task: TaskRecord) -> bool:
        """Run only the download/ASR stage and release its slot on success."""

        task_id = task.task_id
        if not self._supports_staged_execution():
            self._run_task(task_id)
            return False

        record = self._repository.get_task(task_id)
        if record is None:
            logger.warning("skip missing task task_id=%s", task_id)
            return False

        logger.info("start transcription stage task_id=%s", task_id)
        self._repository.update_status(task_id, TaskStatus.RUNNING)
        self._update_summary_item(
            task_id,
            status=TaskStatus.RUNNING.value,
            progress=5,
            message="开始转写阶段",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="transcription_started",
            progress=5,
            message="开始转写阶段",
        )

        # A transcription persisted by an earlier run of this task is stale now,
        # and so is everything an Agent task cached on top of it.
        self._discard_persisted_transcription(task_id)
        self._discard_agent_state(task_id)
        self._active_runner = self._pipeline_runner
        try:
            context = PipelineContext(task_id=task_id, task_input=record.task_input)
            if record.result and record.result.transcript_text.strip():
                summary_path = record.result.artifacts.get("summary_path")
                task_dir = Path(summary_path).parent if summary_path else Path(self._knowledge_index_settings.tasks_dir) / task_id
                task_dir.mkdir(parents=True, exist_ok=True)
                transcription = PipelineTranscription(
                    title=record.task_input.title or task_id,
                    transcript=record.result.transcript_text,
                    segments=record.result.segments,
                    task_dir=task_dir,
                    source_path=Path(record.task_input.source) if record.task_input.input_type == InputType.VIDEO_FILE else None,
                )
                if record.task_input.input_type == InputType.TRANSCRIPT_TEXT:
                    try:
                        payload = json.loads(record.task_input.source)
                        if isinstance(payload, dict):
                            transcription.source_kind = payload.get("source_kind")
                    except ValueError:
                        pass
                self._repository.append_event(task_id=task_id, stage="transcript_reused", progress=86,
                                              message="已复用保存的转写，跳过下载和 ASR")
            else:
                transcription = self._pipeline_runner.run_transcription(  # type: ignore[attr-defined]
                    context,
                    on_event=self._pipeline_event_handler(task_id),
                )
            if not isinstance(transcription, PipelineTranscription):
                raise TypeError("staged transcription must return PipelineTranscription")
            if record.task_input.options.transcript_only:
                result = self._pipeline_runner.build_transcript_result(transcription)
                self._finalize_summary_task(record, result)
                return False
            # Persist the stage boundary even if a runner did not emit a snapshot.
            if not record.result or not record.result.transcript_text.strip():
                self._repository.save_result(task_id, TaskResult(
                    transcript_text=transcription.transcript, segments=transcription.segments,
                    artifacts={"transcript_path": str(transcription.task_dir / "transcript.txt"),
                               "summary_path": str(transcription.task_dir / "summary.json")},
                ))
            try:
                transcription.save()
            except OSError:
                logger.exception("failed to persist staged transcription task_id=%s", task_id)
            with self._condition:
                self._staged_transcriptions[task_id] = transcription
            return True
        except Exception as exc:
            logger.exception("transcription stage failed task_id=%s error=%s", task_id, exc)
            self._mark_task_failed(task_id, exc)
            return False
        finally:
            self._active_runner = None

    def _run_llm_job(self, job: _LLMJob) -> None:
        """Run summary/knowledge/visual LLM work for a transcribed task."""

        task_id = job.task_id
        record = self._repository.get_task(task_id)
        if record is None:
            logger.warning("skip missing task for LLM stage task_id=%s", task_id)
            return
        with self._condition:
            transcription = self._staged_transcriptions.pop(task_id, None)
        if transcription is None:
            transcription = self._load_persisted_transcription(task_id)
        if transcription is None:
            exc = RuntimeError("转写阶段结果不存在，无法进入 LLM 阶段")
            logger.error("missing staged transcription task_id=%s", task_id)
            self._mark_task_failed(task_id, exc)
            return

        agent_task_dir = self._persisted_task_dir(task_id) if record.task_input.options.routing.is_agent else None
        if agent_task_dir is not None:
            # Replays re-emit the progress of finished steps: keep it monotonic.
            self._start_progress_floor(task_id)
        self._update_summary_item(
            task_id,
            status=TaskStatus.RUNNING.value,
            progress=88,
            message="正在进入 LLM 摘要阶段",
        )
        self._append_progress_event(
            task_id,
            stage="llm_started",
            progress=88,
            message="正在进入 LLM 摘要阶段",
            payload={"llm_concurrency": self._llm_state.concurrency},
        )
        self._active_runner = self._pipeline_runner
        policy = self._summary_policy(record)
        try:
            context = PipelineContext(task_id=task_id, task_input=record.task_input)
            event_handler = self._pipeline_event_handler(task_id)
            with self._agent_session(record, agent_task_dir):
                self._pipeline_runner.preflight(context, on_event=event_handler)
                result = self._run_summary_unit(
                    record,
                    context,
                    transcription,
                    on_event=event_handler,
                    policy=policy,
                )
            if agent_task_dir is not None:
                identities = agent_work.agent_identities(agent_task_dir)
                result = result.model_copy(
                    update={
                        "agent_identities": {
                            stage: AgentIdentity(**value) for stage, value in identities.items()
                        }
                    }
                )
            self._finalize_summary_task(record, result, policy=policy)
        # Single catch point for everything the summary unit raises. Agent
        # signals park the task; they never reach ``_mark_task_failed``.
        except AgentWorkRequired as signal:
            self._wait_for_agent(
                record, agent_task_dir or self._persisted_task_dir(task_id), signal.items
            )
        except AgentContentRejected as signal:
            self._wait_for_agent(
                record,
                agent_task_dir or self._persisted_task_dir(task_id),
                [signal.item] if signal.item is not None else [],
                rejected=(signal.item_id, signal.reason),
            )
        except Exception as exc:
            logger.exception("LLM stage failed task_id=%s error=%s", task_id, exc)
            self._mark_task_failed(task_id, exc)
        finally:
            self._active_runner = None
            self._stop_progress_floor(task_id)

    # -- Agent tasks -------------------------------------------------------
    def resume_agent_task(self, task_id: str) -> bool:
        """Replay an Agent task whose results were submitted: ``agent_summarizing -> running``
        and back into the LLM queue. ``False`` when the task was not waiting (lost race)."""

        record = self._repository.transition(
            task_id, TaskStatus.AGENT_SUMMARIZING, TaskStatus.RUNNING
        )
        if record is None:
            return False
        result = self._enqueue_summary(record, resume_llm=True)
        if result == "rejected":
            logger.warning(
                "replay not queued (worker closed) task_id=%s; recovered on next start", task_id
            )
        return True

    def cancel_agent_task(self, task_id: str) -> bool:
        """User cancel of a task waiting for the Agent: ``agent_summarizing -> cancelled``.
        ``False`` when the task was not waiting."""

        record = self._repository.transition(
            task_id, TaskStatus.AGENT_SUMMARIZING, TaskStatus.CANCELLED
        )
        if record is None:
            return False
        task_dir = self._persisted_task_dir(task_id)
        if task_dir is not None:
            agent_work.release_lease(task_dir)
        self._update_summary_item(task_id, status=TaskStatus.CANCELLED.value, message="任务已取消")
        progress = 0
        try:
            progress = max(
                (int(event.progress) for event in self._repository.list_events(task_id)), default=0
            )
        except Exception:
            logger.exception("failed to read task progress task_id=%s", task_id)
        self._repository.append_event(
            task_id=task_id, stage="cancelled", progress=progress, message="任务已取消"
        )
        return True

    def agent_task_dir(self, task_id: str) -> Path | None:
        """Task directory holding ``agent_work.json`` / ``agent_results/``.

        None without staged execution.
        """

        return self._persisted_task_dir(task_id)

    @contextmanager
    def _agent_session(self, record: TaskRecord, task_dir: Path | None) -> Iterator[None]:
        if task_dir is None:
            yield
            return
        routing = record.task_input.options.routing
        if agent_work.builtin_override(task_dir):
            # "改用内置模型": every step left takes the default chain; cached results still replay.
            routing = routing.model_copy(update={"agent_steps": []})
        with agent_session(routing, agent_work.AgentResultFiles(task_dir)):
            yield

    def _wait_for_agent(
        self,
        record: TaskRecord,
        task_dir: Path | None,
        items: list[PendingItem],
        *,
        rejected: tuple[str, str] | None = None,
    ) -> None:
        """Save this round's pending items, park the task in ``agent_summarizing``
        and give the LLM slot back (the job simply returns)."""

        task_id = record.task_id
        if task_dir is None:
            self._mark_task_failed(task_id, RuntimeError("Agent 任务缺少任务目录，无法保存待办项"))
            return
        document = agent_work.save_pending_items(
            task_dir, items, rejected=None if rejected is None else {rejected[0]: rejected[1]}
        )
        # An inline mindmap aborted by the signal is not "generating" any more.
        self._repository.update_result(
            task_id,
            lambda existing: existing.model_copy(update={"mindmap_status": "idle"})
            if existing.mindmap_status == "generating"
            else existing,
        )
        if (
            self._repository.transition(task_id, TaskStatus.RUNNING, TaskStatus.AGENT_SUMMARIZING)
            is None
        ):
            logger.warning("task left running while waiting for agent task_id=%s", task_id)
            return
        item_ids = [item.item_id for item in items]
        progress = self._progress_floor_value(task_id)
        if rejected is not None:
            self._repository.append_event(
                task_id=task_id,
                stage="agent_work_rejected",
                progress=progress,
                message=f"Agent 提交的 {rejected[0]} 无法使用，等待重新提交",
                payload={"item_id": rejected[0], "reason": rejected[1], "round": document["round"]},
            )
        self._repository.append_event(
            task_id=task_id,
            stage="agent_work_required",
            progress=progress,
            message=f"等待 Agent 处理 {len(item_ids)} 项",
            payload={"items": item_ids, "round": document["round"]},
        )
        self._update_summary_item(
            task_id,
            status=TaskStatus.AGENT_SUMMARIZING.value,
            message="等待 Agent 处理",
        )

    # -- monotonic progress for replays -------------------------------------
    def _start_progress_floor(self, task_id: str) -> None:
        latest = 0
        try:
            latest = max(
                (int(event.progress) for event in self._repository.list_events(task_id)), default=0
            )
        except Exception:
            logger.exception("failed to read task progress task_id=%s", task_id)
        with self._condition:
            self._progress_floor[task_id] = latest

    def _stop_progress_floor(self, task_id: str) -> None:
        with self._condition:
            self._progress_floor.pop(task_id, None)

    def _progress_floor_value(self, task_id: str) -> int:
        with self._condition:
            return int(self._progress_floor.get(task_id, 0))

    def _append_progress_event(
        self,
        task_id: str,
        *,
        stage: str,
        progress: int,
        message: str,
        payload: dict[str, object] | None = None,
        before_append: Callable[[], None] | None = None,
    ) -> bool:
        """``append_event`` that, while a floor is active, drops events not above it.

        ``before_append`` runs only for kept events (e.g. the queue snapshot update)."""

        with self._condition:
            floor = self._progress_floor.get(task_id)
            if floor is not None:
                if int(progress) <= floor:
                    return False
                self._progress_floor[task_id] = int(progress)
        if before_append is not None:
            before_append()
        self._repository.append_event(
            task_id=task_id, stage=stage, progress=progress, message=message, payload=payload
        )
        return True

    def _run_task_job(self, task: TaskRecord) -> None:
        self._run_task(task.task_id)

    def _run_task(self, task_id: str) -> None:
        record = self._repository.get_task(task_id)
        if record is None:
            logger.warning("skip missing task task_id=%s", task_id)
            return

        logger.info(
            "start task execution task_id=%s video_id=%s title=%s",
            task_id,
            record.video_id,
            record.task_input.title,
        )

        self._repository.update_status(task_id, TaskStatus.RUNNING)
        self._update_summary_item(
            task_id,
            status=TaskStatus.RUNNING.value,
            progress=5,
            message="任务开始执行",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="running",
            progress=5,
            message="任务开始执行",
        )

        self._active_runner = self._pipeline_runner
        try:
            context = PipelineContext(task_id=task_id, task_input=record.task_input)
            event_handler = self._pipeline_event_handler(task_id)
            self._pipeline_runner.preflight(context, on_event=event_handler)
            _, result = self._pipeline_runner.run(context, on_event=event_handler)
            self._finalize_summary_task(record, result)
        except Exception as exc:
            logger.exception("task failed task_id=%s error=%s", task_id, exc)
            self._mark_task_failed(task_id, exc)
        finally:
            self._active_runner = None

    def _can_auto_generate_mindmap(self, result: TaskResult) -> bool:
        return bool(
            result.knowledge_note_markdown.strip()
            and result.artifacts.get("summary_path")
        )

    def _can_generate_visual_evidence(self, record: TaskRecord, result: TaskResult) -> bool:
        if result.visual_note_status in {"generating", "ready"}:
            return False
        if not hasattr(self._pipeline_runner, "build_and_export_visual_evidence"):
            return False
        task_mode = getattr(record.task_input.options, "visual_note_mode", None)
        if task_mode is not None:
            if normalize_visual_note_mode(task_mode) == "text":
                return False
            return bool(result.artifacts.get("summary_path") and result.timeline and result.knowledge_note_markdown.strip())
        settings = getattr(self._pipeline_runner, "_settings", None)
        mode = normalize_visual_note_mode(getattr(settings, "visual_note_mode", "frame_insert"), default="frame_insert")
        if mode == "text":
            return False
        return bool(result.artifacts.get("summary_path") and result.timeline and result.knowledge_note_markdown.strip())

    def _index_completed_task(self, video_id: str | None, task_id: str) -> None:
        if not video_id:
            return
        self._repository.append_event(
            task_id=task_id,
            stage="knowledge_index_refreshing",
            progress=100,
            message="正在刷新知识库索引",
        )
        try:
            from video_sum_service.knowledge.index_service import KnowledgeIndexService

            index_service = KnowledgeIndexService(self._repository, self._knowledge_index_settings)
            indexed = index_service.index_video(video_id)
            self._repository.append_event(
                task_id=task_id,
                stage="knowledge_index_completed",
                progress=100,
                message="知识库索引已刷新",
                payload={"indexed": indexed},
            )
            logger.info("auto refreshed knowledge index video_id=%s indexed=%s", video_id, indexed)
        except Exception as exc:
            self._repository.append_event(
                task_id=task_id,
                stage="knowledge_index_failed",
                progress=100,
                message="知识库索引刷新失败",
                payload={"error": str(exc)},
            )
            logger.warning("auto knowledge index refresh failed video_id=%s error=%s", video_id, exc)

    def _run_mindmap_job(self, job: _MindmapJob) -> None:
        self._run_mindmap(job.task_id, job.force)

    def _run_visual_evidence_job(self, job: _VisualEvidenceJob) -> None:
        self._run_visual_evidence(job.task_id, job.force, job.mode)

    def _run_visual_evidence(self, task_id: str, force: bool, mode: str | None = None) -> None:
        record = self._repository.get_task(task_id)
        if record is None or record.result is None:
            logger.warning("skip missing task result for visual evidence task_id=%s", task_id)
            return
        if record.status != TaskStatus.COMPLETED:
            logger.warning("skip non-completed task visual evidence task_id=%s status=%s", task_id, record.status.value)
            return
        if not hasattr(self._pipeline_runner, "build_and_export_visual_evidence"):
            logger.warning("pipeline runner does not support visual evidence task_id=%s", task_id)
            return
        if record.result.visual_note_status == "ready" and not force:
            return

        normalized_mode = normalize_visual_note_mode(
            mode
            or getattr(record.task_input.options, "visual_note_mode", None)
            or getattr(getattr(self._pipeline_runner, "_settings", None), "visual_note_mode", "frame_insert"),
            default="frame_insert",
        )
        if normalized_mode == "text":
            normalized_mode = "frame_insert"
        started_record = self._repository.update_result(
            task_id,
            lambda existing: existing.model_copy(
                update={
                    "visual_note_status": "generating",
                    "visual_note_error_message": None,
                }
            ),
        )
        if started_record is None or started_record.result is None:
            return
        current_result = started_record.result
        self._repository.append_event(
            task_id=task_id,
            stage="visual_generating",
            progress=20,
            message="正在准备图文笔记",
            payload={"force": force, "mode": normalized_mode},
        )

        try:
            def handle_visual_event(event) -> None:
                payload = dict(getattr(event, "payload", None) or {})
                self._repository.append_event(
                    task_id=task_id,
                    stage=event.stage,
                    progress=event.progress,
                    message=event.message,
                    payload=payload,
                )

            with record_routes() as route_log:
                context, note_path, context_path = (
                    self._pipeline_runner.build_and_export_visual_evidence(  # type: ignore[attr-defined]
                        task_id=task_id,
                        task_input=self._resolve_visual_task_input(record),
                        title=record.page_title or record.task_input.title or "图文笔记",
                        result=current_result,
                        mode=normalized_mode,
                        force=force,
                        on_event=handle_visual_event,
                    )
                )
            visual_sources = route_log.model_sources(VISUAL_STEPS)
            status_value = str(context.get("status") or "partial")
            error_message = "\n".join(str(item) for item in context.get("warnings", []) if str(item).strip()) or None
            updated_record = self._repository.update_result(
                task_id,
                lambda existing: merge_llm_usage_records(
                    existing,
                    [item for item in context.get("llm_usage_breakdown", []) if isinstance(item, dict)],
                ).model_copy(
                    update={
                        "artifacts": {
                            **existing.artifacts,
                            "visual_enhanced_note_path": str(Path(note_path)),
                            "visual_note_path": str(Path(note_path).parent / "visual_note.md"),
                            "visual_context_path": str(Path(context_path)),
                            "visual_frame_index_path": str(Path(note_path).parent / "frame_index.json"),
                            "visual_insert_plan_path": str(Path(note_path).parent / "visual_insert_plan.json"),
                        },
                        "visual_note_status": status_value,
                        "visual_note_error_message": error_message if status_value in {"failed", "partial", "unsupported"} else None,
                        "visual_note_artifact_path": str(Path(note_path).parent / "visual_note.md"),
                        "visual_enhanced_note_artifact_path": str(Path(note_path)),
                        "visual_note_updated_at": datetime.now(timezone.utc),
                        "visual_note_mode": str(context.get("mode") or normalized_mode),
                        "visual_frame_count": int(context.get("frame_count") or 0),
                        "visual_insert_count": int(context.get("insert_count") or 0),
                        "model_sources": {**existing.model_sources, **visual_sources},
                    }
                ),
            )
            if updated_record is None or updated_record.result is None:
                return
            final_result = updated_record.result
            if status_value == "ready":
                event_stage = "visual_completed"
                event_message = "图文笔记生成完成"
            elif status_value == "unsupported":
                event_stage = "visual_failed"
                event_message = "当前任务无法生成图文笔记"
            else:
                event_stage = "visual_partial"
                event_message = "图文笔记已降级完成"
            self._repository.append_event(
                task_id=task_id,
                stage=event_stage,
                progress=100,
                message=event_message,
                payload={
                    "status": status_value,
                    "mode": context.get("mode") or normalized_mode,
                    "frame_count": final_result.visual_frame_count,
                    "insert_count": context.get("insert_count", 0),
                    "warnings": context.get("warnings", []),
                },
            )
        except Exception as exc:
            logger.exception("visual evidence generation failed task_id=%s error=%s", task_id, exc)
            failed_record = self._repository.update_result(
                task_id,
                lambda existing: existing.model_copy(
                    update={
                        "visual_note_status": "failed",
                        "visual_note_error_message": str(exc),
                        "visual_note_updated_at": datetime.now(timezone.utc),
                    }
                ),
            )
            if failed_record is None:
                return
            self._repository.append_event(
                task_id=task_id,
                stage="visual_failed",
                progress=100,
                message="图文笔记生成失败",
                payload={"error": str(exc), "force": force, "mode": normalized_mode},
            )

    def _resolve_visual_task_input(self, record: TaskRecord) -> TaskInput:
        if record.task_input.input_type is not InputType.TRANSCRIPT_TEXT:
            return record.task_input
        if not record.video_id:
            return record.task_input
        video = self._repository.get_video_asset(record.video_id)
        if video is None:
            return record.task_input
        page = resolve_video_page(video, record.page_number)
        source_url = str(page.source_url if page is not None else video.source_url or "").strip()
        if not source_url:
            return record.task_input
        return TaskInput(
            input_type=InputType.URL if str(video.platform or "").lower() != "local" else InputType.VIDEO_FILE,
            source=source_url,
            title=record.page_title or (page.title if page is not None else video.title) or record.task_input.title,
            platform_hint=video.platform or record.task_input.platform_hint,
            options=record.task_input.options,
        )

    def _run_mindmap(self, task_id: str, force: bool) -> None:
        record = self._repository.get_task(task_id)
        if record is None or record.result is None:
            logger.warning("skip missing task result for mindmap task_id=%s", task_id)
            return
        if record.status != TaskStatus.COMPLETED:
            logger.warning("skip non-completed task mindmap task_id=%s status=%s", task_id, record.status.value)
            return
        self._generate_mindmap(record, force=force)

    def _generate_mindmap(self, record: TaskRecord, *, force: bool) -> TaskResult | None:
        """Generate the mindmap into the task's stored result.

        Shared by the async mindmap job and the inline summary unit. Returns
        the stored result afterwards (``mindmap_status`` ready or failed), or
        ``None`` when the runner cannot build mindmaps or the task vanished.
        Exceptions in ``SUMMARY_UNIT_SIGNALS`` are re-raised, not recorded.
        """

        task_id = record.task_id
        if not hasattr(self._pipeline_runner, "build_and_export_mindmap"):
            logger.warning("pipeline runner does not support mindmap generation task_id=%s", task_id)
            return None

        started_record = self._repository.update_result(
            task_id,
            lambda existing: existing.model_copy(
                update={
                    "mindmap_status": "generating",
                    "mindmap_error_message": None,
                }
            ),
        )
        if started_record is None or started_record.result is None:
            return None
        current_result = started_record.result
        self._append_progress_event(
            task_id,
            stage="mindmap_llm_request",
            progress=90,
            message="正在调用 LLM 生成思维导图",
            payload={"force": force},
        )

        try:
            with record_routes() as route_log:
                mindmap_result = self._pipeline_runner.build_and_export_mindmap(  # type: ignore[attr-defined]
                    task_id,
                    title=record.task_input.title or "思维导图",
                    result=current_result,
                )
            mindmap_sources = route_log.model_sources(("mindmap",))
            if isinstance(mindmap_result, tuple) and len(mindmap_result) == 3:
                mindmap, mindmap_path, usage_breakdown = mindmap_result
            else:
                mindmap, mindmap_path = mindmap_result
                usage_breakdown = []
            self._append_progress_event(
                task_id,
                stage="mindmap_generating",
                progress=96,
                message="正在整理导图结构并写入结果",
            )
            updated_record = self._repository.update_result(
                task_id,
                lambda existing: merge_llm_usage_records(
                    existing,
                    [item for item in usage_breakdown if isinstance(item, dict)],
                ).model_copy(
                    update={
                        "artifacts": {**existing.artifacts, "mindmap_path": str(Path(mindmap_path))},
                        "mindmap_status": "ready",
                        "mindmap_error_message": None,
                        "mindmap_artifact_path": str(Path(mindmap_path)),
                        "mindmap_updated_at": datetime.now(timezone.utc),
                        "model_sources": {**existing.model_sources, **mindmap_sources},
                    }
                ),
            )
            if updated_record is None or updated_record.result is None:
                return None
            final_result = updated_record.result
            self._append_progress_event(
                task_id,
                stage="mindmap_completed",
                progress=100,
                message="思维导图生成完成",
                payload={
                    "root": mindmap.root,
                    "top_level_count": len(mindmap.nodes[0].children) if mindmap.nodes else 0,
                },
            )
            logger.info(
                "mindmap generation completed task_id=%s root=%s top_level=%d",
                task_id,
                mindmap.root,
                len(mindmap.nodes[0].children) if mindmap.nodes else 0,
            )
            return final_result
        except SUMMARY_UNIT_SIGNALS:
            raise
        except Exception as exc:
            logger.exception("mindmap generation failed task_id=%s error=%s", task_id, exc)
            failed_record = self._repository.update_result(
                task_id,
                lambda existing: existing.model_copy(
                    update={
                        "mindmap_status": "failed",
                        "mindmap_error_message": str(exc),
                    }
                ),
            )
            if failed_record is None:
                return None
            self._append_progress_event(
                task_id,
                stage="mindmap_failed",
                progress=100,
                message="思维导图生成失败",
                payload={"error": str(exc), "force": force},
            )
            return failed_record.result
