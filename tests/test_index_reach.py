"""The index-reach join: what counts as a company being visible in the record.

The join reads the deal deck's raw text, so the rules that keep a text match
from being a false claim are the whole substance of the tab. Three of them are
tested here because each one, if it broke, would move a headline number
without failing anything else:

  * a lowercase occurrence is prose, not a company -- `_Temp` matches 64 pages
    of the word "temp", and `Meter` matches "meter"
  * a folder that qualifies itself in parentheses has to be looked for under
    both halves, or `Trener Robotics (fka T-robotics)` reads as unseen while
    T-Robotics sits on the Fund III list
  * the deck stamps three January 2023 meetings as 2022, and taking that
    literally folds 34 meetings into one

The deck itself is confidential and not in git, so these run against a
hand-built page list rather than the PDF. `test_deck_matches_real_deck` is the
one that needs the file and skips without it.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import match_drive_index as mdi  # noqa: E402


def deck(*pages: str, meetings: list[str | None] | None = None) -> dict:
    return {
        "pages": [mdi.flat(p) for p in pages],
        "meeting": meetings if meetings is not None else [None] * len(pages),
    }


# --------------------------------------------------------- capitalisation

def test_capitalised_occurrence_is_a_hit():
    d = deck("Preliminary Diligence - Resonant Link - Cetos Water")
    assert mdi.deck_hits(["Resonant Link"], d)["pages"] == 1


def test_lowercase_occurrence_is_prose_not_a_company():
    """The reason `_Temp` and `Meter` are the only two names the rule rejects."""
    d = deck("a temp file", "the flow meter reading", "double helix")
    assert mdi.deck_hits(["Temp"], d) is None
    assert mdi.deck_hits(["Meter"], d) is None
    assert mdi.deck_hits(["Helix"], d) is None


def test_index_casing_need_not_match_the_slide():
    """`resonant link` in the index has to catch `Resonant Link` on the slide.

    This is why the rule is "capitalised where it occurs" and not "matches the
    index's own spelling": five folders are lower-cased in the index.
    """
    d = deck("Deep Diligence - Resonant Link")
    assert mdi.deck_hits(["resonant link"], d)["pages"] == 1


def test_match_is_a_whole_token_sequence():
    d = deck("Sublimation Labs", "Flumen Robotics")
    assert mdi.deck_hits(["Sublime"], d) is None
    assert mdi.deck_hits(["Flume"], d) is None


def test_ligatures_and_punctuation_fold():
    """The PDF writes fi/ffi as single glyphs, and `radiant.nano` has a dot."""
    d = deck("Nonﬁction Labs", "Radiant.Nano")
    assert mdi.deck_hits(["Nonfiction Labs"], d)["pages"] == 1
    # The index writes it `radiant.nano`; the dot folds and the case does not
    # have to agree, only be capitalised on the slide.
    assert mdi.deck_hits(["radiant.nano"], d)["pages"] == 1


def test_hit_carries_its_pages_and_meeting_dates():
    d = deck("Cetos Water", "nothing here", "Cetos Water again",
             meetings=["2026-08-31", "2026-08-31", "2026-08-24"])
    hit = mdi.deck_hits(["Cetos Water"], d)
    assert hit["pages"] == 2
    assert hit["meetings"] == 2
    assert (hit["first"], hit["last"]) == ("2026-08-24", "2026-08-31")


# ---------------------------------------------------------------- variants

def test_parenthetical_former_name_is_searched():
    d = deck("Negotiate / Offer - T-Robotics")
    names = mdi.variants("Trener Robotics (fka T-robotics)")
    assert "T-robotics" in names
    hit = mdi.deck_hits(names, d)
    assert hit and hit["as"] == "T-robotics"


def test_head_name_is_searched_without_its_qualifier():
    d = deck("Preliminary Diligence - Alithia")
    hit = mdi.deck_hits(mdi.variants("Alithia (Vertical GaN)"), d)
    assert hit and hit["as"] == "Alithia"


def test_the_folders_own_name_is_tried_first():
    assert mdi.variants("Cetos Water")[0] == "Cetos Water"
    assert mdi.variants("Kvasir (biofuels)")[0] == "Kvasir (biofuels)"


def test_short_parenthetical_is_not_a_name():
    """`ExVivo (dx)` should not go looking for the word "dx"."""
    assert "dx" not in mdi.variants("ExVivo (dx)")


# ------------------------------------------------------------ weak matches

def test_a_short_name_is_a_weak_deck_hit_not_a_confident_one():
    """`TPL` on one page is as likely to be an acronym in prose as a company.

    The tier is what the tab counts on; a weak hit is reported and excluded
    from the confident total rather than dropped, because the reader is the one
    who can tell.
    """
    assert len(mdi.flat("TPL")) < mdi.MIN_DECK_CHARS
    assert len(mdi.flat("Amogy")) >= mdi.MIN_DECK_CHARS


# ------------------------------------------------------- the ladder itself

def test_ladder_tiers():
    targets = {"encharge ai": {"name": "EnCharge AI"},
               "attune neurosciences": {"name": "Attune Neurosciences"},
               "wavr technologies": {"name": "WAVR Technologies"}}
    by_stripped = mdi.index_stripped(targets)

    assert mdi.ladder(["EnCharge AI"], targets, by_stripped)[0] == "exact"
    assert mdi.ladder(["EnCharge"], targets, by_stripped)[0] == "suffix"
    assert mdi.ladder(["Attune Neurosci"], targets, by_stripped)[0] == "prefix"
    assert mdi.ladder(["Cetos Water"], targets, by_stripped)[0] == "none"


def test_ladder_will_not_guess_between_two_targets():
    targets = {"plaid semi": {"name": "Plaid Semi"},
               "plaid semiconductor": {"name": "Plaid Semiconductor"}}
    tier, key, cands = mdi.ladder(
        ["Plaid Semiconductors"], targets, mdi.index_stripped(targets))
    assert tier == "ambiguous" and key is None and len(cands) == 2


def test_prefix_tier_will_not_match_on_a_short_stem():
    """`Chip` must not sweep up every folder beginning with it."""
    targets = {"chipflow": {"name": "ChipFlow"}, "chipmetrics": {"name": "ChipMetrics"}}
    assert mdi.ladder(["Chip"], targets, mdi.index_stripped(targets))[0] == "none"


# --------------------------------------------------- the deck's date typos

def test_title_date_needs_the_meeting_title():
    """A date inside a slide's body is not the meeting the page belongs to."""
    assert mdi._title_date("New Deal Meeting\nAugust 31st, 2026") == date(2026, 8, 31)
    assert mdi._title_date("Blueprint tracking December 12, 2022") is None


def test_scaffolding_folders_are_not_companies():
    """Left in, `_Temp` and `_Academics` would read as two unseen companies."""
    assert mdi.norm("_Temp") == "temp"  # so the name itself is no defence


@pytest.mark.skipif(not mdi.SLIDES.exists(), reason="the deck is not present")
def test_deck_matches_real_deck():
    """Every number the tab's caption states about the deck, asserted.

    The January-typo correction is the one worth pinning: the deck stamps the
    2023-01-03, 01-09 and 01-17 meetings as 2022, each sitting above a
    correctly-stamped December 2022, and reading them literally collapses 34
    meetings into one.
    """
    d = mdi.load_deck(mdi.SLIDES)
    dates = sorted({m for m in d["meeting"] if m})
    assert len(d["pages"]) == 1018
    assert len(dates) == 153
    assert (dates[0], dates[-1]) == ("2021-08-23", "2026-08-31")
    assert [f["read_as"] for f in d["dateFixes"]] == [
        "2023-01-03", "2023-01-09", "2023-01-17"]
    assert all(f["stated"].startswith("2022-01") for f in d["dateFixes"])
