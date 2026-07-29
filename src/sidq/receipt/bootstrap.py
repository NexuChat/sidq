"""Create the small, typed metadata vocabulary used by Sidq receipts."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

PROPERTY_PREFIX = "urn:li:structuredProperty:sidq."
TAG_URNS = ("urn:li:tag:sidq:verified", "urn:li:tag:sidq:blocked")
ENTITY_TYPES = ("dataset",)


@dataclass(frozen=True, slots=True)
class PropertyDefinition:
    """The declarative schema for one queryable receipt field."""

    name: str
    description: str
    multiple: bool = False
    allowed_values: tuple[str, ...] = ()

    @property
    def urn(self) -> str:
        return f"{PROPERTY_PREFIX}{self.name}"


PROPERTY_DEFINITIONS = (
    PropertyDefinition(
        "verdict",
        "Sidq verification verdict.",
        allowed_values=("PASS", "WARN", "BLOCK"),
    ),
    PropertyDefinition("reason_code", "Stable Sidq reason code; empty for PASS."),
    PropertyDefinition("commit_sha", "Exact source commit checked by Sidq."),
    PropertyDefinition(
        "checked_at", "UTC ISO-8601 timestamp at which Sidq checked the asset."
    ),
    PropertyDefinition(
        "policy_hash", "SHA-256 hash of the policy used for this receipt."
    ),
    PropertyDefinition(
        "rules_fired", "Sorted policy rule identifiers that fired.", multiple=True
    ),
    PropertyDefinition("verifier", "Sidq verifier version."),
    PropertyDefinition(
        "evidence_url", "URL or DataHub document URN containing full receipt evidence."
    ),
)


def property_urn(name: str) -> str:
    """Return a Sidq property URN, rejecting writes outside Sidq's namespace."""

    if name not in {definition.name for definition in PROPERTY_DEFINITIONS}:
        raise ValueError(f"unknown Sidq structured property: {name}")
    return f"{PROPERTY_PREFIX}{name}"


def ensure_sidq_properties(
    graph: Any | None = None, *, gms_url: str | None = None
) -> dict[str, tuple[str, ...]]:
    """Idempotently create receipt definitions and the two Sidq-owned tags.

    Definitions cannot be created by the DataHub MCP mutation surface, so this
    bootstrap uses the supported DataHub SDK only for definitions/tags. Receipt
    values themselves are always written through the official MCP tools.
    """

    owns_graph = graph is None
    if graph is None:
        from datahub.ingestion.graph.client import DataHubGraph
        from datahub.ingestion.graph.config import DatahubClientConfig

        graph = DataHubGraph(
            DatahubClientConfig(
                server=gms_url
                or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
            )
        )

    created: list[str] = []
    existing: list[str] = []
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            PropertyCardinalityClass,
            PropertyValueClass,
            StructuredPropertyDefinitionClass,
            TagPropertiesClass,
        )
        from datahub.metadata.urns import Urn

        for definition in PROPERTY_DEFINITIONS:
            current = graph.get_aspect(
                definition.urn, StructuredPropertyDefinitionClass
            )
            if current is not None:
                existing.append(definition.urn)
                continue
            aspect = StructuredPropertyDefinitionClass(
                qualifiedName=f"sidq.{definition.name}",
                valueType=Urn.make_data_type_urn("string"),
                entityTypes=[
                    Urn.make_entity_type_urn(entity_type)
                    for entity_type in ENTITY_TYPES
                ],
                displayName=f"Sidq {definition.name.replace('_', ' ').title()}",
                description=definition.description,
                cardinality=(
                    PropertyCardinalityClass.MULTIPLE
                    if definition.multiple
                    else PropertyCardinalityClass.SINGLE
                ),
                allowedValues=[
                    PropertyValueClass(value=value)
                    for value in definition.allowed_values
                ]
                or None,
                immutable=False,
            )
            graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=definition.urn, aspect=aspect)
            )
            created.append(definition.urn)

        for tag_urn in TAG_URNS:
            current = graph.get_aspect(tag_urn, TagPropertiesClass)
            if current is not None:
                existing.append(tag_urn)
                continue
            tag_name = tag_urn.rsplit(":", 1)[-1]
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=tag_urn,
                    aspect=TagPropertiesClass(
                        name=tag_name,
                        description=f"Sidq receipt badge: {tag_name}.",
                    ),
                )
            )
            created.append(tag_urn)
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()
    return {"created": tuple(created), "existing": tuple(existing)}


def definitions() -> Iterable[PropertyDefinition]:
    """Expose the fixed receipt schema without a mutable module global."""

    return PROPERTY_DEFINITIONS
