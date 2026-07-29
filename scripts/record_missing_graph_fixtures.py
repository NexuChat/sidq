#!/usr/bin/env python3
"""Record the graph fixtures the flagship example needs but does not have.

`tests/fixtures/graph` held the eleven downstream urns for
`b2fd91…customers.cust_email` but not the downstream entities themselves, so
`ReplayGraphClient.get_dataset` raised on twelve consumers. The blast gate keeps
the proven lineage when that happens, which means `cross_team_owners` — and with
it the `critical_downstream` block — could not be reproduced offline. The golden
regression in `tests/test_golden_examples.py` had to be scoped around the hole.

This reads only what is missing, from the live DataHub GraphQL endpoint, and
writes it in the exact shape `ReplayGraphClient` decodes. It is additive: an
existing fixture is never rewritten unless `--force` is given, so a recording run
cannot silently change a snapshot other tests already depend on.

Usage:
    scripts/record_missing_graph_fixtures.py --dry-run
    scripts/record_missing_graph_fixtures.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sidq.graph.fixtures import _key

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "graph"
DEFAULT_ENDPOINT = "http://localhost:8080/api/graphql"

# The twelve consumers the flagship example's blast radius reaches and the replay
# set could not answer for. Kept explicit rather than crawled: a recording script
# that discovers its own scope can quietly grow the snapshot.
MISSING = (
    "urn:li:dashboard:(looker,b2fd91.dashboards.53)",
    "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry-looker.view.order_details,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Customer_Analytics_Measures,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Essential_KPI_Measures,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Geographic_Measures,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.ORDER_DETAILS,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Product_Perfromance_Measures,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.Time_Inteligence_Measures,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details_replica,PROD)",
)

_QUERY = """
query Entity($urn: String!) {
  entity(urn: $urn) {
    urn
    type
    ... on Dataset {
      deprecation { deprecated }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
      globalTags { tags { tag { urn } } }
      glossaryTerms { terms { term { urn } } }
      schemaMetadata { fields { fieldPath nativeDataType nullable description } }
    }
    ... on Dashboard {
      deprecation { deprecated }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
      globalTags { tags { tag { urn } } }
      glossaryTerms { terms { term { urn } } }
    }
    ... on Chart {
      deprecation { deprecated }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
      globalTags { tags { tag { urn } } }
      glossaryTerms { terms { term { urn } } }
    }
  }
}
"""


def _post(endpoint: str, urn: str) -> dict[str, Any]:
    body = json.dumps({"query": _QUERY, "variables": {"urn": urn}}).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError(f"graphql errors for {urn}: {payload['errors']}")
    entity = (payload.get("data") or {}).get("entity")
    if entity is None:
        raise RuntimeError(f"no entity for {urn}")
    return entity


def _dataset_fixture(entity: dict[str, Any]) -> dict[str, Any]:
    """Shape one entity exactly as ``ReplayGraphClient._dataset`` decodes it."""
    schema = entity.get("schemaMetadata") or {}
    fields = []
    descriptions = {}
    for field in schema.get("fields") or []:
        path = field.get("fieldPath")
        if not path:
            continue
        fields.append(
            {
                "path": path,
                "native_type": field.get("nativeDataType") or "",
                # DataHub reports nullable=None when unknown; the narrow seam
                # treats unknown as nullable so absence never reads as a
                # NOT NULL claim the catalog did not make.
                "nullable": bool(
                    field.get("nullable") if field.get("nullable") is not None else True
                ),
            }
        )
        if field.get("description"):
            descriptions[path] = field["description"]

    owners = sorted(
        {
            owner["owner"]["urn"]
            for owner in ((entity.get("ownership") or {}).get("owners") or [])
            if (owner.get("owner") or {}).get("urn")
        }
    )
    tags = sorted(
        {
            tag["tag"]["urn"]
            for tag in ((entity.get("globalTags") or {}).get("tags") or [])
            if (tag.get("tag") or {}).get("urn")
        }
    )
    terms = sorted(
        {
            term["term"]["urn"]
            for term in ((entity.get("glossaryTerms") or {}).get("terms") or [])
            if (term.get("term") or {}).get("urn")
        }
    )
    fixture: dict[str, Any] = {
        "urn": entity["urn"],
        "fields": sorted(fields, key=lambda item: item["path"]),
        "tags": tags,
        "terms": terms,
        "owners": owners,
        "deprecated": bool((entity.get("deprecation") or {}).get("deprecated", False)),
    }
    if descriptions:
        fixture["field_descriptions"] = descriptions
    return fixture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--dry-run", action="store_true", help="report without writing anything"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite fixtures that already exist"
    )
    arguments = parser.parse_args()

    manifest_path = FIXTURE_DIR / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )

    written = skipped = failed = 0
    for urn in MISSING:
        name = _key("get_dataset", urn)
        target = FIXTURE_DIR / f"{name}.json"
        if target.exists() and not arguments.force:
            print(f"skip (exists) {urn}")
            skipped += 1
            continue
        try:
            fixture = _dataset_fixture(_post(arguments.endpoint, urn))
        except (urllib.error.URLError, RuntimeError, KeyError) as error:
            print(f"FAIL {urn}: {error}")
            failed += 1
            continue
        owners = len(fixture["owners"])
        if arguments.dry_run:
            print(f"would write {target.name}  owners={owners}  urn={urn}")
        else:
            target.write_text(
                json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest[name] = target.name
            print(f"wrote {target.name}  owners={owners}")
        written += 1

    if written and not arguments.dry_run:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"updated {manifest_path.name}")
    print(f"\nwritten={written} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
