#!/usr/bin/env python
"""Cross-check the New Deals index against Affinity and the new deal slides.

    python scripts/match_drive_index.py [--in ui/index.html] [--out ui/index.html]

`src/evpipeline/Index, New Deals Companies, v2026-08-28-01.xlsx` is a tagged
index of every company folder under *New Deals / 02. Companies* -- 986 rows, one
per folder, with sector tags, a one-line description and whether it is an EV
portfolio company. It answers "does EV already have this company in storage?".

This script answers the inverse, which is the question the deal team actually
asks of the index: **of the companies EV holds a folder for, how many ever
reached Affinity, and how many ever reached the new deal slides?** A folder with
neither is storage EV is carrying for a company that never entered the pipeline
-- or, more often, entered it under a different spelling.

So the index is the driving table. Every one of its rows gets two independent
verdicts, and the tab segments on the pair:

  both      in Affinity and on the slides
  affinity  in Affinity, never on a slide
  slides    on a slide, not in the Affinity export
  neither   folder only -- in storage, absent from both systems

The reverse annotation is kept too: every cohort company still carries `idx`, so
the company drawer can say which folder backs it. That direction is a by-product
now rather than the point.

Like screen_diligence.py this runs over the *built* interface rather than the
database, so the join has one definition and does not fork build_ui.py.
Idempotent: every verdict is recomputed from scratch on each run.

The join is on name, because the index carries no domain or entity id. Names on
slides are not the names on folders, so matching is a ladder of four tiers and
the tier is carried into the payload -- a Y is never unaccountable, and the tab
shows what it matched and how:

  exact   normalised name == the target's primary name
  alias   normalised name == a recorded alternate spelling (WAVR Technologies
          -> WAVR), which only the slide side has
  suffix  equal after dropping corporate/descriptor tokens (EnCharge AI ->
          EnCharge)
  prefix  one name is a string prefix of the other, >= 8 characters on the
          shorter side, so "Attune Neurosciences" catches "Attune Neurosci"
          without "Chip" sweeping up everything that starts with it

A tier that hits more than one target is recorded as `ambiguous`, not resolved:
the tab shows it as a question, not a Y. Anything else is `none`.

CAVEAT, and it bounds every "slides" number this writes: the slide side is
whatever cohort is in the input HTML. After screen_diligence.py that is the
advanced-stage cohort, not the full slide population, so a `neither` may simply
be an early-stage company screened out upstream. The payload records the cohort
size and the tab states it; run this against an unscreened build for the full
answer.
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
DATA = REPO_ROOT / "src" / "evpipeline"
INDEX = DATA / "Index, New Deals Companies, v2026-08-28-01.xlsx"
AFFINITY = DATA / "3._Deal_Flow_unsaved_view__export_Sep-02-2026 (2).csv"
SHEET = "New Deals Companies"
GUIDE = "Sector codes & guide"

# The built interface. The slide side of the join is whatever cohort this
# file carries, per the CAVEAT above, so it is the input rather than the
# database even when a caller has a live connection to hand.
BUILT = REPO_ROOT / "ui" / "index.html"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
HIT_TIERS = ("exact", "alias", "suffix", "prefix")


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).lower().strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def stripped(n: str) -> str:
    tokens = [t for t in n.split() if t not in SUFFIX_TOKENS]
    return " ".join(tokens) if tokens else n


def iso_date(v: object) -> str | None:
    """A date as YYYY-MM-DD, whatever the export wrote it as.

    Affinity exports MM/DD/YYYY. Everything else in the payload is ISO, and a
    row mixing the two invites a reader to parse 09/02/2026 as 9 February.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if ISO_DATE.match(s[:10]):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def cell(row, col):
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v).strip() or None


