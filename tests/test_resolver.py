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


def test_manifest_wins_over_explicit_map_and_naming_convention(tmp_path) -> None:
    sql_file = tmp_path / "models" / "analytics" / "orders.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT id FROM raw.orders", encoding="utf-8")
    manifest_path = tmp_path / "target" / "manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "metadata": {"adapter_type": "postgres"},
                "nodes": {
                    "model.demo.orders": {
                        "original_file_path": "models/analytics/orders.sql",
                        "relation_name": "warehouse.analytics.orders",
                        "columns": {"id": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assets_file = tmp_path / ".sidq" / "assets.yml"
    assets_file.parent.mkdir()
    assets_file.write_text(
        "assets:\n  models/analytics/orders.sql: "
        "urn:li:dataset:(urn:li:dataPlatform:redshift,explicit.orders,PROD)\n",
        encoding="utf-8",
    )

    result = resolve_changed_files(
        ["models/analytics/orders.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    asset = result.touched_assets[0]
    assert asset.resolution_strategy == "dbt_manifest"
    assert asset.urn == (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.analytics.orders,PROD)"
    )


def test_explicit_map_wins_over_naming_convention_when_no_manifest_matches(
    tmp_path,
) -> None:
    sql_file = tmp_path / "models" / "finance" / "payments.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT payment_id FROM raw.payments", encoding="utf-8")
    assets_file = tmp_path / ".sidq" / "assets.yml"
    assets_file.parent.mkdir()
    assets_file.write_text(
        "assets:\n  models/finance/payments.sql: "
        "urn:li:dataset:(urn:li:dataPlatform:redshift,explicit.payments,PROD)\n",
        encoding="utf-8",
    )

    result = resolve_changed_files(
        ["models/finance/payments.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    asset = result.touched_assets[0]
    assert asset.resolution_strategy == "explicit_map"
    assert asset.urn == (
        "urn:li:dataset:(urn:li:dataPlatform:redshift,explicit.payments,PROD)"
    )


def test_naming_convention_that_does_not_match_the_path_is_unresolved(tmp_path) -> None:
    sql_file = tmp_path / "seeds" / "reference.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT 1", encoding="utf-8")

    result = resolve_changed_files(
        ["seeds/reference.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),  # expects models/**
    )

    assert result.touched_assets == ()
    assert [item.kind for item in result.evidence] == ["unresolved_asset"]


def test_naming_convention_can_silently_collapse_two_files_onto_one_urn(
    tmp_path,
) -> None:
    """Undocumented behaviour: nothing in the resolver detects or reports that two
    distinct source files under different directories -- the exact "same basename,
    different directory" shape this module has to handle -- resolved to the
    identical dataset URN. Both become their own TouchedAsset sharing one URN, with
    zero ambiguity evidence. There is no documented tie-break, because there is no
    tie-break: the collision is invisible."""
    finance = tmp_path / "models" / "finance" / "orders.sql"
    sales = tmp_path / "models" / "sales" / "orders.sql"
    for path in (finance, sales):
        path.parent.mkdir(parents=True)
        path.write_text("SELECT id FROM raw.t", encoding="utf-8")
    convention = NamingConvention(
        platform="snowflake",
        path_pattern="models/{schema}/{table}.sql",
        relation_template="{table}",  # drops the schema captured just above
    )

    result = resolve_changed_files(
        ["models/finance/orders.sql", "models/sales/orders.sql"],
        repo_root=tmp_path,
        naming_convention=convention,
    )

    assert result.evidence == ()
    assert len(result.touched_assets) == 2
    assert {asset.urn for asset in result.touched_assets} == {
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,orders,PROD)"
    }


def test_manifest_lookup_is_case_sensitive_though_filesystems_often_are_not(
    tmp_path,
) -> None:
    sql_file = tmp_path / "Models" / "Orders.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT id FROM raw.orders", encoding="utf-8")
    manifest_path = tmp_path / "target" / "manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "metadata": {"adapter_type": "postgres"},
                "nodes": {
                    "model.demo.orders": {
                        "original_file_path": "models/orders.sql",  # lower-case
                        "relation_name": "warehouse.analytics.orders",
                        "columns": {"id": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # A checkout that differs only by case from the manifest's recorded path
    # (e.g. checked out on a case-insensitive filesystem) never matches: DataHub
    # URNs are case-sensitive, so this fails closed to "unresolved" rather than
    # guessing which file the manifest node meant.
    result = resolve_changed_files(["Models/Orders.sql"], repo_root=tmp_path)

    assert result.touched_assets == ()
    assert [item.kind for item in result.evidence] == ["unresolved_asset"]


def test_column_diff_against_the_manifest_contract_finds_added_and_removed(
    tmp_path,
) -> None:
    sql_file = tmp_path / "models" / "analytics" / "orders.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text(
        "WITH recent AS (SELECT customer_id, region FROM raw.orders) "
        "SELECT customer_id, region FROM recent",
        encoding="utf-8",
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
                        "relation_name": "warehouse.analytics.orders",
                        "columns": {"customer_id": {}, "total": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = resolve_changed_files(["models/analytics/orders.sql"], repo_root=tmp_path)

    asset = result.touched_assets[0]
    assert asset.added_fields == ("region",)
    assert asset.removed_fields == ("total",)


def test_referenced_fields_span_a_join_with_aliased_tables(tmp_path) -> None:
    sql_file = tmp_path / "models" / "analytics" / "report.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text(
        "SELECT o.id, c.email FROM raw.orders AS o "
        "JOIN raw.customers AS c ON o.customer_id = c.id",
        encoding="utf-8",
    )

    result = resolve_changed_files(
        ["models/analytics/report.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    referenced = {
        (ref.dataset_urn, ref.field_path)
        for ref in result.touched_assets[0].referenced_fields
    }
    orders_urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.orders,PROD)"
    customers_urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.customers,PROD)"
    assert (orders_urn, "id") in referenced
    assert (customers_urn, "email") in referenced


def test_column_from_a_derived_subquery_is_attributed_to_its_real_table(
    tmp_path,
) -> None:
    sql_file = tmp_path / "models" / "analytics" / "report.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text(
        "SELECT o.id, c.email FROM raw.orders AS o "
        "JOIN (SELECT id, email FROM raw.customers) AS c ON o.customer_id = c.id",
        encoding="utf-8",
    )

    result = resolve_changed_files(
        ["models/analytics/report.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    referenced = {
        (ref.dataset_urn, ref.field_path)
        for ref in result.touched_assets[0].referenced_fields
    }
    customers_urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.customers,PROD)"
    assert (customers_urn, "email") in referenced


def test_column_referenced_through_a_cte_alias_resolves_to_its_source_table(
    tmp_path,
) -> None:
    sql_file = tmp_path / "models" / "analytics" / "report.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text(
        "WITH recent AS (SELECT id, email FROM raw.customers) "
        "SELECT o.id, r.email FROM raw.orders AS o "
        "JOIN recent AS r ON o.customer_id = r.id",
        encoding="utf-8",
    )

    result = resolve_changed_files(
        ["models/analytics/report.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    referenced = {
        (ref.dataset_urn, ref.field_path)
        for ref in result.touched_assets[0].referenced_fields
    }
    customers_urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.customers,PROD)"
    assert (customers_urn, "email") in referenced


def test_select_star_projection_is_not_recorded_as_an_added_field(tmp_path) -> None:
    sql_file = tmp_path / "models" / "analytics" / "passthrough.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELECT * FROM raw.orders", encoding="utf-8")

    result = resolve_changed_files(
        ["models/analytics/passthrough.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    asset = result.touched_assets[0]
    assert asset.added_fields == ()
    assert asset.referenced_fields == ()


def test_quoted_identifiers_are_still_captured_as_referenced_fields(tmp_path) -> None:
    sql_file = tmp_path / "models" / "analytics" / "quoted.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text('SELECT "Customer Id" FROM "Raw"."Orders"', encoding="utf-8")

    result = resolve_changed_files(
        ["models/analytics/quoted.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    asset = result.touched_assets[0]
    assert {ref.field_path for ref in asset.referenced_fields} == {"Customer Id"}
    assert asset.added_fields == ("Customer Id",)


def test_unparseable_sql_yields_the_unparseable_signal_not_an_empty_diff(
    tmp_path,
) -> None:
    sql_file = tmp_path / "models" / "analytics" / "broken.sql"
    sql_file.parent.mkdir(parents=True)
    sql_file.write_text("SELEC FROM WHERE (((", encoding="utf-8")

    result = resolve_changed_files(
        ["models/analytics/broken.sql"],
        repo_root=tmp_path,
        naming_convention=NamingConvention(platform="snowflake"),
    )

    assert [item.kind for item in result.evidence] == ["unparseable_sql"]
    asset = result.touched_assets[0]
    # An unparseable file must never report an empty diff on its own: that would
    # read as "nothing changed" instead of "we could not check." The evidence
    # above is what actually carries the "we could not check" signal.
    assert asset.added_fields == ()
    assert asset.referenced_fields == ()


def test_parent_directory_traversal_is_confined_inside_the_repo_root(tmp_path) -> None:
    outside_secret = tmp_path.parent / "secret.sql"
    outside_secret.write_text("SELECT ssn FROM raw.secrets", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = resolve_changed_files(["../secret.sql"], repo_root=repo_root)

    # It never becomes a TouchedAsset pointing outside repo_root: the traversal
    # segments are stripped, so the lookup is confined to the repo (here it just
    # cannot find a match and reports unresolved, never a read of the outside file).
    assert result.touched_assets == ()
    assert [item.subject for item in result.evidence] == ["secret.sql"]


def test_absolute_path_cannot_escape_the_repo_root(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.sql"
    secret.write_text("SELECT ssn, salary FROM raw.secrets", encoding="utf-8")
    assets_dir = repo_root / ".sidq"
    assets_dir.mkdir()
    assets_dir.joinpath("assets.yml").write_text(
        f"assets:\n  {secret}: "
        "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.secrets,PROD)\n",
        encoding="utf-8",
    )

    result = resolve_changed_files([str(secret)], repo_root=repo_root)

    assert result.touched_assets == ()
    assert [item.kind for item in result.evidence] == ["unresolved_asset"]
