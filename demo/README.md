# Sidq demo environment

This directory creates the controlled live source for Gate 0 / `STALE_CONTEXT`.
It is deliberately separate from the `showcase-ecommerce` graph used for the
blast-radius and PII scenes. Do not merge the graphs or invent lineage between
them: this PostgreSQL database has one job, which is to prove that catalog
metadata can become stale relative to a live source.

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

Run these commands from the repository root:

```console
make demo-up
make demo-ingest
make demo-break
make demo-restore
make demo-down
```

- `demo-up` starts PostgreSQL and waits for its health check. A fresh volume is
  initialized with 36 customers, 72 orders, 144 order items, and the
  `analytics.customer_revenue` aggregate view.
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

## dbt resolver fixture

dbt is not installed in this workspace, so this demo takes the fixture route.
`dbt/models/customer_revenue.sql` is the plain model SQL, while
`dbt/manifest.json` is a small dbt-compatible manifest fixture. Its model node
maps `models/customer_revenue.sql` directly to:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.analytics.customer_revenue,PROD)
```

The fixture exists for Sidq's manifest-first resolver; the PostgreSQL
source recipe remains the authority that creates the catalog assets and
lineage used in the live demo.
