import logging

from uvicorn import run as uvicorn_run

from video_sum_infra.logging import configure_logging
from video_sum_infra.paths import ensure_directory
from video_sum_service.app import app, settings_manager

logger = logging.getLogger("video_sum_service.main")


def run() -> None:
    settings = settings_manager.current
    configure_logging()
    logger.info(
        "starting service host=%s port=%s data_dir=%s cache_dir=%s tasks_dir=%s runtime_channel=%s",
        settings.host,
        settings.port,
        settings.data_dir,
        settings.cache_dir,
        settings.tasks_dir,
        settings.runtime_channel,
    )
    ensure_directory(settings.data_dir)
    ensure_directory(settings.cache_dir)
    ensure_directory(settings.tasks_dir)
    # Windowed PyInstaller executables have no stdout/stderr. Uvicorn's
    # default color formatter calls stdout.isatty(), which crashes before the
    # service starts. Keep the file logging already configured above.
    uvicorn_run(app, host=settings.host, port=settings.port, access_log=False, log_config=None)
