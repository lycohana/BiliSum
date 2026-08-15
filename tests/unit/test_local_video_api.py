import json
import sqlite3

import pytest
from fastapi import HTTPException
from video_sum_core.models.tasks import InputType, TaskInput, TaskResult, TaskStatus
from video_sum_service.app import app
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers.videos import (
    create_video_aggregate_summary_task,
    create_video_resummary_tasks_batch,
    create_video_task,
    create_video_tasks_batch,
)
from video_sum_service.schemas import VideoAssetRecord


class FakeTaskWorker:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, record) -> None:
        self.submitted.append(record)


def create_repository() -> SqliteTaskRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository


def make_page(canonical_id: str, page: int, title: str) -> dict[str, str | int]:
    return {
        "page": page,
        "title": title,
        "source_url": f"https://www.bilibili.com/video/{canonical_id}?p={page}",
        "cover_url": "",
    }


def append_completed_page_task(
    repository: SqliteTaskRepository,
    video_id: str,
    page_number: int,
    title: str = "P1",
) -> str:
    record = repository.create_task(
        TaskInput(
            input_type=InputType.URL,
            source=f"https://example.com/video?p={page_number}",
            title=title,
            platform_hint="bilibili",
        ),
        video_id=video_id,
        page_number=page_number,
        page_title=title,
    )
    repository.update_status(record.task_id, TaskStatus.COMPLETED)
    repository.save_result(
        record.task_id,
        TaskResult(
            overview=f"{title} overview",
            knowledge_note_markdown=f"# {title} note",
            transcript_text="[00:00] hello",
            key_points=[f"{title} key point"],
            timeline=[
                {
                    "title": f"{title} chapter",
                    "start": 0,
                    "summary": f"{title} chapter summary",
                }
            ],
            artifacts={"summary_path": f"C:/tmp/{record.task_id}-summary.json"},
        ),
    )
    return record.task_id


def test_create_video_task_uses_video_file_input_for_local_asset() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="local-test-video",
            platform="local",
            title="本地视频",
            source_url=r"C:\videos\sample.mp4",
            cover_url="/media/covers/local-test-video.jpg",
            duration=42.0,
        )
    )
    app.state.task_repository = repository
    app.state.task_worker = worker

    response = create_video_task(type("Request", (), {"app": app})(), asset.video_id)

    assert len(worker.submitted) == 1
    assert worker.submitted[0].task_input.input_type is InputType.VIDEO_FILE
    assert worker.submitted[0].task_input.source == asset.source_url
    assert response.input_type == InputType.VIDEO_FILE.value
    assert response.video_id == asset.video_id


def test_create_video_task_does_not_reprobe_page_metadata_before_submit(monkeypatch) -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-no-reprobe",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-no-reprobe",
            pages=[
                {
                    **make_page("BV-no-reprobe", 1, "P1 开场"),
                    "duration": None,
                },
            ],
        )
    )
    app.state.task_repository = repository
    app.state.task_worker = worker
    monkeypatch.setattr(
        "video_sum_service.routers.videos.probe_video_asset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )

    response = create_video_task(
        type("Request", (), {"app": app})(),
        asset.video_id,
        type("Body", (), {"page_number": 1})(),
    )

    assert len(worker.submitted) == 1
    assert worker.submitted[0].task_input.source == (
        "https://www.bilibili.com/video/BV-no-reprobe?p=1"
    )
    assert response.page_number == 1


def test_create_video_task_passes_summary_scope_through() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-scope",
            platform="bilibili",
            title="作用域测试",
            source_url="https://www.bilibili.com/video/BV-scope",
        )
    )
    app.state.task_repository = repository
    app.state.task_worker = worker

    response = create_video_task(
        type("Request", (), {"app": app})(),
        asset.video_id,
        type("Body", (), {"summary_scope": "summary"})(),
    )

    assert len(worker.submitted) == 1
    assert worker.submitted[0].task_input.options.summary_scope == "summary"
    assert response.task_id == worker.submitted[0].task_id


def test_create_video_task_applies_prompt_preset_id() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-prompt",
            platform="bilibili",
            title="Prompt 视频",
            source_url="https://www.bilibili.com/video/BV-prompt",
        )
    )
    app.state.task_repository = repository
    app.state.task_worker = worker

    create_video_task(
        type("Request", (), {"app": app})(),
        asset.video_id,
        type("Body", (), {"prompt_preset_id": "technical_tutorial"})(),
    )

    assert worker.submitted[0].task_input.options.prompt_preset_id == "technical_tutorial"


def test_create_video_tasks_batch_requires_confirmation_for_completed_pages() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-batch-confirm",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-batch-confirm",
            pages=[
                make_page("BV-batch-confirm", 1, "P1 开场"),
                make_page("BV-batch-confirm", 2, "P2 正片"),
            ],
        )
    )
    append_completed_page_task(repository, asset.video_id, 1, "P1 开场")
    app.state.task_repository = repository
    app.state.task_worker = worker

    response = create_video_tasks_batch(
        asset.video_id,
        type("Body", (), {"page_numbers": [1, 2], "confirm": False})(),
        type("Request", (), {"app": app})(),
    )

    assert response.requires_confirmation is True
    assert response.created_tasks == []
    assert len(response.conflict_pages) == 1
    assert response.conflict_pages[0].page_number == 1
    assert worker.submitted == []


