"""Rewrite references in a verified staging copy, before storage is switched.

This module intentionally uses only the standard library so the frozen backend
can run it without initializing the service or the managed runtime.
"""

import json
import sqlite3
from pathlib import Path


def rewrite_value(value, mappings):
    if isinstance(value, list):
        return [rewrite_value(item, mappings) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_value(item, mappings) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    # SQLite JSON columns contain escaped Windows paths. Parse them before
    # replacing prefixes, and preserve unchanged serialized values verbatim.
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except ValueError:
            pass
        else:
            rewritten = rewrite_value(parsed, mappings)
            return json.dumps(rewritten, ensure_ascii=False) if rewritten != parsed else value
    for mapping in sorted(mappings, key=lambda item: len(item["source"]), reverse=True):
        for source, target in ((mapping["source"], mapping["target"]),
                               (mapping["source"].replace("\\", "/"), mapping["target"].replace("\\", "/"))):
            for prefix in ("", "sqlite:///"):
                old = prefix + source.rstrip("/\\")
                if value.casefold() == old.casefold() or (
                    value[:len(old)].casefold() == old.casefold() and value[len(old):len(old) + 1] in ("/", "\\")
                ):
                    return prefix + target.rstrip("/\\") + value[len(old):]
    return value


def rewrite_database(file: Path, mappings):
    with file.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            return
    connection = sqlite3.connect(file)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        # Only BiliSum's own database is rewritten; model caches and third-party
        # SQLite databases retain their original contents.
        if not {"tasks", "video_assets"}.issubset(tables):
            return
        connection.execute("BEGIN IMMEDIATE")
        for name in tables:
            if name.startswith("sqlite_"):
                continue
            table = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[2].upper() == "TEXT"]
            if not columns:
                continue
            quoted = ['"' + column.replace('"', '""') + '"' for column in columns]
            rows = connection.execute(f"SELECT rowid, {', '.join(quoted)} FROM {table}").fetchall()
            for row in rows:
                values = [rewrite_value(value, mappings) for value in row[1:]]
                if values != list(row[1:]):
                    assignments = ', '.join(f"{column} = ?" for column in quoted)
                    connection.execute(f"UPDATE {table} SET {assignments} WHERE rowid = ?", [*values, row[0]])
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"迁移后的数据库校验失败：{file.name}")
        connection.commit()
    finally:
        connection.close()


def migrate_manifest(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    stage_root = Path(manifest["stageRoot"]).resolve(strict=True)
    mappings = manifest["mappings"]
    for raw in manifest["files"]:
        file = Path(raw).resolve(strict=True)
        if not file.is_relative_to(stage_root):
            raise ValueError("迁移文件不在暂存目录内。")
        if file.suffix.lower() == ".json":
            try:
                original = json.loads(file.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                continue
            rewritten = rewrite_value(original, mappings)
            if rewritten != original:
                file.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif file.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            rewrite_database(file, mappings)


def run(manifest_path):
    migrate_manifest(manifest_path)
