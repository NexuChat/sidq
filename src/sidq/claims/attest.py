"""Where a model belongs in a system whose verdicts must be deterministic.

Sidq keeps no model in the judged path, and `docs/DECISION-COST.md` measures why
that costs nothing. Stated as an absence, that is a defensive claim. Stated
properly it is an architectural boundary with two sides, and this module is
where the boundary actually lives:

    a model may decide **what to test**
    only the engine may decide **what is true**

The task on the model's side is real and has no algorithm. A catalog's field
descriptions are prose written by people — "one row per customer", "must never
be null", "one of: active, inactive" — and turning prose into a testable
assertion is exactly the job no regular expression finishes. `RuleBasedExtractor`
handles the handful of phrasings that carry one meaning and abstains on
everything else; a local model reads what is left.

The task on the engine's side is equally real, and the model never touches it. A
claim is not evidence. It becomes evidence only by being compiled into read-only
SQL and run against the live source, and what the engine then judges is the row
count that came back — not the sentence, not the model, not the confidence.

**The rule that makes this mechanical rather than aspirational** is
`_admissible` below: a model-proposed claim that could not be tested contributes
nothing at all. Not a warning, not a lower-confidence finding — nothing. What
would remain of it is the model's opinion, and this engine does not take
opinions as evidence. A *rule*-proposed claim that could not be tested is kept,
because the reading was deterministic and "we could not check this" is a fact
about the run worth reporting.

So the model can be wrong, slow, differently-versioned, or replaced tomorrow,
and no verdict moves. The worst a bad model can do is waste a query.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sidq.graph.client import DatasetInfo
from sidq.graph.live_source import _relation_from_urn
from sidq.models import Evidence

from .extractor import ClaimExtractor, RuleBasedExtractor
from .models import Claim
from .verify import ClaimVerification, ClaimVerifier


@dataclass(frozen=True, slots=True)
class Attestation:
    """One documented sentence, carried through to what the source actually says."""

    urn: str
    claim: Claim
    verification: ClaimVerification

    @property
    def admissible(self) -> bool:
        return _admissible(self.claim, self.verification)


def _admissible(claim: Claim, verification: ClaimVerification) -> bool:
    """Whether this result may contribute evidence to a verdict.

    The whole boundary is this function. A model-extracted claim earns a place in
    the evidence only by having been *tested*: if the query could not run, what is
    left is the model's reading of a sentence, and a reading is not a measurement.
    A rule-extracted claim survives the same failure because its reading was
    deterministic — anyone can re-derive it from the sentence and get the same
    claim — so reporting that it could not be checked states a fact rather than
    laundering a guess.
    """
    if verification.status != "unverifiable":
        return True
    return claim.origin == "rule"


@dataclass
class AttestationRun:
    """What one pass over a catalog's documentation established, and what it did not."""

    documented_fields: int = 0
    attestations: list[Attestation] = field(default_factory=list)
    # Sentences neither reader would turn into a claim. Published because an
    # extractor that silently drops most of its input looks identical to one
    # that understands everything.
    declined: list[tuple[str, str]] = field(default_factory=list)

    @property
    def admitted(self) -> list[Attestation]:
        return [item for item in self.attestations if item.admissible]

    @property
    def dropped(self) -> list[Attestation]:
        """Model-proposed claims that could not be tested — kept only to be counted."""
        return [item for item in self.attestations if not item.admissible]

    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(item.verification.evidence for item in self.admitted)

    def summary(self) -> dict[str, object]:
        by_origin: dict[str, int] = {}
        for item in self.attestations:
            by_origin[item.claim.origin] = by_origin.get(item.claim.origin, 0) + 1
        statuses: dict[str, int] = {}
        for item in self.admitted:
            status = item.verification.status
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "documented_fields": self.documented_fields,
            "claims_proposed": len(self.attestations),
            "by_origin": dict(sorted(by_origin.items())),
            "declined": len(self.declined),
            "tested": len(self.admitted),
            "dropped_untestable_model_claims": len(self.dropped),
            "results": dict(sorted(statuses.items())),
        }


