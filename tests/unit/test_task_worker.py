import sqlite3
import tempfile
import time
from pathlib import Path
from threading import Event

from video_sum_core.models.tasks import InputType, TaskInput, TaskResult, TaskStatus
from video_sum_core.pipeline.base import (
    PipelineContext,
    PipelineEvent,
    PipelineRunner,
    PipelineTranscription,
)
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord, VideoPageOptionResponse
from video_sum_service.worker import TaskWorker


class FakePipelineRunner(PipelineRunner):
    def __init__(self, result: TaskResult) -> None:
        self._result = result

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        return [], self._result


class ResultPipelineRunner(PipelineRunner):
    def __init__(self, results: dict[str, TaskResult]) -> None:
        self._results = results

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        return [], self._results[context.task_id]


class FakeVisualPipelineRunner(FakePipelineRunner):
    def __init__(self, result: TaskResult) -> None:
        super().__init__(result)
        self.visual_inputs: list[TaskInput] = []
        self.visual_force_values: list[bool] = []

    def build_and_export_visual_evidence(self, task_id: str, task_input: TaskInput, title: str, result: TaskResult, mode=None, force=False, on_event=None):
        self.visual_inputs.append(task_input)
        self.visual_force_values.append(force)
        visual_dir = Path(f"C:/tmp/{task_id}-visual")
        if on_event is not None:
            on_event(PipelineEvent(stage="visual_note_composing", progress=92, message="正在整合图文笔记"))
        context = {"status": "ready", "frame_count": 1, "insert_count": 1, "warnings": [], "mode": mode or "frame_insert"}
        return context, visual_dir / "visual_note.md", visual_dir / "visual_context.json"


class BlockingPipelineRunner(PipelineRunner):
    def __init__(self) -> None:
        self.started: list[str] = []
        self._release_by_task: dict[str, Event] = {}

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        self.started.append(context.task_id)
        gate = self._release_by_task.setdefault(context.task_id, Event())
        gate.wait(timeout=5)
        return [], TaskResult(overview=context.task_id)

    def release(self, task_id: str) -> None:
        self._release_by_task.setdefault(task_id, Event()).set()


class ProgressBlockingPipelineRunner(BlockingPipelineRunner):
    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        self.started.append(context.task_id)
        if on_event is not None:
            on_event(PipelineEvent(stage="transcribing", progress=42, message="正在转写"))
        gate = self._release_by_task.setdefault(context.task_id, Event())
        gate.wait(timeout=5)
        return [], TaskResult(overview=context.task_id)


class StagedBlockingPipelineRunner(PipelineRunner):
    def __init__(self, tasks_dir: Path | None = None) -> None:
        self._tasks_dir = tasks_dir or Path(tempfile.mkdtemp(prefix="bilisum-staged-"))
        self.transcription_started: list[str] = []
        self.summary_started: list[str] = []
        self._transcription_release_by_task: dict[str, Event] = {}
        self._summary_release_by_task: dict[str, Event] = {}

    def supports_staged_execution(self) -> bool:
        return True

    def task_dir(self, task_id: str) -> Path:
        return self._tasks_dir / task_id

    def run_transcription(self, context: PipelineContext, on_event=None) -> PipelineTranscription:
        task_id = context.task_id
        self.transcription_started.append(task_id)
        gate = self._transcription_release_by_task.setdefault(task_id, Event())
        gate.wait(timeout=5)
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return PipelineTranscription(
            title=context.task_input.title or task_id,
            transcript=f"转写-{task_id}",
            segments=[{"start": 0.0, "end": 1.5, "text": f"转写-{task_id}"}],
            task_dir=task_dir,
            source_kind="url",
        )

    def run_summary(self, context: PipelineContext, transcription: PipelineTranscription, on_event=None) -> TaskResult:
        task_id = context.task_id
        self.summary_started.append(task_id)
        gate = self._summary_release_by_task.setdefault(task_id, Event())
        gate.wait(timeout=5)
        return TaskResult(overview=task_id, transcript_text=transcription.transcript)

    def release_transcription(self, task_id: str) -> None:
        self._transcription_release_by_task.setdefault(task_id, Event()).set()

    def release_summary(self, task_id: str) -> None:
        self._summary_release_by_task.setdefault(task_id, Event()).set()


