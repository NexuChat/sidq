"""Serve the landing page, and let a judge actually run the thing from it.

The page used to be static: it printed a verdict and a command, and a judge who
wanted to know whether either was real had to clone the repository. That is a fair
criticism of a submission whose entire claim is "do not take our word for it".

So this serves the same page and adds two endpoints that run the real commands on
the real machine. There is no input to either. The runnable set is a fixed table
of fixed argument lists — no parameter from the request reaches it, no shell is
involved, and a request naming anything outside the table is rejected before any
process starts. Both commands are read-only: one re-derives a published verdict
offline, the other audits the live catalog. Neither can write to a catalog, and
the mutating commands (`--write-receipts`, `repair --apply`) are deliberately
absent from the table rather than guarded inside it.

Concurrency is one run at a time, with a timeout, because this is a demonstration
endpoint on a small host and the honest failure is "busy", not a queue that grows
until the machine falls over.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
VENV = REPO / ".venv" / "bin"
TIMEOUT_SECONDS = 240

# The complete set of things this endpoint can run. Not a prefix, not a template —
# a closed table of exact argument lists.
RUNNABLE: dict[str, tuple[str, tuple[str, ...]]] = {
    "gate-demo": (
        (
            "Re-derive the published verdict from the committed graph recording. "
            "Offline: no DataHub, no network, no credentials."
        ),
        ("make", "gate-demo"),
    ),
    "audit": (
        (
            "Audit the live catalog through the official DataHub MCP server, "
            "read-only, most-consequential assets first."
        ),
        (str(VENV / "sidq"), "audit", "--via-mcp", "--budget", "5"),
    ),
    "repair": (
        (
            "Propose repairs from catalog evidence, then re-run the deterministic "
            "engine against the catalog each repair would create. Dry run — the "
            "--apply flag is deliberately absent from this table, so nothing "
            "is ever written from here."
        ),
        (str(VENV / "sidq"), "repair", "--via-mcp", "--budget", "15"),
    ),
}

_lock = threading.Lock()


def _run(name: str) -> dict[str, object]:
    description, argv = RUNNABLE[name]
    environment = {
        **os.environ,
        "DATAHUB_GMS_URL": os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "LOGURU_LEVEL": "WARNING",
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": " ".join(argv),
            "description": description,
            "exit_code": None,
            "output": f"timed out after {TIMEOUT_SECONDS}s",
        }
    # Exit 1 is a finding, not a failure, and the caller is told the code rather
    # than being shown a red banner for a working audit that found something.
    return {
        "command": " ".join(argv),
        "description": description,
        "exit_code": completed.returncode,
        "output": (completed.stdout or completed.stderr or "").strip(),
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:
        name = self.path.removeprefix("/run/").strip("/")
        if not self.path.startswith("/run/") or name not in RUNNABLE:
            self._json(404, {"error": "no such command"})
            return
        if not _lock.acquire(blocking=False):
            self._json(429, {"error": "a run is already in progress — try again"})
            return
        try:
            self._json(200, _run(name))
        finally:
            _lock.release()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args  # The systemd journal does not need a hit per asset.


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("SIDQ_LANDING_PORT", "8766"))
    with Server(("127.0.0.1", port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
