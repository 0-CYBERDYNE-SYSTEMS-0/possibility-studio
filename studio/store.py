from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def data_path() -> Path:
    configured = os.environ.get("STUDIO_DATA_DIR")
    return Path(configured).expanduser() if configured else Path(__file__).parent.parent / ".studio-data"


def database_path(root: Path) -> Path:
    return root / "studio.sqlite3"


@contextmanager
def connect(root: Path, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path(root), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if immediate:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with connect(root) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                brief TEXT NOT NULL,
                reference_intent TEXT NOT NULL,
                reference_path TEXT,
                reference_type TEXT,
                reference_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id),
                alternative_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT,
                request_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                model TEXT,
                provider_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operation_project_created
                ON operations(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS operation_status
                ON operations(status);
            CREATE TABLE IF NOT EXISTS alternatives (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                direction_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alternative_project_created
                ON alternatives(project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                alternative_id TEXT NOT NULL REFERENCES alternatives(id),
                parent_revision_id TEXT REFERENCES revisions(id),
                source_html TEXT NOT NULL,
                instruction TEXT NOT NULL,
                model TEXT,
                provider_url TEXT,
                reference_path TEXT,
                reference_type TEXT,
                reference_sha256 TEXT,
                assets_json TEXT NOT NULL DEFAULT '[]',
                requirements_json TEXT NOT NULL DEFAULT '[]',
                assumptions_json TEXT NOT NULL DEFAULT '[]',
                checks_json TEXT NOT NULL DEFAULT '{}',
                operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS revision_alternative_created
                ON revisions(alternative_id, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS one_root_revision_per_alternative
                ON revisions(alternative_id) WHERE parent_revision_id IS NULL;
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS asset_project_created
                ON assets(project_id, created_at DESC);
            """
        )
        operation_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(operations)").fetchall()
        }
        if "provider_url" not in operation_columns:
            db.execute("ALTER TABLE operations ADD COLUMN provider_url TEXT")
        if "stage" not in operation_columns:
            db.execute("ALTER TABLE operations ADD COLUMN stage TEXT")
        if "partial_output" not in operation_columns:
            db.execute("ALTER TABLE operations ADD COLUMN partial_output TEXT")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(revisions)").fetchall()}
        for name in (
            "provider_url", "reference_path", "reference_type", "reference_sha256",
            "suggestions_json", "suggestions_status", "suggestions_error",
        ):
            if name not in columns:
                db.execute("ALTER TABLE revisions ADD COLUMN " + name + " TEXT")
        for name, declaration in (
            ("assets_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("requirements_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("assumptions_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("checks_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in columns:
                db.execute("ALTER TABLE revisions ADD COLUMN " + name + " " + declaration)


def mark_unfinished_interrupted(root: Path) -> int:
    stamp = now()
    with connect(root, immediate=True) as db:
        cursor = db.execute(
            """
            UPDATE operations
            SET status = 'interrupted',
                error = 'The server restarted before this operation finished.',
                updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (stamp,),
        )
        db.execute(
            """
            UPDATE alternatives
            SET status = 'interrupted'
            WHERE status = 'generating'
            """
        )
        db.execute(
            """
            UPDATE revisions
            SET suggestions_status = 'failed',
                suggestions_error = 'The server restarted before the suggestions finished.'
            WHERE suggestions_status = 'pending'
            """
        )
        return cursor.rowcount


def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None) -> Any:
    return json.loads(value) if value else None

