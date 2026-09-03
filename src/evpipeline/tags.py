"""Free-text tags, stored as one comma-separated `tags` field per company.

Deliberately not a table. `field_value` is already the schema's generic
per-entity key/value store with provenance and supersede-don't-update history,
and `field_value.field` carries no CHECK constraint -- so a tag list written
there gets the §8 write path, the append-only revision history and the drawer's
provenance row for free. A `tag` / `entity_tag` pair of tables would buy
referential integrity over a vocabulary that is, by request, open: anyone can
coin a tag by typing it. The suggestion list is therefore derived from what is
in use (see `vocabulary`) rather than stored as a controlled picklist, which is
the honest representation -- there is no authority that blesses a tag.

Tags are NOT in `GAP_FIELDS`. A company with no tags is untagged, not missing
data, and putting them on the worklist would move every gap denominator off /5
and make the coverage numbers mean something different than they did.

One canonical spelling per set: trimmed, internal whitespace collapsed,
de-duplicated case-insensitively (first spelling wins) and sorted. Canonical
form is what makes a re-save of the same set a no-op instead of a new
`field_value` revision that says nothing changed.
"""

from __future__ import annotations

from .validate import ValidationError

FIELD = "tags"

# Bounds, so a paste accident cannot write a novel into one field. Both are
# generous relative to how the field is meant to be used; they exist to fail
# loudly rather than to shape usage.
MAX_TAG_LEN = 48
MAX_TAGS = 24


class TagError(ValidationError):
    """A tag list that cannot be stored as written.

    A ValidationError so a bad tag list is refused by the same handler as
    every other §8 rejection, rather than escaping as a 500.
    """


def parse(raw: str | None) -> list[str]:
    """Canonical tag list from a comma-separated string.

    Empty and whitespace-only input is an empty list, which is how "this
    company has no tags" is expressed -- there is no separate unknown state,
    because unlike an enrichment field a tag is not something you go and look
    up and either find or fail to find.
    """
    if not raw:
        return []
    seen: dict[str, str] = {}
    for part in str(raw).split(","):
        tag = " ".join(part.split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise TagError(f"{tag[:MAX_TAG_LEN]!r}... is longer than {MAX_TAG_LEN} characters")
        seen.setdefault(tag.casefold(), tag)
    if len(seen) > MAX_TAGS:
        raise TagError(f"{len(seen)} tags; at most {MAX_TAGS} on one company")
    return [seen[k] for k in sorted(seen)]


def format(tags: list[str]) -> str:
    """The stored text for a canonical tag list."""
    return ", ".join(tags)


def canonical(raw: str | None) -> str:
    """`parse` then `format`: the exact text a write should store."""
    return format(parse(raw))


def vocabulary(all_tags: list[list[str]]) -> list[str]:
    """The suggestion list: every tag in use, one spelling each, sorted.

    Derived rather than stored. A tag exists because a company carries it, so
    the moment the last company drops a tag it stops being suggested -- there
    is no orphaned vocabulary to prune, and no way for the suggestions to offer
    something that would match nothing.
    """
    seen: dict[str, str] = {}
    for tags in all_tags:
        for t in tags:
            seen.setdefault(t.casefold(), t)
    return [seen[k] for k in sorted(seen)]
