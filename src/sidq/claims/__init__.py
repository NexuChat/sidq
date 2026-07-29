"""Turn conservative documentation claims into verified live-source evidence."""

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
    "Claim",
    "ClaimExtractor",
    "ClaimVerification",
    "ClaimVerifier",
    "CompiledClaim",
    "ModelExtractor",
    "RuleBasedExtractor",
    "compile_claim",
    "verify_claim",
]
