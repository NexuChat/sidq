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

Concurrency is bounded globally and per command, with a timeout, because this is
a demonstration endpoint on a small host and the honest failure is "busy", not a
queue that grows until the machine falls over.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import posixpath
import re
import secrets
import shlex
import signal
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
PUBLIC_ASSET_PATHS = frozenset(
    {
        "/",
        "/app.js",
        "/architecture.svg",
        "/index.html",
        # DataHub's own Quality tab, so a judge can see the native assertion
        # without a login. Copied from examples/06-native-assertion/, which
        # keeps the reproducible GraphQL half beside it.
        "/quality-tab.png",
        "/scope.html",
        # Referenced by og:image and twitter:image as an ABSOLUTE url, so it does
        # not appear in a relative href/src scan. Omitting it breaks the link
        # preview wherever the demo url is shared. social-preview.svg is the
        # generation source and is deliberately NOT public.
        "/social-preview.png",
        "/styles.css",
    }
)
VENV_ROOT = Path(os.environ.get("SIDQ_VENV_DIR", str(REPO / ".venv")))
VENV = VENV_ROOT / "bin"
RUNTIME = Path(os.environ.get("SIDQ_RUNTIME_DIR", str(REPO)))
_RUNTIME_LOCK_ARGUMENT = f"--old-file={VENV_ROOT / '.sidq-dev-lock'}"
TIMEOUT_SECONDS = 240
COOLDOWN_SECONDS = 30
CLIENT_WINDOW_SECONDS = 10 * 60
GLOBAL_START_WINDOW_SECONDS = 10 * 60
GLOBAL_START_LIMIT = 20
# Measured on this host rather than guessed: four concurrent `sidq audit`
# runs completed together in 26s — no slower than one alone, because the work
# is DataHub round trips rather than CPU — with 9 GB of 30 GB used and a load
# average of 5.3 across 16 cores. Two was leaving judges queued behind a
# 40-second run for no reason. Raise this only with a fresh measurement; the
# per-command locks below still serialise identical commands, which is what
# keeps one client from monopolising a slot.
GLOBAL_CONCURRENCY = 4
BUSY_RETRY_SECONDS = 5
READINESS_TIMEOUT_SECONDS = 2
READINESS_CACHE_TTL_SECONDS = 10
MAX_READINESS_RESPONSE_BYTES = 32 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
TRUNCATION_MARKER = "\n...[output truncated]"
DEMO_REQUEST_HEADER = "X-Sidq-Demo"
DEMO_REQUEST_HEADER_VALUE = "run"
CAPABILITY_HEADER = "X-Sidq-Capability"
CAPABILITY_REQUEST_VALUE = "capability"
CAPABILITY_TTL_SECONDS = 60
MAX_CONSUMED_CAPABILITIES = GLOBAL_START_LIMIT
MAX_CAPABILITY_TOKEN_LENGTH = 256
MAX_CAPABILITY_EXPIRY_DIGITS = 20
MAX_CAPABILITY_NONCE_LENGTH = 64
HANDOFF_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"
DEFAULT_ALLOWED_ORIGINS = (
    "https://sidq.mlki.app",
    "http://127.0.0.1:8766",
    "http://localhost:8766",
)
COMMAND_ENV_ALLOWLIST = frozenset(
    {
        "CLAIMS_SOURCE",
        "DATAHUB_GMS_TOKEN",
        "DATAHUB_GMS_URL",
        "DATAHUB_TELEMETRY_ENABLED",
        "HOME",
        "HF_HOME",
        "HF_HUB_OFFLINE",
        "LANG",
        "LC_ALL",
        "LOGURU_LEVEL",
        "PATH",
        "PYTHONUNBUFFERED",
        "PYTHONPATH",
        "SENTENCE_TRANSFORMERS_HOME",
        "TOKENIZERS_PARALLELISM",
        "TRANSFORMERS_OFFLINE",
        "VENV",
    }
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; connect-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; form-action 'none'; img-src 'self' data:; "
        "object-src 'none'; script-src 'self'; style-src 'self'"
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
        ("make", _RUNTIME_LOCK_ARGUMENT, "gate-demo"),
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
            "then verifies the semantic entity, complete one-hop lineage, policy, "
            "and age. Read-only."
        ),
        (str(VENV / "sidq"), "verify", HANDOFF_URN, "--max-age-days", "45"),
    ),
    "claims": (
        (
            "Measure documented field claims against the live source with bounded "
            "read-only SQL; query results remain on this host."
        ),
        ("make", _RUNTIME_LOCK_ARGUMENT, "claims-demo"),
    ),
}

