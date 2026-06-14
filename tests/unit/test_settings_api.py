from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

import video_sum_service.app as service_app
import video_sum_service.runtime_support as runtime_support
from video_sum_core.models.tasks import InputType, TaskInput, TaskStatus
from video_sum_infra.config import (
    DEFAULT_VISUAL_FRAME_PLANNING_PROMPT,
    DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT,
    DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE,
    DEFAULT_VISUAL_VLM_PROMPT,
    ServiceSettings,
)
from video_sum_service.app import (
    app,
    install_knowledge_dependencies,
    install_local_asr,
    probe_asr_connection,
    probe_llm_connection,
    recover_incomplete_tasks,
    serialize_settings,
    settings_manager,
    update_settings,
)
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.settings_manager import SettingsUpdatePayload
import sqlite3


def test_update_settings_reuses_environment_probe(monkeypatch, tmp_path: Path) -> None:
    previous = ServiceSettings(
        data_dir=tmp_path / "data-prev",
        cache_dir=tmp_path / "cache-prev",
        tasks_dir=tmp_path / "tasks-prev",
        runtime_channel="base",
    )
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
    )
    settings_manager._settings = previous
    monkeypatch.setattr(settings_manager, "save", lambda payload: current)
    app.state.task_repository = object()

    detect_calls: list[str | None] = []

    monkeypatch.setattr("video_sum_service.app.bootstrap_managed_runtime", lambda runtime_channel: None)
    monkeypatch.setattr("video_sum_service.app.prepend_runtime_path", lambda runtime_channel: None)
    monkeypatch.setattr(
        "video_sum_service.app.detect_environment",
        lambda runtime_channel=None: detect_calls.append(runtime_channel) or {"cudaAvailable": False, "runtimeChannel": runtime_channel or "base"},
    )
    monkeypatch.setattr(
        "video_sum_service.app.build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "runtime_channel": current_settings.runtime_channel,
            "environment": environment_info,
        },
    )

    response = update_settings(SettingsUpdatePayload(llm_enabled=True))

    assert response["saved"] is True
    assert response["settings"]["runtime_channel"] == "base"
    assert detect_calls == ["base"]


def test_update_settings_rejects_invalid_runtime_channel(monkeypatch, tmp_path: Path) -> None:
    previous = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = previous
    save_calls: list[SettingsUpdatePayload] = []
    monkeypatch.setattr(settings_manager, "save", lambda payload: save_calls.append(payload) or previous)

    with pytest.raises(HTTPException) as exc_info:
        update_settings(SettingsUpdatePayload(runtime_channel="../outside"))

    assert exc_info.value.status_code == 400
    assert save_calls == []


def test_update_settings_ensures_selected_runtime_channel(monkeypatch, tmp_path: Path) -> None:
    previous = ServiceSettings(
        data_dir=tmp_path / "data-prev",
        cache_dir=tmp_path / "cache-prev",
        tasks_dir=tmp_path / "tasks-prev",
        runtime_channel="base",
    )
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="gpu-cu128",
    )
    settings_manager._settings = previous
    app.state.task_repository = object()
    ensure_calls: list[str] = []

    monkeypatch.setattr(settings_manager, "save", lambda payload: current)
    monkeypatch.setattr("video_sum_service.app.ensure_runtime_channel", lambda runtime_channel: ensure_calls.append(runtime_channel) or tmp_path / runtime_channel)
    monkeypatch.setattr("video_sum_service.app.bootstrap_managed_runtime", lambda runtime_channel: None)
    monkeypatch.setattr("video_sum_service.app.prepend_runtime_path", lambda runtime_channel: None)
    monkeypatch.setattr("video_sum_service.app.activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(
        "video_sum_service.app.detect_environment",
        lambda runtime_channel=None: {"cudaAvailable": False, "runtimeChannel": runtime_channel or "base"},
    )
    monkeypatch.setattr(
        "video_sum_service.app.build_worker",
        lambda repository, current_settings, environment_info=None: object(),
    )

    response = update_settings(SettingsUpdatePayload(runtime_channel="gpu-cu128"))

    assert response["saved"] is True
    assert ensure_calls == ["gpu-cu128"]


