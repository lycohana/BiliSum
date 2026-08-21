from __future__ import annotations

import sqlite3

from video_sum_core.utils import normalize_video_url
from video_sum_service import app as service_app
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.routers import videos as videos_router
from video_sum_service.schemas import VideoAssetRecord, VideoPageOptionResponse


class _FakeYoutubeDL:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeYoutubeDL":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
        return self._payload


def test_normalize_video_url_preserves_requested_page() -> None:
    result = normalize_video_url("https://www.bilibili.com/video/BV1xx411c7mD?p=3")

    assert result.normalized_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=3"
    assert result.canonical_id == "BV1xx411c7mD"
    assert result.platform == "bilibili"
    assert result.page_number == 3


def test_normalize_video_url_accepts_raw_bvid() -> None:
    result = normalize_video_url("BV1xx411c7mD")

    assert result.normalized_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert result.canonical_id == "BV1xx411c7mD"
    assert result.platform == "bilibili"


def test_normalize_video_url_accepts_raw_bvid_with_page() -> None:
    result = normalize_video_url("BV1xx411c7mD?p=2")

    assert result.normalized_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert result.canonical_id == "BV1xx411c7mD"
    assert result.platform == "bilibili"
    assert result.page_number == 2


def test_fetch_bilibili_collection_payload_reads_ugc_season(monkeypatch) -> None:
    monkeypatch.setattr(
        service_app.video_assets,
        "_fetch_bilibili_initial_state_payload",
        lambda url: {
            "videoData": {
                "pic": "https://example.com/current.jpg",
                "owner": {
                    "mid": 150415653,
                    "name": "测试 UP 主",
                    "face": "https://example.com/face.jpg",
                },
                "ugc_season": {
                    "id": 1677227,
                    "title": "测试合集",
                    "cover": "https://example.com/collection.jpg",
                    "sections": [
                        {
                            "episodes": [
                                {
                                    "bvid": "BV1other",
                                    "arc": {"title": "第一集", "pic": "https://example.com/one.jpg"},
                                    "page": {"duration": 61},
                                },
                                {
                                    "bvid": "BV1current",
                                    "arc": {"title": "第二集"},
                                    "page": {"duration": 95},
                                },
                            ],
                        },
                    ],
                },
            },
        },
    )

    collection = service_app.video_assets.fetch_bilibili_collection_payload(
        "https://www.bilibili.com/video/BV1current/"
    )

    assert collection is not None
    assert collection["collection_id"] == "1677227"
    assert collection["title"] == "测试合集"
    assert collection["item_count"] == 2
    assert collection["owner_mid"] == "150415653"
    assert collection["owner_name"] == "测试 UP 主"
    assert collection["owner_url"] == "https://space.bilibili.com/150415653"
    assert collection["items"][1]["is_current"] is True
    assert collection["items"][0]["source_url"] == "https://www.bilibili.com/video/BV1other/"


def test_normalize_video_url_accepts_youtube_watch_url() -> None:
    result = normalize_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123")

    assert result.normalized_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.canonical_id == "dQw4w9WgXcQ"
    assert result.platform == "youtube"
    assert result.page_number is None


def test_normalize_video_url_accepts_youtu_be_and_shorts() -> None:
    short_link = normalize_video_url("https://youtu.be/dQw4w9WgXcQ?t=43")
    shorts_link = normalize_video_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")

    assert short_link.normalized_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert short_link.canonical_id == "dQw4w9WgXcQ"
    assert short_link.platform == "youtube"
    assert shorts_link.normalized_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert shorts_link.canonical_id == "dQw4w9WgXcQ"
    assert shorts_link.platform == "youtube"


