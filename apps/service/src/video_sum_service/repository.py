import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from video_sum_core.models.tasks import TaskInput, TaskResult, TaskStatus
from video_sum_infra.db import sqlite_cursor
from video_sum_service.schemas import (
    KnowledgeIndexChunkRecord,
    TaskEventRecord,
    TaskRecord,
    VideoTagRecord,
    VideoAssetRecord,
    VideoFolderResponse,
    VideoLibraryPreferencesResponse,
    VideoPageOptionResponse,
)

_UNSET = object()


class SqliteTaskRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = Lock()

    def initialize(self) -> None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_assets (
                    video_id TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    cover_url TEXT,
                    duration REAL,
                    page_catalog_json TEXT NOT NULL DEFAULT '[]',
                    latest_task_id TEXT,
                    latest_status TEXT,
                    latest_stage TEXT,
                    latest_error_message TEXT,
                    is_favorite INTEGER NOT NULL DEFAULT 0,
                    favorite_updated_at TEXT,
                    folder_id TEXT,
                    global_order REAL NOT NULL DEFAULT 0,
                    folder_order REAL NOT NULL DEFAULT 0,
                    global_pinned INTEGER NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(cursor, "video_assets", "page_catalog_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(cursor, "video_assets", "is_favorite", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_assets", "favorite_updated_at", "TEXT")
            self._ensure_column(cursor, "video_assets", "folder_id", "TEXT")
            self._ensure_column(cursor, "video_assets", "global_order", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_assets", "folder_order", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_assets", "global_pinned", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_assets", "folder_pinned", "INTEGER NOT NULL DEFAULT 0")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_folders (
                    folder_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_folder_memberships (
                    video_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    folder_order REAL NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, folder_id),
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id),
                    FOREIGN KEY(folder_id) REFERENCES video_folders(folder_id)
                )
                """
            )
            cursor.execute(
                """
                INSERT OR IGNORE INTO video_folder_memberships (video_id, folder_id, folder_order, folder_pinned, created_at, updated_at)
                SELECT video_id, folder_id, folder_order, folder_pinned, created_at, updated_at
                FROM video_assets
                WHERE folder_id IS NOT NULL
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS library_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    video_id TEXT,
                    status TEXT NOT NULL,
                    task_input_json TEXT NOT NULL,
                    page_number INTEGER,
                    page_title TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(cursor, "tasks", "video_id", "TEXT")
            self._ensure_column(cursor, "tasks", "page_number", "INTEGER")
            self._ensure_column(cursor, "tasks", "page_title", "TEXT")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_results (
                    task_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_tags (
                    video_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, tag),
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_index (
                    chunk_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    indexed_content TEXT NOT NULL,
                    index_type TEXT NOT NULL DEFAULT 'video_summary',
                    segment_order INTEGER,
                    anchor_label TEXT,
                    anchor_seconds REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id)
                )
                """
            )
            self._ensure_column(cursor, "knowledge_index", "chunk_id", "TEXT")
            self._ensure_column(cursor, "knowledge_index", "video_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cursor, "knowledge_index", "embedding_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(cursor, "knowledge_index", "indexed_content", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cursor, "knowledge_index", "index_type", "TEXT NOT NULL DEFAULT 'video_summary'")
            self._ensure_column(cursor, "knowledge_index", "segment_order", "INTEGER")
            self._ensure_column(cursor, "knowledge_index", "anchor_label", "TEXT")
            self._ensure_column(cursor, "knowledge_index", "anchor_seconds", "REAL")
            self._ensure_column(cursor, "knowledge_index", "created_at", "TEXT")
            self._ensure_column(cursor, "knowledge_index", "updated_at", "TEXT")

    def _ensure_column(self, cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
        rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
        if column not in names:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _canonical_family_pattern(self, canonical_id: str) -> tuple[str, str]:
        base = str(canonical_id or "").split("?", 1)[0]
        return base, f"{base}?p=%"

    def _select_video_columns(self, prefix: str = "v") -> str:
        return f"""
                    {prefix}.video_id, {prefix}.canonical_id, {prefix}.platform, {prefix}.title, {prefix}.source_url, {prefix}.cover_url, {prefix}.duration,
                    {prefix}.page_catalog_json,
                    {prefix}.latest_task_id, {prefix}.latest_status, {prefix}.latest_stage, {prefix}.latest_error_message,
                    {prefix}.is_favorite, {prefix}.favorite_updated_at,
                    {prefix}.folder_id, {prefix}.global_order, {prefix}.folder_order, {prefix}.global_pinned, {prefix}.folder_pinned,
                    {prefix}.created_at, {prefix}.updated_at, r.result_json AS latest_result_json
        """

    def _next_video_order(self, cursor: sqlite3.Cursor, field: str, folder_id: str | None = None) -> float:
        preference = self._get_library_preference(cursor, "new_video_position", "front")
        if field == "folder_order":
            if folder_id is None:
                row = cursor.execute("SELECT MIN(folder_order) AS min_order, MAX(folder_order) AS max_order FROM video_assets WHERE folder_id IS NULL").fetchone()
            else:
                row = cursor.execute(
                    "SELECT MIN(folder_order) AS min_order, MAX(folder_order) AS max_order FROM video_folder_memberships WHERE folder_id = ?",
                    (folder_id,),
                ).fetchone()
        else:
            row = cursor.execute("SELECT MIN(global_order) AS min_order, MAX(global_order) AS max_order FROM video_assets").fetchone()
        min_order = float(row["min_order"]) if row and row["min_order"] is not None else 0
        max_order = float(row["max_order"]) if row and row["max_order"] is not None else 0
        return max_order + 1000 if preference == "back" else min_order - 1000

    def _get_library_preference(self, cursor: sqlite3.Cursor, key: str, default: str) -> str:
        row = cursor.execute("SELECT value FROM library_preferences WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def _consolidate_video_family(self, cursor: sqlite3.Cursor, canonical_id: str) -> tuple[str, str] | None:
        family, pattern = self._canonical_family_pattern(canonical_id)
        rows = cursor.execute(
            """
            SELECT video_id, canonical_id, created_at, updated_at
            FROM video_assets
            WHERE canonical_id = ? OR canonical_id LIKE ?
            ORDER BY
                CASE WHEN canonical_id = ? THEN 0 ELSE 1 END,
                updated_at DESC,
                created_at ASC
            """,
            (family, pattern, family),
        ).fetchall()
        if not rows:
            return None

        primary = rows[0]
        primary_video_id = primary["video_id"]
        created_at = primary["created_at"]

        if primary["canonical_id"] != family:
            cursor.execute(
                "UPDATE video_assets SET canonical_id = ? WHERE video_id = ?",
                (family, primary_video_id),
            )

        duplicate_ids = [row["video_id"] for row in rows[1:]]
        for duplicate_id in duplicate_ids:
            cursor.execute(
                "UPDATE tasks SET video_id = ? WHERE video_id = ?",
                (primary_video_id, duplicate_id),
            )
            cursor.execute("DELETE FROM video_assets WHERE video_id = ?", (duplicate_id,))
            cursor.execute(
                "UPDATE OR IGNORE video_folder_memberships SET video_id = ? WHERE video_id = ?",
                (primary_video_id, duplicate_id),
            )
            cursor.execute("DELETE FROM video_folder_memberships WHERE video_id = ?", (duplicate_id,))

        return primary_video_id, created_at

    def upsert_video_asset(self, asset: VideoAssetRecord) -> VideoAssetRecord:
        updated_at = datetime.now(timezone.utc).isoformat()
        created_at = asset.created_at.isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            consolidated = self._consolidate_video_family(cursor, asset.canonical_id)
            existing = cursor.execute(
                "SELECT video_id, created_at FROM video_assets WHERE canonical_id = ?",
                (asset.canonical_id,),
            ).fetchone()
            if existing is not None:
                video_id = existing["video_id"]
                created = existing["created_at"]
            elif consolidated is not None:
                video_id, created = consolidated
            else:
                video_id = asset.video_id
                created = created_at
            existing_detail = cursor.execute(
                """
                SELECT folder_id, global_order, folder_order, global_pinned, folder_pinned
                FROM video_assets
                WHERE video_id = ?
                """,
                (video_id,),
            ).fetchone()
            global_order = (
                float(existing_detail["global_order"])
                if existing_detail is not None and existing_detail["global_order"] is not None
                else self._next_video_order(cursor, "global_order")
            )
            folder_order = (
                float(existing_detail["folder_order"])
                if existing_detail is not None and existing_detail["folder_order"] is not None
                else self._next_video_order(cursor, "folder_order", asset.folder_id)
            )
            folder_id = existing_detail["folder_id"] if existing_detail is not None else asset.folder_id
            global_pinned = bool(existing_detail["global_pinned"]) if existing_detail is not None else asset.global_pinned
            folder_pinned = bool(existing_detail["folder_pinned"]) if existing_detail is not None else asset.folder_pinned

            cursor.execute(
                """
                INSERT INTO video_assets (
                    video_id, canonical_id, platform, title, source_url, cover_url, duration, page_catalog_json,
                    latest_task_id, latest_status, latest_stage, latest_error_message, is_favorite, favorite_updated_at,
                    folder_id, global_order, folder_order, global_pinned, folder_pinned,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    title = excluded.title,
                    source_url = excluded.source_url,
                    cover_url = excluded.cover_url,
                    duration = excluded.duration,
                    page_catalog_json = excluded.page_catalog_json,
                    updated_at = excluded.updated_at
                """,
                (
                    video_id,
                    asset.canonical_id,
                    asset.platform,
                    asset.title,
                    asset.source_url,
                    asset.cover_url,
                    asset.duration,
                    json.dumps([page.model_dump(mode="json") for page in asset.pages], ensure_ascii=False),
                    asset.latest_task_id,
                    asset.latest_status.value if asset.latest_status else None,
                    asset.latest_stage,
                    asset.latest_error_message,
                    1 if asset.is_favorite else 0,
                    asset.favorite_updated_at.isoformat() if asset.favorite_updated_at else None,
                    folder_id,
                    global_order,
                    folder_order,
                    1 if global_pinned else 0,
                    1 if folder_pinned else 0,
                    created,
                    updated_at,
                ),
            )
            if folder_id is not None:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO video_folder_memberships (video_id, folder_id, folder_order, folder_pinned, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (video_id, folder_id, folder_order, 1 if folder_pinned else 0, created, updated_at),
                )
        refreshed = self.get_video_asset(video_id)
        assert refreshed is not None
        return refreshed

    def get_video_asset(self, video_id: str) -> VideoAssetRecord | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT
                    {columns}
                FROM video_assets v
                LEFT JOIN task_results r ON r.task_id = v.latest_task_id
                WHERE v.video_id = ?
                """.format(columns=self._select_video_columns("v")),
                (video_id,),
            ).fetchone()
        return self._row_to_video_asset(row) if row is not None else None

    def get_video_asset_by_canonical_id(self, canonical_id: str) -> VideoAssetRecord | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT
                    {columns}
                FROM video_assets v
                LEFT JOIN task_results r ON r.task_id = v.latest_task_id
                WHERE v.canonical_id = ?
                """.format(columns=self._select_video_columns("v")),
                (canonical_id,),
            ).fetchone()
        return self._row_to_video_asset(row) if row is not None else None

    def list_video_assets(self) -> list[VideoAssetRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT
                    {columns}
                FROM video_assets v
                LEFT JOIN task_results r ON r.task_id = v.latest_task_id
                ORDER BY v.global_pinned DESC, v.global_order ASC, v.updated_at DESC
                """.format(columns=self._select_video_columns("v"))
            ).fetchall()
        videos = [self._row_to_video_asset(row) for row in rows]
        grouped: dict[str, VideoAssetRecord] = {}
        for video in videos:
            family, _ = self._canonical_family_pattern(video.canonical_id)
            current = grouped.get(family)
            if current is None:
                grouped[family] = video
                continue
            current_updated = current.updated_at.timestamp()
            video_updated = video.updated_at.timestamp()
            if video_updated > current_updated or ("?p=" in current.canonical_id and "?p=" not in video.canonical_id):
                grouped[family] = video
        return list(grouped.values())

    def create_task(
        self,
        task_input: TaskInput,
        video_id: str | None = None,
        *,
        page_number: int | None = None,
        page_title: str | None = None,
    ) -> TaskRecord:
        record = TaskRecord(
            task_input=task_input,
            video_id=video_id,
            page_number=page_number,
            page_title=page_title,
        )
        payload = self._serialize_record(record)
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, video_id, status, task_input_json, page_number, page_title, result_json, error_code,
                    error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["task_id"],
                    payload["video_id"],
                    payload["status"],
                    payload["task_input_json"],
                    payload["page_number"],
                    payload["page_title"],
                    payload["result_json"],
                    payload["error_code"],
                    payload["error_message"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            if video_id:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_task_id = ?, latest_status = ?, latest_stage = ?, latest_error_message = NULL, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (record.task_id, record.status.value, "queued", payload["updated_at"], video_id),
                )
        return record

    def list_tasks(self) -> list[TaskRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT
                    t.task_id, t.video_id, t.status, t.task_input_json, t.page_number, t.page_title,
                    r.result_json, t.error_code,
                    t.error_message, t.created_at, t.updated_at
                FROM tasks t
                LEFT JOIN task_results r ON r.task_id = t.task_id
                ORDER BY t.created_at DESC
                """
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_recoverable_tasks(self) -> list[TaskRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT
                    t.task_id, t.video_id, t.status, t.task_input_json, t.page_number, t.page_title,
                    NULL AS result_json, t.error_code,
                    t.error_message, t.created_at, t.updated_at
                FROM tasks t
                WHERE t.status IN (?, ?)
                ORDER BY t.created_at ASC, t.task_id ASC
                """,
                (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_tasks_for_video(self, video_id: str) -> list[TaskRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            asset_row = cursor.execute(
                "SELECT canonical_id FROM video_assets WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if asset_row is None:
                return []
            family, pattern = self._canonical_family_pattern(asset_row["canonical_id"])
            rows = cursor.execute(
                """
                SELECT
                    t.task_id, t.video_id, t.status, t.task_input_json, t.page_number, t.page_title,
                    r.result_json, t.error_code,
                    t.error_message, t.created_at, t.updated_at
                FROM tasks t
                JOIN video_assets v ON v.video_id = t.video_id
                LEFT JOIN task_results r ON r.task_id = t.task_id
                WHERE v.canonical_id = ? OR v.canonical_id LIKE ?
                ORDER BY t.created_at DESC
                """,
                (family, pattern),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT
                    t.task_id, t.video_id, t.status, t.task_input_json, t.page_number, t.page_title,
                    r.result_json, t.error_code,
                    t.error_message, t.created_at, t.updated_at
                FROM tasks t
                LEFT JOIN task_results r ON r.task_id = t.task_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def delete_task(self, task_id: str) -> bool:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            video_id = row["video_id"]
            cursor.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            cursor.execute("DELETE FROM task_results WHERE task_id = ?", (task_id,))
            cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            if video_id:
                latest = cursor.execute(
                    """
                    SELECT task_id, status, error_message, updated_at
                    FROM tasks
                    WHERE video_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (video_id,),
                ).fetchone()
                if latest is None:
                    cursor.execute(
                        """
                        UPDATE video_assets
                        SET latest_task_id = NULL, latest_status = NULL, latest_stage = NULL,
                            latest_error_message = NULL, updated_at = ?
                        WHERE video_id = ?
                        """,
                        (datetime.now(timezone.utc).isoformat(), video_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE video_assets
                        SET latest_task_id = ?, latest_status = ?, latest_error_message = ?, updated_at = ?
                        WHERE video_id = ?
                        """,
                        (
                            latest["task_id"],
                            latest["status"],
                            latest["error_message"],
                            latest["updated_at"],
                            video_id,
                        ),
                    )
        return True

    def delete_video_asset(self, video_id: str) -> bool:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            video_row = cursor.execute(
                "SELECT canonical_id, cover_url FROM video_assets WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if video_row is None:
                return False

            family, pattern = self._canonical_family_pattern(video_row["canonical_id"])
            family_rows = cursor.execute(
                "SELECT video_id FROM video_assets WHERE canonical_id = ? OR canonical_id LIKE ?",
                (family, pattern),
            ).fetchall()
            video_ids = [row["video_id"] for row in family_rows]

            placeholders = ",".join("?" for _ in video_ids)
            task_rows = cursor.execute(
                f"SELECT task_id FROM tasks WHERE video_id IN ({placeholders})",
                tuple(video_ids),
            ).fetchall()
            task_ids = [row["task_id"] for row in task_rows]

            for task_id in task_ids:
                cursor.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
                cursor.execute("DELETE FROM task_results WHERE task_id = ?", (task_id,))
            cursor.execute(f"DELETE FROM video_folder_memberships WHERE video_id IN ({placeholders})", tuple(video_ids))
            cursor.execute(f"DELETE FROM video_tags WHERE video_id IN ({placeholders})", tuple(video_ids))
            cursor.execute(f"DELETE FROM knowledge_index WHERE video_id IN ({placeholders})", tuple(video_ids))
            cursor.execute(f"DELETE FROM tasks WHERE video_id IN ({placeholders})", tuple(video_ids))
            cursor.execute(f"DELETE FROM video_assets WHERE video_id IN ({placeholders})", tuple(video_ids))
        return True

    def get_library_preferences(self) -> VideoLibraryPreferencesResponse:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            value = self._get_library_preference(cursor, "new_video_position", "front")
        return VideoLibraryPreferencesResponse(new_video_position="back" if value == "back" else "front")

    def update_library_preferences(self, *, new_video_position: str) -> VideoLibraryPreferencesResponse:
        normalized = "back" if new_video_position == "back" else "front"
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                INSERT INTO library_preferences (key, value, updated_at)
                VALUES ('new_video_position', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (normalized, updated_at),
            )
        return VideoLibraryPreferencesResponse(new_video_position=normalized)

    def list_video_folders(self) -> list[VideoFolderResponse]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT folder_id, parent_id, name, position, created_at, updated_at
                FROM video_folders
                ORDER BY parent_id IS NOT NULL, parent_id, position ASC, updated_at ASC
                """
            ).fetchall()
        return [self._row_to_video_folder(row) for row in rows]

    def create_video_folder(self, name: str, parent_id: str | None = None) -> VideoFolderResponse | None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if parent_id is not None and not self._folder_exists(cursor, parent_id):
                return None
            row = cursor.execute(
                "SELECT MIN(position) AS min_position FROM video_folders WHERE parent_id IS ?",
                (parent_id,),
            ).fetchone()
            min_position = float(row["min_position"]) if row and row["min_position"] is not None else 0
            folder_id = uuid4().hex
            cursor.execute(
                """
                INSERT INTO video_folders (folder_id, parent_id, name, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (folder_id, parent_id, normalized_name, min_position - 1000, now, now),
            )
        folder = self.get_video_folder(folder_id)
        assert folder is not None
        return folder

    def get_video_folder(self, folder_id: str) -> VideoFolderResponse | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT folder_id, parent_id, name, position, created_at, updated_at
                FROM video_folders
                WHERE folder_id = ?
                """,
                (folder_id,),
            ).fetchone()
        return self._row_to_video_folder(row) if row is not None else None

    def update_video_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        parent_id: str | None | object = _UNSET,
        position: float | None = None,
    ) -> VideoFolderResponse | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            current = cursor.execute(
                "SELECT folder_id, parent_id, name, position FROM video_folders WHERE folder_id = ?",
                (folder_id,),
            ).fetchone()
            if current is None:
                return None
            next_parent_id = parent_id if parent_id is not _UNSET else current["parent_id"]
            if next_parent_id == folder_id:
                raise ValueError("Folder cannot be moved into itself.")
            if next_parent_id is not None:
                if not self._folder_exists(cursor, next_parent_id):
                    return None
                if next_parent_id in self._descendant_folder_ids(cursor, folder_id):
                    raise ValueError("Folder cannot be moved into its descendant.")
            next_name = str(name).strip() if name is not None else current["name"]
            if not next_name:
                next_name = current["name"]
            next_position = float(position) if position is not None else float(current["position"] or 0)
            cursor.execute(
                """
                UPDATE video_folders
                SET name = ?, parent_id = ?, position = ?, updated_at = ?
                WHERE folder_id = ?
                """,
                (next_name, next_parent_id, next_position, updated_at, folder_id),
            )
        return self.get_video_folder(folder_id)

    def delete_video_folder(self, folder_id: str) -> bool:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if not self._folder_exists(cursor, folder_id):
                return False
            folder_ids = [folder_id, *self._descendant_folder_ids(cursor, folder_id)]
            placeholders = ",".join("?" for _ in folder_ids)
            cursor.execute(
                f"""
                UPDATE video_assets
                SET folder_id = NULL, folder_order = ?, folder_pinned = 0, updated_at = ?
                WHERE folder_id IN ({placeholders})
                """,
                (self._next_video_order(cursor, "folder_order", None), datetime.now(timezone.utc).isoformat(), *folder_ids),
            )
            cursor.execute(f"DELETE FROM video_folder_memberships WHERE folder_id IN ({placeholders})", tuple(folder_ids))
            cursor.execute(f"DELETE FROM video_folders WHERE folder_id IN ({placeholders})", tuple(folder_ids))
        return True

    def move_video_to_folder(self, video_id: str, folder_id: str | None) -> VideoAssetRecord | None:
        return self.set_video_folders(video_id, [] if folder_id is None else [folder_id])

    def add_video_to_folder(self, video_id: str, folder_id: str) -> VideoAssetRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            if not self._folder_exists(cursor, folder_id):
                return None
            order = self._next_video_order(cursor, "folder_order", folder_id)
            cursor.execute(
                """
                INSERT INTO video_folder_memberships (video_id, folder_id, folder_order, folder_pinned, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(video_id, folder_id) DO NOTHING
                """,
                (video_id, folder_id, order, updated_at, updated_at),
            )
            primary = cursor.execute(
                "SELECT folder_id FROM video_assets WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if primary is not None and primary["folder_id"] is None:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET folder_id = ?, folder_order = ?, folder_pinned = 0, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (folder_id, order, updated_at, video_id),
                )
            else:
                cursor.execute("UPDATE video_assets SET updated_at = ? WHERE video_id = ?", (updated_at, video_id))
        return self.get_video_asset(video_id)

    def set_video_folders(self, video_id: str, folder_ids: list[str]) -> VideoAssetRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        normalized_folder_ids = [str(folder_id).strip() for folder_id in folder_ids if str(folder_id).strip()]
        seen: set[str] = set()
        unique_folder_ids = [folder_id for folder_id in normalized_folder_ids if not (folder_id in seen or seen.add(folder_id))]
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            if any(not self._folder_exists(cursor, folder_id) for folder_id in unique_folder_ids):
                return None
            existing_rows = cursor.execute(
                "SELECT folder_id, folder_order, folder_pinned, created_at FROM video_folder_memberships WHERE video_id = ?",
                (video_id,),
            ).fetchall()
            existing = {row["folder_id"]: row for row in existing_rows}
            cursor.execute("DELETE FROM video_folder_memberships WHERE video_id = ?", (video_id,))
            for folder_id in unique_folder_ids:
                existing_row = existing.get(folder_id)
                folder_order = (
                    float(existing_row["folder_order"])
                    if existing_row is not None and existing_row["folder_order"] is not None
                    else self._next_video_order(cursor, "folder_order", folder_id)
                )
                folder_pinned = bool(existing_row["folder_pinned"]) if existing_row is not None else False
                created_at = existing_row["created_at"] if existing_row is not None else updated_at
                cursor.execute(
                    """
                    INSERT INTO video_folder_memberships (video_id, folder_id, folder_order, folder_pinned, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (video_id, folder_id, folder_order, 1 if folder_pinned else 0, created_at, updated_at),
                )
            primary_folder_id = unique_folder_ids[0] if unique_folder_ids else None
            primary_order = (
                float(existing[primary_folder_id]["folder_order"])
                if primary_folder_id is not None and primary_folder_id in existing and existing[primary_folder_id]["folder_order"] is not None
                else self._next_video_order(cursor, "folder_order", primary_folder_id)
            )
            primary_pinned = (
                bool(existing[primary_folder_id]["folder_pinned"])
                if primary_folder_id is not None and primary_folder_id in existing
                else False
            )
            cursor.execute(
                """
                UPDATE video_assets
                SET folder_id = ?, folder_order = ?, folder_pinned = 0, updated_at = ?
                WHERE video_id = ?
                """,
                (primary_folder_id, primary_order, updated_at, video_id),
            )
            if primary_pinned:
                cursor.execute("UPDATE video_assets SET folder_pinned = 1 WHERE video_id = ?", (video_id,))
        return self.get_video_asset(video_id)

    def set_video_pin(
        self,
        video_id: str,
        *,
        global_pinned: bool | None = None,
        folder_pinned: bool | None = None,
    ) -> VideoAssetRecord | None:
        updates: list[str] = []
        values: list[object] = []
        if global_pinned is not None:
            updates.append("global_pinned = ?")
            values.append(1 if global_pinned else 0)
        if folder_pinned is not None:
            updates.append("folder_pinned = ?")
            values.append(1 if folder_pinned else 0)
        if not updates:
            return self.get_video_asset(video_id)
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(video_id)
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            cursor.execute(f"UPDATE video_assets SET {', '.join(updates)} WHERE video_id = ?", tuple(values))
            if folder_pinned is not None:
                cursor.execute(
                    """
                    UPDATE video_folder_memberships
                    SET folder_pinned = ?, updated_at = ?
                    WHERE video_id = ? AND folder_id = (
                        SELECT folder_id FROM video_assets WHERE video_id = ?
                    )
                    """,
                    (1 if folder_pinned else 0, values[-2], video_id, video_id),
                )
        return self.get_video_asset(video_id)

    def reorder_videos(self, video_ids: list[str], folder_id: str | None = None) -> list[VideoAssetRecord]:
        ordered_ids = [str(video_id) for video_id in video_ids if str(video_id).strip()]
        if not ordered_ids:
            return []
        field = "global_order" if folder_id == "__global__" else "folder_order"
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if folder_id not in (None, "__global__") and not self._folder_exists(cursor, folder_id):
                return []
            valid_ids = self._video_ids_in_scope(cursor, folder_id)
            if any(video_id not in valid_ids for video_id in ordered_ids):
                raise ValueError("Video reorder payload contains videos outside the target scope.")
            for index, video_id in enumerate(ordered_ids):
                if field == "global_order":
                    cursor.execute(
                        "UPDATE video_assets SET global_order = ?, updated_at = ? WHERE video_id = ?",
                        ((index + 1) * 1000, updated_at, video_id),
                    )
                elif folder_id is None:
                    cursor.execute(
                        "UPDATE video_assets SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id IS NULL",
                        ((index + 1) * 1000, updated_at, video_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE video_folder_memberships SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id = ?",
                        ((index + 1) * 1000, updated_at, video_id, folder_id),
                    )
                    cursor.execute(
                        "UPDATE video_assets SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id = ?",
                        ((index + 1) * 1000, updated_at, video_id, folder_id),
                    )
        return [video for video_id in ordered_ids if (video := self.get_video_asset(video_id)) is not None]

    def add_video_tag(self, video_id: str, tag: str, source: str = "manual", confidence: float = 1.0) -> bool:
        normalized_tag = str(tag or "").strip()
        if not normalized_tag:
            return False

        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            video = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if video is None:
                return False
            cursor.execute(
                """
                INSERT INTO video_tags (video_id, tag, source, confidence, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_id, tag) DO UPDATE SET
                    source = CASE
                        WHEN excluded.source = 'manual' THEN 'manual'
                        ELSE video_tags.source
                    END,
                    confidence = CASE
                        WHEN excluded.source = 'manual' THEN 1.0
                        ELSE excluded.confidence
                    END,
                    created_at = CASE
                        WHEN excluded.source = 'manual' THEN excluded.created_at
                        ELSE video_tags.created_at
                    END
                """,
                (video_id, normalized_tag, source, float(confidence), created_at),
            )
        return True

    def remove_video_tag(self, video_id: str, tag: str) -> bool:
        normalized_tag = str(tag or "").strip()
        if not normalized_tag:
            return False
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute("DELETE FROM video_tags WHERE video_id = ? AND tag = ?", (video_id, normalized_tag))
            return cursor.rowcount > 0

    def list_video_tags(self, video_id: str) -> list[VideoTagRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT video_id, tag, source, confidence, created_at
                FROM video_tags
                WHERE video_id = ?
                ORDER BY source = 'manual' DESC, confidence DESC, tag COLLATE NOCASE ASC
                """,
                (video_id,),
            ).fetchall()
        return [
            VideoTagRecord(
                video_id=row["video_id"],
                tag=row["tag"],
                source=row["source"],
                confidence=float(row["confidence"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_all_tags(self) -> list[dict[str, object]]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT tag, COUNT(*) AS count
                FROM video_tags
                GROUP BY tag
                ORDER BY count DESC, tag COLLATE NOCASE ASC
                """
            ).fetchall()
            videos_by_tag_rows = cursor.execute(
                """
                SELECT tag, video_id
                FROM video_tags
                ORDER BY tag COLLATE NOCASE ASC, video_id ASC
                """
            ).fetchall()
        videos_by_tag: dict[str, list[str]] = {}
        for row in videos_by_tag_rows:
            videos_by_tag.setdefault(row["tag"], []).append(row["video_id"])
        return [
            {"tag": row["tag"], "count": int(row["count"]), "videos": videos_by_tag.get(row["tag"], [])}
            for row in rows
        ]

    def list_all_video_tags(self) -> list[VideoTagRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT video_id, tag, source, confidence, created_at
                FROM video_tags
                ORDER BY tag COLLATE NOCASE ASC, confidence DESC, video_id ASC
                """
            ).fetchall()
        return [
            VideoTagRecord(
                video_id=row["video_id"],
                tag=row["tag"],
                source=row["source"],
                confidence=float(row["confidence"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_untagged_video_ids(self) -> list[str]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT v.video_id
                FROM video_assets v
                LEFT JOIN video_tags t ON t.video_id = v.video_id
                WHERE t.video_id IS NULL
                ORDER BY v.updated_at DESC
                """
            ).fetchall()
        return [row["video_id"] for row in rows]

    def replace_knowledge_chunks(self, video_id: str, chunks: list[KnowledgeIndexChunkRecord]) -> int:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute("DELETE FROM knowledge_index WHERE video_id = ?", (video_id,))
            for chunk in chunks:
                cursor.execute(
                    """
                    INSERT INTO knowledge_index (
                        chunk_id, video_id, embedding_json, indexed_content, index_type,
                        segment_order, anchor_label, anchor_seconds, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.video_id,
                        chunk.embedding_json,
                        chunk.indexed_content,
                        chunk.index_type,
                        chunk.segment_order,
                        chunk.anchor_label,
                        chunk.anchor_seconds,
                        chunk.created_at.isoformat(),
                        chunk.updated_at.isoformat(),
                    ),
                )
        return len(chunks)

    def delete_knowledge_chunks(self, video_id: str) -> bool:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute("DELETE FROM knowledge_index WHERE video_id = ?", (video_id,))
            return cursor.rowcount > 0

    def clear_knowledge_chunks(self) -> None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute("DELETE FROM knowledge_index")

    def list_knowledge_chunks(self, video_id: str | None = None) -> list[KnowledgeIndexChunkRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if video_id:
                rows = cursor.execute(
                    """
                    SELECT
                        chunk_id, video_id, embedding_json, indexed_content, index_type,
                        segment_order, anchor_label, anchor_seconds, created_at, updated_at
                    FROM knowledge_index
                    WHERE video_id = ?
                    ORDER BY segment_order ASC, updated_at DESC
                    """,
                    (video_id,),
                ).fetchall()
            else:
                rows = cursor.execute(
                    """
                    SELECT
                        chunk_id, video_id, embedding_json, indexed_content, index_type,
                        segment_order, anchor_label, anchor_seconds, created_at, updated_at
                    FROM knowledge_index
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [
            KnowledgeIndexChunkRecord(
                chunk_id=row["chunk_id"],
                video_id=row["video_id"],
                embedding_json=row["embedding_json"],
                indexed_content=row["indexed_content"],
                index_type=row["index_type"],
                segment_order=row["segment_order"],
                anchor_label=row["anchor_label"],
                anchor_seconds=row["anchor_seconds"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def get_knowledge_chunk_count(self) -> int:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT COUNT(*) AS count FROM knowledge_index").fetchone()
        return int(row["count"]) if row is not None else 0

    def set_video_favorite(self, video_id: str, is_favorite: bool) -> VideoAssetRecord | None:
        favorite_updated_at = datetime.now(timezone.utc).isoformat() if is_favorite else None
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                UPDATE video_assets
                SET is_favorite = ?, favorite_updated_at = ?
                WHERE video_id = ?
                """,
                (1 if is_favorite else 0, favorite_updated_at, video_id),
            )
        return self.get_video_asset(video_id)

    def update_status(self, task_id: str, status: TaskStatus) -> TaskRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status.value, updated_at, task_id),
            )
            row = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and row["video_id"]:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_task_id = ?, latest_status = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (task_id, status.value, updated_at, row["video_id"]),
                )
        return self.get_task(task_id)

    def save_result(self, task_id: str, result: TaskResult) -> TaskRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                INSERT INTO task_results (task_id, result_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET result_json = excluded.result_json, updated_at = excluded.updated_at
                """,
                (task_id, payload, updated_at),
            )
            cursor.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (updated_at, task_id))
            row = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and row["video_id"]:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_task_id = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (task_id, updated_at, row["video_id"]),
                )
        return self.get_task(task_id)

    def update_error(self, task_id: str, error_code: str, error_message: str) -> TaskRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                UPDATE tasks SET error_code = ?, error_message = ?, updated_at = ? WHERE task_id = ?
                """,
                (error_code, error_message, updated_at, task_id),
            )
            row = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and row["video_id"]:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_task_id = ?, latest_error_message = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (task_id, error_message, updated_at, row["video_id"]),
                )
        return self.get_task(task_id)

    def append_event(
        self,
        task_id: str,
        stage: str,
        progress: int,
        message: str,
        payload: dict[str, object] | None = None,
    ) -> TaskEventRecord:
        event = TaskEventRecord(task_id=task_id, stage=stage, progress=progress, message=message, payload=payload or {})
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            latest_row = cursor.execute(
                """
                SELECT created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if latest_row is not None:
                latest_created_at = datetime.fromisoformat(latest_row["created_at"])
                if event.created_at <= latest_created_at:
                    event.created_at = latest_created_at + timedelta(microseconds=1)
            cursor.execute(
                """
                INSERT INTO task_events (event_id, task_id, stage, progress, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.stage,
                    event.progress,
                    event.message,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )
            row = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and row["video_id"]:
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_stage = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (stage, updated_at, row["video_id"]),
                )
        return event

    def list_events(self, task_id: str) -> list[TaskEventRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT event_id, task_id, stage, progress, message, payload_json, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_events_after(self, task_id: str, after_created_at: str | None) -> list[TaskEventRecord]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if after_created_at:
                rows = cursor.execute(
                    """
                    SELECT event_id, task_id, stage, progress, message, payload_json, created_at
                    FROM task_events
                    WHERE task_id = ? AND created_at > ?
                    ORDER BY created_at ASC
                    """,
                    (task_id, after_created_at),
                ).fetchall()
            else:
                rows = cursor.execute(
                    """
                    SELECT event_id, task_id, stage, progress, message, payload_json, created_at
                    FROM task_events
                    WHERE task_id = ?
                    ORDER BY created_at ASC
                    """,
                    (task_id,),
                ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_latest_event(self, task_id: str) -> TaskEventRecord | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT event_id, task_id, stage, progress, message, payload_json, created_at
                FROM task_events WHERE task_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def _serialize_record(self, record: TaskRecord) -> dict[str, str | None]:
        return {
            "task_id": record.task_id,
            "video_id": record.video_id,
            "status": record.status.value,
            "task_input_json": json.dumps(record.task_input.model_dump(mode="json"), ensure_ascii=False),
            "page_number": str(record.page_number) if record.page_number is not None else None,
            "page_title": record.page_title,
            "result_json": (
                json.dumps(record.result.model_dump(mode="json"), ensure_ascii=False)
                if record.result is not None
                else None
            ),
            "error_code": record.error_code,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        task_input = TaskInput.model_validate(json.loads(row["task_input_json"]))
        result = TaskResult.model_validate(json.loads(row["result_json"])) if row["result_json"] else None
        return TaskRecord(
            task_id=row["task_id"],
            video_id=row["video_id"],
            status=TaskStatus(row["status"]),
            task_input=task_input,
            page_number=row["page_number"],
            page_title=row["page_title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result=result,
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def _row_to_video_asset(self, row: sqlite3.Row) -> VideoAssetRecord:
        latest_result = (
            TaskResult.model_validate(json.loads(row["latest_result_json"]))
            if row["latest_result_json"]
            else None
        )
        return VideoAssetRecord(
            video_id=row["video_id"],
            canonical_id=row["canonical_id"],
            platform=row["platform"],
            title=row["title"],
            source_url=row["source_url"],
            cover_url=row["cover_url"] or "",
            duration=row["duration"],
            pages=[
                VideoPageOptionResponse.model_validate(item)
                for item in json.loads(row["page_catalog_json"] or "[]")
            ],
            latest_task_id=row["latest_task_id"],
            latest_status=TaskStatus(row["latest_status"]) if row["latest_status"] else None,
            latest_stage=row["latest_stage"],
            latest_result=latest_result,
            latest_error_message=row["latest_error_message"],
            is_favorite=bool(row["is_favorite"]),
            favorite_updated_at=datetime.fromisoformat(row["favorite_updated_at"]) if row["favorite_updated_at"] else None,
            folder_id=row["folder_id"],
            folder_ids=self._folder_ids_for_video(row["video_id"]),
            global_order=float(row["global_order"] or 0),
            folder_order=float(row["folder_order"] or 0),
            global_pinned=bool(row["global_pinned"]),
            folder_pinned=bool(row["folder_pinned"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_video_folder(self, row: sqlite3.Row) -> VideoFolderResponse:
        return VideoFolderResponse(
            folder_id=row["folder_id"],
            parent_id=row["parent_id"],
            name=row["name"],
            position=float(row["position"] or 0),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _folder_exists(self, cursor: sqlite3.Cursor, folder_id: str) -> bool:
        return cursor.execute("SELECT folder_id FROM video_folders WHERE folder_id = ?", (folder_id,)).fetchone() is not None

    def _folder_ids_for_video(self, video_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT folder_id
            FROM video_folder_memberships
            WHERE video_id = ?
            ORDER BY created_at ASC, folder_order ASC
            """,
            (video_id,),
        ).fetchall()
        return [str(row["folder_id"]) for row in rows]

    def _video_ids_in_scope(self, cursor: sqlite3.Cursor, folder_id: str | None) -> set[str]:
        if folder_id == "__global__":
            rows = cursor.execute("SELECT video_id FROM video_assets").fetchall()
        elif folder_id is None:
            rows = cursor.execute(
                """
                SELECT v.video_id
                FROM video_assets v
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM video_folder_memberships m
                    WHERE m.video_id = v.video_id
                )
                """
            ).fetchall()
        else:
            rows = cursor.execute("SELECT video_id FROM video_folder_memberships WHERE folder_id = ?", (folder_id,)).fetchall()
        return {str(row["video_id"]) for row in rows}

    def _descendant_folder_ids(self, cursor: sqlite3.Cursor, folder_id: str) -> list[str]:
        descendants: list[str] = []
        pending = [folder_id]
        while pending:
            current = pending.pop(0)
            rows = cursor.execute("SELECT folder_id FROM video_folders WHERE parent_id = ?", (current,)).fetchall()
            child_ids = [row["folder_id"] for row in rows]
            descendants.extend(child_ids)
            pending.extend(child_ids)
        return descendants

    def _row_to_event(self, row: sqlite3.Row) -> TaskEventRecord:
        return TaskEventRecord(
            event_id=row["event_id"],
            task_id=row["task_id"],
            stage=row["stage"],
            progress=row["progress"],
            message=row["message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            payload=json.loads(row["payload_json"]),
        )
