from __future__ import annotations

import difflib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from mcp import Client, ClientSession

from sidq import cli
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField
from sidq.graph.fixtures import ReplayGraphClient
from sidq.graph.live_source import LiveConstraint
from sidq.mcp_server import SidqService, VerificationStore, create_server
from sidq.policy.engine import default_policy_path
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
            a, b, source_column=source_column, target_column=target_column
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
    """Write a one-model dbt project matching the CUSTOMERS fixture; return its SQL."""
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


def _call(service: SidqService, name: str, arguments: dict):
    """Call one tool over a real in-process MCP session and return the result."""

    async def exercise():
        async with Client(create_server(service), mode="legacy") as client:
            return await client.call_tool(name, arguments)

    return anyio.run(exercise)


# ---------------------------------------------------------------------------
# 1. Tool contract: schema matches the handler, required args, output shape.
# ---------------------------------------------------------------------------


def test_tool_listing_names_and_read_only_annotations() -> None:
    service = SidqService(ReplayCatalog(), repo_root=".")

    async def exercise():
        async with Client(create_server(service), mode="legacy") as client:
            assert isinstance(client.session, ClientSession)
            return await client.list_tools()

    listed = anyio.run(exercise)

    assert [tool.name for tool in listed.tools] == [
        "check_change",
        "verify_context",
        "search_verified",
    ]
    assert all(tool.annotations.read_only_hint is True for tool in listed.tools)
    assert all(tool.output_schema["type"] == "object" for tool in listed.tools)


def test_check_change_schema_has_no_required_fields_but_reads_all_three(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path)

    async def exercise():
        async with Client(create_server(service), mode="legacy") as client:
            return await client.list_tools()

    listed = anyio.run(exercise)
    tool = next(tool for tool in listed.tools if tool.name == "check_change")

    assert set(tool.input_schema["properties"]) == {"diff", "sql", "policy_path"}
    assert tool.input_schema.get("required", []) == []


def test_verify_context_and_search_verified_schemas_match_their_handlers() -> None:
    service = SidqService(ReplayCatalog(), repo_root=".")

    async def exercise():
        async with Client(create_server(service), mode="legacy") as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in anyio.run(exercise).tools}

    assert tools["verify_context"].input_schema["required"] == ["urn"]
    assert set(tools["verify_context"].input_schema["properties"]) == {"urn"}
    search_props = tools["search_verified"].input_schema["properties"]
    assert tools["search_verified"].input_schema["required"] == ["query"]
    assert search_props["max_age_days"]["default"] == 7
    assert search_props["max_age_days"]["minimum"] == 0


@pytest.mark.parametrize(
    ("name", "output_keys"),
    [
        ("check_change", {"decision", "error", "verdict", "commit_sha"}),
        ("verify_context", {"urn", "truthful", "unverifiable", "error"}),
        ("search_verified", {"verified", "rejected", "unverified", "error"}),
    ],
)
def test_output_schema_declares_the_fields_the_handler_actually_returns(
    name: str, output_keys: set[str]
) -> None:
    service = SidqService(ReplayCatalog(), repo_root=".")

    async def exercise():
        async with Client(create_server(service), mode="legacy") as client:
            return await client.list_tools()

    tool = next(tool for tool in anyio.run(exercise).tools if tool.name == name)

    assert output_keys <= set(tool.output_schema["properties"])


@pytest.mark.parametrize("arguments", [{}, {"diff": "a", "sql": "b"}])
def test_check_change_requires_exactly_one_of_diff_or_sql(
    tmp_path: Path, arguments: dict
) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", arguments))

    assert result == {
        "error": {
            "code": "INVALID_INPUT",
            "message": "Provide exactly one of diff or sql.",
            "details": {},
        }
    }


def test_unknown_argument_is_ignored_not_rejected(tmp_path: Path) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path, clock=lambda: FIXED_NOW)

    plain = _assert_canonical(_call(service, "verify_context", {"urn": CUSTOMERS}))
    with_extra = _assert_canonical(
        _call(service, "verify_context", {"urn": CUSTOMERS, "bogus": "ignored"})
    )

    assert with_extra == plain


