# Repository data-check applicability

This repository maintains both a data-verification engine and data examples. A
software dependency update is not a catalog asset. Treating it as a successful
asset check would be false; treating an unknown path as harmless would be unsafe.

The generic composite action and `sidq check` remain strict. Only this
repository's trusted publisher has the applicability boundary below.

## Outcomes

- **Data only:** execute the existing engine with trusted-base policy and recorded
  graph fixtures. Its PASS/WARN/BLOCK decision and fixture provenance are unchanged.
- **Supported non-data maintenance only:** after authenticated software checks,
  publish a neutral `Sidq policy verdict` headed **NOT APPLICABLE — no data-asset
  certification**. No graph verdict or asset receipt is produced. This is not a
  source-code, dependency-safety, or vulnerability certification.
- **Mixed data/non-data, protected or unknown changes:** refuse certification.
  Missing, skipped, unsuccessful, ambiguous, or unverifiable checks also refuse.
  Superseded events publish nothing for a newer revision.

A dependency + documentation + supported test change is wholly non-data. “Mixed”
means data inputs combined with non-data maintenance, not multiple maintenance
categories.

## Trusted scope

Only modifications of existing, tracked, regular files can qualify as maintenance.
New files, deletion/rename, executable-mode changes, unsafe paths, symlinks,
malformed mapping files and uncertain inventories cannot silently expand scope.

Supported paths are deliberately bounded in `src/sidq/bot/applicability.py`:

- `uv.lock` and the five application requirement exports
- README, architecture and contribution documentation, plus existing ordinary
  Markdown documentation outside protected operational/contract documents
- Four existing presentation files: `web/index.html`, `web/scope.html`,
  `web/styles.css`, `web/app.js`
- Five existing maintenance-test modules: dependency security, claim reader,
  reader calibration, datasheet and landing experience

This first version does **not** exempt arbitrary runtime Python. Workflow and
build controls, all engine/enforcement source, scripts, deployments, operational
contracts, signing/security material and enforcement tests require explicit
review. The independently resolved MCP-service lock is also outside this scope.

Explicit asset maps and dbt manifest paths from **both** trees override ordinary
maintenance categories. Both `original_file_path` and the resolver's `path`
fallback are recognized, including top-level asset-map fallback entries. Custom
naming conventions require strict assessment. SQL/data extensions and the data,
demo, example and graph-fixture roots are data inputs. Protected control-plane
paths remain protected even when mapped to an asset.

## Evidence and privilege boundary

`CI / check` is an independent read-only software job. It checks out the immutable
PR head, verifies `git HEAD` equals the event head, installs hash-locked packages,
runs `pip check`, Ruff, formatting, mypy and pytest/coverage. Export tests compare
every exported package/version/hash set with `uv.lock`; they do not claim a
vulnerability audit or prove optional-platform dependency reachability. The
trusted publisher independently rechecks proposed dependency pins and artifact
hashes as bounded data, so editing a PR test cannot remove that prerequisite.

A separate `workflow_run` publisher executes trusted default/base-branch code and
hash-locked dependencies. Proposed files are checked out only as data, after
context validation. It does not execute proposed source, restore PR caches or
consume PR-produced artifacts. The GitHub token is never persisted in checkout.

Authenticated API evidence binds the repository, workflow ID/path, pull-request
event and number, immutable run head, latest run and attempt, mandatory job and
individual successful steps. Same-named checks and a successful workflow summary
alone are insufficient. The current base must be an ancestor of the head; this
ensures the exact tested head contains the trusted base. Missing or ambiguous PR
associations refuse certification rather than guessing.

Changed files come from an **immutable base-SHA...head-SHA comparison**, not the
mutable PR-files endpoint. GitHub's 300-file comparison limit is refused at the
boundary. Large or ambiguous changes need explicit review. Currentness is checked
again before publication. Checks carry both head and trusted-base identifiers.
An in-progress check is created before updating the sticky comment and becomes
neutral/success only after that evidence is published. Partial publication
cannot create a new completed successful check.

## Adoption and PR #6

This change intentionally touches protected controls, so its own draft does not
self-authorize applicability. A maintainer must review and explicitly adopt it on
the default/base branch; this document is not permission to merge or bypass rules.
Keep **both** the software `check` and `Sidq policy verdict` required in repository
rules, and require branches to be up to date before merging. GitHub statuses bind
to a head SHA; a past result is not automatically invalidated merely because its
base advances. This freshness rule is therefore an adoption prerequisite, not an
optional optimization. A maintainer must verify the actual repository rules before
activation; this change does not modify branch protection.

After adoption, refresh PR #6 so its head contains the new base, reconcile its
regression and mechanically checked documentation counts, and obtain a **new**
PR-triggered CI run. Merely rerunning the old failed workflow is insufficient:
it lacks the immutable-head verification step and may use obsolete workflow code.
The refreshed dependency/test/documentation-only diff can receive neutral N/A
only after the new mandatory checks pass and no protected/data changes remain.
The PyJWT lock synchronization already published to PR #6 is preserved separately.

Until that explicit base rollout and refresh, the existing strict fixture bot
continues to report `unresolved_asset`. This draft does not make PR #6 green,
merge it, or deploy anything.
