from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

ELIGIBLE_DESCRIPTION = (
    "Build Python services in Berlin. Visa sponsorship is provided for this role."
)
GERMAN_DESCRIPTION = (
    "Fluent German is required. Visa sponsorship is not available. "
    "German or EU citizenship is required. Security clearance is required."
)
RECRUITER_DESCRIPTION = (
    "Recruiting agency role for a backend engineer. The client supports relocation."
)
UNCERTAIN_DESCRIPTION = (
    "Platform engineer role. Work authorization and language requirements are unclear."
)

@dataclass
class FixtureState:
    """Store the single named failure switch used by the acceptance workflow."""

    failed_source: str | None = None


@dataclass(frozen=True)
class AcceptanceServers:
    """Expose the loopback root for the isolated Jobsuche fixture."""

    jobsuche_url: str
    state: FixtureState


class _FixtureServer(ThreadingHTTPServer):
    fixture_kind: str
    fixture_state: FixtureState


def _response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _json_response(
    handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200
) -> None:
    _response(
        handler,
        status,
        json.dumps(payload, separators=(",", ":")).encode(),
        "application/json",
    )


class _Handler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep acceptance output deterministic and quiet."""

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        self._dispatch()

    def do_GET(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        kind = self.server.fixture_kind
        if self.server.fixture_state.failed_source == kind:
            _response(self, 503, b"fixture source unavailable", "text/plain")
            return
        if kind == "jobsuche":
            self._jobsuche()
        else:
            _response(self, 404, b"not found", "text/plain")

    def _jobsuche(self) -> None:
        path = self.path.split("?", 1)[0]
        root = _server_root(self.server)
        jobs = (
            ("JS-ELIGIBLE", "Visa Platform Engineer", ELIGIBLE_DESCRIPTION),
            ("JS-GERMAN", "German Security Engineer", GERMAN_DESCRIPTION),
            ("JS-RECRUITER", "Recruiter Backend Engineer", RECRUITER_DESCRIPTION),
            ("JS-UNCERTAIN", "Uncertain Platform Engineer", UNCERTAIN_DESCRIPTION),
        )
        if path == "/pc/v4/jobs":
            _json_response(
                self,
                {
                    "maxErgebnisse": len(jobs),
                    "stellenangebote": [
                        {
                            "refnr": external_id,
                            "arbeitgeber": "Federal Fixture GmbH",
                            "titel": title,
                            "arbeitsort": {"plz": "10115", "ort": "Berlin"},
                            "aktuelleVeroeffentlichungsdatum": "2026-08-01",
                            "externeUrl": f"{root}/apply/{external_id}",
                        }
                        for external_id, title, _description in jobs
                    ],
                },
            )
            return
        for external_id, title, description in jobs:
            if path == f"/pc/v4/jobdetails/{external_id}":
                _json_response(
                    self,
                    {
                        "refnr": external_id,
                        "arbeitgeber": "Federal Fixture GmbH",
                        "stellenangebotsTitel": title,
                        "arbeitsorte": [{"plz": "10115", "ort": "Berlin"}],
                        "aktuelleVeroeffentlichungsdatum": "2026-08-01",
                        "externeUrl": f"{root}/apply/{external_id}",
                        "stellenangebotsBeschreibung": description,
                    },
                )
                return
        _response(self, 404, b"not found", "text/plain")


def _server_root(server: ThreadingHTTPServer) -> str:
    host, port = cast(tuple[str, int], server.server_address)
    return f"http://{host}:{port}"


@contextmanager
def acceptance_servers() -> Iterator[AcceptanceServers]:
    """Run the loopback Jobsuche fixture on an OS-selected temporary port."""
    state = FixtureState()
    with ExitStack() as stack:
        jobsuche_url = stack.enter_context(_serve("jobsuche", state))
        yield AcceptanceServers(jobsuche_url=jobsuche_url, state=state)


@contextmanager
def _serve(kind: str, state: FixtureState) -> Iterator[str]:
    server = _FixtureServer(("127.0.0.1", 0), _Handler)
    server.fixture_kind = kind
    server.fixture_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _server_root(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
