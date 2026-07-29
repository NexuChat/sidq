"""Deterministic, decision-first Markdown rendering for pull requests."""

from __future__ import annotations

import html
import re
import shlex
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from sidq.models import Evidence, Finding, Verdict

STICKY_MARKER = "<!-- sidq-pr-bot:sticky -->"
_LONG_LIST = 5
_DECISION_HEADLINES = {
    "BLOCK": "🚫 BLOCKED",
    "WARN": "⚠️ WARN",
    "PASS": "✅ PASS",
}
_SEVERITY_ICONS = {"block": "🚫", "warn": "⚠️", "info": "ℹ️"}
_PLATFORMS = {
    "dbt": "dbt",
    "looker": "Looker",
    "powerbi": "Power BI",
    "postgres": "Postgres",
    "snowflake": "Snowflake",
    "tableau": "Tableau",
}


def render_comment(
    verdict: Verdict,
    *,
    mode: Literal["live", "fixture"] = "live",
    reproduce_command: str | None = None,
    advisory_findings: Sequence[Finding] | None = None,
) -> str:
    """Render one stable Markdown comment from a Sidq verdict.

    ``advisory_findings`` is deliberately outside the deterministic
    :class:`~sidq.models.Verdict` today. If that boundary is added to the model,
    the renderer will also read a future ``verdict.advisory_findings`` field.
    """
    if mode not in {"live", "fixture"}:
        raise ValueError("mode must be 'live' or 'fixture'")
    advisory = tuple(
        advisory_findings
        if advisory_findings is not None
        else getattr(verdict, "advisory_findings", ())
    )
    deterministic = tuple(
        finding for finding in verdict.findings if not _is_advisory(finding)
    )
    advisory = (
        *advisory,
        *(item for item in verdict.findings if _is_advisory(item)),
    )

    lines = [
        STICKY_MARKER,
        _headline(verdict.decision, deterministic, advisory),
        "",
        _provenance(mode),
        "",
        "## Deterministic policy decision",
        "",
        (
            "Only the deterministic policy findings in this section affect the "
            "merge decision."
        ),
    ]
    if deterministic:
        for finding in deterministic:
            lines.extend(("", *_render_finding(finding)))
    else:
        lines.extend(("", "No deterministic finding matched a policy rule."))

    if advisory:
        lines.extend(
            (
                "",
                "## Non-blocking model-assisted evidence",
                "",
                (
                    "> **Advisory only.** These model-assisted findings cannot "
                    "block this pull request and are not part of the deterministic "
                    "policy decision."
                ),
            )
        )
        for finding in advisory:
            lines.extend(("", *_render_finding(finding, advisory=True)))

    command = reproduce_command or _default_reproduce_command(verdict)
    lines.extend(
        (
            "",
            "---",
            "",
            (
                "Reproducibility: "
                f"<code>policy_hash={_escape(verdict.policy_hash)}</code> · "
                f"<code>commit_sha={_escape(verdict.commit_sha)}</code> · "
                f"run <code>{_escape(command)}</code>"
            ),
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _headline(
    decision: str,
    deterministic_findings: Sequence[Finding],
    advisory_findings: Sequence[Finding],
) -> str:
    try:
        headline = _DECISION_HEADLINES[decision]
    except KeyError as error:
        raise ValueError(f"unknown Sidq decision: {decision!r}") from error
    wanted = "block" if decision == "BLOCK" else "warn" if decision == "WARN" else ""
    rule_ids = _unique(
        finding.rule_id
        for finding in deterministic_findings
        if finding.severity.lower() == wanted
    )
    if rule_ids:
        rules = ", ".join(f"<code>{_escape(item)}</code>" for item in rule_ids)
        return f"# {headline} — {rules}"
    if decision == "WARN" and advisory_findings:
        rules = ", ".join(
            f"<code>{_escape(item)}</code>"
            for item in _unique(finding.rule_id for finding in advisory_findings)
        )
        return f"# {headline} — {rules} (non-blocking advisory)"
    if decision == "PASS":
        return f"# {headline} — no blocking or warning policy rule fired"
    fallback = "unclassified_policy_failure"
    return f"# {headline} — <code>{fallback}</code>"


def _provenance(mode: str) -> str:
    if mode == "fixture":
        return (
            "> **FIXTURE REPLAY — NOT LIVE DATAHUB.** This verdict used recorded "
            "graph responses. See the sealed demo pull requests for live-graph "
            "verdicts."
        )
    return "> **Provenance: LIVE DATAHUB.** Evidence was read from the live graph."


def _render_finding(finding: Finding, *, advisory: bool = False) -> list[str]:
    severity = finding.severity.lower()
    icon = "🧭" if advisory else _SEVERITY_ICONS.get(severity, "ℹ️")
    label = "ADVISORY" if advisory else severity.upper()
    reason = _plain_reason(finding)
    lines = [
        f"### {icon} <code>{_escape(finding.rule_id)}</code> — {label}",
        "",
        f"**Why:** {_escape(reason)}",
    ]
    if not finding.evidence:
        lines.extend(("", "**Evidence:** No structured evidence was attached."))
        return lines
    for index, evidence in enumerate(finding.evidence, start=1):
        suffix = f" {index}" if len(finding.evidence) > 1 else ""
        lines.extend(("", f"**Evidence{suffix}:** {_evidence_link(evidence)}"))
        detail = _render_evidence_detail(evidence, advisory=advisory)
        if detail:
            lines.extend(("", *detail))
    return lines


def _plain_reason(finding: Finding) -> str:
    reason = finding.message.strip()
    for evidence in sorted(
        finding.evidence, key=lambda item: len(item.subject), reverse=True
    ):
        reason = reason.replace(evidence.subject, _human_label(evidence.subject))
    return " ".join(reason.split())


def _evidence_link(evidence: Evidence) -> str:
    label = _escape(_human_label(evidence.subject))
    links = sorted(link for link in evidence.graph_links if _safe_graph_link(link))
    if not links:
        return f"<code>{label}</code> (no DataHub deep link was recorded)"
    if len(links) == 1:
        return f"[<code>{label}</code>]({_markdown_url(links[0])})"
    return f"<code>{label}</code> — " + ", ".join(
        f"[open in DataHub {index}]({_markdown_url(link)})"
        for index, link in enumerate(links, start=1)
    )


def _render_evidence_detail(evidence: Evidence, *, advisory: bool = False) -> list[str]:
    detail = evidence.detail if isinstance(evidence.detail, Mapping) else {}
    lines: list[str] = []
    changed_field = detail.get("changed_field")
    if isinstance(changed_field, str):
        lines.append(f"- Changed column: <code>{_escape(changed_field)}</code>")

    tags = _strings(detail.get("pii_tags"))
    if tags:
        lines.append(
            "- PII tags: "
            + ", ".join(
                f"<code>{_escape(_human_label(tag))}</code>" for tag in sorted(tags)
            )
        )

    count = detail.get("downstream_count")
    depth = detail.get("depth")
    if isinstance(count, int) and not isinstance(count, bool):
        depth_note = (
            f" within {depth} hops"
            if isinstance(depth, int) and not isinstance(depth, bool)
            else ""
        )
        lines.append(f"- Blast radius: **{count} downstream consumers**{depth_note}")

    path = _impact_path(evidence)
    if path:
        lines.extend(
            (
                "- Column-level impact path:",
                "",
                "  "
                + " → ".join(
                    f"<code>{_escape(_human_label(item))}</code>" for item in path
                ),
            )
        )

    dashboards = _strings(detail.get("dashboards"))
    if dashboards and not path:
        lines.extend(_render_values("Dashboards", dashboards))

    downstream = _strings(detail.get("downstream_urns"))
    if downstream:
        lines.extend(_render_values("Downstream consumers", downstream))

    owners = _strings(detail.get("cross_team_owners"))
    if owners:
        lines.extend(_render_values("Cross-team owners", owners))

    critical = _strings(detail.get("critical_assets"))
    if critical:
        lines.extend(_render_values("Critical assets", critical))

    if advisory:
        producers = _unique(
            value
            for key in ("producer", "checker", "model")
            if isinstance((value := detail.get(key)), str) and value
        )
        if producers:
            lines.append(
                "- Model/producer: "
                + ", ".join(f"<code>{_escape(item)}</code>" for item in producers)
            )
    return lines


def _render_values(label: str, values: Sequence[str]) -> list[str]:
    readable = sorted({_human_label(value) for value in values})
    if len(readable) <= _LONG_LIST:
        return [
            f"- {label}: "
            + ", ".join(f"<code>{_escape(value)}</code>" for value in readable)
        ]
    lines = [
        "",
        "<details>",
        f"<summary>{_escape(label)} ({len(readable)})</summary>",
        "",
    ]
    lines.extend(f"- <code>{_escape(value)}</code>" for value in readable)
    lines.extend(("", "</details>"))
    return lines


def _impact_path(evidence: Evidence) -> tuple[str, ...]:
    raw_paths = evidence.detail.get("paths")
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        return ()
    edges: set[tuple[str, str]] = set()
    nodes: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, Mapping):
            continue
        hops = raw_path.get("hops")
        if isinstance(hops, Mapping):
            ordered_hops = (hops[key] for key in sorted(hops, key=str))
        elif isinstance(hops, Sequence) and not isinstance(hops, (str, bytes)):
            ordered_hops = iter(hops)
        else:
            ordered_hops = ()
        saw_hop = False
        for hop in ordered_hops:
            if not isinstance(hop, Mapping):
                continue
            left, right = hop.get("from"), hop.get("to")
            if isinstance(left, str) and isinstance(right, str):
                saw_hop = True
                edges.add((left, right))
                nodes.update((left, right))
        if not saw_hop:
            left, right = raw_path.get("source"), raw_path.get("target")
            if isinstance(left, str) and isinstance(right, str):
                edges.add((left, right))
                nodes.update((left, right))
    if not edges:
        return ()
    # DataHub returns column lineage as schema-field URNs and chart lineage as
    # dataset URNs. Join those two views at their shared dataset so the rendered
    # chain can continue all the way to the dashboard.
    for node in tuple(nodes):
        dataset = _dataset_from_field(node)
        if dataset and dataset in nodes:
            edges.add((node, dataset))

    start = _path_start(evidence.subject, nodes)
    targets = sorted(node for node in nodes if node.startswith("urn:li:dashboard:"))
    if start is None or not targets:
        return ()
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
    visited = {start}
    target_set = set(targets)
    while queue:
        node, path = queue.popleft()
        if node in target_set:
            return path
        for neighbour in sorted(adjacency[node], key=_human_label):
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, (*path, neighbour)))
    return ()


