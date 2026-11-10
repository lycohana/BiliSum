"""C2 through the HTTP API: create endpoints accept 补充要求 and output language.

Real worker + real pipeline; the model is a FakeModelClient.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import video_sum_service.app as service_app
import video_sum_service.routers.system as system_router
from fastapi.testclient import TestClient
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.model_client_fake import FakeModelClient
from video_sum_service.app import app
from video_sum_service.auth import ACCESS_TOKEN_ENV, AccessTokenManager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord
from video_sum_service.worker import TaskWorker

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "第一段内容"},
    {"start": 4.0, "end": 9.0, "text": "第二段内容"},
]
SUMMARY = {
    "title": "标题",
    "overview": "概览",
    "bulletPoints": ["要点"],
    "chapters": [{"title": "开场", "start": 0, "summary": "开场"}],
}
NOTE = {"knowledgeNoteMarkdown": "# 笔记\n\n内容"}


class _Env:
    def __init__(
        self, client: TestClient, repository: SqliteTaskRepository, fake: FakeModelClient
    ) -> None:
        self.client = client
        self.repository = repository
        self.fake = fake


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    originals = {
        name: getattr(app.state, name, None)
        for name in ("task_repository", "task_worker", "runtime_startup")
    }
    manager = AccessTokenManager(Path("unused"), env={ACCESS_TOKEN_ENV: TOKEN})
    monkeypatch.setattr(service_app, "access_token_manager", manager)
    monkeypatch.setattr(system_router, "access_token_manager", manager)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    fake = FakeModelClient(by_stage={"knowledge_note": [NOTE] * 4}, default=SUMMARY)
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path / "tasks",
            llm_enabled=True,
            llm_api_key="sk-test",
            llm_base_url="http://llm.invalid/v1",
            llm_model="test-model",
            summary_context_mode="full",
        ),
        model_client=fake,
    )
    worker = TaskWorker(repository, runner)
    app.state.task_repository = repository
    app.state.task_worker = worker
    app.state.runtime_startup = None
    try:
        yield _Env(TestClient(app), repository, fake)
    finally:
        worker.shutdown(wait=True, timeout=10)
        for name, value in originals.items():
            setattr(app.state, name, value)


def _transcript_body(options: dict[str, object]) -> dict[str, object]:
    return {
        "input_type": "transcript_text",
        "source": json.dumps(
            {"title": "测试", "transcript": "第一段内容\n第二段内容", "segments": SEGMENTS}
        ),
        "title": "测试",
        "options": options,
    }


def _wait_for_terminal(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
        if detail["status"] in {"completed", "failed", "paused", "cancelled"}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


def test_task_with_instructions_sends_them_and_records_them_on_the_result(env: _Env) -> None:
    response = env.client.post(
        "/api/v1/tasks",
        json=_transcript_body({"extra_instructions": "重点讲技术细节", "language": "en"}),
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text

    detail = _wait_for_terminal(env.client, response.json()["task_id"])

    assert detail["status"] == "completed", detail
    assert detail["result"]["extra_instructions"] == "重点讲技术细节"
    assert detail["result"]["language"] == "en"
    summary_requests = [
        r for r in env.fake.requests if r.usage_stage in {"summary_full", "knowledge_note"}
    ]
    assert summary_requests
    for request in summary_requests:
        user = [m for m in request.payload["messages"] if m["role"] == "user"][-1]["content"]
        assert "【补充要求】" in user and user.index("重点讲技术细节") > user.index("【补充要求】")
        assert "请使用 English" in user


def test_task_without_instructions_sends_no_extra_sections(env: _Env) -> None:
    response = env.client.post("/api/v1/tasks", json=_transcript_body({}), headers=HEADERS)
    assert response.status_code == 201, response.text

    detail = _wait_for_terminal(env.client, response.json()["task_id"])

    assert detail["result"]["extra_instructions"] is None
    for request in env.fake.requests:
        text = json.dumps(request.payload, ensure_ascii=False)
        assert "【补充要求】" not in text and "【输出语言】" not in text


def test_too_long_instructions_are_rejected_at_create(env: _Env) -> None:
    response = env.client.post(
        "/api/v1/tasks", json=_transcript_body({"extra_instructions": "字" * 1001}), headers=HEADERS
    )

    assert response.status_code == 422
    assert "1000" in response.text
    assert env.repository.list_tasks() == []


@pytest.fixture
def video(env: _Env, monkeypatch) -> VideoAssetRecord:
    """A library video; dispatch to the worker is stubbed so no download starts."""
    import video_sum_service.routers.videos as videos_router

    monkeypatch.setattr(videos_router, "submit_task_or_queue", lambda *_args, **_kwargs: None)
    return env.repository.upsert_video_asset(
        VideoAssetRecord(canonical_id="BV1test", title="视频", source_url="https://www.bilibili.com/video/BV1test")
    )


def test_video_task_endpoint_rejects_too_long_instructions(
    env: _Env, video: VideoAssetRecord
) -> None:
    response = env.client.post(
        f"/api/v1/videos/{video.video_id}/tasks",
        json={"extra_instructions": "字" * 1001},
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert "1000" in response.text
    assert env.repository.list_tasks() == []


def test_video_task_endpoint_stores_instructions_and_language(
    env: _Env, video: VideoAssetRecord
) -> None:
    response = env.client.post(
        f"/api/v1/videos/{video.video_id}/tasks",
        json={"extra_instructions": "  重点讲技术细节 ", "language": "ja"},
        headers=HEADERS,
    )

    assert response.status_code == 201, response.text
    options = env.repository.get_task(response.json()["task_id"]).task_input.options
    assert options.extra_instructions == "重点讲技术细节"
    assert options.language == "ja"
