# DataHub live recon

Recon date: 2026-07-28 (UTC)

> **COLUMN-LEVEL LINEAGE IS PRESENT.** The stored graph has 844 fine-grained field
> edges on 32 datasets, including cross-platform Snowflake → dbt/Looker/Power BI/
> Tableau paths. The demo can use real column blast-radius evidence.

All evidence below comes from commands run against the already-running local DataHub,
not from documentation claims.

## A. Datapack actually loaded

**Verdict: `showcase-ecommerce` is already loaded.** I did not reload it because the
live graph already contains the datapack's generated `b2fd91` ecommerce graph across
Postgres, Snowflake, dbt, S3, Looker, Tableau, and Power BI. This is not the classic
single-platform sample metadata.

The CLI's local load record independently confirms the completed load:

```text
$ sed -n '1,120p' ~/.datahub/datapack-loads/showcase-ecommerce.json
{
  "pack_name": "showcase-ecommerce",
  "run_id": "datapack-showcase-ecommerce-1785267206656",
  "loaded_at": "2026-07-28T19:33:30.730443+00:00",
  "pack_url": "https://raw.githubusercontent.com/datahub-project/static-assets/main/datapacks/showcase-ecommerce/index.json",
  "pack_sha256": null
}
```

Observed GraphQL `search` output (the command queried `DATASET`, `DASHBOARD`, and
`CHART`, with `query: "*"` and `count: 500`):

```text
DATASET: total=67, returned=67, platforms={'dbt': 13, 'looker': 2, 'postgres': 12, 'powerbi': 6, 's3': 12, 'snowflake': 14, 'tableau': 8}
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.addresses,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.inventories,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.order_items,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.orders,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.product_categories,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.products,PROD)
  urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.regions,PROD)
DASHBOARD: total=3, returned=3, platforms={'(n/a)': 3}
  urn:li:dashboard:(tableau,b2fd91.843bf583-900b-f1ba-0532-b5e67a0373dc)
  urn:li:dashboard:(powerbi,b2fd91.reports.66666666-7777-8888-9999-000000000000)
  urn:li:dashboard:(looker,b2fd91.dashboards.53)
CHART: total=12, returned=12, platforms={'(n/a)': 12}
  urn:li:chart:(tableau,b2fd91.89f38fd7-058d-b66a-6db0-4f85f105468a)
  urn:li:chart:(tableau,b2fd91.b8c660a8-10ea-e32a-b823-fa655e1c2f43)
  urn:li:chart:(tableau,b2fd91.e051d978-989f-a329-5458-e01721b05570)
  urn:li:chart:(tableau,b2fd91.e36d7772-ac4d-4fd0-a893-aec88f3aa13e)
  urn:li:chart:(looker,b2fd91.dashboard_elements.221)
  urn:li:chart:(looker,b2fd91.dashboard_elements.222)
  urn:li:chart:(looker,b2fd91.dashboard_elements.223)
  urn:li:chart:(looker,b2fd91.dashboard_elements.224)
```

The locally installed CLI is `acryl-datahub 1.6.0.16`. Its experimental datapack
group help has a packaging defect (the specific `load --help` command still works):

```text
FileNotFoundError: [Errno 2] No such file or directory:
'/home/dev/sidq/.venv/lib/python3.12/site-packages/datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md'
```

That defect did not prevent this recon because the datapack had already been loaded by
the previous worker.

## B. Column-level (fine-grained) lineage

**Verdict: present, extensive, and cross-platform.** I fetched each dataset's persisted
`upstreamLineage` aspect through `DataHubGraph.get_aspect(...,
UpstreamLineageClass)`. The 835 stored `fineGrainedLineages` records contain 844
upstream field references (eight transformations have multiple inputs), yielding
844 concrete upstream-field → downstream-field edge pairs:

```text
datasets_scanned=67 datasets_with_upstreams=32 datasets_with_fine_grained_lineage=32
fine_grained_records=835 upstream_field_refs=844 downstream_field_refs=835
cartesian_edge_pairs=844 multi_input_records=8
platform_transitions:
  dbt -> dbt: 60
  dbt -> snowflake: 60
  looker -> looker: 63
  snowflake -> dbt: 102
  snowflake -> looker: 57
  snowflake -> powerbi: 347
  snowflake -> snowflake: 121
  snowflake -> tableau: 17
  tableau -> tableau: 17
```

