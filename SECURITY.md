# Security and production deployment

The public landing page contains no DataHub login, access token, database
password, or tunnel token. Judges receive a DataHub **Reader** login out of band
through the Devpost testing instructions. Do not put that login in HTML,
JavaScript, screenshots, repository files, browser storage, or shell history.

## Pin DataHub and create its secrets

`deploy/datahub-version.env` pins DataHub to `v1.5.0.6` and the UI ingestion CLI
to `1.6.0.16`. The secure override requires five operator-generated values. Put
them in `/etc/sidq/datahub-secrets.env`, owned by `root:root` and mode `0600`:

```sh
sudo install -d -o root -g root -m 0750 /etc/sidq
sudo install -o root -g root -m 0600 /dev/null /etc/sidq/datahub-secrets.env
sudoedit /etc/sidq/datahub-secrets.env
sudo chown root:root /etc/sidq/datahub-secrets.env
sudo chmod 0600 /etc/sidq/datahub-secrets.env
```

The file has this shape, with independently generated values in place of every
placeholder:

```dotenv
DATAHUB_SYSTEM_CLIENT_ID=<dedicated-system-client-id>
DATAHUB_SYSTEM_CLIENT_SECRET=<random-system-client-secret>
DATAHUB_FRONTEND_SECRET=<different-random-frontend-session-secret>
DATAHUB_TOKEN_SERVICE_SIGNING_KEY=<random-token-signing-key>
DATAHUB_TOKEN_SERVICE_SALT=<different-random-token-salt>
```

The same system client ID and secret reach GMS, frontend, and actions. The
frontend session secret must be unique and at least 32 random characters. The
signing key and salt reach GMS and system update. Do not reuse the bootstrap
Admin password for any of them; `openssl rand -hex 32` produces a suitable
independent value for each secret.

## Bootstrap native users before the auth cutover

The native-user creation API must run once before the authenticated deployment
can replace the default JAAS account. Start the pinned images with
`deploy/datahub-bootstrap-auth.yml` only for this one-shot operation. The
override publishes no host ports, removes inherited plugin and AWS mounts, and
runs GMS, frontend, and the CLI in one private network namespace. GMS auth is
off only inside that namespace; no bootstrap service has a host port. Never
publish the bootstrap override or leave this stack running after the users are
created:

```sh
cd /home/dev/sidq-public
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-bootstrap-auth.yml \
  --profile quickstart --profile bootstrap up -d --wait \
  datahub-gms-quickstart frontend-quickstart
```

Preserving `HOME=/home/dev` is required because the generated base Compose file
contains `${HOME}` bind paths; invoking it with root's home would silently use
`/root/.datahub`. Run the pinned ingestion image as an ephemeral CLI sharing the
GMS network namespace. CLI 1.6 uses the email as the native identity, so use
`--email-as-id` and do not supply a separate `--id`. Both commands prompt for a
hidden password; neither the password nor a token appears in an argument or
environment file:

```sh
cd /home/dev/sidq-public
read -r -p 'Operator Admin email: ' admin_email
read -r -p 'Operator Reader email: ' reader_email
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-bootstrap-auth.yml \
  --profile quickstart --profile bootstrap \
  run --rm datahub-auth-bootstrap user add \
  --email "$admin_email" --email-as-id \
  --display-name 'Sidq Admin' --password --role Admin
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-bootstrap-auth.yml \
  --profile quickstart --profile bootstrap \
  run --rm datahub-auth-bootstrap user add \
  --email "$reader_email" --email-as-id \
  --display-name 'Sidq Reader' --password --role Reader
```

Before changing auth, verify the actual email URNs, native-user flags, and roles.
The result must identify `urn:li:corpuser:$admin_email` with Admin and
`urn:li:corpuser:$reader_email` with Reader:

