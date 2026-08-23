from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers import videos as videos_router
from video_sum_service.schemas import (
    VideoAssetRecord,
    VideoCollectionItemsOrderRequest,
    VideoCollectionTaskCreateRequest,
)


def create_repository() -> tuple[SqliteTaskRepository, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository, connection


def add_collection_video(
    repository: SqliteTaskRepository,
    bvid: str,
    title: str,
) -> VideoAssetRecord:
    return repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id=bvid,
            platform="bilibili",
            title=title,
            source_url=f"https://www.bilibili.com/video/{bvid}/",
            is_global_visible=False,
        )
    )


def collection_item(position: int, bvid: str, title: str | None = None) -> dict[str, object]:
    return {
        "position": position,
        "bvid": bvid,
        "title": title or bvid,
        "source_url": f"https://www.bilibili.com/video/{bvid}/",
        "source_section_id": "section-1",
        "source_section_title": "第一章",
        "source_section_position": 1,
    }


def test_old_collection_database_migrates_with_safe_view_defaults() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    now = datetime.now(UTC).isoformat()
    connection.executescript(
        """
        CREATE TABLE video_collections (
            collection_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'bilibili',
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            cover_url TEXT,
            auto_check_on_open INTEGER NOT NULL DEFAULT 1,
            last_checked_at TEXT,
            pending_items_json TEXT NOT NULL DEFAULT '[]',
            visibility_initialized INTEGER NOT NULL DEFAULT 0,
            global_order REAL NOT NULL DEFAULT 0,
            owner_mid TEXT,
            owner_name TEXT,
            owner_face TEXT,
            owner_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE video_collection_items (
            collection_id TEXT NOT NULL,
            bvid TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            cover_url TEXT,
            duration REAL,
            video_id TEXT,
            is_promoted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, bvid)
        );
        """
    )
    connection.execute(
        """
        INSERT INTO video_collections (
            collection_id, title, source_url, created_at, updated_at
        ) VALUES ('legacy', '旧合集', 'https://example.com/legacy', ?, ?)
        """,
        (now, now),
    )
    connection.execute(
        """
        INSERT INTO video_collection_items (
            collection_id, bvid, position, title, source_url, created_at, updated_at
        ) VALUES ('legacy', 'BV-legacy', 7, '旧视频', 'https://example.com/video', ?, ?)
        """,
        (now, now),
    )
    connection.commit()

    repository = SqliteTaskRepository(connection)
    repository.initialize()
    collection = repository.get_video_collection("legacy")

    collection_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(video_collections)")
    }
    item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(video_collection_items)")
    }
    assert {"view_sort_mode", "view_group_mode"} <= collection_columns
    assert {
        "custom_position",
        "source_section_id",
        "source_section_title",
        "source_section_position",
        "source_present",
        "removed_from_source_at",
    } <= item_columns
    assert collection is not None
    assert collection.view_sort_mode == "source"
    assert collection.view_group_mode == "none"
    assert collection.items[0].custom_position == 7
    assert connection.execute(
        "SELECT source_present FROM video_collection_items WHERE bvid = 'BV-legacy'"
    ).fetchone()["source_present"] == 1


