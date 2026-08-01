"""Turn a policy verdict into an immutable, serializable Sidq receipt."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sidq import __version__
from sidq.models import Verdict


@dataclass(frozen=True, slots=True)
class Receipt:
    urn: str
    verdict: str
    reason_code: str | None
    commit_sha: str
    checked_at: str
    policy_hash: str
    rules_fired: tuple[str, ...]
    verifier: str
    evidence_url: str
    evidence: tuple[dict[str, Any], ...]
    # Swarm identity. Empty for a solo run: a lone auditor has no cooperation to
    # record, and writing an empty worker id would imply one where none existed.
    swarm_run: str = ""
    worker_id: str = ""

    def structured_property_values(self) -> dict[str, list[str]]:
        """The exact MCP shape for the queryable receipt body."""

        return {
            "urn:li:structuredProperty:sidq.verdict": [self.verdict],
            "urn:li:structuredProperty:sidq.reason_code": [self.reason_code or ""],
            "urn:li:structuredProperty:sidq.commit_sha": [self.commit_sha],
            "urn:li:structuredProperty:sidq.checked_at": [self.checked_at],
            "urn:li:structuredProperty:sidq.policy_hash": [self.policy_hash],
            "urn:li:structuredProperty:sidq.rules_fired": list(self.rules_fired),
            "urn:li:structuredProperty:sidq.verifier": [self.verifier],
            "urn:li:structuredProperty:sidq.evidence_url": [self.evidence_url],
            # Written only when a swarm produced this receipt, so a solo audit
            # leaves no trace of cooperation that did not happen.
            **(
                {
                    "urn:li:structuredProperty:sidq.swarm_run": [self.swarm_run],
                    "urn:li:structuredProperty:sidq.worker_id": [self.worker_id],
                }
                if self.swarm_run
                else {}
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def evidence_markdown(self) -> str:
        """A readable document while keeping the receipt's machine facts canonical."""

        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True, default=list)
        return f"# Sidq receipt\n\n```json\n{payload}\n```\n"


def build_receipt(
    urn: str,
    verdict: Verdict,
    *,
    checked_at: datetime | None = None,
    verifier: str | None = None,
    evidence_url: str = "",
    swarm_run: str = "",
    worker_id: str = "",
) -> Receipt:
    """Build a receipt after a verdict is decided; never influence the verdict."""

    if verdict.decision not in {"PASS", "WARN", "BLOCK"}:
        raise ValueError(f"unsupported Sidq verdict: {verdict.decision}")
    timestamp = (checked_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    fired = tuple(
        sorted(
            {
                finding.rule_id
                for finding in verdict.findings
                if finding.severity.lower() in {"warn", "block"}
            }
        )
    )
    evidence = tuple(
        {
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "message": finding.message,
            "evidence": [
                {
                    "kind": item.kind,
                    "subject": item.subject,
                    "detail": item.detail,
                    "graph_links": item.graph_links,
                }
                for item in finding.evidence
            ],
        }
        for finding in verdict.findings
    )
    return Receipt(
        urn=urn,
        verdict=verdict.decision,
        reason_code=verdict.reason_code,
        commit_sha=verdict.commit_sha,
        checked_at=timestamp.isoformat().replace("+00:00", "Z"),
        policy_hash=verdict.policy_hash,
        rules_fired=fired,
        verifier=verifier or f"sidq@{__version__}",
        evidence_url=evidence_url,
        evidence=evidence,
        swarm_run=swarm_run,
        worker_id=worker_id,
    )
