# Sidq landing operations

This runbook covers the judge-facing landing process. The public endpoint exposes
only fixed, read-only demonstrations. DataHub and PostgreSQL remain separately
managed dependencies.

## One-time adoption of a legacy production runtime

Use this procedure only when the currently running production runtime predates
the five compatibility copies and all five copies are absent. It records the
inputs from the immutable release already served by that runtime; it does not
switch `/opt/sidq/current`, replace either runtime, or restart the service. If
even one compatibility copy exists, stop and inspect the mixed state instead of
adopting or rebuilding over it.

```bash
set -euo pipefail
legacy_release=$(readlink -f /opt/sidq/current)
case "$legacy_release" in
  /opt/sidq/releases/*) ;;
  *) echo 'STOP: current does not resolve below /opt/sidq/releases' >&2; exit 1 ;;
esac
sudo test -L /opt/sidq/current
sudo test -d "$legacy_release"
sudo test ! -L "$legacy_release"
sudo test "$(sudo stat -c '%U:%G' "$legacy_release")" = root:root
sudo test -z "$(sudo find "$legacy_release" ! -user root -print -quit)"
sudo test -z "$(sudo find "$legacy_release" -perm /0222 -print -quit)"

compatibility_present=0
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test -f "$legacy_release/$compatibility_input"
  sudo test ! -L "$legacy_release/$compatibility_input"
  if sudo test -e "/opt/sidq/runtime/$compatibility_input" \
      || sudo test -L "/opt/sidq/runtime/$compatibility_input"; then
    compatibility_present=$((compatibility_present + 1))
  fi
done
if ! sudo test "$compatibility_present" -eq 0; then
  echo 'STOP: compatibility copies are present; adoption requires all five absent' >&2
  exit 1
fi

sudo test -d /opt/sidq/runtime
sudo test ! -L /opt/sidq/runtime
sudo test "$(sudo stat -c '%U:%G' /opt/sidq/runtime)" = root:root
# The immutable release is a-w; the root-owned legacy runtime may retain owner
# write permission, but non-symlink entries must not be writable by their group
# or other users. Internal venv symlinks have no effective write mode.
for runtime_dir in venv mcp; do
  sudo test -d "/opt/sidq/runtime/$runtime_dir"
  sudo test ! -L "/opt/sidq/runtime/$runtime_dir"
  sudo test "$(sudo stat -c '%U:%G' "/opt/sidq/runtime/$runtime_dir")" = root:root
  sudo test -z \
    "$(sudo find "/opt/sidq/runtime/$runtime_dir" ! -user root -print -quit)"
  sudo test -z \
    "$(sudo find "/opt/sidq/runtime/$runtime_dir" ! -type l -perm /0022 -print -quit)"
done
sudo test -f /opt/sidq/runtime/venv/.sidq-dev-lock
sudo test ! -L /opt/sidq/runtime/venv/.sidq-dev-lock
sudo test -x /opt/sidq/runtime/venv/bin/python
sudo test -x /opt/sidq/runtime/mcp/bin/python
sudo test -x /opt/sidq/runtime/mcp/bin/mcp-server-datahub
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test /opt/sidq/runtime/venv/.sidq-dev-lock \
    -nt "$legacy_release/$compatibility_input"
done

sudo /opt/sidq/runtime/venv/bin/python -m pip check
sudo /opt/sidq/runtime/venv/bin/python -m pip install \
  --dry-run --no-index --require-hashes --no-deps --quiet --report - \
  -r "$legacy_release/requirements-landing.lock" \
  | sudo /opt/sidq/runtime/venv/bin/python -c \
    'import json, sys; raise SystemExit(bool(json.load(sys.stdin)["install"]))'
sudo /opt/sidq/runtime/venv/bin/python -c \
  'import sidq, sentence_transformers; print(sidq.__name__, sentence_transformers.__version__)'
sudo /opt/sidq/runtime/mcp/bin/python -m pip check
sudo /opt/sidq/runtime/mcp/bin/python -m pip install \
  --dry-run --no-index --require-hashes --no-deps --quiet --report - \
  -r "$legacy_release/requirements-mcp.lock" \
  | sudo /opt/sidq/runtime/mcp/bin/python -c \
    'import json, sys; raise SystemExit(bool(json.load(sys.stdin)["install"]))'
sudo /opt/sidq/runtime/mcp/bin/mcp-server-datahub --help >/dev/null
systemctl is-active --quiet sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz

for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo install -o root -g root -m 0444 \
    "$legacy_release/$compatibility_input" \
    "/opt/sidq/runtime/$compatibility_input"
  sudo cmp --silent "$legacy_release/$compatibility_input" \
    "/opt/sidq/runtime/$compatibility_input"
done
```