class BlockingSummaryAndMindmapRunner(BlockingPipelineRunner):
    def __init__(self) -> None:
        super().__init__()
        self.mindmap_started: list[str] = []
        self._mindmap_release_by_task: dict[str, Event] = {}

    def build_and_export_mindmap(self, task_id: str, title: str, result: TaskResult):
        self.mindmap_started.append(task_id)
        gate = self._mindmap_release_by_task.setdefault(task_id, Event())
        gate.wait(timeout=5)
        mindmap = type(
            "MindMap",
            (),
            {"root": "root", "nodes": [type("Node", (), {"children": []})()]},
        )()
        return mindmap, Path(f"C:/tmp/{task_id}-mindmap.json")

    def release_mindmap(self, task_id: str) -> None:
        self._mindmap_release_by_task.setdefault(task_id, Event()).set()

    def build_and_export_visual_evidence(self, task_id: str, task_input: TaskInput, title: str, result: TaskResult, mode=None, on_event=None):
        visual_dir = Path(f"C:/tmp/{task_id}-visual")
        context = {
            "status": "ready",
            "frame_count": 1,
            "insert_count": 1,
            "mode": mode or "frame_insert",
            "warnings": [],
            "observations": [{"frame_id": "f0001", "timestamp_seconds": 0, "caption": "画面"}],
        }
        return context, visual_dir / "visual_note.md", visual_dir / "visual_context.json"


class PartialFailurePipelineRunner(PipelineRunner):
    def preflight(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        if on_event is not None:
            on_event(PipelineEvent(stage="preflight", progress=2, message="预检通过"))

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        if on_event is not None:
            on_event(
                PipelineEvent(
                    stage="transcribing",
                    progress=86,
                    message="转写完成，已保存可复用文本",
                    payload={
                        "result": TaskResult(
                            transcript_text="[00:00] 测试转写",
                            artifacts={
                                "transcript_path": "C:/tmp/transcript.txt",
                                "summary_path": "C:/tmp/summary.json",
                            },
                        ).model_dump(mode="json"),
                        "result_scope": "transcript",
                    },
                )
            )
        raise RuntimeError("llm api key invalid")


class PreflightFailurePipelineRunner(PipelineRunner):
    def __init__(self) -> None:
        self.run_called = False

    def preflight(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        raise RuntimeError("llm preflight failed")

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        self.run_called = True
        return [], TaskResult()


class TrackingTaskWorker(TaskWorker):
    def __init__(
        self,
        repository: SqliteTaskRepository,
        pipeline_runner: PipelineRunner,
        *,
        auto_generate_mindmap: bool,
        auto_generate_visual_evidence: bool = False,
    ) -> None:
        super().__init__(
            repository,
            pipeline_runner,
            auto_generate_mindmap=auto_generate_mindmap,
            auto_generate_visual_evidence=auto_generate_visual_evidence,
        )
        self.mindmap_calls: list[tuple[str, bool]] = []
        self.visual_calls: list[tuple[str, bool]] = []

    def submit_mindmap(self, task_id: str, *, force: bool = False) -> None:  # type: ignore[override]
        self.mindmap_calls.append((task_id, force))

    def submit_visual_evidence(self, task_id: str, *, force: bool = False) -> None:  # type: ignore[override]
        self.visual_calls.append((task_id, force))


def create_repository() -> SqliteTaskRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository


def create_task(repository: SqliteTaskRepository):
    return repository.create_task(
        TaskInput(input_type=InputType.URL, source="https://example.com/video", title="测试视频")
    )


def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def test_worker_auto_generates_mindmap_when_enabled() -> None:
    repository = create_repository()
    record = create_task(repository)
    result = TaskResult(
        overview="概览",
        knowledge_note_markdown="# 知识笔记",
        artifacts={"summary_path": str(Path("C:/tmp/summary.json"))},
    )
    worker = TrackingTaskWorker(repository, FakePipelineRunner(result), auto_generate_mindmap=True)

    worker._run_task(record.task_id)

    refreshed = repository.get_task(record.task_id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.COMPLETED
    assert worker.mindmap_calls == [(record.task_id, False)]


def test_worker_skips_auto_mindmap_when_disabled() -> None:
    repository = create_repository()
    record = create_task(repository)
    result = TaskResult(
        overview="概览",
        knowledge_note_markdown="# 知识笔记",
        artifacts={"summary_path": str(Path("C:/tmp/summary.json"))},
    )
    worker = TrackingTaskWorker(repository, FakePipelineRunner(result), auto_generate_mindmap=False)

    worker._run_task(record.task_id)

    assert worker.mindmap_calls == []


def test_worker_skips_auto_mindmap_when_required_inputs_are_missing() -> None:
    repository = create_repository()
    record = create_task(repository)
    result = TaskResult(
        overview="概览",
        knowledge_note_markdown="",
        artifacts={},
    )
    worker = TrackingTaskWorker(repository, FakePipelineRunner(result), auto_generate_mindmap=True)

    worker._run_task(record.task_id)

    assert worker.mindmap_calls == []


def test_worker_auto_generates_visual_evidence_when_enabled() -> None:
    repository = create_repository()
    record = create_task(repository)
    result = TaskResult(
        overview="概览",
        knowledge_note_markdown="# 知识笔记",
        timeline=[{"title": "章节", "start": 0.0, "summary": "摘要"}],
        artifacts={"summary_path": str(Path("C:/tmp/summary.json"))},
    )
    worker = TrackingTaskWorker(
        repository,
        FakeVisualPipelineRunner(result),
        auto_generate_mindmap=False,
        auto_generate_visual_evidence=True,
    )

    worker._run_task(record.task_id)

    assert worker.visual_calls == [(record.task_id, False)]


def test_worker_visual_evidence_uses_bound_video_page_for_transcript_tasks() -> None:
    repository = create_repository()
    video = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1visual",
            platform="bilibili",
            title="合集",
            source_url="https://www.bilibili.com/video/BV1visual",
            pages=[
                VideoPageOptionResponse(page=1, title="P1", source_url="https://www.bilibili.com/video/BV1visual?p=1"),
                VideoPageOptionResponse(page=72, title="P72", source_url="https://www.bilibili.com/video/BV1visual?p=72"),
            ],
        )
    )
    record = repository.create_task(
        TaskInput(input_type=InputType.TRANSCRIPT_TEXT, source='{"transcript":"[00:00] 内容"}', title="P72"),
        video_id=video.video_id,
        page_number=72,
        page_title="P72",
    )
    result = TaskResult(
        overview="概览",
        knowledge_note_markdown="# 知识笔记",
        timeline=[{"title": "章节", "start": 0.0, "summary": "摘要"}],
        artifacts={"summary_path": str(Path("C:/tmp/summary.json"))},
    )
    repository.save_result(record.task_id, result)
    repository.update_status(record.task_id, TaskStatus.COMPLETED)
    runner = FakeVisualPipelineRunner(result)
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False)

    worker._run_visual_evidence(record.task_id, force=True)

    assert runner.visual_inputs
    visual_input = runner.visual_inputs[0]
    assert visual_input.input_type == InputType.URL
    assert visual_input.source == "https://www.bilibili.com/video/BV1visual?p=72"
    assert visual_input.title == "P72"
    assert runner.visual_force_values == [True]


