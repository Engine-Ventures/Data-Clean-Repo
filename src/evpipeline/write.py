"""Creating a company by hand, through the §8 write path.

The browser add form and any script that needs to create a company call
`add_company`; nothing else inserts into `entity`. Field values go through
`validate.write_field`, so a hand-created company carries the same provenance
columns and passes the same blocking rules as an enriched one -- there is no
second write path with its own copy of the rules to drift from.

Two things are deliberately *not* done here:

  * A name is never auto-matched (§9). A name that collides with an existing
    company produces a warning the caller must acknowledge; it does not
    silently resolve to the existing entity, and it does not block. Deciding
    that two names are one company is a merge, and merges go to the review
    queue.
  * Nothing is enriched behind the caller's back. The form may *suggest* a
    website and country from a public lookup (`lookup.py`), but a suggestion
    is only ever written because a person saw it and left it in place, and it
    is written with the source and citation it actually came from rather than
    laundered into `Manual` under their name. What the caller supplies is what
    gets written; every other worklist field is recorded as `not_checked`
    rather than left absent, so the three-state distinction (present /
    genuinely zero / unknown-unchecked) holds from the company's very first
    write instead of starting as an implicit blank.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from dataclasses import field as dc_field

from . import tags as tagsmod
from .metrics import GAP_FIELDS
from .validate import (
    ValidationError,
    _coerce,
    check_new_entity,
    resolve_gap,
    validate_field_write,
    write_field,
)

# The fields the add form offers. A subset of GAP_FIELDS, in form order.
# round_size_usd is in GAP_FIELDS but not here: it is the one worklist field
# the form does not ask for, so it is always created as an explicit gap.
ADD_FIELDS = ("website", "hq_country", "stage", "owner_name")

# Sources a hand-add may claim. 'Slides' and 'Affinity' are extraction sources
# and belong to the ingest path -- a browser write must not be able to dress
# itself up as one.
ADD_SOURCES = frozenset({"Manual", "Public"})


def domain_from_website(website: str | None) -> str | None:
    """Bare registrable host from a website value, or None.

    Domain is the primary match key (§9), so it is stored without scheme,
    without `www.`, without a path, and casefolded -- otherwise
    `https://Acme.com/` and `acme.com` are two different keys for one company.
    """
    if not website:
        return None
    d = re.sub(r"^https?://", "", str(website).strip(), flags=re.IGNORECASE)
    d = re.sub(r"^www\.", "", d, flags=re.IGNORECASE).rstrip("/").casefold()
    d = d.split("/")[0]
    return d or None


@dataclass(frozen=True)
class Duplicate:
    """An existing company the new one might already be."""

    entity_id: int
    name: str
    domain: str | None
    matched_on: str  # 'name' | 'alias' | 'domain'
    matched_value: str


@dataclass
class AddResult:
    entity_id: int
    name: str
    domain: str | None
    written: list[str] = dc_field(default_factory=list)
    gaps: list[str] = dc_field(default_factory=list)
    tags: list[str] = dc_field(default_factory=list)
    duplicates: list[Duplicate] = dc_field(default_factory=list)
    review_ids: list[int] = dc_field(default_factory=list)
    # False when the exact spelling was already an alias of another entity.
    alias_claimed: bool = True


class DuplicateName(ValidationError):
    """The name matches something already in the database.

    A ValidationError subclass so a caller that only catches ValidationError
    still stops, but a caller that wants to offer "add anyway" can catch this
    specifically and re-call with `allow_duplicate=True`.
    """

    def __init__(self, duplicates: list[Duplicate]):
        self.duplicates = duplicates
        names = ", ".join(f"{d.name} (EV{d.entity_id:04d}, on {d.matched_on})" for d in duplicates)
        super().__init__(f"name or domain already matches {len(duplicates)} company/ies: {names}")


def find_duplicates(
    conn: sqlite3.Connection, name: str, domain: str | None = None
) -> list[Duplicate]:
    """Live companies whose name, alias or domain matches.

    The name comparison is a plain lowercase/trim equality, not `norm_name`:
    it is a warning shown to a human who is about to type a company in, so it
    should fire on the case that actually happens (the same name typed twice)
    and stay quiet otherwise. Anything cleverer belongs in the merge
    proposals, which run over the whole population with a confidence score.

    The domain comparison is exact on the bare host. It is here because
    `idx_entity_domain` is deliberately non-unique and the schema's own note
    says a shared domain should surface as a merge proposal rather than be
    silently accepted or rejected -- and because a name-only check would miss
    the case this table exists for: the same company entered under a
    different spelling.
    """
    key = name.strip().lower()
    hits: dict[int, Duplicate] = {}

    for r in conn.execute(
        "SELECT entity_id, canonical_name, domain FROM entity WHERE merged_into IS NULL"
    ):
        if str(r["canonical_name"]).strip().lower() == key:
            eid = int(r["entity_id"])
            hits[eid] = Duplicate(
                eid, r["canonical_name"], r["domain"], "name", r["canonical_name"]
            )

    for r in conn.execute(
        """SELECT a.alias_text, e.entity_id, e.canonical_name, e.domain
           FROM alias a JOIN entity e ON e.entity_id = a.entity_id
           WHERE e.merged_into IS NULL"""
    ):
        eid = int(r["entity_id"])
        if eid in hits:
            continue
        if str(r["alias_text"]).strip().lower() == key:
            hits[eid] = Duplicate(eid, r["canonical_name"], r["domain"], "alias", r["alias_text"])

    if domain:
        for r in conn.execute(
            "SELECT entity_id, canonical_name, domain FROM entity "
            "WHERE merged_into IS NULL AND domain = ?",
            (domain,),
        ):
            eid = int(r["entity_id"])
            if eid not in hits:
                hits[eid] = Duplicate(eid, r["canonical_name"], r["domain"], "domain", domain)

    return sorted(hits.values(), key=lambda d: d.entity_id)


def add_company(
    conn: sqlite3.Connection,
    name: str,
    values: dict[str, str],
    user: str,
    *,
    allow_duplicate: bool = False,
    source: str = "Manual",
    field_sources: dict[str, tuple[str, str | None]] | None = None,
    tags: str | None = None,
) -> AddResult:
    """Create one company and write the fields it was given.

    `values` holds raw strings keyed by field name; empty and missing values
    are treated identically (the field becomes an unchecked gap) rather than
    written as NULL, because blank means unknown and a blank write would
    claim the field had been looked at.

    `field_sources` overrides the source (and citation) for individual fields:
    `{"hq_country": ("Public", "https://www.wikidata.org/wiki/Q42")}`. It
    exists for the add form's suggestions -- a value the person accepted
    unedited came from the lookup, not from them, and recording it as `Manual`
    would put their name on a fact they never checked. An edited value comes
    back with no override and is written `Manual`, which is then true. The §8
    citation-required rule is untouched and does the enforcing: a `Public`
    write with no citation is rejected here exactly as anywhere else.

    `tags` is a comma-separated list; see `evpipeline.tags`. It is written as
    an ordinary provenanced field and is not a worklist gap.

    Raises DuplicateName unless `allow_duplicate` is set. When it *is* set and
    matches exist, each one is filed as an open merge_proposal: the company is
    created, and the question of whether it is really a new company is left
    for the review queue instead of being answered here (§9).
    """
    name = " ".join(str(name).split())
    if not name:
        raise ValidationError("a company needs a name")

    supplied = {
        f: str(values.get(f, "") or "").strip()
        for f in ADD_FIELDS
        if str(values.get(f, "") or "").strip()
    }

    sources = {f: (source, None) for f in supplied}
    for f, (src, cite) in (field_sources or {}).items():
        if f not in supplied:
            continue  # an override for a field nobody filled in writes nothing
        if src not in ADD_SOURCES:
            raise ValidationError(
                f"{src!r} is not a source a hand-add may claim "
                f"({', '.join(sorted(ADD_SOURCES))})"
            )
        sources[f] = (src, cite or None)

    tag_text = tagsmod.canonical(tags)

    domain = domain_from_website(supplied.get("website"))
    # §8: a new record requires a domain. The form's website field is the only
    # place one can come from, so this is what makes website effectively
    # required on the add form rather than optional like the other three.
    check_new_entity(domain)

    dupes = find_duplicates(conn, name, domain)
    if dupes and not allow_duplicate:
        raise DuplicateName(dupes)

    # Validate every value before creating anything. write_field commits per
    # field, so a value rejected halfway through would otherwise leave a
    # half-populated company behind. None of the rules that apply to these
    # four fields read entity_id (that is only check_first_meeting_order, and
    # first_meeting is not on this form), so validating against the
    # not-yet-existing id is safe here and asserted in the tests.
    coerced: list[tuple[str, str | None, float | None, bool]] = []
    for f, raw in supplied.items():
        text, num, is_zero = _coerce(f, raw)
        src, cite = sources[f]
        validate_field_write(
            conn, 0, f, text, src, citation=cite, value_num=num, is_zero=is_zero
        )
        coerced.append((f, text, num, is_zero))
    if tag_text:
        validate_field_write(conn, 0, tagsmod.FIELD, tag_text, source)

    # Entity and alias go in together or not at all: alias_text is globally
    # UNIQUE, so a confirmed same-name duplicate would otherwise commit an
    # entity and then fail, leaving a company with no alias row behind.
    try:
        cur = conn.execute(
            "INSERT INTO entity (canonical_name, domain) VALUES (?, ?)", (name, domain)
        )
        entity_id = int(cur.lastrowid)
        # OR IGNORE, because that same UNIQUE means a spelling belongs to
        # exactly one entity. When the name is already claimed -- only
        # reachable via allow_duplicate -- the older entity keeps it and this
        # one has none, which is the honest state: the merge proposal filed
        # below is what decides who the spelling really refers to.
        conn.execute(
            "INSERT OR IGNORE INTO alias "
            "(entity_id, alias_text, alias_norm, source, match_method, confidence) "
            "VALUES (?, ?, ?, ?, 'manual', 1.0)",
            (entity_id, name, _norm_for_alias(name), source),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    result = AddResult(
        entity_id=entity_id,
        name=name,
        domain=domain,
        duplicates=dupes,
        alias_claimed=bool(
            conn.execute(
                "SELECT 1 FROM alias WHERE entity_id = ? AND alias_text = ?", (entity_id, name)
            ).fetchone()
        ),
    )

    for f, text, num, is_zero in coerced:
        src, cite = sources[f]
        write_field(
            conn, entity_id, f, text, src, user,
            citation=cite, value_num=num, is_zero=is_zero,
        )
        resolve_gap(
            conn, entity_id, f, "filled", user,
            note="set on create" if src == "Manual" else f"accepted from {src} on create",
        )
        result.written.append(f)

    # After the gap loop's fields, and outside it: tags resolve no gap, because
    # an untagged company is not missing anything.
    if tag_text:
        write_field(conn, entity_id, tagsmod.FIELD, tag_text, source, user)
        result.written.append(tagsmod.FIELD)
        result.tags = tagsmod.parse(tag_text)

    for f in GAP_FIELDS:
        if f in supplied:
            continue
        resolve_gap(conn, entity_id, f, "not_checked", user, note="not asked on create")
        result.gaps.append(f)

    for d in dupes:
        cur = conn.execute(
            "INSERT INTO review_item (kind, entity_id, target_id, detail, proposed_by) "
            "VALUES ('merge_proposal', ?, ?, ?, ?)",
            (
                entity_id,
                d.entity_id,
                (
                    f"{name!r} was added by hand and matches existing "
                    f"{d.name!r} on {d.matched_on} ({d.matched_value!r}); "
                    f"the person adding it chose to proceed. Merge or reject."
                ),
                user,
            ),
        )
        result.review_ids.append(int(cur.lastrowid))
    if dupes:
        conn.commit()

    return result


def read_tags(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    """The tags currently on one company."""
    row = conn.execute(
        "SELECT value_text FROM v_field_current WHERE entity_id = ? AND field = ?",
        (entity_id, tagsmod.FIELD),
    ).fetchone()
    return tagsmod.parse(row[0]) if row else []


def set_tags(
    conn: sqlite3.Connection, entity_id: int, raw: str | None, user: str, *, source: str = "Manual"
) -> list[str]:
    """Replace one company's tags, and return the canonical list.

    The only field this module lets a browser edit after creation. It is safe
    to open where the enrichment fields are not because a tag asserts nothing
    about the company -- it is the team's own label, so there is no external
    fact for a careless edit to contradict and nothing for the review queue to
    adjudicate. Editing `hq_country` in the same way would need a provenance
    conversation this does not.

    Clearing the tags writes an empty string rather than deleting the row:
    field_value is append-only, and "someone removed the last tag" is a fact
    the history should carry. `parse` reads that back as no tags.
    """
    row = conn.execute(
        "SELECT 1 FROM entity WHERE entity_id = ? AND merged_into IS NULL", (entity_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"EV{entity_id:04d} is not a live company")

    text = tagsmod.canonical(raw)
    if text == tagsmod.format(read_tags(conn, entity_id)):
        return tagsmod.parse(text)  # unchanged; no revision that says nothing

    write_field(conn, entity_id, tagsmod.FIELD, text, source, user)
    return tagsmod.parse(text)


def _norm_for_alias(name: str) -> str:
    """alias_norm for a hand-added name.

    Imported lazily from ingest so the alias table's normal form has exactly
    one definition; importing at module scope would pull pandas into the
    server process for a one-line function.
    """
    from .ingest import norm_name

    return norm_name(name)
