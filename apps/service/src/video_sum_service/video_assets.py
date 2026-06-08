from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

from fastapi import HTTPException

from video_sum_core.models.tasks import InputType
from video_sum_core.utils import extract_bilibili_page, normalize_video_url
from video_sum_infra.runtime import ffmpeg_location

from video_sum_service.context import COVER_CACHE_DIR, settings_manager
from video_sum_service.integrations import cache_cover_image
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord, VideoPageOptionResponse

logger = logging.getLogger("video_sum_service.app")
YoutubeDL = None
DownloadError = None

_BILIBILI_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
}
LOCAL_VIDEO_SUFFIXES = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".webm",
    ".flv",
    ".m4v",
    ".ts",
    ".mpeg",
    ".mpg",
}
LOCAL_AUDIO_SUFFIXES = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
}


def with_bilibili_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query_pairs: list[tuple[str, str]] = []
    if parsed.query:
        for chunk in parsed.query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            if key != "p":
                query_pairs.append((key, value))
    query_pairs.append(("p", str(page)))
    encoded_query = urlencode(query_pairs)
    return parsed._replace(query=encoded_query).geturl()


def build_page_canonical_id(base_id: str, page: int) -> str:
    return f"{base_id}?p={page}"


def extract_page_part_from_title(base_title: str, page: int, title: str) -> str:
    candidate = str(title or "").strip()
    if not candidate:
        return ""

    normalized_base = re.sub(r"\s+", " ", base_title).strip()
    normalized_candidate = re.sub(r"\s+", " ", candidate).strip()
    if normalized_base and normalized_candidate.startswith(normalized_base):
        candidate = normalized_candidate[len(normalized_base):].strip(" -_")
    else:
        candidate = normalized_candidate

    candidate = re.sub(rf"^[Pp]0*{page}\s*", "", candidate).strip(" -_")
    candidate = re.sub(rf"^第?\s*0*{page}\s*[Pp]?\s*", "", candidate).strip(" -_")
    return candidate


def build_page_title(base_title: str, page: int, info: dict[str, object]) -> str:
    part = str(info.get("part") or "").strip()
    title = str(info.get("title") or "").strip()
    candidate = part or extract_page_part_from_title(base_title, page, title)
    if not candidate:
        return f"P{page}"
    return f"P{page} {candidate}"


def build_ydl_probe_options(*, extract_flat: bool = False) -> dict[str, object]:
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": _BILIBILI_HTTP_HEADERS,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "sleep_interval_requests": 1,
        "socket_timeout": 30,
    }
    cookie_file = str(
        settings_manager.current.ytdlp_cookies_file
        or os.environ.get("VIDEO_SUM_YTDLP_COOKIES_FILE")
        or os.environ.get("YTDLP_COOKIES_FILE")
        or ""
    ).strip()
    if cookie_file:
        cookie_path = Path(cookie_file).expanduser()
        if cookie_path.exists() and cookie_path.is_file():
            options["cookiefile"] = str(cookie_path)
        else:
            logger.warning("yt-dlp cookie file does not exist: %s", cookie_path)
    if extract_flat:
        options["extract_flat"] = "in_playlist"
    return options


