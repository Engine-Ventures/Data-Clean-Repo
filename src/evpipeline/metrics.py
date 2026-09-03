"""Derived metrics. Nothing here is stored — §9: store inputs, derive metrics.

Every count-returning function also returns its coverage, because §8 requires
coverage to be shown next to any count and UNKNOWN treated as a gap rather
than a category.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import psycopg

# Fields the enrichment worklist tracks, in the order the workbook used.
#
# Note on "stage": this is the ROUND stage (Seed, Series A, ...) as Affinity
# records it, not the funnel stage on the slides. The two are different axes
# and the workbook's column name for the former is `stage`, so that is the
# field name in field_value. Slide position is stage_id on slide_observation
# and never appears in field_value.
GAP_FIELDS = ["website", "hq_country", "stage", "round_size_usd", "owner_name"]

# The fields reported per-cohort. A superset of GAP_FIELDS: the worklist only
# ever tracked five, but coverage is a fair question about any of them, and the
# three score_* columns and affinity_status are the ones that decide whether
# Affinity can answer a diligence question at all.
#
# Only the five in GAP_FIELDS have gap_status rows, so only those five can
# distinguish "checked, genuinely unavailable" from "never looked at". For the
# rest every absence reports as not_checked, which is accurate: nothing has
# recorded a check either way.
REPORT_FIELDS = [
    "website",
    "hq_country",
    "hq_region",
    "stage",
    "round_size_usd",
    "owner_name",
    "affinity_status",
    "score_team",
    "score_tech",
    "score_oppt",
]


@dataclass(frozen=True)
class Coverage:
    """A count with its denominator, so it can never be quoted bare.

    ``confirmed_unavailable`` splits the missing side into the two states §8
    requires be kept apart: a gap somebody checked and found genuinely
    unavailable is closed, and a gap nobody has looked at is work. Only the
    second is actionable, so only the second should ever be quoted as the
    number of gaps to fill.

    It defaults to 0, which is also what a field with no gap_status rows
    truthfully reports: nothing has been confirmed unavailable, so everything
    missing is unchecked.
    """

    present: int
    total: int
    confirmed_unavailable: int = 0

    @property
    def pct(self) -> float:
        return 0.0 if self.total == 0 else self.present / self.total

    @property
    def missing(self) -> int:
        return self.total - self.present

    @property
    def not_checked(self) -> int:
        """The actionable gap: missing, and not confirmed unavailable."""
        return self.missing - self.confirmed_unavailable

    def __str__(self) -> str:
        return f"{self.present}/{self.total} ({self.pct:.1%})"


def _live_entity_filter(include_phantoms: bool) -> str:
    """Live entities only: not merged away, and optionally excluding phantoms."""
    clause = "e.merged_into IS NULL"
    if not include_phantoms:
        clause += " AND e.is_phantom = 0"
    return clause


def entity_count(conn: psycopg.Connection, include_phantoms: bool = True) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM entity e WHERE {_live_entity_filter(include_phantoms)}"
        ).fetchone()[0]
    )


def field_coverage(
    conn: psycopg.Connection, field: str, cohort_sql: str | None = None
) -> Coverage:
    """Coverage of one field, optionally within a cohort of entity_ids.

    A value counts as present if a current field_value row exists. A confirmed
    genuine zero counts as present; an unknown does not. That is the three-state
    rule the schema encodes.

    The missing side is split by gap_status: a row in state
    'confirmed_unavailable' is a closed gap, anything else missing is
    unchecked. `filled` and `not_checked` rows are both ignored here -- the
    presence of a field_value row is the authority on whether a value exists,
    so a stale 'filled' gap_status row cannot inflate coverage.
    """
    scope = f" AND e.entity_id IN ({cohort_sql})" if cohort_sql else ""
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM entity e WHERE e.merged_into IS NULL{scope}"
        ).fetchone()[0]
    )
    present = int(
        conn.execute(
            f"""SELECT COUNT(DISTINCT e.entity_id) FROM entity e
                JOIN v_field_current fc ON fc.entity_id = e.entity_id
                WHERE e.merged_into IS NULL AND fc.field = %s
                  AND (fc.value_text IS NOT NULL OR fc.value_num IS NOT NULL){scope}""",
            (field,),
        ).fetchone()[0]
    )
    # Counted only where no current value exists, so present and
    # confirmed_unavailable can never overlap and double-count an entity.
    confirmed = int(
        conn.execute(
            f"""SELECT COUNT(DISTINCT e.entity_id) FROM entity e
                JOIN gap_status g ON g.entity_id = e.entity_id
                WHERE e.merged_into IS NULL AND g.field = %s
                  AND g.state = 'confirmed_unavailable'
                  AND NOT EXISTS (
                      SELECT 1 FROM v_field_current fc
                      WHERE fc.entity_id = e.entity_id AND fc.field = %s
                        AND (fc.value_text IS NOT NULL OR fc.value_num IS NOT NULL)
                  ){scope}""",
            (field, field),
        ).fetchone()[0]
    )
    return Coverage(present, total, confirmed)


def furthest_stage_distribution(conn: psycopg.Connection) -> dict[str, int]:
    return {
        str(r[0]): int(r[1])
        for r in conn.execute(
            # furthest_stage_id is in the GROUP BY because it is in the ORDER
            # BY: SQLite allowed ordering a grouped query by an ungrouped
            # column, Postgres does not. Grouping by it as well changes no
            # counts -- it is functionally dependent on s.name, both sides of
            # a join on equality.
            """SELECT s.name, COUNT(*) FROM v_entity_funnel f
               JOIN stage s ON s.stage_id = f.furthest_stage_id
               GROUP BY s.name, f.furthest_stage_id
               ORDER BY f.furthest_stage_id DESC"""
        )
    }


def funnel_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Both semantics side by side (see the v_entity_funnel comment)."""
    row = conn.execute(
        """SELECT SUM(observed_at_prelim_diligence) obs_prelim,
                  SUM(observed_at_deep_diligence)   obs_deep,
                  SUM(observed_at_negotiate_offer)  obs_negotiate,
                  SUM(observed_at_legal)            obs_legal,
                  SUM(reached_prelim_diligence)     reached_prelim,
                  SUM(reached_deep_diligence)       reached_deep,
                  SUM(reached_negotiate_offer)      reached_negotiate,
                  SUM(reached_legal)                reached_legal
           FROM v_entity_funnel"""
    ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}  # noqa: SIM118