def test_serialize_settings_includes_persisted_file_flag(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    monkeypatch.setattr(settings_manager, "_settings_path", tmp_path / "data" / "settings.json")

    payload = serialize_settings(current, environment_info={"cudaAvailable": False, "runtimeChannel": "base"})

    assert payload["settings_file_exists"] is False
    assert payload["task_concurrency"] == current.task_concurrency
    assert payload["mindmap_concurrency"] == current.mindmap_concurrency
    assert payload["knowledge_note_system_prompt"] == current.knowledge_note_system_prompt
    assert payload["knowledge_note_user_prompt_template"] == current.knowledge_note_user_prompt_template
    assert payload["visual_multimodal_enabled"] == current.visual_multimodal_enabled
    assert payload["visual_download_resolution"] == current.visual_download_resolution
    assert payload["visual_vlm_provider"] == current.visual_vlm_provider
    assert payload["visual_evidence_image_quality"] == current.visual_evidence_image_quality
    assert payload["visual_frame_planning_prompt"] == current.visual_frame_planning_prompt
    assert payload["visual_vlm_prompt"] == current.visual_vlm_prompt
    assert payload["defaults"]["knowledge_note_system_prompt"] == DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT
    assert payload["defaults"]["knowledge_note_user_prompt_template"] == DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE
    assert payload["defaults"]["visual_frame_planning_prompt"] == DEFAULT_VISUAL_FRAME_PLANNING_PROMPT
    assert payload["defaults"]["visual_vlm_prompt"] == DEFAULT_VISUAL_VLM_PROMPT

    settings_manager._settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_manager._settings_path.write_text("{}", encoding="utf-8")

    payload = serialize_settings(current, environment_info={"cudaAvailable": False, "runtimeChannel": "base"})

    assert payload["settings_file_exists"] is True


def test_serialize_settings_masks_provider_api_keys(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        siliconflow_asr_api_key="asr-secret",
        llm_api_key="llm-secret",
        knowledge_llm_api_key="knowledge-secret",
        siliconflow_embedding_api_key="embedding-secret",
        siliconflow_embedding_base_url="https://api.siliconflow.cn/v1",
        siliconflow_embedding_model="BAAI/bge-large-zh-v1.5",
        visual_evidence_api_key="visual-secret",
    )

    payload = serialize_settings(current, environment_info={"cudaAvailable": False, "runtimeChannel": "base"})

    assert payload["siliconflow_asr_api_key"] == ""
    assert payload["llm_api_key"] == ""
    assert payload["knowledge_llm_api_key"] == ""
    assert payload["siliconflow_embedding_api_key"] == ""
    assert payload["visual_evidence_api_key"] == ""
    assert payload["siliconflow_asr_api_key_configured"] is True
    assert payload["llm_api_key_configured"] is True
    assert payload["knowledge_llm_api_key_configured"] is True
    assert payload["siliconflow_embedding_api_key_configured"] is True
    assert payload["siliconflow_embedding_base_url"] == "https://api.siliconflow.cn/v1"
    assert payload["siliconflow_embedding_model"] == "BAAI/bge-large-zh-v1.5"
    assert payload["visual_evidence_api_key_configured"] is True


def test_update_settings_preserves_configured_api_keys_when_payload_is_blank(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_api_key="saved-asr-key",
        llm_api_key="saved-llm-key",
        knowledge_llm_api_key="saved-knowledge-key",
        siliconflow_embedding_api_key="saved-embedding-key",
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="",
            llm_api_key="",
            knowledge_llm_api_key="",
            siliconflow_embedding_api_key="",
            llm_model="new-model",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "saved-asr-key"
    assert next_settings.llm_api_key == "saved-llm-key"
    assert next_settings.knowledge_llm_api_key == "saved-knowledge-key"
    assert next_settings.siliconflow_embedding_api_key == "saved-embedding-key"
    assert next_settings.llm_model == "new-model"


def test_update_settings_preserves_configured_api_keys_when_payload_is_masked(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_api_key="saved-asr-key",
        llm_api_key="saved-llm-key",
        knowledge_llm_api_key="saved-knowledge-key",
        siliconflow_embedding_api_key="saved-embedding-key",
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="******",
            llm_api_key="******",
            knowledge_llm_api_key="******",
            siliconflow_embedding_api_key="******",
            llm_model="new-model",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "saved-asr-key"
    assert next_settings.llm_api_key == "saved-llm-key"
    assert next_settings.knowledge_llm_api_key == "saved-knowledge-key"
    assert next_settings.siliconflow_embedding_api_key == "saved-embedding-key"
    assert next_settings.llm_model == "new-model"


def test_update_settings_replaces_api_keys_when_payload_has_new_values(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_api_key="saved-asr-key",
        llm_api_key="saved-llm-key",
        knowledge_llm_api_key="saved-knowledge-key",
        siliconflow_embedding_api_key="saved-embedding-key",
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="new-asr-key",
            llm_api_key="new-llm-key",
            knowledge_llm_api_key="new-knowledge-key",
            siliconflow_embedding_api_key="new-embedding-key",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "new-asr-key"
    assert next_settings.llm_api_key == "new-llm-key"
    assert next_settings.knowledge_llm_api_key == "new-knowledge-key"
    assert next_settings.siliconflow_embedding_api_key == "new-embedding-key"


def test_update_settings_persists_visual_summary_options(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            visual_multimodal_enabled=True,
            visual_download_resolution="720p",
            visual_vlm_provider="openai-compatible",
            visual_evidence_base_url="https://vlm.example/v1",
            visual_evidence_model="vlm-model",
            visual_evidence_api_key="vlm-key",
            visual_evidence_frame_width=1280,
            visual_evidence_image_quality=72,
            visual_frame_planning_prompt="plan frames",
            visual_vlm_prompt="describe frame",
            visual_note_user_prompt_template="compose note",
        )
    )

    assert next_settings.visual_multimodal_enabled is True
    assert next_settings.visual_download_resolution == "720p"
    assert next_settings.visual_vlm_provider == "openai-compatible"
    assert next_settings.visual_evidence_base_url == "https://vlm.example/v1"
    assert next_settings.visual_evidence_model == "vlm-model"
    assert next_settings.visual_evidence_api_key == "vlm-key"
    assert next_settings.visual_evidence_frame_width == 1280
    assert next_settings.visual_evidence_image_quality == 72
    assert next_settings.visual_frame_planning_prompt == "plan frames"
    assert next_settings.visual_vlm_prompt == "describe frame"
    assert next_settings.visual_note_user_prompt_template == "compose note"

    stored = settings_manager._settings_path.read_text(encoding="utf-8")
    assert "visual_download_resolution" in stored
    assert "visual_frame_planning_prompt" in stored


def test_install_local_asr_refreshes_environment(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr("video_sum_service.app.ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr("video_sum_service.app.runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr("video_sum_service.app._install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr("video_sum_service.app._ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(
        "video_sum_service.app._run_command",
        lambda command, runtime_channel, timeout=1800: type("Result", (), {"stdout": "ok", "stderr": ""})(),
    )
    monkeypatch.setattr("video_sum_service.app.clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(
        "video_sum_service.app.detect_environment",
        lambda runtime_channel=None: {
            "runtimeChannel": runtime_channel or "base",
            "localAsrInstalled": True,
            "localAsrAvailable": True,
            "localAsrVersion": "1.1.1",
        },
    )
    monkeypatch.setattr(
        "video_sum_service.app.build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )
    monkeypatch.setattr("video_sum_service.app.write_runtime_metadata", lambda runtime_channel, payload: None)

    response = install_local_asr()

    assert response["installed"] is True
    assert response["runtimeChannel"] == "base"
    assert response["environment"]["localAsrVersion"] == "1.1.1"


def test_install_local_asr_retries_with_mirror_when_official_index_fails(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr("video_sum_service.app.ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr("video_sum_service.app.runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr("video_sum_service.app._install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr("video_sum_service.app._ensure_runtime_pip", lambda python_executable, runtime_channel: None)

    commands: list[list[str]] = []

    def fake_run(command, runtime_channel, timeout=1800):
        commands.append(command)
        if "--index-url" not in command:
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr="SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))",
            )
        return type("Result", (), {"stdout": "ok", "stderr": ""})()

    monkeypatch.setattr("video_sum_service.app._run_command", fake_run)
    monkeypatch.setattr("video_sum_service.app.clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(
        "video_sum_service.app.detect_environment",
        lambda runtime_channel=None: {
            "runtimeChannel": runtime_channel or "base",
            "localAsrInstalled": True,
            "localAsrAvailable": True,
            "localAsrVersion": "1.1.1",
        },
    )
    monkeypatch.setattr(
        "video_sum_service.app.build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )
    monkeypatch.setattr("video_sum_service.app.write_runtime_metadata", lambda runtime_channel, payload: None)

    response = install_local_asr()

    assert response["installed"] is True
    assert len(commands) == 2
    assert "--index-url" not in commands[0]
    assert "--upgrade-strategy" in commands[0]
    strategy_flag = commands[0].index("--upgrade-strategy")
    assert commands[0][strategy_flag + 1] == "only-if-needed"
    assert "--index-url" in commands[1]
    index_flag = commands[1].index("--index-url")
    assert commands[1][index_flag + 1] == "https://pypi.tuna.tsinghua.edu.cn/simple"


def test_install_knowledge_dependencies_refreshes_environment(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(
        runtime_support,
        "run_command",
        lambda command, runtime_channel, timeout=1800: type("Result", (), {"stdout": "ok", "stderr": ""})(),
    )
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(
        runtime_support,
        "detect_environment",
        lambda runtime_channel=None: {
            "runtimeChannel": runtime_channel or "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.2.3",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.4.5",
            "knowledgeDependenciesReady": True,
        },
    )
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    response = install_knowledge_dependencies()

    assert response["installed"] is True
    assert response["runtimeChannel"] == "base"
    assert response["environment"]["chromadbVersion"] == "1.2.3"
    assert response["environment"]["sentenceTransformersVersion"] == "3.4.5"


def test_recover_incomplete_tasks_resubmits_queued_and_running_records() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    queued = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com/queued", title="queued"))
    running = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com/running", title="running"))
    repository.update_status(running.task_id, TaskStatus.RUNNING)
    completed = repository.create_task(TaskInput(input_type=InputType.URL, source="https://example.com/completed", title="completed"))
    repository.update_status(completed.task_id, TaskStatus.COMPLETED)

    class FakeWorker:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(self, record) -> None:
            self.submitted.append(record.task_id)

    worker = FakeWorker()

    recovered = recover_incomplete_tasks(repository, worker)

    assert recovered == 2
    assert set(worker.submitted) == {queued.task_id, running.task_id}


def test_detect_environment_uses_persisted_cache(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        cache_dir=tmp_path / "cache",
        runtime_channel="base",
    )
    current.cache_dir.mkdir(parents=True)
    cache_path = current.cache_dir / "environment-probe-cache.json"
    cache_path.write_text(
        """
        {
          "base": {
            "runtimeChannel": "base",
            "runtimeReady": true,
            "runtimePython": "cached-python",
            "cudaAvailable": true,
            "localAsrAvailable": true,
            "knowledgeDependenciesReady": true
          }
        }
        """,
        encoding="utf-8",
    )
    runtime_support._environment_probe_cache.clear()
    runtime_support._environment_probe_failures.clear()
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "is_frozen", lambda: False)
    monkeypatch.setattr(
        runtime_support,
        "run_host_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    environment = runtime_support.detect_environment("base")

    assert environment["cudaAvailable"] is True
    assert environment["localAsrAvailable"] is True


def test_detect_environment_requires_importable_knowledge_dependencies(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        cache_dir=tmp_path / "cache",
        runtime_channel="base",
    )
    current.cache_dir.mkdir(parents=True)
    runtime_support._environment_probe_cache.clear()
    runtime_support._environment_probe_failures.clear()
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: True)

    def fake_run_host_command(command, timeout=120):
        script = command[-1]
        shim = """
import builtins
import importlib.metadata
import sys
import types

real_import = builtins.__import__

def fake_version(name):
    versions = {
        "yt-dlp": "2025.1.1",
        "chromadb": "1.0.0",
        "sentence-transformers": "3.0.0",
    }
    if name in versions:
        return versions[name]
    raise importlib.metadata.PackageNotFoundError(name)

def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "torch":
        raise ImportError("torch missing")
    if name == "chromadb":
        raise ImportError("chromadb broken")
    if name == "sentence_transformers":
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module
    return real_import(name, globals, locals, fromlist, level)

importlib.metadata.version = fake_version
builtins.__import__ = fake_import
sys.modules["sentence_transformers"] = types.ModuleType("sentence_transformers")
"""
        return subprocess.run(
            [sys.executable, "-c", shim + "\n" + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    monkeypatch.setattr(runtime_support, "run_host_command", fake_run_host_command)

    environment = runtime_support.detect_environment("base")

    assert environment["chromadbInstalled"] is False
    assert environment["chromadbVersion"] == "1.0.0"
    assert "chromadb" in environment["chromadbError"]
    assert environment["sentenceTransformersInstalled"] is True
    assert environment["sentenceTransformersVersion"] == "3.0.0"
    assert environment["knowledgeDependenciesReady"] is False
    assert "chromadb" in environment["knowledgeDependenciesError"]


def test_detect_environment_applies_siliconflow_dependency_policy_to_cached_probe(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        cache_dir=tmp_path / "cache",
        runtime_channel="base",
        knowledge_embedding_provider="siliconflow",
    )
    current.cache_dir.mkdir(parents=True)
    runtime_support._environment_probe_cache.clear()
    runtime_support._environment_probe_failures.clear()
    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "is_frozen", lambda: False)
    monkeypatch.setattr(
        runtime_support,
        "run_host_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )
    cache_path = current.cache_dir / runtime_support._ENVIRONMENT_PROBE_CACHE_FILE
    cache_path.write_text(
        '{"base":{"runtimeChannel":"base","runtimeReady":true,"runtimePython":"","chromadbInstalled":true,'
        '"sentenceTransformersInstalled":false,"knowledgeDependenciesReady":false}}',
        encoding="utf-8",
    )

    environment = runtime_support.detect_environment("base")

    assert environment["chromadbInstalled"] is True
    assert environment["sentenceTransformersInstalled"] is False
    assert environment["knowledgeDependenciesReady"] is True
    assert environment["knowledgeRequiredPackages"] == ["chromadb"]


def test_knowledge_requirements_are_provider_specific() -> None:
    siliconflow = runtime_support.get_knowledge_requirements("siliconflow")
    local_hf = runtime_support.get_knowledge_requirements("local_huggingface")
    local_ms = runtime_support.get_knowledge_requirements("local_modelscope")

    assert siliconflow == {"required": ["chromadb"], "optional": [], "preinstalled": []}
    assert local_hf == {"required": ["chromadb", "sentence-transformers"], "optional": [], "preinstalled": []}
    assert local_ms == {"required": ["chromadb", "sentence-transformers", "modelscope"], "optional": [], "preinstalled": []}


def test_siliconflow_knowledge_dependencies_only_install_chromadb(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        knowledge_embedding_provider="siliconflow",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    commands: list[tuple[list[str], bool]] = []
    environments = [
        {
            "runtimeChannel": "base",
            "chromadbInstalled": False,
            "chromadbVersion": "",
            "sentenceTransformersInstalled": False,
            "sentenceTransformersVersion": "",
            "knowledgeDependenciesReady": False,
        },
        {
            "runtimeChannel": "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": False,
            "sentenceTransformersVersion": "",
            "knowledgeDependenciesReady": True,
        },
    ]

    def fake_detect_environment(runtime_channel=None):
        return environments.pop(0)

    def fake_pip_install(python_executable, runtime_channel, packages, *, reinstall=False, **kwargs):
        commands.append((packages, reinstall))
        return type("Result", (), {"stdout": "installed chromadb", "stderr": ""})()

    monkeypatch.setattr(runtime_support, "detect_environment", fake_detect_environment)
    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )

    result, _worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        provider="siliconflow",
    )

    assert result["installed"] is True
    assert commands == [(["chromadb>=1.0.0"], False)]


def test_install_knowledge_dependencies_repairs_broken_imports(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    commands: list[tuple[list[str], bool]] = []
    environments = [
        {
            "runtimeChannel": "base",
            "chromadbInstalled": False,
            "chromadbVersion": "1.0.0",
            "chromadbError": "ImportError: missing dependency",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "sentenceTransformersError": "",
            "knowledgeDependenciesReady": False,
            "knowledgeDependenciesError": "ImportError: missing dependency",
        },
        {
            "runtimeChannel": "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "knowledgeDependenciesReady": True,
        },
    ]

    def fake_detect_environment(runtime_channel=None):
        return environments.pop(0)

    def fake_pip_install(python_executable, runtime_channel, packages, *, reinstall=False, **kwargs):
        commands.append((packages, reinstall))
        return type("Result", (), {"stdout": "repaired", "stderr": ""})()

    monkeypatch.setattr(runtime_support, "detect_environment", fake_detect_environment)
    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )

    response = install_knowledge_dependencies()

    assert response["installed"] is True
    assert response["repairReinstall"] is True
    assert commands == [(["chromadb>=1.0.0", "transformers>=4.40,<4.50", "sentence-transformers>=3.0"], True)]


def test_install_knowledge_dependencies_auto_uninstalls_broken_sentence_transformers(
    monkeypatch, tmp_path: Path
) -> None:
    """When switching to siliconflow, broken sentence-transformers should be auto-uninstalled."""
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    pip_commands: list[tuple[list[str], bool]] = []
    run_commands: list[list[str]] = []

    environments = [
        {
            "runtimeChannel": "base",
            "chromadbInstalled": False,
            "chromadbVersion": "",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "sentenceTransformersBroken": True,
            "sentenceTransformersError": "ImportError: broken",
            "modelscopeInstalled": False,
            "modelscopeVersion": "",
            "knowledgeDependenciesReady": False,
        },
        {
            "runtimeChannel": "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": False,
            "sentenceTransformersVersion": "",
            "knowledgeDependenciesReady": True,
        },
    ]

    def fake_detect_environment(runtime_channel=None):
        return environments.pop(0)

    def fake_pip_install(python_executable, runtime_channel, packages, *, reinstall=False, **kwargs):
        pip_commands.append((packages, reinstall))
        return type("Result", (), {"stdout": "installed", "stderr": ""})()

    def fake_run_command(command, runtime_channel, timeout=300):
        run_commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "uninstalled", "stderr": ""})()

    monkeypatch.setattr(runtime_support, "detect_environment", fake_detect_environment)
    monkeypatch.setattr(runtime_support, "append_install_log", lambda session_id, line: None)
    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)
    monkeypatch.setattr(runtime_support, "run_command", fake_run_command)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )

    result, _worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        provider="siliconflow",
    )

    assert result["installed"] is True
    # sentence-transformers should have been auto-uninstalled
    assert len(run_commands) == 1
    assert "uninstall" in run_commands[0]
    assert "sentence-transformers" in run_commands[0]
    # only chromadb should be pip-installed (siliconflow provider)
    # reinstall=True because sentenceTransformersVersion is set (repair mode)
    assert pip_commands == [(["chromadb>=1.0.0"], True)]


def test_install_knowledge_dependencies_auto_uninstall_failure_non_fatal(
    monkeypatch, tmp_path: Path
) -> None:
    """If auto-uninstall of broken residual packages fails, install should still proceed."""
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    import logging

    logger_warnings: list[str] = []
    original_warning = runtime_support.logger.warning

    def capture_warning(msg, *args, **kwargs):
        logger_warnings.append(msg % args if args else msg)
        return original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(runtime_support.logger, "warning", capture_warning)

    pip_commands: list[tuple[list[str], bool]] = []
    environments = [
        {
            "runtimeChannel": "base",
            "chromadbInstalled": False,
            "chromadbVersion": "",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "sentenceTransformersBroken": True,
            "sentenceTransformersError": "ImportError: broken",
            "modelscopeInstalled": True,
            "modelscopeVersion": "1.0",
            "modelscopeBroken": True,
            "modelscopeError": "ImportError: broken",
            "knowledgeDependenciesReady": False,
        },
        {
            "runtimeChannel": "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": False,
            "sentenceTransformersVersion": "",
            "modelscopeInstalled": False,
            "modelscopeVersion": "",
            "knowledgeDependenciesReady": True,
        },
    ]

    def fake_detect_environment(runtime_channel=None):
        return environments.pop(0)

    def fake_pip_install(python_executable, runtime_channel, packages, *, reinstall=False, **kwargs):
        pip_commands.append((packages, reinstall))
        return type("Result", (), {"stdout": "installed", "stderr": ""})()

    def fake_run_command(command, runtime_channel, timeout=300):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(runtime_support, "detect_environment", fake_detect_environment)
    monkeypatch.setattr(runtime_support, "append_install_log", lambda session_id, line: None)
    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)
    monkeypatch.setattr(runtime_support, "run_command", fake_run_command)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )

    result, _worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        provider="siliconflow",
    )

    # Installation should still succeed despite uninstall failure
    assert result["installed"] is True
    # There should be a warning logged about the uninstall failure
    assert any("failed to uninstall residual broken packages" in w for w in logger_warnings)
    # chromadb should still be installed (repair reinstall due to broken packages having versions)
    assert pip_commands == [(["chromadb>=1.0.0"], True)]


def test_install_knowledge_dependencies_no_auto_uninstall_when_required(
    monkeypatch, tmp_path: Path
) -> None:
    """Broken packages required by current provider should NOT be auto-uninstalled."""
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current
    app.state.task_repository = object()
    app.state.task_worker = object()

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    run_commands: list[list[str]] = []
    pip_commands: list[tuple[list[str], bool]] = []

    environments = [
        {
            "runtimeChannel": "base",
            "chromadbInstalled": False,
            "chromadbVersion": "",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "sentenceTransformersBroken": True,
            "sentenceTransformersError": "ImportError: broken",
            "knowledgeDependenciesReady": False,
        },
        {
            "runtimeChannel": "base",
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "knowledgeDependenciesReady": True,
        },
    ]

    def fake_detect_environment(runtime_channel=None):
        return environments.pop(0)

    def fake_pip_install(python_executable, runtime_channel, packages, *, reinstall=False, **kwargs):
        pip_commands.append((packages, reinstall))
        return type("Result", (), {"stdout": "installed", "stderr": ""})()

    def fake_run_command(command, runtime_channel, timeout=300):
        run_commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(runtime_support, "detect_environment", fake_detect_environment)
    monkeypatch.setattr(runtime_support, "pip_install_with_fallbacks", fake_pip_install)
    monkeypatch.setattr(runtime_support, "run_command", fake_run_command)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "repository": repository,
            "environment": environment_info,
        },
    )

    result, _worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        provider="local_huggingface",
    )

    assert result["installed"] is True
    # sentence-transformers is required by local_huggingface, so it should NOT be uninstalled
    assert len(run_commands) == 0


