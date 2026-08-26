"""A receipt must be provably the engine's, or say plainly that it is not.

Before this, a receipt was nine structured-property strings and nothing tied one
to Sidq: anyone able to write to the catalog could write `sidq.verdict = PASS` by
hand and every reader would accept it. `context_hash` is no defence — it hashes
public catalog data with a published function, so a forger computes a valid one.

These tests hold the four cases that matter, and the fourth is the one a design
review usually misses: a *genuine* signed receipt, lifted off a harmless dataset
and pasted onto a dangerous one. It fails only because the URN is inside the
signed payload.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sidq.receipt.attestation import (
    SIGNATURE_PROPERTY,
    Attestation,
    SigningKeyError,
    attest,
    load_private_key,
    load_public_key,
    sign,
    signing_payload,
)
from sidq.receipt.build import Receipt

HARMLESS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.lookup_country,DEV)"
DANGEROUS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.customers_pii,PROD)"


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _receipt(urn: str = HARMLESS) -> Receipt:
    return Receipt(
        urn=urn,
        verdict="PASS",
        reason_code=None,
        commit_sha="c0ffee",
        checked_at="2026-08-26T00:00:00Z",
        policy_hash="66f48004",
        rules_fired=(),
        verifier="sidq@0.1.0",
        evidence_url="urn:li:document:shared-1",
        evidence=(),
        context_hash="sha256:abc",
    )


def test_a_receipt_nobody_signed_is_unattested_not_forged(
    key: Ed25519PrivateKey,
) -> None:
    """The third state exists so absence of proof is not read as proof of absence."""
    receipt = _receipt()
    assert (
        attest(receipt.urn, receipt.structured_property_values(), key.public_key())
        is Attestation.UNATTESTED
    )


def test_a_signed_receipt_verifies(key: Ed25519PrivateKey) -> None:
    signed = _receipt().signed(key)
    assert (
        attest(signed.urn, signed.structured_property_values(), key.public_key())
        is Attestation.SIGNED
    )


def test_editing_one_field_after_signing_is_caught(key: Ed25519PrivateKey) -> None:
    signed = _receipt().signed(key)
    values = signed.structured_property_values()
    values["urn:li:structuredProperty:sidq.verdict"] = ["PASS"]
    values["urn:li:structuredProperty:sidq.policy_hash"] = ["a-policy-never-shipped"]
    assert attest(signed.urn, values, key.public_key()) is Attestation.TAMPERED


def test_a_genuine_receipt_moved_to_another_asset_is_caught(
    key: Ed25519PrivateKey,
) -> None:
    """The attack signing alone would miss: nothing in the body names the asset.

    Every field verifies, because every field really was signed. Only the URN
    disagrees, which is exactly why it is signed with them.
    """
    signed = _receipt(HARMLESS).signed(key)
    values = signed.structured_property_values()
    assert attest(HARMLESS, values, key.public_key()) is Attestation.SIGNED
    assert attest(DANGEROUS, values, key.public_key()) is Attestation.TAMPERED


def test_a_signature_that_is_not_even_base64_is_tampered(
    key: Ed25519PrivateKey,
) -> None:
    values = _receipt().structured_property_values()
    values[SIGNATURE_PROPERTY] = ["not base64 at all !!"]
    assert attest(HARMLESS, values, key.public_key()) is Attestation.TAMPERED


def test_a_reader_with_no_public_key_says_unattested_not_tampered(
    key: Ed25519PrivateKey, tmp_path: object
) -> None:
    """A reader that cannot check a signature has not proven one wrong."""
    signed = _receipt().signed(key)
    values = signed.structured_property_values()
    missing = load_public_key(tmp_path / "absent.pub")  # type: ignore[operator]
    assert missing is None
    assert attest(signed.urn, values, missing) is Attestation.UNATTESTED


def test_an_absent_property_and_an_empty_one_sign_identically(
    key: Ed25519PrivateKey,
) -> None:
    """A catalog may return either shape for a property with no values.

    Without this the receipt of an asset with no rules fired would come back
    reading as tampered, and the alarm would be the reader's, not an attacker's.
    """
    values = _receipt().structured_property_values()
    with_empty = {**values, "urn:li:structuredProperty:sidq.rules_fired": []}
    without = {
        key_: value
        for key_, value in values.items()
        if key_ != "urn:li:structuredProperty:sidq.rules_fired"
    }
    assert signing_payload(HARMLESS, with_empty) == signing_payload(HARMLESS, without)


def test_the_payload_does_not_depend_on_property_order(
    key: Ed25519PrivateKey,
) -> None:
    values = _receipt().structured_property_values()
    reversed_order = dict(reversed(list(values.items())))
    assert signing_payload(HARMLESS, values) == signing_payload(
        HARMLESS, reversed_order
    )


def test_the_signing_key_is_read_from_the_environment(
    key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv("SIDQ_SIGNING_KEY", base64.b64encode(seed).decode())
    loaded = load_private_key()
    assert loaded is not None
    values = _receipt().structured_property_values()
    assert sign(HARMLESS, values, loaded) == sign(HARMLESS, values, key)


def test_no_signing_key_configured_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIDQ_SIGNING_KEY", raising=False)
    assert load_private_key() is None


@pytest.mark.parametrize("bad", ["not-base64", base64.b64encode(b"short").decode()])
def test_unusable_key_material_is_refused_loudly(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently signing nothing would be worse than failing to start."""
    monkeypatch.setenv("SIDQ_SIGNING_KEY", bad)
    with pytest.raises(SigningKeyError):
        load_private_key()


def test_the_committed_public_key_is_loadable() -> None:
    """The key a reader verifies against ships with the engine and is pinned."""
    assert load_public_key() is not None


def _status(attestation: str) -> dict[str, object]:
    return {
        "urn": HARMLESS,
        "verdict": "PASS",
        "reason_code": None,
        "commit_sha": "c0ffee",
        "checked_at": "2026-08-26T00:00:00Z",
        "policy_hash": "66f48004",
        "rules_fired": [],
        "verifier": "sidq@0.1.0",
        "evidence_url": "",
        "context_hash": "sha256:abc",
        "stale": False,
        "stale_reason": "receipt records PASS; continue",
        "attestation": attestation,
    }


def test_a_tampered_receipt_stops_the_agent_rather_than_only_labelling_itself() -> None:
    """Detecting forgery and still answering CONTINUE would make it decoration.

    An agent asks `may_continue`; it does not read further down for a line that
    said TAMPERED.
    """
    from sidq.receipt.state import judge

    assert judge(_status("TAMPERED")).may_continue is False


def test_an_unattested_receipt_is_not_refused() -> None:
    """Unproven is not disproven — refusing these would reject every receipt
    written before a signing key existed, which is a different claim entirely."""
    from sidq.receipt.state import judge

    assert judge(_status("UNATTESTED")).may_continue is True
    assert judge(_status("SIGNED")).may_continue is True
