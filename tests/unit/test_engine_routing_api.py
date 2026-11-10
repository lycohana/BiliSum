"""F2-1: default engine, default agent capabilities and per-task model routing.

Driven through the HTTP API (TestClient). No worker is attached, so created
tasks stay queued; the routing is fixed at creation and read back from the
task detail (``model_routing``, ``batch_id``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import video_sum_service.app as service_app
import video_sum_service.context as service_context
import video_sum_service.routers.system as system_router
from fastapi.testclient import TestClient
from video_sum_infra.config import ServiceSettings
from video_sum_service.app import app
from video_sum_service.auth import ACCESS_TOKEN_ENV, AccessTokenManager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord, VideoPageOptionResponse

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SOURCE = "https://www.bilibili.com/video/BV1route"
# Steps a text-only Agent takes (STEPS order); vision adds visual_frame (F3).
TEXT_STEPS = ["summary", "knowledge_note", "keyframe_plan", "visual_compose", "mindmap"]
ALL_STEPS = [
    "summary",
    "knowledge_note",
    "keyframe_plan",
    "visual_frame",
    "visual_compose",
    "mindmap",
]


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    original = {
        key: getattr(app.state, key, None)
        for key in ("task_repository", "task_worker", "runtime_startup")
    }
    manager = AccessTokenManager(Path("unused"), env={ACCESS_TOKEN_ENV: TOKEN})
    monkeypatch.setattr(service_app, "access_token_manager", manager)
    monkeypatch.setattr(system_router, "access_token_manager", manager)

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    app.state.task_repository = repository
    app.state.task_worker = None
    app.state.runtime_startup = None
    video = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1route",
            platform="bilibili",
            title="路由测试视频",
            source_url=SOURCE,
            pages=[
                VideoPageOptionResponse(page=1, title="P1", source_url=f"{SOURCE}?p=1"),
                VideoPageOptionResponse(page=2, title="P2", source_url=f"{SOURCE}?p=2"),
            ],
        )
    )

    def configure(**settings: object) -> None:
        values = {
            "data_dir": tmp_path,
            "cache_dir": tmp_path / "cache",
            "tasks_dir": tmp_path / "tasks",
            **settings,
        }
        monkeypatch.setattr(
            service_context.settings_manager, "_settings", ServiceSettings(_env_file=None, **values)
        )

    configure()
    yield type(
        "Env",
        (),
        {
            "client": TestClient(app),
            "repo": repository,
            "video": video,
            "configure": staticmethod(configure),
        },
    )()
    for key, value in original.items():
        setattr(app.state, key, value)


def create(env, **body: object) -> dict:
    response = env.client.post(
        f"/api/v1/videos/{env.video.video_id}/tasks", json=body, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_desktop_task_runs_builtin_even_when_default_engine_is_agent(env) -> None:
    env.configure(default_engine="agent")

    task = create(env, page_number=1)

    assert task["model_routing"] == {
        "engine": "builtin",
        "agent_capabilities": [],
        "agent_steps": [],
    }


def test_agent_caller_follows_default_engine_and_default_capabilities(env) -> None:
    env.configure(default_engine="agent")

    task = create(env, page_number=1, agent_caller=True)

    assert task["model_routing"] == {
        "engine": "agent",
        "agent_capabilities": ["text", "vision"],
        "agent_steps": ALL_STEPS,
    }


def test_agent_caller_with_builtin_default_engine_stays_builtin(env) -> None:
    task = create(env, page_number=1, agent_caller=True)

    assert task["model_routing"]["engine"] == "builtin"
    assert task["model_routing"]["agent_steps"] == []


def test_explicit_engine_wins_over_default_engine(env) -> None:
    env.configure(default_engine="agent")
    assert (
        create(env, page_number=1, agent_caller=True, engine="builtin")["model_routing"]["engine"]
        == "builtin"
    )

    env.configure(default_engine="builtin")
    assert (
        create(env, page_number=2, agent_caller=True, engine="agent")["model_routing"]["engine"]
        == "agent"
    )


def test_explicit_agent_engine_from_non_agent_caller_runs_builtin(env) -> None:
    task = create(env, page_number=1, engine="agent")

    assert task["model_routing"]["engine"] == "builtin"


def test_explicit_capabilities_win_over_default_capabilities(env) -> None:
    env.configure(default_engine="agent", agent_default_capabilities="text")

    vision_only = create(env, page_number=1, agent_caller=True, agent_capabilities=["vision"])
    assert vision_only["model_routing"]["agent_capabilities"] == ["vision"]
    assert vision_only["model_routing"]["agent_steps"] == ["visual_frame"]

    defaulted = create(env, page_number=2, agent_caller=True)
    assert defaulted["model_routing"]["agent_capabilities"] == ["text"]
    assert defaulted["model_routing"]["agent_steps"] == TEXT_STEPS


def test_only_steps_allowed_by_agent_delegation_go_to_the_agent(env) -> None:
    env.configure(default_engine="agent", agent_delegation="summary,mindmap")

    task = create(env, page_number=1, agent_caller=True)

    assert task["model_routing"]["agent_steps"] == ["summary", "mindmap"]


def test_unknown_engine_or_capability_is_rejected(env) -> None:
    url = f"/api/v1/videos/{env.video.video_id}/tasks"
    assert env.client.post(url, json={"engine": "gpt"}, headers=HEADERS).status_code == 422
    assert (
        env.client.post(url, json={"agent_capabilities": ["audio"]}, headers=HEADERS).status_code
        == 422
    )


def test_pages_created_in_one_batch_request_share_a_batch_id(env) -> None:
    response = env.client.post(
        f"/api/v1/videos/{env.video.video_id}/tasks/batch",
        json={"page_numbers": [1, 2]},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert len(body["created_tasks"]) == 2
    assert body["batch_id"]
    assert {task["batch_id"] for task in body["created_tasks"]} == {body["batch_id"]}


def test_separate_create_requests_get_separate_batches_unless_one_is_joined(env) -> None:
    first = create(env, page_number=1)
    second = create(env, page_number=2)
    joined = create(env, page_number=2, batch_id=first["batch_id"])

    assert first["batch_id"] and second["batch_id"]
    assert first["batch_id"] != second["batch_id"]
    assert joined["batch_id"] == first["batch_id"]


def test_generic_create_endpoint_routes_and_batches(env) -> None:
    env.configure(default_engine="builtin")
    response = env.client.post(
        "/api/v1/tasks",
        json={
            "input_type": "transcript_text",
            "source": '{"title": "t", "transcript": "x", "segments": []}',
            "options": {"engine": "agent"},
            "agent_caller": True,
            "agent_capabilities": "text",
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    task = response.json()

    assert task["batch_id"]
    assert task["model_routing"] == {
        "engine": "agent",
        "agent_capabilities": ["text"],
        "agent_steps": TEXT_STEPS,
    }


def test_reusable_task_reports_its_batch(env) -> None:
    created = create(env, page_number=1)

    response = env.client.get(
        f"/api/v1/videos/{env.video.video_id}/reusable-task?page_number=1", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_id"] == created["batch_id"]


def test_agent_settings_have_factory_defaults(env) -> None:
    settings = env.client.get("/api/v1/settings", headers=HEADERS).json()

    assert settings["default_engine"] == "builtin"
    assert settings["agent_default_capabilities"] == "text,vision"
    assert (
        settings["agent_delegation"]
        == "summary,knowledge_note,keyframe_plan,visual_frame,visual_compose,mindmap"
    )
    assert settings["agent_lease_ttl"] == 600


def test_agent_settings_are_saved_normalized(tmp_path: Path) -> None:
    from video_sum_service.settings_manager import SettingsManager, SettingsUpdatePayload

    manager = SettingsManager(
        ServiceSettings(
            _env_file=None, data_dir=tmp_path, cache_dir=tmp_path / "c", tasks_dir=tmp_path / "t"
        )
    )
    saved = manager.save(
        SettingsUpdatePayload(
            default_engine="AGENT",
            agent_default_capabilities="vision, text",
            agent_delegation="mindmap,summary",
            agent_lease_ttl=120,
        )
    )

    assert saved.default_engine == "agent"
    assert saved.agent_default_capabilities == "text,vision"
    assert saved.agent_delegation == "summary,mindmap"
    assert saved.agent_lease_ttl == 120
