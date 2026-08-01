"""Serve the landing page, and let a judge actually run the thing from it.

The page used to be static: it printed a verdict and a command, and a judge who
wanted to know whether either was real had to clone the repository. That is a fair
criticism of a submission whose entire claim is "do not take our word for it".

So this serves the same page and adds five endpoints that run the real commands on
the real machine. There is no input to any. The runnable set is a fixed table
of fixed argument lists — no parameter from the request reaches it, no shell is
involved, and a request naming anything outside the table is rejected before any
process starts. All commands are read-only: one re-derives a published verdict
offline, one audits the live catalog, one proposes repairs without applying them,
one independently consumes a receipt, and one tests documented claims against a
live source. None can write to a catalog, and
the mutating commands (`--write-receipts`, `repair --apply`) are deliberately
absent from the table rather than guarded inside it.

Concurrency is one run at a time, with a timeout, because this is a demonstration
endpoint on a small host and the honest failure is "busy", not a queue that grows
until the machine falls over.
"""

from __future__ import annotations

import http.server
import json
import math
import os
import signal
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
VENV = REPO / ".venv" / "bin"
TIMEOUT_SECONDS = 240
COOLDOWN_SECONDS = 30
MAX_OUTPUT_BYTES = 64 * 1024
TRUNCATION_MARKER = "\n...[output truncated]"
HANDOFF_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; connect-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; form-action 'none'; img-src 'self' data:; "
        "object-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

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
    "handoff": (
        (
            "Agent B independently reads Agent A's persisted receipt from DataHub, "
            "then recomputes policy, schema and age freshness. Read-only."
        ),
        (str(VENV / "sidq"), "verify", HANDOFF_URN),
    ),
    "claims": (
        (
            "Measure documented field claims against the live source with bounded "
            "read-only SQL; query results remain on this host."
        ),
        ("make", "claims-demo"),
    ),
}

_lock = threading.Lock()
# Keyed by (client, command), not by client alone. The page offers three buttons
# and a reader clicks them in sequence; a blanket per-client cooldown would make
# the second click fail for no security gain, since `_lock` already allows only
# one run at a time no matter who asks. What this bounds is the same expensive
# command being replayed in a loop.
_last_run_finished: dict[tuple[str, str], float] = {}


def _truncate_output(output: str) -> str:
    output = output.strip()
    encoded = output.encode("utf-8")
    marker = TRUNCATION_MARKER.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return output
    prefix = encoded[: MAX_OUTPUT_BYTES - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _cooldown_remaining(client_ip: str, name: str) -> float:
    now = time.monotonic()
    expired = [
        key
        for key, finished in _last_run_finished.items()
        if now - finished >= COOLDOWN_SECONDS
    ]
    for key in expired:
        del _last_run_finished[key]
    finished = _last_run_finished.get((client_ip, name))
    return 0.0 if finished is None else COOLDOWN_SECONDS - (now - finished)


def _health_payload() -> dict[str, object]:
    """Liveness has no dependencies: if this returns, the landing process lives."""
    return {
        "status": "ok",
        "service": "sidq-landing",
        "live_demos": sorted(RUNNABLE),
    }


def _datahub_ready() -> bool:
    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080").rstrip("/")
    try:
        with urllib.request.urlopen(f"{gms_url}/health", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _readiness_payload(
    probe: Callable[[], bool] = _datahub_ready,
) -> dict[str, object]:
    """Readiness names the live dependency instead of returning a vague 503."""
    ready = probe()
    return {
        "status": "ready" if ready else "degraded",
        "service": "sidq-landing",
        "datahub": "ok" if ready else "unavailable",
    }


def _run(name: str) -> dict[str, object]:
    description, argv = RUNNABLE[name]
    environment = {
        **os.environ,
        "DATAHUB_GMS_URL": os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "LOGURU_LEVEL": "WARNING",
    }
    try:
        process = subprocess.Popen(
            argv,
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Each command gets a new session so the timeout cannot leave descendants
        # consuming the host after their parent is gone.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
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
        "exit_code": process.returncode,
        "output": _truncate_output(stdout or stderr or ""),
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)  # type: ignore[arg-type]

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, _health_payload())
            return
        if self.path == "/readyz":
            payload = _readiness_payload()
            self._json(200 if payload["status"] == "ready" else 503, payload)
            return
        super().do_GET()

    def do_POST(self) -> None:
        name = self.path.removeprefix("/run/").strip("/")
        if not self.path.startswith("/run/") or name not in RUNNABLE:
            self._json(404, {"error": "no such command"})
            return
        content_length = getattr(self, "headers", {}).get("Content-Length", "0")
        transfer_encoding = getattr(self, "headers", {}).get("Transfer-Encoding")
        try:
            has_body = int(content_length) != 0
        except ValueError:
            has_body = True
        if has_body or transfer_encoding:
            self._json(400, {"error": "request body is not accepted"})
            return
        if not _lock.acquire(blocking=False):
            self._json(429, {"error": "a run is already in progress — try again"})
            return
        client_ip = self.client_address[0]
        try:
            remaining = _cooldown_remaining(client_ip, name)
            if remaining > 0:
                self._json(
                    429,
                    {
                        "error": "run cooldown active — try again later",
                        "retry_after": math.ceil(remaining),
                    },
                )
                return
            payload = _run(name)
            _last_run_finished[client_ip, name] = time.monotonic()
            self._json(200, payload)
        finally:
            _lock.release()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
