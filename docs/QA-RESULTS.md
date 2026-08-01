# Public landing QA — 2026-08-01

Target: `https://sidq.mlki.app` at commit `574c931`.

## Browser and accessibility

- Playwright loaded the public HTTPS page with the expected title.
- Responsive full-page review passed at 375x812, 768x1024, and 1440x1000: no
  clipped sections, horizontal overflow, missing controls, or broken layout was
  visible.
- The page exposed one `h1`, ordered section headings, named navigation regions,
  semantic buttons, useful image alternatives, and a keyboard-focusable live
  result.
- AccessLint live-DOM WCAG 2.2 AA audit after the entrance animation completed:
  **0 violations**. An immediate audit caught the hero mid-animation at partial
  opacity; repeating against the settled DOM removed it, so this was timing rather
  than the final rendered contrast.
- Browser console before measurement instrumentation: 0 errors, 0 warnings.

## Live journeys

All 5/5 fixed POST paths returned HTTP 200 through the public TLS route:

| Journey | Browser-visible result |
|---|---|
| `handoff` | Exit 0; a separate read returns `VERIFIED` with receipt time, policy hash, verifier, and evidence document. |
| `gate-demo` | Exit 0; the committed `BLOCK` verdict is re-derived byte-identically. |
| `audit` | Exit 1 by design; four findings are displayed and explicitly labelled “findings, not a failure.” |
| `repair` | Exit 1 by design; one six-column proposal is proven, nothing is written, and ten findings remain named. |
| `claims` | Exit 0; four claims are tested and a measured documentation violation produces `WARN`. |

The server disables every run button while a command is active. Its runnable set
contains fixed argument arrays only; request bodies and unknown commands are
rejected, output and execution time are bounded, and the public routes contain no
catalog-write flag.

## Service boundary

- Local and public `/healthz`: `status=ok`, with exactly five live demos.
- Local and public `/readyz`: `status=ready`, `datahub=ok`.
- HTTPS response includes CSP, HSTS, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, Permissions Policy, Referrer Policy, COOP, and CORP.
- Navigation timing on the tested route: DOMContentLoaded 69 ms, load 201 ms,
  first contentful paint 204 ms, and measured CLS 0. These are a single live run,
  not a latency guarantee.

## Automated gates

- `pytest`: 594 passed, 2 explicitly skipped because their 32 MB regenerable
  benchmark corpus is intentionally not committed.
- Coverage: 80.22% branch-aware total, above the enforced 80.0% threshold.
- Ruff lint, Ruff format, and mypy: pass.
- GitHub Actions CI for `574c931`: pass.