class Target:
    """One side of a join: normalised names -> record, with the match ladder.

    A record may be reachable under several keys (a canonical name plus recorded
    aliases). Which key hit decides `exact` versus `alias`, so the tier stays
    honest about how much of a stretch the match was.
    """

    def __init__(self, label: str):
        self.label = label
        self.by_key: dict[str, dict] = {}
        self.canonical: set[str] = set()
        self._by_stripped: dict[str, list[str]] = {}

    def add(self, name: str, record: dict, *, canonical: bool = True) -> None:
        key = norm(name)
        if not key:
            return
        # First writer wins, but a canonical name always outranks an alias that
        # got there first -- otherwise a stray alias would shadow a real company.
        if key in self.by_key and not (canonical and key not in self.canonical):
            return
        self.by_key[key] = record
        if canonical:
            self.canonical.add(key)
        self._by_stripped.setdefault(stripped(key), []).append(key)

    def match(self, names: list[str]) -> tuple[str, dict | None, list[str]]:
        """(tier, record, candidate names) for a query's names, primary first.

        `exact` is reserved for the unambiguous case -- the query's own primary
        name against the target's primary name. A hit that needed an alternate
        spelling on *either* side is `alias`, so the tier stays a claim about how
        much the join had to stretch rather than about which table held the alias.
        """
        keys = [k for k in (norm(n) for n in names) if k]

        if keys and keys[0] in self.canonical:
            return "exact", self.by_key[keys[0]], []
        for k in keys:
            if k in self.by_key:
                return "alias", self.by_key[k], []

        cands = {c for k in keys for c in self._by_stripped.get(stripped(k), [])}
        if len(cands) == 1:
            return "suffix", self.by_key[cands.pop()], []
        if len(cands) > 1:
            return "ambiguous", None, self._names(cands)

        pre: set[str] = set()
        for k in keys:
            for other in self.by_key:
                short, long = sorted((k, other), key=len)
                if len(short) >= MIN_PREFIX and long.startswith(short):
                    pre.add(other)
        if len(pre) == 1:
            return "prefix", self.by_key[pre.pop()], []
        if len(pre) > 1:
            return "ambiguous", None, self._names(pre)

        return "none", None, []

    def _names(self, keys) -> list[str]:
        return sorted({self.by_key[k]["name"] for k in keys})


def load_index(path: Path) -> tuple[list[dict], str]:
    """(folder records in sheet order, index version string)."""
    df = pd.read_excel(path, sheet_name=SHEET)

    folders: list[dict] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        key = norm(r["Company"])
        if not key or key in seen:
            continue
        seen.add(key)
        folders.append({
            "name": str(r["Company"]).strip(),
            "portfolio": cell(r, "Portfolio Company") == "Y",
            "sectors": cell(r, "EV Sector tag(s)"),
            "vertical": cell(r, "Vertical"),
            "desc": cell(r, "One-line description"),
            "basis": cell(r, "Tag basis"),
            "modified": (cell(r, "Folder last modified") or "")[:10] or None,
        })

    version = ""
    try:
        guide = pd.read_excel(path, sheet_name=GUIDE)
        m = re.search(r"Version:\s*(\S+)", str(guide.iloc[0, 0]))
        version = m.group(1).rstrip("·").strip() if m else ""
    except Exception:  # the version string is cosmetic; never fail the join for it
        version = ""

    return folders, version


def load_affinity(path: Path) -> Target:
    """Affinity organisations, keyed by name. One row per organisation."""
    df = pd.read_csv(path)
    target = Target("Affinity")
    for _, r in df.iterrows():
        name = cell(r, "Name")
        if not name:
            continue
        target.add(name, {
            "name": name,
            "domain": cell(r, "Website"),
            "status": cell(r, "Status"),
            "stage": cell(r, "Stage"),
            "owner": (cell(r, "Engine Team") or "").split("<")[0].strip() or None,
            "added": cell(r, "Date Added"),
            # Shown on the row, because "in Affinity" and "in Affinity and
            # actually met" are different findings about a folder.
            "lastMeeting": iso_date(cell(r, "Last Meeting")),
        })
    return target


def slide_target(payload: dict) -> Target:
    """The cohort in the built interface, reachable by canonical name or alias."""
    aliases = payload.get("aliases", {})
    target = Target("slides")
    for c in payload["companies"]:
        rec = {
            "name": c["name"],
            "id": c["id"],
            "domain": c.get("domain"),
            "furthest": c.get("furthest"),
            "appearances": c.get("appearances") or 0,
            "last": c.get("last"),
        }
        target.add(c["name"], rec, canonical=True)
        for a in aliases.get(str(c["id"]), []):
            if a != c["name"]:
                target.add(a, rec, canonical=False)
    return target


