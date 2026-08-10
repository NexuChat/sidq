"""Persist a completed receipt through the official DataHub MCP mutation tools."""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, InvalidStateError
from dataclasses import replace
from typing import Any

from .build import Receipt
from .read import _sidq_values, _single_entity, decision_context_hash

ToolCaller = Callable[[str, Mapping[str, Any]], Any]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]

_CONFIRMATION_TIMEOUT_SECONDS = 30.0
_CONFIRMATION_INITIAL_DELAY_SECONDS = 0.1
_CONFIRMATION_MAX_DELAY_SECONDS = 1.0
# Compensation has its own bound: long enough for ordinary MCP/DataHub latency,
# but finite so a failed confirmation cannot make the caller hang indefinitely.
_ROLLBACK_READ_TIMEOUT_SECONDS = 2.0

_BADGE_BY_VERDICT = {
    "PASS": "urn:li:tag:sidq:verified",
    "WARN": "urn:li:tag:sidq:verified",
    "BLOCK": "urn:li:tag:sidq:blocked",
}
_MANAGED_BADGES = frozenset(_BADGE_BY_VERDICT.values())
_SIDQ_PROPERTY_PREFIX = "urn:li:structuredProperty:sidq."
_URN_WRITE_LOCKS = tuple(threading.Lock() for _ in range(64))

# The two tools every receipt-shaped caller needs to read what it is about to
# decide on: the entity aspects themselves (`_single_entity` below, plus
# `read.py`) and the one-hop lineage that `decision_context_hash` folds into the
# context hash. Consuming a receipt needs nothing else.
RECEIPT_READ_TOOLS = frozenset({"get_entities", "get_lineage"})

# Exactly the mutations `write_receipt` and its rollback issue, and nothing
# more: the evidence document, the managed verdict badge, and Sidq's own
# structured properties. `add_terms` and `add_owners` are absent because no
# receipt path calls them — that absence is the point.
RECEIPT_TOOLS = RECEIPT_READ_TOOLS | frozenset(
    {
        "save_document",
        "add_tags",
        "remove_tags",
        "add_structured_properties",
        "remove_structured_properties",
    }
)


class ReceiptWriteUnconfirmed(RuntimeError):
    """The mutations returned, but the exact receipt was not readable in time."""


class ToolNotAllowed(PermissionError):
    """A tool name outside the allowlist this caller was constructed with."""


