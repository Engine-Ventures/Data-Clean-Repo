"""Tags: canonical on the way in, provenanced like any other field, editable."""

from __future__ import annotations

import pytest

from evpipeline import connect, create_schema, tags
from evpipeline.ingest import seed_vocab
from evpipeline.metrics import GAP_FIELDS, coverage_report, gap_counts
from evpipeline.validate import ValidationError
from evpipeline.write import add_company, read_tags, set_tags


@pytest.fixture
def db(tmp_path):
    c = connect(tmp_path / "t.db")
    create_schema(c)
    seed_vocab(c)  # round_stage etc. -- create_schema only makes the tables
    yield c
    c.close()


def _add(db, name="Acme Bio", **over):
    values = {"website": f"{name.split()[0].lower()}.com"}
    values.update(over)
    return add_company(db, name, values, "tester", tags=over.pop("tags", None))


# --- canonical form ---------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("climate, hardware", ["climate", "hardware"]),
        ("  climate ,,  hardware  ", ["climate", "hardware"]),
        # Case-insensitive de-duplication, first spelling wins.
        ("Climate, climate, CLIMATE", ["Climate"]),
        # Sorted, so re-saving the same set is not a new revision.
        ("hardware, climate", ["climate", "hardware"]),
        ("deep  tech", ["deep tech"]),
        ("", []),
        (None, []),
        ("   ,  , ", []),
    ],
)
def test_parse_is_canonical(raw, want):
    assert tags.parse(raw) == want


def test_an_overlong_tag_is_refused():
    with pytest.raises(ValidationError, match="longer than"):
        tags.parse("x" * (tags.MAX_TAG_LEN + 1))


def test_too_many_tags_are_refused():
    with pytest.raises(ValidationError, match="at most"):
        tags.parse(", ".join(f"t{i}" for i in range(tags.MAX_TAGS + 1)))


def test_vocabulary_is_one_spelling_per_tag_sorted():
    assert tags.vocabulary([["AI", "climate"], ["ai", "Robotics"]]) == [
        "AI", "climate", "Robotics"
    ]


# --- on create --------------------------------------------------------------

def test_tags_are_written_as_a_provenanced_field(db):
    r = _add(db, tags="hardware, Climate, climate")
    assert r.tags == ["Climate", "hardware"]
    row = db.execute(
        "SELECT value_text, source, created_by FROM v_field_current "
        "WHERE entity_id = ? AND field = 'tags'", (r.entity_id,)
    ).fetchone()
    assert row["value_text"] == "Climate, hardware"
    assert (row["source"], row["created_by"]) == ("Manual", "tester")


def test_tags_are_not_a_worklist_gap(db):
    """An untagged company is untagged, not incomplete."""
    r = _add(db, tags="climate")
    states = dict(
        db.execute("SELECT field, state FROM gap_status WHERE entity_id = ?", (r.entity_id,))
    )
    assert "tags" not in states
    assert set(states) == set(GAP_FIELDS)
    # The gap count and coverage denominators are untouched by tagging.
    assert gap_counts(db)[r.entity_id] == len(GAP_FIELDS) - 1  # website only
    assert "tags" not in coverage_report(db)


def test_a_bad_tag_list_is_refused_before_the_company_exists(db):
    with pytest.raises(ValidationError):
        add_company(db, "Acme", {"website": "acme.com"}, "tester", tags="y" * 200)
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


# --- editing ----------------------------------------------------------------

def test_editing_supersedes_rather_than_updates(db):
    r = _add(db, tags="climate")
    assert set_tags(db, r.entity_id, "climate, hardware", "someone") == ["climate", "hardware"]
    rows = db.execute(
        "SELECT value_text, created_by, superseded_at FROM field_value "
        "WHERE entity_id = ? AND field = 'tags' ORDER BY field_value_id", (r.entity_id,)
    ).fetchall()
    assert [x["value_text"] for x in rows] == ["climate", "climate, hardware"]
    assert rows[0]["superseded_at"] is not None
    assert rows[1]["superseded_at"] is None
    assert rows[1]["created_by"] == "someone"


def test_re_saving_the_same_set_writes_no_revision(db):
    r = _add(db, tags="climate, hardware")
    before = db.execute("SELECT COUNT(*) FROM field_value WHERE field = 'tags'").fetchone()[0]
    # Same set, different spelling of the same input.
    set_tags(db, r.entity_id, "  hardware ,climate ", "someone")
    after = db.execute("SELECT COUNT(*) FROM field_value WHERE field = 'tags'").fetchone()[0]
    assert after == before


def test_clearing_records_the_removal_instead_of_deleting_the_row(db):
    r = _add(db, tags="climate")
    assert set_tags(db, r.entity_id, "", "someone") == []
    assert read_tags(db, r.entity_id) == []
    # The history still carries that someone removed the last tag.
    assert db.execute(
        "SELECT COUNT(*) FROM field_value WHERE entity_id = ? AND field = 'tags'",
        (r.entity_id,),
    ).fetchone()[0] == 2


def test_tags_cannot_be_set_on_a_company_that_is_not_live(db):
    live = _add(db, "Acme Bio")
    dead = add_company(db, "Acme Bio", {"website": "old.com"}, "t", allow_duplicate=True)
    db.execute(
        "UPDATE entity SET merged_into = ? WHERE entity_id = ?", (live.entity_id, dead.entity_id)
    )
    db.commit()
    with pytest.raises(ValidationError, match="not a live company"):
        set_tags(db, dead.entity_id, "climate", "tester")
    with pytest.raises(ValidationError, match="not a live company"):
        set_tags(db, 9999, "climate", "tester")
