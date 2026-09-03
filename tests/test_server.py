"""Tests for src/evpipeline/server.py.

Runs the real HTTPServer on an ephemeral port and issues real HTTP requests --
not a mock of BaseHTTPRequestHandler. The dispatch table, the JSON encoding,
the status-code mapping and the path-traversal guard are all things a fake
request object would let slip past unnoticed.

Each test gets a private copy of the built database, monkeypatched in as
`evpipeline.server.DEFAULT_DB_PATH` -- that module imported the name directly
(`from evpipeline.db import DEFAULT_DB_PATH`), so it is a name in the server
module's own namespace and has to be patched there, not on `evpipeline.db`.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from evpipeline import server as server_module
from evpipeline.server import Handler


@pytest.fixture()
def live(report, tmp_path, monkeypatch):
    """A running workbench server, pointed at a private copy of the database."""
    db_path = tmp_path / "server_test.db"
    shutil.copyfile(report["_db_path"], db_path)
    monkeypatch.setattr(server_module, "DEFAULT_DB_PATH", db_path)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", db_path
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _get(base, path):
    resp = urllib.request.urlopen(f"{base}{path}", timeout=5)
    return resp.status, resp.headers, resp.read()


def _post(base, path, body):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, json.loads(resp.read())


def _post_expect_error(base, path, body):
    """POST that is expected to fail; urlopen raises HTTPError on 4xx/5xx."""
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    err = exc_info.value
    return err.code, json.loads(err.read())


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------


def test_api_state_returns_the_live_payload(live):
    base, _ = live
    status, _headers, body = _get(base, "/api/state")
    data = json.loads(body)
    assert status == 200
    assert data["totals"]["entities"] > 0
    assert "review" in data and "companies" in data


def test_api_state_is_never_cached(live):
    """The whole point of serving is that a decision is reflected immediately."""
    base, _ = live
    _, headers, _ = _get(base, "/api/state")
    assert headers.get("Cache-Control") == "no-store"


def test_api_state_reflects_a_write_without_restarting(live):
    """The dual-mode page's entire premise: fetch after a POST sees the change."""
    base, _db_path = live
    _, _, body1 = _get(base, "/api/state")
    entities_before = json.loads(body1)["totals"]["entities"]

    review = json.loads(body1)["review"]
    proposal = next(
        r for r in review if r["kind"] == "merge_proposal" and r.get("target")
    )
    status, _result = _post(base, "/api/review/accept", {"review_id": proposal["id"]})
    assert status == 200

    _, _, body2 = _get(base, "/api/state")
    entities_after = json.loads(body2)["totals"]["entities"]
    assert entities_after == entities_before - 1


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


def test_root_serves_the_dual_mode_template(live):
    base, _ = live
    status, headers, body = _get(base, "/")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    assert b"__PIPELINE_DATA__" in body, (
        "the served page must still carry the placeholder, not inlined data -- "
        "that is what makes IS_STATIC false and the buttons live"
    )


def test_unknown_static_path_is_404(live):
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base}/no-such-file.html", timeout=5)
    assert exc_info.value.code == 404


def test_path_traversal_outside_ui_dir_is_refused(live):
    """The one real security-relevant check in the static handler.

    Without the UI_DIR containment check this would serve pyproject.toml or
    worse straight off disk to anyone who can reach the port.
    """
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base}/../pyproject.toml", timeout=5)
    assert exc_info.value.code == 404


# ---------------------------------------------------------------------------
# POST dispatch and error mapping
# ---------------------------------------------------------------------------


def test_unknown_action_is_404(live):
    base, _ = live
    code, err = _post_expect_error(base, "/api/not-a-real-action", {})
    assert code == 404
    assert "no such action" in err["error"]


def test_malformed_json_body_is_400(live):
    base, _ = live
    req = urllib.request.Request(
        f"{base}/api/company", data=b"{not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 400
    body = json.loads(exc_info.value.read())
    assert "bad request body" in body["error"]


def test_missing_required_field_is_400_not_500(live):
    """_dispatch reads body["name"] directly; a missing key raises KeyError,
    which the handler maps to 400. Confirms that mapping, not a raw 500."""
    base, _ = live
    code, err = _post_expect_error(base, "/api/company", {"values": {}})
    assert code == 400
    assert "missing field" in err["error"]


def test_a_refused_write_is_409_with_the_validation_message_verbatim(live):
    """ValidationError text is written for a human to read; confirm it passes
    through the HTTP layer unchanged rather than being replaced or wrapped."""
    base, _ = live
    code, err = _post_expect_error(
        base, "/api/company", {"name": "No Domain Co", "values": {}}
    )
    assert code == 409
    assert "domain" in err["error"]


def test_server_error_does_not_leak_a_traceback_to_the_client(live, monkeypatch):
    """Anything unexpected still returns the generic message, not internals."""
    def boom(*a, **k):
        raise RuntimeError("unexpected failure detail")

    monkeypatch.setattr(server_module.write, "add_company", boom)
    base, _ = live
    code, err = _post_expect_error(
        base, "/api/company", {"name": "X", "values": {"website": "x.example"}}
    )
    assert code == 500
    assert "unexpected failure detail" not in err["error"]
    assert err["error"] == "server error; see the terminal"


# ---------------------------------------------------------------------------
# /api/company end to end -- the intake path, through the real HTTP layer
# ---------------------------------------------------------------------------


def test_add_company_end_to_end_through_http(live):
    base, _ = live
    status, out = _post(
        base,
        "/api/company",
        {
            "name": "Halide Thermal",
            "values": {
                "website": "halidethermal.example",
                "hq_country": "United States",
                "owner_name": "",
            },
        },
    )
    assert status == 200
    assert out["name"] == "Halide Thermal"
    assert set(out["written"]) == {"website", "hq_country"}
    assert set(out["gaps"]) >= {"owner_name", "stage", "round_size_usd"}
    assert "entity_id" in out


def test_add_company_public_field_without_citation_is_refused_over_http(live):
    base, _ = live
    code, err = _post_expect_error(
        base,
        "/api/company",
        {
            "name": "Uncited Co",
            "values": {"website": "uncited.example", "hq_country": "Chile"},
            "field_sources": {"hq_country": ["Public", None]},
        },
    )
    assert code == 409
    assert "citation" in err["error"]


def test_duplicate_domain_files_a_review_item_over_http(live):
    base, _ = live
    _, _ = _post(
        base, "/api/company",
        {"name": "First Co", "values": {"website": "dupe.example"}},
    )
    code, err = _post_expect_error(
        base, "/api/company",
        {"name": "Second Co", "values": {"website": "dupe.example"}},
    )
    assert code == 409
    assert "duplicates" in err  # DuplicateName's dedicated 409 shape
    assert err["duplicates"][0]["matched_on"] == "domain"


def test_api_vocab_returns_only_the_locked_picklists(live):
    base, _ = live
    status, out = _post(base, "/api/vocab", {})
    assert status == 200
    assert set(out) == {"round_stage", "working_group", "affinity_status"}
    assert "Seed" in out["round_stage"]
