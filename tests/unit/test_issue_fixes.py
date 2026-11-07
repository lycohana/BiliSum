import json
import io
import shutil
import sqlite3
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from video_sum_core.errors import LLMAuthenticationError, VideoSumError
from video_sum_core.llm_response import parse_llm_response_json
from video_sum_core.models.tasks import InputType, TaskInput, TaskOptions, TaskResult, TaskStatus
from video_sum_core.pipeline.base import PipelineRunner, PipelineTranscription
from video_sum_core.pipeline.real import PipelineSettings, RealPipelineRunner
from video_sum_infra.config import ServiceSettings
from video_sum_service import runtime_support
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers.tasks import router
from video_sum_service.worker import TaskWorker
from video_sum_service.storage_migration import migrate_manifest


def repository():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = SqliteTaskRepository(connection)
    store.initialize()
    return store


def wait_for(check):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check()


@pytest.mark.parametrize("separator", ["\n\n", "\n", "\r\n\r\n"])
def test_forced_sse_reassembles_content_and_usage(separator):
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"overview":'}}]},
        {"choices": [{"index": 0, "delta": {"content": '"你好"}'}}]},
        {"choices": [], "usage": {"total_tokens": 12}},
    ]
    raw = separator.join([*(f"data: {json.dumps(chunk)}" for chunk in chunks), "data: [DONE]"])
    result = parse_llm_response_json(httpx.Response(200, text=raw))
    assert json.loads(result["choices"][0]["message"]["content"]) == {"overview": "你好"}
    assert result["usage"]["total_tokens"] == 12


def test_json_with_done_suffix_and_standard_json():
    body = {"choices": [{"message": {"content": "hello"}}], "usage": {"total_tokens": 1}}
    for suffix in ("", "\n\ndata: [DONE]\n"):
        assert parse_llm_response_json(httpx.Response(200, text=json.dumps(body) + suffix)) == body
    with pytest.raises(VideoSumError):
        parse_llm_response_json(httpx.Response(200, text=json.dumps(body) + "\n{\"unexpected\":1}"))


def test_sse_error_is_not_silently_accepted():
    with pytest.raises(VideoSumError, match="stream error"):
        parse_llm_response_json(httpx.Response(200, text='data: {"error":{"message":"overloaded"}}\n\n'))


def test_ndjson_gateway_chunks_keep_usage_and_skip_reasoning():
    from video_sum_core.pipeline.real import _parse_llm_response_json

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": '{"overview":'}}]},
        {"choices": [{"delta": {"content": '"ok"}'}}]},
        {"choices": [], "usage": {"total_tokens": 7}},
    ]
    response = httpx.Response(200, text="\n".join(json.dumps(chunk) for chunk in chunks))
    for parser in (parse_llm_response_json, _parse_llm_response_json):
        parsed = parser(response)
        assert json.loads(parsed["choices"][0]["message"]["content"]) == {"overview": "ok"}
        assert parsed["usage"] == {"total_tokens": 7}