```sh
admin_urn="urn:li:corpuser:$admin_email"
reader_urn="urn:li:corpuser:$reader_email"
identity_query=$(jq -nc --arg admin "$admin_urn" --arg reader "$reader_urn" \
  '{query:"query($admin:String!,$reader:String!){admin:corpUser(urn:$admin){urn isNativeUser} reader:corpUser(urn:$reader){urn isNativeUser} adminAccess:debugAccess(userUrn:$admin){allRoles} readerAccess:debugAccess(userUrn:$reader){allRoles}}",variables:{admin:$admin,reader:$reader}}')
identity_result=$(printf '%s' "$identity_query" | \
  sudo env HOME=/home/dev docker compose \
    --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
    --env-file deploy/datahub-version.env \
    --env-file /etc/sidq/datahub-secrets.env \
    -f /home/dev/.datahub/quickstart/docker-compose.yml \
    -f deploy/datahub-bootstrap-auth.yml \
    --profile quickstart --profile bootstrap \
    exec -T datahub-gms-quickstart curl --fail-with-body -sS \
      -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:8080/api/graphql)
jq -e --arg admin "$admin_urn" --arg reader "$reader_urn" \
  '.errors == null and .data.admin.urn == $admin and .data.admin.isNativeUser == true and .data.reader.urn == $reader and .data.reader.isNativeUser == true and (.data.adminAccess.allRoles | index("urn:li:dataHubRole:Admin")) != null and (.data.readerAccess.allRoles | index("urn:li:dataHubRole:Reader")) != null' \
  <<<"$identity_result"
```

Stop the private bootstrap stack before applying any configuration that has
host ports. `down` removes its containers and private namespace, but not the
named data volumes because `--volumes` is deliberately absent:

```sh
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-bootstrap-auth.yml \
  --profile quickstart --profile bootstrap down
```

Only after that command succeeds, validate and apply the secure override. Its
GMS auth is enabled before Docker creates the loopback port mappings, and it
sets `AUTH_JAAS_ENABLED=false`. Prove both native logins after the secure
cutover using the status-only checks in the verification section.

## Disk and OpenSearch preflight

OpenSearch applies a flood-stage `read_only_allow_delete` block when its disk is
nearly full. Before either Compose `up`, inspect the filesystem that actually
contains Docker data and stop at 90% usage; 85% is an operator warning:

```sh
docker_root=$(sudo docker info --format '{{.DockerRootDir}}')
disk_used_percent=$(df -P "$docker_root" | awk 'NR == 2 {gsub("%", "", $5); print $5}')
test "$disk_used_percent" -lt 90 || {
  echo "Docker storage is ${disk_used_percent}% full; free space before DataHub startup" >&2
  exit 1
}
test "$disk_used_percent" -lt 85 ||
  echo "warning: Docker storage is ${disk_used_percent}% full" >&2
```

After OpenSearch starts, detect an inherited block through the private container
network:

```sh
opensearch_settings=$(sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-secure-override.yml --profile quickstart \
  exec -T opensearch curl -fsS \
  'http://localhost:9200/_all/_settings?filter_path=*.settings.index.blocks.read_only_allow_delete') || {
  echo 'could not inspect OpenSearch index blocks' >&2
  exit 1
}
if jq -e '[.. | .read_only_allow_delete? // empty] | any(. == "true" or . == true)' \
  <<<"$opensearch_settings"; then
  echo 'OpenSearch indices are read-only; free space first, then use the guarded recovery below' >&2
  exit 1
fi
```

Do not use a general deletion or prune command. After operator-reviewed cleanup,
free space first and require usage below 85%; only then remove the flood-stage
setting and verify the API acknowledged it:

```sh
docker_root=$(sudo docker info --format '{{.DockerRootDir}}')
disk_used_percent=$(df -P "$docker_root" | awk 'NR == 2 {gsub("%", "", $5); print $5}')
test "$disk_used_percent" -lt 85 || {
  echo "Docker storage remains ${disk_used_percent}% full; refusing to unlock OpenSearch" >&2
  exit 1
}
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-secure-override.yml --profile quickstart \
  exec -T opensearch curl -fsS -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"index.blocks.read_only_allow_delete": null}' \
  http://localhost:9200/_all/_settings | jq -e '.acknowledged == true'
```

## Validate and apply DataHub