def extract_video_info(url: str, *, extract_flat: bool = False) -> dict[str, object]:
    global DownloadError, YoutubeDL
    if YoutubeDL is None:
        from yt_dlp import YoutubeDL as YtDlpYoutubeDL

        YoutubeDL = YtDlpYoutubeDL
    if DownloadError is None:
        from yt_dlp.utils import DownloadError as YtDlpDownloadError

        DownloadError = YtDlpDownloadError

    options = build_ydl_probe_options(extract_flat=extract_flat)
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        message = str(exc)
        if "Could not copy Chrome cookie database" in message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "无法读取 Chrome 登录态：yt-dlp 不能复制 Chrome Cookie 数据库。"
                    "请通过 VidMind 的 B 站登录窗口重新捕获登录态，或按教程导出 cookies.txt 并填写 yt-dlp Cookies 文件。"
                ),
            ) from exc
        if "Failed to decrypt with DPAPI" in message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "无法读取浏览器登录态：yt-dlp 解密 Windows 浏览器 Cookie 失败（DPAPI）。"
                    "请通过 VidMind 的 B 站登录窗口重新捕获登录态，或按教程导出 cookies.txt 并填写 yt-dlp Cookies 文件。"
                ),
            ) from exc
        if "HTTP Error 412" in message and "BiliBili" in message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "B 站返回 HTTP 412，当前请求可能被风控拦截。"
                    "请稍后重试、切换网络，或通过 VidMind 的 B 站登录窗口捕获登录态。"
                ),
            ) from exc
        raise
    if not isinstance(info, dict):
        raise HTTPException(status_code=400, detail="无法读取视频信息。")
    return info


def extract_thumbnail_url(payload: dict[str, object] | None) -> str:
    if not payload:
        return ""

    for key in ("thumbnail", "cover", "pic", "first_frame"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    thumbnails = payload.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                value = item.get("url")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            elif isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def first_non_empty(*values: str | None) -> str:
    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), "")