def _path_start(subject: str, nodes: set[str]) -> str | None:
    dataset, separator, field = subject.partition("#")
    if separator:
        expected = f"urn:li:schemaField:({dataset},{field})"
        if expected in nodes:
            return expected
        matching = sorted(
            node
            for node in nodes
            if node.startswith("urn:li:schemaField:")
            and _human_label(node) == _human_label(subject)
        )
        if matching:
            return matching[0]
    if subject in nodes:
        return subject
    matching = sorted(node for node in nodes if _dataset_from_field(node) == dataset)
    return matching[0] if matching else None


def _human_label(value: str) -> str:
    dataset, separator, field = value.partition("#")
    if separator and dataset.startswith("urn:li:dataset:"):
        return f"{_human_label(dataset)}.{field}"
    if value.startswith("urn:li:schemaField:(") and value.endswith(")"):
        inner = value[len("urn:li:schemaField:(") : -1]
        boundary = inner.rfind("),")
        if boundary >= 0:
            return f"{_human_label(inner[: boundary + 1])}.{inner[boundary + 2 :]}"
    dataset_match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^,)]+)\)",
        value,
    )
    if dataset_match:
        platform, name, _environment = dataset_match.groups()
        label = _PLATFORMS.get(platform.lower(), platform)
        lowered_name = name.lower()
        if platform.lower() == "looker" and ".explore." in lowered_name:
            label = "Looker explore"
        elif platform.lower() == "looker" and ".view." in lowered_name:
            label = "Looker view"
        return f"{label} · {_short_name(name)}"
    for entity, label in (
        ("dashboard", "dashboard"),
        ("chart", "chart"),
        ("corpGroup", "group"),
        ("corpuser", "user"),
        ("tag", "tag"),
    ):
        prefix = f"urn:li:{entity}:"
        if value.startswith(prefix):
            payload = value.removeprefix(prefix).strip("()")
            parts = payload.split(",", 1)
            platform = parts[0] if len(parts) == 2 else ""
            name = parts[-1]
            if platform.lower() == "looker" and entity in {"dashboard", "chart"}:
                label = f"Looker {label}"
            return f"{label} · {_short_name(name)}"
    return value


