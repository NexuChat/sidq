from __future__ import annotations

from sidq.models import Evidence, TouchedAsset
from sidq.policy.engine import PolicyEngine
from sidq.serialization import canonical_json


def test_identical_inputs_produce_byte_identical_verdict_json() -> None:
    evidence = [
        Evidence("unowned_asset", "urn:li:dataset:b", {"owners": []}),
        Evidence("future_evidence", "urn:li:dataset:a", {"nested": {"z": 1, "a": 2}}),
    ]
    touched = [
        TouchedAsset("urn:li:dataset:b", "b.sql", ("z", "a"), (), ()),
        TouchedAsset("urn:li:dataset:a", "a.sql", (), (), ()),
    ]
    engine = PolicyEngine()

    first = canonical_json(engine.decide(evidence, touched=touched, commit_sha="abc123"))
    second = canonical_json(engine.decide(evidence, touched=touched, commit_sha="abc123"))

    assert first == second
