import time as _time

import pytest
from video_sum_service import cli_idle


def test_idle_disabled_when_not_cli_managed(monkeypatch) -> None:
    monkeypatch.delenv(cli_idle.CLI_MANAGED_ENV, raising=False)
    monkeypatch.delenv(cli_idle.IDLE_TIMEOUT_ENV, raising=False)

    assert cli_idle.enabled() is False
    assert cli_idle.idle_timeout_seconds() == 0


def test_idle_timeout_reads_env(monkeypatch) -> None:
    monkeypatch.setenv(cli_idle.CLI_MANAGED_ENV, "1")
    monkeypatch.setenv(cli_idle.IDLE_TIMEOUT_ENV, "120")

    assert cli_idle.enabled() is True
    assert cli_idle.idle_timeout_seconds() == 120


def test_idle_timeout_defaults_to_600(monkeypatch) -> None:
    monkeypatch.setenv(cli_idle.CLI_MANAGED_ENV, "1")
    monkeypatch.delenv(cli_idle.IDLE_TIMEOUT_ENV, raising=False)

    assert cli_idle.idle_timeout_seconds() == cli_idle.DEFAULT_IDLE_TIMEOUT_SECONDS


def test_idle_timeout_clamps_to_minimum(monkeypatch) -> None:
    monkeypatch.setenv(cli_idle.CLI_MANAGED_ENV, "1")
    monkeypatch.setenv(cli_idle.IDLE_TIMEOUT_ENV, "5")

    assert cli_idle.idle_timeout_seconds() == cli_idle.MIN_IDLE_TIMEOUT_SECONDS


def test_idle_timeout_tolerates_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv(cli_idle.CLI_MANAGED_ENV, "1")
    monkeypatch.setenv(cli_idle.IDLE_TIMEOUT_ENV, "not-a-number")

    assert cli_idle.idle_timeout_seconds() == cli_idle.DEFAULT_IDLE_TIMEOUT_SECONDS


def test_touch_resets_activity_window() -> None:
    cli_idle.touch()

    assert cli_idle.seconds_since_activity() < 5


def test_start_watchdog_returns_none_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv(cli_idle.CLI_MANAGED_ENV, raising=False)

    assert cli_idle.start_watchdog(repository=None) is None


def test_watchdog_exits_when_idle_and_queue_empty(monkeypatch) -> None:
    monkeypatch.setattr(cli_idle.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli_idle, "_last_activity", _time.monotonic() - 100)
    exited: list[int] = []

    def fake_exit(code: int) -> None:
        exited.append(code)
        raise RuntimeError("exit-called")

    monkeypatch.setattr(cli_idle.os, "_exit", fake_exit)

    class EmptyRepo:
        def list_recoverable_tasks(self):
            return []

    with pytest.raises(RuntimeError, match="exit-called"):
        cli_idle._watchdog_loop(30, EmptyRepo())

    assert exited == [0]


def test_watchdog_stays_alive_while_tasks_active(monkeypatch) -> None:
    sleeps = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] >= 3:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr(cli_idle.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli_idle, "_last_activity", _time.monotonic() - 100)
    exited: list[int] = []
    monkeypatch.setattr(cli_idle.os, "_exit", lambda code: exited.append(code))

    class ActiveRepo:
        def list_recoverable_tasks(self):
            return [object()]  # queued/running task keeps the service alive

    with pytest.raises(RuntimeError, match="stop-loop"):
        cli_idle._watchdog_loop(30, ActiveRepo())

    assert exited == []
    # The active-task branch re-arms the idle window.
    assert cli_idle.seconds_since_activity() < 10
