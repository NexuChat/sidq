# Constraint reconciliation: the catalog's blind spot

This is a real, read-only comparison between the seeded PostgreSQL source and the DataHub schema already ingested from it. It needs no model: PostgreSQL supplies the authoritative constraint DDL through `pg_catalog`, while DataHub supplies the catalog-side claims.

Run it after `make demo-up` and `make demo-ingest`:

```bash
.venv/bin/python examples/04-constraint-reconciliation/run.py
```

The script reads `raw.orders` from `sidq-demo-postgres` and the corresponding DataHub asset, then writes `findings.json`. DataHub currently knows the `NOT NULL` flags, primary-key field, and foreign-key target. It does not carry either `CHECK` constraint, so the captured output has two informational `constraint_missing_in_catalog` findings:

- `order_total` — the database's raw DDL is `CHECK (order_total >= 0::numeric)`; the catalog has no `expression` claim.
- `status` — the database's raw DDL is `CHECK (status = ANY (ARRAY[...]))`; PostgreSQL's canonical form is conservatively recognized as the accepted values `pending`, `paid`, `fulfilled`, and `refunded`, while the catalog has no `accepted_values` claim.

Each finding puts `raw_ddl`, the derived database claim, and the actual catalog claims beside one another. The script never mutates PostgreSQL or DataHub. `constraint_contradicts_catalog` is a policy warning; missing and unparseable constraints are informational. An unsupported `CHECK` is never guessed: it becomes `constraint_unparseable` with its raw DDL.
