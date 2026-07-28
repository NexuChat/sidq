# Reproducing the local sidq/DataHub environment

This is the exact sequence for recreating the environment reconnoitred on
2026-07-28. Run it from a fresh checkout of this repository. The quickstart and
datapack downloads require access to GitHub and the container registries.

## Tested versions

```text
Python 3.12.3
Docker version 29.4.3, build 055a478
Docker Compose version v5.1.3
acryl-datahub==1.6.0.16
mcp-server-datahub==0.6.0
mcp==1.29.0
pytest==9.1.1
ruff==0.16.0
sqlglot==30.14.0
PyYAML==6.0.3
```

The DataHub CLI warns that Python versions above 3.11 are not actively tested;
Python 3.12.3 is nevertheless the version used by this surviving workspace.

The CLI and quickstart images intentionally have different versions. With
`acryl-datahub==1.6.0.16`, the quickstart's `default` mapping was
`v1.5.0.6`; the commands below pin that image version explicitly.

## 1. Python environment

```bash
cd /home/dev/sidq

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade \
  pip==26.1.2 \
  setuptools==81.0.0
.venv/bin/python -m pip install \
  --editable '.[dev]' \
  acryl-datahub==1.6.0.16 \
  mcp-server-datahub==0.6.0

.venv/bin/python -m pip list --format=freeze |
  grep -E '^(acryl-datahub|mcp|mcp-server-datahub|pytest|ruff|sqlglot|PyYAML)=='
```

Observed version output:

```text
acryl-datahub==1.6.0.16
mcp==1.29.0
mcp-server-datahub==0.6.0
pytest==9.1.1
PyYAML==6.0.3
ruff==0.16.0
sqlglot==30.14.0
```

## 2. DataHub OSS quickstart

```bash
DATAHUB_TELEMETRY_ENABLED=false \
  .venv/bin/datahub docker quickstart --version v1.5.0.6

curl --fail --silent --show-error http://localhost:8080/health
curl --fail --silent --show-error --output /dev/null http://localhost:9002

.venv/bin/datahub init \
  --host http://localhost:8080 \
  --username datahub \
  --password datahub \
  --force
```

The running image set resolves to:

```text
gms=acryldata/datahub-gms:v1.5.0.6
frontend=acryldata/datahub-frontend-react:v1.5.0.6
actions=acryldata/datahub-actions:v1.5.0.6-slim
mysql=mysql:8.2
kafka=confluentinc/cp-kafka:8.0.0
opensearch=opensearchproject/opensearch:2.19.3
```

Current health proof:

```text
$ curl -sS -o /dev/null -w 'GMS %{http_code}\n' http://localhost:8080/health
GMS 200
$ curl -sS -o /dev/null -w 'Frontend %{http_code}\n' http://localhost:9002
Frontend 200
```

## 3. Showcase ecommerce datapack

Load the experimental datapack once:

```bash
DATAHUB_TELEMETRY_ENABLED=false \
  .venv/bin/datahub datapack load showcase-ecommerce
```

Do not substitute the classic `datahub docker ingest-sample-data` command: the
demo needs the showcase pack's cross-platform lineage.

The installed `acryl-datahub==1.6.0.16` wheel is missing
`datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md`, so the group-level
`datahub datapack --help` fails. The actual
`datahub datapack load showcase-ecommerce` command is valid (and
`datahub datapack load --help` works). Registry access can be slow; wait for the
load to complete rather than starting a second load.

Verify the load record:

```bash
sed -n '1,120p' \
  "$HOME/.datahub/datapack-loads/showcase-ecommerce.json"
```

Observed completed record:

```json
{
  "pack_name": "showcase-ecommerce",
  "run_id": "datapack-showcase-ecommerce-1785267206656",
  "loaded_at": "2026-07-28T19:33:30.730443+00:00",
  "pack_url": "https://raw.githubusercontent.com/datahub-project/static-assets/main/datapacks/showcase-ecommerce/index.json",
  "pack_sha256": null
}
```

Then run the MCP-only smoke test. It starts `mcp-server-datahub` over stdio,
connects a real MCP client, searches, reads schema, and reads downstream
column lineage:

```bash
DATAHUB_GMS_URL=http://localhost:8080 \
  .venv/bin/python scripts/smoke_mcp.py
```

The verified run exited `0` and began:

```text
Connected: server=datahub version=3.4.5 gms=http://localhost:8080

=== MCP search ===
{
  "query": "b2fd91.order_entry_db.order_entry.customers",
  "selected": {
    "properties": {
      "name": "CUSTOMERS"
    },
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)"
  },
  "total": 52
}
```

It subsequently reported `22` schema fields and `12` downstream lineage
results for `cust_email`; the complete real output is in `docs/RECON.md`.

For an MCP server process that advertises writes, set the opt-in flag in the
server's environment:

```bash
DATAHUB_GMS_URL=http://localhost:8080 \
TOOLS_IS_MUTATION_ENABLED=true \
DATAHUB_TELEMETRY_ENABLED=false \
  .venv/bin/mcp-server-datahub
```

Without `TOOLS_IS_MUTATION_ENABLED=true`, only the eight read/document tools
are exposed. With it, the twelve mutation tools listed in `docs/RECON.md` are
also exposed.

## 4. Live Gate 0 source

The showcase datapack is metadata-only, so the repository contains a separate,
controlled PostgreSQL source. Start and ingest it:

```bash
make demo-up
make demo-ingest
```

This starts `postgres:16-alpine` as `sidq-demo-postgres`, publishes it on
`localhost:55432`, seeds 36 customers, 72 orders, and 144 order items, and
ingests its tables/view using
`acryldata/datahub-ingestion:v1.5.0.6`.

Verify the live source:

```bash
docker compose -f demo/docker-compose.yml ps
docker compose -f demo/docker-compose.yml exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U sidq -d warehouse \
  -c "SELECT (SELECT count(*) FROM raw.customers) AS customers,
             (SELECT count(*) FROM raw.orders) AS orders,
             (SELECT count(*) FROM raw.order_items) AS order_items;"
```

Observed:

```text
NAME                       IMAGE                STATUS
sidq-demo-postgres   postgres:16-alpine   Up (healthy)

 customers | orders | order_items
-----------+--------+-------------
        36 |     72 |         144
```

The controlled drift/restore cycle is:

```bash
make demo-break
# Run sidq here: live PostgreSQL has email_address while DataHub has email.
make demo-restore
```

To destroy only the disposable demo database and its named volume:

```bash
make demo-down
```

## 5. Project verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```