def test_worker_skips_auto_visual_evidence_when_disabled() -> None:
    repository = create_repository()
    record = create_task(repository)
    result = TaskResult(
        overview="概览",
        timeline=[{"title": "章节", "start": 0.0, "summary": "摘要"}],
        artifacts={"summary_path": str(Path("C:/tmp/summary.json"))},
    )
    worker = TrackingTaskWorker(repository, FakePipelineRunner(result), auto_generate_mindmap=False)

    worker._run_task(record.task_id)

    assert worker.visual_calls == []


def test_worker_preserves_transcript_result_when_summary_phase_fails() -> None:
    repository = create_repository()
    record = create_task(repository)
    worker = TaskWorker(repository, PartialFailurePipelineRunner(), auto_generate_mindmap=False)

    worker._run_task(record.task_id)

    refreshed = repository.get_task(record.task_id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.PAUSED
    assert refreshed.result is not None
    assert refreshed.result.transcript_text == "[00:00] 测试转写"
    assert refreshed.result.artifacts["summary_path"] == "C:/tmp/summary.json"


def test_worker_stops_before_run_when_preflight_fails() -> None:
    repository = create_repository()
    record = create_task(repository)
    runner = PreflightFailurePipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False)

    worker._run_task(record.task_id)

    refreshed = repository.get_task(record.task_id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.FAILED
    assert runner.run_called is False


def test_worker_snapshot_starts_idle_and_tracks_parallel_pending() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(3)]
    runner = BlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=2)

    initial_batch = worker.snapshot()["batch"]
    assert initial_batch == {
        "completed_tasks": 0,
        "total_tasks": 0,
        "pending_tasks": 0,
        "running_tasks": 0,
        "concurrency": 2,
        "transcription_concurrency": 2,
        "llm_concurrency": 2,
        "mode": "parallel",
        "total_tokens": 0,
        "runtime_seconds": 0.0,
        "started_at": None,
        "completed_at": None,
        "state": "idle",
        "visible": False,
        "items": [],
    }

    for record in records:
        worker.submit(record)

    wait_for(lambda: len(runner.started) == 2)
    batch = worker.snapshot()["batch"]
    assert batch["completed_tasks"] == 0
    assert batch["total_tasks"] == 3
    assert batch["pending_tasks"] == 1
    assert batch["running_tasks"] == 2
    assert batch["concurrency"] == 2
    assert batch["mode"] == "parallel"
    assert batch["state"] == "active"
    assert batch["visible"] is True

    for record in records:
        runner.release(record.task_id)
    wait_for(lambda: all(repository.get_task(record.task_id).status == TaskStatus.COMPLETED for record in records))