EXPECTED_SECONDS = {
    "gate-demo": 15,
    "audit": 45,
    "repair": 90,
    "handoff": 30,
    "claims": 90,
}
CLIENT_RUN_LIMIT = len(RUNNABLE)

_state_lock = threading.Lock()
_global_slots = threading.BoundedSemaphore(GLOBAL_CONCURRENCY)
_command_locks = {name: threading.Lock() for name in RUNNABLE}
_last_run_finished: dict[tuple[str | None, str], float] = {}
_client_run_started: dict[str, deque[float]] = {}
_global_run_started: deque[float] = deque()
_capability_signing_key = secrets.token_bytes(32)
_consumed_capabilities: dict[str, float] = {}
_readiness_lock = threading.Lock()
_readiness_cache: tuple[float, bool] | None = None

_INTERNAL_URL_RE = re.compile(
    r"https?://(?:localhost|127(?:\.\d{1,3}){3}|\[::1\]|"
    r"[^\s/:]*datahub-gms[^\s/:]*)(?::\d+)?(?:/[^\s]*)?",
    re.IGNORECASE,
)
_RELEASE_SHA_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_RELEASES_ROOT = Path("/opt/sidq/releases")


def _normalised_static_path(request_target: str) -> str | None:
    """Return a safe, normalised origin-form path, or reject the request."""
    parsed = urllib.parse.urlsplit(request_target)
    if parsed.scheme or parsed.netloc:
        return None
    try:
        path = urllib.parse.unquote(parsed.path, errors="surrogatepass")
    except UnicodeDecodeError:
        path = urllib.parse.unquote(parsed.path)
    if not path.startswith("/"):
        return None
    if any(part in (".", "..") for part in path.split("/")):
        return None
    return posixpath.normpath(path)


