"""Nothing a judge can click, grep, or count may be wrong.

Individual guards already pin individual numbers. These pin the *classes* of
defect that keep recurring across surfaces, so the next one fails here instead of
in front of a reviewer:

- a link to a file the release does not ship (the datasheet cited
  `refilter-v6-report.json`; `docs/LORA.md` cited an eval split that lives only
  in the historical experiment)
- a placeholder that survived into a published document
- copy that describes a limit the server does not actually enforce

Each check reads the judge-facing surfaces only. Internal notes are free to be
rough; the documents linked from the README are not.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Everything a judge is pointed at from the README, the repo root, or a worked
# example. `docs/` is included wholesale because the README links into it.
JUDGE_SURFACES: tuple[Path, ...] = (
    ROOT / "README.md",
    ROOT / "ARCHITECTURE.md",
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
    *sorted(ROOT.glob("docs/*.md")),
    *sorted(ROOT.glob("examples/*/README.md")),
    *sorted(ROOT.glob("skills/*/*.md")),
)

# Paths that name something the *reader* creates, something in another
# repository, or something a command generates on first run. Each is a
# deliberate exception, and naming it here is cheaper than letting the check be
# vague enough to miss a real dangling citation.
NOT_OURS_TO_SHIP = frozenset(
    {
        ".codex/config.toml",  # written by the person following the setup
        ".mcp.json",  # same
        ".sidq/assets.yml",  # optional user-supplied resolver map
        "policy.yaml",  # the operator's own policy, when overriding the shipped one
        "good_change.sql",  # illustrative filenames in a worked walk-through
        "bad_change.sql",
        # DataHub's own repository, cited when explaining their agent context
        # and, in the upstream-contribution note, the two resource files their
        # wheel omits. Ours to report, not ours to ship.
        "datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md",
        "datahub/cli/datapack/resources/registry.json",
        "datahub/cli/datapack/registry.py",
        # The film is produced in a separate repository; VIDEO.md is the
        # production contract for it, and says so.
        "public/v4/audio/narration.provenance.json",
        "public/v4/proof/block-current.png",
        "datahub-receipt-ui.png",
    }
)

# What the release ships is what git tracks — not what happens to be on this
# machine's disk. The basename fallback used to consult the working tree, so a
# locally generated corpus made this guard pass here and fail on a fresh clone:
# the exact split the guard exists to prevent, inside the guard itself.
_TRACKED = frozenset(
    subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
)
_TRACKED_NAMES = frozenset(Path(name).name for name in _TRACKED)

_CITED_PATH = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|md|json|jsonl|yaml|yml|sql|toml|lock|svg|png|css|html|npz|gz))`"
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# A backticked path used as a link label — `[`verdict.json`](examples/.../verdict.json)`.
# The target is what a reader follows, and the link test already checks it.
_LINK_LABEL = re.compile(r"\[`([^`]+)`\]\(")

# A document may discuss an artifact the release does not ship, provided it says
# so where it says it. These are the disclaimers already in use; a new phrasing
# has to be added here deliberately, which is the point — the exemption should
# cost a moment's thought rather than being inferred from vague wording.
_DISCLAIMED = (
    "not part of the public release",
    "not included in this public release",
    "not shipped",
    "is not shipped here",
    "does not ship",
    "not included in this",
)


@pytest.mark.parametrize(
    "document", JUDGE_SURFACES, ids=lambda path: str(path.relative_to(ROOT))
)
def test_every_relative_link_resolves(document: Path) -> None:
    """A broken link is the cheapest possible way to look careless."""
    for match in _MARKDOWN_LINK.finditer(document.read_text(encoding="utf-8")):
        target = match.group(1)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path, _, _fragment = target.partition("#")
        if not path:
            continue
        assert (document.parent / path).resolve().exists(), (
            f"{document.relative_to(ROOT)} links to {target}, which does not exist"
        )


@pytest.mark.parametrize(
    "document", JUDGE_SURFACES, ids=lambda path: str(path.relative_to(ROOT))
)
def test_every_cited_repository_path_is_shipped(document: Path) -> None:
    """Citing a file the release does not contain is an unverifiable claim.

    It reads as evidence and cannot be opened, which is the same failure as a
    number nobody can check — and it is easier to reintroduce, because prose
    naturally reaches for a filename.

    A document may still *discuss* an artifact it does not ship — it just has to
    say so in the same paragraph. Silence is what this forbids.
    """
    text = document.read_text(encoding="utf-8")
    labels = set(_LINK_LABEL.findall(text))
    paragraphs = text.split("\n\n")

    for paragraph in paragraphs:
        lowered = paragraph.lower()
        disclaimed = any(phrase in lowered for phrase in _DISCLAIMED)
        for match in _CITED_PATH.finditer(paragraph):
            cited = match.group(1)
            if cited in NOT_OURS_TO_SHIP or cited in labels or disclaimed:
                continue
            # Resolve from the repo root, from the document, or by basename
            # anywhere — prose legitimately calls `src/sidq/models.py` just
            # `models.py` when the surrounding section already located it.
            if cited in _TRACKED or Path(cited).name in _TRACKED_NAMES:
                continue
            pytest.fail(
                f"{document.relative_to(ROOT)} cites `{cited}`, which this "
                "release does not ship. Commit it, describe it without a path, "
                "or say in the same paragraph that it is not shipped."
            )


# Markers that mean "this is not finished". Anchored to the forms a draft
# actually leaves behind: `TODO`, `TBD`, a bracketed slot, an angle-bracket
# insert. Deliberately not the bare word "placeholder" — `SECURITY.md` uses it
# correctly, in a sentence instructing the operator to replace one, and a guard
# that fires on correct prose gets suppressed rather than fixed.
_PLACEHOLDER = re.compile(
    r"(?:^|\s)(TODO|TBD|FIXME|XXX|COMING SOON|LOREM IPSUM)(?:\b|:)"
    r"|(<INSERT[^>]*>)"
    r"|(\[(?:TODO|TBD|PLACEHOLDER|YOUR [A-Z ]+)\])",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "document", JUDGE_SURFACES, ids=lambda path: str(path.relative_to(ROOT))
)
def test_no_placeholder_survived_into_a_published_document(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    found = {
        next(group for group in match.groups() if group).strip().upper()
        for match in _PLACEHOLDER.finditer(text)
    }

    assert not found, f"{document.relative_to(ROOT)} still contains {sorted(found)}"


def test_the_published_video_url_is_the_same_one_everywhere() -> None:
    """The film went public on 2026-08-06; every surface must cite that URL.

    Until the owner uploaded, this guard asserted the opposite — that no URL
    was published anywhere. The invariant that survives the flip is oneness:
    a judge who follows the video link from the README, the runbook, or the
    submission copy must land on the same watch page, and no stray YouTube id
    from a draft or a superseded cut may sit beside it.
    """
    canonical_id = "W0uHsq2Kb0E"
    canonical_url = f"https://www.youtube.com/watch?v={canonical_id}"

    # Every judge surface, not the three that happened to carry a link on the
    # day this was written: CLAIMS-MATRIX.md and QA-RESULTS.md also cite the
    # film, and a stray id in either is just as reachable from the README.
    # Normalised, because the citations wrap across lines and a guard that a
    # reflow can silence is not a guard.
    surfaces = {
        document.relative_to(ROOT): " ".join(
            document.read_text(encoding="utf-8").split()
        )
        for document in (*JUDGE_SURFACES, ROOT / "web" / "index.html")
    }

    # Every shape YouTube serves a video under, so a `shorts/` or `live/` link
    # to a re-upload cannot slip past a regex written for `watch?v=`.
    citation = re.compile(
        r"(?:youtube\.com/(?:watch\?[\w=&%-]*?v=|embed/|shorts/|live/|v/)"
        r"|youtu\.be/)([\w-]{11})"
    )

    cited = 0
    for name, text in surfaces.items():
        for match in citation.finditer(text):
            cited += 1
            assert match.group(1) == canonical_id, (
                f"{name} cites a YouTube id other than the published film: "
                f"{match.group(1)}"
            )
    assert cited, "no judge surface cites the published film at all"

    for name in ("README.md", "docs/VIDEO.md", "docs/DEVPOST.md"):
        text = surfaces[Path(name)]
        assert canonical_url in text, f"{name} must cite the published film"


def test_the_landing_quota_copy_matches_what_the_server_enforces() -> None:
    """The page tells a judge the rate limit before they hit it.

    Stating a limit the server does not enforce is a small lie with an immediate
    cost: a judge who reads "5 runs per 10 minutes" and is refused on the third
    concludes the demo is broken, not that the copy was stale.
    """
    from web import server

    landing = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    quoted = re.search(
        r"(\d+)\s+proof runs? per client every\s+(\d+)\s+minutes", landing
    )
    assert quoted, "the landing page must state the run quota before the buttons"

    runs, minutes = int(quoted.group(1)), int(quoted.group(2))
    assert runs == server.CLIENT_RUN_LIMIT, (
        f"page says {runs} runs; server enforces {server.CLIENT_RUN_LIMIT}"
    )
    assert minutes * 60 == server.CLIENT_WINDOW_SECONDS, (
        f"page says {minutes} minutes; server window is {server.CLIENT_WINDOW_SECONDS}s"
    )
