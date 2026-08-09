# Security audit — 2026-08-02

## Result and severity model

The final review found no known unresolved **P0** or **P1** issue in the scoped
repository and public landing path. This is not a claim that the system is free
of all vulnerabilities.

- **P0:** invalidates safety, eligibility, or the core truth claim.
- **P1:** likely harms security, correctness, or judging.
- **P2:** meaningful robustness or defence-in-depth gap.
- **P3:** optional hardening or polish.

Scope: current Python and browser code, public command execution, Receipt
write/read/rollback behavior, proxy identity and rate limiting, rendered-output
escaping, deployment units, current lockfiles, and high-signal secret patterns in
the current tree and Git history. It does not include a third-party penetration
test, every transitive platform service, or host compromise.

## Fixed findings

| Severity | Finding | Resolution and proof |
|---|---|---|
| P1 | Receipt mutation acknowledgment could be mistaken for persistence. | Success now requires a fresh direct read to observe exact structured properties and exactly one correct managed badge. Timeout or mismatch returns `write_unconfirmed`; tests cover delayed, stale, partial, and acknowledged-no-op backends. |
| P1 | A failed multi-tool Receipt write could leave misleading state. | The writer records prior state, saves the evidence document first, applies the badge second, publishes structured properties last, and performs bounded compensating rollback. Rollback re-reads before restoring, refuses to overwrite divergent external state, preserves a prior BLOCK/swarm provenance, and surfaces `rollback_incomplete` details. |
| P1 | Concurrent in-process writes to one URN could interleave. | A bounded 64-stripe per-URN lock serializes same-process attempts without an unbounded key registry. Cross-process limits remain documented below. |
| P1 | `audit --write-receipts --json` hid rollback detail behind a summary. | JSON now includes deterministic attempted/written/failed counts and an untruncated failure list with URN, verdict, and detail. No-write JSON remains byte-identical. |
| P1 | Public action execution needed a closed, abuse-resistant boundary. | The server accepts only five fixed argument arrays, rejects bodies/unknown commands, uses no shell interpolation, caps input/output/time/concurrency/rate, escapes output, validates capability replay, and never exposes a write flag. Tests cover XSS, traversal, proxy spoofing, IPv4/IPv6 identities, CSRF expectations, cooldowns, and redaction. |
| P1 | Credential values could be partially exposed when one secret overlapped another secret or an internal runtime path. | Public-output redaction now finds every credential match on the original text, merges overlapping spans, and redacts them before host paths or URLs. Regression tests cover containment and partial overlap; an independent adversarial review returned no remaining finding. |
| P1 | Public `make` demos could treat a newer immutable release lockfile as a reason to rebuild the shared read-only runtime. | The hosted allowlist passes Make's exact internal `--old-file` operand for the existing runtime marker and hides that operand from the judge-facing command. Regression tests pin the subprocess argv, public-label omission, and runtime-path redaction; deployment separately byte-compares all runtime inputs before activation. Local clone behavior is unchanged. |

Focused adversarial suites and the full required suite pass; exact counts are in
[`QA-RESULTS.md`](QA-RESULTS.md).

## Remaining bounded risks

### P2 — DataHub mutation atomicity and cross-process races

DataHub's property, tag, and document tools do not form one transaction and the
MCP surface has no compare-and-set primitive. The in-process lock cannot
coordinate different Sidq processes. A process crash or an external writer can
therefore leave partial state; a rollback can also conflict with a legitimate
concurrent edit. Sidq contains the risk by saving the document first, requiring
exact direct readback, checking the union of prior/attempted/current state before
rollback, and returning failure instead of a Receipt success. This is honest
best-effort compensation, not transactional exactly-once storage.

### P2 — isolated MCP dependency advisory

`uv audit --locked --no-cache` resolved 126 entries / audited 125 packages and
reported no known vulnerability or adverse project status for the application
lock. `pip-audit` reports two rows for one unique advisory in the isolated MCP
lock: `setuptools==81.0.0`, `PYSEC-2026-3447` / `CVE-2026-59890`; the published
fix is 83.0.0 while the current DataHub SDK constrains setuptools below 82. The
affected macOS Unicode-path sdist creation is not reachable in the deployed
Linux, hash-locked, wheel-only MCP runtime, which does not build or publish
sdists or accept caller-controlled packaging paths. The finding is recorded,
not suppressed, and must be re-audited when the upstream constraint changes.

### P2 — trusted local peer boundary

Only the loopback Cloudflare peer is trusted to supply `CF-Connecting-IP`.
Remote clients cannot connect to the loopback origin directly. A malicious local
process could spoof that header, but remains subject to global start,
concurrency, command-lock, and cooldown limits. Local-host compromise is outside
this remote boundary.

## Verification performed

```text
make check
  Ruff check: pass
  Ruff format --check: pass
  mypy src/: pass
  pytest: 1142 collected, 1141 passed, 1 skipped   # re-run 2026-08-09
  branch coverage: 84.61% (minimum 80%)

uv audit --locked --no-cache
  125 packages audited; no known application-lock vulnerability

uvx pip-audit --disable-pip --requirement requirements-mcp.lock
  one unique advisory, repeated in two rows: setuptools 81.0.0 / PYSEC-2026-3447

high-signal current-tree scan: clean
high-signal full-Git-history patch scan: clean
final-film source, package, subtitle, and frame review: clean
```

The high-signal scans covered common AWS, GitHub, OpenAI-style secret prefixes
and private-key headers without printing candidate values. They are not a
substitute for provider-side secret scanning; `gitleaks` was unavailable on this
host.

The final-film check also scanned high-confidence secret-value patterns across
the source, SRT, provenance, and upload package, and visually reviewed contact
sheets sampled every two seconds. It found no actionable leak or stale claim.
The literal `pii_exposure` and `live writeback was captured` source hits are
negative assertions in the forbidden-output and forbidden-claim contracts.

The independent receipt/security review also ran focused state-machine, CLI,
public-web, release-contract, and command-boundary tests. Reader credential
values were never printed. Production authorization denial is tested only on the
disposable `sidq.receipt.authorization_probe` dataset, as specified in
[`SECURITY.md`](../SECURITY.md#verification), never on production metadata.

## Operational controls

- Credentials enter the systemd service through root-owned `LoadCredential`
  files, not command arguments.
- The public origin is loopback-bound and reached through Cloudflare Tunnel.
- The public demo subprocess receives a Reader token only for DataHub-dependent
  fixed commands; the offline gate receives neither the token nor claims DSN.
- Releases are immutable, root-owned directories; activation is one atomic
  symlink move with the prior release retained for rollback.
- `/healthz` reports liveness and release identity; `/readyz` separately proves
  an authenticated read-only DataHub dependency call.

See [`SECURITY.md`](../SECURITY.md) for the full threat boundary, credential
provisioning, Reader mutation-denial probe, and rollback procedure.
