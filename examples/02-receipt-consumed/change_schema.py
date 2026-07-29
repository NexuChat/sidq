"""Change the disposable asset's schema after it has received a receipt."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
)
from prepare_asset import URN


def main() -> None:
    graph = DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("SIDQ_DATAHUB_UI_URL", "http://localhost:8080")
        )
    )
    schema = graph.get_aspect(URN, SchemaMetadataClass)
    assert schema is not None
    schema.fields.append(
        SchemaFieldClass(
            fieldPath="receipt_proof_marker",
            type=SchemaFieldDataTypeClass(type=StringTypeClass()),
            nativeDataType="varchar",
            nullable=True,
        )
    )
    schema.hash = "sidq-receipt-proof-v2"
    schema.lastModified = AuditStampClass(
        time=int(datetime.now(UTC).timestamp() * 1000), actor="urn:li:corpuser:datahub"
    )
    graph.emit_mcp(MetadataChangeProposalWrapper(entityUrn=URN, aspect=schema))
    print("schema updated: added receipt_proof_marker")


if __name__ == "__main__":
    main()