@pytest.mark.parametrize(
    ("name", "arguments"), [("verify_context", {}), ("search_verified", {})]
)
def test_missing_required_argument_is_a_tool_error_not_a_crash(
    name: str, arguments: dict
) -> None:
    service = SidqService(ReplayCatalog(), repo_root=".")

    result = _call(service, name, arguments)

    assert result.is_error is True
    assert "Traceback" not in result.content[0].text
    assert "required" in result.content[0].text.lower()


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x", "max_age_days": "not-a-number"},
        {"query": "x", "max_age_days": -1},
    ],
)
def test_search_verified_rejects_bad_argument_types_and_ranges(
    arguments: dict,
) -> None:
    service = SidqService(ReplayCatalog(), repo_root=".")

    result = _call(service, "search_verified", arguments)

    assert result.is_error is True


# ---------------------------------------------------------------------------
# 2. Error paths: never a raw traceback, always a structured, actionable error.
# ---------------------------------------------------------------------------


def test_check_change_missing_policy_file_is_invalid_input_not_a_crash(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(
        _call(
            service,
            "check_change",
            {"sql": sql, "policy_path": str(tmp_path / "does-not-exist.yaml")},
        )
    )

    assert result["error"]["code"] == "INVALID_INPUT"
    assert "does-not-exist.yaml" in result["error"]["message"]


def test_check_change_unparseable_policy_is_invalid_input_not_a_crash(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\nrules:\n  - id: bad\n", encoding="utf-8")
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(
        _call(service, "check_change", {"sql": sql, "policy_path": str(policy)})
    )

    assert result["error"]["code"] == "INVALID_INPUT"


def test_check_change_relaxed_policy_actually_changes_the_decision(
    tmp_path: Path,
) -> None:
    """Proves policy_path is really plumbed into the engine, not just accepted."""
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    proposed = sql.replace("    cust_email,\n", "")
    policy = tmp_path / "empty.yaml"
    policy.write_text("version: 1\nrules: []\n", encoding="utf-8")
    service = SidqService(graph, repo_root=tmp_path)

    blocked = _assert_canonical(_call(service, "check_change", {"sql": proposed}))
    relaxed = _assert_canonical(
        _call(service, "check_change", {"sql": proposed, "policy_path": str(policy)})
    )

    assert blocked["decision"] == "BLOCK"
    assert relaxed["decision"] == "PASS"


def test_check_change_engine_failure_is_a_structured_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    monkeypatch.setattr(
        cli, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret dsn"))
    )
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"sql": sql}))

    assert result == {
        "error": {
            "code": "ENGINE_ERROR",
            "message": "Sidq could not complete the change check.",
            "details": {"type": "RuntimeError"},
        }
    }
    assert "secret dsn" not in result["error"]["message"]


def test_verify_context_asset_not_found_is_structured_not_an_empty_pass() -> None:
    class NoSuchAsset(ReplayCatalog):
        def get_dataset(self, urn: str):
            return None

    service = SidqService(NoSuchAsset(), repo_root=".", clock=lambda: FIXED_NOW)

    result = _assert_canonical(_call(service, "verify_context", {"urn": CUSTOMERS}))

    assert result["truthful"] is False
    assert result["error"] == {
        "code": "ASSET_NOT_FOUND",
        "message": "The requested asset was not found in the catalog.",
        "details": {"urn": CUSTOMERS},
    }