def verdict(tier: str, rec: dict | None, cands: list[str]) -> dict | None:
    """The payload shape for one side of one index row. None means no match."""
    if rec is not None:
        return {"tier": tier, **rec}
    if tier == "ambiguous":
        return {"tier": "ambiguous", "candidates": cands}
    return None


def _slide_verdict(tier: str, rec: dict | None, cands: list[str],
                   window: list[str] | None) -> dict | None:
    """The `slides` half of one index row, in the shape the tab reads.

    `how` is what the tab tests, not the tier: a tier says how hard the name
    match was, `how` says what kind of record was matched. Only `entity` is
    available here -- a name the pipeline itself holds. The design also allows
    `deck` and `weak` for names found in the deck's raw text outside the
    extracted window; nothing in this repo reads the deck, so no row is ever
    given one, rather than a text match being invented to fill the column.
    """
    if rec is not None:
        last = rec.get("last")
        return {
            "how": "entity",
            "tier": tier,
            "as": rec["name"],
            "entity": rec.get("id"),
            "observations": rec.get("appearances") or 0,
            "last": last,
            # True for every cohort company in the ordinary case, since the
            # cohort is drawn from the loaded window. Computed rather than
            # assumed so a company carried in from outside it cannot read as
            # current.
            "loaded": bool(last and window and window[0] <= last <= window[1]),
        }
    if tier == "ambiguous":
        return {"how": "ambiguous", "candidates": cands}
    return None


def _crm_verdict(tier: str, rec: dict | None, cands: list[str]) -> dict | None:
    """The `crm` half of one index row."""
    if rec is not None:
        return {
            "how": "crm",
            "tier": tier,
            "name": rec["name"],
            "status": rec.get("status"),
            "lastMeeting": rec.get("lastMeeting"),
        }
    if tier == "ambiguous":
        return {"how": "ambiguous", "candidates": cands}
    return None


def verdict(tier: str, rec: dict | None, cands: list[str]) -> dict | None:
    """The reverse annotation: which folder backs a cohort company."""
    if rec is not None:
        return {"tier": tier, **rec}
    if tier == "ambiguous":
        return {"tier": "ambiguous", "candidates": cands}
    return None


def loaded_window(payload: dict) -> list[str] | None:
    """[first, last] meeting date the interface actually carries."""
    dates = sorted(m["date"] for m in payload.get("meetings", []) if m.get("date"))
    return [dates[0], dates[-1]] if dates else None


def annotate(payload: dict, folders: list[dict]) -> Counter:
    """Give every cohort company the folder that backs it.

    Derived from the same ladder as the forward join, so the two directions
    cannot drift.
    """
    folder_target = Target("index")
    for f in folders:
        folder_target.add(f["name"], f)

    aliases = payload.get("aliases", {})
    back: Counter = Counter()
    for c in payload["companies"]:
        own = [a for a in aliases.get(str(c["id"]), []) if a != c["name"]]
        tier, rec, cands = folder_target.match([c["name"], *own])
        back[tier] += 1
        c["idx"] = verdict(tier, rec, cands)
    return back