def _truncate_output(output: str) -> str:
    output = output.strip()
    encoded = output.encode("utf-8")
    marker = TRUNCATION_MARKER.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return output
    prefix = encoded[: MAX_OUTPUT_BYTES - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _redact_credentials(value: str, credentials: set[str]) -> str:
    """Redact every credential span, including partially overlapping values."""
    spans: list[tuple[int, int]] = []
    for credential in credentials:
        start = 0
        while (match := value.find(credential, start)) != -1:
            spans.append((match, match + len(credential)))
            start = match + 1
    if not spans:
        return value

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.extend((value[cursor:start], "[redacted]"))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


def _sanitize_public_text(value: str) -> str:
    """Remove host topology from the response without changing command behavior."""
    sanitized = value
    credentials: set[str] = set()
    for path_variable in ("SIDQ_DATAHUB_TOKEN_FILE", "SIDQ_CLAIMS_DSN_FILE"):
        path = os.environ.get(path_variable)
        if not path:
            continue
        try:
            credential = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if credential:
            credentials.add(credential)
    sanitized = _redact_credentials(sanitized, credentials)
    runtime_paths = {path for path in (VENV_ROOT, RUNTIME) if path != REPO}
    for path in sorted(runtime_paths, key=lambda item: len(str(item)), reverse=True):
        sanitized = sanitized.replace(str(path), "[runtime]")
    sanitized = sanitized.replace(str(REPO), "<repository>")
    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080").rstrip("/")
    if gms_url:
        sanitized = sanitized.replace(gms_url, "[internal DataHub endpoint]")
    return _INTERNAL_URL_RE.sub("[internal service]", sanitized)


def _public_command(argv: tuple[str, ...]) -> str:
    if argv and argv[0] == str(VENV / "sidq"):
        public_argv = ("sidq", *argv[1:])
    elif argv and argv[0] == "make":
        public_argv = tuple(arg for arg in argv if arg != _RUNTIME_LOCK_ARGUMENT)
    else:
        public_argv = argv
    return shlex.join(public_argv)


def _cooldown_remaining(client_identity: str | None, name: str) -> float:
    with _state_lock:
        now = time.monotonic()
        expired = [
            key
            for key, finished in _last_run_finished.items()
            if now - finished >= COOLDOWN_SECONDS
        ]
        for key in expired:
            del _last_run_finished[key]
        finished = _last_run_finished.get((client_identity, name))
        return 0.0 if finished is None else COOLDOWN_SECONDS - (now - finished)


def _reserve_client_run(client_identity: str | None, now: float | None = None) -> float:
    """Reserve one of five runs per window, enough to exercise every demo once."""
    if client_identity is None:
        return 0.0
    current = time.monotonic() if now is None else now
    with _state_lock:
        for identity, history in list(_client_run_started.items()):
            while history and current - history[0] >= CLIENT_WINDOW_SECONDS:
                history.popleft()
            if not history:
                del _client_run_started[identity]
        starts = _client_run_started.setdefault(client_identity, deque())
        if len(starts) >= CLIENT_RUN_LIMIT:
            return CLIENT_WINDOW_SECONDS - (current - starts[0])
        starts.append(current)
        return 0.0


def _reserve_run_start(
    client_identity: str | None, now: float | None = None
) -> tuple[str | None, float]:
    """Reserve client and host rolling-window capacity atomically."""
    current = time.monotonic() if now is None else now
    with _state_lock:
        for identity, history in list(_client_run_started.items()):
            while history and current - history[0] >= CLIENT_WINDOW_SECONDS:
                history.popleft()
            if not history:
                del _client_run_started[identity]
        while (
            _global_run_started
            and current - _global_run_started[0] >= GLOBAL_START_WINDOW_SECONDS
        ):
            _global_run_started.popleft()

        starts = (
            None
            if client_identity is None
            else _client_run_started.setdefault(client_identity, deque())
        )
        if starts is not None and len(starts) >= CLIENT_RUN_LIMIT:
            return "client", CLIENT_WINDOW_SECONDS - (current - starts[0])
        if len(_global_run_started) >= GLOBAL_START_LIMIT:
            return (
                "global",
                GLOBAL_START_WINDOW_SECONDS - (current - _global_run_started[0]),
            )
        if starts is not None:
            starts.append(current)
        _global_run_started.append(current)
        return None, 0.0


def _allowed_origins() -> tuple[str, ...]:
    configured = os.environ.get("SIDQ_ALLOWED_ORIGINS")
    candidates = configured.split(",") if configured else DEFAULT_ALLOWED_ORIGINS
    allowed: list[str] = []
    for candidate in candidates:
        origin = candidate.strip()
        parsed = urllib.parse.urlsplit(origin)
        if (
            origin
            and parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            allowed.append(origin)
    return tuple(allowed)


def _trusted_proxy(peer_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    configured = os.environ.get("SIDQ_TRUSTED_PROXIES", "")
    for raw_network in configured.split(","):
        raw_network = raw_network.strip()
        if not raw_network:
            continue
        try:
            if peer_ip in ipaddress.ip_network(raw_network, strict=False):
                return True
        except ValueError:
            continue
    return False


def _client_identity(peer: str, headers: object) -> str | None:
    """Return an attributable client only through an explicitly trusted proxy."""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if not _trusted_proxy(peer_ip):
        return None
    forwarded = getattr(headers, "get", lambda *_: None)("CF-Connecting-IP")
    if forwarded:
        try:
            forwarded_ip = ipaddress.ip_address(forwarded.strip())
            if isinstance(forwarded_ip, ipaddress.IPv4Address):
                return str(forwarded_ip)
            if forwarded_ip.ipv4_mapped is not None:
                return str(forwarded_ip.ipv4_mapped)
            return str(ipaddress.IPv6Network((forwarded_ip, 64), strict=False))
        except ValueError:
            pass
    return None


def _https_redirect_target(peer: str, headers: object, target: str) -> str | None:
    """Return a canonical HTTPS target only for explicit, trusted proxy metadata."""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if not _trusted_proxy(peer_ip):
        return None

    get_header = getattr(headers, "get", lambda *_: None)
    forwarded_proto = get_header("X-Forwarded-Proto")
    host = get_header("Host")
    if not isinstance(forwarded_proto, str) or not isinstance(host, str):
        return None
    if forwarded_proto.strip().casefold() != "http":
        return None

    allowed_origin = next(
        (
            urllib.parse.urlsplit(origin)
            for origin in _allowed_origins()
            if urllib.parse.urlsplit(origin).scheme == "https"
            and urllib.parse.urlsplit(origin).netloc.casefold()
            == host.strip().casefold()
        ),
        None,
    )
    if allowed_origin is None:
        return None

    requested = urllib.parse.urlsplit(target)
    path = requested.path or "/"
    if not path.startswith("/"):
        return None
    return urllib.parse.urlunsplit(
        ("https", allowed_origin.netloc, path, requested.query, "")
    )


def _request_is_same_origin(headers: object) -> bool:
    get_header = getattr(headers, "get", lambda *_: None)
    if get_header(DEMO_REQUEST_HEADER) != DEMO_REQUEST_HEADER_VALUE:
        return False
    if get_header("Sec-Fetch-Site") != "same-origin":
        return False
    origin = get_header("Origin")
    host = get_header("Host")
    if not origin or not host:
        return False
    allowed = _allowed_origins()
    if origin not in allowed:
        return False
    parsed = urllib.parse.urlsplit(origin)
    return parsed.netloc.casefold() == host.casefold()


def _issue_capability(
    client_identity: str | None, command: str, now: float | None = None
) -> tuple[str, float]:
    """Issue a stateless client-and-command-bound anti-replay token."""
    current = time.monotonic() if now is None else now
    expires_at = current + CAPABILITY_TTL_SECONDS
    encoded_expiry = str(math.ceil(expires_at * 1_000_000))
    nonce = secrets.token_urlsafe(18)
    subject = client_identity or ""
    unsigned = f"v1\0{encoded_expiry}\0{nonce}\0{subject}\0{command}".encode()
    signature = hmac.digest(_capability_signing_key, unsigned, "sha256").hex()
    return f"v1.{encoded_expiry}.{nonce}.{signature}", 0.0


def _validate_capability(
    headers: object,
    client_identity: str | None,
    command: str,
    now: float | None = None,
) -> tuple[str, float] | None:
    current = time.monotonic() if now is None else now
    token = getattr(headers, "get", lambda *_: None)(CAPABILITY_HEADER)
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_CAPABILITY_TOKEN_LENGTH
    ):
        return None
    try:
        version, encoded_expiry, nonce, supplied_signature = token.split(".")
    except ValueError:
        return None
    if (
        version != "v1"
        or not 1 <= len(encoded_expiry) <= MAX_CAPABILITY_EXPIRY_DIGITS
        or not encoded_expiry.isascii()
        or not encoded_expiry.isdigit()
        or not 1 <= len(nonce) <= MAX_CAPABILITY_NONCE_LENGTH
        or re.fullmatch(r"[A-Za-z0-9_-]+", nonce) is None
        or re.fullmatch(r"[0-9a-f]{64}", supplied_signature) is None
    ):
        return None
    expires_at = int(encoded_expiry) / 1_000_000
    if version != "v1" or not math.isfinite(expires_at) or expires_at <= current:
        return None
    subject = client_identity or ""
    unsigned = f"v1\0{encoded_expiry}\0{nonce}\0{subject}\0{command}".encode()
    expected_signature = hmac.digest(_capability_signing_key, unsigned, "sha256").hex()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    return hashlib.sha256(token.encode()).hexdigest(), expires_at


def _accept_run_start(
    client_identity: str | None,
    capability: tuple[str, float],
    now: float | None = None,
) -> tuple[str | None, float]:
    """Atomically reserve start capacity and record one accepted capability."""
    current = time.monotonic() if now is None else now
    capability_id, expires_at = capability
    with _state_lock:
        for identity, history in list(_client_run_started.items()):
            while history and current - history[0] >= CLIENT_WINDOW_SECONDS:
                history.popleft()
            if not history:
                del _client_run_started[identity]
        while (
            _global_run_started
            and current - _global_run_started[0] >= GLOBAL_START_WINDOW_SECONDS
        ):
            _global_run_started.popleft()
        for consumed, consumed_expiry in list(_consumed_capabilities.items()):
            if consumed_expiry <= current:
                del _consumed_capabilities[consumed]

        if capability_id in _consumed_capabilities:
            return "replay", 0.0
        starts = (
            None
            if client_identity is None
            else _client_run_started.setdefault(client_identity, deque())
        )
        if starts is not None and len(starts) >= CLIENT_RUN_LIMIT:
            return "client", CLIENT_WINDOW_SECONDS - (current - starts[0])
        if len(_global_run_started) >= GLOBAL_START_LIMIT:
            return (
                "global",
                GLOBAL_START_WINDOW_SECONDS - (current - _global_run_started[0]),
            )
        if len(_consumed_capabilities) >= MAX_CONSUMED_CAPABILITIES:
            earliest_expiry = min(_consumed_capabilities.values())
            return "replay_capacity", max(earliest_expiry - current, 1.0)
        if starts is not None:
            starts.append(current)
        _global_run_started.append(current)
        _consumed_capabilities[capability_id] = expires_at
        return None, 0.0


def _capability_request_is_same_origin(headers: object) -> bool:
    get_header = getattr(headers, "get", lambda *_: None)
    if get_header(DEMO_REQUEST_HEADER) != CAPABILITY_REQUEST_VALUE:
        return False
    if get_header("Sec-Fetch-Site") != "same-origin":
        return False
    host = get_header("Host")
    if not host:
        return False
    return any(
        urllib.parse.urlsplit(origin).netloc.casefold() == host.casefold()
        for origin in _allowed_origins()
    )


def _reset_request_state_for_tests() -> None:
    """Reset bounded in-memory request state; never called by the service."""
    global _global_slots, _command_locks, _readiness_cache
    with _state_lock:
        _last_run_finished.clear()
        _client_run_started.clear()
        _global_run_started.clear()
        _consumed_capabilities.clear()
    with _readiness_lock:
        _readiness_cache = None
    _global_slots = threading.BoundedSemaphore(GLOBAL_CONCURRENCY)
    _command_locks = {name: threading.Lock() for name in RUNNABLE}


def _release_sha() -> str | None:
    """Return an attributable release SHA without invoking Git or a shell."""
    explicit = os.environ.get("SIDQ_RELEASE_SHA")
    if explicit is not None and _RELEASE_SHA_RE.fullmatch(explicit):
        return explicit.casefold()
    try:
        release = REPO.resolve()
    except OSError:
        return None
    if release.parent == _RELEASES_ROOT and _RELEASE_SHA_RE.fullmatch(release.name):
        return release.name.casefold()
    return None


def _health_payload() -> dict[str, object]:
    """Liveness has no dependencies: if this returns, the landing process lives."""
    release_sha = _release_sha()
    return {
        "status": "ok",
        "service": "sidq-landing",
        "live_demos": sorted(RUNNABLE),
        "release": {
            "state": "deployed" if release_sha is not None else "local/dev",
            "commit_sha": release_sha,
        },
    }


def _datahub_ready(token: str) -> bool:
    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080").rstrip("/")
    body = json.dumps(
        {
            "query": (
                "query SidqLandingReadiness { "
                'search(input: {type: DATASET, query: "*", start: 0, count: 1}) '
                "{ total } }"
            )
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{gms_url}/api/graphql",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=READINESS_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read(MAX_READINESS_RESPONSE_BYTES))
            return (
                response.status == 200
                and isinstance(payload, dict)
                and not payload.get("errors")
                and isinstance(payload.get("data"), dict)
                and isinstance(payload["data"].get("search"), dict)
            )
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return False


def _readiness_payload(
    probe: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Readiness names the live dependency instead of returning a vague 503."""
    if probe is not None:
        ready = probe()
    else:
        ready = _cached_datahub_readiness()
    return {
        "status": "ready" if ready else "degraded",
        "service": "sidq-landing",
        "datahub": "ok" if ready else "unavailable",
    }


def _cached_datahub_readiness() -> bool:
    """Bound public readiness probes without retaining credentials or errors."""
    global _readiness_cache
    cached = _readiness_cache
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    if not _readiness_lock.acquire(blocking=False):
        cached = _readiness_cache
        return cached[1] if cached is not None else False
    try:
        now = time.monotonic()
        if _readiness_cache is not None and now < _readiness_cache[0]:
            return _readiness_cache[1]
        try:
            credential = _read_credential("SIDQ_DATAHUB_TOKEN_FILE")
            ready = _datahub_ready(credential)
        except Exception:  # noqa: BLE001 -- public readiness must fail closed
            ready = False
        _readiness_cache = (time.monotonic() + READINESS_CACHE_TTL_SECONDS, ready)
        return ready
    finally:
        _readiness_lock.release()


def _read_credential(path_variable: str) -> str:
    path = os.environ.get(path_variable)
    if not path:
        raise RuntimeError(f"required credential path {path_variable} is not set")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"credential file for {path_variable} is empty")
    return value


def _command_environment(name: str) -> dict[str, str]:
    """Build a closed environment and scope credentials to commands that need them."""
    environment = {
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LOGURU_LEVEL": "WARNING",
        "PATH": os.environ.get(
            "SIDQ_COMMAND_PATH",
            f"{VENV}:{RUNTIME / 'mcp' / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        ),
        "PYTHONUNBUFFERED": "1",
        # Dependencies come from the hash-locked runtime, while application code
        # must always come from the exact immutable release served by this process.
        "PYTHONPATH": str(REPO / "src"),
        "VENV": str(VENV_ROOT),
    }
    if name in {"audit", "repair", "handoff", "claims"}:
        environment["DATAHUB_GMS_URL"] = os.environ.get(
            "DATAHUB_GMS_URL", "http://127.0.0.1:8080"
        )
        environment["DATAHUB_GMS_TOKEN"] = _read_credential("SIDQ_DATAHUB_TOKEN_FILE")
    if name == "claims":
        environment["CLAIMS_SOURCE"] = _read_credential("SIDQ_CLAIMS_DSN_FILE")
        model_cache = str(RUNTIME / "huggingface")
        environment.update(
            {
                "HF_HOME": model_cache,
                "HF_HUB_OFFLINE": "1",
                "SENTENCE_TRANSFORMERS_HOME": str(Path(model_cache) / "hub"),
                "TOKENIZERS_PARALLELISM": "false",
                "TRANSFORMERS_OFFLINE": "1",
            }
        )
    return environment


def _run(name: str) -> dict[str, object]:
    description, argv = RUNNABLE[name]
    started = time.monotonic()
    try:
        environment = _command_environment(name)
        process = subprocess.Popen(
            argv,
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, RuntimeError, UnicodeError):
        return {
            "command": _public_command(argv),
            "description": description,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "expected_seconds": EXPECTED_SECONDS[name],
            "exit_code": None,
            "output": "command unavailable",
        }
    try:
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
            "command": _public_command(argv),
            "description": description,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "expected_seconds": EXPECTED_SECONDS[name],
            "exit_code": None,
            "output": f"timed out after {TIMEOUT_SECONDS}s",
        }
    # Exit 1 is a finding, not a failure, and the caller is told the code rather
    # than being shown a red banner for a working audit that found something.
    return {
        "command": _public_command(argv),
        "description": description,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "expected_seconds": EXPECTED_SECONDS[name],
        "exit_code": process.returncode,
        "output": _truncate_output(_sanitize_public_text(stdout or stderr or "")),
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)  # type: ignore[arg-type]

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def _redirect_forwarded_http(self) -> bool:
        target = _https_redirect_target(self.client_address[0], self.headers, self.path)
        if target is None:
            return False
        self.send_response(308)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def do_GET(self) -> None:
        if self._redirect_forwarded_http():
            return
        parsed_path = urllib.parse.urlsplit(self.path)
        if parsed_path.path == "/healthz":
            self._json(200, _health_payload())
            return
        if parsed_path.path == "/readyz":
            payload = _readiness_payload()
            self._json(200 if payload["status"] == "ready" else 503, payload)
            return
        if parsed_path.path == "/capability":
            if not _capability_request_is_same_origin(self.headers):
                self._json(403, {"error": "cross-site capability request rejected"})
                return
            requested = urllib.parse.parse_qs(parsed_path.query).get("command", [])
            if len(requested) != 1 or requested[0] not in RUNNABLE:
                self._json(404, {"error": "no such command"})
                return
            client_identity = _client_identity(self.client_address[0], self.headers)
            self._json(
                200,
                {
                    "capability": _issue_capability(client_identity, requested[0])[0],
                    "expires_in": CAPABILITY_TTL_SECONDS,
                    "authentication": False,
                },
            )
            return
        super().do_GET()

    def send_head(self):
        """Serve only landing-page assets, retaining stdlib path containment."""
        path = _normalised_static_path(self.path)
        if path not in PUBLIC_ASSET_PATHS:
            self.send_error(404, "File not found")
            return None
        return super().send_head()

    def list_directory(self, path: str):
        del path
        self.send_error(404, "Directory listing disabled")

    def do_HEAD(self) -> None:
        if self._redirect_forwarded_http():
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if self._redirect_forwarded_http():
            return
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
        if not _request_is_same_origin(self.headers):
            self._json(403, {"error": "cross-site demo request rejected"})
            return
        client_identity = _client_identity(self.client_address[0], self.headers)
        capability = _validate_capability(self.headers, client_identity, name)
        if capability is None:
            self._json(403, {"error": "demo capability missing, expired, or replayed"})
            return
        command_lock = _command_locks[name]
        if not command_lock.acquire(blocking=False):
            self._json(
                429,
                {
                    "error": "this demo is already running — try again",
                    "retry_after": BUSY_RETRY_SECONDS,
                },
            )
            return
        if not _global_slots.acquire(blocking=False):
            command_lock.release()
            self._json(
                429,
                {
                    "error": "demo capacity is busy — try again",
                    "retry_after": BUSY_RETRY_SECONDS,
                },
            )
            return
        try:
            remaining = _cooldown_remaining(client_identity, name)
            if remaining > 0:
                self._json(
                    429,
                    {
                        "error": "run cooldown active — try again later",
                        "retry_after": math.ceil(remaining),
                    },
                )
                return
            scope, remaining = _accept_run_start(client_identity, capability)
            if scope is not None:
                if scope == "replay":
                    self._json(
                        403,
                        {"error": "demo capability missing, expired, or replayed"},
                    )
                    return
                error = (
                    "client run limit active — try again later"
                    if scope == "client"
                    else (
                        "global safety start cap active — this is DoS containment, "
                        "not authentication"
                    )
                )
                self._json(
                    429,
                    {
                        "error": error,
                        "retry_after": math.ceil(remaining),
                    },
                )
                return
            payload = _run(name)
            with _state_lock:
                _last_run_finished[client_identity, name] = time.monotonic()
            self._json(200, payload)
        finally:
            _global_slots.release()
            command_lock.release()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 429 and "retry_after" in payload:
            self.send_header("Retry-After", str(payload["retry_after"]))
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
