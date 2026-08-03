"""The upstream-bound skill must stay true to the engine and to its house style.

`skills/datahub-verify/` is prepared as a contribution to
`datahub-project/datahub-skills`, which is a stated bonus criterion rather than
decoration. Two ways it can rot, and both are silent:

1. **It can teach a vocabulary the code no longer speaks.** The skill tells an
   agent to act on `action` and to read `CURRENT RECEIPT · BLOCK · STOP`. If
   those strings drift from `sidq.receipt.state`, the skill instructs an agent to
   look for output that never arrives — the exact failure it exists to prevent,
   committed in the document that prevents it.

2. **It can drift out of the shape upstream accepts.** The sponsor's repo runs
   prettier and markdownlint over every skill and expects a consistent section
   layout. A contribution that does not look like the other five reads as a dump
   rather than a contribution, and nothing in this repository would notice.

These pin both without requiring the Node toolchain in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidq.receipt.state import VERDICTS, Action, ReceiptState

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "datahub-verify" / "SKILL.md"

# The sections every upstream catalog skill carries. Order is not enforced —
# only that a reviewer finds each one where they expect it to be.
HOUSE_SECTIONS = (
    "## Multi-Agent Compatibility",
    "## Not This Skill",
    "## Common Mistakes",
    "## Red Flags",
    "## Remember",
)


@pytest.fixture(scope="module")
def skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_carries_the_upstream_frontmatter_fields(skill: str) -> None:
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]

    for field in ("name:", "description:", "user-invocable:", "allowed-tools:"):
        assert field in frontmatter, field
    assert "name: datahub-verify" in frontmatter


def test_the_skill_declares_exactly_the_tools_the_server_exposes(skill: str) -> None:
    """A skill that names a fourth tool would be asking for one that is not there."""
    server = (ROOT / "src" / "sidq" / "mcp_server" / "server.py").read_text(
        encoding="utf-8"
    )
    frontmatter = skill.split("---\n", 2)[1]
    declared = {
        item.strip()
        for item in frontmatter.split("allowed-tools:", 1)[1].splitlines()[0].split(",")
    }

    assert declared == {
        "mcp__sidq__check_change",
        "mcp__sidq__verify_context",
        "mcp__sidq__search_verified",
    }
    # And each one is a tool the server actually registers, so renaming a tool
    # breaks here rather than in an agent session.
    for tool in declared:
        assert f'name="{tool.removeprefix("mcp__sidq__")}"' in server, tool


def test_the_skill_keeps_the_upstream_section_layout(skill: str) -> None:
    for heading in HOUSE_SECTIONS:
        assert heading in skill, heading


def test_the_skill_teaches_the_receipt_vocabulary_the_reader_actually_prints(
    skill: str,
) -> None:
    """Every state, action, and headline in the skill must exist in the engine."""
    for state in ReceiptState:
        assert f"`{state.value}`" in skill, state

    for action in Action:
        assert action.value in skill, action

    for verdict in VERDICTS:
        assert verdict in skill, verdict

    # The three current-receipt headlines, byte-for-byte as `render_verification`
    # composes them, plus the one phrase reserved for the other three states.
    for verdict, action in (
        ("PASS", Action.CONTINUE),
        ("WARN", Action.REVIEW),
        ("BLOCK", Action.STOP),
    ):
        assert f"CURRENT RECEIPT · {verdict} · {action.value}" in skill
    assert "`NOT VERIFIED`" in skill


def test_the_skill_never_tells_an_agent_a_refusal_means_unchecked(skill: str) -> None:
    """The distinction the whole project rests on, asserted in its own teaching."""
    assert "A refusal is not an absence." in skill
    assert "Coverage is not permission." in skill


def test_the_generated_block_leaves_the_blank_line_upstream_lint_requires(
    skill: str,
) -> None:
    """Two gates in this repo once disagreed about one newline, permanently.

    `scripts/regenerate_example_01.py` owns the worked-example section, and it
    wrote that section flush against the horizontal rule below it.
    `markdownlint` — which the sponsor's repository runs over every skill —
    rejects that. `make regen-check` would have demanded the blank line be
    removed on every run, and the upstream lint would have demanded it back.

    A contradiction between two gates does not resolve itself; it gets suppressed
    by whoever is in a hurry. So the shape the generator must emit is asserted.
    """
    from scripts.regenerate_example_01 import _replace_skill_worked_example

    assert "governance decision.\n\n---\n" in skill

    rewritten = _replace_skill_worked_example(skill, "### Worked example: rewritten\n")
    assert "rewritten\n\n---\n" in rewritten


def test_the_skill_tables_are_pipe_aligned_as_upstream_formats_them(
    skill: str,
) -> None:
    """Prettier aligns markdown table pipes; upstream CI rejects a table that is
    not aligned. Checking the shape here means a hand edit fails in this
    repository rather than in the sponsor's review queue."""
    rows: list[str] = []
    for line in skill.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            rows.append(stripped)
            continue
        if len(rows) > 1:
            widths = {len(row) for row in rows}
            assert len(widths) == 1, f"ragged table near: {rows[0][:60]}"
        rows = []