def reach(payload: dict | None = None, *, index: Path = INDEX,
          affinity: Path = AFFINITY, verbose: bool = False) -> dict:
    """The index-reach join as the `indexReach` payload the tab renders.

    One row per index folder, each carrying two independent verdicts and the
    evidence for them, plus the counts the tab's vitals read. This is the whole
    of what the Record coverage tab needs: hand the return value to the payload
    as `indexReach` and the tab renders.

    `payload` is a built interface's payload, whose cohort is the slide side of
    the join. With none, the snapshot at `BUILT` is read -- which is what
    `scripts/serve.py` does, since the join depends on the index workbook, the
    Affinity export and the built cohort, and on nothing a browser write can
    change. When a payload *is* given it also gets the reverse annotation.

    Raises FileNotFoundError if a source is missing, rather than returning a
    half-join that would read as a real result with everything absent.
    """
    for path in (index, affinity):
        if not path.exists():
            raise FileNotFoundError(path)

    if payload is None:
        if not BUILT.exists():
            raise FileNotFoundError(BUILT)
        m = PAYLOAD_RE.search(BUILT.read_text())
        if not m:
            raise FileNotFoundError(f"no inlined payload in {BUILT}")
        payload = json.loads(m.group(2))

    folders, version = load_index(index)
    crm = load_affinity(affinity)
    slides = slide_target(payload)
    window = loaded_window(payload)

    rows = []
    counts: Counter = Counter()
    segments: Counter = Counter()
    aff_tiers: Counter = Counter()
    slide_tiers: Counter = Counter()

    for f in folders:
        s_tier, s_rec, s_cands = slides.match([f["name"]])
        a_tier, a_rec, a_cands = crm.match([f["name"]])
        slide_tiers[s_tier] += 1
        aff_tiers[a_tier] += 1

        on_slides = s_tier in HIT_TIERS
        in_crm = a_tier in HIT_TIERS
        counts["slides"] += on_slides
        counts["affinity"] += in_crm
        counts["both"] += on_slides and in_crm
        counts["either"] += on_slides or in_crm
        counts["neither"] += not (on_slides or in_crm)
        segments[("both" if on_slides and in_crm else
                  "affinity" if in_crm else
                  "slides" if on_slides else "neither")] += 1

        rows.append({
            **f,
            "slides": _slide_verdict(s_tier, s_rec, s_cands, window),
            "crm": _crm_verdict(a_tier, a_rec, a_cands),
        })

    back = annotate(payload, folders)

    meta = {
        "file": index.name,
        "sheet": SHEET,
        "version": version,
        "folders": len(folders),
        "affinityFile": affinity.name,
        "affinityRecords": len(crm.by_key),
        "cohort": len(payload["companies"]),
        "cohortNote": payload.get("screen", {}).get("cohort"),
        "loadedMeetings": len(payload.get("meetings", [])),
        "loadedWindow": window,
        "rows": rows,
        "counts": dict(counts),
        "segments": dict(segments),
        "affinityTiers": dict(aff_tiers),
        "slideTiers": dict(slide_tiers),
        # Retained for the reverse view in the company drawer.
        "matched": sum(v for k, v in back.items() if k in HIT_TIERS),
        "tiers": dict(back),
    }

    if verbose:
        c = meta["counts"]
        print(f"index reach: {c['either']} of {len(folders)} folders visible "
              f"({c['neither']} in neither)", file=sys.stderr)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", type=Path, default=REPO_ROOT / "ui" / "index.html")
    ap.add_argument("--out", dest="dst", type=Path, default=None)
    ap.add_argument("--index", type=Path, default=INDEX)
    ap.add_argument("--affinity", type=Path, default=AFFINITY)
    args = ap.parse_args()
    dst = args.dst or args.src

    if not args.src.exists():
        print(f"missing {args.src}; run scripts/build_ui.py first", file=sys.stderr)
        return 1
    for p in (args.index, args.affinity):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 1

    html = args.src.read_text()
    m = PAYLOAD_RE.search(html)
    if not m:
        print(f"no inlined payload in {args.src}", file=sys.stderr)
        return 1

    payload = json.loads(m.group(2))
    meta = reach(payload, index=args.index, affinity=args.affinity)
    # The previous shape, which nothing reads now that the tab is on
    # `indexReach`. Dropped rather than written alongside it: two copies of 982
    # rows in one file is a megabyte of payload that can disagree with itself.
    payload.pop("indexRows", None)
    payload.pop("driveIndex", None)
    payload["indexReach"] = meta
    segments = meta["segments"]

    body = json.dumps(payload, separators=(",", ":"))
    dst.write_text(html[:m.start(2)] + body + html[m.end(2):])

    total = meta["folders"]
    print(f"wrote {dst.relative_to(REPO_ROOT)}  ({dst.stat().st_size / 1024:.0f} KB)")
    print(f"  index:    {total} folders, version {meta['version'] or 'unknown'}")
    print(f"  affinity: {meta['affinityRecords']} organisations")
    print(f"  slides:   {meta['cohort']} companies in the built cohort")
    print(f"  of {total} indexed folders:")
    for seg, label in (("both", "in Affinity and on slides"),
                       ("affinity", "in Affinity only"),
                       ("slides", "on slides only"),
                       ("neither", "in neither")):
        n = segments.get(seg, 0)
        print(f"    {seg:9} {n:4}  ({n / total:5.1%})  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
