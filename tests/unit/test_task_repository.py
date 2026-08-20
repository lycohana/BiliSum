from concurrent.futures import ThreadPoolExecutor
import sqlite3
from datetime import timezone

from video_sum_core.models.tasks import (
    InputType,
    LLMUsageRecord,
    TaskInput,
    TaskResult,
    TaskStatus,
    merge_llm_usage_records,
)
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord


def test_repository_create_and_fetch_task() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(
        TaskInput(input_type=InputType.URL, source="https://example.com/video")
    )
    fetched = repository.get_task(record.task_id)

    assert fetched is not None
    assert fetched.task_id == record.task_id
    assert fetched.status == TaskStatus.QUEUED


def test_repository_upserts_video_asset() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-test",
            platform="bilibili",
            title="示例视频",
            source_url="https://www.bilibili.com/video/BV-test",
            cover_url="https://example.com/cover.jpg",
            duration=120.0,
        )
    )
    fetched = repository.get_video_asset(asset.video_id)

    assert fetched is not None
    assert fetched.title == "示例视频"
    assert fetched.cover_url.endswith("cover.jpg")
    assert fetched.is_favorite is False
    assert fetched.favorite_updated_at is None


def test_repository_updates_status() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com"))
    updated = repository.update_status(record.task_id, TaskStatus.RUNNING)

    assert updated is not None
    assert updated.status == TaskStatus.RUNNING


def test_repository_saves_result_and_events() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com"))
    repository.append_event(record.task_id, stage="running", progress=50, message="处理中")
    repository.save_result(
        record.task_id,
        TaskResult(
            transcript_text="hello",
            knowledge_note_markdown="# hello",
            chapter_groups=[{"title": "大章节 1", "children": [{"title": "章节 1", "start": 0, "summary": "摘要"}]}],
            llm_total_tokens=321,
            mindmap_status="ready",
            mindmap_artifact_path="C:/tmp/mindmap.json",
        ),
    )

    fetched = repository.get_task(record.task_id)
    events = repository.list_events(record.task_id)
    listed = repository.list_tasks()

    assert fetched is not None
    assert fetched.result is not None
    assert fetched.result.transcript_text == "hello"
    assert fetched.result.knowledge_note_markdown == "# hello"
    assert fetched.result.chapter_groups[0]["title"] == "大章节 1"
    assert fetched.result.llm_total_tokens == 321
    assert fetched.result.mindmap_status == "ready"
    assert fetched.result.mindmap_artifact_path == "C:/tmp/mindmap.json"
    assert listed[0].result is not None
    assert listed[0].result.llm_total_tokens == 321
    assert len(events) == 1
    assert events[0].stage == "running"


def test_repository_updates_result_atomically() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com"))
    repository.save_result(record.task_id, TaskResult())

    def append_usage(stage: str):
        return repository.update_result(
            record.task_id,
            lambda current: merge_llm_usage_records(
                current,
                [LLMUsageRecord(stage=stage, total_tokens=10)],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(append_usage, ["mindmap", "visual_note"]))

    fetched = repository.get_task(record.task_id)
    assert fetched is not None
    assert fetched.result is not None
    assert {item.stage for item in fetched.result.llm_usage_breakdown} == {"mindmap", "visual_note"}
    assert fetched.result.llm_total_tokens == 20


def test_task_summary_includes_duration_and_llm_tokens() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com"))
    repository.save_result(record.task_id, TaskResult(llm_total_tokens=456))
    repository.update_status(record.task_id, TaskStatus.COMPLETED)

    fetched = repository.get_task(record.task_id)

    assert fetched is not None
    summary = fetched.to_summary()
    assert summary.llm_total_tokens == 456
    assert summary.task_duration_seconds is not None
    assert summary.task_duration_seconds >= 0


def test_repository_lists_incremental_events() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com"))
    first = repository.append_event(record.task_id, stage="queued", progress=0, message="排队中")
    repository.append_event(record.task_id, stage="running", progress=50, message="处理中")

    events = repository.list_events_after(record.task_id, first.created_at.isoformat())

    assert len(events) == 1
    assert events[0].stage == "running"


def test_repository_deletes_task() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-delete",
            platform="bilibili",
            title="待删除视频",
            source_url="https://www.bilibili.com/video/BV-delete",
        )
    )
    record = repository.create_task(
        TaskInput(input_type=InputType.URL, source="https://example.com"),
        video_id=asset.video_id,
    )
    repository.append_event(record.task_id, stage="queued", progress=0, message="排队中")

    deleted = repository.delete_task(record.task_id)

    assert deleted is True
    assert repository.get_task(record.task_id) is None


