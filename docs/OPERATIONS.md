# Sidq landing operations

This runbook covers the judge-facing landing process. The public endpoint exposes
only fixed, read-only demonstrations. DataHub and PostgreSQL remain separately
managed dependencies.

## Release

Record the exact known-good revision before changing anything:

```bash
cd /home/dev/sidq-public
git rev-parse HEAD
git status --short
make check
sudo systemctl restart sidq-landing
```

Do not release a dirty tree. The service reads the checkout directly, so a local
edit would create a deployment that cannot be reproduced from its Git SHA.

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

Use the known-good SHA captured before release. A detached checkout makes the
exact deployed revision unambiguous while preserving the branch for recovery:

```bash
cd /home/dev/sidq-public
git switch --detach KNOWN_GOOD_SHA
sudo systemctl restart sidq-landing
curl --fail --silent http://127.0.0.1:8766/healthz
curl --fail --silent http://127.0.0.1:8766/readyz
```

After diagnosing the failed release, return to the release branch with
`git switch main`. Never discard a dirty tree during rollback; stop and preserve
those changes first.
