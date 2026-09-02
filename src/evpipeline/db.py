"""SQLite connection handling and schema creation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "0.1.0"

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "pipeline.db"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with the settings this schema assumes."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def start_run(conn: sqlite3.Connection, source_file: str | Path, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO ingest_run (source_file, source_sha256, schema_version, note) "
        "VALUES (?, ?, ?, ?)",
        (str(source_file), sha256(source_file), SCHEMA_VERSION, note),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, row_counts: dict) -> None:
    conn.execute(
        "UPDATE ingest_run SET finished_at = datetime('now'), row_counts = ? "
        "WHERE run_id = ?",
        (json.dumps(row_counts, sort_keys=True), run_id),
    )
    conn.commit()
