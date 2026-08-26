"""What a previous run could not establish must shape the next run's order.

`sidq.agent.memory.recall` reads back the receipts a run earned and skips those
assets, so the budget flows to the untouched tail. Coverage gaps are the other
half of that memory and nothing read them: an asset that resisted examination ten
times came back the eleventh with exactly the same priority as one no run had
ever touched, and the budget drained into it while the real tail waited.

The design constraint these tests hold is the one that is easy to get wrong.
Resisting a check is not evidence of being harmless, so a standing gap must never
lower an asset's consequence — it breaks ties, and only ties.
"""

from __future__ import annotations

from sidq.agent.auditor import Target
from sidq.agent.memory import standing_gaps
from sidq.receipt.assertion import _GAP_RULE_ID, assertion_urn

A = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.orders,PROD)"
B = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.customers,PROD)"


def _order(targets: list[Target]) -> list[str]:
    return [
        item.urn
        for item in sorted(targets, key=lambda t: (-t.consequence, t.resisted, t.urn))
    ]


def test_consequence_still_decides_even_when_the_asset_resisted() -> None:
    """A hard asset is not a harmless one; burying it would invert the point."""
    assert _order(
        [
            Target(B, 50, (), resisted=False),
            Target(A, 90, (), resisted=True),
        ]
    ) == [A, B]


def test_among_equals_the_untouched_asset_goes_first() -> None:
    """This is the whole change: the budget buys something new before it retries."""
    assert _order(
        [
            Target(A, 50, (), resisted=True),
            Target(B, 50, (), resisted=False),
        ]
    ) == [B, A]


def test_the_order_is_still_fully_deterministic() -> None:
    targets = [
        Target(A, 50, (), resisted=True),
        Target(B, 50, (), resisted=True),
    ]
    # B sorts before A on the URN, which is the final tie-break and the reason
    # two runs over the same catalog produce the same transcript.
    assert _order(targets) == _order(list(reversed(targets))) == [B, A]


class _Transport:
    def __init__(self, results: dict[str, str | None]) -> None:
        self.results = results
        self.queries = 0

    def graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        self.queries += 1
        data: dict[str, object] = {}
        for index, urn in enumerate([A, B]):
            address = assertion_urn(urn, _GAP_RULE_ID)
            if address not in query:
                continue
            outcome = self.results.get(urn)
            data[f"a{index}"] = {
                "runEvents": {
                    "runEvents": [{"result": {"type": outcome}}] if outcome else []
                }
            }
        return data


def test_an_error_assertion_is_read_back_as_a_standing_gap() -> None:
    assert standing_gaps([A, B], _Transport({A: "ERROR"})) == frozenset({A})


def test_a_success_assertion_is_not_a_gap() -> None:
    assert standing_gaps([A, B], _Transport({A: "SUCCESS"})) == frozenset()


def test_an_asset_with_no_assertion_at_all_is_not_a_gap() -> None:
    assert standing_gaps([A, B], _Transport({})) == frozenset()


def test_a_broken_transport_invents_no_scars() -> None:
    """Planning as though nothing was ever tried is the honest failure here."""

    class Broken:
        def graphql(
            self, query: str, variables: dict[str, object]
        ) -> dict[str, object]:
            raise RuntimeError("catalog unreachable")

    assert standing_gaps([A, B], Broken()) == frozenset()


def test_gap_addresses_are_derived_not_stored() -> None:
    """The catalog is asked about addresses this computed, not a list it keeps."""
    transport = _Transport({A: "ERROR"})
    standing_gaps([A, B], transport)
    assert transport.queries == 1


def test_asking_about_nothing_costs_nothing() -> None:
    class Never:
        def graphql(
            self, query: str, variables: dict[str, object]
        ) -> dict[str, object]:
            raise AssertionError("should not have been called")

    assert standing_gaps([], Never()) == frozenset()
