"""Create only the disposable dataset used by the live receipt proof."""

from __future__ import annotations

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    OtherSchemaClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
)

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"


def main() -> None:
    graph = DataHubGraph(DatahubClientConfig(server="http://localhost:8080"))
    graph.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=URN,
            aspect=DatasetPropertiesClass(name="Sidq receipt consumption proof"),
        )
    )
    graph.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=URN,
            aspect=SchemaMetadataClass(
                schemaName="sidq.receipt.consumed",
                platform="urn:li:dataPlatform:postgres",
                version=0,
                hash="sidq-receipt-proof-v1",
                platformSchema=OtherSchemaClass(rawSchema="receipt proof"),
                fields=[
                    SchemaFieldClass(
                        fieldPath="id",
                        type=SchemaFieldDataTypeClass(type=StringTypeClass()),
                        nativeDataType="varchar",
                        nullable=False,
                    )
                ],
            ),
        )
    )
    print(URN)


if __name__ == "__main__":
    main()
