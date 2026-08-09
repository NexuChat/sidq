# SIDQ RECEIPT SPEC — implemented receipt contract

Verified facts: `docs/RECON.md`.

The receipt is the answer to judging criterion #1 ("depth of DataHub use **including
writing back to the graph**"). It fails that criterion the moment it becomes decorative.
Two properties make it non-decorative: it is **queryable**, and it is **read by someone
who is not us**.

---

## 1. Vehicle — through the official MCP server, not a side channel

`mcp-server-datahub` v0.6.0 exposes mutation tools behind `TOOLS_IS_MUTATION_ENABLED=true`.
We use the required component to both read and write. Three parts, each with a job:

| Part | Tool | Job |
|---|---|---|
| **Queryable body** | `add_structured_properties` / `remove_structured_properties` | machine-readable facts, filterable in DataHub search |
| **Visible badge** | `add_tags` / `remove_tags` | exactly one of `sidq:verified` / `sidq:blocked` — renders in the UI, screenshots well, one glance |
| **Full evidence** | `save_document` | the human-readable receipt: rules fired, evidence, links |

**Gotcha — do this first:** structured properties must be *defined* as entities before any
value can be set on an asset. Wave 3 starts by creating the definitions once, idempotently,
and `demo/` or `scripts/` must carry that bootstrap so a judge's fresh environment works.
If structured properties prove awkward, the documented fallback is
`DataHubGraph` + `DatasetPatchBuilder.set_custom_properties()` + `emit()` — but try
structured properties first; they are searchable and the custom-properties path is legacy.

## 2. Schema — `sidq.*` structured properties

| Property | Type | Value |
|---|---|---|
| `sidq.verdict` | string, enum | `PASS` · `WARN` · `BLOCK` |
| `sidq.reason_code` | string | e.g. `STALE_CONTEXT`, empty on PASS |
| `sidq.commit_sha` | string | the exact commit the verdict was computed on |
| `sidq.checked_at` | string | ISO-8601 UTC |
| `sidq.policy_hash` | string | sha256 of the policy file actually used |
| `sidq.context_hash` | string | sha256 of semantic entity metadata plus complete immediate lineage |
| `sidq.rules_fired` | string, multiple | rule ids, sorted |
| `sidq.verifier` | string | `sidq@<version>` |
| `sidq.evidence_url` | string | link to the PR comment / document |

`policy_hash` + `commit_sha` are **provenance**: they identify which policy and which
code revision produced the captured verdict. A verdict is byte-reproducible only with
the captured diff, graph/context snapshot, policy, configuration, code and tool
versions, and canonical serialization — these two hashes alone are not enough.
They do not sign the Receipt, make it tamper-proof, or prove that the catalog has
not changed. The independently recomputed `context_hash`, policy comparison, and
age check determine whether the latest Receipt still applies.

### Native assertion mirror (explicit opt-in)

`sidq audit --via-mcp --write-receipts --write-assertions` additionally reports each
evidence-backed Sidq rule evaluation as a native DataHub Assertion. Its stable assertion
URN is derived from the dataset URN and rule id; `assertionInfo` records the rule and
external Sidq source, and `assertionRunEvent` records the verdict, policy hash, commit,
check time, and evidence summary. This makes the result visible in DataHub's own
Validation/Quality surface without changing the receipt's `sidq.*` contract.

The option is off by default and requires `--write-receipts`; an assertion reports an
already-determined verdict and is never input to Sidq's deterministic judgment.

Each run event reports **one rule by that rule's own severity**, not by the receipt's
verdict: `warn` and `block` did not pass and take `FAILURE`, everything else takes
`SUCCESS`. A BLOCK receipt therefore does not paint its `info` findings red. Only when
a receipt carries no per-rule evidence at all is the whole verdict reported once, under
`sidq.verdict`, and then the receipt verdict decides. `AssertionResultType` also has
`INIT` and `ERROR`, but those describe a run that started or could not finish; every
verdict Sidq publishes is a completed evaluation.

### Retiring a rule that stopped firing

DataHub keeps every assertion ever written, so a rule that fires once and is then
fixed would go on stating a failure Sidq no longer holds. Each run therefore closes
what it no longer reports: after emitting the current rules for a dataset, Sidq lists
the assertions asserting on it, keeps the ones it wrote itself, and emits one closing
`SUCCESS` run event — `sidq.severity=retired`, summary "This rule did not fire in the
latest Sidq evaluation." — for any that this run did not evaluate. Nothing is deleted;
the history stays readable and the latest event tells the truth.

Two things are deliberately left alone. An assertion Sidq did not write (a dbt or
Snowflake check on the same dataset) is somebody else's claim. And a soft-deleted
assertion stays deleted: DataHub keeps the `Asserts` relationship after a soft delete,
so retiring one would emit a fresh run event and pull an assertion the operator removed
back into their Quality tab. Measured 2026-08-09, before the filter existed, that is
exactly what happened.

