"""Smoke-test the Sidq MCP server over a real stdio client session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = ("check_change", "verify_context", "search_verified")
SHOWCASE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
    "b2fd91.order_entry_db.order_entry.customers,PROD)"
)


class SmokeFailure(RuntimeError):
    """Fail-closed smoke result safe to print without server payload details."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-command",
        help="Path to sidq-mcp (defaults to the current venv or PATH)",
    )
    return parser.parse_args()


def server_command(explicit: str | None) -> str:
    if explicit:
        return explicit
    beside_python = Path(sys.executable).with_name("sidq-mcp")
    if beside_python.is_file():
        return str(beside_python)
    discovered = shutil.which("sidq-mcp")
    if discovered:
        return discovered
    raise SystemExit(
        "sidq-mcp was not found; run 'make install' or pass --server-command"
    )


def tool_payload(result: Any) -> dict[str, Any]:
    if result.is_error:
        raise SmokeFailure("Sidq verify_context MCP call failed closed")
    payload = result.structured_content
    if payload is None:
        text = next(
            (
                item.text
                for item in result.content
                if getattr(item, "type", None) == "text"
            ),
            None,
        )
        payload = json.loads(text) if text is not None else None
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        raise SmokeFailure("Sidq verify_context returned no structured object")
    return payload


def verification_summary(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") if isinstance(error.get("code"), str) else "UNKNOWN"
        if code == "GRAPH_UNAVAILABLE":
            raise SmokeFailure(
                "Sidq chain failed closed: GRAPH_UNAVAILABLE from the catalog graph"
            )
        raise SmokeFailure(f"Sidq verify_context failed closed: {code}")
    if payload.get("urn") != SHOWCASE_URN:
        raise SmokeFailure("Sidq verify_context returned an unexpected asset")
    findings = payload.get("findings")
    unverifiable = payload.get("unverifiable")
    return {
        "urn": SHOWCASE_URN,
        "truthful": payload.get("truthful") is True,
        "findings": len(findings) if isinstance(findings, list) else 0,
        "unverifiable": len(unverifiable) if isinstance(unverifiable, list) else 0,
    }


async def smoke(command: str, verification_store: Path, server_stderr: TextIO) -> None:
    environment = dict(os.environ)
    environment["DATAHUB_TELEMETRY_ENABLED"] = "false"
    environment["SIDQ_VERIFICATION_STORE"] = str(verification_store)
    parameters = StdioServerParameters(command=command, env=environment)

    async with (
        stdio_client(parameters, errlog=server_stderr) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        names = tuple(tool.name for tool in listed.tools)
        if names != EXPECTED_TOOLS:
            raise SmokeFailure(
                "Sidq MCP tool contract mismatch: "
                f"expected {', '.join(EXPECTED_TOOLS)}; got {', '.join(names)}"
            )
        verified = verification_summary(
            tool_payload(
                await session.call_tool("verify_context", {"urn": SHOWCASE_URN})
            )
        )
    print(
        "Connected:",
        f"server={initialized.server_info.name}",
        f"version={initialized.server_info.version}",
    )
    print("Tools:", ", ".join(names))
    print(
        "Chain:",
        "client->sidq-mcp->mcp-server-datahub->GMS",
        f"urn={verified['urn']}",
        f"truthful={str(verified['truthful']).lower()}",
        f"findings={verified['findings']}",
        f"unverifiable={verified['unverifiable']}",
    )


def main() -> None:
    args = parse_args()
    with (
        tempfile.TemporaryDirectory(prefix="sidq-mcp-smoke-") as temporary,
        open(os.devnull, "w", encoding="utf-8") as server_stderr,
    ):
        asyncio.run(
            smoke(
                server_command(args.server_command),
                Path(temporary) / "verifications.json",
                server_stderr,
            )
        )


if __name__ == "__main__":
    main()