class DocumentationAttester:
    """Read a catalog's prose, test what it asserts, and let the engine judge.

    `extra` is consulted only where the rule-based reader abstained. That order is
    not a performance choice: it means a model can never overturn a deterministic
    reading of a sentence, only reach sentences no deterministic reading covers.
    """

    def __init__(
        self,
        verifier: ClaimVerifier,
        *,
        extra: ClaimExtractor | None = None,
        min_confidence: float = 0.0,
    ) -> None:
        self._verifier = verifier
        self._rules = RuleBasedExtractor()
        self._extra = extra
        self._min_confidence = min_confidence

    def run(
        self, datasets: Iterable[DatasetInfo], *, budget: int = 50
    ) -> AttestationRun:
        result = AttestationRun()
        for dataset in datasets:
            for schema_field in dataset.fields:
                sentence = (schema_field.description or "").strip()
                if not sentence:
                    continue
                result.documented_fields += 1
                if len(result.attestations) >= budget:
                    continue
                claim = self._propose(sentence, schema_field.path, dataset)
                if claim is None:
                    result.declined.append((schema_field.path, sentence))
                    continue
                result.attestations.append(
                    Attestation(
                        dataset.urn, claim, self._verifier.verify(claim, dataset.urn)
                    )
                )
        return result

    def _propose(
        self, sentence: str, column: str, dataset: DatasetInfo
    ) -> Claim | None:
        # `table_name` is not decoration. A trained reader is given the table
        # alongside the sentence during training, so withholding it here would
        # move inference off the distribution the head was fitted on — quietly,
        # and only visible as a worse abstention rate nobody could attribute.
        relation = _relation_from_urn(dataset.urn)
        context: Mapping[str, object] = {
            "urn": dataset.urn,
            "column": column,
            "table_name": relation[1] if relation else "",
        }
        claim = self._rules.extract(sentence, column, context)
        if claim is not None:
            return claim
        if self._extra is None:
            return None
        try:
            proposed = self._extra.extract(sentence, column, context)
        except Exception:  # noqa: BLE001 - a model runtime may fail in many ways
            # A model that errors is a model that abstained. It cannot fail the
            # run, because nothing downstream depends on it having spoken.
            return None
        if proposed is None or proposed.confidence < self._min_confidence:
            return None
        # Stamped here rather than trusted from the extractor: origin decides
        # admissibility, so an extractor must not be able to claim it is a rule.
        return Claim(
            proposed.type,
            proposed.column,
            values=proposed.values,
            expr=proposed.expr,
            source_sentence=proposed.source_sentence,
            confidence=proposed.confidence,
            origin="model",
        )


def render(run: AttestationRun, decision: str | None = None) -> list[str]:
    """The report, written so the boundary is visible rather than asserted."""
    summary = run.summary()
    by_origin = summary["by_origin"]
    assert isinstance(by_origin, dict)
    lines = [
        "Documentation attested against the live source",
        "",
        f"  documented fields   {summary['documented_fields']}",
        (
            f"  claims proposed     {summary['claims_proposed']}"
            f"  (rule {by_origin.get('rule', 0)} · model {by_origin.get('model', 0)})"
        ),
        (
            f"  sentences declined  {summary['declined']}  "
            "(no reader would commit to a single meaning)"
        ),
        f"  claims tested       {summary['tested']}",
    ]
    if run.dropped:
        lines.append(
            f"  dropped untested    {len(run.dropped)}  "
            "(model-proposed and unverifiable - a reading is not a measurement)"
        )
    results = summary["results"]
    assert isinstance(results, dict)
    if results:
        lines.append("")
        for status in ("violated", "holds", "unverifiable"):
            if status in results:
                lines.append(f"  {status:<14} {results[status]}")
    violated = [item for item in run.admitted if item.verification.status == "violated"]
    if violated:
        lines.append("")
        lines.append("What the documentation says, and what the source does:")
        for item in violated[:10]:
            count = item.verification.violating_row_count
            lines.append(
                f'  {item.claim.column}: "{item.claim.source_sentence}" '
                f"[{item.claim.origin}] — {count} rows disagree"
            )
    if decision is not None:
        lines.append("")
        lines.append(f"  decision            {decision}")
    return lines


def datasets_from(source: object, urns: Sequence[str]) -> list[DatasetInfo]:
    """Fetch the datasets to attest, skipping anything the source will not return.

    An asset the source refuses is simply absent from the run rather than fatal:
    a documentation pass over ten tables should not be lost because one of them
    is unreadable, and the summary reports how many were actually read.
    """
    found = []
    for urn in urns:
        dataset = None
        try:
            dataset = source.get_dataset(urn)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - source transports raise many types
            dataset = None
        if dataset is not None:
            found.append(dataset)
    return found