def test_install_knowledge_dependencies_can_target_runtime_channel(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
    )
    settings_manager._settings = current

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / runtime_channel / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)
    monkeypatch.setattr(
        runtime_support,
        "detect_environment",
        lambda runtime_channel=None: {
            "runtimeChannel": runtime_channel,
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "knowledgeDependenciesReady": True,
        },
    )
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "runtime_channel": current_settings.runtime_channel,
            "environment": environment_info,
        },
    )

    result, worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        runtime_channel="gpu-cu128",
    )

    assert result["runtimeChannel"] == "gpu-cu128"
    assert worker is None


def test_install_knowledge_dependencies_refreshes_worker_for_saved_runtime_channel(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="gpu-cu128",
    )
    settings_manager._settings = current

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / runtime_channel / "python.exe")
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "activate_runtime_pythonpath", lambda runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)
    monkeypatch.setattr(
        runtime_support,
        "detect_environment",
        lambda runtime_channel=None: {
            "runtimeChannel": runtime_channel,
            "chromadbInstalled": True,
            "chromadbVersion": "1.0.0",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.0.0",
            "knowledgeDependenciesReady": True,
        },
    )
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "runtime_channel": current_settings.runtime_channel,
            "environment": environment_info,
        },
    )

    result, worker = runtime_support.install_knowledge_dependencies(
        reinstall=False,
        repository=object(),
        runtime_channel="gpu-cu128",
    )

    assert result["runtimeChannel"] == "gpu-cu128"
    assert worker == {
        "runtime_channel": "gpu-cu128",
        "environment": result["environment"],
    }


def test_ensure_runtime_pip_tries_ensurepip_for_broken_pip(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, runtime_channel, timeout=120):
        calls.append(command)
        if command[-2:] == ["pip", "--version"] and len(calls) == 1:
            raise subprocess.CalledProcessError(1, command, stderr="ImportError: broken pip")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runtime_support, "run_command", fake_run)

    runtime_support.ensure_runtime_pip(tmp_path / "python.exe", "base")

    assert calls[0][-2:] == ["pip", "--version"]
    assert calls[1][-3:] == ["ensurepip", "--upgrade", "--default-pip"]
    assert calls[2][-2:] == ["pip", "--version"]


