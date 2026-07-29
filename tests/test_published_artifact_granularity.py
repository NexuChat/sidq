"""Published lineage claims must match the example verdict's path evidence."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
VERDICT = ROOT / "examples" / "01-blocked-pii-dashboard" / "verdict.json"


def test_published_artifacts_match_example_path_granularity() -> None:
    raw = json.loads(VERDICT.read_text(encoding="utf-8"))
    granularities = {
        path["granularity"]
        for finding in raw["findings"]
        for evidence in finding["evidence"]
        for path in evidence["detail"].get("paths", ())
    }

    assert len(granularities) == 1
    granularity = granularities.pop()
    assert granularity in {"column", "table"}
    expected = f"{granularity}-level"
    unexpected = "table-level" if granularity == "column" else "column-level"
    artifacts = (
        ROOT / "README.md",
        ROOT / "web" / "index.html",
        ROOT / "docs" / "PR-BOT.md",
        *sorted((ROOT / "examples").glob("**/pr-comment.md")),
    )

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        assert expected in text, f"{artifact} must say {expected}"
        assert unexpected not in text, f"{artifact} must not say {unexpected}"
