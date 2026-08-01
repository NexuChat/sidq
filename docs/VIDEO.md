# Final three-minute film runbook

Target length: 2:45. Record a terminal, the live DataHub UI, and the public Sidq
page at 1440x900. This sequence demonstrates write → DataHub inspection →
independent read; do not substitute screenshots or pre-rendered output.

## 00:00-00:20 — the problem

**Picture:** Open `https://sidq.mlki.app`, hold on the thesis, then show the
one-picture architecture.

**Voice:** “Agents increasingly read metadata graphs before they act. But the
graph can be stale, internally contradictory, or missing the evidence an agent
needs. Sidq asks the question first: is this context supported?”

## 00:20-00:55 — Agent A writes a real receipt

**Picture:** In a terminal already configured for the live MCP server, run:

```bash
.venv/bin/python examples/02-receipt-consumed/write_receipt.py
```

Keep the returned `success: true`, `verdict`, `checked_at`, `policy_hash`, and
`evidence_url` visible. This is the only write in the film and must use the
disposable `sidq.receipt.consumed` asset.

**Voice:** “Agent A runs the deterministic policy and writes its receipt through
DataHub's official MCP mutation tools. The response acknowledges structured
properties, a visible status tag, and an evidence document.”

## 00:55-01:25 — inspect what DataHub persisted

**Picture:** Open the disposable dataset in `https://datahub.mlki.app` with the
Reader account. Show the `sidq:verified` tag and the `sidq.*` properties. Open the
evidence document, and visually match its policy hash to the terminal output.

**Voice:** “Now inspect DataHub itself, not the writer's response. The catalog
persisted a queryable verdict, commit, time, policy hash, verifier, and evidence
document. Reader access is enough to inspect all of it.”

## 01:25-01:55 — Agent B performs an independent read

**Picture:** Return to `https://sidq.mlki.app`. Click **Prove the agent handoff —
read receipt memory**. Keep `VERIFIED`, `checked at`, the same policy hash, and the
evidence document visible.

**Voice:** “Agent B is a separate process with no private handoff state. It reads
the persisted receipt, verifies the same semantic entity and its complete one-hop
lineage context, and independently recomputes policy and age freshness before it
returns VERIFIED. The catalog is durable, queryable memory between agents.”

## 01:55-02:25 — refusal with reproducible evidence

**Picture:** Click **Offline verdict**. Hold on `BLOCK`, `pii_exposure`, the 16
consumers, commit SHA, and policy hash. Briefly show the column-level lineage path.

**Voice:** “Receipts do not grant blind trust. For a data-code change, Sidq follows
the changed PII column into sixteen downstream consumers and blocks the proposal.
The committed graph recording, policy, and commit re-derive the same deterministic
verdict. No model participates in permission.”

## 02:25-02:45 — close with scope and proof

**Picture:** Show the GitHub link, CI badge, DataHub link, and the evidence links.
End on “Evidence before confidence.”

**Voice:** “Sidq is Apache-2.0 and usable from CLI, CI, or MCP. The repository
ships hash-locked dependencies, complete compact regression evidence, and the
commands that re-run its claims. Make context provable before an agent acts.”

## Dynamic-output rules

- Receipt time, policy hash, evidence document URN, and tool wording are dynamic.
  Narrate what each field means; never paste an older value over a fresh run.
- A current run may legitimately show a different hash from the historical
  transcript in `examples/02-receipt-consumed/README.md` after policy changes.
- Stop recording if the write is rejected, DataHub does not show the new receipt,
  or Agent B does not independently return `VERIFIED` for the same receipt.
- Do not call the fixture-backed offline verdict a live-graph result.
- Do not speed up terminal output or hide waits with cuts that imply an instant run.

## Captions checklist

- [ ] Captions say “fixture replay” over the offline verdict and “live DataHub”
  over the receipt write, inspection, and read.
- [ ] `Agent A: write`, `DataHub: persisted evidence`, and `Agent B: independent
  read` appear as three distinct captions.
- [ ] Dynamic values in captions match the captured run exactly.
- [ ] The 285 catalog findings, if mentioned, are described as concentrated in 5
  assets after examining 67 datasets.
- [ ] Writeback is described as explicit and optional; the public landing buttons
  are described as read-only.
- [ ] Captions identify model-proposed documentation checks as advisory `WARN`
  evidence, never as blocking verdicts.

## Recording checklist

- [ ] Use no copyrighted music. Silence is acceptable.
- [ ] Keep the cursor visible for every live click.
- [ ] Re-run `make gate-demo` and `make check` before capture; stop if either fails.
- [ ] Verify the Reader credentials are shared only through the Devpost Testing
  instructions field visible to judges, not a public project field.
- [ ] Verify the final public video is viewable without sign-in and remains under
  three minutes.