The override requires Docker Compose 2.24.4 or newer because it uses
`!override`. Validate the merged model without printing interpolated secrets:

```sh
cd /home/dev/sidq-public
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-secure-override.yml \
  --profile quickstart config -q
```

Apply the identical file set during the maintenance window:

```sh
cd /home/dev/sidq-public
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-secure-override.yml \
  --profile quickstart up -d
```

Only GMS `127.0.0.1:8080` and frontend `127.0.0.1:9002` remain published.
MySQL, Kafka, and OpenSearch have no host port mappings. Keep the host firewall
default-deny for inbound TCP 8080, 9002, 3306, 9092, 9200, 55432, and 8766.
Cloudflare Tunnel is the only public route to an intended loopback origin.

After the secure override is healthy, complete the first login using the new
native Admin. In **Settings -> Permissions -> Policies**, edit **All Users - Base
Platform Privileges** and remove `GENERATE_PERSONAL_ACCESS_TOKENS`; DataHub
v1.5.0.6 grants that privilege to all users by default. Confirm the Admin role
retains **Manage All Access Tokens** (`MANAGE_ACCESS_TOKENS`). Admin generates the PAT on behalf of the Reader identity
and delivers it through the operator secret channel. That PAT is a personal
access token belonging to the Reader identity; it is never an Admin PAT. Log in
as Reader and confirm access-token generation is absent or denied before the PAT
is installed in `/etc/sidq/datahub-reader.token`.

Use the same `/etc/sidq/datahub-secrets.env` throughout bootstrap and cutover.
In particular, preserve `DATAHUB_TOKEN_SERVICE_SIGNING_KEY` through native-user
creation, auth cutover, and Reader PAT creation. If that signing key must change,
revoke and reissue (rotate) every previously issued token immediately before
installing the new Reader PAT and restarting the landing service.

If the native Admin cannot log in after cutover, keep the secure deployment
stopped. First remove the published containers without removing their named
volumes:

```sh
sudo env HOME=/home/dev docker compose \
  --env-file /home/dev/.datahub/quickstart/.local-secrets.env \
  --env-file deploy/datahub-version.env \
  --env-file /etc/sidq/datahub-secrets.env \
  -f /home/dev/.datahub/quickstart/docker-compose.yml \
  -f deploy/datahub-secure-override.yml --profile quickstart down
```

Apply the private bootstrap command above; never layer the bootstrap override
onto running published services. Inspect the identity through the
container-local GraphQL check. If the original identity cannot be recovered,
create a distinct emergency native Admin with the same one-shot `user add`
procedure, verify its Admin role internally, and run the bootstrap `down`
command. Then validate and reapply `deploy/datahub-secure-override.yml` and
prove a fresh emergency-Admin login before repairing or disabling the original
identity. Never publish the bootstrap override and never fall back to the
unoverridden base.

## Harden the existing PostgreSQL volume

Changing Compose environment variables does not rotate credentials in an
existing PostgreSQL volume. The `sidq` role is PostgreSQL's OID 10 bootstrap superuser
for this volume, and PostgreSQL refuses to demote that bootstrap identity.
Rotate its password, keep it isolated for database administration and loading,
and never reference it from the landing service. Create a separate constrained
Reader:

```sql
\password sidq

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE warehouse FROM PUBLIC;

CREATE ROLE sidq_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4;
\password sidq_reader
ALTER ROLE sidq_reader SET default_transaction_read_only = on;
ALTER ROLE sidq_reader SET statement_timeout = '15s';
ALTER ROLE sidq_reader SET lock_timeout = '3s';
ALTER ROLE sidq_reader SET idle_in_transaction_session_timeout = '15s';
GRANT CONNECT ON DATABASE warehouse TO sidq_reader;
GRANT USAGE ON SCHEMA raw, analytics TO sidq_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA raw, analytics TO sidq_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE sidq IN SCHEMA raw, analytics
  GRANT SELECT ON TABLES TO sidq_reader;
```

The `sidq` bootstrap superuser remains powerful by design, so isolate it from
the landing service and normal application paths. The landing service uses only
`sidq_reader`; do not grant that role create, alter, write, replication,
role-management, temporary-table, or database-owner privileges.

