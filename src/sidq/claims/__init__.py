"""Turn conservative documentation claims into verified live-source evidence."""

from .attest import (
    Attestation,
    AttestationRun,
    DocumentationAttester,
)
from .extractor import ClaimExtractor, ModelExtractor, RuleBasedExtractor
from .models import Claim
from .verify import (
    ClaimVerification,
    ClaimVerifier,
    CompiledClaim,
    compile_claim,
    verify_claim,
)

__all__ = [
    "Attestation",
    "AttestationRun",
    "Claim",
    "ClaimExtractor",
    "ClaimVerification",
    "ClaimVerifier",
    "CompiledClaim",
    "DocumentationAttester",
    "ModelExtractor",
    "RuleBasedExtractor",
    "compile_claim",
    "verify_claim",
]