def test_probe_video_asset_requires_page_selection_for_multi_page(monkeypatch) -> None:
    payload = {
        "id": "BV1xx411c7mD",
        "title": "测试合集",
        "thumbnail": "https://example.com/cover.jpg",
        "extractor_key": "BiliBili",
        "entries": [
            {"page": 1, "title": "P1 开场", "duration": 61, "thumbnail": "https://example.com/p1.jpg"},
            {"page": 2, "title": "P2 正片", "duration": 95, "thumbnail": "https://example.com/p2.jpg"},
        ],
    }

    monkeypatch.setattr(service_app.video_assets, "YoutubeDL", lambda options: _FakeYoutubeDL(payload))
    monkeypatch.setattr(service_app.video_assets, "cache_cover_image", lambda source_url, canonical_id, referer_url=None: source_url)

    asset, pages, requires_selection = service_app.probe_video_asset("https://www.bilibili.com/video/BV1xx411c7mD")

    assert requires_selection is True
    assert asset.canonical_id == "BV1xx411c7mD"
    assert asset.source_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert len(pages) == 2
    assert pages[1].page == 2
    assert pages[1].source_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"


def test_probe_video_asset_uses_flat_playlist_thumbnail_for_multi_page_cover(monkeypatch) -> None:
    payload = {
        "id": "BV1xx411c7mD",
        "title": "测试合集",
        "extractor_key": "BiliBili",
        "entries": [
            {"url": "https://www.bilibili.com/video/BV1xx411c7mD?p=1", "title": "P1 开场"},
            {"url": "https://www.bilibili.com/video/BV1xx411c7mD?p=2", "title": "P2 正片"},
        ],
    }

    monkeypatch.setattr(service_app.video_assets, "YoutubeDL", lambda options: _FakeYoutubeDL(payload))
    monkeypatch.setattr(
        service_app.video_assets,
        "fetch_bilibili_page_catalog_payload",
        lambda url: (
            [
                {"page": 1, "part": "开场"},
                {"page": 2, "part": "正片"},
            ],
            "https://example.com/cover.jpg",
        ),
    )
    monkeypatch.setattr(service_app.video_assets, "cache_cover_image", lambda source_url, canonical_id, referer_url=None: f"/media/covers/{canonical_id}.jpg" if source_url else "")

    asset, pages, requires_selection = service_app.probe_video_asset("https://www.bilibili.com/video/BV1xx411c7mD")

    assert requires_selection is True
    assert asset.cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert pages[0].cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert pages[1].cover_url == "/media/covers/BV1xx411c7mD.jpg"


