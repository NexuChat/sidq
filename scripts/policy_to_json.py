#!/usr/bin/env python3
"""Render the shipped policy as canonical JSON so a non-Python reader can decide.

`docs/ENGINE-SPEC.md` states the resolution completely — any `block` wins, else any
`warn`, else `PASS` — but a specification nobody has implemented twice is a claim,
not a property. `scripts/rederive.sh` re-derives the decision from evidence with
`jq` and nothing else, and jq cannot read YAML. This emits the same rules as JSON
so it can.

The conversion is the one place a reader must still take this repository's word, so
it is kept as small and as checkable as possible: keys sorted, two-space indent,
no transformation beyond what `json` does to a parsed YAML document. `policy_hash`
is unaffected — it hashes the raw bytes of `default_policy.yaml`, so the pinned
artifact stays the YAML, and `rederive.sh` verifies that hash before it decides.
The JSON is a rendering of the pinned file, never a second source of truth.

`tests/test_independent_rederivation.py` regenerates and compares, so the two files
cannot drift apart without breaking the build.

Usage:
    scripts/policy_to_json.py --check
    scripts/policy_to_json.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "sidq" / "policy" / "default_policy.yaml"
TARGET = ROOT / "src" / "sidq" / "policy" / "default_policy.json"


def render() -> str:
    document = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed copy has drifted"
    )
    arguments = parser.parse_args()

    rendered = render()
    if arguments.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                f"{TARGET.relative_to(ROOT)} is stale; run scripts/policy_to_json.py",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET.relative_to(ROOT)} matches the policy it renders")
        return 0

    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