### Removing an assertion Sidq wrote

`deleteAssertion` refuses a `CUSTOM` assertion — measured 2026-08-09,
`Unsupported Assertion Type CUSTOM provided`. The soft-delete path accepts it and the
assertion leaves the tab:

```graphql
mutation { batchUpdateSoftDeleted(input: {urns: ["urn:li:assertion:sidq-..."], deleted: true}) }
```

### What this surface still does not do

**A WARN reads as failing in DataHub's aggregate.** The run event carries
`sidq.verdict=WARN` and `sidq.severity`, but the tab's summary chip counts passing
against failing with no third state, so a warning is counted with the failures. The
distinction survives in the run event; it does not survive in the chip. Sidq cannot fix
this from its side without reporting a warning as a pass, which would be the worse lie.

This is the one explicit SDK exception to the receipt writeback route. The official
`mcp-server-datahub` mutation tools include no assertion tool, so definition and run
event emission use `DataHubGraph`, `DatahubClientConfig`, and
`MetadataChangeProposalWrapper`, following the receipt bootstrap boundary. Receipt
values themselves continue through official MCP tools.

That exception has a cost, and it is stated rather than hidden. `acryl-datahub`
is not a Sidq dependency and is not offered as an extra: measured with pip on
2026-08-09, `acryl-datahub==1.6.0.16` resolves `pydantic` to 2.11.10, while
`mcp 2.0.0` — the client every other Sidq command reads through — declares
`pydantic>=2.12.0`, and pip reports the conflict.

The distinction matters, so it is drawn precisely. In a throwaway environment
holding both, Sidq's own MCP suites — 64 tests across `test_mcp_snapshot.py`
and `test_mcp_server.py` — passed under `pydantic` 2.11.10. So this is an install whose *declared*
constraints are unsatisfied, not one observed to break. The full `--via-mcp …
--write-assertions` CLI invocation has not been run end to end against a live
catalog; what is proven is the emission path, called directly. Sidq still declines to ship it as an extra, because an environment that
happens to work while contradicting its own metadata is not something to put
in a judge's or an operator's install path; the next patch release owes it
nothing.

`--write-assertions` therefore runs only from an interpreter that already
carries the SDK. Everywhere else it raises `DataHubSDKUnavailable` naming that
conflict, and it raises it as a precondition — before the catalog is read and
before any receipt is written — because an operator who reaches this path needs
the reason up front, not a `ModuleNotFoundError` after a spent budget. Nothing
else in Sidq is affected: receipts, verification, and every read path need no
SDK at all.

`examples/06-native-assertion/` holds the live run: the emitted assertion, the
verbatim GraphQL response from the same query DataHub's UI issues, and a second
emission returning `created=0, existing=1` to show the same assertion is
rewritten rather than duplicated.

## 3. Consumption — `sidq verify <urn>`

Receipt consumption is a CLI operation, not a fourth Sidq MCP tool. Run it from
a process separate from the writer:

```bash
sidq verify 'urn:li:dataset:(urn:li:dataPlatform:postgres,example.table,PROD)' --json
```

It reads the latest receipt through the official DataHub MCP dependency and
returns:

```json
{
  "urn": "...",
  "verdict": "PASS",
  "reason_code": null,
  "commit_sha": "9f2c1ab",
  "checked_at": "2026-08-02T11:04:00Z",
  "policy_hash": "sha256:...",
  "context_hash": "sha256:...",
  "rules_fired": [],
  "stale": true,
  "stale_reason": "asset decision context changed",
  "receipt_state": "STALE",
  "action": "RECHECK",
  "covers_asset": false,
  "judgment": "receipt is stale: asset decision context changed"
}
```

### Three questions, three fields

A receipt is asked three different things, and Sidq answers them separately
because the answers genuinely differ. Collapsing them into one boolean is how a
refusal came to be printed as "nobody checked", and how a refused asset came to
be re-examined every run while the rest of the catalog went unread.

| Field | Question | Values |
|---|---|---|
| `receipt_state` | Does this receipt apply right now? | `CURRENT` · `STALE` · `ABSENT` · `INVALID` |
| `verdict` | What did the engine decide? | `PASS` · `WARN` · `BLOCK` |
| `action` | What may the reader do? | `CONTINUE` · `REVIEW_OR_ESCALATE` · `STOP` · `RECHECK` |
| `covers_asset` | Has the asset been examined under conditions that still hold? | `true` · `false` |

`receipt_state` is decided **before** the verdict is read, so a refusal that has
gone stale is `STALE`, not a standing `BLOCK`. Applicability then fixes the rest:

| State + verdict | `action` | `covers_asset` | Rendered as |
|---|---|---|---|
| `CURRENT` + `PASS` | `CONTINUE` | `true` | `CURRENT RECEIPT · PASS · CONTINUE` |
| `CURRENT` + `WARN` | `REVIEW_OR_ESCALATE` | `true` | `CURRENT RECEIPT · WARN · REVIEW_OR_ESCALATE` |
| `CURRENT` + `BLOCK` | `STOP` | `true` | `CURRENT RECEIPT · BLOCK · STOP` |
| `STALE` / `ABSENT` / `INVALID` | `RECHECK` | `false` | `NOT VERIFIED` |

Two consequences are load-bearing:

- **A current `BLOCK` covers its asset.** "We checked and refused" is knowledge,
  not a gap, so `sidq audit --resume` and the swarm move their budget on instead
  of re-deriving the same refusal forever. It authorizes nothing: `action` stays
  `STOP`.
- **`NOT VERIFIED` means only absent, stale, or unreadable.** It is a reader
  state, never a fourth verdict, and never the label on a receipt that says
  `BLOCK`.

`INVALID` covers a receipt this engine did not write — an unrecognised verdict —
and one whose freshness could not be established at all, such as a payload
missing the computed staleness marker. Neither buys coverage, so a hostile or
broken catalog cannot retire an asset from the audit queue by writing nonsense.

Exit codes follow `action`: `0` for `CONTINUE` and `REVIEW_OR_ESCALATE`, `1` for
`STOP` and `RECHECK`, and `2` when the catalog could not be read at all. `STOP`
and `RECHECK` share an exit code because a script's only safe move is the same
in both cases; the printed headline is what tells them apart.

**Receipts expire — this is the point.** `stale` is computed, never stored:

- a policy-hash mismatch invalidates immediately, **or**
- the current semantic entity metadata or complete one-hop upstream and downstream
  lineage differs from the recorded context hash, **or**
- missing, partial, or error context is stale (fail-closed), **or**
- `checked_at` is missing or invalid, **or**
- `checked_at` is older than the configured limit. The CLI default maximum age is
  7 days.

Sidq's own receipt properties, badges, and evidence documents are excluded from
the context hash so a receipt does not invalidate itself. The hosted public
handoff alone uses 45 days solely to span judging through August 31, 2026; any
context or policy change still invalidates immediately.

So an analytics agent asking "is this asset verified?" gets a real answer — *"verified at
commit 9f2c1ab, but it has changed since"* — instead of a badge that means nothing. A
receipt that cannot go stale is a sticker.

## 4. Demo obligation (DECISION §6, scene 4)

The PASS receipt must be **visible in the DataHub UI** (that is what the tag buys us), and
a *different* process must then run `sidq verify` and change its behaviour
because of the answer. Not a log line — a behaviour change: it declines, or it warns, or it
picks a different asset. If the demo cannot show a third party acting on the receipt, the
receipt is decorative and criterion #1 is only half won.

Sidq's own `sidq-mcp` server exposes exactly three tools: `check_change`,
`verify_context`, and `search_verified`. None is named as a receipt-status tool.

## 5. Hard rules

- Writeback is attempted **after** the verdict, never as part of computing it. A
  human-readable document is saved first, the visible badge is applied second,
  and the machine-readable structured Receipt is published last because that
  final body is what independent readers trust. If a later mutation or exact
  readback fails, Sidq uses the official remove/add tools to restore the prior
  managed badges and touched `sidq.*` values. The evidence document cannot be
  deleted through this MCP surface and can remain inert. DataHub does not provide
  a transaction across these tools. Same-URN writes are serialized within one
  Sidq process, and compensation refuses to overwrite managed values it does not
  recognize as the prior or attempted state. DataHub exposes no compare-and-swap
  across these tools, so cross-process writers can still race when their states
  are indistinguishable, and a failed compensation call can leave partial state.
  Sidq therefore does not describe the sequence as atomic.
- A mutation acknowledgement is not success: Sidq polls `get_entities` directly
  with bounded backoff until the exact structured Receipt is visible. Timeout or
  mismatch is `write_unconfirmed`, not a written Receipt. No feedback loops.
- In an opted-in writeback run, a `BLOCK` verdict is eligible for a receipt too.
  Recording a refusal is the whole thesis — writing
  only on success would be vanity.
- Receipt writeback holds write permission for the `sidq.*` namespace and Sidq's own
  tags **only**. `sidq repair --apply` is a separate, opt-in surface: it writes tags,
  terms and owners that the engine re-proved on the assets a finding named. Say so in
  the README, and keep the two surfaces named separately — a gate that can silently
  rewrite arbitrary metadata is a liability, and the restraint only counts if the
  boundary is stated accurately.