def discussion_counts(conn: psycopg.Connection) -> dict[str, int]:
    row = conn.execute(
        """SELECT COUNT(*) FILTER (WHERE discussed = 1) entities_discussed,
                  COALESCE(SUM(times_discussed), 0)     bold_appearances
           FROM v_entity_discussion"""
    ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}  # noqa: SIM118


def dwell_medians(conn: psycopg.Connection) -> dict[str, float]:
    """Median length, in meetings held, of one dwell spell at each stage.

    v_dwell is now one row per SPELL rather than per (entity, stage), so this
    is a median over spells and finally means what the name says. It is not
    comparable to what this function returned before the v_dwell fix, which
    was a median of per-stage totals: an entity that visited a stage twice
    contributed one inflated number there and contributes two honest ones now.
    """
    by_stage: dict[str, list[int]] = {}
    for r in conn.execute(
        """SELECT s.name, d.meetings_at_stage FROM v_dwell d
           JOIN stage s ON s.stage_id = d.stage_id"""
    ):
        by_stage.setdefault(str(r[0]), []).append(int(r[1]))
    return {k: statistics.median(v) for k, v in sorted(by_stage.items())}


def gap_counts(conn: psycopg.Connection) -> dict[int, int]:
    """entity_id -> number of the five worklist fields still unknown."""
    out: dict[int, int] = {}
    for r in conn.execute("SELECT entity_id FROM entity WHERE merged_into IS NULL"):
        out[int(r[0])] = 0
    placeholders = ",".join(["%s"] * len(GAP_FIELDS))
    have: dict[int, set[str]] = {}
    for r in conn.execute(
        f"""SELECT entity_id, field FROM v_field_current
            WHERE field IN ({placeholders})
              AND (value_text IS NOT NULL OR value_num IS NOT NULL)""",
        GAP_FIELDS,
    ):
        have.setdefault(int(r[0]), set()).add(str(r[1]))
    for eid in out:
        out[eid] = len(GAP_FIELDS) - len(have.get(eid, set()))
    return out


def enrichment_priority(conn: psycopg.Connection) -> dict[int, str]:
    """Derive the P1-P4 worklist ranking (§4) instead of storing it.

    The rule, reverse-engineered from the staging workbook and reproducing its
    distribution exactly (6 / 8 / 68 / 190 / 226):

      no gaps                              -> P4
      furthest stage >= Deep Diligence     -> P1 advanced stage
      discussed in a meeting               -> P1 discussed
      furthest stage == Prelim Dil, >=3 gaps -> P2
      >= 4 gaps                            -> P3
      otherwise                            -> P4
    """
    gaps = gap_counts(conn)
    funnel = {
        int(r["entity_id"]): int(r["furthest_stage_id"])
        for r in conn.execute("SELECT entity_id, furthest_stage_id FROM v_entity_funnel")
    }
    discussed = {
        int(r["entity_id"])
        for r in conn.execute("SELECT entity_id FROM v_entity_discussion WHERE discussed = 1")
    }

    out: dict[int, str] = {}
    for eid, n in gaps.items():
        stage = funnel.get(eid, 0)
        if n == 0:
            out[eid] = "P4 - acceptable"
        elif stage >= 5:
            out[eid] = "P1 - advanced stage, incomplete"
        elif eid in discussed:
            out[eid] = "P1 - discussed, incomplete"
        elif stage == 4 and n >= 3:
            out[eid] = "P2 - prelim dil, mostly empty"
        elif n >= 4:
            out[eid] = "P3 - sparse record"
        else:
            out[eid] = "P4 - acceptable"
    return out


