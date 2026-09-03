"""The hand-add path: duplicates warn, gaps stay three-state, writes are provenanced."""

from __future__ import annotations

import pytest

from evpipeline import connect, create_schema
from evpipeline.metrics import GAP_FIELDS
from evpipeline.validate import ValidationError
from evpipeline.write import DuplicateName, add_company, domain_from_website, find_duplicates


@pytest.fixture
def db(tmp_path):
    """An empty schema. These tests are about the write path, not the workbook."""
    c = connect(tmp_path / "t.db")
    create_schema(c)
    yield c
    c.close()


def _add(db, name="Acme Bio", **over):
    # Keyword arguments of add_company are routed there; everything else is a
    # field value, which is what most of these tests are varying.
    kwargs = {k: over.pop(k) for k in ("field_sources", "tags") if k in over}
    values = {"website": "https://www.Acme.com/team", "hq_country": "United States",
              "stage": "Seed", "owner_name": "S. Palakodety"}
    values.update(over)
    return add_company(db, name, values, "tester",
                       allow_duplicate=over.pop("dupe_ok", False), **kwargs)


# --- domain -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://www.Acme.com/team", "acme.com"),
        ("http://acme.com/", "acme.com"),
        ("ACME.CO.UK", "acme.co.uk"),
        ("", None),
        (None, None),
    ],
)
def test_domain_is_a_bare_casefolded_host(raw, want):
    assert domain_from_website(raw) == want


def test_a_new_company_requires_a_domain(db):
    with pytest.raises(ValidationError, match="requires a domain"):
        add_company(db, "Nameless", {"hq_country": "France"}, "tester")
    # Rejected before anything was inserted.
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


def test_a_website_without_a_dot_is_not_a_domain(db):
    with pytest.raises(ValidationError, match="requires a domain"):
        add_company(db, "Acme", {"website": "acme"}, "tester")


# --- the write itself -------------------------------------------------------

def test_supplied_fields_are_written_as_manual_with_the_user(db):
    r = _add(db)
    rows = {
        x["field"]: x
        for x in db.execute(
            "SELECT field, value_text, source, created_by FROM v_field_current "
            "WHERE entity_id = ?", (r.entity_id,)
        )
    }
    assert set(rows) == {"website", "hq_country", "stage", "owner_name"}
    assert all(x["source"] == "Manual" and x["created_by"] == "tester" for x in rows.values())
    assert rows["hq_country"]["value_text"] == "United States"
    # The website is stored verbatim; the entity's domain is the derived key.
    assert rows["website"]["value_text"] == "https://www.Acme.com/team"
    assert db.execute(
        "SELECT domain FROM entity WHERE entity_id = ?", (r.entity_id,)
    ).fetchone()[0] == "acme.com"


def test_the_name_is_recorded_as_an_alias(db):
    r = _add(db)
    row = db.execute(
        "SELECT alias_text, source, match_method FROM alias WHERE entity_id = ?", (r.entity_id,)
    ).fetchone()
    assert row["alias_text"] == "Acme Bio"
    assert (row["source"], row["match_method"]) == ("Manual", "manual")


def test_blank_fields_become_unchecked_gaps_not_blank_values(db):
    r = _add(db, hq_country="", owner_name="   ")
    states = dict(
        db.execute("SELECT field, state FROM gap_status WHERE entity_id = ?", (r.entity_id,))
    )
    assert set(states) == set(GAP_FIELDS)
    assert states["hq_country"] == "not_checked"
    assert states["owner_name"] == "not_checked"
    assert states["website"] == "filled"
    # round_size_usd is never asked for on the form.
    assert states["round_size_usd"] == "not_checked"
    assert not db.execute(
        "SELECT 1 FROM field_value WHERE entity_id = ? AND field = 'hq_country'", (r.entity_id,)
    ).fetchall()


def test_an_off_picklist_round_stage_is_rejected_before_the_entity_exists(db):
    with pytest.raises(ValidationError, match="round_stage picklist"):
        _add(db, stage="Series Q")
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM field_value").fetchone()[0] == 0


# --- duplicates -------------------------------------------------------------

def test_same_name_different_case_and_spacing_warns(db):
    _add(db, "Acme Bio")
    for variant in ("acme bio", "  ACME BIO  "):
        with pytest.raises(DuplicateName) as exc:
            add_company(db, variant, {"website": "other.com"}, "tester")
        assert exc.value.duplicates[0].matched_on == "name"