## Install an immutable landing release

Do not execute public requests from a developer checkout. For a reviewed Git
SHA, use the single fail-closed **Release** procedure in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#release). It requires a clean exact
commit, extracts into a uniquely named staging directory, makes the staged tree
root-owned and immutable, rejects or byte-compares any pre-existing release,
checks runtime compatibility, and only then performs the atomic `current`
symlink move. Do not materialize directly into the final SHA path or activate a
partially extracted tree.

Build or replace the application and DataHub MCP environments only with the
single fail-closed **Rebuild the production runtime** procedure in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#rebuild-the-production-runtime).
Never install directly into the active `/opt/sidq/runtime/venv` or
`/opt/sidq/runtime/mcp` path. That procedure starts with `set -euo pipefail`,
installs both hash-locked environments under unique `.next` names, validates the
Sidq import and MCP executable, makes the staged trees root-owned and immutable,
and preserves the prior runtime until the replacement has been activated and
probed. A failed install or validation therefore cannot create an active
readiness marker or replace the running environment.

The marker records a validated application-environment install. For hosted
demos, the exact internal `--old-file` operand—not the marker alone—prevents
immutable-release timestamps from asking Make to rebuild the read-only runtime.
The **Release** procedure intentionally stops if the active runtime and all five
recorded compatibility inputs are absent or do not match; a new host must first
be provisioned through equally staged, fail-closed deployment automation rather
than by adapting the local-development install target.

`requirements-mcp.in` stays separate because the isolated official DataHub MCP
server and Sidq's core MCP 2.x dependency cannot share one environment. Update
and review that lock with:

```sh
uv pip compile --generate-hashes --python-version 3.12 \
  --output-file requirements-mcp.lock requirements-mcp.in
```

### Python dependency advisory status

`PYSEC-2026-3447` / `CVE-2026-59890` affects `setuptools` before 83.0.0:
on filesystems that normalize names to NFD, a crafted filename can bypass an
NFC `MANIFEST.in` exclusion and enter a source distribution. Sidq therefore
requires `setuptools>=83` for its build backend and every compatible application
runtime lock that contains setuptools. Do not suppress this advisory with
`--ignore-vuln`.

The application locks do not contain `acryl-datahub`. The isolated MCP lock
does because `mcp-server-datahub==0.6.0` depends on the DataHub SDK. Both the
pinned `acryl-datahub==1.6.0.16` and the current 1.6.0.17 metadata require
`setuptools<82`, so the MCP resolver cannot install patched setuptools 83. Its
remaining risk is explicit: `pip-audit` reports this advisory for the isolated
MCP lock, and it must be revisited when DataHub removes that upper bound.

The vulnerable path is not technically reachable from the deployed MCP
workload. It requires *creating* an sdist containing attacker-influenced Unicode
filenames on a normalizing filesystem. The Linux service installs reviewed,
hash-locked wheels, never creates release sdists, accepts no filesystem paths
from public requests, and runs in the separately locked-down MCP environment.
This precise non-applicability limits the residual risk; it does not make the
installed vulnerable version patched or justify suppressing the finding.

After regenerating the locks, audit every independently installed environment:

`requirements-bench.lock` is deliberately separate: reproducible pre-flight
training needs scikit-learn, while CI, the GitHub Action, and the public landing
runtime do not. `make regen` and `make regen-check` consume it through the
isolated `.venv-bench` environment.

```sh
for lock in requirements.lock requirements-action.lock requirements-bench.lock \
  requirements-dev.lock requirements-landing.lock; do
  uvx pip-audit --disable-pip --requirement "$lock"
done
# Expected to report PYSEC-2026-3447/CVE-2026-59890 until acryl-datahub lifts
# setuptools<82. Record it; do not add an ignore.
uvx pip-audit --disable-pip --requirement requirements-mcp.lock
```

Install the reviewed tunnel binary as root-owned, non-writable
`/usr/local/bin/cloudflared-sidq`:

