"""CLI-managed idle watchdog.

Only active when the service was spawned by the BiliSum CLI in background mode
(VIDEO_SUM_CLI_MANAGED=1). The desktop app never sets this env var, so this
watchdog can never shut down a desktop-started service.

Shutdown condition: no HTTP activity AND no active (queued/running) tasks for
`timeout` seconds. Checking the task queue protects long-running jobs started
with `bilisum --no-wait`: the service stays alive until the queue drains, then
exits after the idle timeout. All task data is persisted to SQLite/disk, so a
clean exit loses nothing.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("video_sum_service.cli_idle")

CLI_MANAGED_ENV = "VIDEO_SUM_CLI_MANAGED"
IDLE_TIMEOUT_ENV = "VIDEO_SUM_CLI_IDLE_TIMEOUT_SECONDS"
DEFAULT_IDLE_TIMEOUT_SECONDS = 600
MIN_IDLE_TIMEOUT_SECONDS = 30
WATCHDOG_INTERVAL_SECONDS = 10

_activity_lock = threading.Lock()
_last_activity = time.monotonic()


def idle_timeout_seconds() -> int:
    """Idle timeout for a CLI-managed service, 0 when not CLI-managed."""
    if os.environ.get(CLI_MANAGED_ENV) != "1":
        return 0
    raw = os.environ.get(IDLE_TIMEOUT_ENV, str(DEFAULT_IDLE_TIMEOUT_SECONDS))
    try:
        parsed = int(raw)
    except ValueError:
        parsed = DEFAULT_IDLE_TIMEOUT_SECONDS
    return max(MIN_IDLE_TIMEOUT_SECONDS, parsed)


def enabled() -> bool:
    return idle_timeout_seconds() > 0


def touch() -> None:
    global _last_activity
    with _activity_lock:
        _last_activity = time.monotonic()


def seconds_since_activity() -> float:
    with _activity_lock:
        return time.monotonic() - _last_activity


def _watchdog_loop(timeout_seconds: int, repository) -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        if seconds_since_activity() < timeout_seconds:
            continue
        try:
            active = repository is not None and len(repository.list_recoverable_tasks()) > 0
        except Exception:
            logger.exception("cli idle watchdog failed to check task queue")
            active = True  # Be conservative: do not shut down on a check failure.
        if active:
            touch()  # Give the running job more time; re-arm the idle window.
            continue
        logger.info(
            "CLI-managed service idle for %ss without active tasks, shutting down",
            timeout_seconds,
        )
        os._exit(0)


def start_watchdog(repository) -> threading.Thread | None:
    timeout = idle_timeout_seconds()
    if timeout <= 0:
        return None
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(timeout, repository),
        name="cli-idle-watchdog",
        daemon=True,
    )
    thread.start()
    logger.info("CLI idle watchdog enabled timeout=%ss", timeout)
    return thread
