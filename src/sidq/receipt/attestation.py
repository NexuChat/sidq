"""Bind a receipt to the engine that produced it.

A receipt is nine structured-property strings on a DataHub dataset, and until now
nothing tied one to Sidq. Anyone who can write to the catalog could write
`sidq.verdict = PASS` by hand with a plausible policy hash, and every reader —
including `sidq verify` — would accept it. `context_hash` does not close that: it
hashes the semantic entity and its immediate lineage, all of it public catalog
data, so a forger computes a valid one with the same published function.

That is the sharpest hole a project whose whole claim is "the graph may be lying"
can have: its only trust anchor lived inside the thing it tells you not to trust.

An Ed25519 signature over the receipt body closes it. The public key is committed
and pinned to the revision; the private key never is, and reaches a writer through
the environment the way every other credential here does.

**The URN is signed with the body**, deliberately. Without it a genuine PASS
receipt could be lifted off a harmless dataset and pasted onto a dangerous one,
and every field in it would still verify.

What this does not do: an attacker who can write to the catalog can still delete a
real receipt. Signing prevents forgery, not denial — and of the two, forgery is the
one that manufactures false confidence rather than an honest absence.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_PROPERTY = "urn:li:structuredProperty:sidq.signature"
PRIVATE_KEY_ENVIRONMENT = "SIDQ_SIGNING_KEY"
PUBLIC_KEY_FILE = Path(__file__).resolve().parent / "signing-key.pub"


class Attestation(StrEnum):
    """Three answers, because collapsing them loses the one that matters.

    `UNATTESTED` is not `TAMPERED`. A receipt written before signing existed, or by
    an operator without the key, is unproven — not proven false. Reporting the two
    as one repeats the mistake the coverage gaps were built to fix: a single blank
    standing for two different facts.
    """

    SIGNED = "SIGNED"
    TAMPERED = "TAMPERED"
    UNATTESTED = "UNATTESTED"


class SigningKeyError(RuntimeError):
    """The configured key material could not be read."""


def signing_payload(urn: str, values: Mapping[str, list[str]]) -> bytes:
    """The exact bytes a signature covers: the asset and its receipt body.

    Canonical by construction — sorted keys, no insignificant whitespace — so the
    same receipt produces the same payload on any host, in any Python, in any
    order the properties happen to come back from the catalog.
    """
    # A property with no values and a property that is absent are the same fact,
    # and a catalog is free to return either. Normalising them here is what keeps
    # a receipt with no rules fired from reading as tampered on the way back;
    # anything with content is left exactly as it was signed.
    body = {
        key: list(value)
        for key, value in values.items()
        if key != SIGNATURE_PROPERTY and list(value)
    }
    return json.dumps(
        {"urn": urn, "properties": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def load_private_key() -> Ed25519PrivateKey | None:
    """The signing key, from the environment. Absent is a normal state."""
    raw = os.environ.get(PRIVATE_KEY_ENVIRONMENT, "").strip()
    if not raw:
        return None
    try:
        seed = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SigningKeyError(
            f"{PRIVATE_KEY_ENVIRONMENT} is not valid base64"
        ) from error
    if len(seed) != 32:
        raise SigningKeyError(
            f"{PRIVATE_KEY_ENVIRONMENT} must decode to 32 bytes, got {len(seed)}"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_public_key(path: Path | None = None) -> Ed25519PublicKey | None:
    """The verifying key committed beside the engine. Absent is a normal state."""
    source = path or PUBLIC_KEY_FILE
    if not source.exists():
        return None
    raw = source.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        material = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SigningKeyError(f"{source} is not valid base64") from error
    if len(material) != 32:
        raise SigningKeyError(f"{source} must decode to 32 bytes, got {len(material)}")
    return Ed25519PublicKey.from_public_bytes(material)


def sign(urn: str, values: Mapping[str, list[str]], key: Ed25519PrivateKey) -> str:
    """Sign a receipt body for one asset. Returns base64, the shape a property holds."""
    return base64.b64encode(key.sign(signing_payload(urn, values))).decode("ascii")


class _Default:
    """Distinguishes "use the committed key" from "there is no key"."""


DEFAULT_KEY = _Default()


def attest(
    urn: str,
    values: Mapping[str, list[str]],
    key: Ed25519PublicKey | None | _Default = DEFAULT_KEY,
) -> Attestation:
    """Which of the three states this receipt is in.

    `key=None` means the caller has no verifying key and every receipt is
    therefore `UNATTESTED` — a reader that cannot check a signature has not
    proven one wrong. Omitting the argument means "use the committed key".
    Those are different questions, and answering them with the same value once
    let a caller who had just been handed `None` silently verify against the
    shipped key instead of declining.
    """
    signatures = values.get(SIGNATURE_PROPERTY) or []
    signature = signatures[0] if signatures else ""
    if not signature:
        return Attestation.UNATTESTED
    verifier = load_public_key() if isinstance(key, _Default) else key
    if verifier is None:
        return Attestation.UNATTESTED
    try:
        material = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return Attestation.TAMPERED
    try:
        verifier.verify(material, signing_payload(urn, values))
    except InvalidSignature:
        return Attestation.TAMPERED
    return Attestation.SIGNED
