from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
from mcp import Client, ClientSession

from sidq.graph.client import DatasetInfo, LineageResult
from sidq.graph.fixtures import ReplayGraphClient
from sidq.mcp_server import SidqService, VerificationStore, create_server
from sidq.serialization import canonical_json

CUSTOMERS = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.order_entry_db.order_entry.customers,PROD)"
)
NEVER_CHECKED = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.never_checked,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"
FIXED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


class ReplayCatalog:
    """Add replayable upstream/search calls to the recorded graph snapshot."""

    def __init__(self) -> None:
        self.replay = ReplayGraphClient(FIXTURES)

    def get_dataset(self, urn: str):
        return self.replay.get_dataset(urn)

    def find_dataset(self, name_or_urn: str):
        return self.replay.find_dataset(name_or_urn)

    def get_downstream(self, urn: str, depth: int, column: str | None = None):
        return self.replay.get_downstream(urn, depth, column)

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ):
        return self.replay.paths_between(
            a,
            b,
            source_column=source_column,
            target_column=target_column,
        )

    def get_upstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        assert urn == CUSTOMERS
        assert depth == 1
        assert column is not None
        return LineageResult(
            urns=(CUSTOMERS,),
            columns={CUSTOMERS: (column,)},
            granularity="column",
        )

    def search_assets(self, query: str) -> list[str]:
        assert query == "customers"
        return [NEVER_CHECKED, CUSTOMERS]


class MatchingLiveSource:
    def __init__(self, graph: ReplayCatalog) -> None:
        dataset = graph.get_dataset(CUSTOMERS)
        assert dataset is not None
        self.dataset = dataset

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self.dataset if urn == CUSTOMERS else None


def _project(tmp_path: Path, graph: ReplayCatalog) -> str:
    dataset = graph.get_dataset(CUSTOMERS)
    assert dataset is not None
    columns = [field.path for field in dataset.fields]
    manifest = {
        "metadata": {"adapter_type": "dbt"},
        "nodes": {
            "model.sidq.customers": {
                "original_file_path": "models/customers.sql",
                "relation_name": "b2fd91.order_entry_db.order_entry.customers",
                "config": {"meta": {"environment": "PROD"}},
                "columns": {column: {} for column in columns},
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    model = tmp_path / "models" / "customers.sql"
    model.parent.mkdir()
    sql = (
        "select\n    "
        + ",\n    ".join(columns)
        + "\nfrom b2fd91.order_entry_db.order_entry.customers\n"
    )
    model.write_text(sql, encoding="utf-8")
    return sql


def _assert_canonical(result) -> dict:
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.content[0].text == canonical_json(result.structured_content).decode(
        "utf-8"
    )
    return result.structured_content


def test_three_tools_over_real_in_process_mcp_session(tmp_path: Path) -> None:
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    service = SidqService(
        graph,
        live_source=MatchingLiveSource(graph),
        repo_root=tmp_path,
        store=VerificationStore(tmp_path / "verifications.json"),
        clock=lambda: FIXED_NOW,
    )

    async def exercise() -> None:
        async with Client(create_server(service), mode="legacy") as client:
            assert isinstance(client.session, ClientSession)

            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "check_change",
                "verify_context",
                "search_verified",
            ]
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools["check_change"].input_schema["properties"]) == {
                "diff",
                "sql",
                "policy_path",
            }
            assert tools["check_change"].input_schema.get("required", []) == []
            assert tools["verify_context"].input_schema["required"] == ["urn"]
            assert tools["search_verified"].input_schema["required"] == ["query"]
            assert (
                tools["search_verified"].input_schema["properties"]["max_age_days"][
                    "default"
                ]
                == 7
            )
            assert (
                tools["search_verified"].input_schema["properties"]["max_age_days"][
                    "minimum"
                ]
                == 0
            )
            assert all(tool.output_schema["type"] == "object" for tool in listed.tools)
            assert "before proposing" in tools["check_change"].description
            assert "before trusting" in tools["verify_context"].description
            assert "when choosing" in tools["search_verified"].description

            proposed_sql = sql.replace("    cust_email,\n", "")
            checked = _assert_canonical(
                await client.call_tool("check_change", {"sql": proposed_sql})
            )
            assert checked["decision"] == "BLOCK"
            assert checked["touched"][0]["urn"] == CUSTOMERS
            assert checked["commit_sha"].startswith("sha256:")

            verified_call = await client.call_tool("verify_context", {"urn": CUSTOMERS})
            verified = _assert_canonical(verified_call)
            assert verified == {
                "checked_at": "2026-07-28T12:00:00Z",
                "findings": [],
                "truthful": True,
                "unverifiable": [],
                "urn": CUSTOMERS,
            }
            repeated = await client.call_tool("verify_context", {"urn": CUSTOMERS})
            assert repeated.content[0].text == verified_call.content[0].text

            searched = _assert_canonical(
                await client.call_tool(
                    "search_verified",
                    {"query": "customers", "max_age_days": 7},
                )
            )
            assert searched["verified"] == [
                {
                    "checked_at": "2026-07-28T12:00:00Z",
                    "truthful": True,
                    "urn": CUSTOMERS,
                    "verdict": "TRUTHFUL",
                }
            ]
            assert searched["rejected"] == []
            assert searched["unverified"] == [
                {
                    "reason": "never_checked",
                    "status": "unverified",
                    "urn": NEVER_CHECKED,
                }
            ]

    anyio.run(exercise)


def test_search_graph_error_is_explicit(tmp_path: Path) -> None:
    class BrokenSearch(ReplayCatalog):
        def search_assets(self, query: str) -> list[str]:
            raise RuntimeError("offline")

    graph = BrokenSearch()
    _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path, clock=lambda: FIXED_NOW)

    async def exercise() -> None:
        async with Client(create_server(service), mode="legacy") as client:
            result = _assert_canonical(
                await client.call_tool("search_verified", {"query": "customers"})
            )
            assert result["error"]["code"] == "GRAPH_UNAVAILABLE"
            assert result["verified"] == []
            assert "not an empty successful result" in result["error"]["message"]

    anyio.run(exercise)