Any failed marker, executable, package-lock, ownership, liveness, or readiness
probe means the inputs do not match the live legacy runtime. Do not create the
copies in that case; rebuild requires an already recorded runtime and is not a
fallback for an unverified legacy runtime. After successful adoption, the
normal release, rollback, and rebuild procedures can use the recorded copies.

## Rebuild the production runtime

The shared runtime records the exact application and MCP locks it was built
from. If either compatibility check below stops a release or rollback, set
`runtime_release` to that immutable target release and rebuild both production
environments during a maintenance window. If all five compatibility copies are
absent for the existing legacy runtime, complete the one-time adoption above
first. A partial set is an inconsistent state that must be inspected, not
adopted or rebuilt over.

```bash
set -euo pipefail
runtime_release=/opt/sidq/releases/FULL_40_CHARACTER_SHA
case "$runtime_release" in
  /opt/sidq/releases/*) ;;
  *) echo 'STOP: runtime release is outside /opt/sidq/releases' >&2; exit 1 ;;
esac
sudo test -d "$runtime_release"
sudo test ! -L "$runtime_release"
sudo test -f "$runtime_release/requirements-landing.lock"
sudo test -f "$runtime_release/requirements-mcp.lock"
sudo test -d /opt/sidq/runtime
sudo test ! -L /opt/sidq/runtime
sudo test -d /opt/sidq/runtime/venv
sudo test ! -L /opt/sidq/runtime/venv
sudo test -d /opt/sidq/runtime/mcp
sudo test ! -L /opt/sidq/runtime/mcp
sudo test ! -e /opt/sidq/runtime/venv.next
sudo test ! -e /opt/sidq/runtime/mcp.next
sudo test ! -e /opt/sidq/runtime/venv.previous
sudo test ! -e /opt/sidq/runtime/mcp.previous
sudo test ! -e /opt/sidq/runtime/compatibility.previous
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test -f "/opt/sidq/runtime/$compatibility_input"
  sudo test ! -L "/opt/sidq/runtime/$compatibility_input"
done

sudo python3.12 -m venv /opt/sidq/runtime/venv.next
sudo /opt/sidq/runtime/venv.next/bin/python -m pip install \
  --require-hashes -r "$runtime_release/requirements-landing.lock"
sudo /opt/sidq/runtime/venv.next/bin/python -m pip install \
  --no-build-isolation --no-deps "$runtime_release"
sudo /opt/sidq/runtime/venv.next/bin/python -c \
  'import sidq, sentence_transformers; print(sidq.__name__, sentence_transformers.__version__)'
sudo touch /opt/sidq/runtime/venv.next/.sidq-dev-lock

sudo python3.12 -m venv /opt/sidq/runtime/mcp.next
sudo /opt/sidq/runtime/mcp.next/bin/python -m pip install \
  --require-hashes -r "$runtime_release/requirements-mcp.lock"
sudo /opt/sidq/runtime/mcp.next/bin/mcp-server-datahub --help >/dev/null

sudo chown -R root:root /opt/sidq/runtime/venv.next /opt/sidq/runtime/mcp.next
sudo chmod -R a+rX,a-w /opt/sidq/runtime/venv.next /opt/sidq/runtime/mcp.next
sudo systemctl stop sidq-landing
sudo install -d -o root -g root -m 0755 \
  /opt/sidq/runtime/compatibility.previous
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo install -o root -g root -m 0444 \
    "/opt/sidq/runtime/$compatibility_input" \
    "/opt/sidq/runtime/compatibility.previous/$compatibility_input"
done
sudo chmod -R a+rX,a-w /opt/sidq/runtime/compatibility.previous
sudo mv -T /opt/sidq/runtime/venv /opt/sidq/runtime/venv.previous
sudo mv -T /opt/sidq/runtime/mcp /opt/sidq/runtime/mcp.previous
sudo mv -T /opt/sidq/runtime/venv.next /opt/sidq/runtime/venv
sudo mv -T /opt/sidq/runtime/mcp.next /opt/sidq/runtime/mcp
sudo install -o root -g root -m 0444 \
  "$runtime_release/requirements-landing.lock" \
  /opt/sidq/runtime/requirements-landing.lock
sudo install -o root -g root -m 0444 \
  "$runtime_release/requirements-mcp.lock" \
  /opt/sidq/runtime/requirements-mcp.lock
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock; do
  sudo install -o root -g root -m 0444 \
    "$runtime_release/$compatibility_input" \
    "/opt/sidq/runtime/$compatibility_input"
done
```

