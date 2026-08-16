from pathlib import Path
import sys

import pytest

import video_sum_infra.runtime as runtime_module
from video_sum_infra.config import ServiceSettings
from video_sum_infra.runtime import (
    activate_runtime_pythonpath,
    activate_runtime_dll_directories,
    app_data_root,
    bootstrap_managed_runtime,
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
    current_root = tmp_path / "bilisum"
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


def test_bootstrap_base_runtime_refresh_preserves_user_installed_packages(monkeypatch, tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    seed_dir = app_root / "runtime" / "base"
    runtime_dir = tmp_path / "data" / "runtime" / "base"
    seed_site_packages = seed_dir / "Lib" / "site-packages"
    runtime_site_packages = runtime_dir / "Lib" / "site-packages"
    seed_site_packages.mkdir(parents=True)
    runtime_site_packages.mkdir(parents=True)
    (seed_dir / "python.exe").write_text("seed-python", encoding="utf-8")
    (runtime_dir / "python.exe").write_text("old-python", encoding="utf-8")
    (seed_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.17.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (runtime_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.15.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (seed_site_packages / "video_sum_service").mkdir()
    (seed_site_packages / "video_sum_service" / "__init__.py").write_text("new", encoding="utf-8")
    (runtime_site_packages / "video_sum_service").mkdir()
    (runtime_site_packages / "video_sum_service" / "__init__.py").write_text("old", encoding="utf-8")
    (runtime_site_packages / "chromadb").mkdir()
    (runtime_site_packages / "chromadb" / "__init__.py").write_text("user chroma", encoding="utf-8")
    (runtime_site_packages / "chromadb-1.0.0.dist-info").mkdir()
    (runtime_site_packages / "sentence_transformers").mkdir()
    (runtime_site_packages / "sentence_transformers" / "__init__.py").write_text("user st", encoding="utf-8")
    (runtime_site_packages / "sentence_transformers-3.0.0.dist-info").mkdir()

    monkeypatch.setattr(runtime_module, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_module, "bundled_runtime_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(runtime_module, "runtime_seed_available", lambda: True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda runtime_channel: runtime_dir)
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda runtime_channel: runtime_dir / "python.exe" if (runtime_dir / "python.exe").exists() else None,
    )

    assert bootstrap_managed_runtime("base") == runtime_dir

    assert (runtime_dir / "python.exe").read_text(encoding="utf-8") == "seed-python"
    assert (runtime_site_packages / "video_sum_service" / "__init__.py").read_text(encoding="utf-8") == "new"
    assert (runtime_site_packages / "chromadb" / "__init__.py").read_text(encoding="utf-8") == "user chroma"
    assert (runtime_site_packages / "chromadb-1.0.0.dist-info").exists()
    assert (runtime_site_packages / "sentence_transformers" / "__init__.py").read_text(encoding="utf-8") == "user st"


def test_bootstrap_base_runtime_refresh_preserves_pip_tooling(monkeypatch, tmp_path: Path) -> None:
    seed_dir = tmp_path / "app" / "runtime" / "base"
    runtime_dir = tmp_path / "data" / "runtime" / "base"
    seed_site_packages = seed_dir / "Lib" / "site-packages"
    runtime_site_packages = runtime_dir / "Lib" / "site-packages"
    seed_site_packages.mkdir(parents=True)
    runtime_site_packages.mkdir(parents=True)
    (seed_dir / "python.exe").write_text("seed-python", encoding="utf-8")
    (runtime_dir / "python.exe").write_text("old-python", encoding="utf-8")
    (seed_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.17.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (runtime_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.15.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (runtime_site_packages / "pip").mkdir()
    (runtime_site_packages / "pip-25.0.1.dist-info").mkdir()
    (runtime_site_packages / "setuptools-75.8.0.dist-info").mkdir()
    (runtime_site_packages / "wheel-0.45.1.dist-info").mkdir()

    monkeypatch.setattr(runtime_module, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_module, "bundled_runtime_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(runtime_module, "runtime_seed_available", lambda: True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda runtime_channel: runtime_dir)
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda runtime_channel: runtime_dir / "python.exe" if (runtime_dir / "python.exe").exists() else None,
    )

    assert bootstrap_managed_runtime("base") == runtime_dir

    assert (runtime_site_packages / "pip").exists()
    assert (runtime_site_packages / "pip-25.0.1.dist-info").exists()
    assert (runtime_site_packages / "setuptools-75.8.0.dist-info").exists()
    assert (runtime_site_packages / "wheel-0.45.1.dist-info").exists()


def test_bootstrap_base_runtime_refreshes_when_seed_python_version_changes(monkeypatch, tmp_path: Path) -> None:
    seed_dir = tmp_path / "app" / "runtime" / "base"
    runtime_dir = tmp_path / "data" / "runtime" / "base"
    (seed_dir / "Lib" / "site-packages").mkdir(parents=True)
    (runtime_dir / "Lib" / "site-packages").mkdir(parents=True)
    (seed_dir / "python.exe").write_text("seed-python", encoding="utf-8")
    (runtime_dir / "python.exe").write_text("old-python", encoding="utf-8")
    (seed_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.17.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.8"}',
        encoding="utf-8",
    )
    (runtime_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.17.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_module, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_module, "bundled_runtime_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(runtime_module, "runtime_seed_available", lambda: True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda runtime_channel: runtime_dir)
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda runtime_channel: runtime_dir / "python.exe" if (runtime_dir / "python.exe").exists() else None,
    )

    assert bootstrap_managed_runtime("base") == runtime_dir

    assert (runtime_dir / "python.exe").read_text(encoding="utf-8") == "seed-python"


def test_bootstrap_base_runtime_refresh_restores_previous_runtime_when_copy_fails(monkeypatch, tmp_path: Path) -> None:
    seed_dir = tmp_path / "app" / "runtime" / "base"
    runtime_dir = tmp_path / "data" / "runtime" / "base"
    runtime_site_packages = runtime_dir / "Lib" / "site-packages"
    (seed_dir / "Lib" / "site-packages").mkdir(parents=True)
    runtime_site_packages.mkdir(parents=True)
    (seed_dir / "python.exe").write_text("seed-python", encoding="utf-8")
    (runtime_dir / "python.exe").write_text("old-python", encoding="utf-8")
    (seed_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.17.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (runtime_dir / "video_sum_runtime.json").write_text(
        '{"appVersion":"1.15.0","runtimeLayout":"portable-cpython","pythonVersion":"3.12.0"}',
        encoding="utf-8",
    )
    (runtime_site_packages / "chromadb").mkdir()
    (runtime_site_packages / "chromadb" / "__init__.py").write_text("user chroma", encoding="utf-8")

    monkeypatch.setattr(runtime_module, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_module, "bundled_runtime_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(runtime_module, "runtime_seed_available", lambda: True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda runtime_channel: runtime_dir)
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda runtime_channel: runtime_dir / "python.exe" if (runtime_dir / "python.exe").exists() else None,
    )

    original_copytree = runtime_module.shutil.copytree

    def failing_copytree(source, destination, *args, **kwargs):
        if Path(source) == seed_dir and Path(destination) == runtime_dir:
            raise OSError("copy failed")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtime_module.shutil, "copytree", failing_copytree)

    with pytest.raises(OSError, match="copy failed"):
        bootstrap_managed_runtime("base")

    assert (runtime_dir / "python.exe").read_text(encoding="utf-8") == "old-python"
    assert (runtime_site_packages / "chromadb" / "__init__.py").read_text(encoding="utf-8") == "user chroma"
    assert not (runtime_dir.parent / ".base-refresh-backup").exists()


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


def _portable_layout_dir(root: Path, *, with_socket: bool) -> Path:
    runtime_dir = root / "runtime" / "base"
    (runtime_dir / "DLLs").mkdir(parents=True)
    (runtime_dir / "Lib" / "site-packages").mkdir(parents=True)
    if with_socket:
        (runtime_dir / "DLLs" / "_socket.pyd").write_text("x", encoding="utf-8")
    (runtime_dir / "python312._pth").write_text(
        "DLLs\nstdlib\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )
    return runtime_dir


def _patch_python_executable(monkeypatch, runtime_dir: Path) -> None:
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda channel: runtime_dir / "python.exe",
    )


def test_runtime_python_stdlib_healthy_detects_missing_dlls(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = _portable_layout_dir(tmp_path, with_socket=False)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda channel: runtime_dir)
    _patch_python_executable(monkeypatch, runtime_dir)

    assert runtime_module.runtime_python_stdlib_healthy("base") is False


def test_runtime_python_stdlib_healthy_accepts_populated_dlls(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = _portable_layout_dir(tmp_path, with_socket=True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda channel: runtime_dir)
    _patch_python_executable(monkeypatch, runtime_dir)

    assert runtime_module.runtime_python_stdlib_healthy("base") is True


def test_runtime_stdlib_healthy_non_portable_layout(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "base"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda channel: runtime_dir)
    _patch_python_executable(monkeypatch, runtime_dir)

    assert runtime_module.runtime_python_stdlib_healthy("base") is True


def test_runtime_python_stdlib_healthy_false_without_python(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "base"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda channel: runtime_dir)
    monkeypatch.setattr(runtime_module, "runtime_python_executable", lambda channel: None)

    assert runtime_module.runtime_python_stdlib_healthy("base") is False


def test_bootstrap_base_runtime_refreshes_broken_stdlib(monkeypatch, tmp_path: Path) -> None:
    """A runtime whose DLLs went missing (python.exe + metadata still present)
    must be treated as not-ready and refreshed from the healthy seed."""
    seed_dir = tmp_path / "app" / "runtime" / "base"
    runtime_dir = tmp_path / "data" / "runtime" / "base"
    (seed_dir / "DLLs").mkdir(parents=True)
    (seed_dir / "DLLs" / "_socket.pyd").write_text("seed-socket", encoding="utf-8")
    (seed_dir / "Lib" / "site-packages").mkdir(parents=True)
    (runtime_dir / "DLLs").mkdir(parents=True)  # broken: empty DLLs
    (runtime_dir / "Lib" / "site-packages").mkdir(parents=True)
    (seed_dir / "python.exe").write_text("seed-python", encoding="utf-8")
    (runtime_dir / "python.exe").write_text("old-python", encoding="utf-8")
    metadata = (
        '{"appVersion":"1.20.0","runtimeLayout":"portable-cpython",'
        '"pythonVersion":"3.12.10"}'
    )
    (seed_dir / "video_sum_runtime.json").write_text(metadata, encoding="utf-8")
    (runtime_dir / "video_sum_runtime.json").write_text(metadata, encoding="utf-8")
    (runtime_dir / "python312._pth").write_text(
        "DLLs\nstdlib\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime_module, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_module, "bundled_runtime_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(runtime_module, "runtime_seed_available", lambda: True)
    monkeypatch.setattr(runtime_module, "managed_runtime_dir", lambda runtime_channel: runtime_dir)
    monkeypatch.setattr(
        runtime_module,
        "runtime_python_executable",
        lambda runtime_channel: runtime_dir / "python.exe"
        if (runtime_dir / "python.exe").exists()
        else None,
    )

    assert bootstrap_managed_runtime("base") == runtime_dir

    # Broken stdlib must have triggered a refresh from the healthy seed.
    assert (runtime_dir / "python.exe").read_text(encoding="utf-8") == "seed-python"
    assert (runtime_dir / "DLLs" / "_socket.pyd").read_text(encoding="utf-8") == "seed-socket"
