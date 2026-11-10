"""C1: GET /api/v1/videos/{video_id}/reusable-task.

Driven through the HTTP API. Tasks are seeded straight into the repository in
the state under test (completed with a result, still running, failed, ...).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import video_sum_service.app as service_app
import video_sum_service.routers.system as system_router
from fastapi.testclient import TestClient
from video_sum_core.models.tasks import (
    InputType,
    ModelRouting,
    TaskInput,
    TaskOptions,
    TaskResult,
    TaskStatus,
)
from video_sum_service.app import app
from video_sum_service.auth import ACCESS_TOKEN_ENV, AccessTokenManager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import TaskRecord, VideoAssetRecord, VideoPageOptionResponse

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SOURCE = "https://www.bilibili.com/video/BV1reuse"


@pytest.fixture
def env(monkeypatch):
    original_repository = getattr(app.state, "task_repository", None)
    original_runtime_startup = getattr(app.state, "runtime_startup", None)
    manager = AccessTokenManager(Path("unused"), env={ACCESS_TOKEN_ENV: TOKEN})
    monkeypatch.setattr(service_app, "access_token_manager", manager)
    monkeypatch.setattr(system_router, "access_token_manager", manager)

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    app.state.task_repository = repository
    app.state.runtime_startup = None
    video = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1reuse",
            platform="bilibili",
            title="复用测试视频",
            source_url=SOURCE,
            pages=[
                VideoPageOptionResponse(page=1, title="P1", source_url=f"{SOURCE}?p=1"),
                VideoPageOptionResponse(page=2, title="P2", source_url=f"{SOURCE}?p=2"),
            ],
        )
    )
    yield type("Env", (), {"client": TestClient(app), "repo": repository, "video": video})()
    app.state.task_repository = original_repository
    app.state.runtime_startup = original_runtime_startup


_CLOCK = [datetime(2026, 1, 1, tzinfo=UTC)]


def seed(env, *, status=TaskStatus.COMPLETED, page=1, result=True, **options) -> str:
    _CLOCK[0] += timedelta(minutes=1)
    record = TaskRecord(
        task_input=TaskInput(
            input_type=InputType.URL,
            source=f"{SOURCE}?p={page}",
            title="复用测试视频",
            options=TaskOptions(**options),
        ),
        video_id=env.video.video_id,
        page_number=page,
        status=status,
        created_at=_CLOCK[0],
        updated_at=_CLOCK[0],
    )
    record = env.repo.create_task_record(record)
    env.repo.update_status(record.task_id, status)
    if result:
        env.repo.save_result(
            record.task_id, TaskResult(overview="已有总结", transcript_text="转写")
        )
        env.repo.update_status(record.task_id, status)
    return record.task_id


def lookup(env, **params):
    return env.client.get(
        f"/api/v1/videos/{env.video.video_id}/reusable-task",
        params={key: value for key, value in params.items() if value is not None},
        headers=HEADERS,
    )


def test_completed_task_with_same_options_is_reused(env) -> None:
    task_id = seed(env, page=1)

    response = lookup(env, page_number=1)

    assert response.status_code == 200
    body = response.json()
    assert body["reused"] is True
    assert body["match"] == "completed"
    assert body["task"]["task_id"] == task_id
    assert body["task"]["result"]["overview"] == "已有总结"


def test_no_task_returns_404(env) -> None:
    assert lookup(env, page_number=1).status_code == 404


def test_unknown_video_returns_404(env) -> None:
    response = env.client.get("/api/v1/videos/missing/reusable-task", headers=HEADERS)
    assert response.status_code == 404


def test_other_page_is_not_reused(env) -> None:
    seed(env, page=2)
    assert lookup(env, page_number=1).status_code == 404


def test_missing_page_number_means_page_one(env) -> None:
    task_id = seed(env, page=1)
    assert lookup(env).json()["task"]["task_id"] == task_id


@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        ({"prompt_preset_id": "preset-a"}, {"prompt_preset_id": "preset-b"}),
        ({"prompt_preset_id": "preset-a"}, {}),
        ({}, {"prompt_preset_id": "preset-a"}),
        ({"language": "en"}, {"language": "zh"}),
        ({"language": "en"}, {}),
        ({"extra_instructions": "关注数据"}, {}),
        ({}, {"extra_instructions": "关注数据"}),
        ({"extra_instructions": "关注数据"}, {"extra_instructions": "关注结论"}),
        ({"summary_scope": "summary"}, {"summary_scope": "knowledge_note"}),
        ({"summary_scope": "summary"}, {}),
        ({"summary_scope": "transcript_only"}, {}),
        ({"summary_scope": "knowledge_note"}, {"summary_scope": "transcript_only"}),
    ],
)
def test_differing_options_are_not_reused(env, stored, requested) -> None:
    seed(env, **stored)
    assert lookup(env, page_number=1, **requested).status_code == 404


@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        ({"prompt_preset_id": "preset-a"}, {"prompt_preset_id": "preset-a"}),
        ({"language": "en"}, {"language": "en"}),
        ({}, {"language": "zh"}),
        ({"extra_instructions": "关注数据"}, {"extra_instructions": " 关注数据 "}),
        ({"summary_scope": "summary"}, {"summary_scope": "summary"}),
        ({"summary_scope": "transcript_only"}, {"summary_scope": "transcript_only"}),
    ],
)
def test_equal_options_are_reused(env, stored, requested) -> None:
    task_id = seed(env, **stored)
    assert lookup(env, page_number=1, **requested).json()["task"]["task_id"] == task_id


def test_full_summary_satisfies_brief_request(env) -> None:
    task_id = seed(env, summary_scope="knowledge_note")
    assert lookup(env, page_number=1, summary_scope="summary").json()["task"]["task_id"] == task_id


def test_engine_compared_only_when_explicit(env) -> None:
    builtin_id = seed(env, engine="builtin")

    assert lookup(env, page_number=1).json()["task"]["task_id"] == builtin_id
    assert lookup(env, page_number=1, engine="builtin").json()["task"]["task_id"] == builtin_id
    assert lookup(env, page_number=1, engine="agent").status_code == 404


def test_explicit_agent_request_joins_task_routed_to_agent_by_default(env) -> None:
    task_id = seed(
        env,
        status=TaskStatus.AGENT_SUMMARIZING,
        result=False,
        model_routing=ModelRouting(engine="agent", agent_capabilities=["text"], agent_steps=["summary"]),
    )

    body = lookup(env, page_number=1, engine="agent").json()

    assert body["match"] == "in_progress"
    assert body["task"]["task_id"] == task_id
    assert lookup(env, page_number=1, engine="builtin").status_code == 404


def test_explicit_builtin_request_reuses_task_routed_to_builtin_by_default(env) -> None:
    task_id = seed(env, model_routing=ModelRouting(engine="builtin"))

    assert lookup(env, page_number=1, engine="builtin").json()["task"]["task_id"] == task_id
    assert lookup(env, page_number=1, engine="agent").status_code == 404


def test_visual_note_mode_compared_only_when_explicit(env) -> None:
    task_id = seed(env, visual_note_mode="text")

    assert lookup(env, page_number=1).json()["task"]["task_id"] == task_id
    assert lookup(env, page_number=1, visual_note_mode="frame_insert").status_code == 404


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_in_progress_task_is_attached(env, status) -> None:
    task_id = seed(env, status=status, result=False)

    body = lookup(env, page_number=1).json()

    assert body["match"] == "in_progress"
    assert body["task"]["task_id"] == task_id
    assert body["task"]["status"] == status.value
    assert "batch_id" in body


@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_failed_or_cancelled_task_is_not_reused(env, status) -> None:
    seed(env, status=status, result=False)
    assert lookup(env, page_number=1).status_code == 404


def test_completed_task_without_result_is_not_reused(env) -> None:
    seed(env, result=False)
    assert lookup(env, page_number=1).status_code == 404


def test_completed_wins_over_newer_in_progress(env) -> None:
    completed_id = seed(env)
    seed(env, status=TaskStatus.RUNNING, result=False)
    assert lookup(env, page_number=1).json()["task"]["task_id"] == completed_id


def test_newest_completed_task_wins(env) -> None:
    seed(env)
    newest = seed(env)
    assert lookup(env, page_number=1).json()["task"]["task_id"] == newest


def test_exact_scope_preferred_over_full_summary_for_brief(env) -> None:
    brief_id = seed(env, summary_scope="summary")
    seed(env, summary_scope="knowledge_note")
    assert lookup(env, page_number=1, summary_scope="summary").json()["task"]["task_id"] == brief_id
