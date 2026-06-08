from __future__ import annotations

import subprocess
from pathlib import Path

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
        visual_evidence_api_key="visual-secret",
    )

    payload = serialize_settings(current, environment_info={"cudaAvailable": False, "runtimeChannel": "base"})

    assert payload["siliconflow_asr_api_key"] == ""
    assert payload["llm_api_key"] == ""
    assert payload["knowledge_llm_api_key"] == ""
    assert payload["visual_evidence_api_key"] == ""
    assert payload["siliconflow_asr_api_key_configured"] is True
    assert payload["llm_api_key_configured"] is True
    assert payload["knowledge_llm_api_key_configured"] is True
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
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="",
            llm_api_key="",
            knowledge_llm_api_key="",
            llm_model="new-model",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "saved-asr-key"
    assert next_settings.llm_api_key == "saved-llm-key"
    assert next_settings.knowledge_llm_api_key == "saved-knowledge-key"
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
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="******",
            llm_api_key="******",
            knowledge_llm_api_key="******",
            llm_model="new-model",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "saved-asr-key"
    assert next_settings.llm_api_key == "saved-llm-key"
    assert next_settings.knowledge_llm_api_key == "saved-knowledge-key"
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
    )
    settings_manager._settings = current
    settings_manager._settings_path = tmp_path / "settings.json"

    next_settings = settings_manager.save(
        SettingsUpdatePayload(
            siliconflow_asr_api_key="new-asr-key",
            llm_api_key="new-llm-key",
            knowledge_llm_api_key="new-knowledge-key",
        )
    )

    assert next_settings.siliconflow_asr_api_key == "new-asr-key"
    assert next_settings.llm_api_key == "new-llm-key"
    assert next_settings.knowledge_llm_api_key == "new-knowledge-key"


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

    monkeypatch.setattr("video_sum_service.app._uses_current_service_python", lambda runtime_channel: False)
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
            "chromadbInstalled": True,
            "chromadbVersion": "1.2.3",
            "sentenceTransformersInstalled": True,
            "sentenceTransformersVersion": "3.4.5",
            "knowledgeDependenciesReady": True,
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
    assert (gpu_bin / "python").read_text(encoding="utf-8") == "gpu-python"
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


def test_inspect_runtime_channels_reports_outdated_runtime(monkeypatch, tmp_path: Path) -> None:
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
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert calls[0]["json"]["enable_thinking"] is False
    assert calls[0]["json"]["chat_template_kwargs"] == {"enable_thinking": False}


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
    assert file_name == "vidmind-asr-test-zh.wav"
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