def test_worker_snapshot_counts_completed_summary_tasks_and_tokens() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(2)]
    results = {
        records[0].task_id: TaskResult(overview="一", llm_total_tokens=11),
        records[1].task_id: TaskResult(overview="二", llm_total_tokens=22),
    }
    worker = TaskWorker(
        repository,
        ResultPipelineRunner(results),
        auto_generate_mindmap=False,
        task_concurrency=2,
    )

    for record in records:
        worker.submit(record)

    wait_for(lambda: worker.snapshot()["batch"]["completed_tasks"] == 2)
    batch = worker.snapshot()["batch"]
    assert batch["completed_tasks"] == 2
    assert batch["total_tasks"] == 2
    assert batch["pending_tasks"] == 0
    assert batch["running_tasks"] == 0
    assert batch["total_tokens"] == 33
    assert batch["runtime_seconds"] >= 0
    assert batch["started_at"] is not None
    assert batch["completed_at"] is not None
    assert batch["state"] == "completed"
    assert batch["visible"] is True


def test_worker_snapshot_counts_failed_summary_task_as_completed() -> None:
    repository = create_repository()
    record = create_task(repository)
    worker = TaskWorker(repository, PreflightFailurePipelineRunner(), auto_generate_mindmap=False)

    worker.submit(record)
    wait_for(lambda: repository.get_task(record.task_id).status == TaskStatus.FAILED)
    batch = worker.snapshot()["batch"]
    assert batch["completed_tasks"] == 1
    assert batch["total_tasks"] == 1
    assert batch["pending_tasks"] == 0
    assert batch["running_tasks"] == 0
    assert batch["total_tokens"] == 0
    assert batch["state"] == "completed"
    items = {item["task_id"]: item for item in batch["items"]}
    assert items[record.task_id]["status"] == TaskStatus.FAILED.value
    assert items[record.task_id]["progress"] == 0


def test_worker_snapshot_items_track_queued_running_and_completed_progress() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(2)]
    runner = ProgressBlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1)

    worker.submit(records[0])
    worker.submit(records[1])
    wait_for(
        lambda: (
            len(runner.started) == 1
            and {item["task_id"]: item for item in worker.snapshot()["batch"]["items"]}[records[0].task_id]["status"]
            == TaskStatus.RUNNING.value
        )
    )

    items = {item["task_id"]: item for item in worker.snapshot()["batch"]["items"]}
    assert items[records[0].task_id]["status"] == TaskStatus.RUNNING.value
    assert items[records[0].task_id]["progress"] == 42
    assert items[records[0].task_id]["message"] == "正在转写"
    assert items[records[1].task_id]["status"] == TaskStatus.QUEUED.value
    assert items[records[1].task_id]["progress"] == 0

    runner.release(records[0].task_id)
    wait_for(
        lambda: (
            len(runner.started) == 2
            and {item["task_id"]: item for item in worker.snapshot()["batch"]["items"]}[records[0].task_id]["status"]
            == TaskStatus.COMPLETED.value
        )
    )
    items = {item["task_id"]: item for item in worker.snapshot()["batch"]["items"]}
    assert items[records[0].task_id]["progress"] == 100
    assert items[records[1].task_id]["status"] == TaskStatus.RUNNING.value
    assert items[records[1].task_id]["progress"] == 42

    runner.release(records[1].task_id)
    wait_for(lambda: all(repository.get_task(record.task_id).status == TaskStatus.COMPLETED for record in records))
    items = {item["task_id"]: item for item in worker.snapshot()["batch"]["items"]}
    assert all(item["status"] == TaskStatus.COMPLETED.value for item in items.values())
    assert all(item["progress"] == 100 for item in items.values())