def _short_name(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 1 and re.fullmatch(r"[0-9a-fA-F]{6,}", parts[0]):
        parts = parts[1:]
    return ".".join(parts)


def _dataset_from_field(value: str) -> str:
    if not value.startswith("urn:li:schemaField:(") or not value.endswith(")"):
        return ""
    inner = value[len("urn:li:schemaField:(") : -1]
    boundary = inner.rfind("),")
    return inner[: boundary + 1] if boundary >= 0 else ""


def _safe_graph_link(link: str) -> bool:
    try:
        parsed = urlsplit(link)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _markdown_url(link: str) -> str:
    # Evidence is data, even when it came from a trusted graph. Percent-encode
    # Markdown delimiters and control characters so a recorded link cannot add
    # content outside the link destination.
    return quote(link, safe="/:?[]@!$&'*+,;=%-._~")


def _default_reproduce_command(verdict: Verdict) -> str:
    sha = verdict.commit_sha
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        return f"sidq check --diff {sha}^..{sha} --json"
    if len(verdict.touched) == 1:
        return f"sidq check --file {shlex.quote(verdict.touched[0].source_path)} --json"
    return "sidq check --diff BASE...HEAD --json"


def _is_advisory(finding: Finding) -> bool:
    if finding.severity.lower() == "advisory":
        return True
    return any(
        isinstance(evidence.detail, Mapping) and evidence.detail.get("advisory") is True
        for evidence in finding.evidence
    )


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _unique(values: Sequence[str] | Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)
