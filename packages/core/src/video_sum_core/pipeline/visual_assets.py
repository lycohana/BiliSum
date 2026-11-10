"""Reusable visual source assets for visual evidence generation.

``visual_evidence/assets.json`` records the video downloaded for screenshots and the
frames already extracted from it (timestamp → file), so re-running visual evidence does
not download the video again and only extracts timestamps that were not extracted before.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "assets.json"
FRAMES_DIR_NAME = "frames"

# Derived artifacts removed by a forced rebuild. The source video and its manifest entry stay.
DERIVED_FILE_NAMES = (
    "frame_index.json",
    "visual_context.json",
    "visual_keyframe_plan.json",
    "visual_insert_plan.json",
    "visual_note.md",
    "visual_enhanced_note.md",
)

_SCHEMA_VERSION = 1


def frame_file_stem(timestamp: float) -> str:
    """Frame files are named by timestamp so frames survive keyframe plan changes."""
    return f"t{int(round(max(0.0, float(timestamp)) * 1000)):09d}"


def _timestamp_key(timestamp: float) -> str:
    return f"{max(0.0, float(timestamp)):.3f}"


def _source_identity(source_path: Path) -> dict[str, object]:
    try:
        stat = source_path.stat()
    except OSError:
        return {"path": str(source_path)}
    return {"path": str(source_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


@dataclass(frozen=True)
class CachedFrame:
    file_name: str
    analysis_file_name: str
    sha256: str


class VisualAssetManifest:
    def __init__(self, visual_dir: Path) -> None:
        self._visual_dir = visual_dir
        self._path = visual_dir / MANIFEST_NAME
        self._data = self._load()

    def _load(self) -> dict[str, object]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            return {"schema_version": _SCHEMA_VERSION, "videos": [], "frames": None}
        if not isinstance(payload.get("videos"), list):
            payload["videos"] = []
        if not isinstance(payload.get("frames"), dict):
            payload["frames"] = None
        return payload

    def save(self) -> None:
        self._visual_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".assets.", suffix=".tmp", dir=str(self._visual_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, self._path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    # Videos -----------------------------------------------------------------

    def find_video(self, source: str, height: int) -> Path | None:
        for entry in self._data["videos"]:
            if not isinstance(entry, dict):
                continue
            if entry.get("source") != source or entry.get("height") != height:
                continue
            name = Path(str(entry.get("file_name") or "")).name
            candidate = self._visual_dir / name
            if name and candidate.is_file():
                return candidate
        return None

    def record_video(self, source: str, height: int, video_path: Path) -> None:
        videos = [
            entry
            for entry in self._data["videos"]
            if isinstance(entry, dict)
            and not (entry.get("source") == source and entry.get("height") == height)
        ]
        videos.append({"source": source, "height": height, "file_name": video_path.name})
        self._data["videos"] = videos

    # Frames -----------------------------------------------------------------

    def frame_cache(self, source_path: Path, settings: dict[str, object]) -> dict[str, CachedFrame]:
        """Return cached frames for this source and extraction settings.

        When the source or settings differ (or no frame cache exists yet) every
        image under ``frames/`` is stale, so the directory is emptied.
        """
        frames = self._data.get("frames")
        identity = _source_identity(source_path)
        if (
            not isinstance(frames, dict)
            or frames.get("source") != identity
            or frames.get("settings") != settings
        ):
            clear_frames_dir(self._visual_dir)
            self._data["frames"] = {"source": identity, "settings": settings, "items": {}}
            return {}
        cache: dict[str, CachedFrame] = {}
        frames_dir = self._visual_dir / FRAMES_DIR_NAME
        items = frames.get("items") if isinstance(frames.get("items"), dict) else {}
        for key, item in items.items():
            if not isinstance(item, dict):
                continue
            file_name = Path(str(item.get("file_name") or "")).name
            analysis_name = Path(str(item.get("analysis_file_name") or file_name)).name
            if (
                not file_name
                or not (frames_dir / file_name).is_file()
                or not (frames_dir / analysis_name).is_file()
            ):
                continue
            cache[key] = CachedFrame(
                file_name=file_name,
                analysis_file_name=analysis_name,
                sha256=str(item.get("sha256") or ""),
            )
        frames["items"] = {key: items[key] for key in cache}
        return cache

    def lookup_frame(self, cache: dict[str, CachedFrame], timestamp: float) -> CachedFrame | None:
        return cache.get(_timestamp_key(timestamp))

    def record_frame(self, timestamp: float, frame: CachedFrame) -> None:
        frames = self._data.get("frames")
        if not isinstance(frames, dict):
            return
        items = frames.setdefault("items", {})
        items[_timestamp_key(timestamp)] = {
            "timestamp_seconds": float(timestamp),
            "file_name": frame.file_name,
            "analysis_file_name": frame.analysis_file_name,
            "sha256": frame.sha256,
        }

    def forget_frames(self) -> None:
        self._data["frames"] = None


def clear_frames_dir(visual_dir: Path) -> None:
    frames_dir = visual_dir / FRAMES_DIR_NAME
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)


def clear_derived_visual_artifacts(visual_dir: Path) -> None:
    """Remove derived visual artifacts while keeping the source video and its manifest entry."""
    if not visual_dir.exists():
        return
    clear_frames_dir(visual_dir)
    for name in DERIVED_FILE_NAMES:
        try:
            (visual_dir / name).unlink()
        except FileNotFoundError:
            pass
    manifest = VisualAssetManifest(visual_dir)
    manifest.forget_frames()
    manifest.save()
