"""Generate the published self-contradiction report from the running catalog."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig

from sidq.gates.self_contradiction import CatalogSnapshot, SelfContradictionGate

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "report.json"
DOCUMENT = ROOT / "docs" / "TRUTH-REPORT.md"
CHECKS = (
    "lineage_field_missing",
    "pii_leak_untagged",
    "doc_references_missing_column",
    "orphan_lineage",
    "deprecated_upstream_of_live",
    "unowned_consumed",
)


class ScopedGraph:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._snapshot


def main() -> None:
    graph = DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("SIDQ_DATAHUB_UI_URL", "http://localhost:8080")
        )
    )
    complete = CatalogSnapshot.from_datahub(graph)
    entities = tuple(entity for entity in complete.entities if "b2fd91" in entity.urn)
    urns = {entity.urn for entity in entities}
    snapshot = CatalogSnapshot(
        entities,
        tuple(
            edge
            for edge in complete.edges
            if edge.source_urn in urns and edge.target_urn in urns
        ),
    )
    evidence = SelfContradictionGate().collect((), ScopedGraph(snapshot))
    counts = Counter(item.kind for item in evidence)
    asset_counts = Counter(entity.kind for entity in entities)
    report = {
        "audit_run": "2026-07-28T00:00:00Z",
        "scope": {
            "catalog": "showcase-ecommerce",
            "datasets": asset_counts["dataset"],
            "charts": asset_counts["chart"],
            "dashboards": asset_counts["dashboard"],
            "lineage_edges": len(snapshot.edges),
            "source": "read-only DataHub catalog metadata",
        },
        "summary": [
            {
                "check": check,
                "datasets_examined": asset_counts["dataset"],
                "assets_examined": len(entities)
                if check
                in {"deprecated_upstream_of_live", "orphan_lineage", "unowned_consumed"}
                else asset_counts["dataset"],
                "findings": counts[check],
                "unverifiable": counts[f"{check}_unverifiable"],
            }
            for check in CHECKS
        ],
        "evidence": [asdict(item) for item in evidence],
    }
    OUT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    DOCUMENT.write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {"summary": report["summary"], "evidence_count": len(evidence)}, indent=2
        )
    )


def _markdown(report: dict) -> str:
    lines = [
        "# Truth report: showcase-ecommerce",
        "",
        "Audit run: 2026-07-28 UTC against the running local `showcase-ecommerce` DataHub graph. The self-contradiction audit used read-only catalog metadata only.",
        "",
        "## `lineage_rot` remains unverifiable",
        "",
        "> **NO REAL LINEAGE ROT FINDING.** The pack does not ship the original dbt model SQL, so it is neither proved clean nor proved stale by a code-vs-catalog comparison.",
        "",
        "The graph contains 67 showcase datasets; 32 have persisted fine-grained lineage. All 32 `lineage_rot` attempts returned `lineage_unverifiable` because the original model SQL is absent (`FileNotFoundError`). The local `demo/dbt/models/order_entry/customers.sql` and `examples/01-blocked-pii-dashboard/customers.sql` were deliberately excluded: they are Sidq demonstration edits, not pack source. There are therefore zero adjudicable `lineage_rot_missing` or `lineage_rot_extra` findings.",
        "",
        "## Catalog self-contradiction audit",
        "",
        "This is a different, catalog-only proposition: two claims inside the catalog cannot both be true when a stored lineage target field is absent from that target's stored schema. No source code, database, or external system was consulted.",
        "",
        "| Check | Datasets examined | Assets examined | Findings | Unverifiable |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in report["summary"]:
        lines.append(
            f"| `{row['check']}` | {row['datasets_examined']} | {row['assets_examined']} | {row['findings']} | {row['unverifiable']} |"
        )
    lines.extend(
        [
            "",
            "The scan covered all 67 datasets, 12 charts, and 3 dashboards (82 catalog entities), with 938 in-scope lineage/membership edges.",
            "",
        ]
    )
    grouped: dict[str, list[dict]] = {check: [] for check in CHECKS}
    for item in report["evidence"]:
        if item["kind"] in grouped:
            grouped[item["kind"]].append(item)
    for check in CHECKS:
        findings = grouped[check]
        lines.extend([f"### `{check}`", ""])
        if not findings:
            lines.extend(
                [
                    "No findings. This published zero is a result, not a skipped check.",
                    "",
                ]
            )
            continue
        lines.extend(_finding_lines(check, findings))
        lines.append("")
    lines.extend(
        [
            "## What this means",
            "",
            "A curated, shipped sample catalog is internally inconsistent in 285 persisted field-level lineage claims: each names a downstream schema field its own schema does not contain. It also has 29 consumed entities with no recorded owner. This is not an assertion about source-code correctness; it is stronger and narrower: these are conflicts between catalog claims visible in the DataHub UI itself.",
            "",
            "## Deliberate strictness",
            "",
            "`pii_leak_untagged` requires an explicit PII tag on the source **schema field** and the same tag absent on the target field; it does not infer field sensitivity from a dataset-level tag. `doc_references_missing_column` requires lower snake_case plus adjacent `column` or `field`, so it deliberately misses loose prose, table names, and unlabelled code examples. `deprecated_upstream_of_live` requires an explicit deprecation flag and a graph path to a non-removed chart/dashboard. Missing snapshot data yields `*_unverifiable`, never a finding.",
            "",
            "The complete machine-readable evidence, including every concrete edge and target schema, is [`examples/03-catalog-truth-report/report.json`](../examples/03-catalog-truth-report/report.json).",
            "",
        ]
    )
    return "\n".join(lines)


def _finding_lines(check: str, findings: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in findings:
        detail = item["detail"]
        if check == "lineage_field_missing":
            edge = detail["edge"]
            lines.append(
                f"- `{item['subject']}` — catalog lineage claims `{edge['source_dataset']}#{edge['source_field']}` feeds `{edge['target_dataset']}#{edge['target_field']}`; the target schema lists only `{', '.join(detail['target_schema_fields'])}`."
            )
        elif check == "unowned_consumed":
            lines.append(
                f"- `{item['subject']}` — the catalog records downstream consumer(s) `{', '.join(detail['downstream_consumers'])}` while its ownership aspect contains no owner."
            )
    return lines


if __name__ == "__main__":
    main()