def test_install_workspace_packages_bootstraps_hatchling_before_local_packages(
    monkeypatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(service_app, "is_frozen", lambda: False)
    monkeypatch.setattr(service_app, "_ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(service_app, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        service_app,
        "_run_command",
        lambda command, runtime_channel, timeout=1800: commands.append(command) or type("Result", (), {"stdout": "", "stderr": ""})(),
    )

    service_app._install_workspace_packages(tmp_path / "python.exe", runtime_channel="gpu-cu128")

    assert len(commands) == 2
    assert commands[0][:7] == [
        str(tmp_path / "python.exe"),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "setuptools",
    ]
    assert "wheel" in commands[0]
    assert "hatchling>=1.27.0" in commands[0]
    assert commands[1][:5] == [
        str(tmp_path / "python.exe"),
        "-m",
        "pip",
        "install",
        "--no-build-isolation",
    ]
    assert "--no-deps" in commands[1]
    assert str(tmp_path / "packages" / "infra") in commands[1]
    assert str(tmp_path / "packages" / "core") in commands[1]
    assert str(tmp_path / "apps" / "service") in commands[1]


def test_install_workspace_packages_keeps_base_dependencies(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(service_app, "is_frozen", lambda: False)
    monkeypatch.setattr(
        service_app,
        "_ensure_runtime_pip",
        lambda python_executable, runtime_channel: None,
    )
    monkeypatch.setattr(service_app, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        service_app,
        "_run_command",
        lambda command, runtime_channel, timeout=1800: commands.append(command)
        or type("Result", (), {"stdout": "", "stderr": ""})(),
    )

    service_app._install_workspace_packages(tmp_path / "python.exe", runtime_channel="base")

    assert len(commands) == 2
    assert "--no-deps" not in commands[1]


def test_ensure_runtime_channel_syncs_base_preserves_cuda(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_site_packages = base_dir / "Lib" / "site-packages"
    gpu_site_packages = gpu_dir / "Lib" / "site-packages"
    base_scripts = base_dir / "Scripts"
    gpu_scripts = gpu_dir / "Scripts"
    base_stdlib = base_dir / "stdlib"
    gpu_stdlib = gpu_dir / "stdlib"
    base_dlls = base_dir / "DLLs"
    gpu_dlls = gpu_dir / "DLLs"
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    base_scripts.mkdir(parents=True)
    gpu_scripts.mkdir(parents=True)
    base_stdlib.mkdir(parents=True)
    gpu_stdlib.mkdir(parents=True)
    base_dlls.mkdir(parents=True)
    gpu_dlls.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("gpu-python", encoding="utf-8")
    (base_dir / "pythonw.exe").write_text("base-pythonw", encoding="utf-8")
    (gpu_dir / "pythonw.exe").write_text("gpu-pythonw", encoding="utf-8")
    (base_dir / "python3.dll").write_text("base-python3-dll", encoding="utf-8")
    (gpu_dir / "python3.dll").write_text("gpu-python3-dll", encoding="utf-8")
    (base_dir / "python312.dll").write_text("base-python312-dll", encoding="utf-8")
    (gpu_dir / "python312.dll").write_text("gpu-python312-dll", encoding="utf-8")
    (base_dir / "vcruntime140.dll").write_text("base-vcruntime", encoding="utf-8")
    (gpu_dir / "vcruntime140.dll").write_text("gpu-vcruntime", encoding="utf-8")
    (base_dir / "python312._pth").write_text("base-pth", encoding="utf-8")
    (gpu_dir / "python312._pth").write_text("gpu-pth", encoding="utf-8")
    (base_dir / "pyvenv.cfg").write_text("base-venv", encoding="utf-8")
    (gpu_dir / "pyvenv.cfg").write_text("old-venv", encoding="utf-8")
    (base_stdlib / "filecmp.py").write_text("base-stdlib", encoding="utf-8")
    (gpu_stdlib / "filecmp.py").write_text("old-stdlib", encoding="utf-8")
    (base_dlls / "_sqlite3.pyd").write_text("base-dll", encoding="utf-8")
    (gpu_dlls / "_sqlite3.pyd").write_text("old-dll", encoding="utf-8")
    (base_site_packages / "video_sum_service").mkdir()
    (base_site_packages / "video_sum_service" / "__init__.py").write_text(
        "version = 'new'",
        encoding="utf-8",
    )
    (base_site_packages / "video_sum_service-2.0.0.dist-info").mkdir()
    (base_site_packages / "video_sum_service-2.0.0.dist-info" / "METADATA").write_text(
        "new",
        encoding="utf-8",
    )
    (base_site_packages / "new_dependency").mkdir()
    (base_site_packages / "new_dependency" / "__init__.py").write_text(
        "value = 'base'",
        encoding="utf-8",
    )
    (base_site_packages / "new_dependency-2.0.0.dist-info").mkdir()
    (base_site_packages / "new_dependency-2.0.0.dist-info" / "METADATA").write_text(
        "Name: new-dependency",
        encoding="utf-8",
    )
    (base_site_packages / "torch").mkdir()
    (base_site_packages / "torch" / "cpu_marker.txt").write_text("cpu", encoding="utf-8")
    (base_site_packages / "nvidia_cublas_cu12-1.0.0.dist-info").mkdir()
    (base_site_packages / "nvidia_cublas_cu12-1.0.0.dist-info" / "METADATA").write_text(
        "cpu cuda wheel marker",
        encoding="utf-8",
    )
    (gpu_site_packages / "torch").mkdir()
    (gpu_site_packages / "torch" / "cuda_marker.txt").write_text("cuda", encoding="utf-8")
    (gpu_site_packages / "nvidia_cublas_cu12-0.9.0.dist-info").mkdir()
    (gpu_site_packages / "nvidia_cublas_cu12-0.9.0.dist-info" / "METADATA").write_text(
        "gpu cuda wheel marker",
        encoding="utf-8",
    )
    (gpu_site_packages / "video_sum_service").mkdir()
    (gpu_site_packages / "video_sum_service" / "__init__.py").write_text(
        "version = 'old'",
        encoding="utf-8",
    )
    (gpu_site_packages / "video_sum_service-1.0.0.dist-info").mkdir()
    (gpu_site_packages / "video_sum_service-1.0.0.dist-info" / "METADATA").write_text(
        "old",
        encoding="utf-8",
    )
    (base_scripts / "video-sum-transcribe-worker.exe").write_text("new-worker", encoding="utf-8")
    (gpu_scripts / "pip.exe").write_text("keep-pip", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        '{"runtimeChannel":"gpu-cu128","cudaVariant":"cu128","localAsrInstalled":true}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runtime_support,
        "managed_runtime_dir",
        lambda runtime_channel: runtime_root / runtime_channel,
    )
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda runtime_channel: runtime_root / runtime_channel / "python.exe"
        if (runtime_root / runtime_channel / "python.exe").exists()
        else None,
    )

    result = runtime_support.ensure_runtime_channel("gpu-cu128")
    metadata = runtime_support.read_runtime_metadata(gpu_dir)

    assert result == gpu_dir
    assert (gpu_dir / "python.exe").read_text(encoding="utf-8") == "base-python"
    assert (gpu_dir / "pythonw.exe").read_text(encoding="utf-8") == "base-pythonw"
    assert (gpu_dir / "python3.dll").read_text(encoding="utf-8") == "base-python3-dll"
    assert (gpu_dir / "python312.dll").read_text(encoding="utf-8") == "base-python312-dll"
    assert (gpu_dir / "vcruntime140.dll").read_text(encoding="utf-8") == "base-vcruntime"
    assert (gpu_dir / "python312._pth").read_text(encoding="utf-8") == "base-pth"
    assert not (gpu_dir / "pyvenv.cfg").exists()
    assert (gpu_stdlib / "filecmp.py").read_text(encoding="utf-8") == "base-stdlib"
    assert (gpu_dlls / "_sqlite3.pyd").read_text(encoding="utf-8") == "base-dll"
    assert (gpu_site_packages / "torch" / "cuda_marker.txt").exists()
    assert not (gpu_site_packages / "torch" / "cpu_marker.txt").exists()
    assert (gpu_site_packages / "nvidia_cublas_cu12-0.9.0.dist-info").exists()
    assert not (gpu_site_packages / "nvidia_cublas_cu12-1.0.0.dist-info").exists()
    assert (
        (gpu_site_packages / "video_sum_service" / "__init__.py").read_text(encoding="utf-8")
        == "version = 'new'"
    )
    assert (gpu_site_packages / "video_sum_service-2.0.0.dist-info").exists()
    assert not (gpu_site_packages / "video_sum_service-1.0.0.dist-info").exists()
    assert (gpu_site_packages / "new_dependency" / "__init__.py").read_text(encoding="utf-8") == "value = 'base'"
    assert (gpu_site_packages / "new_dependency-2.0.0.dist-info").exists()
    assert (gpu_scripts / "pip.exe").read_text(encoding="utf-8") == "keep-pip"
    assert (
        (gpu_scripts / "video-sum-transcribe-worker.exe").read_text(encoding="utf-8")
        == "new-worker"
    )
    assert metadata["appVersion"] == "2.0.0"
    assert metadata["runtimeLayout"] == "portable-cpython"
    assert metadata["cudaVariant"] == "cu128"
    assert metadata["localAsrInstalled"] is True


def test_ensure_runtime_channel_syncs_base_preserves_macos_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_site_packages = base_dir / "lib" / "python3.12" / "site-packages"
    gpu_site_packages = gpu_dir / "lib" / "python3.12" / "site-packages"
    base_bin = base_dir / "bin"
    gpu_bin = gpu_dir / "bin"
    base_lib = base_dir / "lib"
    gpu_lib = gpu_dir / "lib"
    base_stdlib = base_dir / "stdlib"
    gpu_stdlib = gpu_dir / "stdlib"
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    base_bin.mkdir(parents=True)
    gpu_bin.mkdir(parents=True)
    base_stdlib.mkdir(parents=True)
    gpu_stdlib.mkdir(parents=True)
    (base_bin / "python").write_text("base-python", encoding="utf-8")
    (gpu_bin / "python").write_text("gpu-python", encoding="utf-8")
    (base_lib / "libpython3.12.dylib").write_text("base-libpython", encoding="utf-8")
    (gpu_lib / "libpython3.12.dylib").write_text("gpu-libpython", encoding="utf-8")
    (base_dir / "pythonpath.pth").write_text("base-pythonpath", encoding="utf-8")
    (gpu_dir / "pythonpath.pth").write_text("gpu-pythonpath", encoding="utf-8")
    (gpu_dir / "pyvenv.cfg").write_text("old-venv", encoding="utf-8")
    (base_stdlib / "filecmp.py").write_text("base-stdlib", encoding="utf-8")
    (gpu_stdlib / "filecmp.py").write_text("gpu-stdlib", encoding="utf-8")
    (base_site_packages / "video_sum_service").mkdir()
    (base_site_packages / "video_sum_service" / "__init__.py").write_text(
        "version = 'new'",
        encoding="utf-8",
    )
    (base_site_packages / "video_sum_service-2.0.0.dist-info").mkdir()
    (base_site_packages / "video_sum_service-2.0.0.dist-info" / "METADATA").write_text(
        "new",
        encoding="utf-8",
    )
    (gpu_site_packages / "video_sum_service").mkdir()
    (gpu_site_packages / "video_sum_service" / "__init__.py").write_text(
        "version = 'old'",
        encoding="utf-8",
    )
    (gpu_site_packages / "video_sum_service-1.0.0.dist-info").mkdir()
    (gpu_site_packages / "video_sum_service-1.0.0.dist-info" / "METADATA").write_text(
        "old",
        encoding="utf-8",
    )
    (base_bin / "video-sum-transcribe-worker").write_text("new-worker", encoding="utf-8")
    (gpu_bin / "pip").write_text("keep-pip", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        '{"runtimeChannel":"gpu-cu128","cudaVariant":"cu128","localAsrInstalled":true}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runtime_support,
        "managed_runtime_dir",
        lambda runtime_channel: runtime_root / runtime_channel,
    )
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda runtime_channel: runtime_root / runtime_channel / "bin" / "python"
        if (runtime_root / runtime_channel / "bin" / "python").exists()
        else None,
    )

    result = runtime_support.ensure_runtime_channel("gpu-cu128")
    metadata = runtime_support.read_runtime_metadata(gpu_dir)

    assert result == gpu_dir
    assert (gpu_bin / "python").read_text(encoding="utf-8") == "base-python"
    assert (gpu_lib / "libpython3.12.dylib").read_text(encoding="utf-8") == "base-libpython"
    assert (gpu_dir / "pythonpath.pth").read_text(encoding="utf-8") == "base-pythonpath"
    assert not (gpu_dir / "pyvenv.cfg").exists()
    assert (gpu_stdlib / "filecmp.py").read_text(encoding="utf-8") == "base-stdlib"
    assert (
        (gpu_site_packages / "video_sum_service" / "__init__.py").read_text(encoding="utf-8")
        == "version = 'new'"
    )
    assert (gpu_site_packages / "video_sum_service-2.0.0.dist-info").exists()
    assert not (gpu_site_packages / "video_sum_service-1.0.0.dist-info").exists()
    assert (gpu_bin / "pip").read_text(encoding="utf-8") == "keep-pip"
    assert (gpu_bin / "video-sum-transcribe-worker").read_text(encoding="utf-8") == "new-worker"
    assert metadata["appVersion"] == "2.0.0"
    assert metadata["runtimeLayout"] == "portable-cpython"
    assert metadata["pythonVersion"] == "3.12.0"
    assert metadata["cudaVariant"] == "cu128"
    assert metadata["localAsrInstalled"] is True


def test_ensure_runtime_channel_syncs_macos_python_entrypoints(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_bin = base_dir / "bin"
    gpu_bin = gpu_dir / "bin"
    base_site_packages = base_dir / "lib" / "python3.12" / "site-packages"
    gpu_site_packages = gpu_dir / "lib" / "python3.12" / "site-packages"
    base_bin.mkdir(parents=True)
    gpu_bin.mkdir(parents=True)
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    (base_bin / "python").write_text("base-python", encoding="utf-8")
    (base_bin / "python3").write_text("base-python3", encoding="utf-8")
    (base_bin / "python3.12").write_text("base-python3.12", encoding="utf-8")
    (base_bin / "pip").write_text("base-pip", encoding="utf-8")
    (base_bin / "video-sum-transcribe-worker").write_text("new-worker", encoding="utf-8")
    (gpu_bin / "python").write_text("old-python", encoding="utf-8")
    (gpu_bin / "python3").write_text("old-python3", encoding="utf-8")
    (gpu_bin / "python3.12").write_text("old-python3.12", encoding="utf-8")
    (gpu_bin / "pip").write_text("keep-pip", encoding="utf-8")
    (gpu_bin / "video-sum-transcribe-worker").write_text("old-worker", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"1.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runtime_support,
        "managed_runtime_dir",
        lambda runtime_channel: runtime_root / runtime_channel,
    )
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda runtime_channel: runtime_root / runtime_channel / "bin" / "python"
        if (runtime_root / runtime_channel / "bin" / "python").exists()
        else None,
    )

    runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_bin / "python").read_text(encoding="utf-8") == "base-python"
    assert (gpu_bin / "python3").read_text(encoding="utf-8") == "base-python3"
    assert (gpu_bin / "python3.12").read_text(encoding="utf-8") == "base-python3.12"
    assert (gpu_bin / "pip").read_text(encoding="utf-8") == "keep-pip"
    assert (gpu_bin / "video-sum-transcribe-worker").read_text(encoding="utf-8") == "new-worker"


