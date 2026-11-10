"""HTTP test seam: FastAPI TestClient + real TaskWorker + real pipeline + FakeModelClient.

Usage::

    def test_x(tmp_path, monkeypatch):
        fake = routed_fake(by_stage={...})
        with service_seam(monkeypatch, tmp_path, fake, llm_enabled=True) as seam:
            task_id = seam.create_transcript_task()
            detail = seam.wait_for_terminal(task_id)

``routed_fake`` turns on the StepRouter guard: any model call that does not go
through ``StepRouter`` fails the test. Pipeline settings default to an enabled,
fully configured internal LLM; override any ``PipelineSettings`` field by kwarg.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import video_sum_service.app as service_app
import video_sum_service.routers.system as system_router
from fastapi.testclient import TestClient
from video_sum_core.pipeline.model_steps import require_step_router
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.model_client_fake import FakeModelClient
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
TERMINAL = {"completed", "failed", "paused", "cancelled"}


def routed_fake(*script: object, **kwargs: object) -> FakeModelClient:
    """``FakeModelClient`` that rejects calls made outside ``StepRouter``."""
    return FakeModelClient(list(script) or None, guard=require_step_router, **kwargs)  # type: ignore[arg-type]


def pipeline_settings(tmp_path: Path, **overrides: object) -> PipelineSettings:
    values: dict[str, object] = {
        "tasks_dir": tmp_path / "tasks",
        "llm_enabled": True,
        "llm_api_key": "sk-test",
        "llm_base_url": "http://llm.invalid/v1",
        "llm_model": "test-model",
        "summary_context_mode": "full",
    }
    values.update(overrides)
    return PipelineSettings(**values)  # type: ignore[arg-type]


@dataclass
class ServiceSeam:
    client: TestClient
    fake: FakeModelClient
    repository: SqliteTaskRepository
    worker: TaskWorker

    def create_transcript_task(
        self,
        *,
        title: str = "测试视频",
        transcript: str = TRANSCRIPT,
        segments: list[dict[str, object]] | None = None,
        options: dict[str, object] | None = None,
    ) -> str:
        body: dict[str, object] = {
            "input_type": "transcript_text",
            "source": json.dumps(
                {"title": title, "transcript": transcript, "segments": segments or SEGMENTS},
                ensure_ascii=False,
            ),
            "title": title,
        }
        if options is not None:
            body["options"] = options
        response = self.client.post("/api/v1/tasks", json=body, headers=HEADERS)
        assert response.status_code == 201, response.text
        return response.json()["task_id"]

    def detail(self, task_id: str) -> dict:
        return self.client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()

    def wait_for_terminal(self, task_id: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = self.detail(task_id)
            if detail["status"] in TERMINAL:
                return detail
            time.sleep(0.05)
        raise AssertionError(f"task {task_id} did not finish: {detail.get('status')}")

    def events(self, task_id: str) -> list[dict]:
        response = self.client.get(f"/api/v1/tasks/{task_id}/events", headers=HEADERS)
        assert response.status_code == 200, response.text
        return response.json()


@contextlib.contextmanager
def service_seam(
    monkeypatch,
    tmp_path: Path,
    fake: FakeModelClient,
    *,
    worker_kwargs: dict[str, object] | None = None,
    **settings_overrides: object,
) -> Iterator[ServiceSeam]:
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
    runner = RealPipelineRunner(
        pipeline_settings(tmp_path, **settings_overrides), model_client=fake
    )
    worker = TaskWorker(repository, runner, **(worker_kwargs or {}))  # type: ignore[arg-type]
    app.state.task_repository = repository
    app.state.task_worker = worker
    app.state.runtime_startup = None
    try:
        yield ServiceSeam(TestClient(app), fake, repository, worker)
    finally:
        worker.shutdown(wait=True, timeout=10)
        for key, value in original.items():
            setattr(app.state, key, value)