```sh
sudo chown root:root /usr/local/bin/cloudflared-sidq
sudo chmod 0755 /usr/local/bin/cloudflared-sidq
```

Create the Hugging Face cache as a dedicated empty runtime directory and warm
only `microsoft/harrier-oss-v1-270m` at the pinned revision used by
`src/sidq/claims/reader.py`. Do not copy a developer's entire cache. Run the
same load once with network disabled before cutover, then return the cache to
root-owned read-only state:

```sh
sudo install -d -o root -g root -m 0755 /opt/sidq/runtime/huggingface
sudo env HF_HOME=/opt/sidq/runtime/huggingface \
  SENTENCE_TRANSFORMERS_HOME=/opt/sidq/runtime/huggingface/hub \
  /opt/sidq/runtime/venv/bin/python -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('microsoft/harrier-oss-v1-270m', revision='31de22b673913c7d658c0f03f792d77c2dcf8ebd', trust_remote_code=True)"
sudo env HF_HOME=/opt/sidq/runtime/huggingface \
  SENTENCE_TRANSFORMERS_HOME=/opt/sidq/runtime/huggingface/hub \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
  /opt/sidq/runtime/venv/bin/python -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('microsoft/harrier-oss-v1-270m', revision='31de22b673913c7d658c0f03f792d77c2dcf8ebd', trust_remote_code=True)"
sudo chown -R root:root /opt/sidq/runtime/huggingface
sudo chown -R root:root /opt/sidq/runtime
sudo chmod -R a+rX,a-w /opt/sidq/runtime
sudo -u nobody env HOME=/tmp HF_HOME=/opt/sidq/runtime/huggingface \
  SENTENCE_TRANSFORMERS_HOME=/opt/sidq/runtime/huggingface/hub \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
  /opt/sidq/runtime/venv/bin/python -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('microsoft/harrier-oss-v1-270m', revision='31de22b673913c7d658c0f03f792d77c2dcf8ebd', trust_remote_code=True)"
```

The claims child receives `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and the
root-owned cache paths from its closed environment. The judge run therefore has
no dependency on an ambient `/home` cache or a model download.

## Install service credentials and units

Systemd `LoadCredential` exposes each secret to its service through a private,
read-only path below `/run/credentials`; tokens never appear in an
`EnvironmentFile` or the process command line. Create the source files as
`root:root` and mode `0600`:

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/sidq/datahub-reader.token
sudo install -o root -g root -m 0600 /dev/null /etc/sidq/claims.dsn
sudo install -o root -g root -m 0600 /dev/null /etc/sidq/pg_service.conf
sudo install -o root -g root -m 0600 /dev/null /etc/sidq/pgpass
sudo install -d -o root -g root -m 0750 /etc/cloudflared
sudo install -o root -g root -m 0600 /dev/null /etc/cloudflared/sidq.token
sudoedit /etc/sidq/datahub-reader.token
sudoedit /etc/sidq/claims.dsn
sudoedit /etc/sidq/pg_service.conf
sudoedit /etc/sidq/pgpass
sudoedit /etc/cloudflared/sidq.token
```

`datahub-reader.token` contains only the Reader-identity PAT. `claims.dsn`
contains the libpq DSN for `sidq_reader`, for example its loopback host, database,
user, and operator-generated password. For operator verification,
`pg_service.conf` contains only non-secret connection fields:

```ini
[sidq_reader]
host=127.0.0.1
port=55432
dbname=warehouse
user=sidq_reader
```

`pgpass` contains the corresponding libpq password record, with the real
operator-generated password replacing the final placeholder:

```text
127.0.0.1:55432:warehouse:sidq_reader:<password>
```

Keep both files `root:root` mode `0600`; the tunnel file contains only its token.

Verify and install the units, then start them:

```sh
cd /home/dev/sidq-public
systemd-analyze verify deploy/sidq-landing.service deploy/cloudflared-sidq.service
sudo install -o root -g root -m 0644 deploy/sidq-landing.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/cloudflared-sidq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sidq-landing.service cloudflared-sidq.service
```

