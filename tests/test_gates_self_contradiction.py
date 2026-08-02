from __future__ import annotations

import pytest

from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
    SelfContradictionGate,
    is_pii_tag,
)

SOURCE = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.source,PROD)"
TARGET = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.target,PROD)"
ORPHAN = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.missing,PROD)"
CHART = "urn:li:chart:(looker,analytics.chart)"

NEGATED_PII_TAGS = (
    "not_pii",
    "not-pii",
    "NOTPII",
    "nonPII",
    "noPII",
    "not_personally_identifiable",
    "not-personally-identifiable",
    "NOTPERSONALLYIDENTIFIABLE",
    "nonPersonallyIdentifiable",
    "noPersonallyIdentifiable",
)


class Graph:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._snapshot


def _snapshot() -> CatalogSnapshot:
    return CatalogSnapshot(
        entities=(
            CatalogEntity(
                SOURCE,
                "dataset",
                fields=(CatalogField("email", tags=("urn:li:tag:PII",)),),
                deprecated=True,
            ),
            CatalogEntity(
                TARGET,
                "dataset",
                description="The `missing_column` field is historical; use real_column instead.",
                fields=(
                    CatalogField(
                        "real_column", description="Derived from column absent_field."
                    ),
                    CatalogField("email_copy"),
                ),
                owners=("urn:li:corpuser:owner",),
            ),
            CatalogEntity(CHART, "chart", owners=("urn:li:corpuser:owner",)),
        ),
        edges=(
            LineageEdge(SOURCE, "email", TARGET, "email_copy"),
            LineageEdge(SOURCE, "email", TARGET, "missing_target"),
            LineageEdge(ORPHAN, "id", TARGET, "real_column"),
            LineageEdge(SOURCE, None, CHART, None),
        ),
    )


def test_detects_each_catalog_self_contradiction_with_concrete_evidence() -> None:
    evidence = SelfContradictionGate().collect((), Graph(_snapshot()))

    assert [(item.kind, item.subject) for item in evidence] == [
        ("deprecated_upstream_of_live", SOURCE),
        ("doc_references_missing_column", f"{TARGET}#real_column"),
        ("doc_references_missing_column", TARGET),
        ("lineage_field_missing", f"{TARGET}#missing_target"),
        ("orphan_lineage", ORPHAN),
        ("pii_leak_untagged", f"{TARGET}#email_copy"),
        ("unowned_consumed", SOURCE),
    ]
    field_missing = next(
        item for item in evidence if item.kind == "lineage_field_missing"
    )
    assert field_missing.detail["edge"]["target_field"] == "missing_target"
    assert field_missing.detail["target_schema_fields"] == ["email_copy", "real_column"]
    leak = next(item for item in evidence if item.kind == "pii_leak_untagged")
    assert leak.detail["source_pii_tags"] == ["urn:li:tag:PII"]
    assert leak.detail["target_tags"] == []


def test_document_match_is_deliberately_strict() -> None:
    snapshot = CatalogSnapshot(
        entities=(
            CatalogEntity(
                TARGET,
                "dataset",
                description="A customer has a name. The existing customer_id is stable.",
                fields=(CatalogField("customer_id"),),
                owners=("urn:li:corpuser:owner",),
            ),
        )
    )

    evidence = SelfContradictionGate().collect((), Graph(snapshot))

    assert not [
        item for item in evidence if item.kind == "doc_references_missing_column"
    ]


def test_missing_complete_snapshot_is_unverifiable_not_a_finding() -> None:
    evidence = SelfContradictionGate().collect((), object())

    assert [item.kind for item in evidence] == [
        "deprecated_upstream_of_live_unverifiable",
        "doc_references_missing_column_unverifiable",
        "lineage_field_missing_unverifiable",
        "orphan_lineage_unverifiable",
        "pii_leak_untagged_unverifiable",
        "unowned_consumed_unverifiable",
    ]


@pytest.mark.parametrize("tag", NEGATED_PII_TAGS)
def test_a_negated_pii_source_tag_is_not_classified_as_pii(tag: str) -> None:
    snapshot = CatalogSnapshot(
        entities=(
            CatalogEntity(
                SOURCE,
                "dataset",
                fields=(CatalogField("email", tags=(f"urn:li:tag:{tag}",)),),
                owners=("urn:li:corpuser:owner",),
            ),
            CatalogEntity(
                TARGET,
                "dataset",
                fields=(CatalogField("email_copy"),),
                owners=("urn:li:corpuser:owner",),
            ),
        ),
        edges=(LineageEdge(SOURCE, "email", TARGET, "email_copy"),),
    )

    evidence = SelfContradictionGate().collect((), Graph(snapshot))

    assert not [item for item in evidence if item.kind == "pii_leak_untagged"]


@pytest.mark.parametrize("tag", (*NEGATED_PII_TAGS, "not_confidential"))
def test_a_negated_target_tag_does_not_claim_equivalent_protection(tag: str) -> None:
    snapshot = CatalogSnapshot(
        entities=(
            CatalogEntity(
                SOURCE,
                "dataset",
                fields=(CatalogField("email", tags=("urn:li:tag:PII",)),),
                owners=("urn:li:corpuser:owner",),
            ),
            CatalogEntity(
                TARGET,
                "dataset",
                fields=(CatalogField("email_copy", tags=(f"urn:li:tag:{tag}",)),),
                owners=("urn:li:corpuser:owner",),
            ),
        ),
        edges=(LineageEdge(SOURCE, "email", TARGET, "email_copy"),),
    )

    evidence = SelfContradictionGate().collect((), Graph(snapshot))

    assert [item.kind for item in evidence] == ["pii_leak_untagged"]


@pytest.mark.parametrize(
    ("source_tag", "target_tag"),
    (("unpii", ""), ("PII", "pci")),
)
def test_positive_pii_and_protection_markers_remain_supported(
    source_tag: str, target_tag: str
) -> None:
    snapshot = CatalogSnapshot(
        entities=(
            CatalogEntity(
                SOURCE,
                "dataset",
                fields=(CatalogField("email", tags=(f"urn:li:tag:{source_tag}",)),),
                owners=("urn:li:corpuser:owner",),
            ),
            CatalogEntity(
                TARGET,
                "dataset",
                fields=(
                    CatalogField(
                        "email_copy",
                        tags=(f"urn:li:tag:{target_tag}",) if target_tag else (),
                    ),
                ),
                owners=("urn:li:corpuser:owner",),
            ),
        ),
        edges=(LineageEdge(SOURCE, "email", TARGET, "email_copy"),),
    )

    evidence = SelfContradictionGate().collect((), Graph(snapshot))
    leaks = [item for item in evidence if item.kind == "pii_leak_untagged"]

    assert bool(leaks) is (target_tag == "")


@pytest.mark.parametrize("tag", ("notice_pii", "nonprofit_pii", "pii_not_confidential"))
def test_negation_like_text_after_a_positive_marker_does_not_negate_pii(
    tag: str,
) -> None:
    assert is_pii_tag(f"urn:li:tag:{tag}")