def test_ensure_runtime_channel_restores_gpu_runtime_when_sync_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_site_packages = base_dir / "Lib" / "site-packages"
    gpu_site_packages = gpu_dir / "Lib" / "site-packages"
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("gpu-python", encoding="utf-8")
    (base_site_packages / "video_sum_service").mkdir()
    (base_site_packages / "video_sum_service" / "__init__.py").write_text("new", encoding="utf-8")
    (gpu_site_packages / "video_sum_service").mkdir()
    (gpu_site_packages / "video_sum_service" / "__init__.py").write_text("old", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"1.0.0","pythonVersion":"3.12.0","cudaVariant":"cu128",'
            '"localAsrInstalled":true}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    original_copy_runtime_item = runtime_support.copy_runtime_item

    def failing_copy_runtime_item(source: Path, target: Path) -> None:
        if source.name == "video_sum_service":
            raise OSError("sync failed")
        original_copy_runtime_item(source, target)

    monkeypatch.setattr(runtime_support, "copy_runtime_item", failing_copy_runtime_item)

    with pytest.raises(OSError, match="sync failed"):
        runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_dir / "python.exe").read_text(encoding="utf-8") == "gpu-python"
    assert (gpu_site_packages / "video_sum_service" / "__init__.py").read_text(encoding="utf-8") == "old"
    assert runtime_support.read_runtime_metadata(gpu_dir)["appVersion"] == "1.0.0"
    assert not (runtime_root / ".gpu-cu128-refresh-backup").exists()


def test_ensure_runtime_channel_does_not_restore_partial_gpu_backup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    backup_dir = runtime_root / ".gpu-cu128-refresh-backup"
    backup_temp_dir = runtime_root / "..gpu-cu128-refresh-backup-temp"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    backup_temp_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("gpu-python", encoding="utf-8")
    (backup_temp_dir / "python.exe").write_text("partial-backup-python", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"1.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    original_copytree = runtime_support.shutil.copytree

    def failing_copytree(source, destination, *args, **kwargs):
        if Path(destination) == backup_temp_dir:
            backup_temp_dir.mkdir(parents=True, exist_ok=True)
            (backup_temp_dir / "leftover.txt").write_text("partial", encoding="utf-8")
            raise OSError("backup failed")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtime_support.shutil, "copytree", failing_copytree)

    with pytest.raises(OSError, match="backup failed"):
        runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_dir / "python.exe").read_text(encoding="utf-8") == "gpu-python"
    assert not backup_dir.exists()
    assert not backup_temp_dir.exists()

    monkeypatch.setattr(runtime_support.shutil, "copytree", original_copytree)
    assert runtime_support.ensure_runtime_channel("gpu-cu128") == gpu_dir
    assert (gpu_dir / "python.exe").read_text(encoding="utf-8") == "base-python"
    assert not (gpu_dir / "leftover.txt").exists()


def test_ensure_runtime_channel_restores_interrupted_gpu_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    backup_dir = runtime_root / ".gpu-cu128-refresh-backup"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("partial-python", encoding="utf-8")
    (backup_dir / "python.exe").write_text("backup-python", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (backup_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0","cudaVariant":"cu128"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    assert runtime_support.ensure_runtime_channel("gpu-cu128") == gpu_dir

    assert (gpu_dir / "python.exe").read_text(encoding="utf-8") == "backup-python"
    assert not backup_dir.exists()


def test_ensure_runtime_channel_restores_not_ready_gpu_runtime_when_copy_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "user-package.txt").write_text("keep me", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    original_copytree = runtime_support.shutil.copytree

    def failing_copytree(source, destination, *args, **kwargs):
        if Path(source) == base_dir:
            raise OSError("copy failed")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtime_support.shutil, "copytree", failing_copytree)

    with pytest.raises(OSError, match="copy failed"):
        runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_dir / "user-package.txt").read_text(encoding="utf-8") == "keep me"
    assert not (runtime_root / ".gpu-cu128-refresh-backup").exists()


def test_ensure_runtime_channel_syncs_base_preserves_extension_dependency_closure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_site_packages = base_dir / "Lib" / "site-packages"
    gpu_site_packages = gpu_dir / "Lib" / "site-packages"
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("gpu-python", encoding="utf-8")
    (base_site_packages / "chromadb").mkdir()
    (base_site_packages / "chromadb" / "__init__.py").write_text("base chroma", encoding="utf-8")
    (base_site_packages / "numpy").mkdir()
    (base_site_packages / "numpy" / "__init__.py").write_text("base numpy", encoding="utf-8")
    (base_site_packages / "numpy-2.0.0.dist-info").mkdir()
    (base_site_packages / "numpy-2.0.0.dist-info" / "METADATA").write_text(
        "Name: numpy\n",
        encoding="utf-8",
    )
    (base_site_packages / "pydantic").mkdir()
    (base_site_packages / "pydantic" / "__init__.py").write_text("base pydantic", encoding="utf-8")
    (gpu_site_packages / "chromadb").mkdir()
    (gpu_site_packages / "chromadb" / "__init__.py").write_text("gpu chroma", encoding="utf-8")
    (gpu_site_packages / "chromadb-1.0.0.dist-info").mkdir()
    (gpu_site_packages / "chromadb-1.0.0.dist-info" / "METADATA").write_text(
        "Name: chromadb\nRequires-Dist: numpy>=1.26\nRequires-Dist: helper-extra[fast] (>=1); python_version >= '3.12'\n",
        encoding="utf-8",
    )
    (gpu_site_packages / "numpy").mkdir()
    (gpu_site_packages / "numpy" / "__init__.py").write_text("gpu numpy", encoding="utf-8")
    (gpu_site_packages / "numpy-1.26.0.dist-info").mkdir()
    (gpu_site_packages / "numpy-1.26.0.dist-info" / "METADATA").write_text(
        "Name: numpy\n",
        encoding="utf-8",
    )
    (gpu_site_packages / "pydantic").mkdir()
    (gpu_site_packages / "pydantic" / "__init__.py").write_text("old pydantic", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"1.0.0","pythonVersion":"3.12.0","cudaVariant":"cu128"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_site_packages / "chromadb" / "__init__.py").read_text(encoding="utf-8") == "gpu chroma"
    assert (gpu_site_packages / "numpy" / "__init__.py").read_text(encoding="utf-8") == "gpu numpy"
    assert (gpu_site_packages / "numpy-1.26.0.dist-info").exists()
    assert not (gpu_site_packages / "numpy-2.0.0.dist-info").exists()
    assert (gpu_site_packages / "pydantic" / "__init__.py").read_text(encoding="utf-8") == "base pydantic"


