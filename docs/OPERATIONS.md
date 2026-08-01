# Sidq landing operations

This runbook covers the judge-facing landing process. The public endpoint exposes
only fixed, read-only demonstrations. DataHub and PostgreSQL remain separately
managed dependencies.

## Release

Build the release from an exact clean commit, never by copying the mutable
checkout. Save the currently deployed immutable release before changing the
symlink; it is the release whose dependency inputs the shared runtime is known to
match. Every release occupies `/opt/sidq/releases/<SHA>`.

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
if sudo test -e "$release_dir" || sudo test -L "$release_dir"; then
  echo 'STOP: immutable release path already exists' >&2
  exit 1
fi
staging_dir=$(sudo mktemp -d "/opt/sidq/releases/.${release_sha}.XXXXXX")
git archive "$release_sha" | sudo tar -x -C "$staging_dir"
sudo chown -R root:root "$staging_dir"
sudo chmod -R a-w,a+rX "$staging_dir"
sudo mv -T "$staging_dir" "$release_dir"
sudo test -d "$release_dir"
sudo test ! -L "$release_dir"
sudo test "$(sudo stat -c '%U:%G' "$release_dir")" = root:root
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
  echo 'STOP: dependency inputs changed; do not switch, touch, or restart' >&2
  echo 'Rebuild the hash-locked runtime first using docs/SETUP.md, then retry' >&2
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
  && sudo systemctl restart sidq-landing
```

The byte comparisons must succeed before the symlink moves. A mismatch means the
existing runtime is not evidence for the new release: stop without touching the
marker or restarting, rebuild the hash-locked runtime using `docs/SETUP.md`, and
rerun the release procedure. This runbook does not claim to automate that
rebuild. The later timestamp checks only protect Make's marker contract; they are
not a substitute for content compatibility.

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

The committed unit is `deploy/sidq-landing.service`. Operator overrides belong in
`/etc/sidq/landing.env`; do not place secrets in the repository or in the unit.

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
root-owned immutable release, not a checkout or a symlink. Compare its dependency
inputs with the release currently compatible with the shared runtime before
moving `/opt/sidq/current`.

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
  echo 'STOP: rollback dependency inputs do not match the shared runtime' >&2
  echo 'Rebuild the hash-locked runtime for the target using docs/SETUP.md first' >&2
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
  && sudo systemctl restart sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
```

If compatibility fails, do not switch the symlink, touch the marker, or restart.
Rebuild and verify the hash-locked runtime for the target first using
`docs/SETUP.md`; no automatic rebuild is implied here. A compatible rollback
switches only the deployment symlink and does not rewrite the source checkout or
discard local changes.
