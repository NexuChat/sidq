"""Serve the post-competition site with four offline demonstrations.

Run from the repository with ``.venv/bin/python -m web.post_competition``.
The shared server owns request validation, capabilities, quotas, subprocess
limits, and response sanitization for both presentations.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from web import server

SUBMISSION_SHA = "02969cb46a86c44a7b411ff98d9e05c4f6fd3c93"


def _health_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "sidq-post-competition",
        "mode": "offline",
        "live_demos": sorted(server.RUNNABLE),
        "release": {
            "state": "deployed" if server._release_sha() else "local/dev",
            "commit_sha": server._release_sha(),
            "submission_baseline": SUBMISSION_SHA,
        },
    }


def _readiness_payload() -> dict[str, object]:
    ready = (
        all(shutil.which(tool) for tool in ("jq", "sha256sum", "make"))
        and (server.VENV / "python").is_file()
        and all(
            (server.REPO / name).is_file()
            for name in (
                "scripts/rederive.sh",
                "src/sidq/policy/default_policy.json",
                "demos/attestation.py",
                "demos/gap_order.py",
            )
        )
    )
    return {
        "status": "ready" if ready else "degraded",
        "service": "sidq-post-competition",
        "mode": "offline",
    }


def configure() -> None:
    server.ROOT = Path(__file__).resolve().parent / "post-competition"
    server.PUBLIC_ASSET_PATHS = frozenset(
        {"/", "/index.html", "/app.js", "/styles.css"}
    )
    server.DEFAULT_ALLOWED_ORIGINS = (
        "https://sidq2.mlki.app",
        "http://127.0.0.1:8767",
        "http://localhost:8767",
    )
    server.RUNNABLE = {
        "rederive": (
            "Independently re-derive the recorded verdict with jq, offline.",
            (
                str(server.REPO / "scripts/rederive.sh"),
                str(server.REPO / "examples/01-blocked-pii-dashboard/verdict.json"),
            ),
        ),
        "attestation": (
            "Check unsigned, signed, modified and transplanted receipts, offline.",
            (str(server.VENV / "python"), str(server.REPO / "demos/attestation.py")),
        ),
        "gap-order": (
            "Show how prior coverage gaps affect the next audit budget, offline.",
            (str(server.VENV / "python"), str(server.REPO / "demos/gap_order.py")),
        ),
        "gate-demo": server.RUNNABLE["gate-demo"],
    }
    server.EXPECTED_SECONDS = {name: 1 for name in server.RUNNABLE}
    server.COOLDOWN_SECONDS = 3
    server.CLIENT_RUN_LIMIT = len(server.RUNNABLE)
    server._health_payload = _health_payload
    server._readiness_payload = _readiness_payload
    server._command_locks = {name: threading.Lock() for name in server.RUNNABLE}


def main() -> None:
    configure()
    port = int(os.environ.get("SIDQ_LANDING_PORT", "8767"))
    with server.Server(("127.0.0.1", port), server.Handler) as service:
        print(f"Listening on http://127.0.0.1:{service.server_address[1]}", flush=True)
        service.serve_forever()


if __name__ == "__main__":
    main()
