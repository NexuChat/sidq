# Sidq demo environment

This directory creates the controlled live source for Gate 0 / `STALE_CONTEXT`.
It is deliberately separate from the `showcase-ecommerce` graph used for the
blast-radius and PII scenes. Do not merge the graphs or invent lineage between
them: this PostgreSQL database has one job, which is to prove that catalog
metadata can become stale relative to a live source.

The repository Compose file supplies only this controlled PostgreSQL service.
It does not start DataHub. It relies on an already-running DataHub OSS
quickstart and its external `datahub_network`; the source and catalog are not a
single Compose graph.

## Prerequisites

- Docker with Compose
- A healthy DataHub OSS quickstart on `http://localhost:8080`
- The quickstart's external Docker network named `datahub_network`

## Connections

- PostgreSQL from the host: `localhost:55432`
- PostgreSQL from `datahub_network`: `sidq-demo-postgres:5432`
- Database/user/password: `warehouse` / `sidq` / `sidq`
- DataHub GMS from the host: `http://localhost:8080`
- DataHub GMS from `datahub_network`: `http://datahub-gms-quickstart:8080`
- DataHub frontend: `http://localhost:9002`

The PostgreSQL 16 container joins both its Compose network and the external
`datahub_network`. Data is stored in a named Docker volume and initialized from
`seed.sql` when that volume is first created.

## One-line demo targets

For the complete connected journey, run these commands from the repository
root:

```console
make mcp-install
make demo-stack
make mcp-smoke
make live-loop
```

`demo-stack` requires the already-running, separate DataHub quickstart, then
starts and ingests this PostgreSQL Compose project. `mcp-smoke` initializes `sidq-mcp` and requires its
exact three-tool list, then exercises the official `mcp-server-datahub` graph
dependency against DataHub. `live-loop` performs the complete read, decide,
write, and independent-read sequence.

The lower-level source lifecycle targets remain available:

```console
make demo-up
make demo-ingest
make demo-break
make demo-restore
make demo-down
```

- `demo-up` starts PostgreSQL and waits for its health check. A fresh volume is
  initialized with 11 raw tables: the original 36 customers, 72 orders, and
  144 order items plus categories, products, payments, shipments, refunds,
  subscriptions, web sessions, and support tickets. The original
  `analytics.customer_revenue` aggregate view remains available.
- `demo-ingest` runs the matching DataHub `v1.5.0.6` ingestion image against
  `demo/ingest.dhub.yaml` on `datahub_network`. It ingests the `raw` and
  `analytics` schemas. Run this before `demo-break`, not after it.
- `demo-break` renames `raw.customers.email` to `email_address` in PostgreSQL
  without re-ingesting DataHub. Repeated calls are harmless.
- `demo-restore` conditionally renames the live column back to `email`.
  Repeated calls are harmless.
- `demo-down` removes the container, Compose network, and named volume, making
  the next `demo-up` a clean seed.

## Ingested dataset URNs

Queried back from DataHub's graph after ingestion:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.analytics.customer_revenue,PROD)
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.customers,PROD)
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.order_items,PROD)
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.orders,PROD)
```

## Proof of divergence

Customers dataset URN:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.customers,PROD)
```

Captured after `make demo-break`, without re-ingesting:

```text
LIVE information_schema.columns                    | DATAHUB schemaMetadata
customer_id|bigint                                 | customer_id|BIGINT
email_address|text                                 | email|TEXT
full_name|text                                     | full_name|TEXT
country|text                                       | country|TEXT
created_at|timestamp with time zone                | created_at|TIMESTAMP WITH TIME ZONE
```

The left side is live database reality; the right side is the schema DataHub
last ingested. The `email_address` versus `email` mismatch proves the catalog
can be internally valid yet stale. Gate 0 must report
`catalog_reality_mismatch`, which policy maps to `BLOCK / STALE_CONTEXT`.

After the proof was captured, `make demo-restore` was run twice and the live
schema returned to:

```text
ordinal_position | column_name | data_type
1                | customer_id | bigint
2                | email       | text
3                | full_name   | text
4                | country     | text
5                | created_at  | timestamp with time zone
```

## dbt warehouse fixture

`demo/dbt` is a small but production-shaped dbt project named `sidq_demo`.
It has 18 models in three layers:

- `staging/`: one explicit-list model for each of the 11 `raw` tables.
- `intermediate/`: `int_order_enriched`, `int_customer_lifetime`, and
  `int_payment_reconciliation`.
- `marts/`: `customer_360`, `revenue_daily`, `product_performance`, and
  `order_funnel`.

All relationships are declared with dbt `source`/`ref` dependency comments,
which dbt evaluates while leaving the executable SQL as ordinary PostgreSQL.
This also lets SQLGlot parse every committed model without a Jinja rendering
step. The longest lineage paths are source → staging → intermediate → mart.

The project includes model and column descriptions, source-facing contracts,
and tests for nullability, uniqueness, controlled values, and relationships.
PII is marked with `meta: {pii: true}`. In particular,
`raw.customers.email` and `raw.customers.full_name` flow through
`stg_customers` and `int_customer_lifetime` into `customer_360`; email also
flows through `int_order_enriched` into `order_funnel`.

`dbt` is not installed in this workspace, so `dbt/manifest.json` is a
hand-authored dbt-compatible manifest generated from the committed model SQL
and schema contracts. It contains the raw and compiled code, dependencies,
and columns for all 18 model nodes. Keep it in lockstep with model changes;
Sidq's manifest-first resolver uses it as a fixture, while `seed.sql` remains
the authority for the live PostgreSQL source tables.