class StdioMCPReceiptToolCaller:
    """A synchronous caller for the official MCP server with mutations enabled.

    This is deliberately receipt-local: the graph reader owns a read-only
    session, while this boundary runs a child with mutations enabled. That
    privilege is bounded by ``allowed_tools``, a **required** constructor
    argument — the class cannot be built without one, because a permissive
    default is precisely how such a boundary silently reopens. Anything not on
    the list raises :class:`ToolNotAllowed` in the calling thread, before the
    MCP session is started or a request is queued, so a rejected tool name
    never reaches the subprocess.

    The allowlist constrains the *object*: which tools this session may invoke
    at all. It is orthogonal to, and does not replace, the data-level limits
    that keep the receipt payload inside Sidq's namespace (``_MANAGED_BADGES``
    here, ``property_urn`` in ``bootstrap.py``).

    Callers pass one of the sets derived from the paths they actually drive:
    ``RECEIPT_READ_TOOLS``, ``RECEIPT_TOOLS``, or ``sidq.repair.REPAIR_TOOLS``.
    """

    def __init__(
        self,
        allowed_tools: frozenset[str],
        command: str = "mcp-server-datahub",
        *,
        args: tuple[str, ...] = (),
        gms_url: str | None = None,
    ) -> None:
        # A ``frozenset`` specifically: a mutable set would let any holder of a
        # reference widen this caller's privilege after construction, which is
        # the same fail-open hole one indirection further out.
        if not isinstance(allowed_tools, frozenset):
            raise TypeError(
                "allowed_tools must be a frozenset of tool names, not "
                f"{type(allowed_tools).__name__}"
            )
        if not allowed_tools or not all(
            isinstance(name, str) and name for name in allowed_tools
        ):
            raise ValueError("allowed_tools must be a non-empty set of tool names")
        self._allowed_tools = allowed_tools
        self._command = command
        self._args = args
        self._gms_url = gms_url or os.environ.get(
            "DATAHUB_GMS_URL", "http://localhost:8080"
        )
        self._requests: queue.Queue[
            tuple[str, Mapping[str, Any], Future[Any], float | None] | None
        ] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: Future[None] = Future()

    @property
    def allowed_tools(self) -> frozenset[str]:
        """The tools this caller may invoke. Read-only by construction."""
        return self._allowed_tools

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return self._call(name, arguments, deadline=None)

    def call_with_timeout(
        self, name: str, arguments: Mapping[str, Any], *, timeout: float
    ) -> Any:
        """Call one tool without letting the calling thread wait forever."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        return self._call(name, arguments, deadline=time.monotonic() + timeout)

    def _call(
        self, name: str, arguments: Mapping[str, Any], *, deadline: float | None
    ) -> Any:
        # First statement on the only path to ``session.call_tool``: refuse
        # here, in the calling thread, before a transport exists to carry it.
        if name not in self._allowed_tools:
            raise ToolNotAllowed(
                f"tool {name!r} is not permitted by this caller; allowed tools: "
                + ", ".join(sorted(self._allowed_tools))
            )
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="sidq-receipt-mcp", daemon=True
            )
            self._thread.start()
        startup_timeout = _remaining_seconds(deadline)
        self._startup.result(timeout=startup_timeout)
        result: Future[Any] = Future()
        self._requests.put((name, dict(arguments), result, deadline))
        try:
            return result.result(timeout=_remaining_seconds(deadline))
        except TimeoutError:
            if not result.done():
                result.cancel()
            raise

    def _run(self) -> None:
        import anyio

        try:
            anyio.run(self._serve)
        except Exception as error:  # noqa: BLE001 - relay MCP startup failures to the synchronous caller
            if not self._startup.done():
                self._startup.set_exception(error)

    async def _serve(self) -> None:
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        with tempfile.TemporaryDirectory(prefix="sidq-receipt-mcp-") as home:
            environment = _mcp_subprocess_environment(self._gms_url, home=home)
            parameters = StdioServerParameters(
                command=self._command, args=list(self._args), env=environment
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                self._startup.set_result(None)
                while request := await anyio.to_thread.run_sync(self._requests.get):
                    name, arguments, result, deadline = request
                    if result.cancelled():
                        continue
                    timeout = _remaining_seconds(deadline)
                    if timeout is not None and timeout <= 0:
                        _set_future_exception_if_pending(
                            result, TimeoutError(f"MCP {name} request timed out")
                        )
                        continue
                    try:
                        # Never cancel an in-flight call: a synchronous MCP handler
                        # cannot observe cancellation, so mcp 1.29.0 responds twice
                        # and trips `assert not self._completed, "Request already responded to"`.
                        # The caller gives up via Future.result; this thread finishes
                        # the call, and the next queued request waits behind it.
                        response = await session.call_tool(name, dict(arguments))
                        is_error = getattr(response, "is_error", None)
                        if is_error is None:
                            is_error = getattr(response, "isError", False)
                        if is_error:
                            messages = _text_messages(response.content)
                            raise RuntimeError(
                                f"MCP {name} failed: {' '.join(messages)}"
                            )
                        structured = getattr(response, "structured_content", None)
                        if structured is None:
                            structured = getattr(response, "structuredContent", None)
                        if structured is not None:
                            _set_future_result_if_pending(result, structured)
                        else:
                            text = next(
                                (text for text in _text_messages(response.content)),
                                "{}",
                            )
                            _set_future_result_if_pending(result, json.loads(text))
                    except Exception as error:  # noqa: BLE001 - relay every MCP tool failure through its Future
                        _set_future_exception_if_pending(result, error)

    def close(self) -> None:
        if self._thread is not None:
            self._requests.put(None)
            self._thread.join(timeout=5)
        self._thread = None


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(deadline - time.monotonic(), 0.0)


def _set_future_result_if_pending(result: Future[Any], value: Any) -> None:
    if result.cancelled():
        return
    try:
        result.set_result(value)
    except InvalidStateError:
        if not result.cancelled():
            raise


def _set_future_exception_if_pending(result: Future[Any], error: BaseException) -> None:
    if result.cancelled():
        return
    try:
        result.set_exception(error)
    except InvalidStateError:
        if not result.cancelled():
            raise


def _mcp_subprocess_environment(gms_url: str, *, home: str) -> dict[str, str]:
    """Return only the environment needed by the receipt-writing MCP child."""
    environment = {
        "DATAHUB_GMS_URL": gms_url,
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "HOME": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LOGURU_LEVEL": "WARNING",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TOOLS_IS_MUTATION_ENABLED": "true",
    }
    if token := os.environ.get("DATAHUB_GMS_TOKEN"):
        environment["DATAHUB_GMS_TOKEN"] = token
    return environment


def _text_messages(contents: Sequence[object]) -> list[str]:
    """Return text payloads while safely ignoring non-text MCP content blocks."""
    return [
        text
        for item in contents
        if getattr(item, "type", "") == "text"
        if isinstance(text := getattr(item, "text", None), str)
    ]


def write_receipt(
    receipt: Receipt,
    tool_caller: ToolCaller,
    *,
    confirmation_timeout: float = _CONFIRMATION_TIMEOUT_SECONDS,
    confirmation_initial_delay: float = _CONFIRMATION_INITIAL_DELAY_SECONDS,
    confirmation_max_delay: float = _CONFIRMATION_MAX_DELAY_SECONDS,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any]:
    """Write evidence, queryable fields, and a visible badge through MCP.

    The document is saved first so its returned URN becomes the evidence link on
    a receipt that did not already have an externally supplied evidence URL.
    """

    with _write_lock_for(receipt.urn):
        return _write_receipt_under_lock(
            receipt,
            tool_caller,
            confirmation_timeout=confirmation_timeout,
            confirmation_initial_delay=confirmation_initial_delay,
            confirmation_max_delay=confirmation_max_delay,
            monotonic=monotonic,
            sleep=sleep,
        )


def _write_lock_for(urn: str) -> threading.Lock:
    return _URN_WRITE_LOCKS[hash(urn) % len(_URN_WRITE_LOCKS)]


def _write_receipt_under_lock(
    receipt: Receipt,
    tool_caller: ToolCaller,
    *,
    confirmation_timeout: float,
    confirmation_initial_delay: float,
    confirmation_max_delay: float,
    monotonic: Clock,
    sleep: Sleeper,
) -> dict[str, Any]:

    entity = _single_entity(
        tool_caller("get_entities", {"urns": [receipt.urn]}), receipt.urn
    )
    existing_sidq_values = _sidq_values(entity)
    existing_badges, ambiguous_badges = _managed_badge_state(entity)
    if ambiguous_badges:
        raise ReceiptWriteUnconfirmed(
            f"write_unconfirmed: conflicting tag URN aliases for {receipt.urn}"
        )
    context_hash = decision_context_hash(receipt.urn, entity, tool_caller)
    prepared = replace(receipt, context_hash=context_hash)
    saved = tool_caller(
        "save_document",
        {
            "document_type": "Decision",
            "title": f"Sidq {prepared.verdict} receipt for {prepared.urn}",
            "content": prepared.evidence_markdown(),
            "related_assets": [prepared.urn],
        },
    )
    document_reference = _document_reference(saved)
    if not document_reference:
        raise RuntimeError("save_document did not return a valid document URN")
    evidence_url = prepared.evidence_url or document_reference
    persisted = replace(prepared, evidence_url=evidence_url)
    desired_badge = _BADGE_BY_VERDICT[persisted.verdict]
    property_values = persisted.structured_property_values()
    badge_mutation_attempted = False
    touched_properties: set[str] = set()
    tag: Any = {"unchanged": True}
    try:
        obsolete_badges = sorted(existing_badges - {desired_badge})
        if obsolete_badges:
            badge_mutation_attempted = True
            tool_caller(
                "remove_tags",
                {
                    "tag_urns": obsolete_badges,
                    "entity_urns": [persisted.urn],
                },
            )
        if desired_badge not in existing_badges:
            badge_mutation_attempted = True
            tag = tool_caller(
                "add_tags",
                {
                    "tag_urns": [desired_badge],
                    "entity_urns": [persisted.urn],
                },
            )

        if not persisted.swarm_run:
            stale_swarm_properties = [
                f"{_SIDQ_PROPERTY_PREFIX}{name}"
                for name in ("swarm_run", "worker_id")
                if name in existing_sidq_values
            ]
            if stale_swarm_properties:
                touched_properties.update(stale_swarm_properties)
                tool_caller(
                    "remove_structured_properties",
                    {
                        "property_urns": stale_swarm_properties,
                        "entity_urns": [persisted.urn],
                    },
                )

        # The queryable body is the reader's authority, so publish it only after
        # the visible badge is in its intended state. Its direct, exact readback
        # is the success boundary for the whole non-transactional sequence.
        touched_properties.update(property_values)
        structured = tool_caller(
            "add_structured_properties",
            {
                "property_values": property_values,
                "entity_urns": [persisted.urn],
            },
        )
        confirmation_attempts = _confirm_receipt_readback(
            persisted,
            tool_caller,
            timeout=confirmation_timeout,
            initial_delay=confirmation_initial_delay,
            max_delay=confirmation_max_delay,
            monotonic=monotonic,
            sleep=sleep,
        )
    except Exception as error:
        rollback_errors = _restore_prior_receipt_state(
            tool_caller,
            persisted.urn,
            existing_sidq_values=existing_sidq_values,
            existing_badges=existing_badges,
            attempted_sidq_values=property_values,
            desired_badge=desired_badge,
            restore_badges=badge_mutation_attempted,
            touched_properties=touched_properties,
        )
        if rollback_errors:
            error.__dict__["receipt_rollback_errors"] = tuple(rollback_errors)
            error.add_note(
                "receipt rollback was incomplete: " + "; ".join(rollback_errors)
            )
        raise
    return {
        "receipt": persisted.to_dict(),
        "save_document": saved,
        "add_structured_properties": structured,
        "add_tags": tag,
        "confirmed": True,
        "confirmation_attempts": confirmation_attempts,
    }


def _managed_badges(entity: Mapping[str, Any]) -> frozenset[str]:
    return _managed_badge_state(entity)[0]


def _managed_badge_state(entity: Mapping[str, Any]) -> tuple[frozenset[str], bool]:
    urns: set[str] = set()
    ambiguous = False
    for field in ("globalTags", "global_tags", "tags"):
        surface_urns, surface_ambiguous = _tag_surface_urns(entity.get(field))
        urns.update(surface_urns & _MANAGED_BADGES)
        ambiguous = ambiguous or surface_ambiguous
    return frozenset(urns), ambiguous


def _tag_surface_urns(value: Any) -> tuple[set[str], bool]:
    if isinstance(value, str):
        return ({value} if value else set()), False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        urns: set[str] = set()
        ambiguous = False
        for assignment in value:
            assignment_urns, assignment_ambiguous = _tag_surface_urns(assignment)
            urns.update(assignment_urns)
            ambiguous = ambiguous or assignment_ambiguous
        return urns, ambiguous
    if not isinstance(value, Mapping):
        return set(), False

    direct_urns = {
        urn
        for urn in (value.get("tagUrn"), value.get("urn"))
        if isinstance(urn, str) and urn
    }
    nested = value.get("tag")
    if isinstance(nested, Mapping):
        nested_urn = nested.get("urn")
        if isinstance(nested_urn, str) and nested_urn:
            direct_urns.add(nested_urn)

    ambiguous = len(direct_urns) > 1
    if "tags" not in value:
        return direct_urns, ambiguous
    wrapped_urns, wrapped_ambiguous = _tag_surface_urns(value.get("tags"))
    return (
        direct_urns | wrapped_urns,
        ambiguous or wrapped_ambiguous or bool(direct_urns),
    )


def _restore_prior_receipt_state(
    tool_caller: ToolCaller,
    urn: str,
    *,
    existing_sidq_values: Mapping[str, list[str]],
    existing_badges: frozenset[str],
    attempted_sidq_values: Mapping[str, list[str]],
    desired_badge: str,
    restore_badges: bool,
    touched_properties: set[str],
) -> list[str]:
    """Best-effort compensation for DataHub's non-transactional mutations."""

    errors: list[str] = []

    if not restore_badges and not touched_properties:
        return errors

    try:
        entity = _single_entity(
            _confirmation_call(
                tool_caller,
                urn,
                deadline=time.monotonic() + _ROLLBACK_READ_TIMEOUT_SECONDS,
                monotonic=time.monotonic,
            ),
            urn,
        )
    except Exception as rollback_error:  # noqa: BLE001 - preserve original failure
        return [f"get_entities: {type(rollback_error).__name__}"]

    if not _managed_state_belongs_to_attempt(
        entity,
        existing_sidq_values=existing_sidq_values,
        existing_badges=existing_badges,
        attempted_sidq_values=attempted_sidq_values,
        desired_badge=desired_badge,
    ):
        return ["state_conflict: concurrent managed receipt detected"]

    def attempt(name: str, arguments: Mapping[str, Any]) -> None:
        try:
            tool_caller(name, arguments)
        except Exception as rollback_error:  # noqa: BLE001 - preserve original failure
            errors.append(f"{name}: {type(rollback_error).__name__}")

    if restore_badges:
        attempt(
            "remove_tags",
            {"tag_urns": sorted(_MANAGED_BADGES), "entity_urns": [urn]},
        )
        if existing_badges:
            attempt(
                "add_tags",
                {"tag_urns": sorted(existing_badges), "entity_urns": [urn]},
            )

    if touched_properties:
        attempt(
            "remove_structured_properties",
            {"property_urns": sorted(touched_properties), "entity_urns": [urn]},
        )
        prior_values = {
            property_urn: list(existing_sidq_values[name])
            for property_urn in sorted(touched_properties)
            if (name := property_urn.removeprefix(_SIDQ_PROPERTY_PREFIX))
            in existing_sidq_values
        }
        if prior_values:
            attempt(
                "add_structured_properties",
                {"property_values": prior_values, "entity_urns": [urn]},
            )

    return errors


