"""Security boundary tests for the public judge-facing demo service."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).parents[1]


class _StaticAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        for name, value in attrs:
            if name in {"href", "src"} and value is not None:
                self.references.append(value)


def _handler(server, monkeypatch, responses, path: str, *, peer: str = "127.0.0.1"):
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.client_address = (peer, 12345)
    handler.headers = {
        "Host": "sidq.mlki.app",
        "Origin": "https://sidq.mlki.app",
        "Sec-Fetch-Site": "same-origin",
        server.DEMO_REQUEST_HEADER: server.DEMO_REQUEST_HEADER_VALUE,
        server.CAPABILITY_HEADER: server._issue_capability(
            server._client_identity(peer, {}), path.removeprefix("/run/")
        )[0],
    }
    monkeypatch.setattr(
        handler, "_json", lambda status, payload: responses.append((status, payload))
    )
    return handler


@pytest.fixture(autouse=True)
def _reset_public_server_state():
    from web import server

    server._reset_request_state_for_tests()
    yield
    server._reset_request_state_for_tests()


@pytest.mark.parametrize(
    "headers",
    (
        {
            "Host": "sidq.mlki.app",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
        {
            "Host": "sidq.mlki.app",
            "Origin": "https://sidq.mlki.app",
            "Sec-Fetch-Site": "same-origin",
        },
        {
            "Host": "sidq.mlki.app",
            "Origin": "https://sidq.mlki.app",
            "Sec-Fetch-Site": "same-site",
            "X-Sidq-Demo": "run",
        },
        {
            "Host": "attacker.example",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "same-origin",
            "X-Sidq-Demo": "run",
        },
    ),
)
def test_demo_posts_require_same_origin_fetch_metadata_and_custom_header(
    monkeypatch, headers
) -> None:
    from web import server

    responses: list = []
    handler = _handler(server, monkeypatch, responses, "/run/gate-demo")
    handler.headers = headers
    monkeypatch.setattr(
        server, "_run", lambda name: pytest.fail("rejected POST started a command")
    )

    handler.do_POST()

    assert responses == [(403, {"error": "cross-site demo request rejected"})]


def test_configured_origin_is_matched_exactly(monkeypatch) -> None:
    from web import server

    monkeypatch.setenv(
        "SIDQ_ALLOWED_ORIGINS",
        "https://sidq.mlki.app,https://preview.sidq.example",
    )
    valid = {
        "Host": "preview.sidq.example",
        "Origin": "https://preview.sidq.example",
        "Sec-Fetch-Site": "same-origin",
        server.DEMO_REQUEST_HEADER: server.DEMO_REQUEST_HEADER_VALUE,
    }
    suffix_attack = {**valid, "Host": "preview.sidq.example.attacker.test"}
    suffix_attack["Origin"] = "https://preview.sidq.example.attacker.test"

    assert server._request_is_same_origin(valid)
    assert not server._request_is_same_origin(suffix_attack)


def test_demo_capability_is_short_lived_one_time_and_not_authentication() -> None:
    from web import server

    token = server._issue_capability("203.0.113.19", "audit", now=100.0)[0]
    headers = {server.CAPABILITY_HEADER: token}

    validated = server._validate_capability(headers, "203.0.113.19", "audit", now=101.0)
    assert validated is not None
    assert server._accept_run_start("203.0.113.19", validated, now=101.0) == (
        None,
        0,
    )
    assert server._accept_run_start("203.0.113.19", validated, now=102.0)[0] == (
        "replay"
    )
    expired = server._issue_capability("203.0.113.19", "audit", now=200.0)[0]
    assert (
        server._validate_capability(
            {server.CAPABILITY_HEADER: expired},
            "203.0.113.19",
            "audit",
            now=200.0 + server.CAPABILITY_TTL_SECONDS + 1,
        )
        is None
    )


def test_capability_is_bound_to_client_and_command() -> None:
    from web import server

    wrong_client = server._issue_capability("203.0.113.19", "audit", now=100.0)[0]
    wrong_command = server._issue_capability("203.0.113.19", "audit", now=100.0)[0]

    assert (
        server._validate_capability(
            {server.CAPABILITY_HEADER: wrong_client},
            "198.51.100.8",
            "audit",
            now=101.0,
        )
        is None
    )
    assert (
        server._validate_capability(
            {server.CAPABILITY_HEADER: wrong_command},
            "203.0.113.19",
            "repair",
            now=101.0,
        )
        is None
    )


def test_capability_tampering_is_rejected() -> None:
    from web import server

    token = server._issue_capability("203.0.113.19", "audit", now=100.0)[0]
    replacement = "A" if token[-1] != "A" else "B"
    tampered = f"{token[:-1]}{replacement}"

    assert (
        server._validate_capability(
            {server.CAPABILITY_HEADER: tampered},
            "203.0.113.19",
            "audit",
            now=101.0,
        )
        is None
    )


def test_unattributable_cloudflared_visitors_do_not_share_a_client_bucket(
    monkeypatch,
) -> None:
    from web import server

    monkeypatch.delenv("SIDQ_TRUSTED_PROXIES", raising=False)
    current = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(server, "_run", lambda name: {"command": name})
    responses: list = []

    for visitor in range(server.CLIENT_RUN_LIMIT + 1):
        current[0] = 100.0 + visitor * (server.COOLDOWN_SECONDS + 1)
        forwarded = f"198.51.100.{visitor + 1}"

        capability = _handler(
            server,
            monkeypatch,
            responses,
            "/capability?command=audit",
        )
        capability.headers[server.DEMO_REQUEST_HEADER] = server.CAPABILITY_REQUEST_VALUE
        capability.headers["CF-Connecting-IP"] = forwarded
        capability.headers.pop(server.CAPABILITY_HEADER)
        capability.do_GET()
        token = responses[-1][1]["capability"]

        run = _handler(server, monkeypatch, responses, "/run/audit")
        run.headers["CF-Connecting-IP"] = forwarded
        run.headers[server.CAPABILITY_HEADER] = token
        run.do_POST()

    run_responses = responses[1::2]
    assert len(run_responses) == server.CLIENT_RUN_LIMIT + 1
    assert all(status == 200 for status, _ in run_responses)
    assert not server._client_run_started
    assert len(server._global_run_started) == server.CLIENT_RUN_LIMIT + 1
    assert 0 < len(server._consumed_capabilities) <= server.MAX_CONSUMED_CAPABILITIES
    assert len(server._last_run_finished) == 1

    server._reset_request_state_for_tests()
    for offset in range(server.GLOBAL_START_LIMIT):
        token = server._issue_capability(None, "audit", now=100.0)[0]
        validated = server._validate_capability(
            {server.CAPABILITY_HEADER: token}, None, "audit", now=101.0
        )
        assert validated is not None
        assert server._accept_run_start(None, validated, now=101.0) == (None, 0)

    token = server._issue_capability(None, "audit", now=100.0)[0]
    validated = server._validate_capability(
        {server.CAPABILITY_HEADER: token}, None, "audit", now=101.0
    )
    assert validated is not None

    scope, retry_after = server._accept_run_start(None, validated, now=101.0)

    assert scope == "global"
    assert retry_after == server.GLOBAL_START_WINDOW_SECONDS
    assert not server._client_run_started
    assert len(server._global_run_started) == server.GLOBAL_START_LIMIT
    assert len(server._consumed_capabilities) == server.MAX_CONSUMED_CAPABILITIES


def test_malformed_capability_with_huge_expiry_is_rejected_by_handler(
    monkeypatch,
) -> None:
    from web import server

    responses: list = []
    handler = _handler(server, monkeypatch, responses, "/run/audit")
    handler.headers[server.CAPABILITY_HEADER] = f"v1.{('9' * 3_000)}.nonce.{('0' * 64)}"
    monkeypatch.setattr(
        server, "_run", lambda name: pytest.fail("malformed token started a command")
    )

    handler.do_POST()

    assert responses == [
        (403, {"error": "demo capability missing, expired, or replayed"})
    ]


def test_256_spoofed_forwarded_identities_cannot_fill_capability_issuance(
    monkeypatch,
) -> None:
    from web import server

    monkeypatch.delenv("SIDQ_TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    responses: list = []

    for offset in range(256):
        handler = _handler(
            server,
            monkeypatch,
            responses,
            "/capability?command=audit",
        )
        handler.headers[server.DEMO_REQUEST_HEADER] = server.CAPABILITY_REQUEST_VALUE
        handler.headers["CF-Connecting-IP"] = (
            f"198.51.{offset // 250}.{offset % 250 + 1}"
        )
        handler.headers.pop(server.CAPABILITY_HEADER)
        handler.do_GET()

    judge = _handler(
        server,
        monkeypatch,
        responses,
        "/capability?command=audit",
    )
    judge.headers[server.DEMO_REQUEST_HEADER] = server.CAPABILITY_REQUEST_VALUE
    judge.headers["CF-Connecting-IP"] = "203.0.113.19"
    judge.headers.pop(server.CAPABILITY_HEADER)
    judge.do_GET()

    assert len(responses) == 257
    assert all(status == 200 for status, _ in responses)
    assert not server._consumed_capabilities

    token = responses[-1][1]["capability"]
    runs: list = []
    run = _handler(server, monkeypatch, runs, "/run/audit")
    run.headers["CF-Connecting-IP"] = "203.0.113.19"
    run.headers[server.CAPABILITY_HEADER] = token
    monkeypatch.setattr(server, "_run", lambda name: {"command": name})
    run.do_POST()

    assert runs[0][0] == 200
    assert len(server._consumed_capabilities) == 1

    server._last_run_finished.clear()
    replay = _handler(server, monkeypatch, runs, "/run/audit")
    replay.headers[server.CAPABILITY_HEADER] = token
    replay.do_POST()

    assert runs[-1][0] == 403


def test_capability_issuance_is_stateless_and_does_not_grow_replay_memory() -> None:
    from web import server

    for offset in range(10_000):
        server._issue_capability("127.0.0.1", "audit", now=float(offset))

    assert not server._consumed_capabilities


def test_replay_ledger_is_bounded_and_prunes_expired_entries() -> None:
    from web import server

    for offset in range(server.MAX_CONSUMED_CAPABILITIES):
        client = f"198.51.100.{offset + 1}"
        token = server._issue_capability(client, "audit", now=100.0)[0]
        validated = server._validate_capability(
            {server.CAPABILITY_HEADER: token}, client, "audit", now=101.0
        )
        assert validated is not None
        assert server._accept_run_start(client, validated, now=101.0) == (None, 0)

    assert len(server._consumed_capabilities) == server.MAX_CONSUMED_CAPABILITIES
    assert server.MAX_CONSUMED_CAPABILITIES <= server.GLOBAL_START_LIMIT

    token = server._issue_capability("203.0.113.19", "audit", now=701.0)[0]
    validated = server._validate_capability(
        {server.CAPABILITY_HEADER: token}, "203.0.113.19", "audit", now=702.0
    )
    assert validated is not None
    assert server._accept_run_start("203.0.113.19", validated, now=702.0) == (
        None,
        0,
    )
    assert len(server._consumed_capabilities) == 1


def test_rejected_busy_start_does_not_enter_the_replay_ledger(
    monkeypatch,
) -> None:
    from web import server

    token = server._issue_capability(None, "audit", now=100.0)[0]
    responses: list = []
    monkeypatch.setattr(server.time, "monotonic", lambda: 101.0)
    handler = _handler(server, monkeypatch, responses, "/run/audit")
    handler.headers[server.CAPABILITY_HEADER] = token
    assert server._command_locks["audit"].acquire(blocking=False)

    try:
        handler.do_POST()
    finally:
        server._command_locks["audit"].release()

    assert responses[0][0] == 429
    assert not server._consumed_capabilities


def test_cloudflare_client_ip_requires_an_explicit_trusted_proxy(
    monkeypatch,
) -> None:
    from web import server

    headers = {"CF-Connecting-IP": "203.0.113.19"}

    monkeypatch.delenv("SIDQ_TRUSTED_PROXIES", raising=False)
    assert server._client_identity("127.0.0.1", headers) is None
    assert server._client_identity("198.51.100.8", headers) is None

    monkeypatch.setenv("SIDQ_TRUSTED_PROXIES", "127.0.0.1/32")
    assert server._client_identity("127.0.0.1", headers) == "203.0.113.19"


def test_invalid_cloudflare_client_ip_has_no_attributable_identity() -> None:
    from web import server

    assert (
        server._client_identity("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"}) is None
    )


def test_one_judge_can_run_every_demo_but_not_evade_limits_by_alternating() -> None:
    from web import server

    for offset, name in enumerate(server.RUNNABLE):
        assert server._reserve_client_run("203.0.113.19", 100.0 + offset) == 0

    retry_after = server._reserve_client_run("203.0.113.19", 106.0)

    assert retry_after == server.CLIENT_WINDOW_SECONDS - 6


def test_global_rolling_start_cap_cannot_be_evaded_with_new_client_ips() -> None:
    from web import server

    for offset in range(server.GLOBAL_START_LIMIT):
        scope, retry_after = server._reserve_run_start(
            f"203.0.113.{offset + 1}", now=100.0 + offset
        )
        assert (scope, retry_after) == (None, 0)

    scope, retry_after = server._reserve_run_start("198.51.100.1", now=125.0)

    assert scope == "global"
    assert retry_after == server.GLOBAL_START_WINDOW_SECONDS - 25


def test_different_commands_can_run_together_up_to_the_global_cap(
    monkeypatch,
) -> None:
    from web import server

    entered = threading.Barrier(server.GLOBAL_CONCURRENCY + 1)
    release = threading.Event()
    responses: list = []

    def blocking_run(name: str):
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return {
            "command": name,
            "description": name,
            "elapsed_seconds": 0.1,
            "expected_seconds": 1,
            "exit_code": 0,
            "output": "ok",
        }

    monkeypatch.setattr(server, "_run", blocking_run)
    names = list(server.RUNNABLE)[: server.GLOBAL_CONCURRENCY]
    threads = [
        threading.Thread(
            target=_handler(server, monkeypatch, responses, f"/run/{name}").do_POST
        )
        for name in names
    ]
    for thread in threads:
        thread.start()
    entered.wait(timeout=2)

    _handler(
        server,
        monkeypatch,
        responses,
        f"/run/{list(server.RUNNABLE)[server.GLOBAL_CONCURRENCY]}",
    ).do_POST()
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert any(
        status == 429
        and payload.get("retry_after")
        and "capacity" in payload.get("error", "")
        for status, payload in responses
    )
    assert sum(status == 200 for status, _ in responses) == server.GLOBAL_CONCURRENCY


def test_public_result_hides_repo_paths_and_internal_endpoints(
    monkeypatch, tmp_path
) -> None:
    from web import server

    class Completed:
        returncode = 0

        def communicate(self, timeout):
            assert timeout == server.TIMEOUT_SECONDS
            return (
                (
                    f"read {server.REPO}/private.json via "
                    "http://localhost:8080 and http://datahub-gms-quickstart:8080"
                ),
                "",
            )

    token_file = tmp_path / "reader-token"
    token_file.write_text("reader-secret\n", encoding="utf-8")
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: Completed())

    result = server._run("audit")
    rendered = f"{result['command']}\n{result['output']}"

    assert str(server.REPO) not in rendered
    assert str(server.VENV) not in rendered
    assert "localhost" not in rendered
    assert "datahub-gms" not in rendered
    assert result["command"].startswith("sidq audit")
    assert result["elapsed_seconds"] >= 0
    assert result["expected_seconds"] > 0


def test_command_environments_are_allowlisted_and_credentials_are_scoped(
    monkeypatch, tmp_path
) -> None:
    from web import server

    token_file = tmp_path / "reader-token"
    claims_file = tmp_path / "claims-dsn"
    token_file.write_text("reader-secret\n", encoding="utf-8")
    claims_file.write_text(
        "host=127.0.0.1 user=sidq_reader password=secret\n", encoding="utf-8"
    )
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("SIDQ_CLAIMS_DSN_FILE", str(claims_file))
    monkeypatch.setenv("UNRELATED_AMBIENT_SECRET", "must-not-pass")

    gate = server._command_environment("gate-demo")
    datahub_environments = {
        name: server._command_environment(name)
        for name in ("audit", "repair", "handoff", "claims")
    }
    audit = datahub_environments["audit"]
    claims = datahub_environments["claims"]

    assert "UNRELATED_AMBIENT_SECRET" not in gate | audit | claims
    assert "DATAHUB_GMS_TOKEN" not in gate and "CLAIMS_SOURCE" not in gate
    assert all(
        environment["DATAHUB_GMS_TOKEN"] == "reader-secret"
        for environment in datahub_environments.values()
    )
    assert all(
        "CLAIMS_SOURCE" not in environment
        for name, environment in datahub_environments.items()
        if name != "claims"
    )
    assert "CLAIMS_SOURCE" not in audit
    assert claims["DATAHUB_GMS_TOKEN"] == "reader-secret"
    assert claims["CLAIMS_SOURCE"].startswith("host=127.0.0.1 user=sidq_reader")
    assert claims["HF_HOME"] == str(server.RUNTIME / "huggingface")
    assert claims["SENTENCE_TRANSFORMERS_HOME"] == str(
        server.RUNTIME / "huggingface" / "hub"
    )
    assert claims["HF_HUB_OFFLINE"] == "1"
    assert claims["TRANSFORMERS_OFFLINE"] == "1"
    assert claims["TOKENIZERS_PARALLELISM"] == "false"
    for key in (
        "HF_HOME",
        "SENTENCE_TRANSFORMERS_HOME",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "TOKENIZERS_PARALLELISM",
    ):
        assert key not in gate and key not in audit
    assert set(gate) <= server.COMMAND_ENV_ALLOWLIST
    assert all(
        set(environment) <= server.COMMAND_ENV_ALLOWLIST
        for environment in (gate, *datahub_environments.values())
    )


def test_claims_source_is_not_published_or_passed_in_process_arguments() -> None:
    from web import server

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    claims_recipe = makefile.split("claims-demo:", 1)[1].split("\n\n", 1)[0]
    secret = "postgresql://reader:secret@warehouse/private"
    dry_run = subprocess.run(
        ["make", "-n", f"CLAIMS_SOURCE={secret}", "claims-demo"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--source" not in claims_recipe
    assert secret not in dry_run.stdout + dry_run.stderr
    assert secret not in server._public_command(server.RUNNABLE["claims"][1])


def test_public_output_redacts_credential_file_values_and_tolerates_missing_files(
    monkeypatch, tmp_path
) -> None:
    from web import server

    token_file = tmp_path / "reader-token"
    claims_file = tmp_path / "claims-dsn"
    token_file.write_text("token-value\n", encoding="utf-8")
    claims_file.write_text(
        "postgresql://reader:dsn-value@warehouse/db\n", encoding="utf-8"
    )
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("SIDQ_CLAIMS_DSN_FILE", str(claims_file))

    output = server._sanitize_public_text(
        "token-value postgresql://reader:dsn-value@warehouse/db"
    )

    assert output == "[redacted] [redacted]"

    claims_file.unlink()
    assert server._sanitize_public_text("ordinary error") == "ordinary error"


@pytest.mark.parametrize("failure", ("credential", "process"))
def test_run_returns_a_generic_result_when_setup_or_process_start_fails(
    monkeypatch, tmp_path, failure: str
) -> None:
    from web import server

    secret = "must-never-be-public"
    missing = tmp_path / secret
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(missing))

    if failure == "process":
        token_file = tmp_path / "reader-token"
        token_file.write_text(f"{secret}\n", encoding="utf-8")
        monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))

        def unavailable(*args, **kwargs):
            raise OSError(f"cannot execute {secret}")

        monkeypatch.setattr(server.subprocess, "Popen", unavailable)

    result = server._run("audit")

    assert result["exit_code"] is None
    assert result["output"] == "command unavailable"
    assert secret not in str(result)


def test_handoff_has_explicit_judging_age_and_complete_semantic_context() -> None:
    from web import server

    description, argv = server.RUNNABLE["handoff"]

    assert argv[-2:] == ("--max-age-days", "45")
    for required in ("semantic entity", "complete one-hop lineage", "policy", "age"):
        assert required in description


def test_readiness_requires_credential_file_and_authenticated_catalog_access(
    monkeypatch, tmp_path
) -> None:
    from web import server

    missing = tmp_path / "missing-token"
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(missing))
    monkeypatch.setattr(
        server,
        "_datahub_ready",
        lambda token: pytest.fail("missing credential reached the catalog probe"),
    )

    assert server._readiness_payload() == {
        "status": "degraded",
        "service": "sidq-landing",
        "datahub": "unavailable",
    }
    server._reset_request_state_for_tests()

    secret = "readiness-secret-must-not-leak"
    token_file = tmp_path / "reader-token"
    token_file.write_text(f"{secret}\n", encoding="utf-8")
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))
    observed: list[str] = []
    monkeypatch.setattr(
        server, "_datahub_ready", lambda token: observed.append(token) or True
    )

    payload = server._readiness_payload()

    assert payload == {
        "status": "ready",
        "service": "sidq-landing",
        "datahub": "ok",
    }
    assert observed == [secret]
    assert secret not in str(payload)
    assert str(token_file) not in str(payload)


def test_catalog_readiness_probe_uses_bearer_auth_and_a_read_only_graphql_query(
    monkeypatch,
) -> None:
    from web import server

    class Response:
        status = 200

        def __init__(self, body: dict) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            assert limit == server.MAX_READINESS_RESPONSE_BYTES
            return json.dumps(self.body).encode("utf-8")

    requests = []
    response_body = {"data": {"search": {"total": 0}}}

    def open_request(request, timeout):
        requests.append(request)
        assert timeout == server.READINESS_TIMEOUT_SECONDS
        return Response(response_body)

    monkeypatch.setenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(server.urllib.request, "urlopen", open_request)

    assert server._datahub_ready("catalog-secret")
    request = requests[0]
    assert request.full_url == "http://127.0.0.1:8080/api/graphql"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer catalog-secret"
    assert b"search" in request.data and b"mutation" not in request.data.lower()

    response_body.clear()
    response_body["errors"] = [{"message": "denied"}]
    assert not server._datahub_ready("catalog-secret")


def test_readiness_result_is_cached_until_its_short_ttl_expires(
    monkeypatch, tmp_path
) -> None:
    from web import server

    secret = "readiness-cache-secret"
    token_file = tmp_path / "reader-token"
    token_file.write_text(secret, encoding="utf-8")
    monkeypatch.setenv("SIDQ_DATAHUB_TOKEN_FILE", str(token_file))
    current = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: current[0])
    refresh_started = threading.Event()
    refresh_release = threading.Event()
    probes: list[str] = []

    def probe(token: str) -> bool:
        probes.append(token)
        if len(probes) == 1:
            return True
        refresh_started.set()
        assert refresh_release.wait(timeout=2)
        return False

    monkeypatch.setattr(server, "_datahub_ready", probe)

    assert server._readiness_payload()["status"] == "ready"
    current[0] += server.READINESS_CACHE_TTL_SECONDS - 0.1
    assert server._readiness_payload()["status"] == "ready"
    assert probes == [secret]

    current[0] += 0.1
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh = executor.submit(server._readiness_payload)
        assert refresh_started.wait(timeout=2)
        stale = executor.submit(server._readiness_payload)
        try:
            stale_payload = stale.result(timeout=0.5)
            assert not refresh.done()
        finally:
            refresh_release.set()
        refreshed_payload = refresh.result(timeout=2)

    assert stale_payload["status"] == "ready"
    assert refreshed_payload["status"] == "degraded"
    assert probes == [secret, secret]
    assert secret not in repr(server._readiness_cache)


def test_concurrent_readiness_requests_share_one_in_flight_probe(monkeypatch) -> None:
    from web import server

    secret = "single-flight-secret"
    started = threading.Event()
    release = threading.Event()
    probes: list[str] = []
    monkeypatch.setattr(server, "_read_credential", lambda name: secret)

    def probe(token: str) -> bool:
        probes.append(token)
        started.set()
        assert release.wait(timeout=2)
        return True

    monkeypatch.setattr(server, "_datahub_ready", probe)

    with ThreadPoolExecutor(max_workers=8) as executor:
        owner = executor.submit(server._readiness_payload)
        assert started.wait(timeout=2)
        non_owners = [executor.submit(server._readiness_payload) for _ in range(7)]
        try:
            payloads = [future.result(timeout=0.5) for future in non_owners]
            assert not owner.done()
        finally:
            release.set()
        owner_payload = owner.result(timeout=2)

    assert probes == [secret]
    assert all(payload["status"] == "degraded" for payload in payloads)
    assert owner_payload["status"] == "ready"
    assert secret not in repr(server._readiness_cache)


def test_readiness_probe_exception_is_degraded_and_negatively_cached(
    monkeypatch,
) -> None:
    from web import server

    secret = "exception-secret-must-not-leak"
    probes: list[str] = []
    monkeypatch.setattr(server, "_read_credential", lambda name: secret)

    def failing_probe(token: str) -> bool:
        probes.append(token)
        raise RuntimeError(secret)

    monkeypatch.setattr(server, "_datahub_ready", failing_probe)

    first = server._readiness_payload()
    second = server._readiness_payload()

    assert (
        first
        == second
        == {
            "status": "degraded",
            "service": "sidq-landing",
            "datahub": "unavailable",
        }
    )
    assert probes == [secret]
    assert secret not in str(first)
    assert secret not in repr(server._readiness_cache)


@pytest.mark.parametrize(
    ("status", "datahub", "expected_status"),
    (("ready", "ok", 200), ("degraded", "unavailable", 503)),
)
def test_readiness_endpoint_serves_status_json_and_security_headers_over_loopback(
    monkeypatch, status: str, datahub: str, expected_status: int
) -> None:
    from web import server

    payload = {
        "status": status,
        "service": "sidq-landing",
        "datahub": datahub,
    }
    monkeypatch.setattr(server, "_readiness_payload", lambda: payload)

    with server.Server(("127.0.0.1", 0), server.Handler) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*service.server_address, timeout=2)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
            service.shutdown()
            thread.join(timeout=2)

    assert response.status == expected_status
    assert body == payload
    assert response.getheader("Content-Type") == "application/json"
    assert response.getheader("Cache-Control") == "no-store"
    for name, value in server.SECURITY_HEADERS.items():
        assert response.getheader(name) == value


def test_landing_uses_a_hardened_external_script_and_progress_text() -> None:
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web/styles.css").read_text(encoding="utf-8")

    assert "username:" not in html.lower()
    assert "password:" not in html.lower()
    assert '<script src="app.js?v=48de92b937cbdd17" defer></script>' in html
    assert "X-Sidq-Demo" in script
    assert "/capability" in script and "X-Sidq-Capability" in script
    assert "setInterval" in script and "elapsed" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert '<link rel="stylesheet" href="styles.css?v=d827dadad3d44a61">' in html
    assert "<style" not in html
    assert "style=" not in html
    assert not re.search(r"<script(?![^>]+\bsrc=)", html)
    assert "body" in styles and ".logo" in styles

    from web.server import SECURITY_HEADERS

    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "script-src 'self' 'unsafe-inline'" not in csp
    assert "style-src 'self'" in csp and "style-src 'self' 'unsafe-inline'" not in csp


def test_landing_external_static_assets_are_content_addressed() -> None:
    web_root = ROOT / "web"
    parser = _StaticAssetParser()
    parser.feed((web_root / "index.html").read_text(encoding="utf-8"))

    assets: list[tuple[str, Path]] = []
    for reference in parser.references:
        parsed = urlsplit(reference)
        asset = web_root / parsed.path.lstrip("/")
        if not parsed.scheme and not parsed.netloc and asset.is_file():
            assets.append((reference, asset))

    assert {asset.name for _, asset in assets} >= {
        "app.js",
        "architecture.svg",
        "styles.css",
    }
    for reference, asset in assets:
        version = parse_qs(urlsplit(reference).query).get("v")
        expected = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]

        assert version == [expected], reference
        assert re.fullmatch(r"[0-9a-f]{16}", version[0])


def test_deployment_keeps_raw_services_local_and_secrets_out_of_units() -> None:
    compose = (ROOT / "demo/docker-compose.yml").read_text(encoding="utf-8")
    landing = (ROOT / "deploy/sidq-landing.service").read_text(encoding="utf-8")
    tunnel = (ROOT / "deploy/cloudflared-sidq.service").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert '"127.0.0.1:55432:5432"' in compose
    assert "DynamicUser=true" in landing and "User=dev" not in landing
    assert "SENTENCE_TRANSFORMERS_HOME=/opt/sidq/runtime/huggingface/hub" in landing
    assert "WorkingDirectory=/opt/sidq/current" in landing
    assert "ExecStart=/opt/sidq/runtime/venv/bin/python" in landing
    assert (
        "LoadCredential=datahub-reader-token:/etc/sidq/datahub-reader.token" in landing
    )
    assert "LoadCredential=claims-dsn:/etc/sidq/claims.dsn" in landing
    assert "EnvironmentFile=" not in landing and "DATAHUB_GMS_TOKEN=" not in landing
    assert "SIDQ_TRUSTED_PROXIES" not in landing
    assert "ProtectProc=invisible" in landing and "ProtectHome=true" in landing
    assert "/home/dev" not in landing
    assert "DynamicUser=true" in tunnel and "User=dev" not in tunnel
    assert "EnvironmentFile=" not in tunnel
    assert "LoadCredential=sidq.token:/etc/cloudflared/sidq.token" in tunnel
    assert "--token-file %d/sidq.token" in tunnel
    assert "ExecStart=/usr/local/bin/cloudflared-sidq" in tunnel
    assert "ProtectProc=invisible" in tunnel and "ProtectHome=true" in tunnel
    assert "/home/dev" not in tunnel
    assert "MemoryMax=2G" in landing
    assert "observed 1G" in landing and "two" in landing.lower()
    for required in (
        "DATAHUB_GMS_TOKEN",
        "root:root",
        "0600",
        "Reader",
        "127.0.0.1",
        "firewall",
        "GRANT SELECT",
        "Admin generates the PAT on behalf of the Reader identity",
        "never an Admin PAT",
        "/opt/sidq/current",
        "LoadCredential",
        "default is 7 days",
        "45-day judging window",
        "microsoft/harrier-oss-v1-270m",
        "HF_HUB_OFFLINE=1",
    ):
        assert required in security


def test_datahub_secure_override_merges_auth_versions_and_private_ports(
    tmp_path,
) -> None:
    base = tmp_path / "base.yml"
    base.write_text(
        textwrap.dedent(
            """
            services:
              datahub-gms-quickstart:
                image: acryldata/datahub-gms:${DATAHUB_VERSION}
                ports: ["8080:8080"]
                volumes:
                  - "${HOME}/.datahub/plugins:/etc/datahub/plugins"
                  - "${HOME}/.datahub/search:/etc/datahub/search"
              frontend-quickstart:
                image: acryldata/datahub-frontend-react:${DATAHUB_VERSION}
                ports: ["9002:9002"]
                volumes: ["${HOME}/.datahub/plugins:/etc/datahub/plugins"]
              datahub-actions-quickstart:
                image: acryldata/datahub-actions:${DATAHUB_VERSION}-slim
                volumes: ["${HOME}/.aws:/home/datahub/.aws"]
              system-update-quickstart:
                image: acryldata/datahub-upgrade:${DATAHUB_VERSION}
                restart: unless-stopped
                volumes: ["${HOME}/.datahub/plugins:/etc/datahub/plugins"]
              mysql:
                image: mysql:8.2
                ports: ["3306:3306"]
              kafka-broker:
                image: confluentinc/cp-kafka:8.0.0
                ports: ["9092:9092"]
              opensearch:
                image: opensearchproject/opensearch:2.19.3
                ports: ["9200:9200"]
            """
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "DATAHUB_SYSTEM_CLIENT_ID": "dummy-system-client",
        "DATAHUB_SYSTEM_CLIENT_SECRET": "dummy-system-secret",
        "DATAHUB_FRONTEND_SECRET": "dummy-unique-frontend-secret",
        "DATAHUB_TOKEN_SERVICE_SIGNING_KEY": "dummy-signing-key",
        "DATAHUB_TOKEN_SERVICE_SALT": "dummy-signing-salt",
    }
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "deploy/datahub-version.env"),
            "-f",
            str(base),
            "-f",
            str(ROOT / "deploy/datahub-secure-override.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(completed.stdout)["services"]

    assert services["datahub-gms-quickstart"]["image"].endswith(":v1.5.0.6")
    assert services["frontend-quickstart"]["image"].endswith(":v1.5.0.6")
    assert services["datahub-actions-quickstart"]["image"].endswith(":v1.5.0.6-slim")
    for name in ("datahub-gms-quickstart", "frontend-quickstart"):
        assert services[name]["environment"]["METADATA_SERVICE_AUTH_ENABLED"] == "true"
        assert services[name]["ports"][0]["host_ip"] == "127.0.0.1"
        assert not services[name].get("volumes")
    frontend = services["frontend-quickstart"]["environment"]
    assert frontend["AUTH_JAAS_ENABLED"] == "false"
    assert frontend["AUTH_NATIVE_ENABLED"] == "true"
    assert frontend["DATAHUB_SECRET"] == "dummy-unique-frontend-secret"
    for name in (
        "datahub-gms-quickstart",
        "frontend-quickstart",
        "datahub-actions-quickstart",
    ):
        env = services[name]["environment"]
        assert env["DATAHUB_SYSTEM_CLIENT_ID"] == "dummy-system-client"
        assert env["DATAHUB_SYSTEM_CLIENT_SECRET"] == "dummy-system-secret"
    assert not services["datahub-actions-quickstart"].get("volumes")
    system_update = services["system-update-quickstart"]["environment"]
    assert system_update["DATAHUB_TOKEN_SERVICE_SIGNING_KEY"] == "dummy-signing-key"
    assert system_update["DATAHUB_TOKEN_SERVICE_SALT"] == "dummy-signing-salt"
    assert "DATAHUB_SYSTEM_CLIENT_ID" not in system_update
    assert services["system-update-quickstart"]["restart"] == "no"
    assert not services["system-update-quickstart"].get("volumes")
    for name in ("mysql", "kafka-broker", "opensearch"):
        assert not services[name].get("ports")


def test_datahub_bootstrap_is_unpublished_and_uses_a_private_one_shot(
    tmp_path,
) -> None:
    base = tmp_path / "base.yml"
    base.write_text(
        textwrap.dedent(
            """
            services:
              datahub-gms-quickstart:
                image: acryldata/datahub-gms:${DATAHUB_VERSION}
                ports: ["8080:8080"]
                volumes: ["${HOME}/.datahub/plugins:/etc/datahub/plugins"]
                healthcheck:
                  test: ["CMD", "true"]
              frontend-quickstart:
                image: acryldata/datahub-frontend-react:${DATAHUB_VERSION}
                ports: ["9002:9002"]
                volumes: ["${HOME}/.datahub/plugins:/etc/datahub/plugins"]
              datahub-actions-quickstart:
                image: acryldata/datahub-actions:${DATAHUB_VERSION}-slim
                volumes: ["${HOME}/.aws:/home/datahub/.aws"]
              system-update-quickstart:
                image: acryldata/datahub-upgrade:${DATAHUB_VERSION}
                restart: unless-stopped
              mysql:
                image: mysql:8.2
                ports: ["3306:3306"]
              kafka-broker:
                image: confluentinc/cp-kafka:8.0.0
                ports: ["9092:9092"]
              opensearch:
                image: opensearchproject/opensearch:2.19.3
                ports: ["9200:9200"]
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "deploy/datahub-version.env"),
            "-f",
            str(base),
            "-f",
            str(ROOT / "deploy/datahub-bootstrap-auth.yml"),
            "--profile",
            "quickstart",
            "--profile",
            "bootstrap",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(completed.stdout)["services"]

    for name in (
        "datahub-gms-quickstart",
        "frontend-quickstart",
        "mysql",
        "kafka-broker",
        "opensearch",
    ):
        assert not services[name].get("ports")
    assert not services["datahub-gms-quickstart"].get("volumes")
    assert not services["frontend-quickstart"].get("volumes")
    one_shot = services["datahub-auth-bootstrap"]
    assert one_shot["restart"] == "no"
    assert one_shot["read_only"] is True
    assert one_shot["network_mode"] == "service:datahub-gms-quickstart"
    assert one_shot["environment"]["DATAHUB_GMS_URL"] == "http://127.0.0.1:8080"