Leave the service stopped, rerun the applicable release or rollback procedure,
and verify its health checks. Keep `venv.previous` and `mcp.previous` until that
verification succeeds; they are recovery evidence and must be inspected before
a later rebuild if either path still exists.

## Recover or retire a swapped runtime

If activation or its health checks fail, restore the previous runtime and the
immutable release whose recorded inputs match it. Set `recovery_release` to that
known-good release; do not infer it from the failed target:

```bash
set -euo pipefail
recovery_release=/opt/sidq/releases/FULL_40_CHARACTER_SHA
case "$recovery_release" in
  /opt/sidq/releases/*) ;;
  *) echo 'STOP: recovery release is outside /opt/sidq/releases' >&2; exit 1 ;;
esac
sudo test -d "$recovery_release"
sudo test ! -L "$recovery_release"
sudo test "$(sudo stat -c '%U:%G' "$recovery_release")" = root:root
sudo test -z "$(sudo find "$recovery_release" -perm /0222 -print -quit)"
for runtime_dir in venv mcp venv.previous mcp.previous; do
  sudo test -d "/opt/sidq/runtime/$runtime_dir"
  sudo test ! -L "/opt/sidq/runtime/$runtime_dir"
  sudo test "$(sudo stat -c '%U:%G' "/opt/sidq/runtime/$runtime_dir")" = root:root
done
sudo test -d /opt/sidq/runtime/compatibility.previous
sudo test ! -L /opt/sidq/runtime/compatibility.previous
sudo test ! -e /opt/sidq/runtime/venv.failed
sudo test ! -e /opt/sidq/runtime/mcp.failed
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test -f \
    "/opt/sidq/runtime/compatibility.previous/$compatibility_input"
  sudo cmp --silent "$recovery_release/$compatibility_input" \
    "/opt/sidq/runtime/compatibility.previous/$compatibility_input"
done

sudo systemctl stop sidq-landing
next_link=/opt/sidq/current.next
sudo test ! -e "$next_link"
sudo test ! -L "$next_link"
sudo ln -s "releases/$(basename "$recovery_release")" "$next_link"
sudo mv -Tf "$next_link" /opt/sidq/current
sudo mv -T /opt/sidq/runtime/venv /opt/sidq/runtime/venv.failed
sudo mv -T /opt/sidq/runtime/mcp /opt/sidq/runtime/mcp.failed
sudo mv -T /opt/sidq/runtime/venv.previous /opt/sidq/runtime/venv
sudo mv -T /opt/sidq/runtime/mcp.previous /opt/sidq/runtime/mcp
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo install -o root -g root -m 0444 \
    "/opt/sidq/runtime/compatibility.previous/$compatibility_input" \
    "/opt/sidq/runtime/$compatibility_input"
done
sudo systemctl restart sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
```

After either the new activation or the recovery above passes both probes, first
verify the retired trees, then remove only the applicable pair. After a
successful new activation, run:

```bash
set -euo pipefail
systemctl is-active --quiet sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
for retired_dir in venv.previous mcp.previous compatibility.previous; do
  sudo test -d "/opt/sidq/runtime/$retired_dir"
  sudo test ! -L "/opt/sidq/runtime/$retired_dir"
  sudo test "$(sudo stat -c '%U:%G' "/opt/sidq/runtime/$retired_dir")" = root:root
  sudo test -z \
    "$(sudo find "/opt/sidq/runtime/$retired_dir" -perm /0222 -print -quit)"
done
sudo rm -rf -- /opt/sidq/runtime/venv.previous
sudo rm -rf -- /opt/sidq/runtime/mcp.previous
sudo rm -rf -- /opt/sidq/runtime/compatibility.previous
```

After a successful recovery, retire the failed replacement with the same
fail-closed checks:

```bash
set -euo pipefail
systemctl is-active --quiet sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
for retired_dir in venv.failed mcp.failed compatibility.previous; do
  sudo test -d "/opt/sidq/runtime/$retired_dir"
  sudo test ! -L "/opt/sidq/runtime/$retired_dir"
  sudo test "$(sudo stat -c '%U:%G' "/opt/sidq/runtime/$retired_dir")" = root:root
  sudo test -z \
    "$(sudo find "/opt/sidq/runtime/$retired_dir" -perm /0222 -print -quit)"
done
sudo rm -rf -- /opt/sidq/runtime/venv.failed
sudo rm -rf -- /opt/sidq/runtime/mcp.failed
sudo rm -rf -- /opt/sidq/runtime/compatibility.previous
```

Never delete a path that fails an ownership, symlink, or writability check.

## Release

Build the release from an exact clean commit, never by copying the mutable
checkout. Validate the currently deployed immutable release before changing the
symlink. The lock copies under `/opt/sidq/runtime` are the compatibility evidence
for the shared runtime. If all five copies are absent for the existing legacy
runtime, complete the one-time adoption before starting this procedure; never
continue with a partial set. Every release occupies `/opt/sidq/releases/<SHA>`.

```bash
set -euo pipefail
source_repo=/home/dev/sidq-public
cd "$source_repo"
release_sha=$(git rev-parse HEAD)
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo 'STOP: release SHA is not a full 40-character commit id' >&2
  exit 1
}
test -z "$(git status --porcelain=v1)" || {
  echo 'STOP: the source checkout is dirty; no release was created' >&2
  exit 1
}
make check

previous_release=$(readlink -f /opt/sidq/current)
case "$previous_release" in
  /opt/sidq/releases/*) ;;
  *) echo 'STOP: current does not resolve below /opt/sidq/releases' >&2; exit 1 ;;
esac
sudo test -d "$previous_release"
sudo test ! -L "$previous_release"

release_dir="/opt/sidq/releases/$release_sha"
sudo test -d /opt/sidq
sudo test ! -L /opt/sidq
sudo install -d -o root -g root -m 0755 /opt/sidq/releases
sudo test ! -L /opt/sidq/releases
staging_dir=$(sudo mktemp -d "/opt/sidq/releases/.${release_sha}.XXXXXX")
case "$staging_dir" in
  /opt/sidq/releases/."$release_sha".*) ;;
  *) echo 'STOP: staging path is outside the expected release prefix' >&2; exit 1 ;;
esac
git archive "$release_sha" | sudo tar -x -C "$staging_dir"
sudo chown -R root:root "$staging_dir"
sudo chmod -R a-w,a+rX "$staging_dir"
if sudo test -e "$release_dir" || sudo test -L "$release_dir"; then
  sudo test -d "$release_dir"
  sudo test ! -L "$release_dir"
  sudo test "$(sudo stat -c '%U:%G' "$release_dir")" = root:root
  sudo test -z "$(sudo find "$release_dir" ! -user root -print -quit)"
  sudo test -z "$(sudo find "$release_dir" -perm /0222 -print -quit)"
  if ! sudo diff --recursive --brief --no-dereference \
      "$staging_dir" "$release_dir"; then
    echo 'STOP: prepared release does not exactly match the requested commit' >&2
    echo "Inspect the staged comparison tree at $staging_dir" >&2
    exit 1
  fi
  sudo rm -rf -- "$staging_dir"
else
  sudo mv -T "$staging_dir" "$release_dir"
fi
sudo test -d "$release_dir"
sudo test ! -L "$release_dir"
sudo test "$(sudo stat -c '%U:%G' "$release_dir")" = root:root
sudo test -z "$(sudo find "$release_dir" ! -user root -print -quit)"
sudo test -z "$(sudo find "$release_dir" -perm /0222 -print -quit)"

for dependency_input in requirements-dev.lock pyproject.toml uv.lock; do
  sudo test -f "$release_dir/$dependency_input"
  sudo test ! -L "$release_dir/$dependency_input"
  sudo test -f "$previous_release/$dependency_input"
  sudo test ! -L "$previous_release/$dependency_input"
done
if ! sudo cmp --silent "$release_dir/requirements-dev.lock" \
    "$previous_release/requirements-dev.lock" \
  || ! sudo cmp --silent "$release_dir/pyproject.toml" \
    "$previous_release/pyproject.toml" \
  || ! sudo cmp --silent "$release_dir/uv.lock" \
    "$previous_release/uv.lock"; then
  echo 'NOTICE: dependency metadata differs from the current release' >&2
fi
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test -f "$release_dir/$compatibility_input"
  sudo test ! -L "$release_dir/$compatibility_input"
  sudo test -f "/opt/sidq/runtime/$compatibility_input"
  sudo test ! -L "/opt/sidq/runtime/$compatibility_input"
done
if ! sudo cmp --silent "$release_dir/requirements-dev.lock" \
    /opt/sidq/runtime/requirements-dev.lock \
  || ! sudo cmp --silent "$release_dir/pyproject.toml" \
    /opt/sidq/runtime/pyproject.toml \
  || ! sudo cmp --silent "$release_dir/uv.lock" \
    /opt/sidq/runtime/uv.lock \
  || ! sudo cmp --silent "$release_dir/requirements-landing.lock" \
    /opt/sidq/runtime/requirements-landing.lock \
  || ! sudo cmp --silent "$release_dir/requirements-mcp.lock" \
    /opt/sidq/runtime/requirements-mcp.lock; then
  echo 'STOP: dependency inputs changed; do not switch, touch, or restart' >&2
  echo 'Rebuild for this release using the production-runtime procedure, then retry' >&2
  echo 'Do not use docs/SETUP.md to rebuild the production runtime' >&2
  exit 1
fi

next_link=/opt/sidq/current.next
if sudo test -e "$next_link" || sudo test -L "$next_link"; then
  echo 'STOP: /opt/sidq/current.next already exists; inspect it first' >&2
  exit 1
fi
sudo ln -sfn "releases/$release_sha" "$next_link"
sudo mv -Tf "$next_link" /opt/sidq/current
sudo test "$(readlink -f /opt/sidq/current)" = "$release_dir"

sudo touch /opt/sidq/runtime/venv/.sidq-dev-lock
sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/requirements-dev.lock \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/pyproject.toml \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/uv.lock \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt \
    /opt/sidq/current/requirements-landing.lock \
  && sudo systemctl restart sidq-landing
```