def test_probe_video_asset_uses_thumbnail_candidates_for_multi_page_cover(monkeypatch) -> None:
    payload = {
        "id": "BV1xx411c7mD",
        "title": "测试合集",
        "extractor_key": "BiliBili",
        "thumbnails": [
            {"url": "https://example.com/small.jpg"},
            {"url": "https://example.com/large.jpg"},
        ],
        "entries": [
            {
                "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                "title": "P1 开场",
                "thumbnails": [{"url": "https://example.com/p1-large.jpg"}],
            },
            {
                "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
                "title": "P2 正片",
            },
        ],
    }

    monkeypatch.setattr(service_app.video_assets, "YoutubeDL", lambda options: _FakeYoutubeDL(payload))
    monkeypatch.setattr(
        service_app.video_assets,
        "fetch_bilibili_page_catalog_payload",
        lambda url: (
            [
                {"page": 1, "part": "开场"},
                {"page": 2, "part": "正片", "pic": "https://example.com/p2.jpg"},
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        service_app.video_assets,
        "cache_cover_image",
        lambda source_url, canonical_id, referer_url=None: f"/media/covers/{canonical_id}.jpg" if source_url else "",
    )

    asset, pages, requires_selection = service_app.probe_video_asset("https://www.bilibili.com/video/BV1xx411c7mD")

    assert requires_selection is True
    assert asset.cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert pages[0].cover_url == "https://example.com/p1-large.jpg"
    assert pages[1].cover_url == "https://example.com/p2.jpg"


def test_probe_video_asset_returns_selected_page_when_page_is_explicit(monkeypatch) -> None:
    payload = {
        "id": "BV1xx411c7mD",
        "title": "测试合集",
        "thumbnail": "https://example.com/cover.jpg",
        "extractor_key": "BiliBili",
        "entries": [
            {"page": 1, "title": "P1 开场", "duration": 61, "thumbnail": "https://example.com/p1.jpg"},
            {"page": 2, "title": "P2 正片", "duration": 95, "thumbnail": "https://example.com/p2.jpg"},
        ],
    }

    monkeypatch.setattr(service_app.video_assets, "YoutubeDL", lambda options: _FakeYoutubeDL(payload))
    monkeypatch.setattr(service_app.video_assets, "cache_cover_image", lambda source_url, canonical_id, referer_url=None: source_url)

    asset, pages, requires_selection = service_app.probe_video_asset("https://www.bilibili.com/video/BV1xx411c7mD?p=2")

    assert requires_selection is False
    assert asset.canonical_id == "BV1xx411c7mD"
    assert asset.source_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert asset.title == "测试合集"
    assert asset.pages[1].title == "P2 正片"
    assert len(pages) == 2


def test_merge_video_asset_metadata_preserves_existing_cover_for_multi_page_refresh() -> None:
    existing = VideoAssetRecord(
        canonical_id="BV1xx411c7mD",
        platform="bilibili",
        title="测试合集",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
        cover_url="/media/covers/BV1xx411c7mD.jpg",
        duration=120.0,
        pages=[
            VideoPageOptionResponse(
                page=1,
                title="P1 开场",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                cover_url="https://example.com/p1.jpg",
                duration=61.0,
            ),
            VideoPageOptionResponse(
                page=2,
                title="P2 正片",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=2",
                cover_url="https://example.com/p2.jpg",
                duration=95.0,
            ),
        ],
    )
    refreshed = VideoAssetRecord(
        canonical_id="BV1xx411c7mD",
        platform="bilibili",
        title="测试合集",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
        cover_url="",
        duration=None,
        pages=[
            VideoPageOptionResponse(
                page=1,
                title="P1 开场",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                cover_url="",
                duration=None,
            ),
            VideoPageOptionResponse(
                page=2,
                title="P2 正片",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=2",
                cover_url="",
                duration=None,
            ),
        ],
    )

    merged = service_app.video_assets.merge_video_asset_metadata(existing, refreshed)

    assert merged.cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert merged.pages[0].cover_url == "https://example.com/p1.jpg"
    assert merged.pages[1].cover_url == "https://example.com/p2.jpg"
    assert merged.duration == 120.0


def test_probe_video_route_preserves_existing_multi_page_cover_on_force_refresh(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    existing = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1xx411c7mD",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV1xx411c7mD",
            cover_url="/media/covers/BV1xx411c7mD.jpg",
            pages=[
                VideoPageOptionResponse(
                    page=1,
                    title="P1 开场",
                    source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                    cover_url="https://example.com/p1.jpg",
                    duration=61.0,
                ),
            ],
        )
    )

    monkeypatch.setattr(
        videos_router,
        "probe_video_asset",
        lambda url, force_refresh=False: (
            VideoAssetRecord(
                canonical_id="BV1xx411c7mD",
                platform="bilibili",
                title="测试合集",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD",
                cover_url="",
                pages=[
                    VideoPageOptionResponse(
                        page=1,
                        title="P1 开场",
                        source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                        cover_url="",
                        duration=None,
                    ),
                ],
            ),
            [],
            True,
        ),
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"task_repository": repository})()})()})()
    response = videos_router.probe_video(
        videos_router.VideoProbeRequest(url="https://www.bilibili.com/video/BV1xx411c7mD", force_refresh=True),
        request,
    )

    assert response.video.video_id == existing.video_id
    assert response.video.cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert response.video.pages[0].cover_url == "https://example.com/p1.jpg"