The landing unit uses `DynamicUser=true`, an immutable `/opt/sidq/current`, and
root-owned runtime paths. `ProtectProc=invisible`, `ProtectHome=true`, a strict
filesystem, and an empty capability set keep the service away from developer
homes and other processes.

The server accepts exactly the configured `SIDQ_ALLOWED_ORIGINS` value (default
`https://sidq.mlki.app`) together with same-origin fetch metadata and a custom
header. Each browser run also consumes a short-lived one-time capability bound
to the selected command and, when one is attributable through an explicitly
trusted proxy, the client address. Issuance is stateless: the process signs the
expiry, nonce, attributable-client subject, and command with an ephemeral HMAC
key, so any number of unconsumed issuance requests adds no server-side entries.
Only a capability that passes the command lock, concurrency slot, cooldown, and
rolling start caps enters the bounded replay ledger; expired entries are
removed. Tampering, expiry, a different command or attributable client, and
replay all fail.

Remote traffic reaches the loopback-bound origin only through Cloudflare
Tunnel. Cloudflare supplies the authoritative `CF-Connecting-IP`, and the
installed unit explicitly trusts only its loopback peer with
`SIDQ_TRUSTED_PROXIES=127.0.0.1/32`. Forwarded visitors therefore receive
independent five-run client buckets instead of sharing the tunnel's socket
address. Native IPv6 addresses are grouped by canonical `/64`; IPv4 and
IPv4-mapped IPv6 addresses keep exact IPv4 identities. A local process can
connect from loopback and spoof that header, but it remains subject to the
global rolling start cap, global concurrency, and per-command lock and cooldown.
Compromise of the local host is outside this remote threat boundary. These
controls are replay and DoS containment, not user authentication and not a
substitute for Cloudflare access controls.

Forwarded plain-HTTP requests from that trusted tunnel peer receive a canonical
308 redirect to the configured HTTPS origin. The redirect target is built from
the fixed allowed origin rather than an untrusted `Host` header.

For DataHub-dependent subprocesses only, the server reads the Reader credential
and exposes it to that child as `DATAHUB_GMS_TOKEN`; it never inherits the
service manager's ambient environment. The offline gate receives neither that
token nor the claims DSN.

Hosted `make gate-demo` and `make claims-demo` runs pass an internal exact
`--old-file` operand for the already-built shared runtime marker. This prevents a
new immutable release timestamp from triggering a package install into the
read-only runtime. The public command label omits only that exact operand, and
public output redacts credentials before runtime paths and internal URLs.

The CLI default is 7 days for receipt age. The hosted judging handoff explicitly
passes `--max-age-days 45` for the August judging period. This 45-day judging window
relaxes only the age check: a semantic entity, complete one-hop lineage,
policy, or context change still invalidates the receipt.

## Verification

Verify unauthenticated GMS access is denied (the status must be `401`):

```sh
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  --data '{"query":"{ me { corpUser { urn } } }"}' \
  http://127.0.0.1:8080/api/graphql)" = 401
```

Verify both native identities through frontend `/logIn` JSON after JAAS is
disabled. Passwords are read silently, passed through stdin rather than process
arguments, and unset after the status-only checks. Admin and Reader must return
`200`; the removed default `datahub/datahub` login must return `400`:

```sh
read -r -p 'Operator Admin email: ' admin_email
read -rs -p 'Admin password: ' admin_password; echo
read -r -p 'Operator Reader email: ' reader_email
read -rs -p 'Reader password: ' reader_password; echo
login_status() {
  local username=$1 password=$2
  printf '%s\0%s' "$username" "$password" |
    jq -Rs 'split("\u0000") | {username:.[0],password:.[1]}' |
    curl -sS -o /dev/null -w '%{http_code}' \
      -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:9002/logIn
}
admin_login_status=$(login_status "$admin_email" "$admin_password")
reader_login_status=$(login_status "$reader_email" "$reader_password")
default_login_status=$(login_status datahub datahub)
test "$admin_login_status" = 200
test "$reader_login_status" = 200
test "$default_login_status" = 400
unset admin_password reader_password
```