@pytest.mark.parametrize("name", ["check_change", "verify_context", "search_verified"])
def test_no_tool_ever_leaks_the_raw_exception_message_from_the_graph(
    tmp_path: Path, name: str
) -> None:
    secret = "postgres://user:hunter2@internal-host/db"

    class Broken(ReplayCatalog):
        def get_dataset(self, urn: str):
            raise RuntimeError(secret)

        def get_downstream(self, urn: str, depth: int, column: str | None = None):
            raise RuntimeError(secret)

        def search_assets(self, query: str) -> list[str]:
            raise RuntimeError(secret)

    sql = _project(tmp_path, ReplayCatalog()) if name == "check_change" else None
    graph = Broken()
    service = SidqService(graph, repo_root=tmp_path)
    arguments = {
        "check_change": {"sql": sql},
        "verify_context": {"urn": CUSTOMERS},
        "search_verified": {"query": "customers"},
    }[name]

    result = _assert_canonical(_call(service, name, arguments))

    assert secret not in json.dumps(result)
    assert result["error"]["code"] in {"GRAPH_UNAVAILABLE"}


# ---------------------------------------------------------------------------
# 3. Fail-closed must survive the MCP boundary: a refusal is never a soft pass.
# ---------------------------------------------------------------------------


class _MinimalGraph:
    """A tiny graph client that never fails, isolating one scenario at a time."""

    def __init__(self) -> None:
        self._dataset = DatasetInfo(
            CUSTOMERS, (SchemaField("customer_id", "STRING", False),)
        )

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self._dataset if urn == CUSTOMERS else None

    def find_dataset(self, name_or_urn: str) -> str | None:
        return CUSTOMERS if name_or_urn == CUSTOMERS else None

    def get_downstream(self, urn: str, depth: int, column: str | None = None):
        return LineageResult()

    def paths_between(self, a: str, b: str, **kwargs):
        return []


def test_unparseable_sql_blocks_with_unverifiable_change_at_the_top_level(
    tmp_path: Path,
) -> None:
    graph = _MinimalGraph()
    _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(
        _call(service, "check_change", {"sql": "not valid sql at all $$$ ((("})
    )

    assert "error" not in result
    assert result["decision"] == "BLOCK"
    assert result["reason_code"] == "UNVERIFIABLE_CHANGE"


def test_graph_unavailable_during_check_change_still_carries_a_block_verdict(
    tmp_path: Path,
) -> None:
    class BrokenDownstream(ReplayCatalog):
        def get_downstream(self, urn: str, depth: int, column: str | None = None):
            raise RuntimeError("datahub offline")

    graph = BrokenDownstream()
    sql = _project(tmp_path, graph)
    proposed = sql.replace("    cust_email,\n", "")
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"sql": proposed}))

    assert result["error"]["code"] == "GRAPH_UNAVAILABLE"
    assert result["verdict"]["decision"] == "BLOCK"
    assert result["verdict"]["reason_code"] == "UNVERIFIABLE_CHANGE"


def test_graph_unavailable_refusal_is_still_readable_at_the_top_level_decision_key(
    tmp_path: Path,
) -> None:
    class BrokenDownstream(ReplayCatalog):
        def get_downstream(self, urn: str, depth: int, column: str | None = None):
            raise RuntimeError("datahub offline")

    graph = BrokenDownstream()
    sql = _project(tmp_path, graph)
    proposed = sql.replace("    cust_email,\n", "")
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"sql": proposed}))

    assert result.get("decision") == "BLOCK"


# ---------------------------------------------------------------------------
# 4. Determinism: identical inputs produce byte-identical output.
# ---------------------------------------------------------------------------


def test_check_change_is_byte_identical_across_repeated_calls(tmp_path: Path) -> None:
    graph = ReplayCatalog()
    sql = _project(tmp_path, graph)
    service = SidqService(graph, repo_root=tmp_path)

    first = _call(service, "check_change", {"sql": sql})
    second = _call(service, "check_change", {"sql": sql})

    assert first.content[0].text == second.content[0].text


