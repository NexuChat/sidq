from __future__ import annotations

import json

from sidq.resolver import NamingConvention, resolve_changed_files


def test_dbt_manifest_is_the_first_resolution_strategy(tmp_path) -> None:
    sql_file = tmp_path / "models" / "analytics" / "orders.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text(
        "SELECT customer_id, total FROM raw.customers", encoding="utf-8"
    )
    manifest_path = tmp_path / "target" / "manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "metadata": {"adapter_type": "postgres"},
                "nodes": {
                    "model.demo.orders": {
                        "original_file_path": "models/analytics/orders.sql",
                        "relation_name": '"warehouse"."analytics"."orders"',
                        "columns": {"customer_id": {}, "total": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = resolve_changed_files(["models/analytics/orders.sql"], repo_root=tmp_path)

    assert result.evidence == ()
    assert len(result.touched_assets) == 1
    asset = result.touched_assets[0]
    assert asset.resolution_strategy == "dbt_manifest"
    assert (
        asset.urn
        == "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.analytics.orders,PROD)"
    )
    assert asset.added_fields == ()
    assert asset.removed_fields == ()
    assert {reference.field_path for reference in asset.referenced_fields} == {
        "customer_id",
        "total",
    }


def test_naming_convention_resolves_a_non_dbt_file(tmp_path) -> None:
    sql_file = tmp_path / "models" / "finance" / "payments.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT payment_id FROM raw.payments", encoding="utf-8")

    result = resolve_changed_files(
        ["models/finance/payments.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    assert result.evidence == ()
    assert result.touched_assets[0].resolution_strategy == "naming_convention"
    assert (
        result.touched_assets[0].urn
        == "urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.payments,PROD)"
    )


def test_explicit_map_resolves_a_non_dbt_path(tmp_path) -> None:
    assets_file = tmp_path / ".sidq" / "assets.yml"
    assets_file.parent.mkdir()
    assets_file.write_text(
        "assets:\n  mappings/customer.csv: urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customer,PROD)\n",
        encoding="utf-8",
    )

    result = resolve_changed_files(["mappings/customer.csv"], repo_root=tmp_path)

    assert result.evidence == ()
    assert result.touched_assets[0].resolution_strategy == "explicit_map"
    assert (
        result.touched_assets[0].urn
        == "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customer,PROD)"
    )


def test_unresolvable_file_produces_evidence_without_raising(tmp_path) -> None:
    result = resolve_changed_files(["notes/plan.txt"], repo_root=tmp_path)

    assert result.touched_assets == ()
    assert [(item.kind, item.subject) for item in result.evidence] == [
        ("unresolved_asset", "notes/plan.txt")
    ]