def test_refresh_soft_removes_restores_members_and_cleans_stale_pending() -> None:
    repository, connection = create_repository()
    add_collection_video(repository, "BV-a", "A")
    add_collection_video(repository, "BV-b", "B")
    add_collection_video(repository, "BV-c", "C")
    add_collection_video(repository, "BV-stale", "Stale")
    repository.upsert_video_collection(
        {
            "collection_id": "season-removal",
            "title": "成员移除",
            "items": [collection_item(1, "BV-a"), collection_item(2, "BV-b")],
        },
        "https://example.com/season-removal",
    )
    repository.reorder_video_collection_items(
        "season-removal",
        ["BV-b", "BV-a"],
    )

    repository.refresh_video_collection(
        {
            "collection_id": "season-removal",
            "title": "成员移除",
            "items": [
                collection_item(1, "BV-a"),
                collection_item(2, "BV-b"),
                collection_item(3, "BV-c"),
                collection_item(4, "BV-stale"),
            ],
        },
        "https://example.com/season-removal",
    )
    repository.refresh_video_collection(
        {
            "collection_id": "season-removal",
            "title": "成员移除",
            "items": [collection_item(1, "BV-b"), collection_item(2, "BV-c")],
        },
        "https://example.com/season-removal",
    )

    removed = repository.get_video_collection("season-removal")
    assert removed is not None
    assert [item.bvid for item in removed.items] == ["BV-b"]
    assert [item.bvid for item in removed.new_items] == ["BV-c"]
    row = connection.execute(
        """
        SELECT source_present, removed_from_source_at, custom_position
        FROM video_collection_items
        WHERE collection_id = 'season-removal' AND bvid = 'BV-a'
        """
    ).fetchone()
    assert row is not None
    assert row["source_present"] == 0
    assert row["removed_from_source_at"] is not None
    assert row["custom_position"] == 2

    restored = repository.refresh_video_collection(
        {
            "collection_id": "season-removal",
            "title": "成员恢复",
            "items": [
                collection_item(1, "BV-b"),
                collection_item(2, "BV-a"),
                collection_item(3, "BV-c"),
            ],
        },
        "https://example.com/season-removal",
    )
    assert [item.bvid for item in restored.items] == ["BV-b", "BV-a"]
    assert [item.bvid for item in restored.new_items] == ["BV-c"]
    restored_row = connection.execute(
        """
        SELECT source_present, removed_from_source_at, custom_position
        FROM video_collection_items
        WHERE collection_id = 'season-removal' AND bvid = 'BV-a'
        """
    ).fetchone()
    assert restored_row is not None
    assert restored_row["source_present"] == 1
    assert restored_row["removed_from_source_at"] is None
    assert restored_row["custom_position"] == 2


def test_custom_order_is_independent_from_refreshed_source_order_and_new_items_append() -> None:
    repository, _connection = create_repository()
    add_collection_video(repository, "BV-a", "A")
    add_collection_video(repository, "BV-b", "B")
    add_collection_video(repository, "BV-c", "C")
    repository.upsert_video_collection(
        {
            "collection_id": "season-order",
            "title": "顺序测试",
            "items": [collection_item(1, "BV-a"), collection_item(2, "BV-b")],
        },
        "https://example.com/season-order",
    )

    reordered = repository.reorder_video_collection_items(
        "season-order",
        ["BV-b", "BV-a"],
    )
    assert reordered is not None
    assert [item.bvid for item in reordered.items] == ["BV-a", "BV-b"]
    assert all(item.asset_updated_at is not None for item in reordered.items)
    assert {item.bvid: item.custom_position for item in reordered.items} == {
        "BV-a": 2,
        "BV-b": 1,
    }

    refreshed = repository.refresh_video_collection(
        {
            "collection_id": "season-order",
            "title": "顺序测试",
            "items": [
                {
                    **collection_item(1, "BV-b", "B 新标题"),
                    "source_section_id": "section-2",
                    "source_section_title": "第二章",
                    "source_section_position": 2,
                },
                collection_item(2, "BV-a"),
                collection_item(3, "BV-c"),
            ],
        },
        "https://example.com/season-order",
    )
    assert [item.bvid for item in refreshed.items] == ["BV-b", "BV-a"]
    assert {item.bvid: item.custom_position for item in refreshed.items} == {
        "BV-a": 2,
        "BV-b": 1,
    }
    assert refreshed.items[0].source_section_title == "第二章"
    assert [item.bvid for item in refreshed.new_items] == ["BV-c"]

    repository.add_pending_video_collection_items("season-order", ["BV-c"])
    accepted = repository.get_video_collection("season-order")
    assert accepted is not None
    assert [item.bvid for item in accepted.items] == ["BV-b", "BV-a", "BV-c"]
    assert {item.bvid: item.custom_position for item in accepted.items} == {
        "BV-a": 2,
        "BV-b": 1,
        "BV-c": 3,
    }


