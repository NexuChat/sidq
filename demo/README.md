# sidq demo environment

This directory creates the controlled live source for Gate 0 and the golden
video scene. It is deliberately separate from DataHub's metadata-only sample
assets: this PostgreSQL database is real, queryable, and safe to alter.

## Prerequisites

- Docker with Compose
- A healthy DataHub OSS quickstart on `http://localhost:8080`
- The quickstart's external Docker network named `datahub_network`

The PostgreSQL 16 container publishes host port `55432` and also joins
`datahub_network` as `sidq-demo-postgres`. Data is stored in one named
Docker volume and initialized from `seed.sql`.

## One-line demo targets

Run these commands from the repository root:

```console
make demo-up
make demo-ingest
make demo-break
make demo-restore
make demo-down
```

- `demo-up` starts PostgreSQL, waits for it to become healthy, and initializes
  36 customers, 72 orders, 144 order items, and the
  `analytics.customer_revenue` aggregate view.
- `demo-ingest` runs the matching DataHub `v1.5.0.6` ingestion image against
  `demo/ingest.dhub.yaml`. It ingests the two schemas and enables PostgreSQL
  view lineage and view column lineage.
- `demo-break` renames `raw.customers.email` to `email_address` in PostgreSQL
  without re-ingesting DataHub.
- `demo-restore` renames the live column back to `email`.
- `demo-down` removes the container, Compose network, and named volume, making
  the next `demo-up` a clean seed.

The golden scene is the interval after `demo-break` and before
`demo-restore`: the live `information_schema` contains `email_address`, while
DataHub's last ingested schema still contains `email`. Gate 0 must emit
`catalog_reality_mismatch`, and policy maps that evidence to
`BLOCK / STALE_CONTEXT`.

## dbt resolver fixture

dbt is not installed in this workspace, so this demo takes the fixture route.
`dbt/models/customer_revenue.sql` is the plain model SQL, while
`dbt/manifest.json` is a small dbt-compatible manifest fixture. Its model node
maps `models/customer_revenue.sql` directly to:

```text
urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.analytics.customer_revenue,PROD)
```

The fixture exists for sidq's manifest-first resolver; the PostgreSQL
source recipe remains the authority that creates the catalog assets and
lineage used in the live demo.