One concrete sensitive-column path, printed by a breadth-first traversal of those
persisted fine-grained edges:

```text
BFS from urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD),cust_email)
  depth=1: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD),cust_email)
  depth=1: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD),cust_email)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD),cust_email)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry-looker.view.order_details,PROD),cust_email)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Customer_Analytics_Measures,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Essential_KPI_Measures,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Geographic_Measures,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.ORDER_DETAILS,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Product_Perfromance_Measures,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Time_Inteligence_Measures,PROD),CUST_EMAIL)
  depth=2: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details_replica,PROD),cust_email)
  depth=3: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD),order_details.cust_email)
```

The terminal BI datasets feed real charts and dashboards. Direct `chartInfo.inputs`
and `dashboardInfo.charts` reads include:

```text
urn:li:chart:(looker,b2fd91.dashboard_elements.221)
  inputs= ['urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD)']
urn:li:dashboard:(looker,b2fd91.dashboards.53)
  charts= ['urn:li:chart:(looker,b2fd91.dashboard_elements.221)',
           'urn:li:chart:(looker,b2fd91.dashboard_elements.224)',
           'urn:li:chart:(looker,b2fd91.dashboard_elements.222)',
           'urn:li:chart:(looker,b2fd91.dashboard_elements.223)']
urn:li:chart:(powerbi,b2fd91.pages.66666666-7777-8888-9999-000000000000.217abe0d5c1cd421c384)
  inputs= ['urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.ORDER_DETAILS,PROD)',
           'urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Customer_Analytics_Measures,PROD)', ...]
urn:li:dashboard:(powerbi,b2fd91.reports.66666666-7777-8888-9999-000000000000)
  charts= ['urn:li:chart:(powerbi,b2fd91.pages.66666666-7777-8888-9999-000000000000.83a9aaa3207edd6c721e)',
           'urn:li:chart:(powerbi,b2fd91.pages.66666666-7777-8888-9999-000000000000.217abe0d5c1cd421c384)', ...]
```

The fine-grained path ends at a BI dataset field; the subsequent chart input and
dashboard membership hops are entity-level because DataHub's chart and dashboard
aspects do not carry schema fields.

## C. MCP server

### Advertised tools with mutations disabled

I connected a real MCP `ClientSession` over stdio to
`.venv/bin/mcp-server-datahub`, with `DATAHUB_GMS_URL=http://localhost:8080`.
This is the server's `tools/list` response, not a package README:

```text
mutations=false server=datahub version=3.4.5 tool_count=8
  search
  get_lineage
  get_dataset_queries
  get_entities
  list_schema_fields
  get_lineage_paths_between
  search_documents
  grep_documents
```

`serverInfo.version` is the embedded FastMCP server version (`3.4.5`); the installed
distribution is `mcp-server-datahub 0.6.0`. Contrary to the desk-research superset,
this OSS connection does **not** advertise `get_me` or `get_dataset_assertions`.
The server logged `User Tools DISABLED` and `Data Quality Tools DISABLED`.

### Advertised tools with mutations enabled

With only `TOOLS_IS_MUTATION_ENABLED=true` changed:

```text
mutations=true server=datahub version=3.4.5 tool_count=20
  search
  get_lineage
  get_dataset_queries
  get_entities
  list_schema_fields
  get_lineage_paths_between
  search_documents
  grep_documents
  add_tags
  remove_tags
  add_terms
  remove_terms
  add_owners
  remove_owners
  set_domains
  remove_domains
  update_description
  add_structured_properties
  remove_structured_properties
  save_document
```

Therefore all twelve mutation tools do appear when the flag is true.

### End-to-end write and read-back

I created a disposable metadata-only dataset, then performed both the mutation and
read through the MCP client. Creation proof:

```text
CREATE THROWAWAY ASSET
urn= urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.recon.throwaway,DEV)
emit_response= None
datasetProperties= OrderedDict({'customProperties': {}, 'name': 'Sidq MCP recon throwaway', 'description': 'Disposable asset for MCP write/read proof', 'tags': []})
```

