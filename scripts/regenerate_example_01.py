#!/usr/bin/env python3
"""Regenerate `examples/01-blocked-pii-dashboard/verdict.json` from the engine.

The flagship example is the artifact a judge opens, and the README publishes a
reproduction command against its `policy_hash` + `commit_sha`. That only means
anything if the committed verdict is what the shipped engine and policy actually
produce. It was maintained by hand and drifted: the policy gained rules, its hash
changed, and the published file kept citing the old one — so the reproduction
command a judge would run no longer matched.

This makes the artifact generated rather than curated, and
`tests/test_golden_examples.py` fails if the committed copy differs from a fresh
run. Drift now breaks the build instead of shipping.

The reconstruction is deliberately faithful to how the example was first
produced: the dbt manifest declares only `cust_email`, so the twenty-one
surviving columns read as added and `cust_email` as removed, and the model sits
at `models/order_entry/customers.sql`. It runs offline from the committed graph
replay snapshot.

Usage:
    scripts/regenerate_example_01.py --check
    scripts/regenerate_example_01.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sidq import cli
from sidq.bot.comment import STICKY_MARKER, render_comment
from sidq.graph.fixtures import ReplayGraphClient
from sidq.serialization import canonical_data, canonical_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "graph"
EXAMPLE = ROOT / "examples" / "01-blocked-pii-dashboard"
VERDICT = EXAMPLE / "verdict.json"
COMMENT = EXAMPLE / "pr-comment.md"

# Every file that quotes the reproduction command. They are prose, so the hash is
# substituted in place rather than regenerated, but they are covered by a test
# that fails when any of them disagrees with the verdict.
HASH_CITING = ("README.md", "docs/PR-BOT.md", "skills/datahub-verify/SKILL.md")

# The example's own history, kept explicit so the reconstruction cannot drift
# into a different scenario than the one that was published.
RELATION = "b2fd91.order_entry_db.order_entry.customers"
SOURCE_PATH = "models/order_entry/customers.sql"
CONTRACT_COLUMNS = ("cust_email",)
COMMIT_SHA = "5addb753788935d4d1aa6a9483c28c6fc124e5c7"

# Evidence carries a DataHub UI link built from this variable, which defaults to
# localhost. A published artifact must point at the host a judge can actually
# open, so it is pinned here rather than left to the caller's shell — a
# regeneration run from a bare terminal would otherwise reintroduce the dead
# localhost links that were deliberately removed from every judge-facing output.
DATAHUB_UI_URL = "https://datahub.mlki.app"


@contextmanager
def _published_graph_host() -> Iterator[None]:
    """Set the published UI host for one call, then put the environment back.

    This module is imported by the test suite, so mutating the process
    environment permanently would leak into every later test. `test_cli.py`
    asserts the localhost default, and it only passed because it happens to sort
    before `test_golden_examples.py` — any reordering would have broken CI.
    """
    previous = os.environ.get("SIDQ_DATAHUB_UI_URL")
    os.environ["SIDQ_DATAHUB_UI_URL"] = DATAHUB_UI_URL
    try:
        yield
    finally:
        if previous is None:
            del os.environ["SIDQ_DATAHUB_UI_URL"]
        else:
            os.environ["SIDQ_DATAHUB_UI_URL"] = previous


def _verdict_object():
    """Return the engine's own Verdict, not its JSON, for the comment renderer."""
    sql = (EXAMPLE / "customers.sql").read_text(encoding="utf-8")
    with (
        _published_graph_host(),
        tempfile.TemporaryDirectory(prefix="sidq-example-01-") as workspace,
    ):
        root = _write_project(Path(workspace), sql)
        return cli.check(
            [str(root / SOURCE_PATH)],
            graph=ReplayGraphClient(FIXTURES),
            live_source=None,
            repo_root=root,
            commit_sha=COMMIT_SHA,
        )


