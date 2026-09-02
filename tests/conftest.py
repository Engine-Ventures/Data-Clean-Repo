"""Shared fixtures. Builds the database once per test session."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evpipeline import connect, create_schema
from evpipeline.ingest import build

RAW = REPO_ROOT / "data" / "raw"
DRAFT = RAW / "EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx"
V2 = RAW / "EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx"
AFFINITY = RAW / "affinity_export_2026-09-01.csv"


@pytest.fixture(scope="session")
def report(tmp_path_factory) -> dict:
    """Build into a throwaway database; return the ingest coverage report."""
    db = tmp_path_factory.mktemp("db") / "pipeline.db"
    conn = connect(db)
    create_schema(conn)
    rep = build(conn, DRAFT, v2_path=V2, affinity_path=AFFINITY)
    conn.close()
    rep["_db_path"] = str(db)
    return rep


@pytest.fixture(scope="session")
def conn(report):
    c = connect(report["_db_path"])
    yield c
    c.close()


@pytest.fixture(scope="session", autouse=True)
def _require_sources():
    missing = [p.name for p in (DRAFT, V2, AFFINITY) if not p.exists()]
    if missing:
        pytest.skip(f"raw sources not present: {', '.join(missing)}")
