#!/usr/bin/env python
"""Measure how much of the New Deals companies index the pipeline can see.

    python scripts/match_drive_index.py [--in ui/index.html] [--out ui/index.html]

`Index, New Deals Companies, v2026-08-28-01.xlsx` is a tagged index of every
company folder under *New Deals / 02. Companies* -- 986 rows, 982 distinct
companies, with sector tags, a one-line description and whether it is an EV
portfolio company. It is the closest thing EV has to a list of every company it
has ever opened a file on.

This script asks, for each of those 982 folders, whether the company is
*visible* in either of the two records this repo is built from:

  slides    `Copy of Monday - New Deal Meeting.pdf` -- the whole deck,
            1,018 pages and 153 dated meetings back to August 2021
  affinity  `affinity_export_2026-09-01.csv` -- the whole Deal Flow
            view, 2,869 rows and 2,815 distinct organisations, most of them
            passed or never contacted. Being in Affinity is not being in the
            pipeline, so each row carries the Affinity status it matched.

A folder visible in neither is a company EV holds material on that never
reached a deal meeting slide and has no CRM record: either genuinely outside
the deal process, or a gap in the two records this pipeline treats as its
evidence. That is the half worth reading, which is why the tab counts it.

The direction matters. `build_ui.py` builds the pipeline's own population and
asks which of *those* companies have a folder. This asks the reverse, and the
denominator is the index, not the cohort -- so it is the index's 982 rows that
appear in the payload, not the interface's companies.

Scope note: the slides side reads the *deck*, not the database. The database
holds 43 of the deck's 153 meetings (2025-10-14 onward), so joining the index
against the loaded population would score a 2021 folder as unseen purely
because the load window opens in 2025. Every row therefore carries the meeting
date it was last seen on, and whether that falls inside the loaded window, so
the narrower reading is still available on the tab.

Both joins are on name -- the index carries no domain and no entity id. Three
ladders, each recorded per row so no Y is unaccountable:

  slides, structured   the name resolves to an extracted slide entity (the 498
                       in the database), by the four-tier name ladder below
  slides, deck text    the name occurs in the deck's text as a whole token
                       sequence, capitalised where it occurs. Capitalisation is
                       what separates the company `Meter` from the word
                       "meter"; without it `_Temp` matches 64 pages of "temp".
  affinity             the same four-tier ladder against the CRM export

The four-tier name ladder, unchanged from the version of this script that ran
the other direction:

  exact   normalised name == target
  alias   a recorded slide spelling matches   (WAVR -> WAVR Technologies)
  suffix  equal after dropping corporate/descriptor tokens (EnCharge -> EnCharge AI)
  prefix  one name is a string prefix of the other, >= 8 characters on the
          shorter side, so "Attune Neurosci" catches "Attune Neurosciences"
          without "Chip" sweeping up every folder that starts with it

A tier hitting more than one target is `ambiguous`, not resolved: the tab shows
it as a question. A deck-text hit on a name of three characters or fewer is
`weak` -- `TPL` on one page is as likely to be an acronym in prose as a
company -- and is counted apart from the confident hits rather than folded in.

Idempotent: every row is recomputed from scratch on each run. The deck's
extracted text is cached in `data/slide_text.json` (keyed on the PDF's size and
mtime) because extracting 1,018 pages takes about ten seconds.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]


def source(name: str) -> Path:
    """A confidential source file, wherever it is kept.

    `data/raw/` is where .gitignore says the deck, the Affinity export and the
    workbooks belong; some of them have also lived loose in `src/`. Both are
    checked so a move does not break the join, and the `data/raw/` path is what
    a missing-file message names.
    """
    for parent in (REPO_ROOT / "data" / "raw", REPO_ROOT / "src"):
        if (parent / name).exists():
            return parent / name
    return REPO_ROOT / "data" / "raw" / name


INDEX = source("Index, New Deals Companies, v2026-08-28-01.xlsx")
SHEET = "New Deals Companies"
GUIDE = "Sector codes & guide"

SLIDES = source("Copy of Monday - New Deal Meeting.pdf")
SLIDE_CACHE = REPO_ROOT / "data" / "slide_text.json"
AFFINITY = source("affinity_export_2026-09-01.csv")
DB = REPO_ROOT / "data" / "pipeline.db"

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

# A deck-text hit on a name this short is reported as `weak` rather than as a
# confident yes: at three characters a match is as likely to be an acronym in
# prose as the company. The structured and Affinity ladders are unaffected --
# there the target is a company name, not running text.
MIN_DECK_CHARS = 4

# How far a mis-stamped meeting title may be advanced before the order is
# treated as something other than a new-year typo. Two is already generous;
# the three real cases need one.
MAX_YEAR_BUMPS = 2

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
_MON = "|".join(MONTHS)

# The deck's meeting title, in the two orders it appears in. Anchoring on the
# title rather than on any date keeps in-slide dates -- "Blueprint tracking
# December 12, 2022", a company's own founding year -- from being read as the
# meeting a page belongs to.
TITLE_RE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"(?:New Deal Meeting|Deal Meeting|Deal Flow Meeting)[^A-Za-z0-9]{0,10}"
    r"(" + _MON + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)",
    r"(" + _MON + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)[^A-Za-z0-9]{0,10}"
    r"(?:New Deal Meeting|Deal Meeting)",
))


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).lower().strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def flat(s: object) -> str:
    """Like `norm` but case-preserving: punctuation and the PDF's fi/ffi
    ligatures collapse to single spaces, capitalisation survives."""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", s)).strip()


def stripped(n: str) -> str:
    tokens = [t for t in n.split() if t not in SUFFIX_TOKENS]
    return " ".join(tokens) if tokens else n


PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
# What the index writes in front of a former name inside the parenthetical.
FORMERLY_RE = re.compile(r"^(?:fka|f/k/a|aka|previously|prev|former(?:ly)?|"
                         r"now|was)\b[\s:.-]*", re.IGNORECASE)


def variants(name: str) -> list[str]:
    """The names one folder should be looked for under, canonical first.

    21 folders qualify themselves in parentheses, and the parenthetical is
    usually a name in its own right -- `Trener Robotics (fka T-robotics)` is
    on the slides and the Fund III list as T-Robotics, and would otherwise
    read as unseen. Both halves are searched; whichever hits is recorded, so
    the row says which name the evidence is under.
    """
    out = [name.strip()]
    head = PAREN_RE.sub("", name).strip()
    if head and head not in out:
        out.append(head)
    for m in PAREN_RE.finditer(name):
        inner = FORMERLY_RE.sub("", m.group(1)).strip(" -:.")
        # A parenthetical that is a description rather than a name is dropped
        # by the same rule that governs the deck search: no lone short words.
        if len(inner) >= MIN_DECK_CHARS and inner not in out:
            out.append(inner)
    return out


# ---------------------------------------------------------------- the index

def load_index(path: Path) -> tuple[dict[str, dict], str, list[str]]:
    """(company name -> folder record, index version, non-company folders skipped)."""
    df = pd.read_excel(path, sheet_name=SHEET)

    def cell(row, col):
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return str(v).strip() or None

    folders: dict[str, dict] = {}
    skipped: list[str] = []
    for _, r in df.iterrows():
        raw_name = str(r["Company"]).strip()
        key = norm(r["Company"])
        if not key or key in folders:
            continue
        if raw_name.startswith("_"):
            # `_Temp` and `_Academics` are scaffolding folders, not companies.
            # Left in the denominator they would read as two companies the
            # pipeline cannot see, and `_Temp` alone matches 64 pages of the
            # word "temp".
            skipped.append(raw_name)
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

    return folders, version, skipped


# ---------------------------------------------------------------- the deck

def _title_date(text: str) -> date | None:
    for rx in TITLE_RE:
        m = rx.search(text)
        if m:
            try:
                return date(int(m.group(3)), MONTH_NUM[m.group(1).title()], int(m.group(2)))
            except ValueError:
                return None
    return None


def load_deck(pdf: Path, cache: Path | None = SLIDE_CACHE,
              verbose: bool = False) -> dict:
    """The deck as `{"pages": [flattened text], "meeting": [iso | None]}`.

    Each page is attributed to the meeting whose title page most recently
    preceded it: the deck runs newest-first, so a page belongs to the title
    above it.

    Read oldest-first the title dates must strictly increase, and where they do
    not the deck has stamped a January meeting with the outgoing year -- three
    do, the meetings of 2023-01-03, 01-09 and 01-17, all filed as 2022 and all
    sitting above a correctly-stamped December 2022. Those are corrected by
    advancing the year until the order holds, and the corrections are returned
    so the count can be stated rather than assumed. Taking the deck's dates
    literally instead would fold 34 meetings into one.
    """
    stamp = f"{pdf.stat().st_size}:{int(pdf.stat().st_mtime)}"
    if cache and cache.exists():
        try:
            blob = json.loads(cache.read_text())
            if blob.get("stamp") == stamp:
                return blob
        except (ValueError, OSError):
            pass

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - an environment problem
        raise SystemExit("the deck join needs pypdf: pip install pypdf") from exc

    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # the deck has bad floats

    if verbose:
        print(f"extracting {pdf.name} ...", file=sys.stderr)
    reader = PdfReader(str(pdf))
    raw = []
    for page in reader.pages:
        try:
            raw.append(page.extract_text() or "")
        except Exception:
            raw.append("")

    titles = [(i, d) for i, d in
              ((i, _title_date(t)) for i, t in enumerate(raw)) if d is not None]

    fixed: list[dict] = []
    prev: date | None = None
    for k, (page, found) in enumerate(reversed(titles)):  # oldest first
        stated, bumps = found, 0
        while prev is not None and found <= prev and bumps < MAX_YEAR_BUMPS:
            found = found.replace(year=found.year + 1)
            bumps += 1
        if found != stated:
            fixed.append({"page": page + 1, "stated": stated.isoformat(),
                          "read_as": found.isoformat()})
        titles[len(titles) - 1 - k] = (page, found)
        prev = found

    meetings: list[str | None] = []
    current: date | None = None
    by_page = dict(titles)
    for i in range(len(raw)):
        if i in by_page:
            current = by_page[i]
        meetings.append(current.isoformat() if current else None)

    blob = {
        "stamp": stamp,
        "file": pdf.name,
        "pages": [flat(t) for t in raw],
        "meeting": meetings,
        "dateFixes": fixed,
    }
    if cache:
        cache.write_text(json.dumps(blob, separators=(",", ":")))
    return blob


def deck_pattern(name: str) -> re.Pattern | None:
    tokens = flat(name).split()
    if not tokens:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9])" + r"\s+".join(map(re.escape, tokens)) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def deck_hits(names: list[str], deck: dict) -> dict | None:
    """Where a name occurs in the deck, or None.

    An occurrence counts only if it is capitalised (or starts with a digit)
    somewhere on the page. Slides capitalise company names and prose does not,
    which is the only signal available for telling the company `Meter` from
    the word "meter" -- and it is the signal that keeps `resonant link` in the
    index matching `Resonant Link` on the slide.
    """
    for name in names:
        rx = deck_pattern(name)
        if rx is None:
            continue
        pages: list[int] = []
        dates: list[str] = []
        for i, (text, meeting) in enumerate(zip(deck["pages"], deck["meeting"], strict=True)):
            found = [m.group(0) for m in rx.finditer(text)]
            if not found:
                continue
            if not any(f[0].isupper() or f[0].isdigit() for f in found):
                continue  # lowercase everywhere on this page: prose, not a name
            pages.append(i + 1)
            if meeting:
                dates.append(meeting)
        if pages:
            return {
                "as": name,
                "pages": len(pages),
                "firstPage": pages[0],
                "meetings": len(set(dates)),
                "first": min(dates) if dates else None,
                "last": max(dates) if dates else None,
            }
    return None


# --------------------------------------------------- the two name ladders

def ladder(names: list[str], targets: dict[str, dict],
           by_stripped: dict[str, list[str]]) -> tuple[str, str | None, list[str]]:
    """(tier, matched key, candidates) for one company's names, canonical first."""
    keys = [k for k in (norm(n) for n in names) if k]

    if keys and keys[0] in targets:
        return "exact", keys[0], []
    for k in keys[1:]:
        if k in targets:
            return "alias", k, []

    cands = {c for k in keys for c in by_stripped.get(stripped(k), [])}
    if len(cands) == 1:
        return "suffix", cands.pop(), []
    if len(cands) > 1:
        return "ambiguous", None, sorted(cands)

    pre: set[str] = set()
    for k in keys:
        for target_key in targets:
            short, long = sorted((k, target_key), key=len)
            if len(short) >= MIN_PREFIX and long.startswith(short):
                pre.add(target_key)
    if len(pre) == 1:
        return "prefix", pre.pop(), []
    if len(pre) > 1:
        return "ambiguous", None, sorted(pre)

    return "none", None, []


