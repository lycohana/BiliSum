from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import video_sum_service.settings_manager as settings_manager_module
from fastapi import BackgroundTasks, HTTPException
from video_sum_infra.config import ServiceSettings
from video_sum_service.routers import system as system_router
from video_sum_service.settings_manager import SettingsUpdatePayload


def _make_app_state() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def _record_call(calls: dict[str, list[str]], key: str, channel: str) -> str:
    calls.setdefault(key, []).append(channel)
    return channel


def _monkeypatch_runtime_helpers(monkeypatch, calls: dict[str, list[str]]) -> None:
    monkeypatch.setattr(
        system_router,
        "ensure_runtime_channel",
        lambda channel: _record_call(calls, "ensure", channel) or Path(channel),
    )
    monkeypatch.setattr(
        system_router,
        "bootstrap_managed_runtime",
        lambda channel: _record_call(calls, "bootstrap", channel),
    )
    monkeypatch.setattr(
        system_router,
        "prepend_runtime_path",
        lambda channel: _record_call(calls, "prepend", channel),
    )
    monkeypatch.setattr(
        system_router,
        "activate_runtime_pythonpath",
        lambda channel: _record_call(calls, "activate", channel),
    )
    monkeypatch.setattr(
        system_router,
        "clear_environment_probe_cache",
        lambda channel=None: calls.setdefault("clear", []).append(channel or ""),
    )
    monkeypatch.setattr(
        system_router,
        "_clear_knowledge_service_cache",
        lambda app_state: calls.setdefault("knowledge", []).append("called"),
    )
    monkeypatch.setattr(
        system_router,
        "_load_cached_environment_snapshot",
        lambda channel: {"cudaAvailable": False, "runtimeChannel": channel},
    )


def test_router_settings_unchanged_channel_skips_runtime_sync(monkeypatch, tmp_path: Path) -> None:
    """A plain settings save (e.g. toggling the summary prompt mode) must not
    trigger the expensive runtime ensure/bootstrap/sync path — that is what
    blocked the response and surfaced as Internal Server Error."""
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
        prompt_router_mode="auto",
    )
    monkeypatch.setattr(system_router.settings_manager, "_settings", previous)
    monkeypatch.setattr(system_router.settings_manager, "save", lambda payload: current)

    calls: dict[str, list[str]] = {}
    _monkeypatch_runtime_helpers(monkeypatch, calls)

    response = system_router.update_settings(
        SettingsUpdatePayload(prompt_router_mode="auto"),
        _make_app_state(),
        BackgroundTasks(),
    )

    assert response["saved"] is True
    assert response["settings"]["prompt_router_mode"] == "auto"
    assert calls == {}


def test_router_settings_changed_channel_ensures_runtime(monkeypatch, tmp_path: Path) -> None:
    """Switching the runtime channel still runs ensure_runtime_channel and the
    channel-scoped cleanup, exactly once."""
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
    monkeypatch.setattr(system_router.settings_manager, "_settings", previous)
    monkeypatch.setattr(system_router.settings_manager, "save", lambda payload: current)

    calls: dict[str, list[str]] = {}
    _monkeypatch_runtime_helpers(monkeypatch, calls)

    response = system_router.update_settings(
        SettingsUpdatePayload(runtime_channel="gpu-cu128"),
        _make_app_state(),
        BackgroundTasks(),
    )

    assert response["saved"] is True
    assert calls["ensure"] == ["gpu-cu128"]
    assert calls["bootstrap"] == ["gpu-cu128"]
    assert calls["prepend"] == ["gpu-cu128"]
    assert calls["activate"] == ["gpu-cu128"]
    assert set(calls["clear"]) == {"base", "gpu-cu128"}
    assert calls["knowledge"] == ["called"]


def test_router_settings_runtime_ensure_failure_reports_error(monkeypatch, tmp_path: Path) -> None:
    """A failed runtime sync surfaces as a readable HTTP error, not a bare 500."""
    previous = ServiceSettings(
        data_dir=tmp_path / "data-prev",
        cache_dir=tmp_path / "cache-prev",
        tasks_dir=tmp_path / "tasks-prev",
        runtime_channel="base",
    )
    monkeypatch.setattr(system_router.settings_manager, "_settings", previous)
    save_calls: list[SettingsUpdatePayload] = []
    monkeypatch.setattr(
        system_router.settings_manager,
        "save",
        lambda payload: save_calls.append(payload) or previous,
    )
    monkeypatch.setattr(
        system_router,
        "ensure_runtime_channel",
        lambda channel: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as exc_info:
        system_router.update_settings(
            SettingsUpdatePayload(runtime_channel="gpu-cu128"),
            _make_app_state(),
            BackgroundTasks(),
        )

    assert exc_info.value.status_code == 500
    assert "运行环境切换失败" in str(exc_info.value.detail)
    assert save_calls == []


def test_settings_manager_save_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    base = ServiceSettings(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        tasks_dir=tmp_path / "tasks",
    )
    fresh = settings_manager_module.SettingsManager(base)
    saved = fresh.save(SettingsUpdatePayload(prompt_router_mode="auto"))
    assert saved.prompt_router_mode == "auto"
    assert (tmp_path / "settings.json").exists()
    assert not list(tmp_path.glob("settings.json.tmp"))
    # Second save keeps the file valid and still replaces atomically.
    fresh.save(SettingsUpdatePayload(prompt_router_mode="confirm"))
    assert fresh.current.prompt_router_mode == "confirm"
    assert not list(tmp_path.glob("settings.json.tmp"))

    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["prompt_router_mode"] == "confirm"
