"""Load the staging workbook into the data layer.

Design decisions worth knowing before reading the code:

1. The DRAFT workbook (498 rows) is treated as the *raw extraction record* and
   loaded verbatim. v2_DEDUPED's 17 row removals are imported as merge/phantom
   *proposals* in the review queue, not applied. Two reasons: §9 says fuzzy
   matches and line-wrap-looking entries go to a human rather than being
   written silently, and v2's choices are demonstrably uneven (it kept the
   line-wrapped `Artisan Insight` over `Artisan Insights`, left seven Cetos
   variants standing, and dropped `One Biosciences` with no merge target).

2. Most of the workbook's 56 columns are derived, not input. Anything
   reconstructible from the observation log — furthest_stage, reached_*,
   date_*, slide_appearances, times_discussed, enrichment_gaps, needs_* — is
   NOT stored (§9: store inputs, derive metrics). It comes back out of the
   views in schema.sql. Only ~20 genuinely-external fields get written to
   field_value, each with its source attached.

3. Affinity's `Round Size` is re-read from the CSV export to recover the 9
   genuine 0.0 values that the workbook flattened to unknown (§5).
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from pathlib import Path

import pandas as pd

from . import vocab
from .db import finish_run, start_run

INGEST_USER = "ingest"

# Columns holding genuinely external values, with the source to attribute them
# to. Everything else in the workbook is either derived (and so recomputed by
# the views) or handled specially below.
AFFINITY_FIELDS = [
    "affinity_status",
    "interest",
    "score_team",
    "score_tech",
    "score_oppt",
    "owner_name",
    "owner_email",
    "affinity_organization_id",
    "affinity_row_id",
    "affinity_date_added",
    "first_meeting",
    "last_meeting",
    "description",
    "stage",
]

# Geography and website come from Affinity unless the row was publicly
# enriched, in which case the citation in public_source applies.
DUAL_SOURCE_FIELDS = ["website", "hq_city", "hq_state_province", "hq_country", "hq_region"]

# Affinity exports these as US-format MM/DD/YYYY strings. §8 requires ISO 8601
# on write, so they are normalised at ingest rather than left for every reader
# to reparse. Month-first is unambiguous in the data (values like 01/16/2026).
DATE_FIELDS = {"affinity_date_added", "first_meeting", "last_meeting"}


def norm_name(s: str) -> str:
    """Casefold and strip punctuation/whitespace for alias comparison.

    Deliberately conservative: it collapses case and spacing but does not stem
    or drop words, so `Attune` and `Attune Tx` stay distinct. Merging those is
    a review decision, not a normalisation one.
    """
    s = str(s).strip().casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean(v):
    """Normalise workbook blanks and Excel text-guard artifacts to None."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if pd.isna(v):
        return None
    s = str(v).strip()
    # score_team/tech/oppt come out of Excel as "'+++" — the leading
    # apostrophe is a text guard, not data.
    if s.startswith("'"):
        s = s[1:].strip()
    if s == "" or s.upper() in {"NAN", "NAT", "NONE"}:
        return None
    return s


def _us_date_to_iso(v: str) -> str | None:
    """Parse Affinity's MM/DD/YYYY export format into ISO 8601."""
    try:
        return pd.to_datetime(v, format="%m/%d/%Y").date().isoformat()
    except (ValueError, TypeError):
        return _iso(v)


def _iso(v) -> str | None:
    if v is None or pd.isna(v):
        return None
    try:
        return pd.Timestamp(v).date().isoformat()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Vocabulary seeding
# ---------------------------------------------------------------------------