def test_worker_snapshot_resets_when_a_new_batch_is_submitted() -> None:
    repository = create_repository()
    first_record, second_record = create_task(repository), create_task(repository)
    runner = BlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1)

    worker.submit(first_record)
    wait_for(lambda: len(runner.started) == 1)
    runner.release(first_record.task_id)
    wait_for(lambda: repository.get_task(first_record.task_id).status == TaskStatus.COMPLETED)
    first_batch = worker.snapshot()["batch"]
    assert first_batch["completed_tasks"] == 1
    assert first_batch["total_tasks"] == 1

    worker.submit(second_record)
    wait_for(lambda: len(runner.started) == 2)
    second_batch = worker.snapshot()["batch"]
    assert second_batch["completed_tasks"] == 0
    assert second_batch["total_tasks"] == 1
    assert second_batch["pending_tasks"] == 0
    assert second_batch["running_tasks"] == 1
    assert second_batch["total_tokens"] == 0
    assert second_batch["started_at"] != first_batch["started_at"]
    assert second_batch["completed_at"] is None
    assert second_batch["state"] == "active"

    runner.release(second_record.task_id)
    wait_for(lambda: repository.get_task(second_record.task_id).status == TaskStatus.COMPLETED)


def test_worker_limits_summary_task_concurrency() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(3)]
    runner = BlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1)

    for record in records:
        worker.submit(record)

    wait_for(lambda: len(runner.started) == 1)
    statuses = {record.task_id: repository.get_task(record.task_id).status for record in records if repository.get_task(record.task_id)}
    assert list(statuses.values()).count(TaskStatus.RUNNING) == 1
    assert list(statuses.values()).count(TaskStatus.QUEUED) == 2

    first_task_id = runner.started[0]
    runner.release(first_task_id)
    wait_for(lambda: len(runner.started) == 2)

    second_running = [task_id for task_id in runner.started if repository.get_task(task_id).status == TaskStatus.RUNNING]
    assert len(second_running) == 1

    second_task_id = [task_id for task_id in runner.started if task_id != first_task_id][0]
    runner.release(second_task_id)
    wait_for(lambda: len(runner.started) == 3)

    for task_id in runner.started:
        runner.release(task_id)
    wait_for(lambda: all(repository.get_task(record.task_id).status == TaskStatus.COMPLETED for record in records))


def test_worker_releases_transcription_slot_before_llm_stage_finishes() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(2)]
    runner = StagedBlockingPipelineRunner()
    worker = TaskWorker(
        repository,
        runner,
        auto_generate_mindmap=False,
        transcription_concurrency=1,
        llm_concurrency=1,
    )

    for record in records:
        worker.submit(record)

    first_task_id = records[0].task_id
    second_task_id = records[1].task_id
    wait_for(lambda: runner.transcription_started == [first_task_id])

    # The first task's LLM stage is intentionally held. Releasing only its
    # transcription must still allow the second task to start transcribing.
    runner.release_transcription(first_task_id)
    wait_for(lambda: runner.transcription_started == [first_task_id, second_task_id])
    wait_for(lambda: runner.summary_started == [first_task_id])
    assert repository.get_task(second_task_id).status == TaskStatus.RUNNING

    runner.release_transcription(second_task_id)
    runner.release_summary(first_task_id)
    wait_for(lambda: runner.summary_started == [first_task_id, second_task_id])
    runner.release_summary(second_task_id)
    wait_for(
        lambda: all(
            repository.get_task(record.task_id).status == TaskStatus.COMPLETED
            for record in records
        )
    )
    batch = worker.snapshot()["batch"]
    assert batch["transcription_concurrency"] == 1
    assert batch["llm_concurrency"] == 1
    assert batch["completed_tasks"] == 2


def test_worker_uses_separate_summary_and_mindmap_pools() -> None:
    repository = create_repository()
    summary_record = create_task(repository)
    mindmap_record = create_task(repository)
    repository.save_result(
        mindmap_record.task_id,
        TaskResult(
            overview="概览",
            knowledge_note_markdown="# 笔记",
            artifacts={"summary_path": "C:/tmp/summary.json"},
        ),
    )
    repository.update_status(mindmap_record.task_id, TaskStatus.COMPLETED)

    runner = BlockingSummaryAndMindmapRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1, mindmap_concurrency=1)

    worker.submit(summary_record)
    worker.submit_mindmap(mindmap_record.task_id)

    wait_for(lambda: len(runner.started) == 1)
    wait_for(lambda: len(runner.mindmap_started) == 1)

    assert repository.get_task(summary_record.task_id).status == TaskStatus.RUNNING
    refreshed_mindmap_record = repository.get_task(mindmap_record.task_id)
    assert refreshed_mindmap_record is not None
    assert refreshed_mindmap_record.result is not None
    assert refreshed_mindmap_record.result.mindmap_status == "generating"

    runner.release(summary_record.task_id)
    runner.release_mindmap(mindmap_record.task_id)
    wait_for(lambda: repository.get_task(summary_record.task_id).status == TaskStatus.COMPLETED)