def test_collection_view_settings_persist_and_dirty_values_fall_back() -> None:
    repository, connection = create_repository()
    repository.upsert_video_collection(
        {"collection_id": "season-view", "title": "视图", "items": []},
        "https://example.com/season-view",
    )

    updated = repository.update_video_collection_settings(
        "season-view",
        view_sort_mode="duration",
        view_group_mode="source_section",
    )
    assert updated is not None
    assert updated.auto_check_on_open is True
    assert updated.view_sort_mode == "duration"
    assert updated.view_group_mode == "source_section"

    legacy_update = repository.update_video_collection_settings(
        "season-view",
        auto_check_on_open=False,
    )
    assert legacy_update is not None
    assert legacy_update.auto_check_on_open is False
    assert legacy_update.view_sort_mode == "duration"
    assert legacy_update.view_group_mode == "source_section"

    connection.execute(
        """
        UPDATE video_collections
        SET view_sort_mode = 'unknown-sort', view_group_mode = 'unknown-group'
        WHERE collection_id = 'season-view'
        """
    )
    connection.commit()
    fallback = repository.get_video_collection("season-view")
    assert fallback is not None
    assert fallback.view_sort_mode == "source"
    assert fallback.view_group_mode == "none"


def test_custom_reorder_requires_the_exact_item_set() -> None:
    repository, _connection = create_repository()
    repository.upsert_video_collection(
        {
            "collection_id": "season-validation",
            "title": "校验",
            "items": [collection_item(1, "BV-a"), collection_item(2, "BV-b")],
        },
        "https://example.com/season-validation",
    )

    with pytest.raises(ValueError, match="duplicates"):
        repository.reorder_video_collection_items(
            "season-validation",
            ["BV-a", "BV-a"],
        )
    with pytest.raises(ValueError, match="every current collection item"):
        repository.reorder_video_collection_items("season-validation", ["BV-a"])
    assert repository.reorder_video_collection_items("missing", []) is None


def test_custom_reorder_route_uses_clear_404_and_422_errors() -> None:
    repository, _connection = create_repository()
    repository.upsert_video_collection(
        {
            "collection_id": "season-route-order",
            "title": "路由校验",
            "items": [collection_item(1, "BV-a"), collection_item(2, "BV-b")],
        },
        "https://example.com/season-route-order",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_repository=repository))
    )

    with pytest.raises(HTTPException) as incomplete_error:
        videos_router.reorder_video_collection_items(
            "season-route-order",
            VideoCollectionItemsOrderRequest(ordered_bvids=["BV-a"]),
            request,
        )
    with pytest.raises(HTTPException) as missing_error:
        videos_router.reorder_video_collection_items(
            "missing",
            VideoCollectionItemsOrderRequest(ordered_bvids=[]),
            request,
        )

    assert incomplete_error.value.status_code == 422
    assert missing_error.value.status_code == 404


def test_bilibili_collection_payload_preserves_source_sections(monkeypatch) -> None:
    monkeypatch.setattr(
        "video_sum_service.video_assets._fetch_bilibili_initial_state_payload",
        lambda _url: {
            "videoData": {
                "ugc_season": {
                    "id": 42,
                    "title": "分区合集",
                    "sections": [
                        {
                            "id": 7,
                            "title": "上篇",
                            "episodes": [
                                {"bvid": "BV1one111111", "arc": {"title": "第一集"}},
                                {"bvid": "BV1two222222", "arc": {"title": "第二集"}},
                            ],
                        },
                        {
                            "id": 8,
                            "title": "下篇",
                            "episodes": [
                                {"bvid": "BV1thr333333", "arc": {"title": "第三集"}},
                            ],
                        },
                    ],
                }
            }
        },
    )
    monkeypatch.setattr(
        "video_sum_service.video_assets.cache_cover_image",
        lambda url, *_args, **_kwargs: url,
    )

    payload = videos_router.fetch_bilibili_collection_payload(
        "https://www.bilibili.com/video/BV1one111111/"
    )

    assert payload is not None
    assert [item["source_section_id"] for item in payload["items"]] == ["7", "7", "8"]
    assert [item["source_section_title"] for item in payload["items"]] == ["上篇", "上篇", "下篇"]
    assert [item["source_section_position"] for item in payload["items"]] == [1, 1, 2]


