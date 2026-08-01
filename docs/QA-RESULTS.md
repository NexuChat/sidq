# Public landing QA — 2026-08-01

Application build under test: `https://sidq.mlki.app`, built from app/web commit
[`bdae060d84397b9bba8c94b2be08fc0f47788940`](https://github.com/NexuChat/sidq/commit/bdae060d84397b9bba8c94b2be08fc0f47788940).

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
| `repair` | Exit 1 by design; dry run with 1 proposed, 1 proven, 0 rejected, and nothing written. |
| `claims` | Exit 0; 6 documented fields, 4 claims proposed, 4 tested, 1 violated, and 3 holding produce `WARN`. |

The server disables every run button while a command is active. Its runnable set
contains fixed argument arrays only; request bodies and unknown commands are
rejected, output and execution time are bounded, and the public routes contain no
catalog-write flag.

## Service boundary

- Local and public `/healthz`: `status=ok`, with exactly five live demos.
- Local and public `/readyz`: `status=ready`, `datahub=ok`.
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

- Current host run after the CI demo-ref fix: 764 collected, 762 passed, and 2
  optional integration tests skipped. The Qwen model weights were not present in
  the cache, and the Ollama model `ibm/granite4:1b-q4_1` was not installed.
- Current host branch coverage: 82.33%, above the enforced 80.0% threshold.
- Ruff lint, Ruff format, and mypy: pass.
- An anonymous public shallow clone before the workflow fix passed 761 tests at
  82.32% branch coverage with 3 skips. The only additional skip was the
  demo-ref guard because shallow clones had not fetched `demo/*` refs. In that
  clone, `make gate-demo` exited 0 and reproduced the expected `BLOCK` verdict.
- The workflow now fetches `demo/*` as remote-tracking refs so the demo-ref guard
  runs in CI instead of skipping.
- Previous [GitHub Actions CI run
  30710463507](https://github.com/NexuChat/sidq/actions/runs/30710463507) passed
  before that workflow fix with 761 passed, 3 skipped, and 82.29% branch
  coverage. It is not evidence for the current 762-passed, 2-skipped host run.

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
