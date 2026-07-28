# Blocked PII dashboard change

`customers.sql` is a dbt model mapped by `demo/dbt/manifest.json` to
`urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)`.
It removes the `cust_email` projection.

`verdict.json` is the real result of running:

```sh
.venv/bin/sidq check --file demo/dbt/models/order_entry/customers.sql --json
```

against the live `showcase-ecommerce` DataHub graph. The `pii_exposure` rule
blocks the change because DataHub records column-level lineage from
`cust_email` to Looker dashboard `b2fd91.dashboards.53`; the evidence includes
the live `urn:li:tag:b2fd91.PII_Data` tag. The companion `wide_blast_radius`
finding records the broader 16-consumer impact.
