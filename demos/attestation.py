"""Four receipts, one key, and the two that do not survive it.

A Sidq receipt is structured-property strings on a DataHub dataset. Anyone who can
write to the catalog can write `sidq.verdict = PASS` by hand, and `context_hash`
is no defence — it hashes public catalog data with a published function, so a
forger computes a valid one. This runs the real reader over four receipts and
prints what it says about each.

The fourth is the one a design review usually misses.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from sidq.receipt.attestation import attest
from sidq.receipt.build import Receipt
from sidq.receipt.read import _PREFIX, render_verification

LOOKUP = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.lookup_country,DEV)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.customers_pii,PROD)"


def _status(urn: str, values: dict[str, list[str]], key: object) -> dict[str, object]:
    short = {k.removeprefix(_PREFIX): v for k, v in values.items()}
    return {
        "urn": urn,
        "verdict": short.get("verdict", [""])[0],
        "reason_code": None,
        "commit_sha": short.get("commit_sha", [""])[0],
        "checked_at": short.get("checked_at", [""])[0],
        "policy_hash": short.get("policy_hash", [""])[0],
        "rules_fired": [],
        "verifier": short.get("verifier", [""])[0],
        "evidence_url": "",
        "context_hash": short.get("context_hash", [""])[0],
        "stale": False,
        "stale_reason": "receipt records PASS; continue",
        "attestation": attest(urn, values, key).value,  # type: ignore[arg-type]
    }


def main() -> int:
    # Generated here, in memory, for this run only. The engine's own key lives in
    # the environment and never in a repository.
    key = Ed25519PrivateKey.generate()
    public = key.public_key()

    hand_written = Receipt(
        urn=LOOKUP,
        verdict="PASS",
        reason_code=None,
        commit_sha="c0ffee",
        checked_at="2026-08-26T00:00:00Z",
        policy_hash="66f48004",
        rules_fired=(),
        verifier="sidq@0.1.0",
        evidence_url="",
        evidence=(),
        context_hash="sha256:abc",
    )
    signed = hand_written.signed(key)
    edited = dict(signed.structured_property_values())
    edited[f"{_PREFIX}policy_hash"] = ["a-policy-that-never-shipped"]

    cases = (
        (
            "1 · an operator writes PASS by hand",
            "No signature. Every field is plausible and every field is invented.",
            LOOKUP,
            hand_written.structured_property_values(),
        ),
        (
            "2 · the engine writes the same receipt, holding the key",
            "Identical body. One more property.",
            LOOKUP,
            signed.structured_property_values(),
        ),
        (
            "3 · one field edited after signing",
            "The policy hash is changed to one that never shipped.",
            LOOKUP,
            edited,
        ),
        (
            "4 · the genuine signed receipt, pasted onto the PII table",
            (
                "Nothing is edited. Every field verifies, because every field "
                "really was signed — only the asset is different."
            ),
            CUSTOMERS,
            signed.structured_property_values(),
        ),
    )

    for title, note, urn, values in cases:
        print(title)
        print(f"    {note}")
        for line in render_verification(urn, _status(urn, values, public)):
            print(f"    {line}")
        print()

    print("The signature covers the receipt body AND the asset URN. Without the URN")
    print("in it, case 4 would pass: a PASS lifted off a harmless table and pasted")
    print("onto the one carrying customer data, with every field still verifying.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
