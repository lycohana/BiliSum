"""F1a: transcript_only tasks finish right after transcription.

Driven through the HTTP API with a real worker and the real pipeline. Model
traffic is observed at the pipeline's HTTP client (the only way the pipeline
reaches an LLM today); every attempt is recorded and refused.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import video_sum_core.pipeline.real as real_pipeline
import video_sum_service.app as service_app
import video_sum_service.routers.system as system_router
from fastapi.testclient import TestClient
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_service.app import app
from video_sum_service.auth import ACCESS_TOKEN_ENV, AccessTokenManager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.worker import TaskWorker

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "第一段内容"},
    {"start": 4.0, "end": 9.0, "text": "第二段内容"},
]
TRANSCRIPT = "第一段内容\n第二段内容"


class RecordingHttpClient:
    """Stands in for httpx.Client inside the pipeline; refuses every request."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def _refuse(self, method: str, url, *args, **kwargs):
        RecordingHttpClient.calls.append(f"{method} {url}")
        raise real_pipeline.httpx.ConnectError("model access refused in test")

    def post(self, url, *args, **kwargs):
        return self._refuse("POST", url)

    def get(self, url, *args, **kwargs):
        return self._refuse("GET", url)

    def stream(self, method, url, *args, **kwargs):
        return self._refuse(method, url)


@pytest.fixture
def client(monkeypatch, tmp_path: Path):
    original_repository = getattr(app.state, "task_repository", None)
    original_worker = getattr(app.state, "task_worker", None)
    original_runtime_startup = getattr(app.state, "runtime_startup", None)

    manager = AccessTokenManager(Path("unused"), env={ACCESS_TOKEN_ENV: TOKEN})
    monkeypatch.setattr(service_app, "access_token_manager", manager)
    monkeypatch.setattr(system_router, "access_token_manager", manager)
    RecordingHttpClient.calls = []
    monkeypatch.setattr(real_pipeline.httpx, "Client", RecordingHttpClient)

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    runner = RealPipelineRunner(
        PipelineSettings(
            tasks_dir=tmp_path / "tasks",
            llm_enabled=True,
            llm_api_key="sk-test",
            llm_base_url="http://llm.invalid/v1",
            llm_model="test-model",
        )
    )
    worker = TaskWorker(
        repository,
        runner,
        auto_generate_mindmap=True,
        auto_generate_visual_evidence=True,
    )
    app.state.task_repository = repository
    app.state.task_worker = worker
    app.state.runtime_startup = None
    try:
        yield TestClient(app)
    finally:
        worker.shutdown(wait=True, timeout=10)
        app.state.task_repository = original_repository
        app.state.task_worker = original_worker
        app.state.runtime_startup = original_runtime_startup


def create_transcript_task(client: TestClient, summary_scope: str | None) -> str:
    body: dict[str, object] = {
        "input_type": "transcript_text",
        "source": json.dumps(
            {"title": "只转写测试", "transcript": TRANSCRIPT, "segments": SEGMENTS}
        ),
        "title": "只转写测试",
    }
    if summary_scope is not None:
        body["options"] = {"summary_scope": summary_scope}
    response = client.post("/api/v1/tasks", json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def wait_for_terminal(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
        if detail["status"] in {"completed", "failed", "paused", "cancelled"}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish: {detail['status']}")


def event_stages(client: TestClient, task_id: str) -> list[str]:
    response = client.get(f"/api/v1/tasks/{task_id}/events", headers=HEADERS)
    assert response.status_code == 200, response.text
    return [event["stage"] for event in response.json()]


def test_transcript_only_task_completes_without_summary_or_model_calls(client: TestClient) -> None:
    task_id = create_transcript_task(client, "transcript_only")

    detail = wait_for_terminal(client, task_id)

    assert detail["status"] == "completed"
    assert RecordingHttpClient.calls == []
    result = client.get(f"/api/v1/tasks/{task_id}/result", headers=HEADERS).json()["result"]
    assert result["transcript_text"] == TRANSCRIPT
    assert [segment["text"] for segment in result["segments"]] == ["第一段内容", "第二段内容"]
    assert result["overview"] == ""
    assert result["knowledge_note_markdown"] == ""
    assert result["key_points"] == []
    assert result["timeline"] == []
    assert result["mindmap_status"] == "idle"
    assert Path(result["artifacts"]["transcript_path"]).read_text(encoding="utf-8") == TRANSCRIPT
    assert "knowledge_note_path" not in result["artifacts"]

    stages = event_stages(client, task_id)
    assert "completed" in stages
    assert "llm_queued" not in stages
    assert "llm_started" not in stages


def test_default_scope_still_reaches_the_model(client: TestClient) -> None:
    task_id = create_transcript_task(client, None)

    wait_for_terminal(client, task_id)

    assert RecordingHttpClient.calls, "default summary scope should attempt a model request"
