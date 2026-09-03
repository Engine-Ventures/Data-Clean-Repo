"""Shared fixtures. Builds the database once per test session.

Ported from SQLite. The old fixtures handed every test a throwaway
``pipeline.db`` under ``tmp_path_factory``; a file per session was the whole
isolation story. Postgres has no equivalent, and dropping/creating a database
needs privileges a Neon role may not have, so isolation is per-session
**schema** instead: each session creates ``evtest_<random>``, points
``search_path`` at it, applies the DDL there, and drops it CASCADE afterwards.
Two sessions -- or a session and a human poking at the same branch -- cannot
collide, and nothing is left behind.

Set ``TEST_DATABASE_URL`` to point at a scratch Neon branch. It falls back to
``DATABASE_URL``, which is convenient but means a careless run creates and
drops a schema in whatever database that names; the schema is namespaced and
dropped, but point this at a branch you do not mind writing to.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evpipeline import db
from evpipeline.ingest import build

RAW = REPO_ROOT / "data" / "raw"
DRAFT = RAW / "EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx"
V2 = RAW / "EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx"
AFFINITY = RAW / "affinity_export_2026-09-01.csv"


def _database_url() -> str:
    """The scratch database URL, or skip the whole session."""
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url.strip():
        pytest.skip(
            "no TEST_DATABASE_URL (or DATABASE_URL) set; these tests need a "
            "PostgreSQL database to build into",
            allow_module_level=True,
        )
    return url.strip()


def _open_isolated_schema(url: str):
    """A connection whose search_path is a fresh, empty, uniquely-named schema.

    Returns ``(conn, schema_name)``. The caller drops it.

    ``search_path`` is set with ``SET`` rather than baked into the URL's
    ``options`` so it survives psycopg reconnecting, and it puts ``public``
    second so extensions installed there stay reachable.
    """
    schema = f"evtest_{secrets.token_hex(4)}"
    conn = db.connect(url)
    # Identifier is generated from token_hex, so it cannot need quoting, but
    # compose it properly anyway -- a schema name reaching a DDL string by
    # concatenation is a habit worth not having.
    from psycopg import sql

    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    conn.commit()
    return conn, schema


def _drop_schema(conn, schema: str) -> None:
    from psycopg import sql

    conn.rollback()  # a failed test may leave the transaction aborted
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()


@pytest.fixture(scope="session")
def seeded_conn():
    """DDL + seed data applied, no workbook loaded.

    This is the fixture for anything that tests the schema itself or the
    write path: the controlled-vocabulary agreement tests, and the
    hand-add/tags tests that only need an empty but valid database. It
    deliberately does NOT depend on the raw workbooks, so those tests still
    run when the sources are absent.
    """
    url = _database_url()
    conn, schema = _open_isolated_schema(url)
    try:
        db.create_schema(conn)
        yield conn
    finally:
        _drop_schema(conn, schema)
        conn.close()


@pytest.fixture(scope="session")
def report(_require_sources) -> dict:
    """Build into a throwaway schema; return the ingest coverage report.

    The connection is kept open for the whole session and handed to the
    ``conn`` fixture, because the data lives in a schema that is dropped when
    this fixture tears down -- unlike the SQLite original, a second connection
    opened later cannot find it unless it sets the same search_path.
    """
    url = _database_url()
    conn, schema = _open_isolated_schema(url)
    try:
        db.create_schema(conn)
        rep = build(conn, DRAFT, v2_path=V2, affinity_path=AFFINITY)
        conn.commit()
        rep["_schema"] = schema
        rep["_conn"] = conn
        yield rep
    finally:
        _drop_schema(conn, schema)
        conn.close()


@pytest.fixture(scope="session")
def conn(report):
    """The built database. Same connection as ``report``, not a new one."""
    return report["_conn"]


# Requested by `report` rather than autouse: tests that build their own empty
# schema (the write-path tests) need no workbook, and should not be skipped
# along with the ingest tests when one is absent.
@pytest.fixture(scope="session")
def _require_sources():
    missing = [p.name for p in (DRAFT, V2, AFFINITY) if not p.exists()]
    if missing:
        pytest.skip(f"raw sources not present: {', '.join(missing)}")