def test_collection_task_batch_freezes_requested_order_and_options() -> None:
    repository, connection = create_repository()
    first = add_collection_video(repository, "BV-first", "第一集")
    second = add_collection_video(repository, "BV-second", "第二集")
    repository.upsert_video_collection(
        {
            "collection_id": "season-tasks",
            "title": "任务顺序",
            "items": [
                collection_item(1, "BV-first"),
                collection_item(2, "BV-second"),
            ],
        },
        "https://example.com/season-tasks",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_repository=repository))
    )

    response = videos_router.create_video_collection_tasks(
        "season-tasks",
        VideoCollectionTaskCreateRequest(
            ordered_video_ids=[second.video_id, first.video_id],
            visual_note_mode="frame_insert",
            prompt_preset_id="preset-1",
            summary_scope="summary",
        ),
        request,
    )

    assert [task.video_id for task in response.created_tasks] == [
        second.video_id,
        first.video_id,
    ]
    replay = videos_router.create_video_collection_tasks(
        "season-tasks",
        VideoCollectionTaskCreateRequest(
            ordered_video_ids=[second.video_id, first.video_id],
            visual_note_mode="frame_insert",
            prompt_preset_id="preset-1",
            summary_scope="summary",
        ),
        request,
    )
    assert response.replayed is False
    assert replay.replayed is True
    assert replay.batch_id == response.batch_id
    assert [task.task_id for task in replay.created_tasks] == [
        task.task_id for task in response.created_tasks
    ]
    assert connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()[
        "count"
    ] == 2
    assert any(
        route.path == "/api/v1/videos/collections/{collection_id}/tasks/batch"
        for route in videos_router.router.routes
    )
    for task in response.created_tasks:
        record = repository.get_task(task.task_id)
        assert record is not None
        assert record.task_input.options.visual_note_mode == "frame_insert"
        assert record.task_input.options.prompt_preset_id == "preset-1"
        assert record.task_input.options.summary_scope == "summary"


def test_collection_task_batch_rolls_back_if_second_record_cannot_persist(
    monkeypatch,
) -> None:
    repository, connection = create_repository()
    first = add_collection_video(repository, "BV-atomic-first", "第一集")
    second = add_collection_video(repository, "BV-atomic-second", "第二集")
    repository.upsert_video_collection(
        {
            "collection_id": "season-atomic",
            "title": "原子批次",
            "items": [
                collection_item(1, "BV-atomic-first"),
                collection_item(2, "BV-atomic-second"),
            ],
        },
        "https://example.com/season-atomic",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_repository=repository))
    )
    original = repository._insert_task_record
    calls = 0

    def fail_second(cursor, record, serialized):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated second-item persistence failure")
        return original(cursor, record, serialized)

    monkeypatch.setattr(repository, "_insert_task_record", fail_second)
    with pytest.raises(RuntimeError, match="second-item persistence failure"):
        videos_router.create_video_collection_tasks(
            "season-atomic",
            VideoCollectionTaskCreateRequest(
                batch_id="atomic-batch",
                ordered_video_ids=[first.video_id, second.video_id],
            ),
            request,
        )

    assert connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()[
        "count"
    ] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM video_collection_task_batches"
    ).fetchone()["count"] == 0