def test_verify_context_is_byte_identical_across_repeated_calls(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    service = SidqService(
        graph,
        live_source=MatchingLiveSource(graph),
        repo_root=tmp_path,
        store=VerificationStore(tmp_path / "verifications.json"),
        clock=lambda: FIXED_NOW,
    )

    first = _call(service, "verify_context", {"urn": CUSTOMERS})
    second = _call(service, "verify_context", {"urn": CUSTOMERS})

    assert first.content[0].text == second.content[0].text


@pytest.mark.parametrize("kind", ["sql", "diff"])
def test_commit_sha_is_a_stable_content_hash_not_a_git_sha(
    tmp_path: Path, kind: str
) -> None:
    graph = ReplayCatalog()
    before = _project(tmp_path, graph)
    proposed = before.replace("    cust_email,\n", "")
    if kind == "sql":
        payload, content = {"sql": proposed}, proposed
    else:
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                proposed.splitlines(keepends=True),
                fromfile="a/models/customers.sql",
                tofile="b/models/customers.sql",
            )
        )
        payload, content = {"diff": diff}, diff
    service = SidqService(graph, repo_root=tmp_path)
    expected = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

    result = _assert_canonical(_call(service, "check_change", payload))

    assert result["commit_sha"] == expected


def test_policy_hash_matches_the_actual_default_policy_bytes(tmp_path: Path) -> None:
    graph = ReplayCatalog()
    before = _project(tmp_path, graph)
    proposed = before.replace("    cust_email,\n", "")
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"sql": proposed}))

    assert (
        result["policy_hash"]
        == hashlib.sha256(default_policy_path().read_bytes()).hexdigest()
    )


