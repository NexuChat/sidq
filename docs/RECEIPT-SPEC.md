# SIDQ RECEIPT SPEC — wave 3 (binding)

Authority: the project design contract §3 and §5. Verified facts: `docs/RECON.md` §3.

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
| **Queryable body** | `add_structured_properties` | machine-readable facts, filterable in DataHub search |
| **Visible badge** | `add_tags` | `sidq:verified` / `sidq:blocked` — renders in the UI, screenshots well, one glance |
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
| `sidq.rules_fired` | string, multiple | rule ids, sorted |
| `sidq.verifier` | string | `sidq@<version>` |
| `sidq.evidence_url` | string | link to the PR comment / document |

`policy_hash` + `commit_sha` together make the attestation **reproducible**: anyone can
re-run the same policy on the same commit and get the same verdict, byte for byte. Say
this out loud in the README — it is the difference between an attestation and a sticker.

## 3. Consumption — `get_verification_status(urn)`

Our MCP tool returns, for the asset's latest receipt:

```json
{
  "urn": "...",
  "verdict": "PASS",
  "reason_code": null,
  "commit_sha": "9f2c1ab",
  "checked_at": "2026-08-02T11:04:00Z",
  "policy_hash": "sha256:...",
  "rules_fired": [],
  "stale": true,
  "stale_reason": "asset schema changed after the last verification"
}
```

**Receipts expire — this is the point.** `stale` is computed, never stored:

- the asset's schema `lastModified` in the graph is newer than `checked_at`, **or**
- `checked_at` is older than the configured max age (default 7 days), **or**
- the current `policy_hash` differs from the one recorded on the receipt.

So an analytics agent asking "is this asset verified?" gets a real answer — *"verified at
commit 9f2c1ab, but it has changed since"* — instead of a badge that means nothing. A
receipt that cannot go stale is a sticker.

## 4. Demo obligation (DECISION §6, scene 4)

The PASS receipt must be **visible in the DataHub UI** (that is what the tag buys us), and
a *different* agent must then call `get_verification_status` and change its behaviour
because of the answer. Not a log line — a behaviour change: it declines, or it warns, or it
picks a different asset. If the demo cannot show a third party acting on the receipt, the
receipt is decorative and criterion #1 is only half won.

## 5. Hard rules

- The receipt is written **after** the verdict, never as part of computing it. No feedback loops.
- A `BLOCK` verdict writes a receipt too. Recording a refusal is the whole thesis — writing
  only on success would be vanity.
- sidq holds write permission for the `sidq.*` namespace and its own tags **only**.
  Say so in the README; a gate that can rewrite arbitrary metadata is a liability, and
  judges will notice the restraint.