@pytest.mark.parametrize("status,expected_calls", [(401, 1), (403, 1), (429, 3), (503, 3)])
def test_llm_retries_only_transient_http_errors(monkeypatch, tmp_path, status, expected_calls):
    calls = []
    original_client = httpx.Client
    def respond(request):
        calls.append(request)
        if len(calls) < 3 or status in (401, 403):
            return httpx.Response(status, json={"error": "temporary"})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{\\\"overview\\\":\\\"ok\\\"}"}}]}\n\ndata: [DONE]')
    monkeypatch.setattr("video_sum_core.pipeline.real.httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("video_sum_core.pipeline.real.time.sleep", lambda _: None)
    runner = RealPipelineRunner(PipelineSettings(tasks_dir=tmp_path, llm_enabled=True, llm_api_key="test", summary_chunk_retry_count=2))
    if status in (401, 403):
        with pytest.raises(LLMAuthenticationError):
            runner._request_llm_json_with_retries(base_url="https://example.com/v1", payload={"messages": []})
    else:
        assert runner._request_llm_json_with_retries(base_url="https://example.com/v1", payload={"messages": []})["overview"] == "ok"
    assert len(calls) == expected_calls


class RecoveryRunner(PipelineRunner):
    def __init__(self, directory, fail=False):
        self.directory = directory
        self.fail = fail
        self.transcriptions = 0
        self.summaries = 0

    def supports_staged_execution(self):
        return True

    def run_transcription(self, context, on_event=None):
        self.transcriptions += 1
        return PipelineTranscription("test", "saved transcript", [{"text": "saved transcript", "start": 0}], self.directory)

    def run_summary(self, context, transcription, on_event=None):
        self.summaries += 1
        if self.fail:
            raise LLMAuthenticationError("HTTP 401")
        assert transcription.transcript == "saved transcript"
        return TaskResult(transcript_text=transcription.transcript, knowledge_note_markdown="# Recovered")


def test_resume_after_worker_restart_reuses_transcript_and_task_id(tmp_path):
    store = repository()
    task = store.create_task(TaskInput(input_type=InputType.URL, source="https://example.com/video"))
    first = RecoveryRunner(tmp_path, fail=True)
    worker = TaskWorker(store, first)
    worker.submit(task)
    wait_for(lambda: store.get_task(task.task_id).status == TaskStatus.PAUSED)
    worker.shutdown(wait=True, timeout=2)
    assert first.transcriptions == 1
    assert "API Key" in store.get_task(task.task_id).error_message
    second = RecoveryRunner(tmp_path)
    worker = TaskWorker(store, second)
    app = FastAPI()
    app.include_router(router)
    app.state.task_repository = store
    app.state.task_worker = worker
    try:
        client = TestClient(app)
        response = client.post(f"/api/v1/tasks/{task.task_id}/resume")
        assert response.status_code == 200
        assert response.json()["task_id"] == task.task_id
        wait_for(lambda: store.get_task(task.task_id).status == TaskStatus.COMPLETED)
        assert second.transcriptions == 0
        assert second.summaries == 1
        assert store.get_task(task.task_id).error_message is None
        assert len(store.list_tasks()) == 1
    finally:
        worker.shutdown(wait=True, timeout=2)


class QuickRunner(PipelineRunner):
    def __init__(self):
        self.started = []

    def run(self, context, on_event=None):
        self.started.append(context.task_id)
        return [], TaskResult(overview="ok")


def test_scheduled_task_survives_restart_and_does_not_block_immediate_work():
    store = repository()
    scheduled = store.create_task(TaskInput(input_type=InputType.URL, source="future", options=TaskOptions(
        scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=0.4))))
    first = TaskWorker(store, QuickRunner())
    first.submit(scheduled)
    first.shutdown(wait=True, timeout=2)
    runner = QuickRunner()
    worker = TaskWorker(store, runner)
    try:
        for task in store.list_recoverable_tasks():
            worker.submit(task)
        immediate = store.create_task(TaskInput(input_type=InputType.URL, source="now"))
        worker.submit(immediate)
        wait_for(lambda: store.get_task(immediate.task_id).status == TaskStatus.COMPLETED)
        assert runner.started == [immediate.task_id]
        wait_for(lambda: store.get_task(scheduled.task_id).status == TaskStatus.COMPLETED)
        assert runner.started == [immediate.task_id, scheduled.task_id]
    finally:
        worker.shutdown(wait=True, timeout=2)


def test_schedule_requires_explicit_timezone():
    with pytest.raises(ValidationError):
        TaskOptions(scheduled_at="2027-01-01T00:00:00")


def test_browser_download_returns_attachment_without_changing_artifact_paths(tmp_path):
    store = repository()
    task = store.create_task(TaskInput(input_type=InputType.URL, source="https://example.com", title="浏览器笔记"))
    store.save_result(task.task_id, TaskResult(knowledge_note_markdown="# 知识笔记\n\nBrowser export body", artifacts={"summary_path": "original.json"}))
    store.update_status(task.task_id, TaskStatus.COMPLETED)
    app = FastAPI()
    app.include_router(router)
    app.state.task_repository = store
    response = TestClient(app).get(f"/api/v1/tasks/{task.task_id}/exports/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "Browser export body" in response.text
    assert store.get_task(task.task_id).result.artifacts == {"summary_path": "original.json"}


@pytest.mark.parametrize("uv_fails", [False, True])
def test_uv_targets_selected_python_and_falls_back_to_pip(monkeypatch, tmp_path, uv_fails):
    monkeypatch.setattr(runtime_support, "bundled_uv_executable", lambda: tmp_path / "uv.exe")
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", ServiceSettings(_env_file=None, cache_dir=tmp_path / "cache"))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1 and uv_fails:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")
    python = tmp_path / "managed-python.exe"
    pip = [str(python), "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--upgrade-strategy", "only-if-needed", "package"]
    runtime_support.run_package_install(pip, python, "base", 60, run)
    assert calls[0][calls[0].index("--python") + 1] == str(python)
    assert str(tmp_path / "cache" / "uv") in calls[0]
    assert "--upgrade-strategy" not in calls[0]
    if uv_fails:
        assert calls[1] == pip