def test_collection_task_batch_reports_dispatch_failure_and_retry_is_idempotent(
    monkeypatch,
) -> None:
    repository, connection = create_repository()
    first = add_collection_video(repository, "BV-dispatch-first", "第一集")
    second = add_collection_video(repository, "BV-dispatch-second", "第二集")
    repository.upsert_video_collection(
        {
            "collection_id": "season-dispatch",
            "title": "提交失败",
            "items": [
                collection_item(1, "BV-dispatch-first"),
                collection_item(2, "BV-dispatch-second"),
            ],
        },
        "https://example.com/season-dispatch",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_repository=repository))
    )
    submitted: list[str] = []

    def fail_second(_app_state, _repository, record):
        submitted.append(record.task_id)
        if len(submitted) == 2:
            raise RuntimeError("simulated worker submission failure")

    monkeypatch.setattr(videos_router, "submit_task_or_queue", fail_second)
    payload = VideoCollectionTaskCreateRequest(
        batch_id="dispatch-batch",
        ordered_video_ids=[first.video_id, second.video_id],
    )
    response = videos_router.create_video_collection_tasks(
        "season-dispatch",
        payload,
        request,
    )
    replay = videos_router.create_video_collection_tasks(
        "season-dispatch",
        payload,
        request,
    )
    with pytest.raises(HTTPException) as conflict:
        videos_router.create_video_collection_tasks(
            "season-dispatch",
            VideoCollectionTaskCreateRequest(
                batch_id="dispatch-batch",
                ordered_video_ids=[second.video_id, first.video_id],
            ),
            request,
        )

    assert len(submitted) == 2
    assert [task.task_id for task in replay.created_tasks] == [
        task.task_id for task in response.created_tasks
    ]
    assert replay.replayed is True
    assert len(response.dispatch_failures) == 1
    assert response.dispatch_failures[0].task_id == response.created_tasks[1].task_id
    assert response.created_tasks[1].status.value == "failed"
    assert conflict.value.status_code == 409
    assert connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()[
        "count"
    ] == 2


def test_collection_task_batch_reports_worker_rejection() -> None:
    repository, _connection = create_repository()
    video = add_collection_video(repository, "BV-worker-reject", "拒绝提交")
    repository.upsert_video_collection(
        {
            "collection_id": "season-worker-reject",
            "title": "队列关闭",
            "items": [collection_item(1, "BV-worker-reject")],
        },
        "https://example.com/season-worker-reject",
    )

    class RejectingWorker:
        def submit(self, _record):
            return "rejected"

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                task_repository=repository,
                task_worker=RejectingWorker(),
            )
        )
    )
    response = videos_router.create_video_collection_tasks(
        "season-worker-reject",
        VideoCollectionTaskCreateRequest(
            batch_id="worker-reject-batch",
            ordered_video_ids=[video.video_id],
        ),
        request,
    )

    assert len(response.dispatch_failures) == 1
    assert response.dispatch_failures[0].error_code == "task_dispatch_rejected"
    assert response.created_tasks[0].status.value == "failed"


@pytest.mark.parametrize(
    ("collection_id", "ordered_video_ids", "expected_status"),
    [
        ("missing", ["video-id"], 404),
        ("season-errors", ["duplicate", "duplicate"], 422),
        ("season-errors", ["outside"], 422),
    ],
)
def test_collection_task_batch_rejects_invalid_membership_and_duplicates(
    collection_id: str,
    ordered_video_ids: list[str],
    expected_status: int,
) -> None:
    repository, _connection = create_repository()
    member = add_collection_video(repository, "BV-member", "成员")
    repository.upsert_video_collection(
        {
            "collection_id": "season-errors",
            "title": "错误校验",
            "items": [collection_item(1, "BV-member")],
        },
        "https://example.com/season-errors",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_repository=repository))
    )
    ids = (
        [member.video_id, member.video_id]
        if ordered_video_ids[0] == "duplicate"
        else ordered_video_ids
    )

    with pytest.raises(HTTPException) as exc_info:
        videos_router.create_video_collection_tasks(
            collection_id,
            VideoCollectionTaskCreateRequest(ordered_video_ids=ids),
            request,
        )

    assert exc_info.value.status_code == expected_status