def seed_vocab(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO stage (stage_id, name, rank) VALUES (?, ?, ?)",
        [(rank, name, rank) for rank, name in vocab.STAGES],
    )
    conn.executemany("INSERT INTO thesis_area (code, name) VALUES (?, ?)", vocab.THESIS_AREAS)
    conn.executemany("INSERT INTO source (name, precedence) VALUES (?, ?)", vocab.SOURCES)
    conn.executemany(
        "INSERT INTO working_group (name) VALUES (?)", [(w,) for w in vocab.WORKING_GROUPS]
    )
    conn.executemany("INSERT INTO round_stage (name, rank) VALUES (?, ?)", vocab.ROUND_STAGES)
    conn.executemany(
        "INSERT INTO affinity_status (name, rank) VALUES (?, ?)", vocab.AFFINITY_STATUSES
    )
    conn.executemany(
        "INSERT INTO enrichment_priority (name, tier) VALUES (?, ?)",
        vocab.ENRICHMENT_PRIORITIES,
    )
    conn.executemany(
        "INSERT INTO slide_section_map (raw_section, stage_id, thesis_code, note) "
        "VALUES (?, ?, ?, ?)",
        [
            (raw, vocab.STAGE_BY_NAME[stage], thesis, note)
            for raw, stage, thesis, note in vocab.SLIDE_SECTION_MAP
        ],
    )
    conn.executemany(
        "INSERT INTO pass_reason_category (name, sort) VALUES (?, ?)",
        vocab.PASS_REASON_CATEGORIES,
    )
    conn.executemany(
        "INSERT INTO outcome_type (name, is_terminal) VALUES (?, ?)", vocab.OUTCOME_TYPES
    )
    conn.executemany(
        "INSERT INTO source_channel (name) VALUES (?)", [(c,) for c in vocab.SOURCE_CHANNELS]
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Entities and aliases
# ---------------------------------------------------------------------------

def load_entities(conn: sqlite3.Connection, companies: pd.DataFrame) -> dict[str, int]:
    """One entity per raw workbook row. Returns company_name -> entity_id."""
    name_to_id: dict[str, int] = {}
    for row in companies.itertuples(index=False):
        name = _clean(row.company_name)
        if name is None:
            continue
        website = _clean(row.website)
        # Domain is the primary match key (§9), so store it bare.
        domain = None
        if website:
            domain = re.sub(r"^https?://", "", website, flags=re.IGNORECASE)
            domain = re.sub(r"^www\.", "", domain, flags=re.IGNORECASE).rstrip("/").casefold()
            domain = domain.split("/")[0] or None

        cur = conn.execute(
            "INSERT INTO entity (canonical_name, domain) VALUES (?, ?)", (name, domain)
        )
        eid = int(cur.lastrowid)
        name_to_id[name] = eid

        conn.execute(
            "INSERT INTO alias (entity_id, alias_text, alias_norm, source, match_method) "
            "VALUES (?, ?, ?, 'Slides', 'exact')",
            (eid, name, norm_name(name)),
        )

        # name_variants_on_slides is pipe-delimited; these are the casing and
        # spelling variants that let the 18 unmatched slide names resolve.
        variants = _clean(row.name_variants_on_slides)
        if variants:
            for v in (x.strip() for x in variants.split("|")):
                if not v or v == name:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO alias "
                    "(entity_id, alias_text, alias_norm, source, match_method) "
                    "VALUES (?, ?, ?, 'Slides', 'manual')",
                    (eid, v, norm_name(v)),
                )
    conn.commit()
    return name_to_id


def canonical_score(name: str) -> tuple[int, int, int]:
    """Rank a spelling by how likely it is to be the company's real name.

    Used only to orient a merge proposal, never to apply one. Without this the
    direction falls out of row order, which proposes nonsense like
    `Fluent BCI -> Fluent` or `Artisan Insights -> Artisan Insight` — pointing
    the canonical at the fragment.

    Ordering, most significant first:

    1. Not visibly truncated. A name with an unbalanced bracket, a trailing
       slash or colon, or an embedded separator is a PDF artifact or a grouped
       entry, so it loses to any clean spelling. This is what keeps
       `Tulip Biosciences (Raising` from beating `Tulip Biosciences` and
       `Flume / Gravity` from beating `Gravity`.
    2. More words, then more characters — a line-wrap fragment is a truncation
       of the full name, so the longer clean form is the fuller one.
    """
    clean = 1
    if name.count("(") != name.count(")") or name.count("[") != name.count("]"):
        clean = 0
    if name.rstrip().endswith(("/", ":", ",", "-")):
        clean = 0
    if re.search(r"[A-Za-z]\s*[/,:]\s*[A-Za-z]", name):
        clean = 0
    return (clean, len(name.split()), len(name))


