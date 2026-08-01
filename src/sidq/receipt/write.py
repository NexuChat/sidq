"""Persist a completed receipt through the official DataHub MCP mutation tools."""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

from .build import Receipt
from .read import _single_entity, decision_context_hash

ToolCaller = Callable[[str, Mapping[str, Any]], Any]

_BADGE_BY_VERDICT = {
    "PASS": "urn:li:tag:sidq:verified",
    "WARN": "urn:li:tag:sidq:verified",
    "BLOCK": "urn:li:tag:sidq:blocked",
}


class StdioMCPReceiptToolCaller:
    """A synchronous caller for the official MCP server with mutations enabled.

    This is deliberately receipt-local: the graph reader owns a read-only
    session, while this boundary advertises the narrow write privilege required
    to persist Sidq's own receipt namespace and badges.
    """

    def __init__(
        self,
        command: str = "mcp-server-datahub",
        *,
        args: tuple[str, ...] = (),
        gms_url: str | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._gms_url = gms_url or os.environ.get(
            "DATAHUB_GMS_URL", "http://localhost:8080"
        )
        self._requests: queue.Queue[
            tuple[str, Mapping[str, Any], Future[Any]] | None
        ] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: Future[None] = Future()

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="sidq-receipt-mcp", daemon=True
            )
            self._thread.start()
            self._startup.result()
        result: Future[Any] = Future()
        self._requests.put((name, dict(arguments), result))
        return result.result()

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

        environment = _mcp_subprocess_environment(self._gms_url)
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
                name, arguments, result = request
                try:
                    response = await session.call_tool(name, dict(arguments))
                    is_error = getattr(response, "is_error", None)
                    if is_error is None:
                        is_error = getattr(response, "isError", False)
                    if is_error:
                        messages = _text_messages(response.content)
                        raise RuntimeError(f"MCP {name} failed: {' '.join(messages)}")
                    structured = getattr(response, "structured_content", None)
                    if structured is None:
                        structured = getattr(response, "structuredContent", None)
                    if structured is not None:
                        result.set_result(structured)
                    else:
                        text = next(
                            (text for text in _text_messages(response.content)),
                            "{}",
                        )
                        result.set_result(json.loads(text))
                except Exception as error:  # noqa: BLE001 - relay every MCP tool failure through its Future
                    result.set_exception(error)

    def close(self) -> None:
        if self._thread is not None:
            self._requests.put(None)
            self._thread.join(timeout=5)
        self._thread = None


def _mcp_subprocess_environment(gms_url: str) -> dict[str, str]:
    """Return only the environment needed by the receipt-writing MCP child."""
    environment = {
        "DATAHUB_GMS_URL": gms_url,
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "HOME": "/tmp",
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


def write_receipt(receipt: Receipt, tool_caller: ToolCaller) -> dict[str, Any]:
    """Write evidence, queryable fields, and a visible badge through MCP.

    The document is saved first so its returned URN becomes the evidence link on
    a receipt that did not already have an externally supplied evidence URL.
    """

    entity = _single_entity(
        tool_caller("get_entities", {"urns": [receipt.urn]}), receipt.urn
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
    structured = tool_caller(
        "add_structured_properties",
        {
            "property_values": persisted.structured_property_values(),
            "entity_urns": [persisted.urn],
        },
    )
    tag = tool_caller(
        "add_tags",
        {
            "tag_urns": [_BADGE_BY_VERDICT[persisted.verdict]],
            "entity_urns": [persisted.urn],
        },
    )
    return {
        "receipt": persisted.to_dict(),
        "save_document": saved,
        "add_structured_properties": structured,
        "add_tags": tag,
    }


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