def test_worker_close_for_new_work_preserves_existing_queue() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(2)]
    runner = BlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1)

    worker.submit(records[0])
    worker.submit(records[1])
    wait_for(lambda: len(runner.started) == 1)

    worker.close_for_new_work()
    rejected_record = create_task(repository)
    assert worker.submit(rejected_record) == "rejected"
    time.sleep(0.1)

    runner.release(records[0].task_id)
    wait_for(lambda: len(runner.started) == 2)
    runner.release(records[1].task_id)
    wait_for(lambda: repository.get_task(records[1].task_id).status == TaskStatus.COMPLETED)

    assert repository.get_latest_event(rejected_record.task_id) is None
    assert repository.get_task(rejected_record.task_id).status == TaskStatus.QUEUED


def test_worker_shutdown_cancels_pending_jobs() -> None:
    repository = create_repository()
    records = [create_task(repository) for _ in range(2)]
    runner = BlockingPipelineRunner()
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False, task_concurrency=1)

    worker.submit(records[0])
    worker.submit(records[1])
    wait_for(lambda: len(runner.started) == 1)

    worker.shutdown(wait=False, cancel_pending=True)
    runner.release(records[0].task_id)
    wait_for(lambda: repository.get_task(records[0].task_id).status == TaskStatus.COMPLETED)
    time.sleep(0.1)

    assert runner.started == [records[0].task_id]


def test_restart_between_transcription_and_summary_resumes_summary(tmp_path: Path) -> None:
    from video_sum_service.app import recover_incomplete_tasks

    repository = create_repository()
    record = create_task(repository)
    task_id = record.task_id
    first_runner = StagedBlockingPipelineRunner(tmp_path)
    first_worker = TaskWorker(repository, first_runner, auto_generate_mindmap=False)
    first_worker.submit(record)
    first_runner.release_transcription(task_id)
    # The service "dies" while the LLM stage is running: the summary never returns.
    wait_for(lambda: first_runner.summary_started == [task_id])
    assert repository.get_task(task_id).status == TaskStatus.RUNNING

    restarted_runner = StagedBlockingPipelineRunner(tmp_path)
    restarted_worker = TaskWorker(repository, restarted_runner, auto_generate_mindmap=False)
    restarted_runner.release_summary(task_id)
    try:
        assert recover_incomplete_tasks(repository, restarted_worker) == 1
        wait_for(lambda: repository.get_task(task_id).status == TaskStatus.COMPLETED)
        assert restarted_runner.transcription_started == []
        assert restarted_runner.summary_started == [task_id]
        result = repository.get_task(task_id).result
        assert result.transcript_text == f"转写-{task_id}"
    finally:
        first_runner.release_summary(task_id)
        first_worker.shutdown(wait=True, timeout=5)
        restarted_worker.shutdown(wait=True, timeout=5)


def test_restart_during_transcription_transcribes_again(tmp_path: Path) -> None:
    from video_sum_service.app import recover_incomplete_tasks

    repository = create_repository()
    record = create_task(repository)
    task_id = record.task_id
    repository.update_status(task_id, TaskStatus.RUNNING)

    runner = StagedBlockingPipelineRunner(tmp_path)
    worker = TaskWorker(repository, runner, auto_generate_mindmap=False)
    runner.release_transcription(task_id)
    runner.release_summary(task_id)
    try:
        assert recover_incomplete_tasks(repository, worker) == 1
        wait_for(lambda: repository.get_task(task_id).status == TaskStatus.COMPLETED)
        assert runner.transcription_started == [task_id]
        assert runner.summary_started == [task_id]
    finally:
        worker.shutdown(wait=True, timeout=5)