def test_datahub_bootstrap_documents_private_cutover_and_safe_recovery() -> None:
    bootstrap = (ROOT / "deploy/datahub-bootstrap-auth.yml").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert 'METADATA_SERVICE_AUTH_ENABLED: "false"' in bootstrap
    assert 'AUTH_JAAS_ENABLED: "true"' in bootstrap
    assert 'AUTH_NATIVE_ENABLED: "true"' in bootstrap
    assert "volumes: !override []" in bootstrap
    assert 'system-update-quickstart:\n    restart: "no"' in bootstrap
    assert "ports: !override []" in bootstrap
    assert "datahub-auth-bootstrap" in bootstrap
    assert "network_mode: service:datahub-gms-quickstart" in bootstrap
    assert security.index("datahub-bootstrap-auth.yml") < security.index("user add")
    assert security.index("user add") < security.index(
        "Stop the private bootstrap stack"
    )
    assert security.index("Stop the private bootstrap stack") < security.index(
        "deploy/datahub-secure-override.yml"
    )
    assert "GENERATE_PERSONAL_ACCESS_TOKENS" in security
    assert "Manage All Access Tokens" in security
    assert "--email-as-id" in security
    assert "--id sidq-admin" not in security
    assert "--id sidq-reader" not in security
    assert "urn:li:corpuser:$admin_email" in security
    assert "urn:li:corpuser:$reader_email" in security
    assert "no bootstrap service has a host port" in security
    assert "Never publish the bootstrap override" in security
    assert "keep the secure deployment stopped" in " ".join(security.split())
    assert "AUTH_JAAS_ENABLED=false" in security
    assert security.index("GENERATE_PERSONAL_ACCESS_TOKENS") < security.index(
        "Admin generates the PAT"
    )
    assert "preserve `DATAHUB_TOKEN_SERVICE_SIGNING_KEY`" in security
    assert "revoke and reissue (rotate) every previously issued token" in security


