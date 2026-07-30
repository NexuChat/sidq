"""Smoke-test the live DataHub MCP server over stdio.

This script deliberately uses only MCP tools for the three catalog operations:
searching for an asset, fetching its schema, and fetching its lineage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_QUERY = "b2fd91.order_entry_db.order_entry.customers"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gms-url",
        default=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        help="DataHub GMS URL (default: %(default)s)",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Catalog search query used to select a dataset",
    )
    parser.add_argument(
        "--column",
        default="cust_email",
        help="Column whose downstream lineage is fetched",
    )
    parser.add_argument(
        "--server-command",
        help="Path to mcp-server-datahub (defaults to the current venv or PATH)",
    )
    return parser.parse_args()


def server_command(explicit: str | None) -> str:
    if explicit:
        return explicit
    beside_python = Path(sys.executable).with_name("mcp-server-datahub")
    if beside_python.is_file():
        return str(beside_python)
    discovered = shutil.which("mcp-server-datahub")
    if discovered:
        return discovered
    raise SystemExit(
        "mcp-server-datahub was not found; install mcp-server-datahub==0.6.0 "
        "in this environment or pass --server-command"
    )


def decode_result(tool_name: str, result: Any) -> Any:
    if result.is_error:
        messages = [
            item.text
            for item in result.content
            if getattr(item, "type", None) == "text"
        ]
        raise RuntimeError(f"{tool_name} failed: {'; '.join(messages)}")
    if result.structuredContent and "result" in result.structuredContent:
        return result.structuredContent["result"]
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise RuntimeError(f"{tool_name} returned no JSON content")


def print_json(label: str, value: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, indent=2, sort_keys=True))


def lineage_summary(lineage: dict[str, Any]) -> dict[str, Any]:
    direction = "downstreams" if "downstreams" in lineage else "upstreams"
    result = lineage.get(direction) or {}
    summarized = []
    for hit in result.get("searchResults") or []:
        entity = hit.get("entity") or {}
        summarized.append(
            {
                "urn": entity.get("urn"),
                "type": entity.get("type"),
                "name": entity.get("name"),
                "platform": (entity.get("platform") or {}).get("name"),
                "degree": hit.get("degree"),
                "paths": [
                    [node.get("urn") for node in path.get("path") or []]
                    for path in hit.get("paths") or []
                ],
            }
        )
    return {
        "direction": direction,
        "total": result.get("total"),
        "returned": len(summarized),
        "searchResults": summarized,
    }


async def smoke(args: argparse.Namespace, server_stderr: TextIO) -> None:
    environment = dict(os.environ)
    environment.update(
        DATAHUB_GMS_URL=args.gms_url,
        DATAHUB_TELEMETRY_ENABLED="false",
        TOOLS_IS_MUTATION_ENABLED="false",
        LOGURU_LEVEL="ERROR",
    )
    parameters = StdioServerParameters(
        command=server_command(args.server_command),
        env=environment,
    )

    async with (
        stdio_client(parameters, errlog=server_stderr) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        print(
            "Connected:",
            f"server={initialized.server_info.name}",
            f"version={initialized.server_info.version}",
            f"gms={args.gms_url}",
        )

        search = decode_result(
            "search",
            await session.call_tool("search", {"query": args.query, "num_results": 5}),
        )
        datasets = [
            hit
            for hit in search.get("searchResults", [])
            if (hit.get("entity") or {}).get("urn", "").startswith("urn:li:dataset:")
        ]
        if not datasets:
            raise RuntimeError(f"search returned no dataset for query {args.query!r}")
        selected = datasets[0]["entity"]
        urn = selected["urn"]
        print_json(
            "MCP search",
            {
                "query": args.query,
                "total": search.get("total"),
                "selected": selected,
            },
        )

        schema = decode_result(
            "list_schema_fields",
            await session.call_tool("list_schema_fields", {"urn": urn, "limit": 100}),
        )
        print_json("MCP list_schema_fields", schema)

        lineage = decode_result(
            "get_lineage",
            await session.call_tool(
                "get_lineage",
                {
                    "urn": urn,
                    "column": args.column,
                    "upstream": False,
                    "max_hops": 3,
                    "max_results": 30,
                },
            ),
        )
        print_json(
            f"MCP get_lineage column={args.column}",
            lineage_summary(lineage),
        )


def main() -> None:
    with open(os.devnull, "w", encoding="utf-8") as server_stderr:
        asyncio.run(smoke(parse_args(), server_stderr))


if __name__ == "__main__":
    main()