def test_ensure_runtime_channel_syncs_base_preserves_extension_top_level_packages(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_site_packages = base_dir / "Lib" / "site-packages"
    gpu_site_packages = gpu_dir / "Lib" / "site-packages"
    base_site_packages.mkdir(parents=True)
    gpu_site_packages.mkdir(parents=True)
    (base_dir / "python.exe").write_text("base-python", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("gpu-python", encoding="utf-8")
    (base_site_packages / "chromadb").mkdir()
    (base_site_packages / "chromadb" / "__init__.py").write_text("base chroma", encoding="utf-8")
    (base_site_packages / "PIL").mkdir()
    (base_site_packages / "PIL" / "__init__.py").write_text("base pillow", encoding="utf-8")
    (base_site_packages / "Pillow-11.0.0.dist-info").mkdir()
    (base_site_packages / "Pillow-11.0.0.dist-info" / "METADATA").write_text(
        "Name: Pillow\n",
        encoding="utf-8",
    )
    (gpu_site_packages / "chromadb").mkdir()
    (gpu_site_packages / "chromadb" / "__init__.py").write_text("gpu chroma", encoding="utf-8")
    (gpu_site_packages / "chromadb-1.0.0.dist-info").mkdir()
    (gpu_site_packages / "chromadb-1.0.0.dist-info" / "METADATA").write_text(
        "Name: chromadb\nRequires-Dist: Pillow>=10\n",
        encoding="utf-8",
    )
    (gpu_site_packages / "PIL").mkdir()
    (gpu_site_packages / "PIL" / "__init__.py").write_text("gpu pillow", encoding="utf-8")
    (gpu_site_packages / "Pillow-10.0.0.dist-info").mkdir()
    (gpu_site_packages / "Pillow-10.0.0.dist-info" / "METADATA").write_text(
        "Name: Pillow\n",
        encoding="utf-8",
    )
    (gpu_site_packages / "Pillow-10.0.0.dist-info" / "top_level.txt").write_text(
        "PIL\n",
        encoding="utf-8",
    )
    (base_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"base","runtimeLayout":"portable-cpython",'
            '"appVersion":"2.0.0","pythonVersion":"3.12.0"}'
        ),
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeChannel":"gpu-cu128","runtimeLayout":"portable-cpython",'
            '"appVersion":"1.0.0","pythonVersion":"3.12.0","cudaVariant":"cu128"}'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "bootstrap_managed_runtime",
        lambda runtime_channel: base_dir if runtime_channel == "base" else None,
    )
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    runtime_support.ensure_runtime_channel("gpu-cu128")

    assert (gpu_site_packages / "PIL" / "__init__.py").read_text(encoding="utf-8") == "gpu pillow"
    assert (gpu_site_packages / "Pillow-10.0.0.dist-info").exists()
    assert not (gpu_site_packages / "Pillow-11.0.0.dist-info").exists()


def test_inspect_runtime_channels_reports_outdated_runtime(monkeypatch, tmp_path: Path) -> None:
    runtime_support._invalidate_inspect_channels_cache()
    runtime_support._invalidate_inspect_channels_cache()
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0"}',
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"1.0.0","cudaVariant":"cu128"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    bootstrap_calls: list[str] = []
    monkeypatch.setattr(runtime_support, "bootstrap_managed_runtime", lambda channel: bootstrap_calls.append(channel))
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    payload = runtime_support.inspect_runtime_channels()
    gpu_status = next(
        channel for channel in payload["channels"] if channel["runtimeChannel"] == "gpu-cu128"
    )

    assert payload["baseAppVersion"] == "2.0.0"
    assert gpu_status["needsUpdate"] is True
    assert gpu_status["cudaVariant"] == "cu128"
    assert [item["label"] for item in payload["pipIndexes"]] == ["official", "tsinghua", "aliyun"]
    assert bootstrap_calls == []


def test_inspect_runtime_channels_ignores_backup_and_temp_dirs(monkeypatch, tmp_path: Path) -> None:
    runtime_support._invalidate_inspect_channels_cache()
    runtime_root = tmp_path / "runtime"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    backup_dir = runtime_root / ".gpu-cu128-refresh-backup"
    temp_dir = runtime_root / ".gpu-cu128-refresh-temp"
    unrelated_dir = runtime_root / "notes"
    for directory in (base_dir, gpu_dir, backup_dir, temp_dir, unrelated_dir):
        directory.mkdir(parents=True)
    (base_dir / "python.exe").write_text("", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (backup_dir / "python.exe").write_text("", encoding="utf-8")
    (temp_dir / "python.exe").write_text("", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_support, "managed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    payload = runtime_support.inspect_runtime_channels()
    channels = {channel["runtimeChannel"] for channel in payload["channels"]}

    assert "gpu-cu128" in channels
    assert ".gpu-cu128-refresh-backup" not in channels
    assert ".gpu-cu128-refresh-temp" not in channels
    assert "notes" not in channels


def test_sync_runtime_channel_rejects_invalid_runtime_channel() -> None:
    with pytest.raises(HTTPException) as exc_info:
        runtime_support.sync_runtime_channel("../outside")

    assert exc_info.value.status_code == 400


def test_inspect_runtime_channels_prefers_cached_environment_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_support._invalidate_inspect_channels_cache()
    runtime_root = tmp_path / "runtime"
    cache_dir = tmp_path / "cache"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0",'
            '"localAsrInstalled":true,"knowledgeDependenciesReady":true}'
        ),
        encoding="utf-8",
    )
    (cache_dir / "environment-probe-cache.json").write_text(
        """
        {
          "gpu-cu128": {
            "runtimeChannel": "gpu-cu128",
            "runtimeReady": true,
            "runtimePython": "%s",
            "runtimePath": "%s",
            "appVersion": "2.0.0",
            "runtimeLayout": "portable-cpython",
            "pythonVersion": "3.12.0",
            "localAsrInstalled": false,
            "knowledgeDependenciesReady": false
          }
        }
        """
        % (
            str(gpu_dir / "python.exe").replace("\\", "\\\\"),
            str(gpu_dir).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    runtime_support._environment_probe_cache.clear()
    current = ServiceSettings(cache_dir=cache_dir, runtime_channel="gpu-cu128")

    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "managed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    payload = runtime_support.inspect_runtime_channels()
    gpu_status = next(
        channel for channel in payload["channels"] if channel["runtimeChannel"] == "gpu-cu128"
    )

    assert gpu_status["localAsrInstalled"] is False
    assert gpu_status["knowledgeDependenciesReady"] is False
    assert gpu_status["environmentStatusSource"] == "probe-cache"


def test_inspect_runtime_channels_ignores_stale_cached_environment_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_support._invalidate_inspect_channels_cache()
    runtime_root = tmp_path / "runtime"
    cache_dir = tmp_path / "cache"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0",'
            '"localAsrInstalled":true,"knowledgeDependenciesReady":true}'
        ),
        encoding="utf-8",
    )
    (cache_dir / "environment-probe-cache.json").write_text(
        """
        {
          "gpu-cu128": {
            "runtimeChannel": "gpu-cu128",
            "runtimeReady": true,
            "runtimePython": "%s",
            "runtimePath": "%s",
            "appVersion": "2.0.0",
            "runtimeLayout": "portable-cpython",
            "pythonVersion": "3.12.0",
            "localAsrInstalled": false,
            "knowledgeDependenciesReady": false
          }
        }
        """
        % (
            str(runtime_root / "old-gpu-cu128" / "python.exe").replace("\\", "\\\\"),
            str(gpu_dir).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    runtime_support._environment_probe_cache.clear()
    current = ServiceSettings(cache_dir=cache_dir, runtime_channel="gpu-cu128")

    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "managed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    payload = runtime_support.inspect_runtime_channels()
    gpu_status = next(
        channel for channel in payload["channels"] if channel["runtimeChannel"] == "gpu-cu128"
    )

    assert gpu_status["localAsrInstalled"] is True
    assert gpu_status["knowledgeDependenciesReady"] is True
    assert gpu_status["environmentStatusSource"] == "metadata"


def test_inspect_runtime_channels_ignores_cache_when_runtime_metadata_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_support._invalidate_inspect_channels_cache()
    runtime_root = tmp_path / "runtime"
    cache_dir = tmp_path / "cache"
    base_dir = runtime_root / "base"
    gpu_dir = runtime_root / "gpu-cu128"
    base_dir.mkdir(parents=True)
    gpu_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (base_dir / "python.exe").write_text("", encoding="utf-8")
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (base_dir / "video_sum_runtime.json").write_text(
        '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (gpu_dir / "video_sum_runtime.json").write_text(
        (
            '{"runtimeLayout":"portable-cpython","appVersion":"2.0.0","pythonVersion":"3.12.0",'
            '"localAsrInstalled":true,"knowledgeDependenciesReady":true}'
        ),
        encoding="utf-8",
    )
    (cache_dir / "environment-probe-cache.json").write_text(
        """
        {
          "gpu-cu128": {
            "runtimeChannel": "gpu-cu128",
            "runtimeReady": true,
            "runtimePython": "%s",
            "runtimePath": "%s",
            "appVersion": "1.0.0",
            "runtimeLayout": "portable-cpython",
            "pythonVersion": "3.12.0",
            "localAsrInstalled": false,
            "knowledgeDependenciesReady": false
          }
        }
        """
        % (
            str(gpu_dir / "python.exe").replace("\\", "\\\\"),
            str(gpu_dir).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    runtime_support._environment_probe_cache.clear()
    current = ServiceSettings(cache_dir=cache_dir, runtime_channel="gpu-cu128")

    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "managed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )

    payload = runtime_support.inspect_runtime_channels()
    gpu_status = next(
        channel for channel in payload["channels"] if channel["runtimeChannel"] == "gpu-cu128"
    )

    assert gpu_status["localAsrInstalled"] is True
    assert gpu_status["knowledgeDependenciesReady"] is True
    assert gpu_status["environmentStatusSource"] == "metadata"


def test_detect_environment_ignores_false_cache_after_runtime_recovers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    cache_dir = tmp_path / "cache"
    gpu_dir = runtime_root / "gpu-cu128"
    gpu_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (gpu_dir / "python.exe").write_text("", encoding="utf-8")
    (cache_dir / "environment-probe-cache.json").write_text(
        """
        {
          "gpu-cu128": {
            "runtimeChannel": "gpu-cu128",
            "runtimeReady": false,
            "runtimePython": "",
            "localAsrInstalled": false,
            "knowledgeDependenciesReady": false
          }
        }
        """,
        encoding="utf-8",
    )
    runtime_support._environment_probe_cache.clear()
    current = ServiceSettings(cache_dir=cache_dir, runtime_channel="gpu-cu128")
    run_calls: list[list[str]] = []

    monkeypatch.setattr(runtime_support.settings_manager, "_settings", current)
    monkeypatch.setattr(runtime_support, "managed_runtime_dir", lambda channel: runtime_root / channel)
    monkeypatch.setattr(
        runtime_support,
        "runtime_python_executable",
        lambda channel: runtime_root / channel / "python.exe"
        if (runtime_root / channel / "python.exe").exists()
        else None,
    )
    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)

    def fake_run_command(command, runtime_channel, timeout=120):
        run_calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"pythonVersion":"3.12.0","torchInstalled":false,"torchVersion":"","cudaAvailable":false,"gpuName":"","ytDlpVersion":"ok","localAsrInstalled":false,"localAsrAvailable":false,"localAsrVersion":"","chromadbInstalled":false,"chromadbVersion":"","sentenceTransformersInstalled":false,"sentenceTransformersVersion":"","knowledgeDependenciesReady":false,"ffmpegLocation":"","recommendedModel":"base","recommendedDevice":"cpu"}',
            stderr="",
        )

    monkeypatch.setattr(runtime_support, "run_command", fake_run_command)

    environment = runtime_support.detect_environment("gpu-cu128")

    assert environment["runtimeReady"] is True
    assert run_calls


def test_torch_install_with_fallbacks_accepts_custom_cuda_index(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    monkeypatch.setenv("VIDEO_SUM_TORCH_INDEX_URLS", "https://mirror.example/pytorch/cu128")

    def fake_run(command, runtime_channel, timeout=1800):
        commands.append(command)
        if command[-1] == "https://download.pytorch.org/whl/cu128":
            raise subprocess.CalledProcessError(1, command, stderr="network error")
        return type("Result", (), {"stdout": "ok", "stderr": ""})()

    runtime_support.torch_install_with_fallbacks(
        tmp_path / "python.exe",
        "gpu-cu128",
        "cu128",
        runner=fake_run,
    )

    assert len(commands) == 2
    assert commands[0][-1] == "https://download.pytorch.org/whl/cu128"
    assert commands[1][-1] == "https://mirror.example/pytorch/cu128"


def test_cleanup_invalid_runtime_distributions_removes_pip_leftovers(monkeypatch, tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    stale_dist = site_packages / "~~mpy-1.14.0.dist-info"
    stale_libs = site_packages / "~umpy.libs"
    valid_package = site_packages / "numpy"
    stale_dist.mkdir()
    stale_libs.mkdir()
    valid_package.mkdir()

    monkeypatch.setattr(runtime_support, "runtime_site_packages_dir", lambda runtime_channel: site_packages)

    removed = runtime_support.cleanup_invalid_runtime_distributions("gpu-cu128")

    assert set(removed) == {"~~mpy-1.14.0.dist-info", "~umpy.libs"}
    assert not stale_dist.exists()
    assert not stale_libs.exists()
    assert valid_package.exists()


def test_pip_install_with_fallbacks_repairs_gpu_torch_family(monkeypatch, tmp_path: Path) -> None:
    runner_commands: list[list[str]] = []
    repair_calls: list[dict[str, object]] = []
    probes = [
        {
            "torch": {"version": "2.12.0+cpu", "distributionVersion": "2.12.0", "error": ""},
            "torchvision": {"version": "0.26.0+cu128", "distributionVersion": "0.26.0+cu128", "error": "operator torchvision::nms does not exist"},
            "torchaudio": {"version": "2.11.0+cu128", "distributionVersion": "2.11.0+cu128", "error": "libtorchaudio.pyd"},
        },
        {
            "torch": {"version": "2.12.0+cu128", "distributionVersion": "2.12.0+cu128", "error": ""},
            "torchvision": {"version": "0.27.0+cu128", "distributionVersion": "0.27.0+cu128", "error": ""},
            "torchaudio": {"version": "2.12.0+cu128", "distributionVersion": "2.12.0+cu128", "error": ""},
        },
    ]

    def fake_runner(command, runtime_channel, timeout=1800):
        runner_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="plain install", stderr="")

    def fake_torch_install(python_executable, runtime_channel, cuda_variant, *, timeout=1800, runner=None, reinstall=False):
        repair_calls.append({"runtime_channel": runtime_channel, "cuda_variant": cuda_variant, "reinstall": reinstall})
        return subprocess.CompletedProcess(["torch-install"], 0, stdout="torch repaired", stderr="")

    monkeypatch.setattr(runtime_support, "cleanup_invalid_runtime_distributions", lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime_support, "_probe_torch_family", lambda *args, **kwargs: probes.pop(0))
    monkeypatch.setattr(runtime_support, "torch_install_with_fallbacks", fake_torch_install)

    result = runtime_support.pip_install_with_fallbacks(
        tmp_path / "python.exe",
        "gpu-cu128",
        ["funasr>=1.1.0"],
        package_label="FunASR 依赖",
        runner=fake_runner,
    )

    assert runner_commands[0][-1] == "funasr>=1.1.0"
    assert repair_calls == [{"runtime_channel": "gpu-cu128", "cuda_variant": "cu128", "reinstall": True}]
    assert "plain install" in result.stdout
    assert "torch repaired" in result.stdout


def test_install_funasr_gpu_repairs_torch_family_before_funasr_pip(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="gpu-cu128",
    )
    settings_manager._settings = current

    monkeypatch.setattr(runtime_support, "uses_current_service_python", lambda runtime_channel: False)
    monkeypatch.setattr(runtime_support, "runtime_python_executable", lambda runtime_channel: tmp_path / "python.exe")
    monkeypatch.setattr(runtime_support, "ensure_runtime_channel", lambda runtime_channel: tmp_path / runtime_channel)
    monkeypatch.setattr(runtime_support, "install_workspace_packages", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "ensure_runtime_pip", lambda python_executable, runtime_channel: None)
    monkeypatch.setattr(runtime_support, "clear_environment_probe_cache", lambda runtime_channel=None: None)
    monkeypatch.setattr(runtime_support, "write_runtime_metadata", lambda runtime_channel, payload: None)

    environments = [
        {
            "runtimeChannel": "gpu-cu128",
            "funasrInstalled": False,
            "funasrVersion": "",
            "funasrError": "",
        },
        {
            "runtimeChannel": "gpu-cu128",
            "funasrInstalled": True,
            "funasrAvailable": True,
            "funasrVersion": "1.3.9",
        },
    ]
    monkeypatch.setattr(runtime_support, "detect_environment", lambda runtime_channel=None: environments.pop(0))

    torch_checks: list[dict[str, object]] = []
    pip_installs: list[list[str]] = []

    def fake_ensure_torch(python_executable, runtime_channel, *, package_label, runner, install_if_missing=False):
        torch_checks.append({"runtime_channel": runtime_channel, "install_if_missing": install_if_missing})
        return subprocess.CompletedProcess(["torch-install"], 0, stdout="torch ok", stderr="")

    def fake_pip_install(python_executable, runtime_channel, packages, **kwargs):
        pip_installs.append(packages)
        return subprocess.CompletedProcess(["pip-install"], 0, stdout="funasr ok", stderr="")

    monkeypatch.setattr(runtime_support, "ensure_torch_family_compatible", fake_ensure_torch)
    monkeypatch.setattr(runtime_support, "_run_pip_install", fake_pip_install)
    monkeypatch.setattr(
        runtime_support,
        "build_worker",
        lambda repository, current_settings, environment_info=None: {
            "environment": environment_info,
        },
    )

    result, worker = runtime_support.install_funasr(
        reinstall=False,
        repository=object(),
    )

    assert result["installed"] is True
    assert worker == {"environment": result["environment"]}
    assert torch_checks == [{"runtime_channel": "gpu-cu128", "install_if_missing": True}]
    assert pip_installs == [["transformers>=4.40,<4.50", "funasr>=1.1.0"]]


def test_llm_connection_uses_unsaved_payload(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://old.example/v1",
        llm_api_key="old-key",
        llm_model="old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"message":"test"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(
        SettingsUpdatePayload(
            llm_base_url="https://api.example.com/v1",
            llm_api_key="new-key",
            llm_model="new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "new-model"
    assert response["jsonOutputAvailable"] is True
    assert response["jsonPreview"] == '{"ok": true, "message": "test"}'
    assert calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer new-key"
    assert calls[0]["json"]["model"] == "new-model"


def test_llm_connection_uses_saved_api_key_when_payload_blanks_key(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://old.example/v1",
        llm_api_key="saved-key",
        llm_model="old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"message":"test"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(
        SettingsUpdatePayload(
            llm_base_url="https://api.example.com/v1",
            llm_api_key="",
            llm_model="new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "new-model"
    assert calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer saved-key"
    assert calls[0]["json"]["model"] == "new-model"


def test_knowledge_llm_connection_uses_saved_api_key_when_payload_masks_key(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://main.example/v1",
        llm_api_key="main-key",
        llm_model="main-model",
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://knowledge-old.example/v1",
        knowledge_llm_api_key="knowledge-saved-key",
        knowledge_llm_model="knowledge-old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"message":"test"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(
        SettingsUpdatePayload(
            llm_test_scope="knowledge",
            llm_enabled=True,
            llm_base_url="https://knowledge-new.example/v1",
            llm_api_key="******",
            llm_model="knowledge-new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "knowledge-new-model"
    assert calls[0]["url"] == "https://knowledge-new.example/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer knowledge-saved-key"
    assert calls[0]["json"]["model"] == "knowledge-new-model"


def test_knowledge_llm_connection_uses_unsaved_api_key_when_present(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://main.example/v1",
        llm_api_key="main-key",
        llm_model="main-model",
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_base_url="https://knowledge-old.example/v1",
        knowledge_llm_api_key="knowledge-saved-key",
        knowledge_llm_model="knowledge-old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"message":"test"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(
        SettingsUpdatePayload(
            llm_test_scope="knowledge",
            llm_enabled=True,
            llm_base_url="https://knowledge-new.example/v1",
            llm_api_key="knowledge-new-key",
            llm_model="knowledge-new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "knowledge-new-model"
    assert calls[0]["url"] == "https://knowledge-new.example/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer knowledge-new-key"
    assert calls[0]["json"]["model"] == "knowledge-new-model"


def test_knowledge_llm_connection_uses_custom_provider(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_provider="openai-compatible",
        llm_base_url="https://main.example/v1",
        llm_api_key="main-key",
        llm_model="main-model",
        knowledge_llm_mode="custom",
        knowledge_llm_enabled=True,
        knowledge_llm_provider="anthropic",
        knowledge_llm_base_url="https://api.anthropic.com/v1",
        knowledge_llm_api_key="knowledge-key",
        knowledge_llm_model="claude-3-5-haiku-latest",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"content":[{"type":"text","text":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}]}'

        def json(self) -> dict[str, object]:
            return {"content": [{"type": "text", "text": '{"ok":true,"message":"test"}'}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(SettingsUpdatePayload(llm_test_scope="knowledge"))

    assert response["ok"] is True
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "knowledge-key"
    assert calls[0]["headers"]["anthropic-version"]
    assert calls[0]["json"]["model"] == "claude-3-5-haiku-latest"


def test_visual_llm_connection_sends_image_and_uses_visual_config(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://main.example/v1",
        llm_api_key="main-key",
        llm_model="main-model",
        visual_multimodal_enabled=True,
        visual_evidence_base_url="https://visual.example/v1",
        visual_evidence_api_key="visual-key",
        visual_evidence_model="visual-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"text\\":\\"BILISUM\\",\\"shape\\":\\"pink circle\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"text":"BILISUM","shape":"pink circle"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection(SettingsUpdatePayload(llm_test_scope="visual"))

    assert response["ok"] is True
    assert response["model"] == "visual-model"
    assert response["visualImageRecognitionAvailable"] is True
    assert calls[0]["url"] == "https://visual.example/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer visual-key"
    messages = calls[0]["json"]["messages"]
    content = messages[1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_llm_connection_normalizes_mimo_model_and_requests_json_mode(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test-key",
        llm_model="MiMo-V2.5-Pro",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"ok":true,"message":"test"}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection()

    assert response["ok"] is True
    assert response["model"] == "MiMo-V2.5-Pro"
    assert calls[0]["json"]["model"] == "mimo-v2.5-pro"
    assert calls[0]["json"]["max_tokens"] == 512


def test_llm_connection_accepts_choice_text_response(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    settings_manager._settings = current

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"text":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"text": '{"ok":true,"message":"test"}'}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection()

    assert response["ok"] is True
    assert response["jsonPreview"] == '{"ok": true, "message": "test"}'


def test_llm_connection_accepts_anthropic_messages_response(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_provider="anthropic",
        llm_base_url="https://api.anthropic.com/v1",
        llm_api_key="test-key",
        llm_model="claude-3-5-haiku-latest",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"content":[{"type":"text","text":"{\\"ok\\":true,\\"message\\":\\"test\\"}"}]}'

        def json(self) -> dict[str, object]:
            return {"content": [{"type": "text", "text": '{"ok":true,"message":"test"}'}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_llm_connection()

    assert response["ok"] is True
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "test-key"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["json"]["model"] == "claude-3-5-haiku-latest"
    assert calls[0]["json"]["system"]
    assert "response_format" not in calls[0]["json"]


def test_llm_connection_requires_base_url(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    settings_manager._settings = current

    try:
        probe_llm_connection()
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "请先填写 API Base URL。"
    else:
        raise AssertionError("expected HTTPException")


def test_llm_connection_rejects_invalid_json_response(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        llm_enabled=True,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    settings_manager._settings = current

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"not json"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "not json"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    try:
        probe_llm_connection()
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "未返回合法 JSON" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_asr_connection_uses_unsaved_payload(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_base_url="https://old.example/v1",
        siliconflow_asr_api_key="old-key",
        siliconflow_asr_model="old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"text":"test transcript"}'

        def json(self) -> dict[str, object]:
            return {"text": "test transcript"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], data: dict[str, object], files: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "data": data, "files": files})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_asr_connection(
        SettingsUpdatePayload(
            siliconflow_asr_base_url="https://api.example.com/v1",
            siliconflow_asr_api_key="new-key",
            siliconflow_asr_model="new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "new-model"
    assert response["responsePreview"] == "test transcript"
    assert calls[0]["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert calls[0]["headers"]["Authorization"] == "Bearer new-key"
    assert calls[0]["data"]["model"] == "new-model"
    file_name, audio_bytes, content_type = calls[0]["files"]["file"]
    assert file_name == "bilisum-asr-test-zh.wav"
    assert content_type == "audio/wav"
    assert len(audio_bytes) > 100_000


def test_asr_connection_uses_saved_api_key_when_payload_masks_key(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_base_url="https://old.example/v1",
        siliconflow_asr_api_key="saved-asr-key",
        siliconflow_asr_model="old-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"text":"test transcript"}'

        def json(self) -> dict[str, object]:
            return {"text": "test transcript"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], data: dict[str, object], files: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "data": data, "files": files})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_asr_connection(
        SettingsUpdatePayload(
            siliconflow_asr_base_url="https://api.example.com/v1",
            siliconflow_asr_api_key="******",
            siliconflow_asr_model="new-model",
        )
    )

    assert response["ok"] is True
    assert response["model"] == "new-model"
    assert response["responsePreview"] == "test transcript"
    assert calls[0]["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert calls[0]["headers"]["Authorization"] == "Bearer saved-asr-key"
    assert calls[0]["data"]["model"] == "new-model"


def test_asr_connection_uses_custom_test_audio_file(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_base_url="https://api.example.com/v1",
        siliconflow_asr_api_key="test-key",
        siliconflow_asr_model="TeleAI/TeleSpeechASR",
    )
    settings_manager._settings = current
    audio_path = tmp_path / "voice.mp3"
    audio_path.write_bytes(b"voice-bytes")
    monkeypatch.setenv("VIDEO_SUM_ASR_TEST_AUDIO_FILE", str(audio_path))
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"text":"你好"}'

        def json(self) -> dict[str, object]:
            return {"text": "你好"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], data: dict[str, object], files: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "data": data, "files": files})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_asr_connection()

    assert response["ok"] is True
    assert response["responsePreview"] == "你好"
    assert calls[0]["files"]["file"] == ("voice.mp3", b"voice-bytes", "audio/mpeg")


def test_multimodal_asr_connection_falls_back_to_reasoning_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        transcription_provider="multimodal",
        multimodal_asr_base_url="https://api.example.com/v1",
        multimodal_asr_api_key="test-key",
        multimodal_asr_model="test-model",
    )
    settings_manager._settings = current

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"reasoning_content":"reasoned transcript"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"reasoning_content": "reasoned transcript"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_asr_connection()

    assert response["ok"] is True
    assert response["responsePreview"] == "reasoned transcript"
    assert calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"]["model"] == "test-model"


def test_asr_connection_requires_api_key(tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_base_url="https://api.example.com/v1",
        siliconflow_asr_api_key="",
        siliconflow_asr_model="TeleAI/TeleSpeechASR",
    )
    settings_manager._settings = current

    try:
        probe_asr_connection()
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "请先填写 SiliconFlow API Key。"
    else:
        raise AssertionError("expected HTTPException")


def test_asr_connection_accepts_empty_transcript_when_endpoint_responds(monkeypatch, tmp_path: Path) -> None:
    current = ServiceSettings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
        runtime_channel="base",
        siliconflow_asr_base_url="https://api.example.com/v1",
        siliconflow_asr_api_key="test-key",
        siliconflow_asr_model="TeleAI/TeleSpeechASR",
    )
    settings_manager._settings = current

    class FakeResponse:
        status_code = 200
        text = '{"text":""}'

        def json(self) -> dict[str, object]:
            return {"text": ""}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, headers: dict[str, str], data: dict[str, object], files: dict[str, object]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(service_app.httpx, "Client", FakeClient)

    response = probe_asr_connection()

    assert response["ok"] is True
    assert "接口已响应，但测试音频未返回文本" in str(response["message"])
    assert response["responsePreview"] == ""


# ═══════════════════════════════════════════════
# probe_script.py packaging regression tests
# ═══════════════════════════════════════════════

_SRC_PROBE = Path(__file__).resolve().parents[2] / "apps" / "service" / "src" / "video_sum_service" / "probe_script.py"


def _read_probe_script_source() -> str:
    """Read probe_script.py from the source tree (not the installed package)."""
    if _SRC_PROBE.exists():
        return _SRC_PROBE.read_text(encoding="utf-8")
    # Fallback: installed package
    import video_sum_service.runtime_support as rs
    probe_path = Path(rs.__file__).parent / "probe_script.py"
    return probe_path.read_text(encoding="utf-8")


def test_probe_script_file_exists() -> None:
    """probe_script.py must exist alongside runtime_support.py for detect_environment()."""
    import video_sum_service.runtime_support as rs
    probe_path = Path(rs.__file__).parent / "probe_script.py"
    assert probe_path.exists(), f"probe_script.py missing at {probe_path}"


def test_probe_script_is_readable_and_valid_python() -> None:
    """probe_script.py must parse as valid Python."""
    import ast
    source = _read_probe_script_source()
    try:
        ast.parse(source)
    except SyntaxError as e:
        pytest.fail(f"probe_script.py has syntax error: {e}")


def test_probe_script_no_future_annotations() -> None:
    """Regression: __future__ import breaks python -c concatenation in tests/CI."""
    import ast
    source = _read_probe_script_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            names = [alias.name for alias in node.names]
            pytest.fail(f"probe_script.py has __future__ import: {names}")


def test_probe_script_contains_probe_and_main() -> None:
    """probe_script.py must define probe() and a __main__ entry point."""
    source = _read_probe_script_source()
    assert "def probe()" in source, "probe_script.py missing probe() function"
    assert 'if __name__ == "__main__"' in source, "probe_script.py missing __main__ guard"


def test_detect_environment_reads_probe_script(monkeypatch, tmp_path: Path) -> None:
    """detect_environment() successfully reads and executes probe_script.py."""
    import video_sum_service.runtime_support as rs

    current = ServiceSettings(cache_dir=tmp_path / "cache", runtime_channel="base")
    current.cache_dir.mkdir(parents=True)
    rs._environment_probe_cache.clear()
    rs._environment_probe_failures.clear()
    monkeypatch.setattr(rs.settings_manager, "_settings", current)
    monkeypatch.setattr(rs, "uses_current_service_python", lambda _: True)
    monkeypatch.setattr(sys, "executable", sys.executable)

    capture: list[list[str]] = []
    def fake_run_host_command(command, timeout=120):
        capture.append(command)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    monkeypatch.setattr(rs, "run_host_command", fake_run_host_command)

    environment = rs.detect_environment("base")
    assert capture, "detect_environment did not run any command"
    # The command should include the probe script content, not a FileNotFoundError
    assert environment.get("pythonVersion"), "probe returned no pythonVersion"
    assert environment.get("torchInstalled") is not None, "probe returned no torchInstalled"
