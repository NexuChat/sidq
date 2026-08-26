"""What a previous run could not establish, and where the next run's budget goes.

`recall` reads back the receipts a run earned and skips those assets. Coverage
gaps are the other half of that memory: the `ERROR` assertions left on assets a
run examined and could establish nothing on. Until they were read back, an asset
that had resisted ten times came back the eleventh with exactly the same priority
as one no run had ever touched.

The constraint this has to respect is the one that is easy to get wrong: a
standing gap must never lower an asset's consequence. Resisting a check is not
evidence of being harmless.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sidq.agent.auditor import Target
from sidq.agent.memory import standing_gaps
from sidq.receipt.assertion import _GAP_RULE_ID, assertion_urn

PII = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.customers_pii,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.orders,PROD)"
COUNTRY = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.lookup_country,DEV)"


class _Catalog:
    """A catalog that has already refused to establish anything on two assets."""

    def __init__(self, resisted: set[str]) -> None:
        self.resisted = resisted
        self.calls = 0

    def graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        data: dict[str, object] = {}
        for index, urn in enumerate((PII, ORDERS, COUNTRY)):
            if assertion_urn(urn, _GAP_RULE_ID) not in query:
                continue
            outcome = "ERROR" if urn in self.resisted else None
            data[f"a{index}"] = {
                "runEvents": {
                    "runEvents": [{"result": {"type": outcome}}] if outcome else []
                }
            }
        return data


def _show(title: str, targets: list[Target]) -> None:
    print(title)
    order = sorted(targets, key=lambda t: (-t.consequence, t.resisted, t.urn))
    for position, target in enumerate(order, start=1):
        name = target.urn.split(",")[1]
        mark = "  ← a previous run established nothing here" if target.resisted else ""
        print(f"    {position}. {name:<28} consequence {target.consequence:>3}{mark}")
    print()


def main() -> int:
    catalog = _Catalog({PII, ORDERS})
    gaps = standing_gaps([PII, ORDERS, COUNTRY], catalog)
    print("Read back from the catalog, from addresses derived rather than stored:")
    print(f"    {len(gaps)} standing gap(s), in {catalog.calls} query\n")

    _show(
        "Without the scars — every asset looks equally untried:",
        [
            Target(PII, 90, ()),
            Target(ORDERS, 50, ()),
            Target(COUNTRY, 50, ()),
        ],
    )
    _show(
        "With them — consequence still decides, and the gap only breaks ties:",
        [
            Target(PII, 90, (), resisted=PII in gaps),
            Target(ORDERS, 50, (), resisted=ORDERS in gaps),
            Target(COUNTRY, 50, (), resisted=COUNTRY in gaps),
        ],
    )
    print("The PII table resisted a previous run and is still examined first: it is")
    print("the most consequential asset in the catalog, and having been hard to")
    print("establish is not evidence of being harmless. What the scar settles is the")
    print("tie between the two assets at 50 — the budget goes to the one no run has")
    print("ever established anything on, because that is where it buys something new.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