def index_stripped(targets: dict[str, dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k in targets:
        out.setdefault(stripped(k), []).append(k)
    return out


def load_slide_entities(db: Path) -> tuple[dict[str, dict], tuple[str, str], int]:
    """(normalised slide name -> entity record, loaded meeting window, meetings).

    Every extracted spelling is a key -- canonical names and slide aliases
    alike -- so a folder named for the spelling the slides used resolves even
    when the pipeline's canonical name differs.
    """
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("""
            SELECT e.entity_id, e.canonical_name, e.is_phantom,
                   (SELECT COUNT(*) FROM slide_observation o WHERE o.entity_id = e.entity_id),
                   (SELECT MAX(o.meeting_date) FROM slide_observation o
                     WHERE o.entity_id = e.entity_id)
              FROM entity e
        """).fetchall()
        aliases = conn.execute("SELECT entity_id, alias_text FROM alias").fetchall()
        window = conn.execute(
            "SELECT MIN(meeting_date), MAX(meeting_date), COUNT(*) FROM meeting"
        ).fetchone()
    finally:
        conn.close()

    entities: dict[str, dict] = {}
    for eid, name, phantom, obs, last in rows:
        rec = {"id": eid, "name": name, "phantom": bool(phantom),
               "observations": obs, "lastSeen": last}
        entities.setdefault(norm(name), rec)
    by_id = {r["id"]: r for r in entities.values()}
    for eid, text in aliases:
        key = norm(text)
        if key and key not in entities and eid in by_id:
            entities[key] = by_id[eid]

    return entities, (window[0], window[1]), window[2]


def load_affinity(path: Path) -> dict[str, dict]:
    """Normalised Affinity organisation name -> the fields the tab shows."""
    records: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            key = norm(row.get("Name"))
            if not key or key in records:
                continue
            records[key] = {
                "name": (row.get("Name") or "").strip(),
                "website": (row.get("Website") or "").strip() or None,
                "status": (row.get("Status") or "").strip() or None,
                "stage": (row.get("Stage") or "").strip() or None,
                "added": (row.get("Date Added") or "").strip() or None,
                "lastMeeting": (row.get("Last Meeting") or "").strip() or None,
            }
    return records


# ---------------------------------------------------------------- the join

def reach(index_path: Path = INDEX, slides: Path = SLIDES,
          affinity: Path = AFFINITY, db: Path = DB,
          verbose: bool = False) -> dict:
    """The `indexReach` payload block: one row per index folder, plus counts."""
    folders, version, skipped = load_index(index_path)
    entities, loaded_window, loaded_meetings = load_slide_entities(db)
    crm = load_affinity(affinity)
    deck = load_deck(slides, verbose=verbose)

    ent_stripped = index_stripped(entities)
    crm_stripped = index_stripped(crm)

    deck_dates = sorted({d for d in deck["meeting"] if d})
    rows: list[dict] = []
    tally: Counter = Counter()

    for key, folder in folders.items():
        name = folder["name"]
        names = variants(name)

        tier, ekey, cands = ladder(names, entities, ent_stripped)
        seen_slides: dict | None = None
        if ekey is not None:
            ent = entities[ekey]
            seen_slides = {
                "how": "entity", "tier": tier, "as": ent["name"],
                "under": names[0] if tier == "exact" else None,
                "entity": ent["id"], "observations": ent["observations"],
                "last": ent["lastSeen"], "loaded": True,
            }
        else:
            hit = deck_hits(names, deck)
            if hit:
                weak = len(flat(hit["as"])) < MIN_DECK_CHARS
                seen_slides = {
                    "how": "weak" if weak else "deck",
                    "tier": "text", **hit,
                    "loaded": bool(hit["last"] and loaded_window[0]
                                   and hit["last"] >= loaded_window[0]),
                }
            elif tier == "ambiguous":
                seen_slides = {"how": "ambiguous", "tier": tier,
                               "candidates": [entities[k]["name"] for k in cands]}

        atier, akey, acands = ladder(names, crm, crm_stripped)
        seen_crm: dict | None = None
        if akey is not None:
            seen_crm = {"how": "crm", "tier": atier, **crm[akey]}
        elif atier == "ambiguous":
            seen_crm = {"how": "ambiguous", "tier": atier,
                        "candidates": [crm[k]["name"] for k in acands]}

        s_yes = bool(seen_slides and seen_slides["how"] in ("entity", "deck"))
        s_weak = bool(seen_slides and seen_slides["how"] == "weak")
        a_yes = bool(seen_crm and seen_crm["how"] == "crm")

        tally["slides"] += s_yes
        tally["weak"] += s_weak
        tally["affinity"] += a_yes
        tally["both"] += s_yes and a_yes
        tally["either"] += s_yes or a_yes
        tally["neither"] += not (s_yes or a_yes)
        if seen_slides:
            tally["how:" + seen_slides["how"]] += 1

        rows.append({**folder, "key": key, "slides": seen_slides, "crm": seen_crm})

    rows.sort(key=lambda r: r["name"].lower())

    return {
        "file": index_path.name,
        "sheet": SHEET,
        "version": version,
        "folders": len(folders),
        "skipped": skipped,
        "slidesFile": deck["file"],
        "slidePages": len(deck["pages"]),
        "slideMeetings": len(deck_dates),
        "slideWindow": [deck_dates[0], deck_dates[-1]] if deck_dates else None,
        "slideDateFixes": deck.get("dateFixes", []),
        "loadedWindow": list(loaded_window),
        "loadedMeetings": loaded_meetings,
        "slideEntities": len({e["id"] for e in entities.values()}),
        "affinityFile": affinity.name,
        "affinityRecords": len(crm),
        "counts": {k: v for k, v in tally.items() if not k.startswith("how:")},
        "how": {k[4:]: v for k, v in tally.items() if k.startswith("how:")},
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", type=Path, default=REPO_ROOT / "ui" / "index.html")
    ap.add_argument("--out", dest="dst", type=Path, default=None)
    ap.add_argument("--index", type=Path, default=INDEX)
    ap.add_argument("--slides", type=Path, default=SLIDES)
    ap.add_argument("--affinity", type=Path, default=AFFINITY)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()
    dst = args.dst or args.src

    for label, path in (("interface", args.src), ("index", args.index),
                        ("deck", args.slides), ("affinity export", args.affinity),
                        ("database", args.db)):
        if not path.exists():
            hint = "; run scripts/build_ui.py first" if label == "interface" else ""
            print(f"missing {label}: {path}{hint}", file=sys.stderr)
            return 1

    html = args.src.read_text()
    m = PAYLOAD_RE.search(html)
    if not m:
        print(f"no inlined payload in {args.src}", file=sys.stderr)
        return 1

    payload = json.loads(m.group(2))
    block = reach(args.index, args.slides, args.affinity, args.db, verbose=True)
    payload["indexReach"] = block
    payload.pop("driveIndex", None)
    for c in payload.get("companies", []):
        c.pop("idx", None)

    body = json.dumps(payload, separators=(",", ":"))
    dst.write_text(html[:m.start(2)] + body + html[m.end(2):])

    n = block["folders"]
    c = block["counts"]
    def pct(v): return f"{v / n:.0%}"
    print(f"wrote {dst.relative_to(REPO_ROOT)}  ({dst.stat().st_size / 1024:.0f} KB)")
    print(f"  index  {n} companies, version {block['version'] or 'unknown'}"
          + (f"  ({', '.join(block['skipped'])} not companies, excluded)"
             if block["skipped"] else ""))
    print(f"  deck   {block['slidePages']} pages, {block['slideMeetings']} meetings "
          f"{block['slideWindow'][0]} .. {block['slideWindow'][1]}")
    for fix in block["slideDateFixes"]:
        print(f"         page {fix['page']} reads {fix['stated']}; "
              f"the deck's own order makes it {fix['read_as']}")
    print(f"  crm    {block['affinityRecords']} Affinity organisations")
    print(f"  visible in slides or Affinity   {c['either']:4} of {n}  ({pct(c['either'])})")
    print(f"    slides                        {c['slides']:4}        ({pct(c['slides'])})")
    print(f"    Affinity                      {c['affinity']:4}        ({pct(c['affinity'])})")
    print(f"    both                          {c['both']:4}        ({pct(c['both'])})")
    print(f"  visible in neither              {c['neither']:4} of {n}  ({pct(c['neither'])})")
    print(f"  of those, a weak short-name deck hit only  {c['weak']:4}")
    for how, v in sorted(block["how"].items(), key=lambda x: -x[1]):
        print(f"    how={how:10} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