def test_search_verified_is_byte_identical_across_repeated_calls(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    store = VerificationStore(tmp_path / "verifications.json")
    service = SidqService(
        graph, repo_root=tmp_path, store=store, clock=lambda: FIXED_NOW
    )
    service.verify_context(CUSTOMERS)

    first = _call(service, "search_verified", {"query": "customers"})
    second = _call(service, "search_verified", {"query": "customers"})

    assert first.content[0].text == second.content[0].text


# ---------------------------------------------------------------------------
# 5. No mutation from a read tool.
# ---------------------------------------------------------------------------


class _NoWriteGraph(ReplayCatalog):
    """Fails the test the moment anything beyond the read-only surface is touched."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def get_dataset(self, urn: str):
        self.calls.append("get_dataset")
        return super().get_dataset(urn)

    def get_downstream(self, urn: str, depth: int, column: str | None = None):
        self.calls.append("get_downstream")
        return super().get_downstream(urn, depth, column)

    def paths_between(self, a: str, b: str, **kwargs):
        self.calls.append("paths_between")
        return super().paths_between(a, b, **kwargs)

    def search_assets(self, query: str) -> list[str]:
        self.calls.append("search_assets")
        return super().search_assets(query)

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected/mutating graph call: {name}")


def test_verify_context_and_search_verified_never_call_a_write_method(
    tmp_path: Path,
) -> None:
    graph = _NoWriteGraph()
    _project(tmp_path, graph)
    service = SidqService(
        graph,
        live_source=MatchingLiveSource(graph),
        repo_root=tmp_path,
        clock=lambda: FIXED_NOW,
    )

    _call(service, "verify_context", {"urn": CUSTOMERS})
    _call(service, "search_verified", {"query": "customers"})

    assert set(graph.calls) <= {"get_dataset", "get_downstream", "search_assets"}


def test_search_verified_never_writes_to_the_verification_store(tmp_path: Path) -> None:
    class NoPutStore(VerificationStore):
        def put(self, result):
            raise AssertionError("search_verified must never persist a record")

    graph = ReplayCatalog()
    store_path = tmp_path / "verifications.json"
    service = SidqService(graph, repo_root=tmp_path, store=NoPutStore(store_path))

    _call(service, "search_verified", {"query": "customers"})

    assert not store_path.exists()


def test_verify_context_does_not_write_to_disk_despite_its_read_only_annotation(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    _project(tmp_path, graph)
    store_path = tmp_path / "verifications.json"
    service = SidqService(
        graph, repo_root=tmp_path, store=VerificationStore(store_path)
    )

    _call(service, "verify_context", {"urn": CUSTOMERS})

    assert not store_path.exists()


# ---------------------------------------------------------------------------
# 6. Injection and escaping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "urn:li:dataset:(urn:li:dataPlatform:dbt,x\n# injected,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:dbt,x```\n**bold**,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:dbt,x\x07\x1b[31m,PROD)",
    ],
)
def test_injected_control_characters_in_urn_cannot_break_the_response(
    payload: str,
) -> None:
    service = SidqService(ReplayCatalog(), repo_root=".", clock=lambda: FIXED_NOW)

    result = _call(service, "verify_context", {"urn": payload})

    text = result.content[0].text
    assert text.count("\n") == 0
    assert json.loads(text)["urn"] == payload
    assert "\n# injected" not in text


def test_injected_markdown_in_search_query_cannot_break_the_response() -> None:
    service = SidqService(ReplayCatalog(), repo_root=".", clock=lambda: FIXED_NOW)
    payload = "customers\n# injected\n<script>alert(1)</script>"

    class Broken(ReplayCatalog):
        def search_assets(self, query: str) -> list[str]:
            return []

    service = SidqService(Broken(), repo_root=".", clock=lambda: FIXED_NOW)
    result = _call(service, "search_verified", {"query": payload})

    text = result.content[0].text
    assert text.count("\n") == 0
    assert json.loads(text)["query"] == payload


# ---------------------------------------------------------------------------
# 7. Verification-store round trip (the closest thing to a receipt this
#    server reads back); the server exposes no receipt-reading tool at all.
# ---------------------------------------------------------------------------


def test_verification_store_round_trips_a_record_through_a_fresh_instance(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "verifications.json"
    written = VerificationStore(store_path)
    record = {
        "urn": CUSTOMERS,
        "truthful": True,
        "checked_at": "2026-07-28T12:00:00Z",
        "findings": [],
        "unverifiable": [],
    }
    written.put(record)

    reread = VerificationStore(store_path).get(CUSTOMERS)

    assert reread == record


def test_corrupted_store_file_is_treated_as_never_checked_not_a_crash(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "verifications.json"
    store_path.write_text("{not valid json", encoding="utf-8")

    assert VerificationStore(store_path).get(CUSTOMERS) is None


def test_forged_store_record_is_rejected_rather_than_trusted(tmp_path: Path) -> None:
    store_path = tmp_path / "verifications.json"
    store_path.write_text(
        json.dumps(
            {
                "records": {
                    NEVER_CHECKED: {
                        "urn": NEVER_CHECKED,
                        "truthful": True,
                        "checked_at": "2026-07-28T11:00:00Z",
                        "findings": [],
                        "unverifiable": [],
                    }
                },
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    service = SidqService(
        ReplayCatalog(),
        store=VerificationStore(store_path),
        clock=lambda: FIXED_NOW,
    )

    result = service.search_verified("customers", 7)

    assert result["verified"] == []


# ---------------------------------------------------------------------------
# Diff workspace: the MCP-server-local unified-diff parser and patch applier.
# ---------------------------------------------------------------------------


def test_check_change_over_a_real_unified_diff_finds_the_removed_column(
    tmp_path: Path,
) -> None:
    graph = ReplayCatalog()
    before = _project(tmp_path, graph)
    after = before.replace("    cust_email,\n", "")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/models/customers.sql",
            tofile="b/models/customers.sql",
        )
    )
    service = SidqService(graph, repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"diff": diff}))

    assert result["decision"] == "BLOCK"
    assert result["touched"][0]["removed_fields"] == ["cust_email"]


@pytest.mark.parametrize(
    ("diff", "expected_message"),
    [
        ("just some text\nno diff markers\n", "unified diff"),
        (
            (
                "--- a/models/unknown.sql\n"
                "+++ b/models/unknown.sql\n"
                "@@ -1,1 +1,1 @@\n-x\n+y\n"
            ),
            "no manifest entry maps changed file",
        ),
    ],
)
def test_malformed_diff_is_invalid_input_not_a_crash(
    tmp_path: Path, diff: str, expected_message: str
) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"metadata": {"adapter_type": "dbt"}, "nodes": {}}),
        encoding="utf-8",
    )
    service = SidqService(ReplayCatalog(), repo_root=tmp_path)

    result = _assert_canonical(_call(service, "check_change", {"diff": diff}))

    assert result["error"]["code"] == "INVALID_INPUT"
    assert expected_message in result["error"]["message"]


# ---------------------------------------------------------------------------
# search_verified partitioning and its private-graph-search fallback.
# ---------------------------------------------------------------------------


def test_search_verified_partitions_by_verification_status(tmp_path: Path) -> None:
    stale_urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,x.stale,PROD)"
    gap_urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,x.gap,PROD)"
    rejected_urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,x.rejected,PROD)"

    class FakeGraph:
        def search_assets(self, query: str) -> list[str]:
            return [CUSTOMERS, NEVER_CHECKED, stale_urn, gap_urn, rejected_urn]

    store = VerificationStore(tmp_path / "verifications.json")
    store.put(
        {"urn": CUSTOMERS, "truthful": True, "checked_at": "2026-07-28T12:00:00Z"}
    )
    store.put(
        {"urn": stale_urn, "truthful": True, "checked_at": "2020-01-01T00:00:00Z"}
    )
    store.put(
        {
            "urn": gap_urn,
            "truthful": False,
            "checked_at": "2026-07-28T12:00:00Z",
            "unverifiable": [{"check": "schema_drift", "reason": "no live source"}],
        }
    )
    store.put(
        {"urn": rejected_urn, "truthful": False, "checked_at": "2026-07-28T12:00:00Z"}
    )
    service = SidqService(FakeGraph(), store=store, clock=lambda: FIXED_NOW)

    result = service.search_verified("x", 7)

    assert [item["urn"] for item in result["verified"]] == [CUSTOMERS]
    assert [item["urn"] for item in result["rejected"]] == [rejected_urn]
    assert {item["urn"]: item["status"] for item in result["unverified"]} == {
        NEVER_CHECKED: "unverified",
        stale_urn: "stale",
        gap_urn: "unverifiable",
    }


def test_search_verified_falls_back_to_the_private_search_method() -> None:
    """MCPGraphClient (production) exposes only ``_search``, never ``search_assets``."""

    class PrivateSearchOnly:
        def _search(self, query: str):
            return {
                "result": [{"entity": {"urn": CUSTOMERS}}, {"urn": "urn:li:chart:x"}]
            }

    service = SidqService(PrivateSearchOnly(), clock=lambda: FIXED_NOW)

    result = service.search_verified("customers", 7)

    assert [item["urn"] for item in result["unverified"]] == [CUSTOMERS]


def test_search_verified_with_no_search_capability_is_graph_unavailable() -> None:
    service = SidqService(object(), clock=lambda: FIXED_NOW)

    result = service.search_verified("customers", 7)

    assert result["error"]["code"] == "GRAPH_UNAVAILABLE"
    assert result["verified"] == []


# ---------------------------------------------------------------------------
# 7. Constraint reconciliation inside verify_context.
# ---------------------------------------------------------------------------


class _ConstraintSource:
    """A live source that reports exactly the constraints it is given."""

    def __init__(self, dataset: DatasetInfo | None, *constraints: LiveConstraint):
        self.dataset = dataset
        self.constraints = constraints

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self.dataset

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        return self.constraints


class _SchemaOnlyGraph:
    """A graph whose only interesting aspect is the schema of one dataset."""

    def __init__(self, *fields: SchemaField) -> None:
        self.dataset = DatasetInfo(CUSTOMERS, fields)

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self.dataset if urn == CUSTOMERS else None

    def get_downstream(self, urn: str, depth: int, column: str | None = None):
        return None

    def paths_between(self, source: str, target: str, column: str | None = None):
        return None


def _not_null(column: str) -> LiveConstraint:
    return LiveConstraint(
        f"{column}_not_null", "not_null", (column,), f"{column} NOT NULL"
    )


def test_catalog_not_null_claim_the_source_does_not_enforce_is_a_finding() -> None:
    """The headline Gate 0 case: the catalog claims more than the source enforces."""
    graph = _SchemaOnlyGraph(
        SchemaField("cust_id", "int", False),
        SchemaField("shipped_at", "timestamp", False),
    )
    source = _ConstraintSource(graph.dataset, _not_null("cust_id"))
    service = SidqService(graph, live_source=source, clock=lambda: FIXED_NOW)

    findings, unverifiable = service._constraint_evidence(CUSTOMERS, graph.dataset)

    kinds = {item.kind for item in findings}
    assert kinds == {"constraint_contradicts_catalog"}
    assert findings[0].subject.endswith("#shipped_at")
    assert unverifiable == []


def test_a_confirmed_constraint_is_not_reported_as_a_finding() -> None:
    graph = _SchemaOnlyGraph(SchemaField("cust_id", "int", False))
    source = _ConstraintSource(graph.dataset, _not_null("cust_id"))
    service = SidqService(graph, live_source=source, clock=lambda: FIXED_NOW)

    findings, unverifiable = service._constraint_evidence(CUSTOMERS, graph.dataset)

    assert findings == []
    assert unverifiable == []


def test_source_enforcing_more_than_the_catalog_claims_is_not_a_truth_finding() -> None:
    """A CHECK the graph seam cannot express must not mark the asset untruthful."""
    graph = _SchemaOnlyGraph(SchemaField("cust_id", "int", False))
    source = _ConstraintSource(
        graph.dataset,
        _not_null("cust_id"),
        LiveConstraint(
            "total_positive", "check", ("order_total",), "CHECK (order_total >= 0)"
        ),
    )
    service = SidqService(graph, live_source=source, clock=lambda: FIXED_NOW)

    findings, unverifiable = service._constraint_evidence(CUSTOMERS, graph.dataset)

    assert findings == []
    assert unverifiable == []


def test_a_live_source_without_constraint_introspection_is_unverifiable() -> None:
    """MatchingLiveSource has get_dataset but no get_constraints."""
    graph = _SchemaOnlyGraph(SchemaField("cust_id", "int", False))

    class NoIntrospection:
        def get_dataset(self, urn: str) -> DatasetInfo | None:
            return graph.dataset

    service = SidqService(graph, live_source=NoIntrospection(), clock=lambda: FIXED_NOW)

    findings, unverifiable = service._constraint_evidence(CUSTOMERS, graph.dataset)

    assert findings == []
    assert [item["check"] for item in unverifiable] == ["constraint_reconciliation"]


def test_failing_constraint_introspection_is_unverifiable_never_a_silent_pass() -> None:
    graph = _SchemaOnlyGraph(SchemaField("cust_id", "int", False))

    class ExplodingSource:
        def get_dataset(self, urn: str) -> DatasetInfo | None:
            return graph.dataset

        def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
            raise RuntimeError("connection reset")

    service = SidqService(graph, live_source=ExplodingSource(), clock=lambda: FIXED_NOW)

    findings, unverifiable = service._constraint_evidence(CUSTOMERS, graph.dataset)

    assert findings == []
    assert unverifiable[0]["check"] == "constraint_reconciliation"
    assert "RuntimeError" in unverifiable[0]["reason"]


def test_verify_context_surfaces_a_constraint_contradiction_over_mcp() -> None:
    """The finding must reach a real MCP caller, not just the service method."""
    graph = _SchemaOnlyGraph(
        SchemaField("cust_id", "int", False),
        SchemaField("shipped_at", "timestamp", False),
    )
    source = _ConstraintSource(graph.dataset, _not_null("cust_id"))
    service = SidqService(graph, live_source=source, clock=lambda: FIXED_NOW)

    payload = _assert_canonical(_call(service, "verify_context", {"urn": CUSTOMERS}))

    assert payload["truthful"] is False
    kinds = {item["kind"] for item in payload["findings"]}
    assert "constraint_contradicts_catalog" in kinds
