#!/usr/bin/env sh
# Read the native assertion through DataHub's UI GraphQL surface. The token is
# deliberately environment-only so the committed example contains no secret.
set -eu

: "${DATAHUB_GMS_URL:?Set DATAHUB_GMS_URL, for example http://localhost:8080}"
: "${DATAHUB_GMS_TOKEN:?Set DATAHUB_GMS_TOKEN}"

curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer ${DATAHUB_GMS_TOKEN}" \
  --header "Content-Type: application/json" \
  --data @- \
  "${DATAHUB_GMS_URL%/}/api/graphql" <<'JSON' | python3 -m json.tool
{
  "query": "query SidqAssertionReadback($urn: String!) { dataset(urn: $urn) { assertions { total assertions { urn info { type description customAssertion { type logic } source { type } } runEvents { total failed succeeded runEvents { status timestampMillis result { type nativeResults { key value } } } } } } } }",
  "variables": {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)"
  }
}
JSON