def _write_project(root: Path, sql: str) -> Path:
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {"adapter_type": "dbt"},
                "nodes": {
                    "model.sidq.customers": {
                        "original_file_path": SOURCE_PATH,
                        "relation_name": RELATION,
                        "config": {"meta": {"environment": "PROD"}},
                        "columns": {name: {} for name in CONTRACT_COLUMNS},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    model = root / SOURCE_PATH
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text(sql, encoding="utf-8")
    return root


def comment() -> str:
    """Render the published PR comment from the same verdict, never by hand.

    `pr-comment.md` is generated output. Editing it directly is what let the
    `policy_hash` in it drift out of step with the verdict beside it.

    The findings are reordered to match `verdict.json` before rendering.
    `canonical_data` orders findings differently from the engine's insertion
    order, and the production bot renders straight off the live object, so the two
    disagree on the order the rule ids appear in the heading. Publishing in
    canonical order keeps the two files a judge reads side by side consistent with
    each other; the divergence itself is pinned by a test.

    Findings are matched by their canonical serialisation, not by rule id. Keying
    on the rule id was a real bug: two gates raise `pii_exposure` on two different
    routes, and a dict keyed by rule id keeps only the last, so the published
    comment showed one finding twice and dropped the other entirely.
    """
    verdict = _verdict_object()
    canonical = canonical_data(verdict)["findings"]
    remaining = list(verdict.findings)
    ordered = []
    for target in canonical:
        match = next(
            finding for finding in remaining if canonical_data(finding) == target
        )
        remaining.remove(match)
        ordered.append(match)
    return render_comment(replace(verdict, findings=tuple(ordered)))


def regenerate() -> dict:
    """Run the shipped engine over the published SQL, offline."""
    return json.loads(canonical_json(_verdict_object()))


def rendered() -> str:
    return json.dumps(regenerate(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed verdict is stale",
    )
    arguments = parser.parse_args()

    fresh = rendered()
    fresh_comment = comment()
    new = json.loads(fresh)
    new_hash = new["policy_hash"]

    if arguments.check:
        stale = []
        if VERDICT.read_text(encoding="utf-8") != fresh:
            stale.append(str(VERDICT.relative_to(ROOT)))
        if COMMENT.read_text(encoding="utf-8") != fresh_comment:
            stale.append(str(COMMENT.relative_to(ROOT)))
        stale.extend(
            name
            for name in HASH_CITING
            if f"policy_hash={new_hash}"
            not in (ROOT / name).read_text(encoding="utf-8")
            and f'"policy_hash": "{new_hash}"'
            not in (ROOT / name).read_text(encoding="utf-8")
        )
        if stale:
            print("stale; rerun scripts/regenerate_example_01.py:")
            for name in stale:
                print(f"  {name}")
            return 1
        print("examples/01 and every artifact quoting its hash are current")
        return 0

    previous = json.loads(VERDICT.read_text(encoding="utf-8"))
    VERDICT.write_text(fresh, encoding="utf-8")
    COMMENT.write_text(fresh_comment, encoding="utf-8")
    print(f"wrote {VERDICT.relative_to(ROOT)}")
    print(f"wrote {COMMENT.relative_to(ROOT)}")
    print(f"  decision:    {previous['decision']} -> {new['decision']}")
    print(f"  rules:       {[f['rule_id'] for f in new['findings']]}")
    print(f"  policy_hash: {previous['policy_hash']} -> {new_hash}")

    for name in HASH_CITING:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        updated = re.sub(
            r'(policy_hash(?:=|": "))[0-9a-f]{64}',
            rf"\g<1>{new_hash}",
            text,
        )
        # docs/PR-BOT.md embeds the comment verbatim from the sticky marker on, and
        # `tests/test_bot.py` asserts that. Substituting only the hash left the
        # embedded body stale the moment the comment itself changed.
        marker_at = updated.find(STICKY_MARKER)
        if marker_at != -1:
            updated = updated[:marker_at] + fresh_comment
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
