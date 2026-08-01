#!/usr/bin/env python3
"""Copy the canonical architecture SVG to every judge-facing surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "architecture.svg"
WEB = ROOT / "web" / "architecture.svg"
GALLERY = ROOT / "docs" / "gallery" / "src" / "03-architecture.html"


def main() -> None:
    svg = CANONICAL.read_text(encoding="utf-8")
    WEB.write_text(svg, encoding="utf-8")

    html = GALLERY.read_text(encoding="utf-8")
    start = html.index("<svg")
    end = html.index("</svg>", start) + len("</svg>")
    GALLERY.write_text(html[:start] + svg.strip() + html[end:], encoding="utf-8")


if __name__ == "__main__":
    main()
