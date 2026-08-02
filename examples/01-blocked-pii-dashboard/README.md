# Blocked cross-team downstream change

`customers.sql` is a dbt model mapped by `demo/dbt/manifest.json` to
`urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)`.
It removes the `cust_email` projection.

`verdict.json` is the real result of running:

```sh
.venv/bin/sidq check --file demo/dbt/models/order_entry/customers.sql --json
```

against the recorded graph replay fixture from the `showcase-ecommerce` DataHub
graph. The decision remains `BLOCK`: `critical_downstream` blocks because the
recorded blast evidence contains cross-team downstream owners. The companion
`wide_blast_radius` warning records the broader 16-consumer impact. The fixture
also records column-level lineage from `cust_email` to Looker dashboard
`b2fd91.dashboards.53` and the `urn:li:tag:b2fd91.PII_Data` tag, but that tag is
sensitivity context only; this change path does not emit `pii_exposure` without
proof of a proposed route delta.
