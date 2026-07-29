"""Read the live demo constraints and reconcile them with its DataHub metadata.

This is a read-only proof script.  It does not run a model and does not write to
PostgreSQL or DataHub; it writes only the captured JSON artifact beside itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import psycopg
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import SchemaMetadataClass

from sidq.claims.models import Claim
from sidq.claims.reconcile import ConstraintReconciler
from sidq.graph.live_source import PostgresLiveSourceClient, _relation_from_urn

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.orders,PROD)"
ROOT = Path(__file__).resolve().parent


def main() -> None:
    graph = DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("SIDQ_DATAHUB_UI_URL", "http://localhost:8080")
        )
    )
    schema = graph.get_aspect(URN, SchemaMetadataClass)
    if schema is None:
        raise RuntimeError(f"DataHub has no schema metadata for {URN}")
    catalog_claims = _catalog_claims(schema)
    dsn = os.environ.get(
        "SIDQ_POSTGRES_DSN", "postgresql://sidq:sidq@localhost:55432/warehouse"
    )
    with psycopg.connect(dsn) as connection:
        findings = ConstraintReconciler(
            PostgresLiveSourceClient(connection)
        ).reconcile(URN, catalog_claims)

    result = {
        "asset": URN,
        "catalog_claims": [_json_claim(claim) for claim in catalog_claims],
        "findings": [asdict(finding) for finding in findings],
        "read_only": True,
    }
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    (ROOT / "findings.json").write_text(output, encoding="utf-8")
    print(output, end="")


def _catalog_claims(schema: SchemaMetadataClass) -> tuple[Claim, ...]:
    """Project only concrete DataHub schema assertions into Claim vocabulary."""
    claims: list[Claim] = []
    for field in schema.fields or []:
        if field.nullable is False:
            claims.append(
                Claim(
                    "not_null",
                    field.fieldPath,
                    source_sentence="DataHub schemaMetadata.nullable=false",
                    confidence=1.0,
                )
            )
        if field.isPartOfKey is True:
            claims.append(
                Claim(
                    "unique",
                    field.fieldPath,
                    source_sentence="DataHub schemaMetadata.isPartOfKey=true",
                    confidence=1.0,
                )
            )
    for foreign_key in schema.foreignKeys or []:
        if len(foreign_key.sourceFields) != 1 or len(foreign_key.foreignFields) != 1:
            continue
        target = _relation_from_urn(foreign_key.foreignDataset)
        if target is None:
            continue
        source_column = foreign_key.sourceFields[0].rsplit(",", 1)[-1].removesuffix(")")
        target_column = foreign_key.foreignFields[0].rsplit(",", 1)[-1].removesuffix(")")
        claims.append(
            Claim(
                "relationships",
                source_column,
                expr=".".join((*target, target_column)),
                source_sentence="DataHub schemaMetadata.foreignKeys",
                confidence=1.0,
            )
        )
    return tuple(claims)


def _json_claim(claim: Claim) -> dict:
    result = asdict(claim)
    if result["values"] is not None:
        result["values"] = list(result["values"])
    return result


if __name__ == "__main__":
    main()