def test_repository_consolidates_legacy_page_asset_into_single_bvid() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    legacy = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-merge?p=1",
            platform="bilibili",
            title="旧版 P1 资产",
            source_url="https://www.bilibili.com/video/BV-merge?p=1",
        )
    )
    repository.create_task(
        TaskInput(input_type=InputType.URL, source="https://www.bilibili.com/video/BV-merge?p=1", title="P1"),
        video_id=legacy.video_id,
    )

    merged = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-merge",
            platform="bilibili",
            title="基础 BV 资产",
            source_url="https://www.bilibili.com/video/BV-merge",
        )
    )

    videos = repository.list_video_assets()
    tasks = repository.list_tasks_for_video(merged.video_id)

    assert merged.video_id == legacy.video_id
    assert merged.canonical_id == "BV-merge"
    assert len(videos) == 1
    assert len(tasks) == 1
    assert tasks[0].video_id == merged.video_id


def test_repository_persists_task_page_metadata() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(
        TaskInput(input_type=InputType.TRANSCRIPT_TEXT, source="{\"transcript\":\"x\"}", title="P3 重摘要"),
        video_id="video-1",
        page_number=3,
        page_title="P3 重摘要",
    )

    fetched = repository.get_task(record.task_id)

    assert fetched is not None
    assert fetched.page_number == 3
    assert fetched.page_title == "P3 重摘要"
    assert fetched.to_summary().page_number == 3


def test_task_summary_does_not_infer_page_number_for_youtube_url() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    record = repository.create_task(
        TaskInput(
            input_type=InputType.URL,
            source="https://www.youtube.com/watch?v=dQw4w9WgXcQ&p=2",
            title="YouTube 示例",
        ),
        video_id="video-youtube",
    )

    fetched = repository.get_task(record.task_id)

    assert fetched is not None
    assert fetched.page_number is None
    assert fetched.to_summary().page_number is None


def test_repository_upserts_youtube_video_asset() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="dQw4w9WgXcQ",
            platform="youtube",
            title="YouTube 视频",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            cover_url="https://example.com/youtube.jpg",
            duration=212.0,
        )
    )

    fetched = repository.get_video_asset(asset.video_id)

    assert fetched is not None
    assert fetched.platform == "youtube"
    assert fetched.pages == []
    assert fetched.canonical_id == "dQw4w9WgXcQ"


def test_repository_sets_video_favorite_without_touching_updated_at() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-favorite",
            platform="bilibili",
            title="收藏视频",
            source_url="https://www.bilibili.com/video/BV-favorite",
        )
    )

    before = repository.get_video_asset(asset.video_id)
    assert before is not None

    favorited = repository.set_video_favorite(asset.video_id, True)

    assert favorited is not None
    assert favorited.is_favorite is True
    assert favorited.favorite_updated_at is not None
    assert favorited.favorite_updated_at.tzinfo == timezone.utc
    assert favorited.updated_at == before.updated_at

    unfavorited = repository.set_video_favorite(asset.video_id, False)

    assert unfavorited is not None
    assert unfavorited.is_favorite is False
    assert unfavorited.favorite_updated_at is None
    assert unfavorited.updated_at == before.updated_at


