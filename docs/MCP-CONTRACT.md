# Live MCP contract

Verified on 2026-07-28 over one stdio `ClientSession` connected to
`mcp-server-datahub` (isolated uv-tool install), with `DATAHUB_GMS_URL=http://localhost:8080`.
The responses below are trimmed from real calls against the loaded
`showcase-ecommerce` graph.

## `search`

Working arguments:

```json
{"query":"customers","filter":"entity_type = dataset","num_results":5}
```

Trimmed real response:

```json
{
  "start": 0,
  "count": 5,
  "total": 26,
  "searchResults": [
    {"entity":{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","properties":{"name":"CUSTOMERS"}}},
    {"entity":{"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","properties":{"name":"customers"}}}
  ]
}
```

Rejected arguments:

```json
{"query":"customers","entity_type":"dataset"}
```

Error: `1 validation error for call[search]`: `entity_type` is an unexpected
keyword argument. Dataset restriction is expressed through the `filter` string,
not an `entity_type` argument.

## `get_entities`

Working arguments:

```json
{"urns":["urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"]}
```

Trimmed real response:

```json
{
  "result": [
    {"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","name":"CUSTOMERS","platform":{"urn":"urn:li:dataPlatform:snowflake","name":"snowflake"}},
    {"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","name":"customers","platform":{"urn":"urn:li:dataPlatform:dbt","name":"dbt"}}
  ]
}
```

Rejected arguments:

```json
{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)"}
```

Error: `2 validation errors for call[get_entities]`: `urns` is a missing
required argument and `urn` is an unexpected keyword argument.

## `list_schema_fields`

Working arguments:

```json
{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","keywords":["cust_email"],"limit":10}
```

Trimmed real response:

```json
{
  "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)",
  "fields": [
    {
      "fieldPath": "cust_email",
      "nativeDataType": "VARCHAR(16777216)",
      "nullable": true,
      "editedGlossaryTerms": ["PII"],
      "glossaryTerms": ["Email Address"]
    }
  ]
}
```

Rejected arguments:

```json
{"dataset_urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)"}
```

Error: `2 validation errors for call[list_schema_fields]`: `urn` is a missing
required argument and `dataset_urn` is an unexpected keyword argument.

## `get_lineage`

Working table-level arguments:

```json
{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","upstream":false,"max_hops":2,"max_results":20}
```

Trimmed real response:

```json
{
  "downstreams": {
    "total": 17,
    "returned": 17,
    "searchResults": [
      {"degree":1,"entity":{"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","type":"DATASET","name":"customers"}}
    ]
  }
}
```

### Column-level lineage

Pass **`"column": "cust_email"`** with the ordinary lineage arguments; this is
the switch to column-level lineage.

```json
{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","upstream":false,"max_hops":2,"max_results":20,"column":"cust_email"}
```

Trimmed real response:

```json
{
  "metadata": {
    "queryType": "column-level-lineage",
    "groupedBy": "dataset",
    "fields": {"lineageColumns":{"semantics":{"downstream":"Columns derived from the source column"}}}
  },
  "downstreams": {
    "total": 11,
    "returned": 11,
    "searchResults": [
      {
        "degree": 1,
        "lineageColumns": ["cust_email"],
        "entity": {"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","type":"DATASET","name":"customers"}
      }
    ]
  }
}
```

`lineageColumns` is on each `downstreams.searchResults[]` item (not inside its
`entity`). The response’s `metadata.queryType` is the reliable granularity
indicator.

Rejected arguments:

```json
{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","direction":"downstream"}
```

Error: `1 validation error for call[get_lineage]`: `direction` is an unexpected
keyword argument. `get_lineage` uses the boolean `upstream` (`false` for
downstream), not `direction`.

## `get_lineage_paths_between`

Working column-level arguments:

```json
{
  "source_urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)",
  "target_urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)",
  "direction":"downstream",
  "source_column":"cust_email",
  "target_column":"cust_email"
}
```

Trimmed real response:

```json
{
  "metadata": {"queryType":"lineage-path-trace","direction":"downstream","pathType":"column-level"},
  "source": {"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","column":"cust_email"},
  "target": {"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","column":"cust_email"},
  "pathCount": 1,
  "paths": [
    {"path":[
      {"type":"SCHEMA_FIELD","fieldPath":"cust_email","parent":{"urn":"urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","type":"DATASET"}},
      {"type":"SCHEMA_FIELD","fieldPath":"cust_email","parent":{"urn":"urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)","type":"DATASET"}}
    ]}
  ]
}
```

Rejected arguments:

```json
{"urns":["urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)","urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"]}
```

Error: `3 validation errors for call[get_lineage_paths_between]`: `source_urn`
and `target_urn` are missing required arguments, and `urns` is an unexpected
keyword argument.