def test_postgres_bootstrap_superuser_is_isolated_not_demoted() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "OID 10 bootstrap superuser" in security
    assert "ALTER ROLE sidq WITH NOSUPERUSER" not in security
    assert "\\password sidq" in security
    for required in (
        "default_transaction_read_only",
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "CONNECTION LIMIT",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "REVOKE TEMPORARY ON DATABASE warehouse FROM PUBLIC",
    ):
        assert required in security


def test_postgres_verification_keeps_passwords_out_of_psql_argv() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert 'psql "$(sudo cat /etc/sidq/claims.dsn)"' not in security
    assert "PGSERVICEFILE=/etc/sidq/pg_service.conf" in security
    assert "PGPASSFILE=/etc/sidq/pgpass" in security
    assert 'psql "service=sidq_reader"' in security
    assert "root:root 600" in security
    assert "sudo cat /etc/sidq/pgpass" not in security


def test_reader_write_denial_is_verified_by_graphql_and_unchanged_read() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    for required in (
        "updateDescription",
        "DescriptionUpdateInput",
        "authorization_probe",
        ".errors",
        "editableProperties.description",
        'test "$before_description" = "$after_description"',
    ):
        assert required in security
    assert "Never test write denial against production metadata" in security


def test_isolated_mcp_runtime_uses_its_own_hash_locked_requirements() -> None:
    source = (ROOT / "requirements-mcp.in").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-mcp.lock").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "mcp-server-datahub==0.6.0" in source
    assert "acryl-datahub==1.6.0.16" in source
    assert "mcp-server-datahub==0.6.0" in lock
    assert "--hash=sha256:" in lock
    assert "uv pip compile" in lock
    assert "--require-hashes -r /opt/sidq/current/requirements-mcp.lock" in security
    assert "uv pip compile" in security and "requirements-mcp.in" in security
    assert "pip install mcp-server-datahub==0.6.0" not in security


