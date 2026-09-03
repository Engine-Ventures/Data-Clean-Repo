#!/usr/bin/env python
"""Propose enrichment values for the cohort's gaps from the Affinity export.

    python scripts/enrich_from_affinity.py [--out data/enrichment_proposals_affinity.csv]

Affinity is the first source in the enrichment order because it is EV's own
CRM: round stage, HQ country, owner and website are all fields it tracks, and
they arrive attributable to a named system rather than to a guess.

This script only *proposes*. It writes a CSV in the shape
`python -m evpipeline.validate --batch` consumes, and
`scripts/apply_enrichment.py` is what puts the values through the validated
write path. Splitting the two means the matching can be reviewed before
anything is written, and a rejected value is visible as a rejection.

Matching, in descending order of how much the key can be trusted (§9):

  org_id   the entity already carries affinity_organization_id, Affinity's own
           stable external key -- the strongest join available.
  domain   entity.domain equals the domain of Affinity's Website.
  name     a canonical name or recorded slide alias matches exactly after
           normalisation, and matches exactly one Affinity row.

A key that hits more than one Affinity row is recorded as `ambiguous` and
proposed for nothing, the same rule the merge proposals follow: a name that
could be two companies is a question, not a value.

Two value-level guards, both of which flag rather than write:

  * `stage` is passed through verbatim. Affinity carries values outside the
    locked picklist (a `Series F`), and the right outcome is the write path
    rejecting them, not this script quietly rounding them to something legal.
  * `round_size_usd` is stored in the same unit Affinity exports -- verified
    at 1.0 ratio across the 153 rows where both sources hold a value -- but
    Affinity also holds a handful of values under $1,000, which are unit
    errors in the CRM rather than real rounds. Those are flagged, not written.
    A genuine 0 is written as a zero (§8's three states), not as unknown.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

INTERFACE = REPO_ROOT / "ui" / "index.html"
_AFFINITY_NAME = "3._Deal_Flow_unsaved_view__export_Sep-02-2026 (2).csv"
AFFINITY = next(
    (p for p in (REPO_ROOT / "data" / "raw" / _AFFINITY_NAME,
                 REPO_ROOT / "src" / _AFFINITY_NAME) if p.exists()),
    REPO_ROOT / "data" / "raw" / _AFFINITY_NAME,
)
DEFAULT_OUT = REPO_ROOT / "data" / "enrichment_proposals_affinity.csv"

PAYLOAD_RE = re.compile(r'<script id="payload"[^>]*>(.*?)</script>', re.DOTALL)

# Affinity column -> pipeline field, for the five fields the worklist tracks.
FIELD_MAP = {
    "Stage": "stage",
    "Headquarters (Country)": "hq_country",
    "Website": "website",
    "Engine Team": "owner_name",
    "Round Size": "round_size_usd",
}

# Below this, an Affinity round size is a unit error rather than a round.
MIN_PLAUSIBLE_ROUND = 1000.0


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).casefold().strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def domain_of(website: object) -> str | None:
    if not isinstance(website, str) or not website.strip():
        return None
    d = re.sub(r"^https?://", "", website.strip(), flags=re.IGNORECASE)
    d = re.sub(r"^www\.", "", d, flags=re.IGNORECASE).rstrip("/").casefold()
    return d.split("/")[0] or None


def first_owner(engine_team: object) -> str | None:
    """Affinity's Engine Team is `Name <email>`, several separated by `; `.

    The pipeline's owner_name is a single name, and the convention already in
    the data is the first entry -- verified against every multi-owner row that
    already carries an owner_name. Taking the first is therefore reproducing
    the existing convention, not inventing one.
    """
    if not isinstance(engine_team, str) or not engine_team.strip():
        return None
    first = engine_team.split(";")[0].strip()
    return re.sub(r"\s*<[^>]*>\s*", "", first).strip() or None


def load_payload(path: Path) -> dict:
    m = PAYLOAD_RE.search(path.read_text())
    if not m:
        raise SystemExit(f"no inlined payload in {path}")
    return json.loads(m.group(1))


def index_affinity(aff: pd.DataFrame) -> tuple[dict, dict, dict]:
    by_org: dict[str, list[int]] = defaultdict(list)
    by_dom: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[int]] = defaultdict(list)
    for i, r in aff.iterrows():
        oid = r["Organization Id"]
        if pd.notna(oid):
            by_org[str(int(oid))].append(i)
        d = domain_of(r["Website"])
        if d:
            by_dom[d].append(i)
        n = norm(r["Name"])
        if n:
            by_name[n].append(i)
    return by_org, by_dom, by_name


def match(company: dict, aliases: list[str], by_org, by_dom, by_name) -> tuple[int | None, str]:
    fields = {f[0]: f[1] for f in company["fields"]}
    oid = fields.get("affinity_organization_id")
    if oid is not None:
        try:
            hits = by_org.get(str(int(float(oid))), [])
        except (TypeError, ValueError):
            hits = []
        if len(hits) == 1:
            return hits[0], "org_id"
        if len(hits) > 1:
            return None, "ambiguous_org_id"

    dom = company.get("domain")
    if dom:
        hits = by_dom.get(dom, [])
        if len(hits) == 1:
            return hits[0], "domain"
        if len(hits) > 1:
            return None, "ambiguous_domain"

    cands = {j for n in [company["name"], *aliases] for j in by_name.get(norm(n), [])}
    if len(cands) == 1:
        return cands.pop(), "name"
    if len(cands) > 1:
        return None, "ambiguous_name"
    return None, "no_match"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interface", default=str(INTERFACE))
    ap.add_argument("--affinity", default=str(AFFINITY))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    payload = load_payload(Path(args.interface))
    companies = payload["companies"]
    aliases = {int(k): list(v) for k, v in payload.get("aliases", {}).items()}

    aff = pd.read_csv(args.affinity)
    by_org, by_dom, by_name = index_affinity(aff)
    export_name = Path(args.affinity).name

    proposals: list[dict[str, object]] = []
    flagged: list[str] = []
    methods: dict[str, int] = defaultdict(int)

    for c in sorted(companies, key=lambda c: c["name"].lower()):
        eid = int(c["id"])
        idx, method = match(c, aliases.get(eid, []), by_org, by_dom, by_name)
        methods[method] += 1
        if idx is None:
            if method.startswith("ambiguous"):
                flagged.append(f"EV{eid:04d} {c['name']}: {method}, proposed nothing")
            continue

        row = aff.loc[idx]
        have = {f[0]: f[1] for f in c["fields"]}

        for col, field in FIELD_MAP.items():
            if have.get(field) is not None:
                continue  # never overwrite a value already on file
            raw = row[col]
            if pd.isna(raw):
                continue

            if field == "owner_name":
                value: object | None = first_owner(raw)
            elif field == "round_size_usd":
                value = float(raw)
                if value != 0 and value < MIN_PLAUSIBLE_ROUND:
                    flagged.append(
                        f"EV{eid:04d} {c['name']}: round size {value:g} is below "
                        f"${MIN_PLAUSIBLE_ROUND:,.0f}, a unit error in Affinity; not written"
                    )
                    continue
                # Plain digits, not 9e+06: the value lands in an audit log a
                # person reads.
                value = f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"
            else:
                value = str(raw).strip()
            if value in (None, ""):
                continue

            proposals.append({
                "company": f"EV{eid:04d}",
                "field": field,
                "value": value,
                "source": "Affinity",
                "citation": "",
                "note": f"{export_name} row {idx + 2} via {method}",
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["company", "field", "value", "source", "citation", "note"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(proposals)

    print(f"matched {len(companies)} cohort companies against {len(aff)} Affinity rows")
    for m, n in sorted(methods.items(), key=lambda kv: -kv[1]):
        print(f"  {m:20} {n:>4}")
    per_field: dict[str, int] = defaultdict(int)
    for p in proposals:
        per_field[str(p["field"])] += 1
    print(f"\n{len(proposals)} proposals -> {out.relative_to(REPO_ROOT)}")
    for f, n in sorted(per_field.items(), key=lambda kv: -kv[1]):
        print(f"  {f:20} {n:>4}")
    if flagged:
        print(f"\n{len(flagged)} flagged, proposed nothing:")
        for f in flagged:
            print(f"  {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
