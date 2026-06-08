from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import sys


@dataclass(slots=True)
class AppInfo:
    name: str
    version: str

    @classmethod
    def load(cls) -> "AppInfo":
        return cls(name="VidMind", version=_resolve_version())


def _resolve_version() -> str:
    version_file = Path(__file__).resolve().parents[4] / "VERSION"

    # In local development, prefer the workspace VERSION file so the UI reflects
    # the checked-out repo version immediately without reinstalling packages.
    if version_file.exists() and not _is_running_from_installed_dist():
        return version_file.read_text(encoding="utf-8").strip()

    for distribution_name in ("video-sum-service", "video-sum-infra"):
        try:
            return metadata.version(distribution_name)
        except metadata.PackageNotFoundError:
            continue

    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _is_running_from_installed_dist() -> bool:
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    current_file = Path(__file__).resolve()
    return base_prefix in current_file.parents