The byte comparisons must succeed before the symlink moves. A mismatch means the
existing runtime is not evidence for the new release: stop without touching the
marker or restarting, follow **Rebuild the production runtime** with
`runtime_release="$release_dir"`, and rerun the release procedure. The later
timestamp check only protects Make's marker contract; it is not a substitute
for the recorded-input content comparisons.

Verify liveness, dependency readiness, and the public TLS route:

```bash
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
curl --fail --silent https://sidq.mlki.app/healthz
systemctl is-active sidq-landing
```

`/healthz` proves that the landing process can serve requests without consulting
another service. `/readyz` separately reports whether its live DataHub dependency
answers. A degraded readiness result should not be mistaken for a dead landing
process.

## Configuration and logs

The committed unit is `deploy/sidq-landing.service`. Keep secrets exclusively in
the root-owned credential files loaded by its `LoadCredential=` directives:
`/etc/sidq/datahub-reader.token` and `/etc/sidq/claims.dsn`. Do not put secrets in
the repository, the unit, a drop-in, or an `EnvironmentFile=`.

Use a systemd drop-in only for non-secret operator overrides. For production,
keep the public origin pinned to `https://sidq.mlki.app`; local development
defaults must never broaden the deployed origin policy. Run:

```bash
sudo systemctl edit sidq-landing
```

Save the following exact drop-in (systemd writes it as
`/etc/systemd/system/sidq-landing.service.d/override.conf`):

```ini
[Service]
Environment=DATAHUB_GMS_URL=http://127.0.0.1:8080
Environment=SIDQ_ALLOWED_ORIGINS=https://sidq.mlki.app
```

