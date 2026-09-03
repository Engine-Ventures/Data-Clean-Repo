"""The verbs that turn a review proposal into a recorded decision.

Everything here is a *decision*, not a derivation. The ingest pipeline proposes
(103 merge candidates, 14 group splits, 5 duplicate listings); these functions
are how a person accepts, rejects or defers one, and they are the only sanctioned
way the population changes.

Three rules the whole module obeys:

1. **Nothing is deleted.** A merge sets `entity.merged_into` and redirects;
   a phantom is flagged. `slide_observation` is never touched, because the
   evidence log is the extraction record and corrections belong in
   `slide_observation_override`. The consequence is that every decision is
   reversible — see `unmerge`.
2. **Every decision carries who and when.** `review_item` gains a state, a
   resolver and a timestamp. A count that dropped from 498 is explainable
   three months later.
3. **One hop, never a chain.** `merge_entities` refuses to merge into an
   entity that is itself merged, which is what lets `v_observation` resolve
   with a single join rather than a recursive CTE.
"""

from __future__ import annotations

import sqlite3

from .validate import ValidationError

REVIEW_STATES = {"open", "accepted", "rejected", "deferred"}


def _entity(conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT entity_id, canonical_name, domain, merged_into, is_phantom "
        "FROM entity WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"no entity {entity_id}")
    return row


def live_count(conn: sqlite3.Connection) -> int:
    """The population as it currently stands: not merged away, not a phantom."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM entity WHERE merged_into IS NULL AND is_phantom = 0"
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_entities(
    conn: sqlite3.Connection,
    src_id: int,
    dst_id: int,
    user: str,
    note: str = "",
    review_id: int | None = None,
) -> dict:
    """Merge `src` into `dst`. Returns what changed.

    `src` keeps its row and its observations; it gains a `merged_into` pointer,
    and `v_observation` resolves through that, so every view collapses the two
    companies into one without a single row being rewritten.

    Aliases move to `dst`, because the point of the alias table is that every
    spelling ever seen on a slide resolves to the live company. After the merge
    `src`'s own name is one of those spellings.

    `review_id` is the queued proposal this merge is closing, when there is
    one — the ordinary path, since `accept_merge_proposal` is what calls this.
    Passing it means this function *updates that row* to accepted rather than
    inserting a second one, which is what happened before: every accepted
    merge left two review_item rows, the original proposal and a fresh
    "applied via the review queue" record describing the same event. Leave it
    None for a merge with no proposal behind it — two companies a person
    noticed are duplicates before ingest ever proposed it — which still needs
    its own audit row inserted from scratch.
    """
    src, dst = _entity(conn, src_id), _entity(conn, dst_id)

    if src_id == dst_id:
        raise ValidationError("cannot merge an entity into itself")
    if src["merged_into"] is not None:
        raise ValidationError(
            f"{src['canonical_name']!r} is already merged into entity "
            f"{src['merged_into']}; unmerge it first"
        )
    if dst["merged_into"] is not None:
        raise ValidationError(
            f"cannot merge into {dst['canonical_name']!r} because it is itself "
            f"merged into entity {dst['merged_into']}. Merging into a merged "
            f"entity would create a chain, and the views resolve one hop only"
        )

    moved = [
        r["alias_text"]
        for r in conn.execute("SELECT alias_text FROM alias WHERE entity_id = ?", (src_id,))
    ]
    # alias_text is globally unique, so a spelling already recorded against the
    # target is simply dropped rather than moved.
    conn.execute(
        "UPDATE OR IGNORE alias SET entity_id = ?, match_method = 'manual' "
        "WHERE entity_id = ?",
        (dst_id, src_id),
    )
    conn.execute("DELETE FROM alias WHERE entity_id = ?", (src_id,))
    conn.execute(
        "UPDATE entity SET merged_into = ? WHERE entity_id = ?", (dst_id, src_id)
    )

    detail = f"merged {src['canonical_name']!r} into {dst['canonical_name']!r}"
    if review_id is not None:
        conn.execute(
            "UPDATE review_item SET state = 'accepted', resolved_by = ?, "
            "resolved_at = datetime('now'), resolution_note = ? WHERE review_id = ?",
            (user, note or detail, review_id),
        )
    else:
        conn.execute(
            "INSERT INTO review_item (kind, entity_id, target_id, detail, proposed_by, "
            "state, resolved_by, resolved_at, resolution_note) "
            "VALUES ('merge_proposal', ?, ?, ?, ?, 'accepted', ?, datetime('now'), ?)",
            (src_id, dst_id, detail, user, user, note or "applied outside the review queue"),
        )
    conn.commit()
    return {
        "merged": src["canonical_name"],
        "into": dst["canonical_name"],
        "aliases_moved": moved,
        "live_count": live_count(conn),
    }


def unmerge(conn: sqlite3.Connection, src_id: int, user: str) -> dict:
    """Undo a merge. The reason nothing is ever deleted.

    Aliases that moved are not moved back: which spelling belongs to which
    company is a separate judgement, and guessing would be worse than leaving
    them where a person put them.
    """
    src = _entity(conn, src_id)
    if src["merged_into"] is None:
        raise ValidationError(f"{src['canonical_name']!r} is not merged")
    conn.execute("UPDATE entity SET merged_into = NULL WHERE entity_id = ?", (src_id,))
    conn.execute(
        "INSERT INTO review_item (kind, entity_id, detail, proposed_by, state, "
        "resolved_by, resolved_at, resolution_note) "
        "VALUES ('merge_proposal', ?, ?, ?, 'rejected', ?, datetime('now'), ?)",
        (
            src_id,
            f"unmerged {src['canonical_name']!r}",
            user,
            user,
            "merge reversed; aliases left where they were",
        ),
    )
    conn.commit()
    return {"unmerged": src["canonical_name"], "live_count": live_count(conn)}


# ---------------------------------------------------------------------------
# Phantoms and review state
# ---------------------------------------------------------------------------


def mark_phantom(conn: sqlite3.Connection, entity_id: int, reason: str, user: str) -> dict:
    """Flag a row that is not a company — a line-wrap fragment, an event name.

    Marked, not deleted: `Q1'26)` carries nine observations, and those pages
    are evidence about the slides even though the row is not a company.
    """
    if not reason.strip():
        raise ValidationError("a phantom needs a stated reason")
    ent = _entity(conn, entity_id)
    conn.execute(
        "UPDATE entity SET is_phantom = 1, phantom_reason = ? WHERE entity_id = ?",
        (f"{reason.strip()} (marked by {user})", entity_id),
    )
    conn.commit()
    return {
        "phantom": ent["canonical_name"],
        "reason": reason.strip(),
        "live_count": live_count(conn),
    }


def unmark_phantom(conn: sqlite3.Connection, entity_id: int, user: str) -> dict:
    ent = _entity(conn, entity_id)
    conn.execute(
        "UPDATE entity SET is_phantom = 0, phantom_reason = NULL WHERE entity_id = ?",
        (entity_id,),
    )
    conn.commit()
    return {"restored": ent["canonical_name"], "live_count": live_count(conn)}


def resolve_review(
    conn: sqlite3.Connection, review_id: int, state: str, user: str, note: str = ""
) -> dict:
    """Record a decision on one review item without acting on it.

    Distinct from accepting a merge: this is how "no, these are two different
    companies" or "come back to this" gets written down, so the queue shrinks
    as judgement is applied rather than only when data changes.
    """
    if state not in REVIEW_STATES:
        raise ValidationError(f"{state!r} is not a review state")
    row = conn.execute(
        "SELECT review_id, kind, state FROM review_item WHERE review_id = ?", (review_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"no review item {review_id}")
    conn.execute(
        "UPDATE review_item SET state = ?, resolved_by = ?, resolved_at = datetime('now'), "
        "resolution_note = ? WHERE review_id = ?",
        (state, user, note or None, review_id),
    )
    conn.commit()
    return {
        "review_id": review_id,
        "kind": row["kind"],
        "was": row["state"],
        "now": state,
        "open_items": int(
            conn.execute(
                "SELECT COUNT(*) FROM review_item WHERE state = 'open'"
            ).fetchone()[0]
        ),
    }


def accept_merge_proposal(conn: sqlite3.Connection, review_id: int, user: str) -> dict:
    """Apply a queued merge proposal and close it in one step.

    This is what the Accept button calls. The proposal already carries the
    oriented src -> target pair that ingest derived from a shared domain, so
    accepting is one decision rather than a re-entry of both ids.
    """
    row = conn.execute(
        "SELECT review_id, kind, entity_id, target_id, state FROM review_item "
        "WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"no review item {review_id}")
    if row["kind"] != "merge_proposal":
        raise ValidationError(
            f"review item {review_id} is a {row['kind']}, not a merge proposal"
        )
    if row["state"] != "open":
        raise ValidationError(f"review item {review_id} is already {row['state']}")
    if row["target_id"] is None:
        raise ValidationError(
            f"review item {review_id} has no merge target — it needs a canonical "
            f"choice before it can be applied"
        )

    # review_id=review_id: merge_entities closes this exact row rather than
    # inserting a second one alongside it, which is what left review_item at
    # 297 instead of 293 after four accepted merges.
    result = merge_entities(
        conn, int(row["entity_id"]), int(row["target_id"]), user,
        note="merge applied", review_id=review_id,
    )
    result["review_id"] = review_id
    result["open_items"] = int(
        conn.execute("SELECT COUNT(*) FROM review_item WHERE state = 'open'").fetchone()[0]
    )
    return result


# ---------------------------------------------------------------------------
# Adding a company
# ---------------------------------------------------------------------------


# Adding a company by hand lives in write.py (add_company), adopted from
# main: it does everything the version formerly here did, plus two things
# that version got wrong or lacked --
#
#   * a hand-add could claim source="Affinity"/"Slides" on a value nobody
#     extracted from either place. write.add_company restricts a hand-add to
#     {Manual, Public} (ADD_SOURCES), which this module's tests exercised as
#     valid input right up until the port -- the bug was real, not theoretical.
#   * a same-name or same-domain collision was a hard block here. write.py
#     warns and lets the caller proceed with allow_duplicate=True, filing a
#     merge_proposal for a human to resolve (§9) instead of the caller having
#     to search first and hope they searched correctly.
#
# "Nothing else inserts into entity" is write.py's own stated rule; keeping a
# second write path here would be exactly the drift that rule exists to avoid.
