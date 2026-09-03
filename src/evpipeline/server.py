"""A localhost server so the workbench can write back.

    python -m evpipeline.server          # then open http://127.0.0.1:8765

WHY THIS EXISTS: the page was a snapshot. A browser blocks `fetch()` over
`file://`, so the whole database had to be inlined into `ui/index.html` — which
is how 366 KB of company names and owner emails ended up committed to GitHub
twice. Serving over http fixes both problems at once: the page can POST a
decision back, and it fetches its data instead of carrying it, so no generated
file contains deal data.

NOT FOR DEPLOYMENT. Binds to 127.0.0.1, no authentication, no CSRF token, no
TLS, single user. It is a local tool for one person triaging a review queue,
and it should never be exposed to a network. There is deliberately no host
argument.

Standard library only — no FastAPI, no uvicorn. This is one user on one
machine hitting a handful of endpoints; adding two dependencies and an ASGI
server to the requirements would cost more than it buys, and `http.server` is
adequate for a single-threaded local tool.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from evpipeline import actions, write  # noqa: E402
from evpipeline.db import DEFAULT_DB_PATH  # noqa: E402
from evpipeline.validate import ValidationError  # noqa: E402
from evpipeline.write import DuplicateName  # noqa: E402

UI_DIR = REPO_ROOT / "ui"
PORT = 8765

# Who the decisions are attributed to. Single-user tool, so this is a constant
# rather than a login; it still ends up in review_item.resolved_by so the
# audit trail is not anonymous.
USER = "workbench"


def _collect(conn: sqlite3.Connection) -> dict:
    """Reuse build_ui.py's payload assembly rather than restating it.

    That function is the single definition of what the page needs. Importing it
    means the server and the static build can never drift.
    """
    from build_ui import collect

    return collect(conn)


class Handler(BaseHTTPRequestHandler):
    server_version = "evpipeline-workbench"

    def _send(self, code: int, payload: dict | None = None, body: bytes | None = None,
              ctype: str = "application/json") -> None:
        if payload is not None:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body or b"")))
        # No caching: the whole point is that the page reflects the database
        # as it is right now, after the last decision.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # One quiet line per request; the default logs to stderr very noisily.
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    # -- GET ---------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path == "/api/state":
                conn = sqlite3.connect(DEFAULT_DB_PATH)
                try:
                    self._send(200, _collect(conn))
                finally:
                    conn.close()
                return

            # template.html is the dual-mode file: served here it fetches
            # /api/state and the buttons are live; built by build_ui.py it
            # carries inlined data and hides them.
            name = "template.html" if path in ("/", "/index.html") else path.lstrip("/")
            target = (UI_DIR / name).resolve()
            # Never serve outside ui/.
            if not str(target).startswith(str(UI_DIR.resolve())) or not target.is_file():
                self._send(404, {"error": f"not found: {path}"})
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css",
                ".js": "text/javascript",
                ".json": "application/json",
            }.get(target.suffix, "application/octet-stream")
            self._send(200, body=target.read_bytes(), ctype=ctype)
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "server error; see the terminal"})

    # -- POST --------------------------------------------------------------

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad request body: {exc}"})
            return

        conn = sqlite3.connect(DEFAULT_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            result = self._dispatch(conn, path, body)
        except DuplicateName as exc:
            # Not a plain refusal: the caller can resubmit with
            # allow_duplicate=true and proceed, so the candidates it might be
            # go alongside the message rather than only being named in text.
            self._send(409, {
                "error": str(exc),
                "duplicates": [
                    {
                        "entity_id": d.entity_id, "name": d.name, "domain": d.domain,
                        "matched_on": d.matched_on, "matched_value": d.matched_value,
                    }
                    for d in exc.duplicates
                ],
            })
            return
        except ValidationError as exc:
            # A refused write is expected traffic, not a failure: the layer
            # says no to a merge into a merged entity, a phantom with no
            # reason, a proposal with no target. The message is written for
            # the person reading it, so pass it through verbatim.
            self._send(409, {"error": str(exc)})
            return
        except KeyError as exc:
            self._send(400, {"error": f"missing field: {exc}"})
            return
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "server error; see the terminal"})
            return
        finally:
            conn.close()

        if result is None:
            self._send(404, {"error": f"no such action: {path}"})
        else:
            self._send(200, result)

    def _dispatch(self, conn: sqlite3.Connection, path: str, body: dict) -> dict | None:
        if path == "/api/review/accept":
            return actions.accept_merge_proposal(conn, int(body["review_id"]), USER)
        if path == "/api/review/resolve":
            return actions.resolve_review(
                conn, int(body["review_id"]), body["state"], USER, body.get("note", "")
            )
        if path == "/api/merge":
            return actions.merge_entities(
                conn, int(body["src"]), int(body["dst"]), USER, body.get("note", "")
            )
        if path == "/api/unmerge":
            return actions.unmerge(conn, int(body["entity_id"]), USER)
        if path == "/api/phantom":
            return actions.mark_phantom(conn, int(body["entity_id"]), body["reason"], USER)
        if path == "/api/unphantom":
            return actions.unmark_phantom(conn, int(body["entity_id"]), USER)
        if path == "/api/company":
            result = write.add_company(
                conn,
                body["name"],
                body.get("values") or {},
                USER,
                allow_duplicate=bool(body.get("allow_duplicate")),
                field_sources={
                    k: tuple(v) for k, v in (body.get("field_sources") or {}).items()
                },
                tags=body.get("tags"),
            )
            return {
                "entity_id": result.entity_id,
                "name": result.name,
                "domain": result.domain,
                "written": result.written,
                "gaps": result.gaps,
                "tags": result.tags,
                "alias_claimed": result.alias_claimed,
                "review_ids": result.review_ids,
                "duplicates": [
                    {
                        "entity_id": d.entity_id, "name": d.name, "domain": d.domain,
                        "matched_on": d.matched_on, "matched_value": d.matched_value,
                    }
                    for d in result.duplicates
                ],
                "live_count": actions.live_count(conn),
            }
        if path == "/api/tags":
            tags_out = write.set_tags(conn, int(body["entity_id"]), body.get("tags"), USER)
            return {"entity_id": int(body["entity_id"]), "tags": tags_out}
        if path == "/api/vocab":
            # The form's dropdowns come from the database, not a copy in the
            # page, so a picklist can never drift from what validate.py accepts.
            return {
                t: [r[0] for r in conn.execute(f"SELECT name FROM {t} ORDER BY name")]
                for t in ("round_stage", "working_group", "affinity_status")
            }
        return None


def main() -> int:
    if not Path(DEFAULT_DB_PATH).exists():
        print(f"missing {DEFAULT_DB_PATH}; run scripts/build_db.py first", file=sys.stderr)
        return 1
    if not (UI_DIR / "template.html").is_file():
        print(f"missing {UI_DIR / 'template.html'}", file=sys.stderr)
        return 1

    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"workbench on http://127.0.0.1:{PORT}   (localhost only, no auth)")
    print(f"  database: {DEFAULT_DB_PATH}")
    print("  ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
