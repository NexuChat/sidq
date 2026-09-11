"""Exercise the standalone findings page through its real HTTP boundary."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://sidq2.mlki.app"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("SIDQ_", "DATAHUB_"))
    }
    environment.update(
        SIDQ_LANDING_PORT="0",
        SIDQ_VENV_DIR=str(Path(sys.executable).parent.parent),
        SIDQ_ALLOWED_ORIGINS=ORIGIN,
        SIDQ_DATAHUB_TOKEN_FILE="/nonexistent/offline-site-must-not-read-this",
    )
    log_path = tmp_path_factory.mktemp("post-competition") / "server.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "web.post_competition"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=10), "site did not start"
            line = process.stdout.readline().strip()
            assert line.startswith("Listening on http://127.0.0.1:"), line
            yield line.removeprefix("Listening on ")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _request(site, path, *, method="GET", headers=None):
    request = urllib.request.Request(site + path, method=method, headers=headers or {})
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def test_offline_site_runs_all_four_demos_without_catalog_credentials(site):
    status, body = _request(site, "/readyz")
    assert status == 200 and json.loads(body)["mode"] == "offline"
    status, body = _request(site, "/healthz")
    assert status == 200
    assert set(json.loads(body)["live_demos"]) == {
        "rederive",
        "attestation",
        "gap-order",
        "gate-demo",
    }
    headers = {
        "Host": "sidq2.mlki.app",
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-Sidq-Demo": "capability",
    }
    for command, expected in {
        "rederive": "Re-derived without this project",
        "attestation": "TAMPERED",
        "gap-order": "budget",
        "gate-demo": "BLOCK",
    }.items():
        status, body = _request(site, f"/capability?command={command}", headers=headers)
        assert status == 200, body
        capability = json.loads(body)["capability"]
        status, body = _request(
            site,
            f"/run/{command}",
            method="POST",
            headers={**headers, "X-Sidq-Demo": "run", "X-Sidq-Capability": capability},
        )
        payload = json.loads(body)
        assert status == 200 and payload["exit_code"] == 0, payload
        assert expected in payload["output"], payload
        assert str(ROOT) not in body.decode(), "response exposed its host checkout path"


def test_offline_site_retains_static_file_and_request_boundaries(site):
    for path in ("/", "/index.html", "/styles.css", "/app.js"):
        status, body = _request(site, path)
        assert status == 200 and body
    for path in ("/server.py", "/.git/config", "/sidq.bundle", "/../pyproject.toml"):
        assert _request(site, path)[0] == 404
    assert _request(site, "/run/repair", method="POST")[0] == 404
    assert _request(site, "/run/attestation", method="POST")[0] == 403
    assert _request(site, "/capability?command=attestation")[0] == 403