def _domain_conflicts(conn: sqlite3.Connection) -> None:
    """Entities sharing a domain are near-certain duplicates. Propose, don't act.

    Domain is the most stable key available (§9), so a shared domain is strong
    evidence of one company behind two spellings. The merge still goes to a
    human: the queue is the decision record, and a wrong auto-merge is far
    harder to undo than an unreviewed proposal.
    """
    rows = conn.execute(
        "SELECT domain, GROUP_CONCAT(entity_id) ids, COUNT(*) n FROM entity "
        "WHERE domain IS NOT NULL GROUP BY domain HAVING n > 1"
    ).fetchall()
    for r in rows:
        ids = [int(x) for x in r["ids"].split(",")]
        names = {
            int(x["entity_id"]): str(x["canonical_name"])
            for x in conn.execute(
                "SELECT entity_id, canonical_name FROM entity WHERE entity_id IN "
                f"({','.join('?' * len(ids))})",
                ids,
            )
        }
        keep = max(ids, key=lambda i: canonical_score(names[i]))
        for other in ids:
            if other == keep:
                continue
            conn.execute(
                "INSERT INTO review_item (kind, entity_id, target_id, detail, confidence, "
                "proposed_by) VALUES ('merge_proposal', ?, ?, ?, 0.95, ?)",
                (
                    other,
                    keep,
                    (
                        f"shares domain {r['domain']}; "
                        f"{names[keep]!r} scores as the fuller spelling"
                    ),
                    INGEST_USER,
                ),
            )
    conn.commit()


def propose_line_wraps(conn: sqlite3.Connection) -> None:
    """Flag names that look like PDF line-break fragments of another name.

    A name is a candidate if it starts with '/' (a wrapped continuation) or is
    a strict word-prefix or word-suffix of another entity's name — which is how
    `Machines` / `Adiabatic Machines`, `Fluent` / `Fluent BCI` and
    `Attune` / `Attune Tx` all arise. These go to review; §9 forbids acting on
    a name match alone.
    """
    rows = conn.execute(
        "SELECT entity_id, canonical_name FROM entity WHERE merged_into IS NULL"
    ).fetchall()
    names = {int(r["entity_id"]): str(r["canonical_name"]) for r in rows}

    for eid, name in names.items():
        if name.startswith("/"):
            conn.execute(
                "UPDATE entity SET is_phantom = 1, phantom_reason = ? WHERE entity_id = ?",
                ("leading slash: PDF line-wrap continuation", eid),
            )
            conn.execute(
                "INSERT INTO review_item (kind, entity_id, detail, confidence, proposed_by) "
                "VALUES ('line_wrap_candidate', ?, ?, 0.9, ?)",
                (eid, f"{name!r} begins with '/'; wrapped continuation", INGEST_USER),
            )
            continue

        toks = name.split()
        for other_id, other in names.items():
            if other_id == eid or len(other) <= len(name):
                continue
            o_toks = other.split()
            if len(o_toks) <= len(toks):
                continue
            is_prefix = o_toks[: len(toks)] == toks
            is_suffix = o_toks[-len(toks) :] == toks
            if is_prefix or is_suffix:
                kind = "prefix" if is_prefix else "suffix"
                conn.execute(
                    "INSERT INTO review_item (kind, entity_id, target_id, detail, "
                    "confidence, proposed_by) "
                    "VALUES ('merge_proposal', ?, ?, ?, ?, ?)",
                    (
                        eid,
                        other_id,
                        (
                            f"{name!r} is a word-{kind} of {other!r}; "
                            f"likely line-wrap fragment or truncation"
                        ),
                        0.6 if len(toks) > 1 else 0.75,
                        INGEST_USER,
                    ),
                )
    conn.commit()