def test_landing_runtime_lock_includes_only_reader_and_live_extras() -> None:
    lock = (ROOT / "requirements-landing.lock").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "sentence-transformers==" in lock
    assert "psycopg==" in lock
    for development_package in ("hypothesis==", "mypy==", "pytest==", "ruff=="):
        assert development_package not in lock
    assert "--hash=sha256:" in lock
    command = (
        "uv export --locked --extra reader --extra live --no-emit-project "
        "--output-file requirements-landing.lock"
    )
    assert command in lock and command in makefile
    assert "--require-hashes -r /opt/sidq/current/requirements-landing.lock" in security
    assert "import sidq, sentence_transformers" in security


def test_compose_commands_preserve_home_and_use_one_secret_path() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "/etc/sidq/datahub-secrets.env" in security
    assert "/etc/datahub" not in security
    assert "sudo docker compose" not in security
    assert security.count("sudo env HOME=/home/dev docker compose") >= 3


def test_datahub_auth_verification_uses_valid_queries_and_json_login() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "{ me { corpUser { urn } } }" in security
    assert "{ me { urn } }" not in security
    assert "http://127.0.0.1:9002/logIn" in security
    assert 'split("\\u0000") | {username:.[0],password:.[1]}' in security
    assert "read -rs" in security
    assert "admin_login_status" in security
    assert "reader_login_status" in security
    assert 'test "$default_login_status" = 400' in security
    assert "unset admin_password reader_password" in security


def test_opensearch_disk_preflight_precedes_safe_unlock() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    for required in (
        "DockerRootDir",
        "disk_used_percent",
        "read_only_allow_delete",
        '"index.blocks.read_only_allow_delete": null',
        "free space first",
        "flood-stage",
    ):
        assert required in security
    assert security.index("free space first") < security.index(
        '"index.blocks.read_only_allow_delete": null'
    )
    assert "docker system prune" not in security
    assert "rm -rf" not in security
