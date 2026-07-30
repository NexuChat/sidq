#!/usr/bin/env python3
"""An analytics agent that asks whether the catalog is telling the truth.

the project gap assessment §6 records an open gap: the demo agent has to be a real
agent, not a script. The distinction it means is not "does an LLM drive it" — it
is whether the thing **chooses its next action from what it observed**. This one
does: it searches for an asset, verifies it, and then branches. Depending on what
the catalog turns out to be, it proceeds, switches to a different asset, or
refuses to answer at all. A script would emit the same SQL regardless.

It is deliberately not LLM-driven. §5 of the same document notes that many
hackathon demos fail on stage because the model behaves differently under the
lights; this agent's control flow is deterministic, so the same catalog state
always produces the same transcript. The LLM-free property is the product, not a
limitation — the model is what Sidq exists to stop, so putting one on the demo's
decision path would undercut the claim.

Two runs, same goal, same catalog:

    --blind    what an agent does today: trusts the catalog and writes SQL
    (default)  the same agent with Sidq: verifies first, and stops when it must

Usage:
    examples/05-agent-that-stops/agent.py --blind
    examples/05-agent-that-stops/agent.py
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mcp import Client

from sidq.graph.fixtures import ReplayGraphClient
from sidq.mcp_server.server import SidqService, create_server

FIXTURES = ROOT / "tests" / "fixtures" / "graph"
CUSTOMERS = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.order_entry_db.order_entry.customers,PROD)"
)
# The question the agent is asked. Answering it needs a customer email column.
GOAL = "How many customers can we reach by email in each region?"


@dataclass
class Transcript:
    """What the agent did, in order, so the demo needs no narration to be legible."""

    lines: list[str] = field(default_factory=list)

    def act(self, what: str) -> None:
        self.lines.append(f"  → {what}")

    def saw(self, what: str) -> None:
        self.lines.append(f"    observed: {what}")

    def decided(self, what: str) -> None:
        self.lines.append(f"    decision: {what}")

    def render(self) -> str:
        return "\n".join(self.lines)


class VerifyingAgent:
    """Perceive, decide, act — with the decision genuinely driven by what it saw."""

    def __init__(self, session: Any, transcript: Transcript) -> None:
        self._session = session
        self._log = transcript

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(tool, arguments)
        if result.structured_content is not None:
            return dict(result.structured_content)
        return json.loads(result.content[0].text)

    async def run(self, goal: str) -> str:
        self._log.act(f'goal: "{goal}"')

        # 1. Find candidate assets. `search_verified` separates assets with fresh
        #    truthful evidence from those with none, so the agent can prefer the
        #    former instead of taking whatever the catalog lists first.
        found = await self._call("search_verified", {"query": "customers", "max_age_days": 7})
        verified = [item["urn"] for item in found.get("verified", ())]
        unverified = [item["urn"] for item in found.get("unverified", ())]
        self._log.act("search_verified(customers)")
        self._log.saw(
            f"{len(verified)} verified, {len(unverified)} never verified"
        )

        target = (verified or unverified or [CUSTOMERS])[0]

        # 2. Ask whether the catalog is telling the truth about it — before
        #    reading a single column name from that catalog.
        truth = await self._call("verify_context", {"urn": target})
        self._log.act(f"verify_context({_short(target)})")
        findings = truth.get("findings") or []
        unverifiable = truth.get("unverifiable") or []
        self._log.saw(
            f"truthful={truth.get('truthful')}, "
            f"{len(findings)} finding(s), {len(unverifiable)} unverifiable"
        )

        # 3. Branch. This is the part a script does not have.
        if not truth.get("truthful"):
            reasons = sorted({str(item.get("kind")) for item in findings})
            if reasons:
                self._log.decided(
                    f"the catalog is not truthful about this asset ({', '.join(reasons)}) "
                    "— refusing to build on it"
                )
                return _refusal(target, reasons, unverifiable)
            self._log.decided(
                "no finding, but checks could not be completed — refusing to treat "
                "an unperformed check as a clean one"
            )
            return _refusal(target, [], unverifiable)

        self._log.decided("catalog verified truthful — proposing SQL")
        sql = _proposed_sql(target)

        # 4. Even then, ask permission before proposing the change.
        gate = await self._call("check_change", {"sql": sql})
        decision = gate.get("decision") or (gate.get("verdict") or {}).get("decision")
        self._log.act("check_change(proposed sql)")
        self._log.saw(f"decision={decision}")
        if decision == "BLOCK":
            self._log.decided("gate blocked the change — not proposing it")
            return _refusal(target, ["check_change: BLOCK"], [])
        return sql


def _short(urn: str) -> str:
    return urn.rsplit(",", 2)[-2] if "," in urn else urn


def _proposed_sql(urn: str) -> str:
    relation = urn.split(",")[1]
    return f"select region_id, count(cust_email)\nfrom {relation}\ngroup by region_id\n"


def _refusal(urn: str, reasons: list[str], unverifiable: list[dict]) -> str:
    """Summarise, never truncate silently.

    `verify_context` can return one `unverifiable` entry per lineage edge, and an
    early version of this printed all eighteen of them verbatim — noise that reads
    as a broken tool rather than as a careful one. They are grouped by check and
    counted, which loses nothing a reader needs and keeps the refusal legible.
    """
    lines = [
        "-- No SQL proposed.",
        f"-- Sidq could not confirm the catalog is truthful about {_short(urn)}.",
    ]
    lines.extend(f"--   finding: {reason}" for reason in reasons)
    grouped: dict[str, list[str]] = {}
    for item in unverifiable:
        grouped.setdefault(str(item.get("check")), []).append(str(item.get("reason")))
    for check, why in sorted(grouped.items()):
        times = f" ×{len(why)}" if len(why) > 1 else ""
        lines.append(f"--   not checked: {check}{times} ({min(set(why))})")
    lines.append("-- An unverified catalog is not a safe basis for a query.")
    return "\n".join(lines)


def blind_run(transcript: Transcript) -> str:
    """What an agent does today: read the catalog, believe it, write the query."""
    transcript.act(f'goal: "{GOAL}"')
    transcript.act("read catalog schema for customers")
    dataset = ReplayGraphClient(FIXTURES).get_dataset(CUSTOMERS)
    columns = [field.path for field in dataset.fields] if dataset else []
    transcript.saw(f"{len(columns)} columns, including cust_email")
    transcript.decided("catalog looks fine — writing the query")
    return _proposed_sql(CUSTOMERS)


async def verified_run(transcript: Transcript) -> str:
    service = SidqService(ReplayGraphClient(FIXTURES), repo_root=str(ROOT))
    async with Client(create_server(service), mode="legacy") as client:
        return await VerifyingAgent(client, transcript).run(GOAL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blind",
        action="store_true",
        help="run without Sidq: trust the catalog, as an agent does today",
    )
    arguments = parser.parse_args()

    transcript = Transcript()
    if arguments.blind:
        print("AGENT WITHOUT SIDQ\n")
        output = blind_run(transcript)
    else:
        print("AGENT WITH SIDQ\n")
        output = anyio.run(verified_run, transcript)

    print(transcript.render())
    print("\n--- what the agent produced ---\n")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
