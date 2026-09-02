#!/usr/bin/env python
"""Flag which pipeline companies also have a Drive folder in the New Deals index.

    python scripts/match_drive_index.py [--in ui/index.html] [--out ui/index.html]

`src/Index, New Deals Companies, v2026-08-28-01.xlsx` is a tagged index of every
company folder under *New Deals / 02. Companies* -- 986 rows, one per folder,
with sector tags, a one-line description and whether it is an EV portfolio
company. It answers "does EV already have this company in storage?".

The interface answers a different question: which companies the slides put in
diligence, enriched from Affinity. This script joins the two and writes a Y/N
per company into the inlined payload, so the "Drive index" tab can say which of
the diligence cohort EV already holds a folder for -- and, more usefully, which
it does not.

Like screen_diligence.py this runs over the *built* interface rather than the
database, so the join has one definition and does not fork build_ui.py.
Idempotent: every company's flag is recomputed from scratch on each run.

The join is on name, because the index carries no domain or entity id. Names on
slides are not the names on folders, so matching is a ladder of four tiers and
the tier is carried into the payload -- a Y is never unaccountable, and the tab
shows which folder it matched and how:

  exact   normalised canonical name == index company
  alias   a recorded slide spelling == index company   (WAVR -> WAVR Technologies)
  suffix  equal after dropping corporate/descriptor tokens (EnCharge -> EnCharge AI)
  prefix  one name is a string prefix of the other, >= 8 characters on the
          shorter side, so "Attune Neurosci" catches "Attune Neurosciences"
          without "Chip" sweeping up every folder that starts with it

A tier that hits more than one folder is recorded as `ambiguous`, not resolved:
the tab shows it as a question, not a Y. Anything else is N -- which for this
cohort is the interesting half, since an N is a diligence company with no folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX = REPO_ROOT / "src" / "Index, New Deals Companies, v2026-08-28-01.xlsx"
SHEET = "New Deals Companies"
GUIDE = "Sector codes & guide"

PAYLOAD_RE = re.compile(r'(<script id="payload"[^>]*>)(.*?)(</script>)', re.DOTALL)

# Dropped before a suffix-tier comparison. Corporate forms and the descriptor
# tokens a folder name carries but a slide does not (or the reverse). Sector
# words -- bio, photonics, semi -- are deliberately absent: "Mobius Bio" and
# "Mobius" are not the same claim, and this script does not make it.
SUFFIX_TOKENS = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "holdings", "group", "the", "ai", "io", "labs", "lab",
    "technologies", "technology", "systems", "system",
}

MIN_PREFIX = 8


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).lower().strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def stripped(n: str) -> str:
    tokens = [t for t in n.split() if t not in SUFFIX_TOKENS]
    return " ".join(tokens) if tokens else n


def load_index(path: Path) -> tuple[dict[str, dict], str]:
    """(normalised company name -> folder record, index version string)."""
    df = pd.read_excel(path, sheet_name=SHEET)

    def cell(row, col):
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return str(v).strip() or None

    folders: dict[str, dict] = {}
    for _, r in df.iterrows():
        key = norm(r["Company"])
        if not key or key in folders:
            continue
        folders[key] = {
            "name": str(r["Company"]).strip(),
            "portfolio": cell(r, "Portfolio Company") == "Y",
            "sectors": cell(r, "EV Sector tag(s)"),
            "vertical": cell(r, "Vertical"),
            "desc": cell(r, "One-line description"),
            "basis": cell(r, "Tag basis"),
            "modified": (cell(r, "Folder last modified") or "")[:10] or None,
        }

    version = ""
    try:
        guide = pd.read_excel(path, sheet_name=GUIDE)
        blurb = str(guide.iloc[0, 0])
        m = re.search(r"Version:\s*(\S+)", blurb)
        version = m.group(1).rstrip("·").strip() if m else ""
    except Exception:  # the version string is cosmetic; never fail the join for it
        version = ""

    return folders, version


def match(names: list[str], folders: dict[str, dict],
          by_stripped: dict[str, list[str]]) -> tuple[str, str | None, list[str]]:
    """(tier, matched key, candidates) for one company's names, canonical first."""
    keys = [norm(n) for n in names]
    keys = [k for k in keys if k]

    if keys and keys[0] in folders:
        return "exact", keys[0], []
    for k in keys[1:]:
        if k in folders:
            return "alias", k, []

    cands = {c for k in keys for c in by_stripped.get(stripped(k), [])}
    if len(cands) == 1:
        return "suffix", cands.pop(), []
    if len(cands) > 1:
        return "ambiguous", None, sorted(cands)

    pre: set[str] = set()
    for k in keys:
        for folder_key in folders:
            short, long = sorted((k, folder_key), key=len)
            if len(short) >= MIN_PREFIX and long.startswith(short):
                pre.add(folder_key)
    if len(pre) == 1:
        return "prefix", pre.pop(), []
    if len(pre) > 1:
        return "ambiguous", None, sorted(pre)

    return "none", None, []


def apply(payload: dict, folders: dict[str, dict], version: str) -> Counter:
    by_stripped: dict[str, list[str]] = {}
    for k in folders:
        by_stripped.setdefault(stripped(k), []).append(k)

    aliases = payload.get("aliases", {})
    tally: Counter = Counter()

    for c in payload["companies"]:
        own = [a for a in aliases.get(str(c["id"]), []) if a != c["name"]]
        tier, key, cands = match([c["name"], *own], folders, by_stripped)
        tally[tier] += 1

        if key is not None:
            c["idx"] = {"tier": tier, **folders[key]}
        elif tier == "ambiguous":
            c["idx"] = {"tier": "ambiguous",
                        "candidates": [folders[k]["name"] for k in cands]}
        else:
            c["idx"] = None

    payload["driveIndex"] = {
        "file": INDEX.name,
        "sheet": SHEET,
        "version": version,
        "folders": len(folders),
        "matched": sum(v for k, v in tally.items() if k not in ("none", "ambiguous")),
        "tiers": dict(tally),
    }
    return tally


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", type=Path, default=REPO_ROOT / "ui" / "index.html")
    ap.add_argument("--out", dest="dst", type=Path, default=None)
    ap.add_argument("--index", type=Path, default=INDEX)
    args = ap.parse_args()
    dst = args.dst or args.src

    if not args.src.exists():
        print(f"missing {args.src}; run scripts/build_ui.py first", file=sys.stderr)
        return 1
    if not args.index.exists():
        print(f"missing {args.index}", file=sys.stderr)
        return 1

    html = args.src.read_text()
    m = PAYLOAD_RE.search(html)
    if not m:
        print(f"no inlined payload in {args.src}", file=sys.stderr)
        return 1

    payload = json.loads(m.group(2))
    folders, version = load_index(args.index)
    tally = apply(payload, folders, version)

    body = json.dumps(payload, separators=(",", ":"))
    dst.write_text(html[:m.start(2)] + body + html[m.end(2):])

    total = len(payload["companies"])
    di = payload["driveIndex"]
    print(f"wrote {dst.relative_to(REPO_ROOT)}  ({dst.stat().st_size / 1024:.0f} KB)")
    print(f"  index: {di['folders']} folders, version {version or 'unknown'}")
    print(f"  {di['matched']} of {total} companies have a folder "
          f"({di['matched'] / total:.0%}); {tally['none']} do not")
    for tier in ("exact", "alias", "suffix", "prefix", "ambiguous"):
        if tally[tier]:
            print(f"    {tier:10} {tally[tier]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