def _managed_state_belongs_to_attempt(
    entity: Mapping[str, Any],
    *,
    existing_sidq_values: Mapping[str, list[str]],
    existing_badges: frozenset[str],
    attempted_sidq_values: Mapping[str, list[str]],
    desired_badge: str,
) -> bool:
    current_values = _sidq_values(entity)
    attempted_values = {
        urn.removeprefix(_SIDQ_PROPERTY_PREFIX): values
        for urn, values in attempted_sidq_values.items()
    }
    names = set(current_values) | set(existing_sidq_values) | set(attempted_values)
    for name in names:
        current = tuple(sorted(current_values.get(name, ())))
        prior = tuple(sorted(existing_sidq_values.get(name, ())))
        attempted = tuple(sorted(attempted_values.get(name, ())))
        if current not in {prior, attempted}:
            return False

    allowed_badges = existing_badges | {desired_badge}
    current_badges, ambiguous_badges = _managed_badge_state(entity)
    return not ambiguous_badges and current_badges <= allowed_badges


def _confirm_receipt_readback(
    receipt: Receipt,
    tool_caller: ToolCaller,
    *,
    timeout: float,
    initial_delay: float,
    max_delay: float,
    monotonic: Clock,
    sleep: Sleeper,
) -> int:
    """Poll the entity aspect directly until the exact receipt is visible.

    Mutation acknowledgements establish only that DataHub accepted the calls.
    They do not establish that a later process can consume the receipt.  This
    bounded read-after-write check deliberately uses ``get_entities`` rather
    than the asynchronously indexed search surface.  It confirms persistence;
    the independent reader still re-computes graph and policy staleness later.
    """

    if timeout < 0:
        raise ValueError("confirmation_timeout must be non-negative")
    if initial_delay <= 0 or max_delay <= 0:
        raise ValueError("confirmation delays must be positive")

    expected = {
        urn.removeprefix("urn:li:structuredProperty:sidq."): tuple(sorted(values))
        for urn, values in receipt.structured_property_values().items()
    }
    deadline = monotonic() + timeout
    delay = min(initial_delay, max_delay)
    attempts = 0
    last_mismatch: str | None = None
    while True:
        attempts += 1
        try:
            entity = _single_entity(
                _confirmation_call(
                    tool_caller,
                    receipt.urn,
                    deadline=deadline,
                    monotonic=monotonic,
                ),
                receipt.urn,
            )
        except ReceiptWriteUnconfirmed as error:
            if last_mismatch is None:
                raise
            raise ReceiptWriteUnconfirmed(
                f"{error}; last observed mismatch: {last_mismatch}"
            ) from error
        actual = {
            name: tuple(sorted(values)) for name, values in _sidq_values(entity).items()
        }
        expected_badge = _BADGE_BY_VERDICT[receipt.verdict]
        actual_badges, ambiguous_badges = _managed_badge_state(entity)
        if (
            actual == expected
            and not ambiguous_badges
            and actual_badges == {expected_badge}
        ):
            return attempts
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        differing = sorted(
            name
            for name in set(expected) & set(actual)
            if expected[name] != actual[name]
        )
        last_mismatch = (
            f"properties missing={missing!r}, unexpected={unexpected!r}, "
            f"differing={differing!r}; "
            "managed_badge="
            f"{'match' if not ambiguous_badges and actual_badges == {expected_badge} else 'mismatch'}"
        )

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ReceiptWriteUnconfirmed(
                "write_unconfirmed: exact receipt was not visible through "
                f"get_entities after {attempts} attempts for {receipt.urn}; "
                f"last observed mismatch: {last_mismatch}"
            )
        sleep(min(delay, remaining))
        delay = min(delay * 2, max_delay)