class EventfulPipelineRunner(PipelineRunner):
    """Emits one progress event per stage, staged or not, optionally failing."""

    def __init__(
        self,
        *,
        staged: bool,
        fail_in: str | None = None,
        tasks_dir: Path | None = None,
        mindmap_error: Exception | None = None,
    ) -> None:
        self._staged = staged
        self._fail_in = fail_in
        self._tasks_dir = tasks_dir or Path(tempfile.mkdtemp(prefix="bilisum-eventful-"))
        self._mindmap_error = mindmap_error
        self.calls: list[str] = []

    def supports_staged_execution(self) -> bool:
        return self._staged

    def task_dir(self, task_id: str) -> Path:
        return self._tasks_dir / task_id

    def preflight(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        on_event(PipelineEvent(stage="preflight", progress=3, message="预检通过"))
        if self._fail_in == "preflight":
            raise RuntimeError("preflight boom")

    def run_transcription(self, context: PipelineContext, on_event=None) -> PipelineTranscription:
        on_event(PipelineEvent(stage="transcribing", progress=40, message="正在转写"))
        if self._fail_in == "transcription":
            raise RuntimeError("asr boom")
        task_dir = self.task_dir(context.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return PipelineTranscription(
            title="t",
            transcript="转写",
            segments=[{"start": 0.0, "end": 1.0, "text": "转写"}],
            task_dir=task_dir,
            source_kind="url",
        )

    def run_summary(
        self, context: PipelineContext, transcription: PipelineTranscription, on_event=None
    ) -> TaskResult:
        self.calls.append("summary")
        on_event(PipelineEvent(stage="summarizing", progress=90, message="正在总结"))
        if self._fail_in == "summary":
            raise RuntimeError("llm boom")
        return self._summary_result(transcription.transcript)

    def run(self, context: PipelineContext, on_event=None):  # type: ignore[override]
        on_event(PipelineEvent(stage="transcribing", progress=40, message="正在转写"))
        if self._fail_in == "transcription":
            raise RuntimeError("asr boom")
        on_event(PipelineEvent(stage="summarizing", progress=90, message="正在总结"))
        if self._fail_in == "summary":
            raise RuntimeError("llm boom")
        return [], self._summary_result("转写")

    def build_and_export_mindmap(self, task_id: str, title: str, result: TaskResult):
        self.calls.append("mindmap")
        if self._mindmap_error is not None:
            raise self._mindmap_error
        mindmap = type(
            "MindMap", (), {"root": "root", "nodes": [type("Node", (), {"children": [1, 2]})()]}
        )()
        return (
            mindmap,
            self._tasks_dir / task_id / "mindmap.json",
            [{"stage": "mindmap", "total_tokens": 7}],
        )

    @staticmethod
    def _summary_result(transcript: str) -> TaskResult:
        return TaskResult(
            overview="概览",
            transcript_text=transcript,
            knowledge_note_markdown="# 笔记",
            artifacts={"summary_path": "C:/tmp/summary.json"},
        )


def run_to_terminal(
    repository: SqliteTaskRepository,
    runner: PipelineRunner,
    *,
    worker_factory=TaskWorker,
    **worker_kwargs,
) -> tuple[str, list[tuple[str, int]]]:
    record = create_task(repository)
    worker_kwargs.setdefault("auto_generate_mindmap", False)
    worker = worker_factory(repository, runner, **worker_kwargs)
    try:
        worker.submit(record)
        wait_for(lambda: worker.snapshot()["batch"]["completed_tasks"] == 1)
    finally:
        worker.shutdown(wait=True, timeout=5)
    events = [(event.stage, event.progress) for event in repository.list_events(record.task_id)]
    return repository.get_task(record.task_id).status.value, events


def test_unstaged_path_event_sequence_on_success() -> None:
    status, events = run_to_terminal(create_repository(), EventfulPipelineRunner(staged=False))

    assert status == "completed"
    assert events == [
        ("queued", 0),
        ("running", 5),
        ("preflight", 3),
        ("transcribing", 40),
        ("summarizing", 90),
        ("completed", 100),
    ]


def test_staged_path_event_sequence_on_success() -> None:
    status, events = run_to_terminal(create_repository(), EventfulPipelineRunner(staged=True))

    assert status == "completed"
    assert events == [
        ("queued", 0),
        ("transcription_started", 5),
        ("transcribing", 40),
        ("llm_queued", 87),
        ("llm_started", 88),
        ("preflight", 3),
        ("summarizing", 90),
        ("completed", 100),
    ]


# The staged path saves the transcript at the stage boundary, so an LLM failure
# there pauses the task for "仅重跑 LLM"; the single-pass path has no saved
# transcript and fails as before.
END_STATE = {False: ("failed", "TASK_EXECUTION_FAILED"), True: ("paused", "LLM_RESUMABLE")}


def test_both_paths_end_with_their_failure_event_on_partial_failure() -> None:
    for staged in (False, True):
        repository = create_repository()
        status, events = run_to_terminal(
            repository, EventfulPipelineRunner(staged=staged, fail_in="summary")
        )

        expected_status, expected_code = END_STATE[staged]
        assert status == expected_status
        assert events[-2:] == [("summarizing", 90), (expected_status, 0)]
        task_id = repository.list_tasks()[0].task_id
        failed = repository.list_events(task_id)[-1]
        assert failed.message.startswith("llm boom")
        assert ("转写已保存" in failed.message) is staged
        assert failed.payload == {"error": "llm boom"}
        assert repository.get_task(task_id).error_code == expected_code


def test_both_paths_fail_before_summary_when_preflight_fails() -> None:
    for staged in (False, True):
        status, events = run_to_terminal(
            create_repository(), EventfulPipelineRunner(staged=staged, fail_in="preflight")
        )

        expected_status, _code = END_STATE[staged]
        assert status == expected_status
        assert events[-2:] == [("preflight", 3), (expected_status, 0)]
        assert ("summarizing", 90) not in events


def test_both_paths_queue_async_mindmap_after_completion() -> None:
    for staged in (False, True):
        runner = EventfulPipelineRunner(staged=staged)
        status, events = run_to_terminal(create_repository(), runner, auto_generate_mindmap=True)

        assert status == "completed"
        stages = [stage for stage, _ in events]
        assert stages.index("completed") < stages.index("mindmap_queued")


class InlinePolicyTaskWorker(TaskWorker):
    """Opts every task into the ``inline`` after-completion policy (Agent mode uses it)."""

    def _summary_policy(self, record):  # type: ignore[override]
        return "inline"


def test_inline_policy_runs_mindmap_inside_summary_unit_before_completion() -> None:
    repository = create_repository()
    runner = EventfulPipelineRunner(staged=True)

    status, events = run_to_terminal(
        repository,
        runner,
        worker_factory=InlinePolicyTaskWorker,
        auto_generate_mindmap=True,
        auto_generate_visual_evidence=True,
    )

    assert status == "completed"
    assert runner.calls == ["summary", "mindmap"]
    stages = [stage for stage, _ in events]
    assert stages[-4:] == [
        "mindmap_llm_request",
        "mindmap_generating",
        "mindmap_completed",
        "completed",
    ]
    assert "mindmap_queued" not in stages
    assert "visual_queued" not in stages
    result = repository.list_tasks()[0].result
    assert result.mindmap_status == "ready"
    assert result.artifacts["mindmap_path"].endswith("mindmap.json")
    assert result.overview == "概览"


def test_inline_policy_skips_mindmap_when_auto_mindmap_is_disabled() -> None:
    runner = EventfulPipelineRunner(staged=True)

    status, events = run_to_terminal(
        create_repository(), runner, worker_factory=InlinePolicyTaskWorker
    )

    assert status == "completed"
    assert runner.calls == ["summary"]
    assert not [stage for stage, _ in events if stage.startswith("mindmap")]


def test_inline_policy_mindmap_failure_still_completes_task_like_async() -> None:
    repository = create_repository()
    runner = EventfulPipelineRunner(staged=True, mindmap_error=RuntimeError("mindmap boom"))

    status, events = run_to_terminal(
        repository, runner, worker_factory=InlinePolicyTaskWorker, auto_generate_mindmap=True
    )

    assert status == "completed"
    stages = [stage for stage, _ in events]
    assert stages[-2:] == ["mindmap_failed", "completed"]
    result = repository.list_tasks()[0].result
    assert result.mindmap_status == "failed"
    assert result.mindmap_error_message == "mindmap boom"


def test_summary_failure_under_inline_policy_skips_mindmap() -> None:
    runner = EventfulPipelineRunner(staged=True, fail_in="summary")

    status, events = run_to_terminal(
        create_repository(),
        runner,
        worker_factory=InlinePolicyTaskWorker,
        auto_generate_mindmap=True,
    )

    assert status == "paused"  # staged path: transcript saved
    assert runner.calls == ["summary"]
    assert events[-1] == ("paused", 0)


def test_summary_unit_signal_crosses_inline_mindmap_fallback() -> None:
    from video_sum_core.pipeline.model_steps import AgentWorkRequired

    repository = create_repository()
    runner = EventfulPipelineRunner(staged=True, mindmap_error=AgentWorkRequired("needs agent"))

    status, events = run_to_terminal(
        repository, runner, worker_factory=InlinePolicyTaskWorker, auto_generate_mindmap=True
    )

    stages = [stage for stage, _ in events]
    assert "mindmap_failed" not in stages
    assert "completed" not in stages
    # The signal is not a failure: the task waits for the Agent.
    assert status == "agent_summarizing"
    assert "paused" not in stages and "failed" not in stages
    assert events[-1][0] == "agent_work_required"