def propose_groups(conn: sqlite3.Connection) -> None:
    """Flag slash/comma-joined slide entries that name several companies (§6 Q2).

    Left as one entity with a group_split review item, so the decision stays
    open. v2_DEDUPED resolved these by deletion, which loses the evidence.
    """
    rows = conn.execute(
        "SELECT entity_id, canonical_name FROM entity WHERE merged_into IS NULL"
    ).fetchall()
    for r in rows:
        name = str(r["canonical_name"])
        # A grouped entry has a separator with alphabetic content on both
        # sides, e.g. "Cetos / Kira /Eden Tech", "ScaleLight / TopoLight".
        if re.search(r"[A-Za-z]\s*[/,]\s*[A-Za-z]", name):
            parts = [p.strip(" /,:") for p in re.split(r"[/,]", name)]
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                conn.execute(
                    "INSERT INTO review_item (kind, entity_id, detail, confidence, "
                    "proposed_by) VALUES ('group_split', ?, ?, 0.7, ?)",
                    (
                        int(r["entity_id"]),
                        f"{name!r} may name {len(parts)} companies: "
                        + ", ".join(repr(p) for p in parts),
                        INGEST_USER,
                    ),
                )
    conn.commit()


def import_v2_proposals(conn: sqlite3.Connection, v2_names: set[str]) -> int:
    """Record which rows v2_DEDUPED removed, as reviewable proposals.

    v2 is a sibling artifact, not an authority: it is imported so its work is
    not lost, but every removal still needs a human decision and a stated
    merge target.
    """
    rows = conn.execute(
        "SELECT entity_id, canonical_name FROM entity WHERE merged_into IS NULL"
    ).fetchall()
    n = 0
    for r in rows:
        if str(r["canonical_name"]) not in v2_names:
            conn.execute(
                "INSERT INTO review_item (kind, entity_id, detail, confidence, proposed_by) "
                "VALUES ('merge_proposal', ?, ?, 0.5, 'v2_DEDUPED')",
                (
                    int(r["entity_id"]),
                    (
                        "removed by v2_DEDUPED without a stated merge target; "
                        "needs a canonical choice"
                    ),
                ),
            )
            n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Meetings and observations
# ---------------------------------------------------------------------------

def load_meetings(conn: sqlite3.Connection, stage_hist: pd.DataFrame) -> None:
    """Insert observed meetings, then fill absent Mondays explicitly (§9)."""
    dates = sorted({_iso(d) for d in stage_hist.meeting_date.dropna()} - {None})
    pages = (
        stage_hist.assign(d=stage_hist.meeting_date.map(_iso))
        .groupby("d")["slide_page"]
        .min()
        .to_dict()
    )
    for d in dates:
        conn.execute(
            "INSERT INTO meeting (meeting_date, status, slide_page) VALUES (?, 'held', ?)",
            (d, int(pages[d]) if d in pages and pd.notna(pages[d]) else None),
        )

    # A skipped week must not look like company attrition, so record it.
    #
    # The cadence is weekly but not a clean 7-day grid: 39 of the 43 meetings
    # fall on a Monday and 4 on a Tuesday, with observed gaps of 6, 7, 8, 14
    # and 21 days. So missing weeks are inferred only from gaps wider than 8
    # days, filling one slot per skipped week, rather than from a fixed grid
    # (which would invent absences wherever a meeting slipped a day).
    for prev, nxt in itertools.pairwise(dates):
        gap = (pd.Timestamp(nxt) - pd.Timestamp(prev)).days
        if gap <= 8:
            continue
        for k in range(1, round(gap / 7)):
            d = (pd.Timestamp(prev) + pd.Timedelta(days=7 * k)).date().isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO meeting (meeting_date, status, note) "
                "VALUES (?, 'not_extracted', ?)",
                (
                    d,
                    (
                        f"no slide extracted between {prev} and {nxt} "
                        f"({gap}-day gap); cause unconfirmed"
                    ),
                ),
            )
    conn.commit()


