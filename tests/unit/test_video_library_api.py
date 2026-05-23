import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_sum_service.app import app
from video_sum_service.auth import ACCESS_TOKEN_ENV, AccessTokenManager
from video_sum_service.repository import SqliteTaskRepository
from video_sum_service.schemas import VideoAssetRecord
import video_sum_service.app as service_app


def create_repository() -> SqliteTaskRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SqliteTaskRepository(connection)
    repository.initialize()
    return repository


def add_video(repository: SqliteTaskRepository, canonical_id: str, title: str) -> VideoAssetRecord:
    return repository.upsert_video_asset(
        VideoAssetRecord(
            canonical_id=canonical_id,
            platform="bilibili",
            title=title,
            source_url=f"https://www.bilibili.com/video/{canonical_id}",
        )
    )


def test_video_library_migration_defaults_and_new_video_front_order() -> None:
    repository = create_repository()

    first = add_video(repository, "BV-first", "First")
    second = add_video(repository, "BV-second", "Second")

    assert repository.get_library_preferences().new_video_position == "front"
    assert second.global_order < first.global_order
    assert second.folder_order < first.folder_order


def test_new_video_back_order_preference() -> None:
    repository = create_repository()
    first = add_video(repository, "BV-first", "First")
    repository.update_library_preferences(new_video_position="back")
    second = add_video(repository, "BV-second", "Second")

    assert repository.get_library_preferences().new_video_position == "back"
    assert second.global_order > first.global_order


def test_folder_crud_prevents_cycles_and_delete_moves_videos_to_unfiled() -> None:
    repository = create_repository()
    parent = repository.create_video_folder("课程")
    assert parent is not None
    child = repository.create_video_folder("第一章", parent.folder_id)
    assert child is not None
    video = add_video(repository, "BV-folder", "Folder video")
    moved = repository.move_video_to_folder(video.video_id, child.folder_id)
    assert moved is not None
    assert moved.folder_id == child.folder_id

    with pytest.raises(ValueError):
        repository.update_video_folder(parent.folder_id, parent_id=child.folder_id)

    assert repository.delete_video_folder(parent.folder_id) is True
    refreshed = repository.get_video_asset(video.video_id)
    assert refreshed is not None
    assert refreshed.folder_id is None
    assert refreshed.folder_pinned is False
    assert repository.list_video_folders() == []


def test_update_video_folder_can_move_child_back_to_root() -> None:
    repository = create_repository()
    parent = repository.create_video_folder("父级")
    assert parent is not None
    child = repository.create_video_folder("子级", parent.folder_id)
    assert child is not None

    updated = repository.update_video_folder(child.folder_id, parent_id=None, position=500)

    assert updated is not None
    assert updated.parent_id is None
    assert updated.position == 500


def test_update_video_folder_http_can_clear_parent_id() -> None:
    repository = create_repository()
    app.state.task_repository = repository
    original_access_token_manager = service_app.access_token_manager
    service_app.access_token_manager = AccessTokenManager(
        Path("unused"),
        env={ACCESS_TOKEN_ENV: "library-test-token"},
    )
    try:
        with TestClient(app) as client:
            app.state.task_repository = repository
            parent = repository.create_video_folder("父级")
            assert parent is not None
            child = repository.create_video_folder("子级", parent.folder_id)
            assert child is not None

            response = client.patch(
                f"/api/v1/videos/folders/{child.folder_id}",
                json={"parent_id": None, "position": 500},
                headers={"Authorization": "Bearer library-test-token"},
            )
            library_response = client.get(
                "/api/v1/videos/library",
                headers={"Authorization": "Bearer library-test-token"},
            )
    finally:
        service_app.access_token_manager = original_access_token_manager

    assert response.status_code == 200
    payload = response.json()
    assert payload["parent_id"] is None
    assert payload["position"] == 500
    assert library_response.status_code == 200
    folders = {folder["folder_id"]: folder for folder in library_response.json()["folders"]}
    assert folders[child.folder_id]["parent_id"] is None


def test_global_and_folder_reorder_with_pins() -> None:
    repository = create_repository()
    folder = repository.create_video_folder("排序")
    assert folder is not None
    first = add_video(repository, "BV-first", "First")
    second = add_video(repository, "BV-second", "Second")
    third = add_video(repository, "BV-third", "Third")

    for video in (first, second, third):
        repository.move_video_to_folder(video.video_id, folder.folder_id)

    repository.reorder_videos([third.video_id, first.video_id, second.video_id], "__global__")
    repository.reorder_videos([second.video_id, third.video_id, first.video_id], folder.folder_id)
    repository.set_video_pin(first.video_id, global_pinned=True)
    repository.set_video_pin(second.video_id, folder_pinned=True)

    videos = {video.video_id: video for video in repository.list_video_assets()}
    assert videos[first.video_id].global_pinned is True
    assert videos[second.video_id].folder_pinned is True
    assert videos[third.video_id].global_order < videos[second.video_id].global_order
    assert videos[second.video_id].folder_order < videos[first.video_id].folder_order


def test_reorder_rejects_videos_outside_folder_scope() -> None:
    repository = create_repository()
    folder = repository.create_video_folder("排序")
    assert folder is not None
    in_folder = add_video(repository, "BV-in-folder", "Inside")
    unfiled = add_video(repository, "BV-unfiled", "Unfiled")
    repository.move_video_to_folder(in_folder.video_id, folder.folder_id)

    with pytest.raises(ValueError):
        repository.reorder_videos([in_folder.video_id, unfiled.video_id], folder.folder_id)

    refreshed = repository.get_video_asset(unfiled.video_id)
    assert refreshed is not None
    assert refreshed.folder_id is None


def test_reorder_http_rejects_cross_scope_video_ids() -> None:
    repository = create_repository()
    app.state.task_repository = repository
    original_access_token_manager = service_app.access_token_manager
    service_app.access_token_manager = AccessTokenManager(
        Path("unused"),
        env={ACCESS_TOKEN_ENV: "library-test-token"},
    )
    try:
        with TestClient(app) as client:
            app.state.task_repository = repository
            folder = repository.create_video_folder("排序")
            assert folder is not None
            in_folder = add_video(repository, "BV-in-folder", "Inside")
            unfiled = add_video(repository, "BV-unfiled", "Unfiled")
            repository.move_video_to_folder(in_folder.video_id, folder.folder_id)

            response = client.post(
                "/api/v1/videos/reorder",
                json={"folder_id": folder.folder_id, "video_ids": [in_folder.video_id, unfiled.video_id]},
                headers={"Authorization": "Bearer library-test-token"},
            )
    finally:
        service_app.access_token_manager = original_access_token_manager

    assert response.status_code == 400
    assert "outside the target scope" in response.json()["detail"]