Verify listeners. Expect GMS, frontend, PostgreSQL, and landing only on
loopback; expect no host listeners for MySQL, Kafka, or OpenSearch:

```sh
sudo ss -ltnp | grep -E ':(8080|9002|55432|8766)\b'
if sudo ss -ltnp | grep -Eq ':(3306|9092|9200)\b'; then exit 1; fi
```

Use Admin once to create the disposable dataset
`urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.authorization_probe,DEV)`
and give it a known editable description. The Reader must be able to read that
asset. Then run this exact mutation-denial check with the installed Reader PAT;
it requires an authorization error and proves a second read is unchanged. Never test write denial against production metadata.

```sh
receipt_urn='urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.authorization_probe,DEV)'
reader_token=$(sudo cat /etc/sidq/datahub-reader.token)
reader_graphql() {
  curl --fail-with-body -sS \
    -H @<(printf 'Authorization: Bearer %s\n' "$reader_token") \
    -H 'Content-Type: application/json' --data "$1" \
    http://127.0.0.1:8080/api/graphql
}
read_payload=$(jq -nc --arg urn "$receipt_urn" \
  '{query:"query($urn:String!){dataset(urn:$urn){urn editableProperties{description}}}",variables:{urn:$urn}}')
before_result=$(reader_graphql "$read_payload")
jq -e --arg urn "$receipt_urn" \
  '.errors == null and .data.dataset.urn == $urn and .data.dataset.editableProperties.description != null' \
  <<<"$before_result"
before_description=$(jq -c '.data.dataset.editableProperties.description' \
  <<<"$before_result")
mutation_payload=$(jq -nc --arg urn "$receipt_urn" \
  '{query:"mutation($input:DescriptionUpdateInput!){updateDescription(input:$input)}",variables:{input:{resourceUrn:$urn,description:"Reader must not write this"}}}')
denial_result=$(reader_graphql "$mutation_payload")
jq -e '.errors | length > 0 and any(.[]; .message | test("authoriz|permission|denied"; "i"))' \
  <<<"$denial_result"
after_result=$(reader_graphql "$read_payload")
jq -e --arg urn "$receipt_urn" \
  '.errors == null and .data.dataset.urn == $urn' <<<"$after_result"
after_description=$(jq -c '.data.dataset.editableProperties.description' \
  <<<"$after_result")
test "$before_description" = "$after_description"
unset reader_token
```

Verify the database Reader can select and cannot write:

```sh
test "$(sudo stat -c '%U:%G %a' /etc/sidq/pg_service.conf)" = "root:root 600"
test "$(sudo stat -c '%U:%G %a' /etc/sidq/pgpass)" = "root:root 600"
sudo env PGSERVICEFILE=/etc/sidq/pg_service.conf \
  PGPASSFILE=/etc/sidq/pgpass \
  psql "service=sidq_reader" -v ON_ERROR_STOP=1 \
  -c 'SELECT 1 FROM raw.orders LIMIT 1'
if sudo env PGSERVICEFILE=/etc/sidq/pg_service.conf \
  PGPASSFILE=/etc/sidq/pgpass \
  psql "service=sidq_reader" -v ON_ERROR_STOP=1 \
  -c 'CREATE TABLE raw.sidq_reader_must_not_create(id integer)'; then
  exit 1
fi
```

Finally verify service health and credential loading without printing secrets:

```sh
curl --fail http://127.0.0.1:8766/healthz
curl --fail http://127.0.0.1:8766/readyz
sudo systemctl show sidq-landing.service cloudflared-sidq.service \
  -p DynamicUser -p LoadCredential -p ProtectProc -p ProtectHome
```

## Rollback

Keep the previous release directory and version file. To roll back the landing,
atomically repoint `/opt/sidq/current` to the previous root-owned release, run
`systemd-analyze verify` on its units, and restart only the affected service. To
roll back DataHub, restore the previously reviewed version env file and apply
the same base plus secure override command. Never remove the secure override to
recover availability: keep services stopped, correct the configuration, run
`docker compose ... config -q`, and only then apply again.
