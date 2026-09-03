"""One public lookup to prefill website and country on the add form.

Wikidata, not a general web search. The reason is provenance: §8 requires a
citation on anything written with source `Public`, and a search engine's first
result is a page, not a fact -- turning it into "the HQ country is France"
means scraping prose and guessing. Wikidata answers the two questions this form
asks as typed properties (P856 official website, P17 country / P159 headquarters
location) on an entity with a permanent URL, so the value and the citation come
out of the same request and the citation actually supports the value.

The honest limitation, and it is a big one: Wikidata's coverage of
early-stage private companies is thin. Most of what this pipeline tracks will
not be in it, and this returns nothing for those -- which is the correct
outcome. A suggestion that is absent costs a person nothing; a suggestion
invented from a name-similar public company costs them a wrong country on a
deal record. Everything here is therefore biased hard toward returning None:
the name match must be a normalised exact match, not the search API's top hit.

No API key and no dependency -- stdlib urllib against the public endpoint.
Nothing here writes; it returns a suggestion for a person to accept or edit.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://www.wikidata.org/w/api.php"
ENTITY_URL = "https://www.wikidata.org/wiki/{}"
USER_AGENT = "evpipeline-workbench/1.0 (internal deal pipeline tool)"

# Wall-clock budget for the whole lookup. The form waits on this, so it is
# short: a slow suggestion is worse than no suggestion, because the person is
# sitting in front of an empty field either way.
TIMEOUT = 4.0

P_WEBSITE = "P856"
P_COUNTRY = "P17"
P_HEADQUARTERS = "P159"
P_INSTANCE_OF = "P31"

# Suffixes people drop when they say a company's name. Stripped from both
# sides before comparing, so "Acme" matches Wikidata's "Acme, Inc.".
_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|ltd|limited|corp|corporation|co|gmbh|ag|sa|sas|bv|nv|oy|ab|plc|"
    r"pty|srl|spa|kk|pte|holdings?|group|technologies|technology|labs?|the)\b"
)


@dataclass(frozen=True)
class Suggestion:
    """What one lookup found. Any field may be None; all of them usually are."""

    website: str | None = None
    hq_country: str | None = None
    citation: str | None = None      # the Wikidata entity URL both values came from
    label: str | None = None         # Wikidata's own name for the entity
    note: str | None = None          # why there is nothing, when there is nothing

    @property
    def empty(self) -> bool:
        return self.website is None and self.hq_country is None

    def as_dict(self) -> dict:
        return {
            "website": self.website,
            "hq_country": self.hq_country,
            "citation": self.citation,
            "label": self.label,
            "note": self.note,
        }


def _norm(s: str) -> str:
    """Comparison form for a company name: fold accents, drop legal suffixes."""
    s = unicodedata.normalize("NFKD", str(s)).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return " ".join(s.split())


def _get(params: dict[str, str]) -> dict:
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # Fixed https host; params are urlencoded, so the scheme cannot be redirected here.
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _search(name: str) -> str | None:
    """The one Wikidata id whose label or alias *is* this name, or None.

    The search API is a ranked full-text index, so its top hit for a startup
    name is frequently an unrelated public company that happens to share a
    word. Requiring a normalised exact match against the returned label or
    alias throws those away.
    """
    data = _get({
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "uselang": "en",
        "type": "item",
        "limit": "10",
    })
    want = _norm(name)
    if not want:
        return None
    for hit in data.get("search", []):
        candidates = [hit.get("label") or "", *(hit.get("aliases") or [])]
        if any(_norm(c) == want for c in candidates):
            return hit.get("id")
    return None


def _claim_ids(claims: dict, prop: str) -> list[str]:
    """The item ids a wikibase-item property points at, best rank first."""
    out = []
    for c in claims.get(prop, []):
        val = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(val, dict) and val.get("id"):
            out.append(val["id"])
    return out


def _claim_strings(claims: dict, prop: str) -> list[str]:
    out = []
    for c in claims.get(prop, []):
        val = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(val, str):
            out.append(val)
    return out


def _labels(ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    data = _get({
        "action": "wbgetentities",
        "ids": "|".join(ids[:20]),
        "props": "labels",
        "languages": "en",
    })
    out = {}
    for qid, ent in (data.get("entities") or {}).items():
        label = ((ent.get("labels") or {}).get("en") or {}).get("value")
        if label:
            out[qid] = label
    return out


def suggest(name: str) -> Suggestion:
    """Look `name` up once and return what can be prefilled.

    Never raises: a lookup that fails for any reason (offline, rate-limited,
    malformed response) is reported as an empty suggestion with a note, because
    the add form must stay usable with no network at all. The form's own
    validation is unchanged either way -- nothing here bypasses it, and nothing
    here is written without a person leaving it in place.
    """
    name = " ".join(str(name or "").split())
    if not name:
        return Suggestion(note="no name to look up")

    try:
        qid = _search(name)
        if qid is None:
            return Suggestion(note=f"no Wikidata entity is named exactly {name!r}")

        data = _get({
            "action": "wbgetentities",
            "ids": qid,
            "props": "claims|labels",
            "languages": "en",
        })
        ent = (data.get("entities") or {}).get(qid) or {}
        claims = ent.get("claims") or {}
        label = ((ent.get("labels") or {}).get("en") or {}).get("value")

        website = next(iter(_claim_strings(claims, P_WEBSITE)), None)

        # Country of the headquarters if it has one, else the entity's own
        # country. HQ is the more specific claim and the one the field means.
        country_ids: list[str] = []
        for hq in _claim_ids(claims, P_HEADQUARTERS):
            country_ids.extend(_claim_ids((_entity_claims(hq) or {}), P_COUNTRY))
            if country_ids:
                break
        country_ids.extend(_claim_ids(claims, P_COUNTRY))

        # instance_of goes along so the caller can show what Wikidata thinks
        # this is -- the cheapest way for a person to spot a wrong match.
        kind_ids = _claim_ids(claims, P_INSTANCE_OF)[:2]
        names = _labels(country_ids[:1] + kind_ids)

        country = names.get(country_ids[0]) if country_ids else None
        kinds = [names[k] for k in kind_ids if k in names]

        sug = Suggestion(
            website=website,
            hq_country=country,
            citation=ENTITY_URL.format(qid),
            label=label,
            note=(", ".join(kinds) or None),
        )
        if sug.empty:
            return Suggestion(
                citation=ENTITY_URL.format(qid),
                label=label,
                note=f"{label or qid} is on Wikidata but records no website or country",
            )
        return sug
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Suggestion(note=f"lookup unavailable ({type(exc).__name__}); type the values in")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        # A response shaped differently than documented -- an intercepting
        # proxy, an error page, an API change. Same outcome as being offline:
        # the form keeps working and the person types the values in.
        return Suggestion(note=f"unreadable response from Wikidata ({type(exc).__name__})")


def _entity_claims(qid: str) -> dict | None:
    """Claims for one entity, or None if it cannot be fetched."""
    try:
        data = _get({"action": "wbgetentities", "ids": qid, "props": "claims"})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return ((data.get("entities") or {}).get(qid) or {}).get("claims")
