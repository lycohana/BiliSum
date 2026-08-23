import json
import sqlite3
from collections.abc import Callable
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
    VideoCollectionItemResponse,
    VideoCollectionResponse,
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
                    latest_task_created_at TEXT,
                    latest_task_completed_at TEXT,
                    latest_task_duration_seconds REAL,
                    last_summary_at TEXT,
                    latest_error_message TEXT,
                    is_favorite INTEGER NOT NULL DEFAULT 0,
                    favorite_updated_at TEXT,
                    folder_id TEXT,
                    global_order REAL NOT NULL DEFAULT 0,
                    folder_order REAL NOT NULL DEFAULT 0,
                    global_pinned INTEGER NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    is_global_visible INTEGER NOT NULL DEFAULT 1,
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
            self._ensure_column(cursor, "video_assets", "latest_task_created_at", "TEXT")
            self._ensure_column(cursor, "video_assets", "latest_task_completed_at", "TEXT")
            self._ensure_column(cursor, "video_assets", "latest_task_duration_seconds", "REAL")
            self._ensure_column(cursor, "video_assets", "last_summary_at", "TEXT")
            self._ensure_column(cursor, "video_assets", "is_global_visible", "INTEGER NOT NULL DEFAULT 1")
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
                CREATE TABLE IF NOT EXISTS video_collections (
                    collection_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT 'bilibili',
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    cover_url TEXT,
                    auto_check_on_open INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TEXT,
                    pending_items_json TEXT NOT NULL DEFAULT '[]',
                    visibility_initialized INTEGER NOT NULL DEFAULT 0,
                    global_order REAL NOT NULL DEFAULT 0,
                    owner_mid TEXT,
                    owner_name TEXT,
                    owner_face TEXT,
                    owner_url TEXT,
                    view_sort_mode TEXT NOT NULL DEFAULT 'source',
                    view_group_mode TEXT NOT NULL DEFAULT 'none',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(cursor, "video_collections", "visibility_initialized", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collections", "global_order", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collections", "owner_mid", "TEXT")
            self._ensure_column(cursor, "video_collections", "owner_name", "TEXT")
            self._ensure_column(cursor, "video_collections", "owner_face", "TEXT")
            self._ensure_column(cursor, "video_collections", "owner_url", "TEXT")
            self._ensure_column(cursor, "video_collections", "is_favorite", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collections", "favorite_updated_at", "TEXT")
            self._ensure_column(cursor, "video_collections", "global_pinned", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collections", "folder_id", "TEXT")
            self._ensure_column(cursor, "video_collections", "folder_order", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collections", "folder_pinned", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(
                cursor,
                "video_collections",
                "view_sort_mode",
                "TEXT NOT NULL DEFAULT 'source'",
            )
            self._ensure_column(
                cursor,
                "video_collections",
                "view_group_mode",
                "TEXT NOT NULL DEFAULT 'none'",
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_collection_folder_memberships (
                    collection_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    folder_order REAL NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (collection_id, folder_id),
                    FOREIGN KEY(collection_id) REFERENCES video_collections(collection_id),
                    FOREIGN KEY(folder_id) REFERENCES video_folders(folder_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_collection_items (
                    collection_id TEXT NOT NULL,
                    bvid TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    cover_url TEXT,
                    duration REAL,
                    video_id TEXT,
                    is_promoted INTEGER NOT NULL DEFAULT 0,
                    custom_position REAL,
                    source_section_id TEXT,
                    source_section_title TEXT,
                    source_section_position INTEGER,
                    source_present INTEGER NOT NULL DEFAULT 1,
                    removed_from_source_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (collection_id, bvid),
                    FOREIGN KEY(collection_id) REFERENCES video_collections(collection_id)
                )
                """
            )
            self._ensure_column(cursor, "video_collection_items", "is_promoted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "video_collection_items", "custom_position", "REAL")
            self._ensure_column(cursor, "video_collection_items", "source_section_id", "TEXT")
            self._ensure_column(cursor, "video_collection_items", "source_section_title", "TEXT")
            self._ensure_column(
                cursor,
                "video_collection_items",
                "source_section_position",
                "INTEGER",
            )
            self._ensure_column(
                cursor,
                "video_collection_items",
                "source_present",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                cursor,
                "video_collection_items",
                "removed_from_source_at",
                "TEXT",
            )
            cursor.execute(
                """
                UPDATE video_collection_items
                SET custom_position = position
                WHERE custom_position IS NULL
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
                CREATE TABLE IF NOT EXISTS video_collection_task_batches (
                    collection_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    task_ids_json TEXT NOT NULL DEFAULT '[]',
                    dispatch_completed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (collection_id, batch_id),
                    FOREIGN KEY(collection_id) REFERENCES video_collections(collection_id)
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    reasoning TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed',
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    references_json TEXT NOT NULL DEFAULT '[]',
                    job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES knowledge_conversations(conversation_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_messages_sequence
                ON knowledge_messages(conversation_id, sequence)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_jobs (
                    job_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    assistant_message_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    query TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    agent_round INTEGER NOT NULL DEFAULT 1,
                    agent_state_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(conversation_id) REFERENCES knowledge_conversations(conversation_id),
                    FOREIGN KEY(assistant_message_id) REFERENCES knowledge_messages(message_id)
                )
                """
            )
            self._ensure_column(cursor, "knowledge_jobs", "agent_round", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(cursor, "knowledge_jobs", "agent_state_json", "TEXT NOT NULL DEFAULT '{}'")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_job_items (
                    job_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    video_id TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    PRIMARY KEY(job_id, task_id),
                    FOREIGN KEY(job_id) REFERENCES knowledge_jobs(job_id),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                )
                """
            )
            cursor.execute(
                """
                UPDATE video_assets
                SET
                    latest_task_created_at = (
                        SELECT created_at FROM tasks WHERE tasks.task_id = video_assets.latest_task_id
                    ),
                    latest_task_completed_at = CASE
                        WHEN latest_status = 'completed' THEN (
                            SELECT updated_at FROM tasks WHERE tasks.task_id = video_assets.latest_task_id
                        )
                        ELSE latest_task_completed_at
                    END,
                    latest_task_duration_seconds = CASE
                        WHEN latest_task_id IS NOT NULL THEN (
                            SELECT MAX(0, (julianday(updated_at) - julianday(created_at)) * 86400.0)
                            FROM tasks WHERE tasks.task_id = video_assets.latest_task_id
                        )
                        ELSE latest_task_duration_seconds
                    END,
                    last_summary_at = CASE
                        WHEN latest_status = 'completed' THEN (
                            SELECT updated_at FROM tasks WHERE tasks.task_id = video_assets.latest_task_id
                        )
                        ELSE last_summary_at
                    END
                WHERE latest_task_id IS NOT NULL
                """
            )

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
                    {prefix}.latest_task_id, {prefix}.latest_status, {prefix}.latest_stage,
                    {prefix}.latest_task_created_at, {prefix}.latest_task_completed_at,
                    {prefix}.latest_task_duration_seconds, {prefix}.last_summary_at,
                    {prefix}.latest_error_message,
                    {prefix}.is_favorite, {prefix}.favorite_updated_at,
                    {prefix}.folder_id, {prefix}.global_order, {prefix}.folder_order, {prefix}.global_pinned, {prefix}.folder_pinned,
                    {prefix}.is_global_visible,
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

    def _next_library_order(self, cursor: sqlite3.Cursor) -> float:
        preference = self._get_library_preference(cursor, "new_video_position", "front")
        row = cursor.execute(
            """
            SELECT MIN(order_value) AS min_order, MAX(order_value) AS max_order
            FROM (
                SELECT global_order AS order_value FROM video_assets WHERE is_global_visible = 1
                UNION ALL
                SELECT global_order AS order_value FROM video_collections
            )
            """
        ).fetchone()
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
                SELECT folder_id, global_order, folder_order, global_pinned, folder_pinned, is_global_visible
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
            is_global_visible = bool(existing_detail["is_global_visible"]) if existing_detail is not None else asset.is_global_visible

            cursor.execute(
                """
                INSERT INTO video_assets (
                    video_id, canonical_id, platform, title, source_url, cover_url, duration, page_catalog_json,
                    latest_task_id, latest_status, latest_stage, latest_task_created_at, latest_task_completed_at,
                    latest_task_duration_seconds, last_summary_at, latest_error_message, is_favorite, favorite_updated_at,
                    folder_id, global_order, folder_order, global_pinned, folder_pinned, is_global_visible,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    asset.latest_task_created_at.isoformat() if asset.latest_task_created_at else None,
                    asset.latest_task_completed_at.isoformat() if asset.latest_task_completed_at else None,
                    asset.latest_task_duration_seconds,
                    asset.last_summary_at.isoformat() if asset.last_summary_at else None,
                    asset.latest_error_message,
                    1 if asset.is_favorite else 0,
                    asset.favorite_updated_at.isoformat() if asset.favorite_updated_at else None,
                    folder_id,
                    global_order,
                    folder_order,
                    1 if global_pinned else 0,
                    1 if folder_pinned else 0,
                    1 if is_global_visible else 0,
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
                WHERE v.is_global_visible = 1
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
        return self.create_task_record(record)

    def create_task_record(self, record: TaskRecord) -> TaskRecord:
        payload = self._serialize_record(record)
        with self._lock, sqlite_cursor(self._connection) as cursor:
            self._insert_task_record(cursor, record, payload)
        return record

    def create_video_collection_task_batch(
        self,
        *,
        collection_id: str,
        batch_id: str,
        request_fingerprint: str,
        records: list[TaskRecord],
    ) -> tuple[list[TaskRecord], bool, bool]:
        """Atomically persist an idempotent collection task batch.

        Returns ``(records, created, dispatch_completed)``. Reusing the same
        batch id with a different request is rejected before any task is added.
        """

        now = datetime.now(timezone.utc).isoformat()
        task_ids: list[str]
        created = False
        dispatch_completed = False
        with self._lock, sqlite_cursor(self._connection) as cursor:
            existing = cursor.execute(
                """
                SELECT request_fingerprint, task_ids_json, dispatch_completed
                FROM video_collection_task_batches
                WHERE collection_id = ? AND batch_id = ?
                """,
                (collection_id, batch_id),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise ValueError(
                        "Batch id is already associated with a different request."
                    )
                task_ids = [
                    str(task_id)
                    for task_id in json.loads(existing["task_ids_json"] or "[]")
                ]
                dispatch_completed = bool(existing["dispatch_completed"])
            else:
                serialized = [self._serialize_record(record) for record in records]
                collection = cursor.execute(
                    "SELECT collection_id FROM video_collections WHERE collection_id = ?",
                    (collection_id,),
                ).fetchone()
                if collection is None:
                    raise ValueError("Collection not found.")
                task_ids = [record.task_id for record in records]
                cursor.execute(
                    """
                    INSERT INTO video_collection_task_batches (
                        collection_id, batch_id, request_fingerprint,
                        task_ids_json, dispatch_completed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        collection_id,
                        batch_id,
                        request_fingerprint,
                        json.dumps(task_ids, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                for record, payload in zip(records, serialized, strict=True):
                    self._insert_task_record(cursor, record, payload)
                created = True

        persisted = [self.get_task(task_id) for task_id in task_ids]
        if any(record is None for record in persisted):
            raise RuntimeError("Persisted collection task batch is incomplete.")
        return (
            [record for record in persisted if record is not None],
            created,
            dispatch_completed,
        )

    def mark_video_collection_task_batch_dispatched(
        self,
        collection_id: str,
        batch_id: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                UPDATE video_collection_task_batches
                SET dispatch_completed = 1, updated_at = ?
                WHERE collection_id = ? AND batch_id = ?
                """,
                (now, collection_id, batch_id),
            )

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

    def list_knowledge_jobs(self, statuses: list[str] | None = None, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            normalized_statuses = [status for status in (statuses or []) if status in {"queued", "running", "aggregating"}]
            bounded_limit = max(1, min(int(limit), 500))
            if normalized_statuses:
                status_values = (normalized_statuses + ["__no_match__"] * 3)[:3]
                rows = cursor.execute(
                    "SELECT job_id FROM knowledge_jobs WHERE status IN (?, ?, ?) ORDER BY updated_at ASC LIMIT ?",
                    (*status_values, bounded_limit),
                ).fetchall()
            else:
                rows = cursor.execute(
                    "SELECT job_id FROM knowledge_jobs ORDER BY updated_at ASC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        jobs: list[dict[str, object]] = []
        for row in rows:
            job = self.get_knowledge_job(str(row["job_id"]))
            if job is not None:
                jobs.append(job)
        return jobs

    def create_knowledge_conversation(self, title: str | None = None) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        conversation_id = uuid4().hex
        clean_title = str(title or "新会话").strip()[:120] or "新会话"
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_conversations (conversation_id, title, created_at, updated_at, last_message_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (conversation_id, clean_title, now, now),
            )
        return self.get_knowledge_conversation(conversation_id) or {}

    def list_knowledge_conversations(self, limit: int = 50) -> list[dict[str, object]]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                """
                SELECT c.conversation_id, c.title, c.created_at, c.updated_at, c.last_message_at,
                       COUNT(m.message_id) AS message_count,
                       COALESCE((
                           SELECT content FROM knowledge_messages pm
                           WHERE pm.conversation_id = c.conversation_id
                           ORDER BY pm.sequence DESC LIMIT 1
                       ), '') AS preview
                FROM knowledge_conversations c
                LEFT JOIN knowledge_messages m ON m.conversation_id = c.conversation_id
                GROUP BY c.conversation_id
                ORDER BY COALESCE(c.last_message_at, c.updated_at) DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [self._row_to_knowledge_conversation(row) for row in rows]

    def get_knowledge_conversation(self, conversation_id: str) -> dict[str, object] | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                """
                SELECT c.conversation_id, c.title, c.created_at, c.updated_at, c.last_message_at,
                       COUNT(m.message_id) AS message_count,
                       COALESCE((
                           SELECT content FROM knowledge_messages pm
                           WHERE pm.conversation_id = c.conversation_id
                           ORDER BY pm.sequence DESC LIMIT 1
                       ), '') AS preview
                FROM knowledge_conversations c
                LEFT JOIN knowledge_messages m ON m.conversation_id = c.conversation_id
                WHERE c.conversation_id = ?
                GROUP BY c.conversation_id
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_knowledge_conversation(row) if row is not None else None

    def update_knowledge_conversation_title(self, conversation_id: str, title: str) -> dict[str, object] | None:
        clean_title = str(title or "").strip()[:120]
        if not clean_title:
            return None
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                "UPDATE knowledge_conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
                (clean_title, updated_at, conversation_id),
            )
            if cursor.rowcount <= 0:
                return None
        return self.get_knowledge_conversation(conversation_id)

    def delete_knowledge_conversation(self, conversation_id: str) -> bool:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            exists = cursor.execute(
                "SELECT conversation_id FROM knowledge_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                return False
            job_rows = cursor.execute(
                "SELECT job_id FROM knowledge_jobs WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            for row in job_rows:
                cursor.execute("DELETE FROM knowledge_job_items WHERE job_id = ?", (row["job_id"],))
            cursor.execute("DELETE FROM knowledge_jobs WHERE conversation_id = ?", (conversation_id,))
            cursor.execute("DELETE FROM knowledge_messages WHERE conversation_id = ?", (conversation_id,))
            cursor.execute("DELETE FROM knowledge_conversations WHERE conversation_id = ?", (conversation_id,))
        return True

    def create_knowledge_message(
        self,
        conversation_id: str,
        role: str,
        content: str = "",
        *,
        status: str = "completed",
        reasoning: str = "",
        sources: list[dict[str, object]] | None = None,
        tools: list[dict[str, object]] | None = None,
        references: list[dict[str, object]] | None = None,
        job_id: str | None = None,
    ) -> dict[str, object] | None:
        now = datetime.now(timezone.utc).isoformat()
        message_id = uuid4().hex
        with self._lock, sqlite_cursor(self._connection) as cursor:
            conversation = cursor.execute(
                "SELECT conversation_id FROM knowledge_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                return None
            sequence_row = cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM knowledge_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            sequence = int(sequence_row["next_sequence"])
            cursor.execute(
                """
                INSERT INTO knowledge_messages (
                    message_id, conversation_id, sequence, role, content, reasoning, status,
                    sources_json, tools_json, references_json, job_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    sequence,
                    role,
                    content,
                    reasoning,
                    status,
                    json.dumps(sources or [], ensure_ascii=False),
                    json.dumps(tools or [], ensure_ascii=False),
                    json.dumps(references or [], ensure_ascii=False),
                    job_id,
                    now,
                    now,
                ),
            )
            cursor.execute(
                "UPDATE knowledge_conversations SET updated_at = ?, last_message_at = ? WHERE conversation_id = ?",
                (now, now, conversation_id),
            )
        return self.get_knowledge_message(message_id)

    def get_knowledge_message(self, message_id: str) -> dict[str, object] | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                "SELECT * FROM knowledge_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return self._row_to_knowledge_message(row) if row is not None else None

    def list_knowledge_messages(self, conversation_id: str) -> list[dict[str, object]]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                "SELECT * FROM knowledge_messages WHERE conversation_id = ? ORDER BY sequence ASC",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_knowledge_message(row) for row in rows]

    def update_knowledge_message(
        self,
        message_id: str,
        *,
        content: str | None = None,
        status: str | None = None,
        reasoning: str | None = None,
        sources: list[dict[str, object]] | None = None,
        tools: list[dict[str, object]] | None = None,
        job_id: str | None | object = _UNSET,
    ) -> dict[str, object] | None:
        has_update = any(value is not None for value in (content, status, reasoning, sources, tools)) or job_id is not _UNSET
        if not has_update:
            return self.get_knowledge_message(message_id)
        now = datetime.now(timezone.utc).isoformat()
        values = (
            1 if content is not None else 0,
            content,
            1 if status is not None else 0,
            status,
            1 if reasoning is not None else 0,
            reasoning,
            1 if sources is not None else 0,
            json.dumps(sources, ensure_ascii=False) if sources is not None else None,
            1 if tools is not None else 0,
            json.dumps(tools, ensure_ascii=False) if tools is not None else None,
            1 if job_id is not _UNSET else 0,
            None if job_id is _UNSET else job_id,
            now,
            message_id,
        )
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                UPDATE knowledge_messages
                SET content = CASE WHEN ? = 1 THEN ? ELSE content END,
                    status = CASE WHEN ? = 1 THEN ? ELSE status END,
                    reasoning = CASE WHEN ? = 1 THEN ? ELSE reasoning END,
                    sources_json = CASE WHEN ? = 1 THEN ? ELSE sources_json END,
                    tools_json = CASE WHEN ? = 1 THEN ? ELSE tools_json END,
                    job_id = CASE WHEN ? = 1 THEN ? ELSE job_id END,
                    updated_at = ?
                WHERE message_id = ?
                """,
                values,
            )
            row = cursor.execute(
                "SELECT conversation_id FROM knowledge_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is not None:
                cursor.execute(
                    "UPDATE knowledge_conversations SET updated_at = ?, last_message_at = ? WHERE conversation_id = ?",
                    (now, now, row["conversation_id"]),
                )
        return self.get_knowledge_message(message_id)

    def create_knowledge_job(
        self,
        conversation_id: str,
        assistant_message_id: str,
        kind: str,
        status: str,
        query: str,
        agent_round: int = 1,
        agent_state: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        now = datetime.now(timezone.utc).isoformat()
        job_id = uuid4().hex
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                INSERT INTO knowledge_jobs (
                    job_id, conversation_id, assistant_message_id, kind, status, query,
                    error_message, created_at, updated_at, completed_at, agent_round, agent_state_json
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    conversation_id,
                    assistant_message_id,
                    kind,
                    status,
                    query,
                    now,
                    now,
                    int(agent_round),
                    json.dumps(agent_state or {}, ensure_ascii=False),
                ),
            )
        return self.get_knowledge_job(job_id)

    def add_knowledge_job_items(self, job_id: str, items: list[tuple[str, str | None, int]]) -> None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.executemany(
                "INSERT OR IGNORE INTO knowledge_job_items (job_id, task_id, video_id, position) VALUES (?, ?, ?, ?)",
                [(job_id, task_id, video_id, position) for task_id, video_id, position in items],
            )

    def get_knowledge_job(self, job_id: str) -> dict[str, object] | None:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            job = cursor.execute("SELECT * FROM knowledge_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                return None
            items = cursor.execute(
                """
                SELECT i.task_id, i.video_id, i.position, i.error_message,
                       t.status, t.task_input_json, t.error_message AS task_error_message,
                       COALESCE((SELECT progress FROM task_events e WHERE e.task_id = t.task_id ORDER BY e.created_at DESC LIMIT 1), 0) AS progress,
                       COALESCE((SELECT message FROM task_events e WHERE e.task_id = t.task_id ORDER BY e.created_at DESC LIMIT 1), '') AS task_message
                FROM knowledge_job_items i
                LEFT JOIN tasks t ON t.task_id = i.task_id
                WHERE i.job_id = ?
                ORDER BY i.position ASC
                """,
                (job_id,),
            ).fetchall()
        payload = dict(job)
        payload["items"] = [dict(item) for item in items]
        return payload

    def update_knowledge_job(
        self,
        job_id: str,
        *,
        status: str,
        error_message: str | None = None,
        completed_at: str | None = None,
        agent_round: int | None = None,
        agent_state: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                UPDATE knowledge_jobs
                SET status = ?, error_message = ?, updated_at = ?, completed_at = ?,
                    agent_round = COALESCE(?, agent_round),
                    agent_state_json = COALESCE(?, agent_state_json)
                WHERE job_id = ?
                """,
                (
                    status,
                    error_message,
                    now,
                    completed_at,
                    int(agent_round) if agent_round is not None else None,
                    json.dumps(agent_state, ensure_ascii=False) if agent_state is not None else None,
                    job_id,
                ),
            )
        return self.get_knowledge_job(job_id)

    def _row_to_knowledge_conversation(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "conversation_id": row["conversation_id"],
            "title": row["title"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "last_message_at": datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
            "message_count": int(row["message_count"] or 0),
            "preview": str(row["preview"] or "")[:240],
        }

    def _row_to_knowledge_message(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "sequence": int(row["sequence"]),
            "role": row["role"],
            "content": row["content"] or "",
            "reasoning": row["reasoning"] or "",
            "status": row["status"],
            "sources": json.loads(row["sources_json"] or "[]"),
            "tools": json.loads(row["tools_json"] or "[]"),
            "references": json.loads(row["references_json"] or "[]"),
            "job_id": row["job_id"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }

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

    def set_video_global_visibility(self, video_id: str, visible: bool) -> VideoAssetRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT video_id FROM video_assets WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            cursor.execute(
                "UPDATE video_assets SET is_global_visible = ?, updated_at = ? WHERE video_id = ?",
                (1 if visible else 0, updated_at, video_id),
            )
        return self.get_video_asset(video_id)

    def initialize_video_collection_visibility(self, collection_id: str) -> None:
        """Keep collection members internal unless the user explicitly promoted them."""
        normalized_collection_id = str(collection_id or "").strip()
        if not normalized_collection_id:
            return
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute(
                "SELECT visibility_initialized FROM video_collections WHERE collection_id = ?",
                (normalized_collection_id,),
            ).fetchone()
            if collection is None:
                return
            cursor.execute(
                """
                UPDATE video_assets
                SET is_global_visible = 0, updated_at = ?
                WHERE video_id IN (
                    SELECT video_id
                    FROM video_collection_items
                    WHERE collection_id = ? AND video_id IS NOT NULL AND is_promoted = 0
                )
                AND is_global_visible != 0
                AND NOT EXISTS (
                    SELECT 1
                    FROM video_collection_items promoted_item
                    WHERE promoted_item.video_id = video_assets.video_id AND promoted_item.is_promoted = 1
                )
                """,
                (updated_at, normalized_collection_id),
            )
            if not bool(collection["visibility_initialized"]):
                cursor.execute(
                    "UPDATE video_collections SET visibility_initialized = 1, updated_at = ? WHERE collection_id = ?",
                    (updated_at, normalized_collection_id),
                )

    def promote_video_collection_item(self, collection_id: str, video_id: str) -> VideoAssetRecord | None:
        """Promote a member to the outer library while retaining its collection membership."""
        normalized_collection_id = str(collection_id or "").strip()
        normalized_video_id = str(video_id or "").strip()
        if not normalized_collection_id or not normalized_video_id:
            return None
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            membership = cursor.execute(
                """
                SELECT 1
                FROM video_collection_items
                WHERE collection_id = ? AND video_id = ?
                """,
                (normalized_collection_id, normalized_video_id),
            ).fetchone()
            if membership is None:
                return None
            asset = cursor.execute(
                "SELECT video_id FROM video_assets WHERE video_id = ?",
                (normalized_video_id,),
            ).fetchone()
            if asset is None:
                return None
            cursor.execute(
                """
                UPDATE video_collection_items
                SET is_promoted = 1, updated_at = ?
                WHERE collection_id = ? AND video_id = ?
                """,
                (updated_at, normalized_collection_id, normalized_video_id),
            )
            cursor.execute(
                "UPDATE video_assets SET is_global_visible = 1, updated_at = ? WHERE video_id = ?",
                (updated_at, normalized_video_id),
            )
        return self.get_video_asset(normalized_video_id)

    def reconcile_video_collection_assets(self, collection_id: str) -> None:
        """Link collection rows to assets that were imported before the collection was detected."""
        normalized_collection_id = str(collection_id or "").strip()
        if not normalized_collection_id:
            return
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            cursor.execute(
                """
                UPDATE video_collection_items
                SET video_id = (
                    SELECT video_id
                    FROM video_assets
                    WHERE canonical_id = video_collection_items.bvid
                ), updated_at = ?
                WHERE collection_id = ?
                  AND video_id IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM video_assets
                      WHERE canonical_id = video_collection_items.bvid
                  )
                """,
                (updated_at, normalized_collection_id),
            )

    def upsert_video_collection(
        self,
        payload: dict[str, object],
        source_url: str,
    ) -> VideoCollectionResponse:
        """Persist a collection discovered while probing its member video."""
        collection_id = str(payload.get("collection_id") or "").strip()
        if not collection_id:
            raise ValueError("Collection id is required.")
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            current = cursor.execute(
                "SELECT created_at, auto_check_on_open, global_order, owner_mid, owner_name, owner_face, owner_url, folder_id, folder_order, folder_pinned, is_favorite, favorite_updated_at, global_pinned FROM video_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            created_at = current["created_at"] if current is not None else now
            auto_check = int(current["auto_check_on_open"]) if current is not None else 1
            global_order = float(current["global_order"]) if current is not None and current["global_order"] is not None else self._next_library_order(cursor)
            owner_mid = str(payload.get("owner_mid") or (current["owner_mid"] if current is not None else "") or "").strip() or None
            owner_name = str(payload.get("owner_name") or (current["owner_name"] if current is not None else "") or "").strip() or None
            owner_face = str(payload.get("owner_face") or (current["owner_face"] if current is not None else "") or "").strip() or None
            owner_url = str(payload.get("owner_url") or (current["owner_url"] if current is not None else "") or "").strip() or None
            folder_id = current["folder_id"] if current is not None else None
            folder_order = float(current["folder_order"] or 0) if current is not None else 0
            folder_pinned = int(current["folder_pinned"] or 0) if current is not None else 0
            is_favorite = int(current["is_favorite"] or 0) if current is not None else 0
            favorite_updated_at = current["favorite_updated_at"] if current is not None else None
            global_pinned = int(current["global_pinned"] or 0) if current is not None else 0
            existing_item_rows = cursor.execute(
                "SELECT bvid FROM video_collection_items WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
            existing_item_bvids = {str(row["bvid"]) for row in existing_item_rows}
            custom_position_row = cursor.execute(
                """
                SELECT MAX(custom_position) AS max_position
                FROM video_collection_items
                WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchone()
            next_custom_position = float(custom_position_row["max_position"] or 0) + 1
            cursor.execute(
                """
                INSERT INTO video_collections (
                    collection_id, platform, title, source_url, cover_url, auto_check_on_open,
                    last_checked_at, pending_items_json, global_order, folder_id, folder_order, folder_pinned, global_pinned,
                    is_favorite, favorite_updated_at, owner_mid, owner_name, owner_face, owner_url, created_at, updated_at
                ) VALUES (?, 'bilibili', ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collection_id) DO UPDATE SET
                    title = excluded.title,
                    source_url = excluded.source_url,
                    cover_url = excluded.cover_url,
                    last_checked_at = excluded.last_checked_at,
                    owner_mid = COALESCE(excluded.owner_mid, video_collections.owner_mid),
                    owner_name = COALESCE(excluded.owner_name, video_collections.owner_name),
                    owner_face = COALESCE(excluded.owner_face, video_collections.owner_face),
                    owner_url = COALESCE(excluded.owner_url, video_collections.owner_url),
                    updated_at = excluded.updated_at
                """,
                (
                    collection_id,
                    str(payload.get("title") or "Bilibili 合集"),
                    str(source_url or ""),
                    str(payload.get("cover_url") or ""),
                    auto_check,
                    now,
                    global_order,
                    folder_id,
                    folder_order,
                    folder_pinned,
                    global_pinned,
                    is_favorite,
                    favorite_updated_at,
                    owner_mid,
                    owner_name,
                    owner_face,
                    owner_url,
                    created_at,
                    now,
                ),
            )
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                bvid = str(raw_item.get("bvid") or "").strip()
                if not bvid:
                    continue
                asset = cursor.execute(
                    "SELECT video_id FROM video_assets WHERE canonical_id = ?",
                    (bvid,),
                ).fetchone()
                cursor.execute(
                    """
                    INSERT INTO video_collection_items (
                        collection_id, bvid, position, custom_position, title, source_url,
                        cover_url,
                        duration, video_id, source_section_id, source_section_title,
                        source_section_position, source_present, removed_from_source_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    ON CONFLICT(collection_id, bvid) DO UPDATE SET
                        position = excluded.position,
                        title = excluded.title,
                        source_url = excluded.source_url,
                        cover_url = excluded.cover_url,
                        duration = excluded.duration,
                        video_id = COALESCE(excluded.video_id, video_collection_items.video_id),
                        source_section_id = excluded.source_section_id,
                        source_section_title = excluded.source_section_title,
                        source_section_position = excluded.source_section_position,
                        source_present = 1,
                        removed_from_source_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        collection_id,
                        bvid,
                        int(raw_item.get("position") or 0),
                        (
                            next_custom_position
                            if bvid not in existing_item_bvids
                            else None
                        ),
                        str(raw_item.get("title") or bvid),
                        str(raw_item.get("source_url") or ""),
                        str(raw_item.get("cover_url") or ""),
                        raw_item.get("duration"),
                        asset["video_id"] if asset is not None else None,
                        str(raw_item.get("source_section_id") or "").strip() or None,
                        str(raw_item.get("source_section_title") or "").strip() or None,
                        raw_item.get("source_section_position"),
                        now,
                        now,
                    ),
                )
                if bvid not in existing_item_bvids:
                    existing_item_bvids.add(bvid)
                    next_custom_position += 1
        return self.get_video_collection(collection_id) or VideoCollectionResponse(
            collection_id=collection_id,
            title=str(payload.get("title") or "Bilibili 合集"),
        )

    def refresh_video_collection(self, payload: dict[str, object], source_url: str) -> VideoCollectionResponse:
        """Store newly detected members as pending until the user accepts them."""
        collection_id = str(payload.get("collection_id") or "").strip()
        if not collection_id:
            raise ValueError("Collection id is required.")
        raw_items = [item for item in (payload.get("items") if isinstance(payload.get("items"), list) else []) if isinstance(item, dict)]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            existing = cursor.execute(
                "SELECT bvid FROM video_collection_items WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
            existing_bvids = {str(row["bvid"]) for row in existing}
            current = cursor.execute(
                "SELECT pending_items_json, created_at, auto_check_on_open FROM video_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if current is None:
                raise ValueError("Collection not found.")
            # The latest source payload is authoritative for source membership.
            # Keep historical rows, but hide missing members without touching
            # their assets, tasks, summaries, or custom positions.
            cursor.execute(
                """
                UPDATE video_collection_items
                SET source_present = 0,
                    removed_from_source_at = COALESCE(removed_from_source_at, ?),
                    updated_at = ?
                WHERE collection_id = ? AND source_present != 0
                """,
                (now, now, collection_id),
            )
            pending_by_bvid: dict[str, dict[str, object]] = {}
            for item in raw_items:
                bvid = str(item.get("bvid") or "").strip()
                if not bvid:
                    continue
                if bvid in existing_bvids:
                    cursor.execute(
                        """
                        UPDATE video_collection_items
                        SET position = ?, title = ?, source_url = ?, cover_url = ?, duration = ?,
                            source_section_id = ?, source_section_title = ?,
                            source_section_position = ?, source_present = 1,
                            removed_from_source_at = NULL, updated_at = ?
                        WHERE collection_id = ? AND bvid = ?
                        """,
                        (
                            int(item.get("position") or 0),
                            str(item.get("title") or bvid),
                            str(item.get("source_url") or ""),
                            str(item.get("cover_url") or ""),
                            item.get("duration"),
                            str(item.get("source_section_id") or "").strip() or None,
                            str(item.get("source_section_title") or "").strip() or None,
                            item.get("source_section_position"),
                            now,
                            collection_id,
                            bvid,
                        ),
                    )
                else:
                    pending_by_bvid[bvid] = item
            cursor.execute(
                """
                UPDATE video_collections
                SET title = ?, source_url = ?, cover_url = ?, last_checked_at = ?, pending_items_json = ?,
                    owner_mid = COALESCE(?, owner_mid), owner_name = COALESCE(?, owner_name),
                    owner_face = COALESCE(?, owner_face), owner_url = COALESCE(?, owner_url), updated_at = ?
                WHERE collection_id = ?
                """,
                (
                    str(payload.get("title") or "Bilibili 合集"),
                    str(source_url or ""),
                    str(payload.get("cover_url") or ""),
                    now,
                    json.dumps(list(pending_by_bvid.values()), ensure_ascii=False),
                    str(payload.get("owner_mid") or "").strip() or None,
                    str(payload.get("owner_name") or "").strip() or None,
                    str(payload.get("owner_face") or "").strip() or None,
                    str(payload.get("owner_url") or "").strip() or None,
                    now,
                    collection_id,
                ),
            )
        return self.get_video_collection(collection_id) or VideoCollectionResponse(collection_id=collection_id, title="Bilibili 合集")

    def add_pending_video_collection_items(
        self,
        collection_id: str,
        bvids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        normalized = {str(bvid).strip() for bvid in (bvids or []) if str(bvid).strip()}
        now = datetime.now(timezone.utc).isoformat()
        added: list[dict[str, object]] = []
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute(
                "SELECT pending_items_json FROM video_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if collection is None:
                return []
            pending = [item for item in json.loads(collection["pending_items_json"] or "[]") if isinstance(item, dict)]
            custom_position_row = cursor.execute(
                """
                SELECT MAX(custom_position) AS max_position
                FROM video_collection_items
                WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchone()
            next_custom_position = float(custom_position_row["max_position"] or 0) + 1
            remaining: list[dict[str, object]] = []
            for item in pending:
                bvid = str(item.get("bvid") or "").strip()
                if normalized and bvid not in normalized:
                    remaining.append(item)
                    continue
                if not bvid:
                    continue
                asset = cursor.execute(
                    "SELECT video_id FROM video_assets WHERE canonical_id = ?",
                    (bvid,),
                ).fetchone()
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO video_collection_items (
                        collection_id, bvid, position, custom_position, title, source_url,
                        cover_url,
                        duration, video_id, source_section_id, source_section_title,
                        source_section_position, source_present, removed_from_source_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (
                        collection_id,
                        bvid,
                        int(item.get("position") or 0),
                        next_custom_position,
                        str(item.get("title") or bvid),
                        str(item.get("source_url") or ""),
                        str(item.get("cover_url") or ""),
                        item.get("duration"),
                        asset["video_id"] if asset is not None else None,
                        str(item.get("source_section_id") or "").strip() or None,
                        str(item.get("source_section_title") or "").strip() or None,
                        item.get("source_section_position"),
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    next_custom_position += 1
                added.append(item)
            cursor.execute(
                "UPDATE video_collections SET pending_items_json = ?, updated_at = ? WHERE collection_id = ?",
                (json.dumps(remaining, ensure_ascii=False), now, collection_id),
            )
        return added

    def update_video_collection_settings(
        self,
        collection_id: str,
        *,
        auto_check_on_open: bool | None = None,
        view_sort_mode: str | None = None,
        view_group_mode: str | None = None,
    ) -> VideoCollectionResponse | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute(
                "SELECT collection_id FROM video_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if collection is None:
                return None
            updates: list[str] = []
            values: list[object] = []
            if auto_check_on_open is not None:
                updates.append("auto_check_on_open = ?")
                values.append(1 if auto_check_on_open else 0)
            if view_sort_mode is not None:
                updates.append("view_sort_mode = ?")
                values.append(view_sort_mode)
            if view_group_mode is not None:
                updates.append("view_group_mode = ?")
                values.append(view_group_mode)
            if updates:
                updates.append("updated_at = ?")
                values.append(now)
                values.append(collection_id)
                cursor.execute(
                    f"UPDATE video_collections SET {', '.join(updates)} WHERE collection_id = ?",
                    tuple(values),
                )
        return self.get_video_collection(collection_id)

    def reorder_video_collection_items(
        self,
        collection_id: str,
        ordered_bvids: list[str],
    ) -> VideoCollectionResponse | None:
        normalized = [str(bvid).strip() for bvid in ordered_bvids]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute(
                "SELECT collection_id FROM video_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if collection is None:
                return None
            if any(not bvid for bvid in normalized):
                raise ValueError("ordered_bvids must not contain empty values.")
            if len(normalized) != len(set(normalized)):
                raise ValueError("ordered_bvids must not contain duplicates.")
            rows = cursor.execute(
                """
                SELECT bvid
                FROM video_collection_items
                WHERE collection_id = ? AND source_present = 1
                """,
                (collection_id,),
            ).fetchall()
            current_bvids = {str(row["bvid"]) for row in rows}
            if len(normalized) != len(current_bvids) or set(normalized) != current_bvids:
                raise ValueError(
                    "ordered_bvids must contain every current collection item exactly once."
                )
            cursor.executemany(
                """
                UPDATE video_collection_items
                SET custom_position = ?, updated_at = ?
                WHERE collection_id = ? AND bvid = ?
                """,
                [
                    (float(index), now, collection_id, bvid)
                    for index, bvid in enumerate(normalized, start=1)
                ],
            )
            cursor.execute(
                "UPDATE video_collections SET updated_at = ? WHERE collection_id = ?",
                (now, collection_id),
            )
        return self.get_video_collection(collection_id)

    def set_video_collection_favorite(self, collection_id: str, is_favorite: bool) -> VideoCollectionResponse | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT collection_id FROM video_collections WHERE collection_id = ?", (collection_id,)).fetchone()
            if row is None:
                return None
            cursor.execute(
                "UPDATE video_collections SET is_favorite = ?, favorite_updated_at = ?, updated_at = ? WHERE collection_id = ?",
                (1 if is_favorite else 0, now if is_favorite else None, now, collection_id),
            )
        return self.get_video_collection(collection_id)

    def set_video_collection_folders(self, collection_id: str, folder_ids: list[str]) -> VideoCollectionResponse | None:
        normalized = []
        seen: set[str] = set()
        for folder_id in folder_ids:
            value = str(folder_id).strip()
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT collection_id, folder_id, folder_order, folder_pinned FROM video_collections WHERE collection_id = ?", (collection_id,)).fetchone()
            if row is None or any(not self._folder_exists(cursor, folder_id) for folder_id in normalized):
                return None
            existing_rows = cursor.execute(
                "SELECT folder_id, folder_order, folder_pinned, created_at FROM video_collection_folder_memberships WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
            existing = {str(item["folder_id"]): item for item in existing_rows}
            cursor.execute("DELETE FROM video_collection_folder_memberships WHERE collection_id = ?", (collection_id,))
            for folder_id in normalized:
                item = existing.get(folder_id)
                order = float(item["folder_order"]) if item is not None and item["folder_order"] is not None else self._next_collection_order(cursor, folder_id)
                pinned = int(item["folder_pinned"] or 0) if item is not None else 0
                created_at = item["created_at"] if item is not None else updated_at
                cursor.execute(
                    "INSERT INTO video_collection_folder_memberships (collection_id, folder_id, folder_order, folder_pinned, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (collection_id, folder_id, order, pinned, created_at, updated_at),
                )
            primary = normalized[0] if normalized else None
            primary_row = existing.get(primary) if primary else None
            primary_order = float(primary_row["folder_order"]) if primary_row is not None and primary_row["folder_order"] is not None else self._next_collection_order(cursor, primary)
            primary_pinned = int(primary_row["folder_pinned"] or 0) if primary_row is not None else 0
            cursor.execute(
                "UPDATE video_collections SET folder_id = ?, folder_order = ?, folder_pinned = ?, updated_at = ? WHERE collection_id = ?",
                (primary, primary_order, primary_pinned, updated_at, collection_id),
            )
        return self.get_video_collection(collection_id)

    def set_video_collection_pin(
        self,
        collection_id: str,
        *,
        global_pinned: bool | None = None,
        folder_pinned: bool | None = None,
    ) -> VideoCollectionResponse | None:
        updates: list[str] = []
        values: list[object] = []
        if global_pinned is not None:
            updates.append("global_pinned = ?")
            values.append(1 if global_pinned else 0)
        if folder_pinned is not None:
            updates.append("folder_pinned = ?")
            values.append(1 if folder_pinned else 0)
        if not updates:
            return self.get_video_collection(collection_id)
        now = datetime.now(timezone.utc).isoformat()
        values.extend([now, collection_id])
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute("SELECT collection_id FROM video_collections WHERE collection_id = ?", (collection_id,)).fetchone()
            if row is None:
                return None
            cursor.execute(f"UPDATE video_collections SET {', '.join(updates)}, updated_at = ? WHERE collection_id = ?", tuple(values))
            if folder_pinned is not None:
                cursor.execute(
                    "UPDATE video_collection_folder_memberships SET folder_pinned = ?, updated_at = ? WHERE collection_id = ? AND folder_id = (SELECT folder_id FROM video_collections WHERE collection_id = ?)",
                    (1 if folder_pinned else 0, now, collection_id, collection_id),
                )
        return self.get_video_collection(collection_id)

    def delete_video_collection(self, collection_id: str, *, detach_contents: bool) -> list[str] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute("SELECT collection_id FROM video_collections WHERE collection_id = ?", (collection_id,)).fetchone()
            if collection is None:
                return None
            rows = cursor.execute("SELECT video_id FROM video_collection_items WHERE collection_id = ? AND video_id IS NOT NULL", (collection_id,)).fetchall()
            video_ids = list(dict.fromkeys(str(row["video_id"]) for row in rows if row["video_id"]))
            if detach_contents and video_ids:
                placeholders = ",".join("?" for _ in video_ids)
                cursor.execute(
                    f"UPDATE video_assets SET is_global_visible = 1, updated_at = ? WHERE video_id IN ({placeholders})",
                    (now, *video_ids),
                )
            cursor.execute("DELETE FROM video_collection_folder_memberships WHERE collection_id = ?", (collection_id,))
            cursor.execute(
                "DELETE FROM video_collection_task_batches WHERE collection_id = ?",
                (collection_id,),
            )
            cursor.execute("DELETE FROM video_collection_items WHERE collection_id = ?", (collection_id,))
            cursor.execute("DELETE FROM video_collections WHERE collection_id = ?", (collection_id,))
        return video_ids

    def _next_collection_order(self, cursor: sqlite3.Cursor, folder_id: str | None) -> float:
        if not folder_id:
            return 0
        row = cursor.execute(
            "SELECT MAX(folder_order) AS max_order FROM video_folder_memberships WHERE folder_id = ?",
            (folder_id,),
        ).fetchone()
        collection_row = cursor.execute(
            "SELECT MAX(folder_order) AS max_order FROM video_collections WHERE folder_id = ?",
            (folder_id,),
        ).fetchone()
        values = [float(item["max_order"]) for item in (row, collection_row) if item is not None and item["max_order"] is not None]
        return (max(values) if values else 0) + 1000

    def list_video_collections(self) -> list[VideoCollectionResponse]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute("SELECT collection_id FROM video_collections ORDER BY global_order ASC, updated_at DESC").fetchall()
        return [collection for row in rows if (collection := self.get_video_collection(str(row["collection_id"]))) is not None]

    def get_video_collection(self, collection_id: str) -> VideoCollectionResponse | None:
        self.reconcile_video_collection_assets(collection_id)
        with self._lock, sqlite_cursor(self._connection) as cursor:
            collection = cursor.execute(
                """
                SELECT collection_id, title, source_url, cover_url, auto_check_on_open, last_checked_at, pending_items_json,
                       global_order, folder_id, folder_order, folder_pinned, global_pinned, is_favorite, favorite_updated_at,
                       owner_mid, owner_name, owner_face, owner_url, view_sort_mode, view_group_mode
                FROM video_collections WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchone()
            if collection is None:
                return None
            items = cursor.execute(
                """
                SELECT collection_id, bvid, position, custom_position, title, source_url,
                       cover_url, duration, video_id, source_section_id, source_section_title,
                       source_section_position
                FROM video_collection_items
                WHERE collection_id = ? AND source_present = 1
                ORDER BY position ASC, bvid ASC
                """,
                (collection_id,),
            ).fetchall()
        response_items: list[VideoCollectionItemResponse] = []
        for row in items:
            asset = self.get_video_asset(str(row["video_id"])) if row["video_id"] else None
            response_items.append(
                VideoCollectionItemResponse(
                    position=int(row["position"] or 0),
                    custom_position=(
                        float(row["custom_position"])
                        if row["custom_position"] is not None
                        else None
                    ),
                    bvid=str(row["bvid"]),
                    title=str(row["title"] or row["bvid"]),
                    source_url=str(row["source_url"] or ""),
                    cover_url=str(row["cover_url"] or ""),
                    duration=row["duration"],
                    source_section_id=str(row["source_section_id"] or "") or None,
                    source_section_title=str(row["source_section_title"] or "") or None,
                    source_section_position=(
                        int(row["source_section_position"])
                        if row["source_section_position"] is not None
                        else None
                    ),
                    video_id=asset.video_id if asset else row["video_id"],
                    has_result=bool(asset and asset.latest_result is not None),
                    latest_status=asset.latest_status if asset else None,
                    last_summary_at=asset.last_summary_at if asset else None,
                    asset_updated_at=asset.updated_at if asset else None,
                    is_global_visible=bool(asset and asset.is_global_visible),
                )
            )
        pending_items = [
            VideoCollectionItemResponse.model_validate(item).model_copy(update={"is_new": True})
            for item in json.loads(collection["pending_items_json"] or "[]")
            if isinstance(item, dict) and item.get("bvid")
        ]
        summarized_count = sum(1 for item in response_items if item.has_result)
        latest_video_updated_at = max(
            (
                asset.last_summary_at
                or asset.latest_task_completed_at
                or asset.latest_task_created_at
                or asset.updated_at
                for asset in (self.get_video_asset(str(row["video_id"])) for row in items)
                if asset is not None
            ),
            default=None,
        )
        view_sort_mode = str(collection["view_sort_mode"] or "source")
        if view_sort_mode not in {
            "source",
            "source_desc",
            "recent_summary",
            "recent_update",
            "duration",
            "custom",
        }:
            view_sort_mode = "source"
        view_group_mode = str(collection["view_group_mode"] or "none")
        if view_group_mode not in {"none", "status", "source_section"}:
            view_group_mode = "none"
        return VideoCollectionResponse(
            collection_id=str(collection["collection_id"]),
            title=str(collection["title"]),
            cover_url=str(collection["cover_url"] or ""),
            global_order=float(collection["global_order"] or 0),
            folder_order=float(collection["folder_order"] or 0),
            folder_id=collection["folder_id"],
            folder_ids=self._folder_ids_for_collection(str(collection["collection_id"])),
            global_pinned=bool(collection["global_pinned"]),
            folder_pinned=bool(collection["folder_pinned"]),
            is_favorite=bool(collection["is_favorite"]),
            favorite_updated_at=datetime.fromisoformat(collection["favorite_updated_at"]) if collection["favorite_updated_at"] else None,
            latest_video_updated_at=latest_video_updated_at,
            owner_mid=str(collection["owner_mid"] or "") or None,
            owner_name=str(collection["owner_name"] or "") or None,
            owner_face=str(collection["owner_face"] or "") or None,
            owner_url=str(collection["owner_url"] or "") or None,
            source_url=str(collection["source_url"] or ""),
            item_count=len(response_items),
            summarized_count=summarized_count,
            unsummarized_count=max(0, len(response_items) - summarized_count),
            last_checked_at=datetime.fromisoformat(collection["last_checked_at"]) if collection["last_checked_at"] else None,
            auto_check_on_open=bool(collection["auto_check_on_open"]),
            view_sort_mode=view_sort_mode,
            view_group_mode=view_group_mode,
            items=response_items,
            new_items=pending_items,
        )

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

    def list_video_ids_in_folder(self, folder_id: str) -> list[str]:
        with self._lock, sqlite_cursor(self._connection) as cursor:
            rows = cursor.execute(
                "SELECT video_id FROM video_folder_memberships WHERE folder_id = ? ORDER BY folder_order ASC, video_id ASC",
                (folder_id,),
            ).fetchall()
        return [str(row["video_id"]) for row in rows]

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
            cursor.execute(f"DELETE FROM video_collection_folder_memberships WHERE folder_id IN ({placeholders})", tuple(folder_ids))
            cursor.execute(
                f"UPDATE video_collections SET folder_id = NULL, folder_order = ?, folder_pinned = 0, updated_at = ? WHERE folder_id IN ({placeholders})",
                (self._next_collection_order(cursor, None), datetime.now(timezone.utc).isoformat(), *folder_ids),
            )
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

    def reorder_library_items(self, items: list[tuple[str, str]], *, folder_id: str | None = "__global__") -> None:
        """Persist mixed video/collection order in the global library or a folder scope."""
        normalized = [(str(kind), str(item_id).strip()) for kind, item_id in items if str(item_id).strip()]
        if not normalized:
            return
        if len({(kind, item_id) for kind, item_id in normalized}) != len(normalized):
            raise ValueError("Library reorder payload contains duplicate items.")
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            if folder_id not in (None, "__global__") and not self._folder_exists(cursor, folder_id):
                raise ValueError("Folder not found.")
            if folder_id == "__global__":
                valid_videos = {str(row["video_id"]) for row in cursor.execute("SELECT video_id FROM video_assets WHERE is_global_visible = 1").fetchall()}
                valid_collections = {str(row["collection_id"]) for row in cursor.execute("SELECT collection_id FROM video_collections").fetchall()}
            elif folder_id is None:
                valid_videos = {
                    str(row["video_id"])
                    for row in cursor.execute(
                        "SELECT v.video_id FROM video_assets v WHERE v.is_global_visible = 1 AND NOT EXISTS (SELECT 1 FROM video_folder_memberships m WHERE m.video_id = v.video_id)"
                    ).fetchall()
                }
                valid_collections = {
                    str(row["collection_id"])
                    for row in cursor.execute(
                        "SELECT c.collection_id FROM video_collections c WHERE NOT EXISTS (SELECT 1 FROM video_collection_folder_memberships m WHERE m.collection_id = c.collection_id)"
                    ).fetchall()
                }
            else:
                valid_videos = {
                    str(row["video_id"])
                    for row in cursor.execute(
                        "SELECT m.video_id FROM video_folder_memberships m JOIN video_assets v ON v.video_id = m.video_id WHERE m.folder_id = ? AND v.is_global_visible = 1",
                        (folder_id,),
                    ).fetchall()
                }
                valid_collections = {
                    str(row["collection_id"])
                    for row in cursor.execute(
                        "SELECT collection_id FROM video_collection_folder_memberships WHERE folder_id = ?",
                        (folder_id,),
                    ).fetchall()
                }
            if any(
                (kind != "video" or item_id not in valid_videos)
                and (kind != "collection" or item_id not in valid_collections)
                for kind, item_id in normalized
            ):
                raise ValueError("Library reorder payload contains items outside the outer library.")
            for index, (kind, item_id) in enumerate(normalized):
                order = (index + 1) * 1000
                if kind == "video" and folder_id == "__global__":
                    cursor.execute(
                        "UPDATE video_assets SET global_order = ?, updated_at = ? WHERE video_id = ?",
                        (order, updated_at, item_id),
                    )
                elif kind == "video" and folder_id is None:
                    cursor.execute(
                        "UPDATE video_assets SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id IS NULL",
                        (order, updated_at, item_id),
                    )
                elif kind == "video":
                    cursor.execute(
                        "UPDATE video_folder_memberships SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id = ?",
                        (order, updated_at, item_id, folder_id),
                    )
                    cursor.execute(
                        "UPDATE video_assets SET folder_order = ?, updated_at = ? WHERE video_id = ? AND folder_id = ?",
                        (order, updated_at, item_id, folder_id),
                    )
                elif folder_id == "__global__":
                    cursor.execute(
                        "UPDATE video_collections SET global_order = ?, updated_at = ? WHERE collection_id = ?",
                        (order, updated_at, item_id),
                    )
                elif folder_id is None:
                    cursor.execute(
                        "UPDATE video_collections SET folder_order = ?, updated_at = ? WHERE collection_id = ? AND folder_id IS NULL",
                        (order, updated_at, item_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE video_collection_folder_memberships SET folder_order = ?, updated_at = ? WHERE collection_id = ? AND folder_id = ?",
                        (order, updated_at, item_id, folder_id),
                    )
                    cursor.execute(
                        "UPDATE video_collections SET folder_order = ?, updated_at = ? WHERE collection_id = ? AND folder_id = ?",
                        (order, updated_at, item_id, folder_id),
                    )

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
            row = cursor.execute("SELECT video_id, created_at FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and row["video_id"]:
                duration = None
                if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    try:
                        duration = max(
                            0.0,
                            (datetime.fromisoformat(updated_at) - datetime.fromisoformat(row["created_at"])).total_seconds(),
                        )
                    except (TypeError, ValueError):
                        duration = None
                cursor.execute(
                    """
                    UPDATE video_assets
                    SET latest_task_id = ?, latest_status = ?, latest_task_completed_at = ?,
                        latest_task_duration_seconds = ?,
                        last_summary_at = CASE WHEN ? = 'completed' THEN ? ELSE last_summary_at END,
                        updated_at = ?
                    WHERE video_id = ?
                    """,
                    (
                        task_id,
                        status.value,
                        updated_at if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED} else None,
                        duration,
                        status.value,
                        updated_at,
                        updated_at,
                        row["video_id"],
                    ),
                )
        return self.get_task(task_id)

    def _save_result_locked(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        result: TaskResult,
        updated_at: str,
    ) -> None:
        payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
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

    def save_result(self, task_id: str, result: TaskResult) -> TaskRecord | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            self._save_result_locked(cursor, task_id, result, updated_at)
        return self.get_task(task_id)

    def update_result(
        self,
        task_id: str,
        updater: Callable[[TaskResult], TaskResult],
    ) -> TaskRecord | None:
        """Read, transform, and persist one task result under the repository lock."""

        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite_cursor(self._connection) as cursor:
            row = cursor.execute(
                "SELECT result_json FROM task_results WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or not row["result_json"]:
                return None
            current_result = TaskResult.model_validate(json.loads(row["result_json"]))
            updated_result = updater(current_result)
            self._save_result_locked(cursor, task_id, updated_result, updated_at)
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

    def _insert_task_record(
        self,
        cursor: sqlite3.Cursor,
        record: TaskRecord,
        payload: dict[str, str | None],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO tasks (
                task_id, video_id, status, task_input_json, page_number, page_title,
                result_json, error_code, error_message, created_at, updated_at
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
        if record.video_id:
            cursor.execute(
                """
                UPDATE video_assets
                SET latest_task_id = ?, latest_status = ?, latest_stage = ?,
                    latest_task_created_at = ?, latest_task_completed_at = NULL,
                    latest_task_duration_seconds = NULL,
                    latest_error_message = NULL, updated_at = ?
                WHERE video_id = ?
                """,
                (
                    record.task_id,
                    record.status.value,
                    "queued",
                    payload["created_at"],
                    payload["updated_at"],
                    record.video_id,
                ),
            )

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
            latest_task_created_at=datetime.fromisoformat(row["latest_task_created_at"]) if row["latest_task_created_at"] else None,
            latest_task_completed_at=datetime.fromisoformat(row["latest_task_completed_at"]) if row["latest_task_completed_at"] else None,
            latest_task_duration_seconds=float(row["latest_task_duration_seconds"]) if row["latest_task_duration_seconds"] is not None else None,
            last_summary_at=datetime.fromisoformat(row["last_summary_at"]) if row["last_summary_at"] else None,
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
            is_global_visible=bool(row["is_global_visible"]),
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

    def _folder_ids_for_collection(self, collection_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT folder_id FROM video_collection_folder_memberships WHERE collection_id = ? ORDER BY created_at ASC, folder_order ASC",
            (collection_id,),
        ).fetchall()
        return [str(row["folder_id"]) for row in rows]

    def _video_ids_in_scope(self, cursor: sqlite3.Cursor, folder_id: str | None) -> set[str]:
        if folder_id == "__global__":
            rows = cursor.execute("SELECT video_id FROM video_assets WHERE is_global_visible = 1").fetchall()
        elif folder_id is None:
            rows = cursor.execute(
                """
                SELECT v.video_id
                FROM video_assets v
                WHERE v.is_global_visible = 1 AND NOT EXISTS (
                    SELECT 1
                    FROM video_folder_memberships m
                    WHERE m.video_id = v.video_id
                )
                """
            ).fetchall()
        else:
            rows = cursor.execute(
                """
                SELECT m.video_id
                FROM video_folder_memberships m
                JOIN video_assets v ON v.video_id = m.video_id
                WHERE m.folder_id = ? AND v.is_global_visible = 1
                """,
                (folder_id,),
            ).fetchall()
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