def test_probe_video_route_updates_existing_empty_multi_page_cover_on_force_refresh(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1xx411c7mD",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV1xx411c7mD",
            cover_url="",
            pages=[
                VideoPageOptionResponse(
                    page=1,
                    title="P1 开场",
                    source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                    cover_url="",
                    duration=None,
                ),
            ],
        )
    )

    monkeypatch.setattr(
        videos_router,
        "probe_video_asset",
        lambda url, force_refresh=False: (
            VideoAssetRecord(
                canonical_id="BV1xx411c7mD",
                platform="bilibili",
                title="测试合集",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD",
                cover_url="/media/covers/BV1xx411c7mD.jpg",
                pages=[
                    VideoPageOptionResponse(
                        page=1,
                        title="P1 开场",
                        source_url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                        cover_url="/media/covers/BV1xx411c7mD.jpg",
                        duration=None,
                    ),
                ],
            ),
            [],
            True,
        ),
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"task_repository": repository})()})()})()
    response = videos_router.probe_video(
        videos_router.VideoProbeRequest(url="https://www.bilibili.com/video/BV1xx411c7mD", force_refresh=True),
        request,
    )
    refreshed = repository.get_video_asset(asset.video_id)

    assert response.video.video_id == asset.video_id
    assert response.video.cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert refreshed is not None
    assert refreshed.cover_url == "/media/covers/BV1xx411c7mD.jpg"


def test_list_videos_refreshes_missing_bilibili_cover(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    asset = repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id="BV1xx411c7mD",
            platform="bilibili",
            title="测试合集",
            source_url="https://www.bilibili.com/video/BV1xx411c7mD",
            cover_url="",
        )
    )

    monkeypatch.setattr(
        service_app.video_assets,
        "probe_video_asset",
        lambda url, force_refresh=False: (
            VideoAssetRecord(
                canonical_id="BV1xx411c7mD",
                platform="bilibili",
                title="测试合集",
                source_url="https://www.bilibili.com/video/BV1xx411c7mD",
                cover_url="/media/covers/BV1xx411c7mD.jpg",
            ),
            [],
            False,
        ),
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"task_repository": repository})()})()})()
    response = videos_router.list_videos(request)
    refreshed = repository.get_video_asset(asset.video_id)

    assert response[0].cover_url == "/media/covers/BV1xx411c7mD.jpg"
    assert refreshed is not None
    assert refreshed.cover_url == "/media/covers/BV1xx411c7mD.jpg"


def test_probe_video_asset_returns_youtube_single_video(monkeypatch) -> None:
    payload = {
        "id": "dQw4w9WgXcQ",
        "title": "示例 YouTube 视频",
        "thumbnail": "https://example.com/youtube-cover.jpg",
        "duration": 123,
        "extractor_key": "Youtube",
    }

    monkeypatch.setattr(service_app.video_assets, "YoutubeDL", lambda options: _FakeYoutubeDL(payload))
    monkeypatch.setattr(service_app.video_assets, "cache_cover_image", lambda source_url, canonical_id, referer_url=None: source_url)

    asset, pages, requires_selection = service_app.probe_video_asset("https://youtu.be/dQw4w9WgXcQ")

    assert requires_selection is False
    assert asset.canonical_id == "dQw4w9WgXcQ"
    assert asset.platform == "youtube"
    assert asset.source_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert asset.title == "示例 YouTube 视频"
    assert pages == []


def test_probe_video_asset_returns_local_video_from_file_path(monkeypatch, tmp_path) -> None:
    local_file = tmp_path / "本地演示视频.mp4"
    local_file.write_bytes(b"fake-video")

    monkeypatch.setattr(service_app.video_assets, "probe_local_media_duration", lambda path: 93.5)
    monkeypatch.setattr(
        service_app.video_assets,
        "extract_local_video_cover",
        lambda path, canonical_id, duration, force_refresh=False: f"/media/covers/{canonical_id}.jpg",
    )

    asset, pages, requires_selection = service_app.probe_video_asset(str(local_file))

    assert requires_selection is False
    assert pages == []
    assert asset.platform == "local"
    assert asset.title == "本地演示视频"
    assert asset.source_url == str(local_file.resolve())
    assert asset.cover_url.startswith("/media/covers/local-")
    assert asset.duration == 93.5