def test_repository_initialize_adds_favorite_columns_to_legacy_database() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    cursor.execute(
        """
        CREATE TABLE video_assets (
            video_id TEXT PRIMARY KEY,
            canonical_id TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            cover_url TEXT,
            duration REAL,
            page_catalog_json TEXT NOT NULL DEFAULT '[]',
            latest_task_id TEXT,
            latest_status TEXT,
            latest_stage TEXT,
            latest_error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            video_id TEXT,
            status TEXT NOT NULL,
            task_input_json TEXT NOT NULL,
            page_number INTEGER,
            page_title TEXT,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE task_results (
            task_id TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE task_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            progress INTEGER NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()

    repository = SqliteTaskRepository(connection)
    repository.initialize()

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(video_assets)").fetchall()
    }

    assert "is_favorite" in columns
    assert "favorite_updated_at" in columns


def test_repository_tracks_collection_membership_and_pending_refresh_items() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    visible = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-visible", title="外层视频", source_url="https://example.com/visible")
    )
    internal = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV-internal",
            title="合集视频",
            source_url="https://example.com/internal",
            is_global_visible=False,
        )
    )
    payload = {
        "collection_id": "season-1",
        "title": "测试合集",
        "cover_url": "/media/covers/season-1.jpg",
        "items": [
            {"position": 1, "bvid": "BV-visible", "title": "外层视频", "source_url": visible.source_url},
            {"position": 2, "bvid": "BV-internal", "title": "合集视频", "source_url": internal.source_url},
        ],
    }
    collection = repository.upsert_video_collection(payload, visible.source_url)
    assert collection.item_count == 2
    assert len(repository.list_video_assets()) == 1

    repository.initialize_video_collection_visibility("season-1")
    assert len(repository.list_video_assets()) == 0
    assert repository.get_video_collection("season-1").items[0].is_global_visible is False

    refreshed = repository.refresh_video_collection(
        {**payload, "items": [*payload["items"], {"position": 3, "bvid": "BV-new", "title": "新视频", "source_url": "https://example.com/new"}]},
        visible.source_url,
    )
    assert [item.bvid for item in refreshed.new_items] == ["BV-new"]

    repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-new", title="新视频", source_url="https://example.com/new", is_global_visible=False)
    )
    repository.add_pending_video_collection_items("season-1", ["BV-new"])
    assert repository.get_video_collection("season-1").item_count == 3

    repository.promote_video_collection_item("season-1", internal.video_id)
    repository.initialize_video_collection_visibility("season-1")
    assert len(repository.list_video_assets()) == 1


def test_repository_reorders_mixed_outer_library_items() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    first = repository.upsert_video_asset(VideoAssetRecord(canonical_id="BV-first", title="第一个", source_url="https://example.com/first"))
    repository.upsert_video_collection(
        {
            "collection_id": "season-mixed",
            "title": "混合集合",
            "items": [],
        },
        "https://space.bilibili.com/1/lists/season-mixed",
    )
    repository.reorder_library_items([
        ("video", first.video_id),
        ("collection", "season-mixed"),
    ])

    videos = repository.list_video_assets()
    collections = repository.list_video_collections()
    assert videos[0].global_order < collections[0].global_order


def test_repository_collection_asset_actions_and_detach_delete() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()

    video = repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV-actions", title="合集视频", source_url="https://example.com/actions", is_global_visible=False)
    )
    folder = repository.create_video_folder("合集归档")
    repository.upsert_video_collection(
        {
            "collection_id": "season-actions",
            "title": "可操作合集",
            "items": [{"position": 1, "bvid": "BV-actions", "title": video.title, "source_url": video.source_url}],
        },
        "https://space.bilibili.com/1/lists/season-actions",
    )

    repository.set_video_collection_favorite("season-actions", True)
    repository.set_video_collection_folders("season-actions", [folder.folder_id])
    repository.set_video_collection_pin("season-actions", folder_pinned=True)
    collection = repository.get_video_collection("season-actions")
    assert collection is not None
    assert collection.is_favorite is True
    assert collection.folder_ids == [folder.folder_id]
    assert collection.folder_pinned is True

    assert repository.delete_video_collection("season-actions", detach_contents=True) == [video.video_id]
    assert repository.get_video_collection("season-actions") is None
    restored = repository.get_video_asset(video.video_id)
    assert restored is not None and restored.is_global_visible is True
