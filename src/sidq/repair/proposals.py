"""Turn a finding into a concrete catalog mutation — or refuse to.

Every proposal here is derived from catalog facts alone. Nothing is guessed, no
model is consulted, and a finding that cannot be repaired from evidence already in
the catalog produces no proposal rather than a plausible one.

That refusal is the substance. Two of the six checks are mechanically repairable
because the catalog already contains the correct value somewhere else; the other
four are not, and each is documented below with the reason. A repair agent that
had an answer for all six would be inventing four of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sidq.gates.self_contradiction import CatalogEntity, CatalogSnapshot, is_pii_tag
from sidq.models import Evidence

# Checks with no mechanical repair, and why. Kept as data because the report
# names them: an agent that silently ignores four of six findings looks like one
# that found nothing to do there.
UNREPAIRABLE: dict[str, str] = {
    "deprecated_upstream_of_live": (
        "either the upstream is not really deprecated or the downstream should be "
        "retired — the catalog does not say which, and both are business decisions"
    ),
    "doc_references_missing_column": (
        "the description names a column that does not exist; rewriting prose to "
        "match a schema requires knowing what the author meant"
    ),
    "lineage_field_missing": (
        "lineage is derived from queries and ingestion, and the MCP server exposes "
        "no lineage mutation — the fix belongs upstream in the pipeline"
    ),
    "orphan_lineage": (
        "an edge to an asset that does not exist is either a stale edge or a "
        "missing asset; deleting evidence of a gap is not a repair"
    ),
}


@dataclass(frozen=True, slots=True)
class Proposal:
    """One concrete mutation, expressed as the exact MCP call that would run it."""

    finding_kind: str
    subject: str
    tool: str
    arguments: dict[str, Any]
    rationale: str
    derived_from: tuple[str, ...] = field(default_factory=tuple)

    @property
    def targets(self) -> tuple[tuple[str, str | None], ...]:
        """Every (asset, column) this one call would change. Often more than one.

        A PII marker repair covers the whole lineage closure, because fixing only
        the column the finding named moves the leak downstream instead of closing
        it. The MCP write tools take parallel lists, so the set is one call.
        """
        urns = [str(item) for item in self.arguments.get("entity_urns") or ()]
        paths = [item for item in self.arguments.get("column_paths") or ()]
        paths += [None] * (len(urns) - len(paths))
        return tuple(
            (urn, str(path) if path else None) for urn, path in zip(urns, paths)
        )

    @property
    def urn(self) -> str:
        return self.targets[0][0] if self.targets else ""

    def key(self) -> tuple[str, str, str]:
        return (self.tool, self.subject, self.finding_kind)


def propose(finding: Evidence, snapshot: CatalogSnapshot) -> Proposal | None:
    """Derive the one defensible mutation for this finding, or return None."""
    if finding.kind == "pii_leak_untagged":
        return _propose_pii_tag(finding, snapshot)
    if finding.kind == "unowned_consumed":
        return _propose_owner(finding, snapshot)
    return None


def propose_all(
    findings: Sequence[Evidence], snapshot: CatalogSnapshot
) -> list[Proposal]:
    """Propose for every finding, keeping one proposal per distinct mutation."""
    proposals: list[Proposal] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        proposal = propose(finding, snapshot)
        if proposal is None or proposal.key() in seen:
            continue
        seen.add(proposal.key())
        proposals.append(proposal)
    return proposals


def _propose_pii_tag(finding: Evidence, snapshot: CatalogSnapshot) -> Proposal | None:
    """Propagate the upstream PII marker down the *whole* field-lineage closure.

    Nothing here is inferred. The catalog already states that this column is fed by
    a column marked PII; the marker on the downstream is simply missing, and the
    value written is the upstream's own URN rather than one of our choosing.

    The closure is the part that had to be learned from the engine. Tagging only
    the column named in the finding was tried first, and `prove` refused it: on the
    live showcase catalog it resolved that finding and immediately created a new
    one, because the newly marked column feeds a Looker explore that is now itself
    an untagged consumer of PII. A one-hop repair does not fix a leak, it moves it.
    So the proposal covers every column the marker reaches, and it is written as a
    single MCP call — the tools take lists, so the whole closure lands or none of it
    does. Anything already carrying the marker is left alone.
    """
    edge = finding.detail.get("edge") or {}
    markers = [
        str(tag)
        for tag in finding.detail.get("source_pii_tags") or ()
        if is_pii_tag(str(tag))
    ]
    urn, _, column = str(finding.subject).partition("#")
    if not markers or not urn or not column:
        return None

    # DataHub marks column PII with a glossary term or a tag, and they are
    # different write tools. A marker that is still a display name was never
    # resolved to a URN, and both tools reject a name — so it is dropped rather
    # than sent, and if that empties the set there is no proposal to make.
    tool, key = _write_tool(markers)
    writable = sorted(marker for marker in markers if marker.startswith("urn:"))
    if tool is None or not writable:
        return None

    targets = _closure(urn, column, snapshot, frozenset(writable))
    if not targets:
        return None
    source_field = edge.get("source_field")
    return Proposal(
        finding_kind=finding.kind,
        subject=finding.subject,
        tool=tool,
        arguments={
            key: writable,
            "entity_urns": [item[0] for item in targets],
            "column_paths": [item[1] for item in targets],
        },
        rationale=(
            f"{source_field} upstream carries {', '.join(writable)}, and the "
            f"catalog's own field lineage carries it into {len(targets)} column(s) "
            "that do not declare it"
        ),
        derived_from=tuple(f"{item[0]}#{item[1]}" for item in targets),
    )


def _closure(
    urn: str, column: str, snapshot: CatalogSnapshot, markers: frozenset[str]
) -> list[tuple[str, str]]:
    """Every column the marker reaches from here that does not already declare it."""
    fields_by_urn = {
        entity.urn: {item.path: item for item in entity.fields}
        for entity in snapshot.entities
    }
    reached: set[tuple[str, str]] = set()
    queue = [(urn, column)]
    while queue:
        node = queue.pop()
        if node in reached:
            continue
        reached.add(node)
        for edge in snapshot.edges:
            if (
                edge.source_urn == node[0]
                and edge.source_field == node[1]
                and edge.target_field is not None
            ):
                queue.append((edge.target_urn, edge.target_field))
    return sorted(
        item
        for item in reached
        if (field := fields_by_urn.get(item[0], {}).get(item[1])) is not None
        and not markers & set(field.tags)
    )


def _write_tool(markers: Sequence[str]) -> tuple[str | None, str]:
    """Which official MCP tool writes this marker back, by what the marker *is*.

    Mixed sets are refused rather than split. Half a repair leaves the finding
    standing, and `prove` would reject it anyway — declining here says so once,
    at the point where the ambiguity actually exists.
    """
    kinds = {marker.split(":")[2] for marker in markers if marker.startswith("urn:li:")}
    if kinds == {"glossaryTerm"}:
        return "add_terms", "term_urns"
    if kinds == {"tag"}:
        return "add_tags", "tag_urns"
    return None, ""


def _propose_owner(finding: Evidence, snapshot: CatalogSnapshot) -> Proposal | None:
    """Propose an owner only when every owned upstream names the same one.

    An unowned table that is consumed by others needs an owner, but the catalog
    does not record who. The one case where it effectively does is unanimity: if
    every upstream that has an owner has the *same* owner, that owner already owns
    the whole path into this asset. Any disagreement means the catalog has two
    candidate answers, and picking one would be the agent deciding — so it refuses.
    """
    urn = str(finding.subject)
    entity = next((item for item in snapshot.entities if item.urn == urn), None)
    if entity is None or entity.owners:
        return None
    upstreams = _upstream_entities(urn, snapshot)
    owner_sets = [frozenset(item.owners) for item in upstreams if item.owners]
    if not owner_sets:
        return None
    candidates = set().union(*owner_sets)
    if len(candidates) != 1:
        return None
    owner = next(iter(candidates))
    return Proposal(
        finding_kind=finding.kind,
        subject=finding.subject,
        tool="add_owners",
        arguments={
            "owner_urns": [owner],
            "entity_urns": [urn],
            "ownership_type": "__system__technical_owner",
        },
        rationale=(
            f"all {len(owner_sets)} owned upstreams of this asset name {owner} and "
            "no other, so the catalog already places the whole path under one owner"
        ),
        derived_from=tuple(sorted(item.urn for item in upstreams if item.owners)),
    )


def _upstream_entities(urn: str, snapshot: CatalogSnapshot) -> list[CatalogEntity]:
    sources = {edge.source_urn for edge in snapshot.edges if edge.target_urn == urn} - {
        urn
    }
    return [entity for entity in snapshot.entities if entity.urn in sources]