Then apply the non-secret override and restart the process:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sidq-landing
journalctl -u sidq-landing --since "15 minutes ago" --no-pager
```

The service runs without additional capabilities, with a read-only home and
system, a private temporary directory, and CPU and memory ceilings. A run also has
an application timeout, bounded output, one-at-a-time execution, and a per-command
cooldown.

## Rollback

Use the known-good SHA captured before release. The target must be an existing
root-owned immutable release, not a checkout or a symlink. Validate the current
release, then compare the target's dependency inputs with the runtime's recorded
copies before moving `/opt/sidq/current`.

```bash
set -euo pipefail
rollback_sha=KNOWN_GOOD_SHA
[[ "$rollback_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo 'STOP: rollback SHA must be the full 40-character commit id' >&2
  exit 1
}
target_release="/opt/sidq/releases/$rollback_sha"
sudo test -d /opt/sidq
sudo test ! -L /opt/sidq
sudo test -d /opt/sidq/releases
sudo test ! -L /opt/sidq/releases
sudo test -d "$target_release"
sudo test ! -L "$target_release"
sudo test "$(sudo stat -c '%U:%G' "$target_release")" = root:root
sudo test -z "$(sudo find "$target_release" -perm /0222 -print -quit)"

runtime_compatible_release=$(readlink -f /opt/sidq/current)
case "$runtime_compatible_release" in
  /opt/sidq/releases/*) ;;
  *) echo 'STOP: current does not resolve below /opt/sidq/releases' >&2; exit 1 ;;
esac
sudo test -d "$runtime_compatible_release"
sudo test ! -L "$runtime_compatible_release"

for dependency_input in requirements-dev.lock pyproject.toml uv.lock; do
  sudo test -f "$target_release/$dependency_input"
  sudo test ! -L "$target_release/$dependency_input"
  sudo test -f "$runtime_compatible_release/$dependency_input"
  sudo test ! -L "$runtime_compatible_release/$dependency_input"
done
if ! sudo cmp --silent "$target_release/requirements-dev.lock" \
    "$runtime_compatible_release/requirements-dev.lock" \
  || ! sudo cmp --silent "$target_release/pyproject.toml" \
    "$runtime_compatible_release/pyproject.toml" \
  || ! sudo cmp --silent "$target_release/uv.lock" \
    "$runtime_compatible_release/uv.lock"; then
  echo 'NOTICE: rollback metadata differs from the current release' >&2
fi
for compatibility_input in requirements-dev.lock pyproject.toml uv.lock \
    requirements-landing.lock requirements-mcp.lock; do
  sudo test -f "$target_release/$compatibility_input"
  sudo test ! -L "$target_release/$compatibility_input"
  sudo test -f "/opt/sidq/runtime/$compatibility_input"
  sudo test ! -L "/opt/sidq/runtime/$compatibility_input"
done
if ! sudo cmp --silent "$target_release/requirements-dev.lock" \
    /opt/sidq/runtime/requirements-dev.lock \
  || ! sudo cmp --silent "$target_release/pyproject.toml" \
    /opt/sidq/runtime/pyproject.toml \
  || ! sudo cmp --silent "$target_release/uv.lock" \
    /opt/sidq/runtime/uv.lock \
  || ! sudo cmp --silent "$target_release/requirements-landing.lock" \
    /opt/sidq/runtime/requirements-landing.lock \
  || ! sudo cmp --silent "$target_release/requirements-mcp.lock" \
    /opt/sidq/runtime/requirements-mcp.lock; then
  echo 'STOP: rollback dependency inputs do not match the shared runtime' >&2
  echo 'Rebuild for the target using the production-runtime procedure first' >&2
  echo 'Do not use docs/SETUP.md to rebuild the production runtime' >&2
  exit 1
fi

next_link=/opt/sidq/current.next
if sudo test -e "$next_link" || sudo test -L "$next_link"; then
  echo 'STOP: /opt/sidq/current.next already exists; inspect it first' >&2
  exit 1
fi
sudo ln -sfn "releases/$rollback_sha" "$next_link"
sudo mv -Tf "$next_link" /opt/sidq/current
sudo test "$(readlink -f /opt/sidq/current)" = "$target_release"

sudo touch /opt/sidq/runtime/venv/.sidq-dev-lock
sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/requirements-dev.lock \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/pyproject.toml \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt /opt/sidq/current/uv.lock \
  && sudo test /opt/sidq/runtime/venv/.sidq-dev-lock -nt \
    /opt/sidq/current/requirements-landing.lock \
  && sudo systemctl restart sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
```

If compatibility fails, do not switch the symlink, touch the marker, or restart.
Follow **Rebuild the production runtime** with
`runtime_release="$target_release"`, then rerun this procedure. A compatible
rollback switches only the deployment symlink and does not rewrite the source
checkout or discard local changes.