def test_storage_migration_rewrites_database_json_and_preserves_source(tmp_path):
    source = tmp_path / "old"
    stage = tmp_path / "stage"
    target = tmp_path / "new"
    source.mkdir()
    database = source / "video_sum.db"
    connection = sqlite3.connect(database, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = SqliteTaskRepository(connection)
    store.initialize()
    task = store.create_task(TaskInput(input_type=InputType.VIDEO_FILE, source=str(source / "clip.mp4")))
    store.save_result(task.task_id, TaskResult(transcript_text="keep this prose", artifacts={"summary_path": str(source / "tasks" / "summary.json")}))
    connection.close()
    original = database.read_bytes()
    (source / "summary.json").write_text(json.dumps({"artifact": str(source / "tasks" / "image.png"), "content": "keep this prose"}), encoding="utf-8")
    shutil.copytree(source, stage)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"stageRoot": str(stage), "mappings": [{"source": str(source), "target": str(target)}], "files": [str(stage / "video_sum.db"), str(stage / "summary.json")]}), encoding="utf-8")
    migrate_manifest(manifest)
    assert database.read_bytes() == original
    connection = sqlite3.connect(stage / "video_sum.db", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    migrated = SqliteTaskRepository(connection).get_task(task.task_id)
    assert migrated.task_input.source == str(target / "clip.mp4")
    assert migrated.result.artifacts["summary_path"] == str(target / "tasks" / "summary.json")
    assert migrated.result.transcript_text == "keep this prose"
    assert json.loads((stage / "summary.json").read_text(encoding="utf-8"))["artifact"] == str(target / "tasks" / "image.png")
    connection.close()


def test_browser_download_bundles_visual_images(tmp_path):
    store = repository()
    task = store.create_task(TaskInput(input_type=InputType.URL, source="https://example.com", title="Images"))
    visual = tmp_path / "visual"
    (visual / "frames").mkdir(parents=True)
    (visual / "frames" / "frame.png").write_bytes(b"png-content")
    note = visual / "visual_note.md"
    note.write_text("# 知识笔记\n\nSee this frame.\n\n![frame](frames/frame.png)", encoding="utf-8")
    store.save_result(task.task_id, TaskResult(knowledge_note_markdown="text note", visual_note_status="ready", visual_note_artifact_path=str(note)))
    store.update_status(task.task_id, TaskStatus.COMPLETED)
    app = FastAPI()
    app.include_router(router)
    app.state.task_repository = store
    response = TestClient(app).get(f"/api/v1/tasks/{task.task_id}/exports/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        markdown = next(name for name in archive.namelist() if name.endswith(".md"))
        image = next(name for name in archive.namelist() if name.endswith(".png"))
        assert image in archive.read(markdown).decode("utf-8")
        assert archive.read(image) == b"png-content"


def test_worker_replacement_preserves_scheduled_queue():
    store = repository()
    task = store.create_task(TaskInput(input_type=InputType.URL, source="future", options=TaskOptions(
        scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=0.4))))
    previous = TaskWorker(store, QuickRunner())
    previous.submit(task)
    runner = QuickRunner()
    replacement = TaskWorker(store, runner)
    state = SimpleNamespace(task_worker=previous, task_repository=store)
    try:
        runtime_support.replace_task_worker(state, replacement)
        wait_for(lambda: store.get_task(task.task_id).status == TaskStatus.COMPLETED)
        assert runner.started == [task.task_id]
    finally:
        previous.shutdown(wait=True, timeout=2)
        replacement.shutdown(wait=True, timeout=2)


def test_windowed_backend_starts_without_console_streams(monkeypatch, tmp_path):
    import sys
    import uvicorn
    from video_sum_service import main
    settings = ServiceSettings(_env_file=None, data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", tasks_dir=tmp_path / "tasks")
    monkeypatch.setattr(main.settings_manager, "_settings", settings)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    configurations = []
    def run(app, **kwargs):
        configurations.append(uvicorn.Config(app, **kwargs))
    monkeypatch.setattr(main, "uvicorn_run", run)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    main.run()
    assert len(configurations) == 1


def test_fresh_packaged_startup_seeds_python_before_probe(monkeypatch, tmp_path):
    from video_sum_service import runtime_startup
    calls = []
    settings = ServiceSettings(_env_file=None, data_dir=tmp_path)
    state = SimpleNamespace(task_repository=repository())
    worker = SimpleNamespace(shutdown=lambda **kwargs: None)
    monkeypatch.setattr(runtime_startup, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_startup, "bootstrap_managed_runtime", lambda channel: calls.append("seed"))
    def probe(channel):
        assert calls == ["seed"]
        calls.append("probe")
        return {"runtimeReady": True}
    monkeypatch.setattr(runtime_startup, "detect_environment", probe)
    monkeypatch.setattr(runtime_startup, "build_worker", lambda **kwargs: worker)
    monkeypatch.setattr(runtime_startup, "replace_task_worker", lambda *args: None)
    runtime_startup.initialize_runtime_startup_state(state, settings)
    thread = runtime_startup.start_runtime_startup(state, state.task_repository, settings, lambda *_: 0)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert state.runtime_startup["status"] == "ready"
    assert calls == ["seed", "probe"]