def test_a_different_name_on_the_same_domain_warns(db):
    _add(db, "Acme Bio")
    with pytest.raises(DuplicateName) as exc:
        add_company(db, "Acme Biosciences", {"website": "acme.com"}, "tester")
    assert exc.value.duplicates[0].matched_on == "domain"


def test_a_merely_similar_name_does_not_warn(db):
    """The check is plain equality, deliberately. Fuzzy matching is the merge
    proposals' job, where it carries a confidence score."""
    _add(db, "Acme Bio")
    r = add_company(db, "Acme Biotech", {"website": "acmebiotech.com"}, "tester")
    assert r.duplicates == []


def test_nothing_is_created_when_a_duplicate_is_refused(db):
    _add(db, "Acme Bio")
    with pytest.raises(DuplicateName):
        add_company(db, "Acme Bio", {"website": "other.com"}, "tester")
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 1


def test_confirming_a_duplicate_creates_it_and_files_a_merge_proposal(db):
    first = _add(db, "Acme Bio")
    second = add_company(
        db, "Acme Bio", {"website": "other.com"}, "tester", allow_duplicate=True
    )
    assert second.entity_id != first.entity_id
    row = db.execute(
        "SELECT kind, entity_id, target_id, state, proposed_by FROM review_item"
    ).fetchone()
    assert row["kind"] == "merge_proposal"
    assert (row["entity_id"], row["target_id"]) == (second.entity_id, first.entity_id)
    assert (row["state"], row["proposed_by"]) == ("open", "tester")


def test_a_merged_away_company_is_not_offered_as_a_duplicate(db):
    live = _add(db, "Acme Bio")
    dead = add_company(db, "Acme Bio", {"website": "old.com"}, "t", allow_duplicate=True)
    db.execute(
        "UPDATE entity SET merged_into = ? WHERE entity_id = ?",
        (live.entity_id, dead.entity_id),
    )
    db.commit()
    assert [d.entity_id for d in find_duplicates(db, "Acme Bio")] == [live.entity_id]


def test_an_empty_name_is_rejected(db):
    with pytest.raises(ValidationError, match="needs a name"):
        add_company(db, "   ", {"website": "acme.com"}, "tester")


# --- suggested values -------------------------------------------------------
#
# A value the person accepted from the public lookup is written with the source
# it came from, not laundered into Manual under their name. These are the rules
# that keep that honest; the lookup itself is exercised in test_lookup.py.

CITE = "https://www.wikidata.org/wiki/Q119718658"


def test_an_accepted_suggestion_is_written_public_with_its_citation(db):
    r = _add(db, hq_country="France", field_sources={"hq_country": ("Public", CITE)})
    rows = {
        x["field"]: x
        for x in db.execute(
            "SELECT field, source, citation, created_by FROM v_field_current "
            "WHERE entity_id = ?", (r.entity_id,)
        )
    }
    assert (rows["hq_country"]["source"], rows["hq_country"]["citation"]) == ("Public", CITE)
    # The person is still recorded: they are who accepted it.
    assert rows["hq_country"]["created_by"] == "tester"
    # Everything they typed stays Manual.
    assert rows["website"]["source"] == "Manual"
    assert rows["website"]["citation"] is None


def test_the_gap_note_says_a_value_was_accepted_rather_than_set(db):
    r = _add(db, hq_country="France", field_sources={"hq_country": ("Public", CITE)})
    notes = dict(
        db.execute("SELECT field, note FROM gap_status WHERE entity_id = ?", (r.entity_id,))
    )
    assert notes["hq_country"] == "accepted from Public on create"
    assert notes["website"] == "set on create"


def test_a_public_source_without_a_citation_is_refused(db):
    """§8's citation rule does the enforcing; nothing here bypasses it."""
    with pytest.raises(ValidationError, match="citation"):
        _add(db, hq_country="France", field_sources={"hq_country": ("Public", None)})
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


def test_an_extraction_source_cannot_be_claimed_by_a_hand_add(db):
    for bad in ("Slides", "Affinity", "made up"):
        with pytest.raises(ValidationError, match="not a source a hand-add may claim"):
            _add(db, field_sources={"hq_country": (bad, CITE)})
    assert db.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0


def test_an_override_for_a_field_nobody_filled_in_writes_nothing(db):
    r = _add(db, hq_country="", field_sources={"hq_country": ("Public", CITE)})
    assert "hq_country" not in r.written
    assert dict(
        db.execute("SELECT field, state FROM gap_status WHERE entity_id = ?", (r.entity_id,))
    )["hq_country"] == "not_checked"
