from pathlib import Path
import sys

import video_sum_infra.runtime as runtime_module
from video_sum_infra.config import ServiceSettings
from video_sum_infra.runtime import (
    activate_runtime_pythonpath,
    activate_runtime_dll_directories,
    app_data_root,
    default_host,
    read_runtime_metadata,
    web_static_dir,
    write_runtime_metadata,
)


def test_service_settings_use_container_friendly_defaults_in_docker(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_SUM_DOCKER", "1")
    monkeypatch.delenv("VIDEO_SUM_APP_DATA_ROOT", raising=False)

    settings = ServiceSettings()

    assert default_host() == "0.0.0.0"
    assert app_data_root() == Path("/data")
    assert settings.host == "0.0.0.0"
    assert settings.data_dir == Path("/data")
    assert settings.cache_dir == Path("/data/cache")
    assert settings.tasks_dir == Path("/data/tasks")
    assert settings.database_url == "sqlite:////data/video_sum.db"


def test_web_static_dir_prefers_explicit_override(monkeypatch, tmp_path: Path) -> None:
    static_dir = tmp_path / "web-static"
    monkeypatch.setenv("VIDEO_SUM_WEB_STATIC_DIR", str(static_dir))

    assert web_static_dir() == static_dir.resolve()


def test_app_data_root_migrates_legacy_briefvid_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("VIDEO_SUM_APP_DATA_ROOT", raising=False)
    monkeypatch.delenv("VIDEO_SUM_DOCKER", raising=False)
    monkeypatch.setattr(runtime_module, "is_running_in_docker", lambda: False)
    monkeypatch.setattr(runtime_module, "_LEGACY_APP_DATA_MIGRATION_DONE", False)

    legacy_root = tmp_path / "briefvid"
    current_root = tmp_path / "vidmind"
    (legacy_root / "data" / "tasks" / "task-1").mkdir(parents=True)
    (legacy_root / "runtime" / "gpu-cu128").mkdir(parents=True)
    (legacy_root / "data" / "video_sum.db").write_text("legacy-db", encoding="utf-8")
    (legacy_root / "data" / "tasks" / "task-1" / "summary.json").write_text("legacy-task", encoding="utf-8")
    (legacy_root / "runtime" / "gpu-cu128" / "python.exe").write_text("legacy-python", encoding="utf-8")
    (current_root / "data").mkdir(parents=True)
    (current_root / "data" / "video_sum.db").write_text("current-db", encoding="utf-8")

    assert app_data_root() == current_root
    assert (current_root / "data" / "video_sum.db").read_text(encoding="utf-8") == "current-db"
    assert (current_root / "data" / "tasks" / "task-1" / "summary.json").read_text(encoding="utf-8") == "legacy-task"
    assert (current_root / "runtime" / "gpu-cu128" / "python.exe").read_text(encoding="utf-8") == "legacy-python"


def test_write_runtime_metadata_merges_existing_payload(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_dir = runtime_root / "gpu-cu128"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setenv("VIDEO_SUM_APP_DATA_ROOT", str(tmp_path))

    write_runtime_metadata(
        "gpu-cu128",
        {
            "runtimeChannel": "gpu-cu128",
            "runtimeLayout": "portable-cpython",
            "appVersion": "1.0.0",
            "cudaVariant": "cu128",
        },
    )
    write_runtime_metadata(
        "gpu-cu128",
        {
            "localAsrInstalled": True,
            "localAsrVersion": "1.1.1",
        },
    )

    metadata = read_runtime_metadata(runtime_dir)

    assert metadata["runtimeLayout"] == "portable-cpython"
    assert metadata["appVersion"] == "1.0.0"
    assert metadata["cudaVariant"] == "cu128"
    assert metadata["localAsrInstalled"] is True


def test_activate_runtime_pythonpath_replaces_managed_site_packages(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIDEO_SUM_APP_DATA_ROOT", str(tmp_path))
    old_runtime_dir = tmp_path / "runtime" / "gpu-cu124"
    new_runtime_dir = tmp_path / "runtime" / "gpu-cu128"
    old_stdlib = old_runtime_dir / "stdlib"
    old_dlls = old_runtime_dir / "DLLs"
    old_site_packages = old_runtime_dir / "Lib" / "site-packages"
    new_stdlib = new_runtime_dir / "stdlib"
    new_dlls = new_runtime_dir / "DLLs"
    new_site_packages = new_runtime_dir / "Lib" / "site-packages"
    for directory in (old_stdlib, old_dlls, old_site_packages, new_stdlib, new_dlls, new_site_packages):
        directory.mkdir(parents=True)
    original_sys_path = list(sys.path)
    sys.path[:] = ["app-bundle", str(old_stdlib), str(old_dlls), str(old_site_packages), str(old_runtime_dir), "tail"]

    try:
        activate_runtime_pythonpath("gpu-cu128")

        assert sys.path == [
            "app-bundle",
            "tail",
            str(new_stdlib),
            str(new_dlls),
            str(new_site_packages),
            str(new_runtime_dir),
        ]
    finally:
        sys.path[:] = original_sys_path


def test_activate_runtime_dll_directories_replaces_managed_handles(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIDEO_SUM_APP_DATA_ROOT", str(tmp_path))
    old_runtime_dir = tmp_path / "runtime" / "gpu-cu124"
    new_runtime_dir = tmp_path / "runtime" / "gpu-cu128"
    for runtime_dir in (old_runtime_dir, new_runtime_dir):
        (runtime_dir / "DLLs").mkdir(parents=True)
        (runtime_dir / "Scripts").mkdir(parents=True)
        (runtime_dir / "python.exe").write_text("", encoding="utf-8")

    class FakeDllHandle:
        def __init__(self, path: str) -> None:
            self.path = path
            self.closed = False

        def close(self) -> None:
            self.closed = True

    handles: list[FakeDllHandle] = []

    def fake_add_dll_directory(path: str) -> FakeDllHandle:
        handle = FakeDllHandle(path)
        handles.append(handle)
        return handle

    original_handles = dict(runtime_module._DLL_DIRECTORY_HANDLES)
    runtime_module._DLL_DIRECTORY_HANDLES.clear()
    monkeypatch.setattr(runtime_module.os, "add_dll_directory", fake_add_dll_directory, raising=False)
    try:
        activate_runtime_dll_directories("gpu-cu124")
        old_handles = list(runtime_module._DLL_DIRECTORY_HANDLES.values())

        activate_runtime_dll_directories("gpu-cu128")

        assert old_handles
        assert all(getattr(handle, "closed", False) for handle in old_handles)
        active_keys = set(runtime_module._DLL_DIRECTORY_HANDLES)
        assert str(new_runtime_dir.resolve()).lower() in active_keys
        assert str((new_runtime_dir / "DLLs").resolve()).lower() in active_keys
        assert str((new_runtime_dir / "Scripts").resolve()).lower() in active_keys
    finally:
        runtime_module._DLL_DIRECTORY_HANDLES.clear()
        runtime_module._DLL_DIRECTORY_HANDLES.update(original_handles)
