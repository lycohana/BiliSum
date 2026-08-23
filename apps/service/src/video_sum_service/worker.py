import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock, Thread, current_thread
from typing import Literal

from video_sum_core.models.tasks import (
    InputType,
    TaskInput,
    TaskResult,
    TaskStatus,
    merge_llm_usage_records,
)
from video_sum_core.pipeline.base import PipelineContext, PipelineRunner, PipelineTranscription
from video_sum_infra.config import ServiceSettings, normalize_visual_note_mode
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import TaskRecord
from video_sum_service.video_assets import resolve_video_page

logger = logging.getLogger("video_sum_service.worker")


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
        if result == "accepted":
            self._repository.update_status(task.task_id, TaskStatus.QUEUED)
            self._repository.append_event(
                task_id=task.task_id,
                stage="queued",
                progress=0,
                message="任务已进入后台队列",
            )
        return result

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

    def _enqueue_summary(self, task: TaskRecord) -> Literal["accepted", "already_queued", "rejected"]:
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

            self._task_state.pending.append(task)
            self._task_state.pending_ids.add(job_id)
            self._summary_batch.total_tasks += 1
            self._summary_batch.items[job_id] = {
                "task_id": job_id,
                "title": task.page_title or task.task_input.title or job_id,
                "status": TaskStatus.QUEUED.value,
                "progress": 0,
                "message": "任务已进入后台队列",
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
                    job = state.pending.popleft()
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
        if status == TaskStatus.CANCELLED.value:
            return status, "任务已取消", task_tokens
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

            self._update_summary_item(
                task_id,
                progress=event.progress,
                message=event.message,
            )
            self._repository.append_event(
                task_id=task_id,
                stage=event.stage,
                progress=event.progress,
                message=event.message,
                payload=payload,
            )

        return handle_pipeline_event

    def _mark_task_failed(self, task_id: str, exc: Exception) -> None:
        self._repository.update_error(task_id, "TASK_EXECUTION_FAILED", str(exc))
        self._repository.update_status(task_id, TaskStatus.FAILED)
        self._update_summary_item(
            task_id,
            status=TaskStatus.FAILED.value,
            progress=0,
            message=str(exc) or "任务执行失败",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="failed",
            progress=0,
            message="任务执行失败",
            payload={"error": str(exc)},
        )

    def _finalize_summary_task(self, record: TaskRecord, result: TaskResult) -> None:
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
        if self._auto_generate_mindmap and self._can_auto_generate_mindmap(final_result):
            self.submit_mindmap(task_id)
        if (
            self._auto_generate_visual_evidence
            and final_result.visual_note_status not in {"generating", "ready", "partial", "unsupported", "failed"}
            and self._can_generate_visual_evidence(record, final_result)
        ):
            self.submit_visual_evidence(task_id)
        if self._knowledge_index_auto_rebuild == "on_task_completed":
            self._index_completed_task(record.video_id, task_id)

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

        self._active_runner = self._pipeline_runner
        try:
            context = PipelineContext(task_id=task_id, task_input=record.task_input)
            transcription = self._pipeline_runner.run_transcription(  # type: ignore[attr-defined]
                context,
                on_event=self._pipeline_event_handler(task_id),
            )
            if not isinstance(transcription, PipelineTranscription):
                raise TypeError("staged transcription must return PipelineTranscription")
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
            exc = RuntimeError("转写阶段结果不存在，无法进入 LLM 阶段")
            logger.error("missing staged transcription task_id=%s", task_id)
            self._mark_task_failed(task_id, exc)
            return

        self._update_summary_item(
            task_id,
            status=TaskStatus.RUNNING.value,
            progress=88,
            message="正在进入 LLM 摘要阶段",
        )
        self._repository.append_event(
            task_id=task_id,
            stage="llm_started",
            progress=88,
            message="正在进入 LLM 摘要阶段",
            payload={"llm_concurrency": self._llm_state.concurrency},
        )
        self._active_runner = self._pipeline_runner
        try:
            context = PipelineContext(task_id=task_id, task_input=record.task_input)
            event_handler = self._pipeline_event_handler(task_id)
            self._pipeline_runner.preflight(context, on_event=event_handler)
            result = self._pipeline_runner.run_summary(  # type: ignore[attr-defined]
                context,
                transcription,
                on_event=event_handler,
            )
            self._finalize_summary_task(record, result)
        except Exception as exc:
            logger.exception("LLM stage failed task_id=%s error=%s", task_id, exc)
            self._mark_task_failed(task_id, exc)
        finally:
            self._active_runner = None

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

                self._update_summary_item(
                    task_id,
                    progress=event.progress,
                    message=event.message,
                )
                self._repository.append_event(
                    task_id=task_id,
                    stage=event.stage,
                    progress=event.progress,
                    message=event.message,
                    payload=payload,
                )

            self._pipeline_runner.preflight(
                context,
                on_event=handle_pipeline_event,
            )

            _, result = self._pipeline_runner.run(
                context,
                on_event=handle_pipeline_event,
            )

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
            if self._auto_generate_mindmap and self._can_auto_generate_mindmap(final_result):
                self.submit_mindmap(task_id)
            if (
                self._auto_generate_visual_evidence
                and final_result.visual_note_status not in {"generating", "ready", "partial", "unsupported", "failed"}
                and self._can_generate_visual_evidence(record, final_result)
            ):
                self.submit_visual_evidence(task_id)
            if self._knowledge_index_auto_rebuild == "on_task_completed":
                self._index_completed_task(record.video_id, task_id)
        except Exception as exc:
            logger.exception("task failed task_id=%s error=%s", task_id, exc)
            self._repository.update_error(task_id, "TASK_EXECUTION_FAILED", str(exc))
            self._repository.update_status(task_id, TaskStatus.FAILED)
            self._update_summary_item(
                task_id,
                status=TaskStatus.FAILED.value,
                progress=0,
                message=str(exc) or "任务执行失败",
            )
            self._repository.append_event(
                task_id=task_id,
                stage="failed",
                progress=0,
                message="任务执行失败",
                payload={"error": str(exc)},
            )
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

            context, note_path, context_path = self._pipeline_runner.build_and_export_visual_evidence(  # type: ignore[attr-defined]
                task_id=task_id,
                task_input=self._resolve_visual_task_input(record),
                title=record.page_title or record.task_input.title or "图文笔记",
                result=current_result,
                mode=normalized_mode,
                force=force,
                on_event=handle_visual_event,
            )
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
        if not hasattr(self._pipeline_runner, "build_and_export_mindmap"):
            logger.warning("pipeline runner does not support mindmap generation task_id=%s", task_id)
            return

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
            return
        current_result = started_record.result
        self._repository.append_event(
            task_id=task_id,
            stage="mindmap_llm_request",
            progress=90,
            message="正在调用 LLM 生成思维导图",
            payload={"force": force},
        )

        try:
            mindmap_result = self._pipeline_runner.build_and_export_mindmap(  # type: ignore[attr-defined]
                task_id=task_id,
                title=record.task_input.title or "思维导图",
                result=current_result,
            )
            if isinstance(mindmap_result, tuple) and len(mindmap_result) == 3:
                mindmap, mindmap_path, usage_breakdown = mindmap_result
            else:
                mindmap, mindmap_path = mindmap_result
                usage_breakdown = []
            self._repository.append_event(
                task_id=task_id,
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
                    }
                ),
            )
            if updated_record is None or updated_record.result is None:
                return
            final_result = updated_record.result
            self._repository.append_event(
                task_id=task_id,
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
                return
            self._repository.append_event(
                task_id=task_id,
                stage="mindmap_failed",
                progress=100,
                message="思维导图生成失败",
                payload={"error": str(exc), "force": force},
            )
