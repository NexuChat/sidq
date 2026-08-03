# Public landing QA — 2026-08-02

Implementation build under test: `https://sidq.mlki.app`, built from
commit [`8039675d68e748b37a73a2796959c8d8bda8dabc`](https://github.com/NexuChat/sidq/commit/8039675d68e748b37a73a2796959c8d8bda8dabc).
The public and local health payloads, immutable release path, and `origin/main`
all reported that exact SHA during the implementation run. Document-only
releases retain that tested application code and are revalidated before
activation; the current deployed revision is read from `/healthz` and the
landing footer rather than hard-coded into a commit that would change its own
identity.

## Browser and accessibility

- Playwright loaded the public HTTPS page with the expected title.
- Responsive full-page review passed at 1440x1000, 768x1024, and 375x812: no
  clipped sections, horizontal overflow, missing controls, or broken layout was
  visible.
- The page exposed one `h1`, ordered section headings, named navigation regions,
  semantic buttons, useful image alternatives, and a keyboard-focusable live
  result.
- Final AccessLint live-DOM WCAG 2.2 AA audit against the steady page after the
  entrance animation completed: **0 violations**.
- Browser console: 0 errors, 0 warnings.

## Live journeys

All 5/5 fixed POST paths returned HTTP 200 through the public TLS route:

| Journey | Browser-visible result |
|---|---|
| `handoff` | Exit 0; a separate read returns `VERIFIED` with receipt time, policy hash, verifier, and evidence document. |
| `gate-demo` | Exit 0; the expected committed `BLOCK` verdict is re-derived byte-identically. |
| `audit` | Exit 1 by design; 5 assets examined, 4 findings, 5 unverifiable checks, and 4 `unowned_consumed` assets. |
| `repair` | Exit 1 by design; catalog-dependent snapshot with 1 proposed, 0 proven, 1 rejected, 11 findings still standing, and nothing written. The live catalog lacked complete lineage, so the proposal failed closed instead of being called safe. |
| `claims` | Exit 0; 6 documented fields, 4 claims proposed, 4 tested, 1 violated, and 3 holding produce `WARN`. |

The server disables every run button while a command is active. Its runnable set
contains fixed argument arrays only; request bodies and unknown commands are
rejected, output and execution time are bounded, and the public routes contain no
catalog-write flag.

## Service boundary

- Local and public `/healthz`: `status=ok`, with exactly five live demos.
- Local and public `/readyz`: `status=ready`, `datahub=ok`.
- Plain HTTP redirects canonically to HTTPS with status 308 and preserves the
  path/query on the configured public host.
- HTTPS response includes CSP, HSTS, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, Permissions Policy, Referrer Policy, COOP, and CORP.
- Every static reference is fingerprinted with a content-derived digest key and
  returned HTTP 200, so cache busting is content-addressed.
- All three external links on the landing page returned HTTP 200.
- One Playwright lab measurement at 1440x1000 recorded 44.1 ms TTFB, 82 ms
  DOMContentLoaded, 87.6 ms load, 144 ms FCP and LCP, CLS 0, a 48 ms maximum
  observed interaction event, no long tasks, and `overflowX=false`. This is a
  single controlled measurement, not field data or a latency guarantee.

## Automated gates

- Current host run: 1006 collected, 1011 passed, and 2 optional integration tests
  skipped. The Qwen model weights were not present in the cache, and the Ollama
  model `ibm/granite4:1b-q4_1` was not installed.
- Current host branch coverage: 83.85%, above the enforced 80.0% threshold.
- Ruff lint, Ruff format, and mypy: pass.
- The authoritative latest-main status is the [main-branch CI workflow
  view](https://github.com/NexuChat/sidq/actions/workflows/ci.yml?query=branch%3Amain).
  The final workflow pins the official `actions/setup-python` v7 Node 24 release
  by full commit SHA to remove the Node 20 deprecation annotation. The workflow
  view is authoritative for the submitted revision; [run
  30747820384](https://github.com/NexuChat/sidq/actions/runs/30747820384)
  passed for the implementation SHA above.

## Final-film media QA

- Upload artifact:
  `/home/dev/sidq-video/artifacts/video/sidq-final-en.mp4`, SHA-256
  `0811a494c3ee6f78f907c3f2d14908ca18df403d81e38d63093cfa7dab46beef`,
  29,636,338 bytes.
- `ffprobe` reported a 169.216-second container with 5,075 decoded 1920x1080
  H.264 frames at 30 fps and AAC-LC 48 kHz stereo audio. Full strict decode
  passed; no qualifying black interval was detected.
- Audio measured -16.1 LUFS integrated and -4.5 dBTP. Silence and freeze reports
  were manually matched to authored gaps, evidence holds, and static designed
  scenes; the rejected replay transition is absent from the final artifact.
- Two-second full-film contact sheets, transitions, first 15 seconds, captions,
  package text, and high-confidence secret/stale-claim patterns were reviewed.
  An independent final proof review returned no actionable finding.
- The adjacent SHA-256 manifest verifies the MP4, 54-cue SRT, 1280x720
  project-owned thumbnail, six-frame contact sheet, and upload metadata. Public
  upload and logged-out playback verification remain owner-only.

## Dependency audit

- `uv.lock`, `requirements.lock`, `requirements-action.lock`,
  `requirements-bench.lock`, `requirements-dev.lock`, and
  `requirements-landing.lock` have no known vulnerabilities in the current
  audit.
- The isolated `requirements-mcp.lock` has one known finding:
  `PYSEC-2026-3447` / `CVE-2026-59890` for `setuptools==81.0.0`. The official
  DataHub SDK constraint `setuptools<82` prevents resolving the patched release.
  The affected sdist-creation path is not reachable in the deployed Linux
  wheel-only, hash-locked MCP runtime; the finding remains recorded rather than
  suppressed. See [`SECURITY.md`](../SECURITY.md#python-dependency-advisory-status).