MCP `add_tags` write output, using the datapack's real PII tag:

```text
MCP WRITE add_tags
{
  "isError": false,
  "content": [
    {
      "type": "text",
      "text": "{\"success\":true,\"message\":\"Successfully added 1 tag(s) to 1 entit(ies)\"}"
    }
  ]
}
```

Immediate MCP `get_entities` read-back:

```text
MCP READ get_entities
{
  "isError": false,
  "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.recon.throwaway,DEV)",
  "name": "Sidq MCP recon throwaway",
  "tags": {
    "tags": [
      {
        "tag": {
          "urn": "urn:li:tag:b2fd91.PII_Data",
          "properties": {
            "name": "PII_Data",
            "description": "Indicates datasets containing personally identifiable information requiring special handling per our privacy policies"
          }
        }
      }
    ]
  }
}
```

This proves the opt-in MCP mutation path works against this OSS GMS and the result is
readable through the MCP server, not merely acknowledged by the write call.

## D. Governance material

### PII tags

At the dataset level, exactly one of the 67 `b2fd91` sample datasets carries the
PII tag. (The MCP throwaway is intentionally excluded here.)

```text
PII_TAGGED_SAMPLE urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD) ['urn:li:tag:b2fd91.PII_Data', 'urn:li:tag:b2fd91.Authoritative Source']
sample_datasets_scanned=67 pii_tagged_datasets=1 datasets_with_terms=66 datasets_with_owners=20
```

PII is much richer at column level via the PII glossary term. The live
`editableSchemaMetadata` for the Snowflake customers dataset reported:

```text
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) customer_id ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) cust_first_name ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) cust_last_name ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) cust_email ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) dob ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) phone_number ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) address_line1 ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) address_line2 ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
PII_FIELD urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) address_line3 ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a']
```

### Glossary terms

Observed term identities:

```text
urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a  PII
urn:li:glossaryTerm:b2fd91.309dd98b-a96c-49de-93ad-acff10e963a2  GDPR
urn:li:glossaryTerm:b2fd91.e7106c45-b307-4eb6-9c8c-e7fff15f095a  SOC2 Auditable
urn:li:glossaryTerm:b2fd91.42266719-3cab-42b8-a8d2-49d782876dbc  Order Total
urn:li:glossaryTerm:b2fd91.26e268c3-3688-4281-949e-8c1aa2600c02  Revenue by Customer Class
urn:li:glossaryTerm:b2fd91.Email_Address  Email Address
urn:li:glossaryTerm:b2fd91.Phone_Number  Phone Number
```

Concrete asset associations from live `glossaryTerms` aspects:

```text
TERM_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD) ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a', 'urn:li:glossaryTerm:b2fd91.e7106c45-b307-4eb6-9c8c-e7fff15f095a', 'urn:li:glossaryTerm:b2fd91.42266719-3cab-42b8-a8d2-49d782876dbc', 'urn:li:glossaryTerm:b2fd91.26e268c3-3688-4281-949e-8c1aa2600c02']
TERM_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD) ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a', 'urn:li:glossaryTerm:b2fd91.e7106c45-b307-4eb6-9c8c-e7fff15f095a', 'urn:li:glossaryTerm:b2fd91.26e268c3-3688-4281-949e-8c1aa2600c02', 'urn:li:glossaryTerm:b2fd91.42266719-3cab-42b8-a8d2-49d782876dbc']
TERM_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD) ['urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a', 'urn:li:glossaryTerm:b2fd91.f5bb0410-d9cd-4b24-ade4-361ff6bde7ea', 'urn:li:glossaryTerm:b2fd91.309dd98b-a96c-49de-93ad-acff10e963a2', 'urn:li:glossaryTerm:b2fd91.42266719-3cab-42b8-a8d2-49d782876dbc', 'urn:li:glossaryTerm:b2fd91.26e268c3-3688-4281-949e-8c1aa2600c02']
```

### Owners

Twenty sample datasets have ownership aspects. Concrete examples:

```text
OWNER_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD) ['urn:li:corpGroup:b2fd91.1e0398a3-113f-475e-b6fc-32ab72a634d2', 'urn:li:corpuser:b2fd91.brock1@example.com', 'urn:li:corpuser:b2fd91.jonny1@example.com']
OWNER_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD) ['urn:li:corpGroup:b2fd91.ORG_DATA_PLATFORM', 'urn:li:corpuser:b2fd91.EMP006', 'urn:li:corpuser:b2fd91.jonny1@example.com']
OWNER_EXAMPLE urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD) ['urn:li:corpGroup:b2fd91.ORG_DATA_PLATFORM', 'urn:li:corpGroup:b2fd91.ORG_BACKEND_ENG', 'urn:li:corpuser:b2fd91.EMP006']
```

### Assertions

**There are no assertions in this graph.** Live GraphQL output:

```json
{
  "data": {
    "search": {
      "total": 0,
      "searchResults": []
    }
  },
  "extensions": {}
}
```

## E. Live-source reality check

**The `showcase-ecommerce` datapack is metadata-only.** None of its Postgres,
Snowflake, dbt, S3, Looker, Tableau, or Power BI names has a live source service
behind it that Gate 0 can query or alter. The MySQL container belongs to DataHub
itself: its only application database is `datahub`.

```text
$ docker ps --format '{{.Names}}\t{{.Image}}' | sort
datahub-datahub-actions-quickstart-1  acryldata/datahub-actions:v1.5.0.6-slim
datahub-datahub-gms-quickstart-1      acryldata/datahub-gms:v1.5.0.6
datahub-frontend-quickstart-1         acryldata/datahub-frontend-react:v1.5.0.6
datahub-kafka-broker-1                confluentinc/cp-kafka:8.0.0
datahub-mysql-1                       mysql:8.2
datahub-opensearch-1                  opensearchproject/opensearch:2.19.3
sidq-demo-postgres              postgres:16-alpine

$ docker exec datahub-mysql-1 sh -lc \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SHOW DATABASES;"'
datahub
information_schema
mysql
performance_schema
sys
```

Gate 0 therefore needs its own seeded, ingested database. That database has
already been added under `demo/`: PostgreSQL 16 on host port `55432`, a
`warehouse` database containing three physical `raw` tables and one
`analytics.customer_revenue` view, plus a Postgres ingestion recipe with view
and view-column lineage enabled.

Live connection and row-count proof:

```text
$ docker exec sidq-demo-postgres psql -U sidq -d warehouse \
  -c "SELECT version();" \
  -c "SELECT schemaname, tablename FROM pg_tables
      WHERE schemaname IN ('raw','analytics') ORDER BY 1,2;" \
  -c "SELECT (SELECT count(*) FROM raw.customers) AS customers,
             (SELECT count(*) FROM raw.orders) AS orders,
             (SELECT count(*) FROM raw.order_items) AS order_items;"
PostgreSQL 16.14 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

 schemaname |  tablename
------------+-------------
 raw        | customers
 raw        | order_items
 raw        | orders

 customers | orders | order_items
-----------+--------+-------------
        36 |     72 |         144
```

The source is genuinely alterable. This transaction renamed the sensitive
column, observed the change through `information_schema`, and rolled it back;
the final query proves the live schema was restored:

```text
$ docker exec sidq-demo-postgres psql -v ON_ERROR_STOP=1 \
  -U sidq -d warehouse \
  -c "BEGIN;
      ALTER TABLE raw.customers RENAME COLUMN email TO email_address;
      SELECT column_name FROM information_schema.columns
      WHERE table_schema='raw' AND table_name='customers'
        AND column_name LIKE 'email%';
      ROLLBACK;" \
  -c "SELECT column_name FROM information_schema.columns
      WHERE table_schema='raw' AND table_name='customers'
        AND column_name LIKE 'email%';"
BEGIN
ALTER TABLE
  column_name
---------------
 email_address
(1 row)

ROLLBACK
 column_name
-------------
 email
(1 row)
```

From a zero checkout the exact Gate 0 addition is `make demo-up` followed by
`make demo-ingest`. The first command starts and seeds `postgres:16-alpine`;
the second runs `acryldata/datahub-ingestion:v1.5.0.6` against
`demo/ingest.dhub.yaml`. `make demo-break` performs the real rename that
creates catalog-versus-live drift, and `make demo-restore` reverses it.