def reconciliation(conn: psycopg.Connection) -> list[str]:
    """Block-total vs cohort-total checks (§8). Returns warnings, empty if clean."""
    warnings: list[str] = []

    entities = entity_count(conn)
    funnel_total = int(conn.execute("SELECT COUNT(*) FROM v_entity_funnel").fetchone()[0])
    dist_total = sum(furthest_stage_distribution(conn).values())
    if dist_total != funnel_total:
        warnings.append(
            f"furthest-stage distribution sums to {dist_total} "
            f"but {funnel_total} entities have observations"
        )
    if funnel_total > entities:
        warnings.append(
            f"{funnel_total} entities have observations but only {entities} entities exist"
        )

    # Every entity that reached a stage must have been observed at or past it.
    bad = int(
        conn.execute(
            """SELECT COUNT(*) FROM v_entity_funnel
               WHERE reached_deep_diligence < observed_at_deep_diligence
                  OR reached_legal < observed_at_legal"""
        ).fetchone()[0]
    )
    if bad:
        warnings.append(f"{bad} entities are observed at a stage they did not reach")

    # An entity with no live observations but a funnel row means a broken merge.
    orphan = int(
        conn.execute(
            """SELECT COUNT(*) FROM v_entity_funnel f
               LEFT JOIN entity e ON e.entity_id = f.entity_id
               WHERE e.entity_id IS NULL OR e.merged_into IS NOT NULL"""
        ).fetchone()[0]
    )
    if orphan:
        warnings.append(f"{orphan} funnel rows point at a merged or missing entity")

    # Observations must never fall on a week recorded as not held.
    ghost = int(
        conn.execute(
            """SELECT COUNT(*) FROM slide_observation o
               JOIN meeting m ON m.meeting_date = o.meeting_date
               WHERE m.status <> 'held'"""
        ).fetchone()[0]
    )
    if ghost:
        warnings.append(f"{ghost} observations fall on a meeting not marked held")

    return warnings


def coverage_report(conn: psycopg.Connection) -> dict[str, Coverage]:
    """Coverage across the worklist fields, plus the advanced-stage cohort."""
    advanced = "SELECT entity_id FROM v_entity_funnel WHERE observed_at_deep_diligence = 1"
    report = {f: field_coverage(conn, f) for f in GAP_FIELDS}
    report["hq_country @ deep-diligence cohort"] = field_coverage(
        conn, "hq_country", cohort_sql=advanced
    )
    report["stage @ discussed cohort"] = field_coverage(
        conn, "stage", cohort_sql="SELECT entity_id FROM v_entity_discussion WHERE discussed = 1"
    )
    return report


# The diligence cohort is a membership list, not a rule. See
# cohort_coverage_report.
DILIGENCE_COHORT_SQL = """
    SELECT DISTINCT a.entity_id FROM alias a
    JOIN diligence_cohort dc ON dc.alias_norm = a.alias_norm
"""


def cohort_coverage_report(
    conn: psycopg.Connection,
    fields: list[str] | None = None,
    cohort_sql: str = DILIGENCE_COHORT_SQL,
) -> dict[str, Coverage]:
    """Per-field coverage within a cohort, three-state split.

    Returns the same :class:`Coverage` objects as :func:`coverage_report`, so
    ``present`` / ``confirmed_unavailable`` / ``not_checked`` read off one
    convention rather than a parallel one. ``not_checked`` is the only
    actionable number of the three.

    The cohort is resolved by joining a ``diligence_cohort`` staging table on
    ``alias_norm``, NOT by a stage predicate, because the 185-company diligence
    cohort is not reproducible from the observation log: it is a curated list
    delivered as its own workbook. The nearest rule -- reached Preliminary
    Diligence -- selects 191 entities here and 182 of the cohort's own 185
    rows, so no threshold recovers it. Any report over this cohort therefore
    carries a name-matching error band, and the caller is expected to state it.
    """
    return {f: field_coverage(conn, f, cohort_sql=cohort_sql) for f in (fields or REPORT_FIELDS)}
