#!/usr/bin/env python
"""Serve the review workbench with the add-company form enabled.

    python scripts/serve.py                 # http://127.0.0.1:8765
    python scripts/serve.py --port 9000 --user you
    python scripts/serve.py --no-lookup     # disable the public autofill lookup

`ui/index.html` stays exactly what `build_ui.py` produces: a portable
file:// snapshot with no write surface, safe to hand to someone. This server
renders the same template from the live database on every GET and exposes one
write endpoint. The form in the page hides itself when `location.protocol` is
file:, so the built snapshot keeps behaving as it always has.

Every GET re-runs `build_ui.collect`, so a company added through the form is
on the page as soon as it reloads -- there is no second copy of the payload to
keep in sync, and no cached state to go stale.

Writes go through `evpipeline.write`, which goes through `validate.write_field`.
Nothing here touches SQL directly, so the §8 rules apply to a browser write
exactly as they do to `--batch`.

The Index reach tab needs the deck, the index workbook and the Affinity
export, and takes about ten seconds to build, so it is computed once at
startup rather than per request; `--no-reach` skips it.

Three endpoints:

  POST /api/company   create a company (add_company)
  POST /api/tags      replace one company's tags (set_tags)
  GET  /api/lookup    a public suggestion for website / country (lookup.suggest)

`/api/lookup` is the only thing in this repo that reaches the internet, and it
reaches exactly one host (wikidata.org) with the company name a person just
typed. That is a confidential deal name leaving the building, so it is
deliberate rather than automatic: the browser calls it when someone asks for a
suggestion, and `--no-lookup` turns it off for the whole session. It reads
nothing and writes nothing.

Binds 127.0.0.1 only, and there is no authentication. This is a local tool
over confidential deal data; do not put it on a network interface.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_ui import DB, PLACEHOLDER, TEMPLATE, collect  # noqa: E402
from evpipeline import connect  # noqa: E402
from evpipeline.validate import ValidationError  # noqa: E402
from evpipeline.write import ADD_FIELDS, DuplicateName, add_company, set_tags  # noqa: E402

# Two optional pieces this server was written against that the tree does not
# carry yet. Both have an existing "off" state here -- --no-lookup, and a None
# _reach that the tab renders as unbuilt -- so a missing module degrades to
# that state rather than stopping the workbench from starting at all.
try:
    from evpipeline import lookup  # noqa: E402
except ImportError:
    lookup = None
try:
    from match_drive_index import reach  # noqa: E402
except ImportError:
    reach = None
from screen_diligence import WORKBOOK, screen, workbook_cohort  # noqa: E402

# A hand-typed company is one row; refusing anything larger keeps a stray
# POST from being read into memory.
MAX_BODY = 64 * 1024

# The fields a suggestion may be accepted for. `stage` and `owner_name` are
# not on it: no public source knows who owns a deal here, and round stage is a
# locked picklist that a scraped value would have to be mapped onto.
SUGGESTABLE = ("website", "hq_country")

# A citation the browser sends back with an accepted suggestion must be the
# lookup's own entity URL. Without this the client could attach any string to
# a 'Public' write, and §8's citation rule would be satisfied by something
# that supports nothing.
CITATION_RE = re.compile(r"^https://www\.wikidata\.org/wiki/Q\d+$")

# Set by --no-lookup.
_lookup_enabled = True


# Screening state, resolved once at startup. The served page must show the
# same population as the built deliverable -- serving the full 498 while
# ui/index.html carries the 185-company diligence cohort would make the two
# disagree about what "the pipeline" is.
_cohort: dict | None = None
_variants: dict | None = None

# The index-reach join, also resolved once at startup. It depends on the deck,
# the index workbook and the Affinity export rather than on anything a browser
# write can change, so recomputing it per request would cost ten seconds a page
# load and never differ. `None` means the sources were not there, and the tab
# says so rather than reading as an empty result.
_reach: dict | None = None


def render_page() -> bytes:
    conn = sqlite3.connect(DB)
    try:
        data = collect(conn)
    finally:
        conn.close()
    if _cohort is not None:
        data, _report = screen(data, _cohort, _variants)
    if _reach is not None:
        data["indexReach"] = _reach
    html = TEMPLATE.read_text()
    if PLACEHOLDER not in html:
        raise RuntimeError(f"template has no {PLACEHOLDER} placeholder")
    return html.replace(PLACEHOLDER, json.dumps(data, separators=(",", ":"))).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "evpipeline-workbench"
    default_user = "unknown"

    # ---- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _read_json(self) -> dict | None:
        """The request body as an object, or None having already sent a 400."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return None
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "empty or oversized body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return None
        return payload

    @staticmethod
    def _accepted_sources(payload: dict) -> dict[str, tuple[str, str | None]]:
        """Per-field source overrides for suggestions the person left in place.

        The browser reports which suggested fields it did not touch, and the
        citation they came from. A field the person edited is simply absent
        here and is written `Manual`, which is then the truth about where the
        value came from.
        """
        accepted = payload.get("accepted") or []
        if not accepted:
            return {}
        if not isinstance(accepted, list):
            raise ValidationError("'accepted' must be a list of field names")
        citation = str(payload.get("citation") or "")
        if not CITATION_RE.match(citation):
            raise ValidationError(
                f"a suggestion accepted for {', '.join(map(str, accepted))} needs its "
                f"lookup citation; {citation!r} is not one"
            )
        unknown = [f for f in accepted if f not in SUGGESTABLE]
        if unknown:
            raise ValidationError(
                f"nothing is suggested for {', '.join(map(str, unknown))}"
            )
        return {str(f): ("Public", citation) for f in accepted}

    def _tags(self) -> None:
        """Replace one company's tags. The only post-creation edit served."""
        payload = self._read_json()
        if payload is None:
            return
        try:
            entity_id = int(payload.get("entityId"))
        except (TypeError, ValueError):
            self._json(400, {"error": "entityId must be an integer"})
            return
        user = str(payload.get("user") or "").strip() or self.default_user

        conn = connect(DB)
        try:
            tags = set_tags(conn, entity_id, payload.get("tags"), user)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": f"database: {exc}"})
            return
        finally:
            conn.close()

        self._json(200, {"entityId": entity_id, "tags": tags})

    # ---- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            try:
                self._send(200, render_page(), "text/html; charset=utf-8")
            except Exception as exc:  # surface it in the browser, not just the console
                self._send(500, f"{type(exc).__name__}: {exc}".encode(), "text/plain")
            return
        if path == "/api/lookup":
            self._lookup(query)
            return
        self._send(404, b"not found", "text/plain")

    def _lookup(self, query: str) -> None:
        """One public suggestion for the name in `?name=`.

        Always 200 with a suggestion object, even when there is nothing to
        suggest -- an empty result is the expected case for the kind of company
        this pipeline tracks, not an error, and the form has to render it the
        same way either way.
        """
        if not _lookup_enabled:
            self._json(200, {
                "enabled": False,
                "fields": {},
                "note": ("lookup disabled with --no-lookup" if lookup is not None
                         else "no evpipeline.lookup module in this tree"),
            })
            return
        name = (urllib.parse.parse_qs(query).get("name") or [""])[0]
        sug = lookup.suggest(name)
        self._json(200, {
            **sug.as_dict(),
            "enabled": True,
            # Only the fields the form is allowed to accept a suggestion for,
            # so the page cannot offer to prefill something the write path
            # will not honour a Public source on.
            "fields": {f: getattr(sug, f) for f in SUGGESTABLE if getattr(sug, f)},
        })

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/tags":
            self._tags()
            return
        if path != "/api/company":
            self._json(404, {"error": "no such endpoint"})
            return

        payload = self._read_json()
        if payload is None:
            return

        name = str(payload.get("name") or "")
        user = str(payload.get("user") or "").strip() or self.default_user
        values = {f: payload.get(f) for f in ADD_FIELDS}
        allow_duplicate = bool(payload.get("allowDuplicate"))

        try:
            field_sources = self._accepted_sources(payload)
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return

        conn = connect(DB)
        try:
            result = add_company(
                conn, name, values, user,
                allow_duplicate=allow_duplicate,
                field_sources=field_sources,
                tags=payload.get("tags"),
            )
        except DuplicateName as exc:
            # 409 is the "warn, don't block" case: the browser shows the
            # matches and can re-POST with allowDuplicate.
            self._json(
                409,
                {
                    "error": str(exc),
                    "duplicates": [asdict(d) for d in exc.duplicates],
                },
            )
            return
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": f"database: {exc}"})
            return
        finally:
            conn.close()

        self._json(
            201,
            {
                "entityId": result.entity_id,
                "companyId": f"EV{result.entity_id:04d}",
                "name": result.name,
                "domain": result.domain,
                "written": result.written,
                "gaps": result.gaps,
                "tags": result.tags,
                "duplicates": [asdict(d) for d in result.duplicates],
                "reviewIds": result.review_ids,
            },
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python scripts/serve.py", description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workbook", default=str(WORKBOOK), help="cohort workbook for the screen")
    ap.add_argument(
        "--no-screen",
        dest="screen",
        action="store_false",
        help="serve the full population instead of the advanced-stage cohort",
    )
    ap.add_argument(
        "--user",
        default="",
        help="recorded as created_by when the browser does not send one",
    )
    ap.add_argument(
        "--no-reach",
        dest="reach",
        action="store_false",
        help="skip the index-reach join; the Index reach tab reads as unbuilt",
    )
    ap.add_argument(
        "--no-lookup",
        dest="lookup",
        action="store_false",
        help="disable the public autofill lookup; no company name leaves the machine",
    )
    args = ap.parse_args(argv)

    if not DB.exists():
        print(f"missing {DB}; run scripts/build_db.py first", file=sys.stderr)
        return 1
    if not TEMPLATE.exists():
        print(f"missing {TEMPLATE}", file=sys.stderr)
        return 1

    if args.user:
        Handler.default_user = args.user

    global _cohort, _variants, _lookup_enabled, _reach
    _lookup_enabled = args.lookup and lookup is not None
    workbook = Path(args.workbook)
    if args.screen and workbook.exists():
        _cohort, _variants = workbook_cohort(workbook)
    elif args.screen:
        print(f"no cohort workbook at {workbook}; serving the full population", file=sys.stderr)

    if args.reach and reach is not None:
        try:
            _reach = reach(verbose=True)
        except (FileNotFoundError, SystemExit) as exc:
            print(f"index-reach join unavailable ({exc}); the tab will say so",
                  file=sys.stderr)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"workbench on http://127.0.0.1:{args.port}  (ctrl-c to stop)")
    print(f"  db     {DB.relative_to(REPO_ROOT)}")
    print(f"  screen {'advanced-stage diligence cohort' if _cohort else 'off (full population)'}")
    if _reach:
        c = _reach["counts"]
        print(f"  reach  {c['either']} of {_reach['folders']} index companies visible "
              f"in the deck or Affinity; {c['neither']} in neither")
    else:
        print("  reach  off")
    print(f"  writes recorded as source=Manual, created_by={Handler.default_user!r}")
    print("  lookup " + ("wikidata.org (company names leave this machine)"
                        if _lookup_enabled else
                        "off (--no-lookup)" if lookup is not None else
                        "off (no evpipeline.lookup module)"))
    if reach is None:
        print("  note   match_drive_index.reach is missing; Index reach tab reads as unbuilt")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