def load_observations(conn: sqlite3.Connection, stage_hist: pd.DataFrame) -> dict[str, int]:
    """Load the append-only evidence log, resolving slide names via aliases."""
    alias_exact = {
        str(r["alias_text"]): int(r["entity_id"])
        for r in conn.execute("SELECT alias_text, entity_id FROM alias")
    }
    alias_by_norm: dict[str, set[int]] = {}
    for r in conn.execute("SELECT alias_norm, entity_id FROM alias"):
        alias_by_norm.setdefault(str(r["alias_norm"]), set()).add(int(r["entity_id"]))

    counts = {"exact": 0, "normalised": 0, "unresolved": 0}
    unresolved: dict[str, int] = {}

    for row in stage_hist.itertuples(index=False):
        name = _clean(row.name_on_slide)
        date = _iso(row.meeting_date)
        if name is None or date is None:
            continue

        eid = alias_exact.get(name)
        method = "exact"
        if eid is None:
            cands = alias_by_norm.get(norm_name(name), set())
            if len(cands) == 1:
                eid = next(iter(cands))
                method = "normalised"
                # Record the alias so the next load resolves it exactly.
                conn.execute(
                    "INSERT OR IGNORE INTO alias "
                    "(entity_id, alias_text, alias_norm, source, match_method) "
                    "VALUES (?, ?, ?, 'Slides', 'exact')",
                    (eid, name, norm_name(name)),
                )
                alias_exact[name] = eid
        if eid is None:
            # Never invent an entity from a slide name we cannot resolve;
            # queue it and skip the row.
            counts["unresolved"] += 1
            unresolved[name] = unresolved.get(name, 0) + 1
            continue

        counts[method] += 1
        stage_name = _clean(row.stage_on_slide)
        conn.execute(
            "INSERT OR IGNORE INTO slide_observation "
            "(meeting_date, entity_id, name_on_slide, stage_id, raw_section, is_bold, "
            " bold_color, slide_page) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                date,
                eid,
                name,
                vocab.STAGE_BY_NAME[stage_name],
                _clean(row.slide_section),
                1 if bool(row.discussed_bold) else 0,
                int(row.slide_page) if pd.notna(row.slide_page) else None,
            ),
        )

    for name, n in unresolved.items():
        conn.execute(
            "INSERT INTO review_item (kind, detail, proposed_by) "
            "VALUES ('first_appearance', ?, ?)",
            (f"slide name {name!r} ({n} observation(s)) matches no entity", INGEST_USER),
        )
    conn.commit()
    return counts


def flag_duplicate_listings(conn: sqlite3.Connection) -> None:
    """Queue cases where one company appears twice on a single slide.

    Distinct from the legitimate dual-listing (agenda column + thesis
    sub-section): here the *same* company is listed twice under two spellings
    at the same meeting, e.g. `NeoLogic` and `Neologic` on 2026-02-17. The
    alias table collapses them to one entity, which is what surfaces the
    double-entry; whether the slide really listed the company twice or the
    extractor read one row twice needs a human to look at the page.
    """
    for r in conn.execute(
        """SELECT o.meeting_date, o.entity_id, o.slide_page,
                  GROUP_CONCAT(DISTINCT o.name_on_slide) names
           FROM slide_observation o
           GROUP BY o.meeting_date, o.entity_id
           HAVING COUNT(*) > 1 AND COUNT(DISTINCT o.name_on_slide) > 1"""
    ).fetchall():
        conn.execute(
            "INSERT INTO review_item (kind, entity_id, detail, proposed_by) "
            "VALUES ('duplicate_listing', ?, ?, ?)",
            (
                int(r["entity_id"]),
                (
                    f"listed twice on {r['meeting_date']} "
                    f"(slide page {r['slide_page']}) as: {r['names']}"
                ),
                INGEST_USER,
            ),
        )
    conn.commit()


