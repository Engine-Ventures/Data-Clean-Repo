"""The add form's public lookup.

Every test here stubs the HTTP layer. The point of these is not that Wikidata
returns the right thing -- it is that this module refuses to turn a
name-similar public company into a suggestion, and that a lookup which fails
in any way degrades to "type the values in" rather than to an exception in the
middle of someone adding a company.
"""

from __future__ import annotations

import urllib.error

import pytest

from evpipeline import lookup

Q = "Q119718658"


def _hit(label, aliases=(), qid=Q):
    return {"id": qid, "label": label, "aliases": list(aliases)}


def _entity(website=None, country_id=None, hq_id=None, label="Mistral AI"):
    claims = {}
    if website:
        claims[lookup.P_WEBSITE] = [
            {"mainsnak": {"datavalue": {"value": website}}}
        ]
    if country_id:
        claims[lookup.P_COUNTRY] = [
            {"mainsnak": {"datavalue": {"value": {"id": country_id}}}}
        ]
    if hq_id:
        claims[lookup.P_HEADQUARTERS] = [
            {"mainsnak": {"datavalue": {"value": {"id": hq_id}}}}
        ]
    return {"claims": claims, "labels": {"en": {"value": label}}}


@pytest.fixture
def api(monkeypatch):
    """Route every request through a scripted responder."""
    calls = []

    def responder(params):
        calls.append(params)
        return responder.routes(params)

    responder.calls = calls
    monkeypatch.setattr(lookup, "_get", responder)
    return responder


# --- matching ---------------------------------------------------------------

def test_an_exact_label_match_is_used(api):
    def routes(p):
        if p["action"] == "wbsearchentities":
            return {"search": [_hit("Mistral AI")]}
        if p.get("props", "").startswith("claims|"):
            return {"entities": {Q: _entity("https://mistral.ai/", country_id="Q142")}}
        return {"entities": {"Q142": {"labels": {"en": {"value": "France"}}}}}
    api.routes = routes

    s = lookup.suggest("Mistral AI")
    assert s.website == "https://mistral.ai/"
    assert s.hq_country == "France"
    assert s.citation == f"https://www.wikidata.org/wiki/{Q}"


def test_a_legal_suffix_does_not_prevent_a_match(api):
    api.routes = lambda p: (
        {"search": [_hit("Acme, Inc.")]} if p["action"] == "wbsearchentities"
        else {"entities": {Q: _entity("https://acme.com/", label="Acme, Inc.")}}
    )
    assert lookup.suggest("Acme").website == "https://acme.com/"


def test_a_merely_similar_top_hit_is_not_a_match(api):
    """The failure this module exists to prevent: the search API's first result
    is a ranked text hit, and for a startup name it is routinely a large public
    company that shares a word. A wrong country on a deal record costs more
    than an empty field does."""
    api.routes = lambda p: {"search": [
        _hit("Apple Inc."), _hit("Apple Records"), _hit("Apple Bank"),
    ]}
    s = lookup.suggest("Apple Orchard Robotics")
    assert s.empty
    assert "no Wikidata entity is named exactly" in s.note
    # It never went on to fetch anything.
    assert [c["action"] for c in api.calls] == ["wbsearchentities"]


def test_no_results_at_all_is_a_quiet_miss(api):
    api.routes = lambda p: {"search": []}
    s = lookup.suggest("Zzqqx Quantum Bio")
    assert s.empty and s.citation is None


def test_an_empty_name_asks_for_nothing(api):
    api.routes = lambda p: pytest.fail("should not have made a request")
    assert lookup.suggest("   ").note == "no name to look up"


# --- country ----------------------------------------------------------------

def test_the_headquarters_country_wins_over_the_entity_country(api):
    """P159 is the more specific claim and the one `hq_country` means."""
    def routes(p):
        if p["action"] == "wbsearchentities":
            return {"search": [_hit("Acme")]}
        if p["ids"] == Q and p.get("props") == "claims|labels":
            return {"entities": {Q: _entity(country_id="Q30", hq_id="Q90", label="Acme")}}
        if p["ids"] == "Q90":                       # the HQ city
            return {"entities": {"Q90": {"claims": {
                lookup.P_COUNTRY: [{"mainsnak": {"datavalue": {"value": {"id": "Q142"}}}}]}}}}
        return {"entities": {"Q142": {"labels": {"en": {"value": "France"}}}}}
    api.routes = routes
    assert lookup.suggest("Acme").hq_country == "France"


def test_an_entity_with_neither_value_says_so_rather_than_suggesting(api):
    api.routes = lambda p: (
        {"search": [_hit("Acme")]} if p["action"] == "wbsearchentities"
        else {"entities": {Q: _entity(label="Acme")}}
    )
    s = lookup.suggest("Acme")
    assert s.empty
    assert "records no website or country" in s.note
    # The citation still comes back: the entity exists, it just says nothing.
    assert s.citation == f"https://www.wikidata.org/wiki/{Q}"


# --- degradation ------------------------------------------------------------

@pytest.mark.parametrize(
    "boom",
    [
        urllib.error.URLError("offline"),
        urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
        TimeoutError(),
        OSError("connection reset"),
    ],
)
def test_a_failed_request_never_raises(api, boom):
    def routes(p):
        raise boom
    api.routes = routes
    s = lookup.suggest("Acme")
    assert s.empty
    assert "lookup unavailable" in s.note


def test_a_malformed_response_never_raises(api):
    api.routes = lambda p: {"search": [_hit("Acme")]} if p["action"] == "wbsearchentities" else None
    s = lookup.suggest("Acme")
    assert s.empty
    assert "unreadable response" in s.note
