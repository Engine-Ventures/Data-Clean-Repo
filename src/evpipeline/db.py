"""PostgreSQL (Neon) connection handling, schema creation and row access.

Ported from SQLite. Four things changed and each one is load-bearing:

  * **Row access.** ``sqlite3.Row`` supported subscripting by column name and
    by position. psycopg3's default row factory is a plain tuple (position
    only) and its ``dict_row`` is a plain dict (name only). This repo uses
    both styles freely -- 47 ``fetchone()[0]`` call sites alongside named
    access throughout -- so ``Row`` below restores the old contract instead of
    rewriting ~50 queries. See the note on placeholders, which is the part
    that could NOT be papered over this way.
  * **Pooling.** Connections come from a module-level ``ConnectionPool``.
    Opening a fresh TCP+TLS connection to Neon per operation is ~100ms of
    handshake, and ``scripts/serve.py`` re-renders the whole page per GET.
  * **Two-file schema.** ``create_schema`` applies ``schema.sql`` then
    ``seed.sql``. The vocabularies used to be seeded inline in the DDL; they
    were split out so the DDL can be re-run against a shared Neon branch
    without fighting over rows. That split is also what fixes the 709-row
    stage mislabel -- see MIGRATION.md.
  * **``lastrowid`` is gone.** Postgres has no rowid; inserts that need their
    key back use ``RETURNING``.

``PRAGMA foreign_keys = ON`` has no equivalent and needs none: Postgres always
enforces foreign keys. This makes the runtime strictly stricter than the
SQLite original, where the PRAGMA was set on connections opened through
``connect()`` but not on every path that touched the file.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# Bumped from 0.1.0: the storage engine changed, and ingest_run.schema_version
# is the only record of which engine produced a given run's row counts.
SCHEMA_VERSION = "0.2.0"

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schema.sql"
SEED_PATH = REPO_ROOT / "seed.sql"

# Small on purpose. Neon's own connection limits are modest on the free and
# launch tiers, and when DATABASE_URL points at the -pooler endpoint there is
# already a PgBouncer in front of the database -- a large client-side pool
# behind a server-side pool mostly adds queueing, not throughput.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 8


# ---------------------------------------------------------------------------
# Row access
# ---------------------------------------------------------------------------


class Row(dict):
    """A result row addressable by column name *or* by position.

    Subclassing ``dict`` is deliberate and does most of the work: it is what
    makes ``row["name"]``, ``row.keys()``, ``dict(row)``, ``in`` and iteration
    behave without any further code, which matters because
    ``metrics.py:104,113`` iterate ``row.keys()``, ``scripts/build_ui.py:96``
    calls ``dict(r)``, and ``scripts/audit_coverage.py:159`` feeds
    ``rows[0].keys()`` to ``csv.DictWriter``. Only integer and slice
    subscripting is added on top.

    One inherited wart, shared with ``sqlite3.Row``: a query selecting the
    same column name twice keeps one entry in the mapping (the last), though
    positional access still resolves both. Alias duplicate columns in the
    query rather than relying on this.
    """

    __slots__ = ("_fields",)

    def __init__(self, fields: tuple[str, ...], values: Sequence[Any]) -> None:
        super().__init__(zip(fields, values, strict=True))
        self._fields = fields

    def __getitem__(self, key: str | int | slice) -> Any:
        if isinstance(key, str):
            return dict.__getitem__(self, key)
        if isinstance(key, int):
            # Negative indices and IndexError both come free from the tuple.
            return dict.__getitem__(self, self._fields[key])
        if isinstance(key, slice):
            return tuple(dict.__getitem__(self, f) for f in self._fields[key])
        raise TypeError(f"row indices must be str, int or slice, not {type(key).__name__}")

    @property
    def fields(self) -> tuple[str, ...]:
        """Column names in select order (mapping order can differ on dupes)."""
        return self._fields


def row_factory(cursor: psycopg.Cursor) -> Any:
    """psycopg3 row factory producing :class:`Row`.

    Called once per result set, so the column-name tuple is built once and
    shared by every row rather than per row.
    """
    description = cursor.description
    if description is None:  # a statement returning no result set
        return lambda values: values
    fields = tuple(column.name for column in description)
    return lambda values: Row(fields, values)


# ---------------------------------------------------------------------------
# Connection string
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """DATABASE_URL is missing or unusable."""


def _is_pooled(url: str) -> bool:
    """Whether the URL points at Neon's PgBouncer endpoint.

    Neon exposes two hostnames per branch: the direct endpoint and
    ``...-pooler...`` which is PgBouncer in transaction pooling mode. It
    matters here because transaction pooling cannot carry a session-scoped
    prepared statement between requests -- see :func:`_configure`.
    """
    host = urlsplit(url).hostname or ""
    return "-pooler." in host


# Hosts that cannot do TLS and must not have sslmode=require forced on them.
# A local development server built by `brew install postgresql@17` has no
# certificate at all and rejects the attempt outright with "server does not
# support SSL, but SSL was required" -- so forcing it unconditionally makes
# the pool unusable in exactly the environment the test suite runs in.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def database_url() -> str:
    """DATABASE_URL from the environment, with Neon's requirements enforced.

    Neon terminates TLS at the proxy and refuses plaintext, and it needs SNI
    to route to the right branch -- libpq sends SNI whenever it negotiates
    TLS, so requiring sslmode is what makes branch routing work as well as
    what encrypts the connection. A URL without it fails at connect time with
    a message that does not mention TLS, so it is added here rather than
    debugged there.

    It is added for **remote** hosts only. A local server has no certificate
    and refuses the attempt, and a loopback connection is not crossing a
    network worth protecting; an explicit sslmode in the URL is always
    honoured either way, so a local server with TLS configured can still ask
    for it.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set. This project no longer opens a local "
            "SQLite file; it needs a PostgreSQL connection string, e.g.\n"
            "  export DATABASE_URL='postgresql://user:pass@ep-x-123-pooler."
            "us-east-2.aws.neon.tech/evpipeline?sslmode=require'   # Neon\n"
            "  export DATABASE_URL='postgresql://localhost/evpipeline'"
            "                        # local"
        )
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigError(
            f"DATABASE_URL must be a postgresql:// URL, got {url.split(':', 1)[0]!r}://"
        )
    host = (urlsplit(url).hostname or "").lower()
    if "sslmode=" not in url and host not in _LOCAL_HOSTS:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _configure(conn: psycopg.Connection) -> None:
    """Per-connection setup, run by the pool on every new connection."""
    conn.row_factory = row_factory
    if _is_pooled(conn.info.dsn):
        # psycopg3 silently promotes a statement to a server-side prepared
        # statement after prepare_threshold (default 5) executions. Behind
        # PgBouncer in transaction mode the next execution can land on a
        # different server session, where that statement does not exist, and
        # the query fails with "prepared statement ... does not exist" only
        # after the sixth call -- i.e. it passes every small test and breaks
        # under load. None disables promotion.
        conn.prepare_threshold = None


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=database_url(),
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            configure=_configure,
            # Fail a request rather than hang a page load behind a dead pool.
            timeout=10.0,
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Close the pool. For test teardown and clean process exit."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection, returned to the pool on exit.

    Preferred over :func:`connect` for anything request-shaped. The
    connection is NOT autocommit: psycopg3 opens a transaction on first
    execute and the caller commits, which is what lets ``write.add_company``
    make its per-field loop one atomic unit.
    """
    with pool().connection() as conn:
        yield conn


def connect(dsn: str | os.PathLike | None = None) -> psycopg.Connection:
    """Open a standalone (unpooled) connection.

    Kept because most of this repo calls ``connect()`` and then hands the
    connection around; the caller owns closing it. Pass nothing to use
    DATABASE_URL.

    A filesystem path is rejected loudly rather than coerced. Several callers
    still pass one -- ``scripts/serve.py``, ``scripts/build_ui.py``,
    ``scripts/audit_coverage.py``, ``scripts/match_drive_index.py`` -- and a
    silent failure there would look like an empty database rather than an
    unported script.
    """
    if dsn is None:
        url = database_url()
    else:
        url = str(dsn)
        if not url.startswith(("postgresql://", "postgres://")):
            raise ConfigError(
                f"connect() got what looks like a SQLite path ({url!r}). This "
                "project now uses PostgreSQL: drop the argument to use "
                "DATABASE_URL, or pass a postgresql:// URL. Callers still "
                "passing a path have not been ported yet."
            )
    conn = psycopg.connect(url, row_factory=row_factory)
    if _is_pooled(url):
        conn.prepare_threshold = None
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def create_schema(conn: psycopg.Connection) -> None:
    """Apply ``schema.sql`` then ``seed.sql``, in that order.

    Two files, not one, and the order is not cosmetic: ``seed.sql`` inserts
    into tables ``schema.sql`` creates. Both are idempotent -- every object is
    ``IF NOT EXISTS`` / ``OR REPLACE`` and every insert is ``ON CONFLICT DO
    NOTHING`` -- so this is safe to re-run against an existing branch.

    ``executescript`` is gone. psycopg3's ``execute()`` will run a
    multi-statement string, but only via the simple query protocol, which it
    uses only when no parameters are passed. Both files are parameter-free by
    construction. A ``%`` in either file would still need doubling; there is
    none today.

    Committed here rather than by the caller: a half-applied schema is not a
    useful thing to hand back, and every caller wants it durable.
    """
    for path in (SCHEMA_PATH, SEED_PATH):
        if not path.exists():
            raise ConfigError(f"missing {path.name}; expected at {path}")
        conn.execute(path.read_text())
    conn.commit()


# ---------------------------------------------------------------------------
# Ingest run log
# ---------------------------------------------------------------------------


def sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def start_run(conn: psycopg.Connection, source_file: str | os.PathLike, note: str = "") -> int:
    """Open a run-log row and return its id.

    ``RETURNING run_id`` replaces ``cur.lastrowid``.

    This commit is deliberately KEPT, and is the one exception to pulling
    commits out of this layer. The run log's purpose is to record that a load
    was attempted, including one that then crashed; holding the row in an
    uncommitted transaction that a failure rolls back would leave no evidence
    of the attempt, which is the opposite of what §9 governance asks for.
    """
    cur = conn.execute(
        "INSERT INTO ingest_run (source_file, source_sha256, schema_version, note) "
        "VALUES (%s, %s, %s, %s) RETURNING run_id",
        (str(source_file), sha256(source_file), SCHEMA_VERSION, note),
    )
    run_id = int(cur.fetchone()[0])
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, row_counts: dict) -> None:
    """Close a run-log row. Does NOT commit -- the caller owns the transaction.

    ``datetime('now')`` became ``now()``. ``row_counts`` is wrapped in
    ``Jsonb`` because the column is ``jsonb`` and psycopg3 will not infer that
    from a bare dict; the old ``json.dumps(..., sort_keys=True)`` is dropped
    because jsonb normalises key order itself, so sorting no longer buys
    diff stability.
    """
    conn.execute(
        "UPDATE ingest_run SET finished_at = now(), row_counts = %s WHERE run_id = %s",
        (Jsonb(row_counts), run_id),
    )