def flag_stage_jumps(conn: sqlite3.Connection) -> None:
    """Queue stage jumps >2 levels and any regression (§8, §9)."""
    for r in conn.execute(
        "SELECT entity_id, from_date, to_date, delta FROM v_stage_transition "
        "WHERE delta > 2 OR delta < 0"
    ).fetchall():
        kind = "stage_jump" if r["delta"] > 0 else "stage_regression"
        conn.execute(
            "INSERT INTO review_item (kind, entity_id, detail, proposed_by) VALUES (?, ?, ?, ?)",
            (
                kind,
                int(r["entity_id"]),
                (
                    f"stage moved {r['delta']:+d} levels between "
                    f"{r['from_date']} and {r['to_date']}"
                ),
                INGEST_USER,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Field values with provenance
# ---------------------------------------------------------------------------

def _put(
    conn: sqlite3.Connection,
    eid: int,
    field: str,
    value,
    source: str,
    citation: str | None = None,
    value_num: float | None = None,
    is_zero: bool = False,
) -> int | None:
    if value is None and value_num is None and not is_zero:
        return None
    cur = conn.execute(
        "INSERT INTO field_value (entity_id, field, value_text, value_num, is_zero, "
        "source, citation, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            field,
            None if value is None else str(value),
            value_num,
            1 if is_zero else 0,
            source,
            citation,
            INGEST_USER,
        ),
    )
    return int(cur.lastrowid)


def load_field_values(
    conn: sqlite3.Connection,
    companies: pd.DataFrame,
    name_to_id: dict[str, int],
    affinity_zeros: set[str],
) -> None:
    for row in companies.itertuples(index=False):
        name = _clean(row.company_name)
        eid = name_to_id.get(name) if name else None
        if eid is None:
            continue

        # Trace back to the staging workbook. Explicitly not the entity key:
        # the brief notes EV#### is stable within one file only.
        _put(conn, eid, "legacy_company_id", _clean(row.company_id), "Slides")

        for field in AFFINITY_FIELDS:
            val = _clean(getattr(row, field))
            if val is not None and field in DATE_FIELDS:
                val = _us_date_to_iso(val)
            _put(conn, eid, field, val, "Affinity")

        publicly_enriched = bool(row.enriched_from_public)
        citation = _clean(row.public_source)
        for field in DUAL_SOURCE_FIELDS:
            val = _clean(getattr(row, field))
            if val is None or val == "UNKNOWN":
                continue
            if publicly_enriched and citation:
                _put(conn, eid, field, val, "Public", citation=citation)
            else:
                _put(conn, eid, field, val, "Affinity")

        # Working group carries its own source column (§3).
        wg = _clean(row.working_group_final)
        wg_src = _clean(row.working_group_source)
        if wg and wg_src and wg_src != "MISSING":
            _put(conn, eid, "working_group", wg,
                 "Slides" if wg_src == "Slide section" else "Affinity")

        _put(conn, eid, "thesis_area", _clean(row.thesis_area_slides), "Slides")

        # Round size: currency always stored as _usd with the local amount
        # alongside (§8). The workbook flattened Affinity's genuine 0.0 values
        # to unknown, so those are recovered from the CSV export.
        rs = row.round_size_usd
        aff_id = _clean(row.affinity_organization_id)
        if aff_id and aff_id.endswith(".0"):
            aff_id = aff_id[:-2]  # workbook stores the id as float64
        if pd.notna(rs):
            fv = _put(conn, eid, "round_size_usd", None, "Affinity", value_num=float(rs))
            if fv:
                conn.execute(
                    "INSERT INTO money_value (field_value_id, amount_usd, currency) "
                    "VALUES (?, ?, 'USD')",
                    (fv, float(rs)),
                )
        elif aff_id and aff_id in affinity_zeros:
            fv = _put(
                conn, eid, "round_size_usd", None, "Affinity", value_num=0.0, is_zero=True
            )
            if fv:
                conn.execute(
                    "INSERT INTO money_value (field_value_id, amount_usd, currency) "
                    "VALUES (?, 0.0, 'USD')",
                    (fv,),
                )
    conn.commit()


def flag_predating_relationships(conn: sqlite3.Connection) -> int:
    """Set relationship_predates_crm where first_meeting precedes the slides.

    §8 rejects a first_meeting earlier than first_slide_date unless this flag
    is set. 105 entities are in that position, some with meetings back to 2018,
    which is expected: the slide extraction window opens 2025-10-14, so any
    older relationship necessarily predates the slide record. This is exactly
    §6 Q3's point that Affinity's dates are migration timestamps and
    first_slide_date is the anchor — the flag records that the ordering is
    genuine rather than a data error.
    """
    rows = conn.execute(
        """SELECT f.entity_id FROM v_entity_funnel f
           JOIN v_field_current fm
             ON fm.entity_id = f.entity_id AND fm.field = 'first_meeting'
           WHERE fm.value_text < f.first_slide_date"""
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO field_value (entity_id, field, value_text, source, created_by) "
            "VALUES (?, 'relationship_predates_crm', '1', 'Affinity', ?)",
            (int(r["entity_id"]), INGEST_USER),
        )
    conn.commit()
    return len(rows)


def load_gap_status(
    conn: sqlite3.Connection, companies: pd.DataFrame, name_to_id: dict[str, int]
) -> None:
    """Seed three-state gap tracking from the workbook's needs_* flags.

    Everything starts at 'not_checked'. Nothing in the current data records
    that a gap was looked for and found genuinely unavailable, which is exactly
    the distinction §8 asks the worklist to make.
    """
    flags = {
        "needs_website": "website",
        "needs_geography": "hq_country",
        "needs_stage": "stage",
        "needs_round_size": "round_size_usd",
        "needs_owner": "owner_name",
    }
    for row in companies.itertuples(index=False):
        name = _clean(row.company_name)
        eid = name_to_id.get(name) if name else None
        if eid is None:
            continue
        for flag, field in flags.items():
            if bool(getattr(row, flag)):
                conn.execute(
                    "INSERT OR IGNORE INTO gap_status (entity_id, field, state) "
                    "VALUES (?, ?, 'not_checked')",
                    (eid, field),
                )
    conn.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(
    conn: sqlite3.Connection,
    draft_path: str | Path,
    v2_path: str | Path | None = None,
    affinity_path: str | Path | None = None,
) -> dict[str, int]:
    """Full load. Returns a row-count / coverage report."""
    run_id = start_run(conn, draft_path, note="initial load from staging workbook")

    companies = pd.read_excel(draft_path, sheet_name="Companies").dropna(how="all")
    stage_hist = pd.read_excel(draft_path, sheet_name="Stage History").dropna(how="all")

    affinity_zeros: set[str] = set()
    if affinity_path:
        aff = pd.read_csv(affinity_path)
        # Keyed on Organization Id, Affinity's stable external key (§9).
        # affinity_row_id is present on only 227 of the 251 Affinity-linked
        # rows, so keying on it would silently miss matches.
        affinity_zeros = {
            str(int(x))
            for x in aff.loc[aff["Round Size"] == 0, "Organization Id"].dropna()
        }

    seed_vocab(conn)
    name_to_id = load_entities(conn, companies)
    load_meetings(conn, stage_hist)
    match_counts = load_observations(conn, stage_hist)
    load_field_values(conn, companies, name_to_id, affinity_zeros)
    predating = flag_predating_relationships(conn)
    load_gap_status(conn, companies, name_to_id)

    _domain_conflicts(conn)
    propose_line_wraps(conn)
    propose_groups(conn)
    flag_duplicate_listings(conn)
    flag_stage_jumps(conn)

    v2_proposals = 0
    if v2_path:
        v2 = pd.read_excel(v2_path, sheet_name="Companies").dropna(how="all")
        v2_proposals = import_v2_proposals(
            conn, {str(n).strip() for n in v2.company_name.dropna()}
        )

    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    report = {
        "entities": one("SELECT COUNT(*) FROM entity"),
        "aliases": one("SELECT COUNT(*) FROM alias"),
        "meetings_held": one("SELECT COUNT(*) FROM meeting WHERE status='held'"),
        "meetings_missing": one("SELECT COUNT(*) FROM meeting WHERE status<>'held'"),
        "observations": one("SELECT COUNT(*) FROM slide_observation"),
        "field_values": one("SELECT COUNT(*) FROM field_value"),
        "gap_rows": one("SELECT COUNT(*) FROM gap_status"),
        "review_open": one("SELECT COUNT(*) FROM review_item WHERE state='open'"),
        "v2_removal_proposals": v2_proposals,
        "predating_relationships_flagged": predating,
        "resolved_exact": match_counts["exact"],
        "resolved_normalised": match_counts["normalised"],
        "unresolved_slide_names": match_counts["unresolved"],
    }
    finish_run(conn, run_id, report)
    return report