def _confirmation_call(
    tool_caller: ToolCaller,
    urn: str,
    *,
    deadline: float,
    monotonic: Clock,
) -> Any:
    """Call the confirmation transport without waiting past its deadline."""

    timeout = max(deadline - monotonic(), 0.0)
    bounded_call = getattr(tool_caller, "call_with_timeout", None)
    if callable(bounded_call):
        try:
            return bounded_call("get_entities", {"urns": [urn]}, timeout=timeout)
        except TimeoutError as error:
            raise ReceiptWriteUnconfirmed(
                "write_unconfirmed: get_entities exceeded the confirmation "
                f"deadline for {urn}"
            ) from error

    result: Future[Any] = Future()

    def call() -> None:
        try:
            result.set_result(tool_caller("get_entities", {"urns": [urn]}))
        except Exception as error:  # noqa: BLE001 - relay the transport failure
            result.set_exception(error)

    threading.Thread(
        target=call,
        name="sidq-receipt-confirmation",
        daemon=True,
    ).start()
    try:
        return result.result(timeout=timeout)
    except TimeoutError as error:
        raise ReceiptWriteUnconfirmed(
            "write_unconfirmed: get_entities exceeded the confirmation "
            f"deadline for {urn}"
        ) from error


def _document_reference(result: Any) -> str:
    """Accept both the MCP JSON response and a direct test-double response."""

    if isinstance(result, Mapping):
        urn = result.get("urn")
        if (
            isinstance(urn, str)
            and urn.startswith("urn:li:document:")
            and len(urn) > len("urn:li:document:")
            and not any(character.isspace() for character in urn)
        ):
            return urn
    return ""