def test_create_video_resummary_tasks_batch_creates_new_tasks_after_confirmation(
    monkeypatch,
) -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-batch-resummary",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-batch-resummary",
            pages=[
                make_page("BV-batch-resummary", 1, "P1 开场"),
                make_page("BV-batch-resummary", 2, "P2 正片"),
            ],
        )
    )
    append_completed_page_task(repository, asset.video_id, 1, "P1 开场")
    app.state.task_repository = repository
    app.state.task_worker = worker
    monkeypatch.setattr(
        "video_sum_service.routers.videos.load_task_segments",
        lambda _path: [{"start": 0, "text": "hello"}],
    )

    preview = create_video_resummary_tasks_batch(
        asset.video_id,
        type("Body", (), {"page_numbers": [1, 2], "confirm": False})(),
        type("Request", (), {"app": app})(),
    )
    confirmed = create_video_resummary_tasks_batch(
        asset.video_id,
        type("Body", (), {"page_numbers": [1, 2], "confirm": True})(),
        type("Request", (), {"app": app})(),
    )

    assert preview.requires_confirmation is True
    assert len(preview.conflict_pages) == 1
    assert preview.conflict_pages[0].page_number == 1
    assert confirmed.requires_confirmation is False
    assert len(confirmed.created_tasks) == 1
    assert confirmed.created_tasks[0].page_number == 1
    assert len(confirmed.skipped_pages) == 1
    assert confirmed.skipped_pages[0].page_number == 2
    assert worker.submitted[-1].task_input.input_type is InputType.TRANSCRIPT_TEXT


def test_create_video_aggregate_summary_uses_only_completed_pages_by_default() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-aggregate",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-aggregate",
            pages=[
                make_page("BV-aggregate", 1, "P1 开场"),
                make_page("BV-aggregate", 2, "P2 正片"),
                make_page("BV-aggregate", 3, "P3 未完成"),
            ],
        )
    )
    append_completed_page_task(repository, asset.video_id, 1, "P1 开场")
    append_completed_page_task(repository, asset.video_id, 2, "P2 正片")
    repository.create_task(
        TaskInput(
            input_type=InputType.URL,
            source="https://example.com/video?p=3",
            title="P3 未完成",
            platform_hint="bilibili",
        ),
        video_id=asset.video_id,
        page_number=3,
        page_title="P3 未完成",
    )
    app.state.task_repository = repository
    app.state.task_worker = worker

    response = create_video_aggregate_summary_task(
        asset.video_id,
        type("Request", (), {"app": app})(),
    )

    assert response.page_number == 0
    assert response.page_title == "全集总结"
    assert response.input_type == InputType.TRANSCRIPT_TEXT.value
    assert len(worker.submitted) == 1
    submitted = worker.submitted[0]
    payload = json.loads(submitted.task_input.source)
    assert submitted.page_number == 0
    assert submitted.page_title == "全集总结"
    assert payload["title"] == "测试合集｜全集总结"
    assert payload["source_kind"] == "aggregate_series"
    assert "P1 开场 overview" in payload["transcript"]
    assert "P2 正片 key point" in payload["transcript"]
    assert "[重点-概览]" in payload["transcript"]
    assert "[00:00] hello" not in payload["transcript"]
    assert "转写文本" not in payload["transcript"]
    assert "P3 未完成" not in payload["transcript"]
    assert payload["segments"][0]["start"] == 1.0
    assert any("P1 开场 chapter summary" in item["text"] for item in payload["segments"])


def test_create_video_aggregate_summary_rejects_only_unfinished_selected_pages() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-aggregate-empty",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-aggregate-empty",
            pages=[
                make_page("BV-aggregate-empty", 1, "P1 开场"),
                make_page("BV-aggregate-empty", 2, "P2 未完成"),
            ],
        )
    )
    append_completed_page_task(repository, asset.video_id, 1, "P1 开场")
    app.state.task_repository = repository
    app.state.task_worker = worker

    with pytest.raises(HTTPException) as exc_info:
        create_video_aggregate_summary_task(
            asset.video_id,
            type("Request", (), {"app": app})(),
            type("Body", (), {"page_numbers": [2]})(),
        )

    assert "还没有可汇总的分 P 摘要" in str(exc_info.value)
    assert worker.submitted == []


def test_create_video_tasks_batch_ignores_aggregate_summary_task_conflicts() -> None:
    repository = create_repository()
    worker = FakeTaskWorker()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-batch-aggregate-ignore",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV-batch-aggregate-ignore",
            pages=[
                make_page("BV-batch-aggregate-ignore", 1, "P1 开场"),
            ],
        )
    )
    aggregate = repository.create_task(
        TaskInput(
            input_type=InputType.TRANSCRIPT_TEXT,
            source='{"transcript":"合集","segments":[{"text":"合集"}]}',
            title="全集总结",
        ),
        video_id=asset.video_id,
        page_number=0,
        page_title="全集总结",
    )
    repository.update_status(aggregate.task_id, TaskStatus.COMPLETED)
    repository.save_result(aggregate.task_id, TaskResult(overview="全集总结", transcript_text="合集"))
    app.state.task_repository = repository
    app.state.task_worker = worker

    response = create_video_tasks_batch(
        asset.video_id,
        type("Body", (), {"page_numbers": [1], "confirm": False})(),
        type("Request", (), {"app": app})(),
    )

    assert response.requires_confirmation is False
    assert len(response.created_tasks) == 1
    assert response.created_tasks[0].page_number == 1