def fetch_bilibili_page_catalog_payload(url: str) -> tuple[list[dict[str, object]], str]:
    import httpx

    try:
        response = httpx.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.bilibili.com/",
            },
            follow_redirects=True,
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("failed to fetch bilibili page catalog: %s", url, exc_info=True)
        return [], ""

    match = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*\(function", response.text, re.S)
    if not match:
        return [], ""

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("failed to parse bilibili page catalog payload: %s", url, exc_info=True)
        return [], ""

    video_data = payload.get("videoData") or {}
    pages = video_data.get("pages") or []
    cover_url = str(video_data.get("pic") or "").strip()
    return [item for item in pages if isinstance(item, dict)], cover_url


def fetch_bilibili_page_catalog(url: str) -> list[dict[str, object]]:
    pages, _cover_url = fetch_bilibili_page_catalog_payload(url)
    return pages


def build_base_video_asset(
    *,
    canonical_id: str,
    platform: str,
    title: str,
    source_url: str,
    cover_url: str,
    duration: float | None,
    pages: list[VideoPageOptionResponse],
) -> VideoAssetRecord:
    return VideoAssetRecord(
        canonical_id=canonical_id,
        platform=platform,
        title=title,
        source_url=source_url,
        cover_url=cover_url,
        duration=duration,
        pages=pages,
    )


def merge_video_asset_metadata(
    existing: VideoAssetRecord,
    refreshed: VideoAssetRecord,
) -> VideoAssetRecord:
    existing_pages = {page.page: page for page in existing.pages}
    merged_pages: list[VideoPageOptionResponse] = []

    if refreshed.pages:
        for refreshed_page in refreshed.pages:
            existing_page = existing_pages.get(refreshed_page.page)
            if existing_page is None:
                merged_pages.append(refreshed_page)
                continue
            merged_pages.append(
                existing_page.model_copy(
                    update={
                        "title": refreshed_page.title or existing_page.title,
                        "source_url": refreshed_page.source_url or existing_page.source_url,
                        "cover_url": refreshed_page.cover_url or existing_page.cover_url,
                        "duration": refreshed_page.duration if refreshed_page.duration is not None else existing_page.duration,
                    }
                )
            )
        refreshed_page_numbers = {page.page for page in refreshed.pages}
        for existing_page in existing.pages:
            if existing_page.page not in refreshed_page_numbers:
                merged_pages.append(existing_page)
    else:
        merged_pages = list(existing.pages)

    cover_url = refreshed.cover_url or existing.cover_url
    if not cover_url and merged_pages:
        cover_url = next((page.cover_url for page in merged_pages if page.cover_url), "")

    return existing.model_copy(
        update={
            "platform": refreshed.platform or existing.platform,
            "title": refreshed.title or existing.title,
            "source_url": refreshed.source_url or existing.source_url,
            "cover_url": cover_url,
            "duration": refreshed.duration if refreshed.duration is not None else existing.duration,
            "pages": merged_pages,
        }
    )


def resolve_local_media_path(value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    candidate_text = raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() == "file":
        candidate_text = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", candidate_text):
            candidate_text = candidate_text.lstrip("/")

    candidate = Path(candidate_text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def is_supported_local_media_file(path: Path) -> bool:
    return path.suffix.lower() in LOCAL_VIDEO_SUFFIXES | LOCAL_AUDIO_SUFFIXES


def infer_local_input_type(source: str | Path) -> InputType:
    path = source if isinstance(source, Path) else Path(str(source))
    return InputType.AUDIO_FILE if path.suffix.lower() in LOCAL_AUDIO_SUFFIXES else InputType.VIDEO_FILE


def build_local_media_canonical_id(file_path: Path) -> str:
    try:
        stats = file_path.stat()
        mtime_ns = getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000))
        fingerprint = f"{str(file_path).lower()}|{stats.st_size}|{mtime_ns}"
    except OSError:
        fingerprint = str(file_path).lower()
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"local-{digest}"


def resolve_ffprobe_executable() -> Path | None:
    ffmpeg_exe = ffmpeg_location()
    if ffmpeg_exe is not None:
        candidate = ffmpeg_exe.with_name("ffprobe.exe" if ffmpeg_exe.suffix.lower() == ".exe" else "ffprobe")
        if candidate.exists():
            return candidate

    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path:
        return Path(ffprobe_path).resolve()
    return None


def probe_local_media_duration(file_path: Path) -> float | None:
    ffprobe_exe = resolve_ffprobe_executable()
    if ffprobe_exe is None:
        return None

    try:
        result = subprocess.run(
            [
                str(ffprobe_exe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("failed to probe local media duration: %s", file_path, exc_info=True)
        return None

    if result.returncode != 0:
        logger.warning("ffprobe duration failed path=%s stderr=%s", file_path, result.stderr.strip())
        return None

    try:
        duration = float((result.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def extract_local_video_cover(
    file_path: Path,
    canonical_id: str,
    duration: float | None,
    *,
    force_refresh: bool = False,
) -> str:
    if file_path.suffix.lower() not in LOCAL_VIDEO_SUFFIXES:
        return ""

    ffmpeg_exe = ffmpeg_location()
    if ffmpeg_exe is None:
        return ""

    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = COVER_CACHE_DIR / f"{canonical_id}.jpg"
    if target.exists() and not force_refresh:
        return f"/media/covers/{target.name}"
    if force_refresh and target.exists():
        try:
            target.unlink()
        except OSError:
            logger.warning("failed to remove stale local cover: %s", target, exc_info=True)

    timestamp_seconds = 0.0
    if duration and duration > 1:
        timestamp_seconds = min(max(duration * 0.25, 0.2), max(duration - 0.2, 0.0))

    command = [str(ffmpeg_exe), "-y"]
    if timestamp_seconds > 0:
        command.extend(["-ss", f"{timestamp_seconds:.3f}"])
    command.extend(["-i", str(file_path), "-frames:v", "1", "-q:v", "2", str(target)])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("failed to extract local video cover: %s", file_path, exc_info=True)
        return ""

    if result.returncode != 0 or not target.exists():
        logger.warning(
            "ffmpeg cover extraction failed path=%s returncode=%s stderr=%s",
            file_path,
            result.returncode,
            result.stderr.strip(),
        )
        return ""

    return f"/media/covers/{target.name}"


def probe_local_video_asset(
    file_path: Path,
    *,
    force_refresh: bool = False,
    title_override: str | None = None,
    canonical_id_override: str | None = None,
) -> VideoAssetRecord:
    canonical_id = canonical_id_override or build_local_media_canonical_id(file_path)
    duration = probe_local_media_duration(file_path)
    cover_url = extract_local_video_cover(
        file_path,
        canonical_id,
        duration,
        force_refresh=force_refresh,
    )
    title = str(title_override or "").strip() or file_path.stem.strip() or file_path.name
    return build_base_video_asset(
        canonical_id=canonical_id,
        platform="local",
        title=title,
        source_url=str(file_path),
        cover_url=cover_url,
        duration=duration,
        pages=[],
    )


def resolve_video_page(video: VideoAssetRecord, page_number: int | None) -> VideoPageOptionResponse | None:
    if not video.pages:
        return None
    target_page = page_number or 1
    return next((page for page in video.pages if page.page == target_page), None)


def probe_video_asset(
    url: str,
    force_refresh: bool = False,
) -> tuple[VideoAssetRecord, list[VideoPageOptionResponse], bool]:
    local_media_path = resolve_local_media_path(url)
    if local_media_path is not None:
        if not is_supported_local_media_file(local_media_path):
            raise HTTPException(status_code=400, detail="当前仅支持导入常见本地视频或音频文件。")
        return probe_local_video_asset(local_media_path, force_refresh=force_refresh), [], False

    normalized = normalize_video_url(url)
    normalized_url = normalized.normalized_url
    canonical_id = normalized.canonical_id
    platform = normalized.platform

    if platform not in {"bilibili", "youtube"}:
        raise HTTPException(status_code=400, detail="当前仅支持 Bilibili 或 YouTube 单条视频链接。")

    if platform == "youtube":
        info = extract_video_info(normalized_url)
        title = str(info.get("title") or canonical_id or normalized_url)
        thumbnail = str(info.get("thumbnail") or "")
        duration = float(info.get("duration")) if info.get("duration") else None
        actual_id = str(info.get("id") or canonical_id or normalized_url)
        cached_cover_url = cache_cover_image(thumbnail, actual_id, referer_url=normalized_url)
        return (
            build_base_video_asset(
                canonical_id=actual_id,
                platform="youtube",
                title=title,
                source_url=normalized_url,
                cover_url=cached_cover_url,
                duration=duration,
                pages=[],
            ),
            [],
            False,
        )

    requested_page = extract_bilibili_page(normalized_url)

    if requested_page is None:
        flat_info = extract_video_info(normalized_url, extract_flat=True)
        flat_entries = [entry for entry in flat_info.get("entries", []) if isinstance(entry, dict)]
        if len(flat_entries) > 1:
            top_title = str(flat_info.get("title") or canonical_id or normalized_url)
            top_id = str(flat_info.get("id") or canonical_id or normalized_url)
            catalog_entries, catalog_cover_url = fetch_bilibili_page_catalog_payload(normalized_url)
            catalog_by_page = {
                int(item.get("page") or 0): item
                for item in catalog_entries
                if str(item.get("page") or "").isdigit()
            }
            top_thumbnail = first_non_empty(
                extract_thumbnail_url(flat_info),
                catalog_cover_url,
                *(extract_thumbnail_url(entry) for entry in flat_entries),
                *(extract_thumbnail_url(entry) for entry in catalog_entries),
            )
            cached_cover_url = cache_cover_image(top_thumbnail, top_id, referer_url=normalized_url)
            pages: list[VideoPageOptionResponse] = []
            for index, entry in enumerate(flat_entries, start=1):
                page_url = str(
                    entry.get("url")
                    or with_bilibili_page(f"https://www.bilibili.com/video/{canonical_id}", index)
                )
                catalog_entry = catalog_by_page.get(index)
                page_title = build_page_title(
                    top_title,
                    index,
                    {
                        "part": str((catalog_entry or {}).get("part") or "").strip(),
                        "title": str(entry.get("title") or "").strip(),
                    },
                )
                pages.append(
                    VideoPageOptionResponse(
                        page=index,
                        title=page_title,
                        source_url=page_url,
                        cover_url=first_non_empty(
                            extract_thumbnail_url(entry),
                            extract_thumbnail_url(catalog_entry),
                            cached_cover_url,
                            top_thumbnail,
                        ),
                        duration=None,
                    )
                )

            return (
                build_base_video_asset(
                    canonical_id=top_id,
                    platform=str(flat_info.get("extractor_key") or "video").lower(),
                    title=top_title,
                    source_url=f"https://www.bilibili.com/video/{canonical_id}",
                    cover_url=cached_cover_url,
                    duration=None,
                    pages=pages,
                ),
                pages,
                True,
            )

    info = extract_video_info(normalized_url)

    title = str(info.get("title") or canonical_id or normalized_url)
    thumbnail = str(info.get("thumbnail") or "")
    duration = float(info.get("duration")) if info.get("duration") else None
    platform = str(info.get("extractor_key") or "video").lower()
    actual_id = str(info.get("id") or canonical_id or normalized_url)
    entries = [entry for entry in info.get("entries", []) if isinstance(entry, dict)]

    if entries:
        pages: list[VideoPageOptionResponse] = []
        for index, entry in enumerate(entries, start=1):
            page = int(entry.get("page") or entry.get("playlist_index") or index)
            page_title = build_page_title(title, page, entry)
            page_url = with_bilibili_page(f"https://www.bilibili.com/video/{canonical_id}", page)
            page_cover = str(entry.get("thumbnail") or thumbnail or "")
            page_duration = float(entry.get("duration")) if entry.get("duration") else None
            pages.append(
                VideoPageOptionResponse(
                    page=page,
                    title=page_title,
                    source_url=page_url,
                    cover_url=page_cover,
                    duration=page_duration,
                )
            )

        selected_page = requested_page or pages[0].page
        selected = next((item for item in pages if item.page == selected_page), pages[0])
        selected_cover = cache_cover_image(
            selected.cover_url or thumbnail,
            actual_id,
            referer_url=selected.source_url,
        )
        return (
            build_base_video_asset(
                canonical_id=actual_id,
                platform=platform,
                title=title,
                source_url=f"https://www.bilibili.com/video/{canonical_id}",
                cover_url=selected_cover,
                duration=duration,
                pages=pages,
            ),
            pages,
            requested_page is None and len(pages) > 1,
        )

    cached_cover_url = cache_cover_image(thumbnail, actual_id, referer_url=normalized_url)
    return (
        build_base_video_asset(
            canonical_id=actual_id,
            platform=platform,
            title=title,
            source_url=normalized_url,
            cover_url=cached_cover_url,
            duration=duration,
            pages=[],
        ),
        [],
        False,
    )


def localize_video_cover(task_store: SqliteTaskRepository, video: VideoAssetRecord) -> VideoAssetRecord:
    if not video.cover_url and str(video.platform or "").lower() == "bilibili":
        probe_url = video.source_url or f"https://www.bilibili.com/video/{str(video.canonical_id).split('?', 1)[0]}"
        try:
            refreshed, _pages, _requires_selection = probe_video_asset(probe_url, force_refresh=True)
        except Exception:
            logger.warning("failed to refresh missing bilibili cover: %s", video.video_id, exc_info=True)
            return video
        merged = merge_video_asset_metadata(video, refreshed)
        if merged.cover_url:
            return task_store.upsert_video_asset(merged)
        return video
    if not video.cover_url or video.cover_url.startswith("/media/"):
        return video
    localized = cache_cover_image(video.cover_url, video.canonical_id, referer_url=video.source_url)
    if localized == video.cover_url:
        return video
    updated = video.model_copy(update={"cover_url": localized})
    return task_store.upsert_video_asset(updated)
