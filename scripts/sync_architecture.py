#!/usr/bin/env python3
"""Copy the canonical architecture SVG to the landing page."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "architecture.svg"
WEB = ROOT / "web" / "architecture.svg"


def main() -> None:
    WEB.write_text(CANONICAL.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    main()
