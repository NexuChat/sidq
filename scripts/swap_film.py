#!/usr/bin/env python3
"""Swap the published film's identity across every surface at once.

The film is named in ten places across six documents, a landing page, a
submission field and a guard, and its SHA-256, byte size and duration are
pinned in `tests/test_published_claims.py`. A previous swap left two documents
disagreeing about which file was live, which is the failure that guard now
exists to catch — so this does the whole set in one pass rather than trusting
anyone to remember the tenth place at the end of a long day.

It refuses to run on anything it has not verified: the new master must exist,
its hash and size are read from the file rather than supplied, and the old
values must actually be present before they are replaced.

    python3 scripts/swap_film.py --master NEW.mp4 --video-id ID \\
        --chapters 7 --live-chapters 6 --srt NEW.en.srt

Nothing is written until every replacement has been located, so a partial swap
is not a state this can leave behind. `--dry-run` reports what it would do.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OLD_ID = "R4GdN36Lsno"
OLD_SHA = "86c6faf7de2f149628940026a7c889fe1e20520e53079f087ce22ea811ddd690"
OLD_SIZE = "41,410,417"
OLD_DURATION = "175.595"
OLD_FRAMES = "5,266"
OLD_CUES = "61-cue"
OLD_CHAPTERS = "five of six chapters"


def probe(master: pathlib.Path) -> tuple[str, int, str, int]:
    """Read identity from the file itself; never accept it as an argument."""
    digest = hashlib.sha256(master.read_bytes()).hexdigest()
    size = master.stat().st_size
    duration = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(master),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Counted, not derived from duration times frame rate — the published record
    # states an exact authored frame count and a reader can re-count it.
    frames = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=nw=1:nk=1", str(master),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return digest, size, f"{float(duration):.3f}", int(frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, type=pathlib.Path)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--srt", type=pathlib.Path, help="the new sidecar SRT, to count its cues")
    parser.add_argument(
        "--chapters", type=int, required=True,
        help="how many chapters the new film has (it cannot be read from the container)",
    )
    parser.add_argument(
        "--live-chapters", type=int, required=True,
        help="how many of them carry real footage rather than illustration",
    )
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    master = arguments.master.expanduser().resolve()
    if not master.is_file():
        print(f"STOP: {master} does not exist", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", arguments.video_id):
        print("STOP: a YouTube id is 11 characters of [A-Za-z0-9_-]", file=sys.stderr)
        return 1

    digest, size, duration, frames = probe(master)
    cues = None
    if arguments.srt and arguments.srt.is_file():
        cues = sum(
            1 for line in arguments.srt.read_text(encoding="utf-8").splitlines()
            if "-->" in line
        )
    if float(duration) >= 180:
        print(f"STOP: {duration}s is not under the three-minute rule", file=sys.stderr)
        return 1

    replacements = {
        OLD_ID: arguments.video_id,
        OLD_SHA: digest,
        OLD_SIZE: f"{size:,}",
        OLD_DURATION: duration,
        OLD_FRAMES: f"{frames:,}",
        OLD_CHAPTERS: (
            f"{arguments.live_chapters} of {arguments.chapters} chapters"
        ),
    }
    if cues is not None:
        replacements[OLD_CUES] = f"{cues}-cue"

    targets = [
        *sorted(ROOT.glob("*.md")),
        *sorted(ROOT.glob("docs/*.md")),
        ROOT / "web" / "index.html",
        ROOT / "tests" / "test_published_claims.py",
    ]

    planned: list[tuple[pathlib.Path, str, dict[str, int]]] = []
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        hits = {old: text.count(old) for old in replacements if old in text}
        if not hits:
            continue
        updated = text
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        planned.append((path, updated, hits))

    if not planned:
        print("STOP: found nothing to replace — is the swap already done?", file=sys.stderr)
        return 1

    print(f"  new master   {master}")
    print(f"  sha256       {digest}")
    print(f"  size         {size:,} bytes")
    print(f"  duration     {duration}s")
    print(f"  frames       {frames:,}  (counted)")
    print(f"  chapters     {arguments.chapters}, {arguments.live_chapters} with real footage")
    if cues is not None:
        print(f"  srt cues     {cues}")
    print(f"  video id     {arguments.video_id}")
    print()
    for path, _, hits in planned:
        detail = ", ".join(f"{k[:12]}×{v}" for k, v in hits.items())
        print(f"  {path.relative_to(ROOT)}  ({detail})")

    if arguments.dry_run:
        print("\n  dry run — nothing written")
        return 0

    for path, updated, _ in planned:
        path.write_text(updated, encoding="utf-8")
    print(f"\n  rewrote {len(planned)} files. Now run `make check`, then redeploy the page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
