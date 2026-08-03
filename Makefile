DEMO_COMPOSE := docker compose -f demo/docker-compose.yml
DEMO_INGEST_IMAGE := acryldata/datahub-ingestion:v1.5.0.6

VENV ?= .venv
BENCH_VENV ?= .venv-bench
PYTHON ?= python3.12

DATAHUB_GMS_URL ?= http://localhost:8080
AUDIT_BUDGET ?= 5
# The most-consumed asset in DataHub's own showcase-ecommerce sample, so the
# budget above always reaches it and the readback below always has a receipt.
RECEIPT_URN ?= urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)

REPAIR_BUDGET ?= 15

.PHONY: help install mcp-install check lock regen regen-check decision-cost claims-demo gate-demo live-loop converge-demo swarm-demo repair-demo repair-reset demo-prereqs demo-stack mcp-smoke doctor demo-up demo-ingest demo-break demo-restore demo-down

# The runbook's first row promises a clone and `make` are enough, and until
# 2026-07-31 that promise was false: a fresh clone had no virtualenv and the
# first command a judge types died with a path error. This rule keeps the
# promise — the first target that needs the venv builds it, once, with exactly
# the dev extras `make check` runs. Order-only (`|`) everywhere below, so an
# existing venv is never rebuilt behind anyone's back. The live rows still
# need DataHub and the isolated MCP server from docs/SETUP.md; that boundary
# is theirs, not this rule's.
$(VENV)/.sidq-dev-lock: requirements-dev.lock pyproject.toml uv.lock
	@echo "first run: building $(VENV) (about a minute) =="
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "ERROR: Python 3.12 is required; install python3.12 or set PYTHON to its executable." >&2; exit 1; }
	@test "$$($(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = "3.12" || { echo "ERROR: Python 3.12 is required to build $(VENV)." >&2; exit 1; }
	test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --quiet --require-hashes -r requirements-dev.lock
	$(VENV)/bin/python -m pip install --quiet --no-build-isolation --no-deps --editable .
	touch $@

$(BENCH_VENV)/.sidq-bench-lock: requirements-bench.lock pyproject.toml uv.lock
	@echo "first run: building $(BENCH_VENV) for reproducible benchmarks =="
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "ERROR: Python 3.12 is required; install python3.12 or set PYTHON to its executable." >&2; exit 1; }
	@test "$$($(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = "3.12" || { echo "ERROR: Python 3.12 is required to build $(BENCH_VENV)." >&2; exit 1; }
	test -x $(BENCH_VENV)/bin/python || $(PYTHON) -m venv $(BENCH_VENV)
	$(BENCH_VENV)/bin/python -m pip install --quiet --require-hashes -r requirements-bench.lock
	touch $@

help:
	@printf '%s\n' \
	  'make install       Build the hash-locked Sidq project environment' \
	  'make mcp-install   Install the official DataHub MCP server as an isolated uv tool' \
	  'make demo-stack    Start Sidq PostgreSQL and ingest it into an already-running DataHub' \
	  'make mcp-smoke     Initialize Sidq MCP, then exercise the official DataHub MCP server' \
	  'make doctor        Diagnose every read-only connected-mode prerequisite'

# Local Sidq installation is fully offline from DataHub. The two CLI paths are
# resolved after the hash-locked environment is ready so callers can copy them
# directly instead of guessing which ambient Python or executable is active.
install: | $(VENV)/.sidq-dev-lock
	@test -x "$(VENV)/bin/sidq" && test -x "$(VENV)/bin/sidq-mcp" || { echo "ERROR: $(VENV) is incomplete; remove $(VENV)/.sidq-dev-lock and rerun 'make install' to repair it." >&2; exit 1; }
	@test "$$($(VENV)/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = "3.12" || { echo "ERROR: $(VENV) is not a Python 3.12 environment; rebuild it before continuing." >&2; exit 1; }
	@$(VENV)/bin/python -c "from pathlib import Path; print('sidq:', Path('$(VENV)/bin/sidq').resolve()); print('sidq-mcp:', Path('$(VENV)/bin/sidq-mcp').resolve())"

# The official DataHub MCP server brings the DataHub SDK into its own uv tool
# environment. It must not be installed into $(VENV), whose mcp>=2,<3 client
# dependency is independently locked by this project.
mcp-install:
	@command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is required; install uv from https://docs.astral.sh/uv/getting-started/installation/ and retry 'make mcp-install'." >&2; exit 127; }
	uv tool install --force --with acryl-datahub==1.6.0.16 --with-executables-from acryl-datahub mcp-server-datahub==0.6.0
	@tool_bin=$$(uv tool dir --bin); \
	  test -x "$$tool_bin/datahub" && test -x "$$tool_bin/mcp-server-datahub" || { echo "ERROR: uv did not install both DataHub executables into $$tool_bin." >&2; exit 1; }; \
	  datahub_path=$$(command -v datahub 2>/dev/null || true); mcp_path=$$(command -v mcp-server-datahub 2>/dev/null || true); \
	  test "$$datahub_path" = "$$tool_bin/datahub" && test "$$mcp_path" = "$$tool_bin/mcp-server-datahub" || { echo "ERROR: PATH shadowing hides the pinned uv tool; put $$tool_bin before other executable directories and retry." >&2; exit 1; }; \
	  printf 'datahub: %s\nmcp-server-datahub: %s\n' "$$tool_bin/datahub" "$$tool_bin/mcp-server-datahub"

check: | $(VENV)/.sidq-dev-lock
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy src/
	$(VENV)/bin/pytest -q --cov=sidq --cov-report=term-missing:skip-covered

# Resolve from pyproject metadata, never from an ambient environment. Install uv,
# run this target, and commit uv.lock plus all five exported hash-locked inputs.
lock:
	uv lock --python 3.12
	uv export --locked --no-emit-project --output-file requirements.lock
	uv export --locked --extra action --no-emit-project --output-file requirements-action.lock
	uv export --locked --extra bench --no-emit-project --output-file requirements-bench.lock
	uv export --locked --extra dev --no-emit-project --output-file requirements-dev.lock
	uv export --locked --extra reader --extra live --no-emit-project --output-file requirements-landing.lock

# Published artifacts are generated, and `make check` fails when a committed copy
# no longer matches the engine. Editing the policy is the usual cause: it changes
# the policy hash, which every published artifact quotes. Run `make regen` and
# commit the result — never hand-edit the artifacts back into agreement.
regen: | $(VENV)/.sidq-dev-lock $(BENCH_VENV)/.sidq-bench-lock
	$(VENV)/bin/python scripts/regenerate_example_01.py
	$(VENV)/bin/python scripts/measure_reconcile.py
	$(VENV)/bin/python scripts/datasheet_stats.py
	$(BENCH_VENV)/bin/python scripts/train_preflight.py
	$(BENCH_VENV)/bin/python scripts/eval_preflight.py

regen-check: | $(VENV)/.sidq-dev-lock $(BENCH_VENV)/.sidq-bench-lock
	$(VENV)/bin/python scripts/regenerate_example_01.py --check
	$(VENV)/bin/python scripts/measure_reconcile.py --check
	$(VENV)/bin/python scripts/datasheet_stats.py --check
	$(BENCH_VENV)/bin/python scripts/train_preflight.py --check
	$(BENCH_VENV)/bin/python scripts/eval_preflight.py --check

# What one decision costs. Deliberately NOT in `regen`/`regen-check`: a timing
# is not byte-reproducible, and a document guarded by a byte comparison it can
# never satisfy would fail `make check` on a machine under load. The published
# claim is an order of magnitude for the same reason.
decision-cost: | $(VENV)/.sidq-dev-lock
	$(VENV)/bin/python scripts/measure_decision_cost.py --write

# The command the landing page tells a judge to run. It existed only on the page
# until 2026-07-30, which meant the one instruction on the first surface a judge
# opens did not work. It runs the flagship change through the real engine against
# the committed graph recording and prints the verdict, so it produces the same
# answer after the locked first-run bootstrap, with no DataHub or credentials.
gate-demo: | $(VENV)/.sidq-dev-lock
	@$(VENV)/bin/python scripts/regenerate_example_01.py --check
	@echo
	@$(VENV)/bin/python -c "import json;v=json.load(open('examples/01-blocked-pii-dashboard/verdict.json'));\
print('DECISION :', v['decision']);\
print('RULES    :', ', '.join(f['rule_id'] for f in v['findings']));\
print('COMMIT   :', v['commit_sha']);\
print('POLICY   :', v['policy_hash'])"
	@echo
	@echo "Reproduced from examples/01-blocked-pii-dashboard/ — same policy, same commit,"
	@echo "byte-identical verdict. Full evidence: examples/01-blocked-pii-dashboard/verdict.json"

# The whole loop, on live DataHub, through the official MCP server only — the one
# command that is category-complete rather than three surfaces implying a loop
# none of them closes. Read with `search`/`get_entities`/`list_schema_fields`/
# `get_lineage`, decide with the shipped policy, write with the mutation tools,
# and then read it back from a process that shares nothing with the writer.
#
# Step 3 is the point. A writer that reports its own success proves nothing; the
# receipt is only worth something because an unrelated process finds it and
# reaches the same conclusion. Step 4 is the other half: an asset carrying no
# receipt must come back NOT VERIFIED, because "we did not check" and "we
# checked and it passed" are the two answers this project exists to keep apart.
# That asset is chosen at run time, not frozen in a variable: the resuming
# audit converges, so any URN written down here would eventually be reached,
# receipted, and turned into a lie — which is exactly how the previous frozen
# choice failed a fresh-clone rehearsal on 2026-07-31.
live-loop: | $(VENV)/.sidq-dev-lock
	@echo "== 1+2. read via official MCP, decide, write receipts via official MCP =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }
	@echo
	@echo "== 3. a separate process reads the receipt back and judges it itself =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq verify '$(RECEIPT_URN)' 2>/dev/null
	@echo
	@echo "== 4. and an asset carrying no receipt is NOT reported as clean =="
	@urn=$$(DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/python scripts/find_unreceipted.py 2>/dev/null); \
	  [ -n "$$urn" ] || { echo "could not choose an asset to ask about"; exit 1; }; \
	  if [ "$$urn" = "ALL_COVERED" ]; then \
	    echo "every dataset in the search page already carries a receipt —"; \
	    echo "the resumable audit has converged; nothing is left to be silent about"; \
	  else \
	    DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	    $(VENV)/bin/sidq verify "$$urn" 2>/dev/null; \
	    status=$$?; [ $$status -eq 1 ] || { echo "expected NOT VERIFIED, got exit $$status"; exit 1; }; \
	  fi

# The audit that resumes. Run one spends the budget worst-first and writes
# receipts; run two reads those receipts back and spends the *same* budget on
# assets run one never reached. No state file is written anywhere in between —
# the memory is the catalog itself, so this is two agents cooperating through
# receipts alone. Watch the `vouched` line appear and `NOT examined` fall.
converge-demo: | $(VENV)/.sidq-dev-lock
	@echo "== run 1: spend the budget worst-first, write receipts =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }
	@echo
	@echo "== run 2: same budget — the receipts vouch, the budget moves on =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts --resume 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }

# Four auditors, one catalog, no coordinator. They are separate processes with
# no IPC between them: the only thing they share is DataHub, and the only way
# they cooperate is by reading the receipts each other writes.
#
# One is killed deliberately mid-run. Its unfinished assets were never assigned
# to it — nothing is — so the survivors pick them up as ordinary work, and a
# fifth process that reads only DataHub prints who did what.
SWARM_BUDGET ?= 6
swarm-demo: | $(VENV)/.sidq-dev-lock
	@run=swarm-$$(date +%s); \
	echo "== four workers start together — no coordinator, no IPC, run $$run =="; \
	pids=""; \
	for w in alpha beta gamma delta; do \
	  DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	    $(VENV)/bin/sidq swarm --via-mcp --worker-id $$w --swarm-run $$run \
	    --budget $(SWARM_BUDGET) --lineage-budget 40 >/tmp/sidq-swarm-$$w.log 2>&1 & \
	  pids="$$pids $$!"; \
	  echo "  started $$w"; \
	done; \
	victim=$$(echo $$pids | awk '{print $$4}'); \
	sleep 30; \
	if kill -9 $$victim 2>/dev/null; then echo; echo "  >> killed delta mid-run — its unfinished assets were never assigned to it"; fi; \
	wait 2>/dev/null || true; \
	echo; \
	for w in alpha beta gamma; do \
	  grep -vE "^INFO|Starting MCP|ExperimentalWarning|from datahub|^\\s*$$" /tmp/sidq-swarm-$$w.log | tail -7; \
	  echo; \
	done; \
	echo "== the ledger, read from DataHub by a process that audited nothing =="; \
	DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq swarm-ledger --via-mcp --swarm-run $$run --budget 60 2>/dev/null


# The one command where a model is allowed to participate, and the shape of that
# participation is the point. Documentation is read from the catalog through the
# official MCP server, each documented sentence is turned into a claim, the claim
# is compiled to read-only SQL and run against the live source, and the row count
# that comes back is what the deterministic engine judges.
#
# `--reader` adds the trained multilingual reader on the sentences the regular
# expressions declined. It proposes what to test; it never decides what is true,
# and a claim it proposes that could not be tested is dropped rather than
# reported. Drop the flag and the same command runs on rules alone.
CLAIMS_SOURCE ?= host=localhost port=55432 dbname=warehouse user=sidq password=sidq
CLAIMS_URNS ?= \
  'urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.orders,PROD)' \
  'urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.customers,PROD)' \
  'urn:li:dataset:(urn:li:dataPlatform:postgres,sidq-demo.warehouse.raw.order_items,PROD)'
claims-demo: export CLAIMS_SOURCE := $(CLAIMS_SOURCE)
claims-demo: | $(VENV)/.sidq-dev-lock
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq claims $(CLAIMS_URNS) --reader 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "claims could not read the catalog or the source"; exit 1; }

# The repair agent, on live DataHub. It proposes only from catalog evidence, then
# re-runs the deterministic engine against the catalog each repair *would* create
# and keeps what survives. On the showcase sample the interesting part is what it
# refuses: tagging just the column named in the finding resolves that finding and
# immediately creates a new one downstream, so the proposal it offers instead
# covers the whole field-lineage closure — 6 columns across dbt, Snowflake and
# Looker, in one MCP call.
#
# Dry run. `sidq repair --via-mcp --apply` writes it; `make repair-reset` restores
# the sample afterwards so the demonstration can be run again.
repair-demo: | $(VENV)/.sidq-dev-lock
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq repair --via-mcp --budget $(REPAIR_BUDGET) 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "repair could not read the catalog"; exit 1; }

repair-reset: | $(VENV)/.sidq-dev-lock
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/python scripts/reset_repair_demo.py 2>/dev/null

# The controlled demo stack adds only Sidq's PostgreSQL source and ingestion.
# DataHub's OSS quickstart must already be running; this target does not launch
# or claim ownership of the full DataHub stack. MCP is not an ingestion
# prerequisite and is checked separately by mcp-smoke.
demo-prereqs:
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is required; install Docker Engine with the Compose plugin." >&2; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "ERROR: the Docker engine is not reachable; start Docker and retry." >&2; exit 1; }
	@docker compose version >/dev/null 2>&1 || { echo "ERROR: Docker Compose is unavailable; install the Docker Compose plugin." >&2; exit 1; }
	@command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required to check DataHub GMS health." >&2; exit 1; }
	@curl --fail --silent --show-error "$(DATAHUB_GMS_URL)/health" >/dev/null || { echo "ERROR: DataHub GMS is not healthy at $(DATAHUB_GMS_URL); start the DataHub OSS quickstart first." >&2; exit 1; }
	@docker network inspect datahub_network >/dev/null 2>&1 || { echo "ERROR: Docker network datahub_network is missing; start the DataHub OSS quickstart first." >&2; exit 1; }
	@catalog_status=$$(if [ -n "$${DATAHUB_GMS_TOKEN:-}" ]; then printf 'header = "Authorization: Bearer %s"\n' "$$DATAHUB_GMS_TOKEN" | curl --config - --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; else curl --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; fi); \
	  if [ "$$catalog_status" = 200 ]; then :; elif [ "$$catalog_status" = 401 ]; then echo "ERROR: DataHub catalog authentication failed (401); export a valid DATAHUB_GMS_TOKEN from your secret manager and retry." >&2; exit 1; else echo "ERROR: DataHub catalog preflight returned HTTP $$catalog_status." >&2; exit 1; fi
	@docker run --rm --network datahub_network --entrypoint python $(DEMO_INGEST_IMAGE) -c "import urllib.request; urllib.request.urlopen('http://datahub-gms-quickstart:8080/health', timeout=10).read()" >/dev/null 2>&1 || { echo "ERROR: datahub-gms-quickstart:8080 is not reachable from datahub_network; verify the quickstart network alias." >&2; exit 1; }

demo-stack: demo-prereqs
	$(MAKE) demo-up
	$(MAKE) demo-ingest

# Initialize Sidq's three-tool MCP server, then exercise the read-only official
# DataHub MCP server. The latter remains an external executable from the
# isolated uv tool install; both clients run from the Sidq project environment.
mcp-smoke: | $(VENV)/.sidq-dev-lock
	@command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required to check DataHub GMS health." >&2; exit 1; }
	@curl --fail --silent --show-error "$(DATAHUB_GMS_URL)/health" >/dev/null || { echo "ERROR: DataHub GMS is not healthy at $(DATAHUB_GMS_URL); start DataHub before running the MCP smoke test." >&2; exit 1; }
	@catalog_status=$$(if [ -n "$${DATAHUB_GMS_TOKEN:-}" ]; then printf 'header = "Authorization: Bearer %s"\n' "$$DATAHUB_GMS_TOKEN" | curl --config - --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; else curl --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; fi); \
	  if [ "$$catalog_status" = 200 ]; then :; elif [ "$$catalog_status" = 401 ]; then echo "ERROR: DataHub catalog authentication failed (401); export a valid DATAHUB_GMS_TOKEN from your secret manager and retry." >&2; exit 1; else echo "ERROR: DataHub catalog preflight returned HTTP $$catalog_status." >&2; exit 1; fi
	@set -e; tool_bin=$$(uv tool dir --bin 2>/dev/null || true); server_command="$$tool_bin/mcp-server-datahub"; \
	  test -n "$$tool_bin" && test -x "$$server_command" || { echo "ERROR: the pinned uv mcp-server-datahub is missing; run 'make mcp-install'." >&2; exit 1; }; \
	  PATH="$$tool_bin:$$PATH" $(VENV)/bin/python scripts/smoke_sidq_mcp.py --server-command "$(VENV)/bin/sidq-mcp"; \
	  DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false $(VENV)/bin/python scripts/smoke_mcp.py --server-command "$$server_command"

# Read-only connected-mode diagnosis. It deliberately checks every layer even
# after a miss, then returns nonzero so automation cannot mistake partial setup
# for a working live environment. No service, network, tool, or secret changes.
doctor:
	@missing=0; echo "== core Sidq =="; \
	if test -x "$(VENV)/bin/sidq" && test -x "$(VENV)/bin/sidq-mcp"; then echo "[ok] project environment"; else echo "[missing] project environment — run 'make install'"; missing=1; fi; \
	echo "== connected mode =="; \
	if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then echo "[ok] Docker engine"; else echo "[missing] Docker engine — install and start Docker"; missing=1; fi; \
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then echo "[ok] Docker Compose"; else echo "[missing] Docker Compose — install the Compose plugin"; missing=1; fi; \
	if command -v curl >/dev/null 2>&1 && curl --fail --silent "$(DATAHUB_GMS_URL)/health" >/dev/null 2>&1; then echo "[ok] DataHub GMS"; else echo "[missing] DataHub GMS — start the OSS quickstart and verify its /health endpoint"; missing=1; fi; \
	catalog_status=$$(if command -v curl >/dev/null 2>&1; then if [ -n "$${DATAHUB_GMS_TOKEN:-}" ]; then printf 'header = "Authorization: Bearer %s"\n' "$$DATAHUB_GMS_TOKEN" | curl --config - --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; else curl --silent --output /dev/null --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{"query":"{ __typename }"}' "$(DATAHUB_GMS_URL)/api/graphql"; fi; else printf 000; fi); \
	if [ "$$catalog_status" = 200 ]; then echo "[ok] DataHub catalog access"; elif [ "$$catalog_status" = 401 ]; then echo "[missing] DataHub catalog access — authentication failed (401); export DATAHUB_GMS_TOKEN from your secret manager"; missing=1; else echo "[missing] DataHub catalog access — HTTP $$catalog_status"; missing=1; fi; \
	if command -v docker >/dev/null 2>&1 && docker network inspect datahub_network >/dev/null 2>&1; then echo "[ok] datahub_network"; else echo "[missing] datahub_network — start the DataHub OSS quickstart"; missing=1; fi; \
	tool_bin=$$(uv tool dir --bin 2>/dev/null || true); \
	if [ -n "$$tool_bin" ] && [ "$$(command -v datahub 2>/dev/null || true)" = "$$tool_bin/datahub" ]; then echo "[ok] DataHub CLI"; else echo "[missing] DataHub CLI — run 'make mcp-install'; check for PATH shadowing"; missing=1; fi; \
	if [ -n "$$tool_bin" ] && [ "$$(command -v mcp-server-datahub 2>/dev/null || true)" = "$$tool_bin/mcp-server-datahub" ]; then echo "[ok] DataHub MCP server"; else echo "[missing] DataHub MCP server — run 'make mcp-install'; check for PATH shadowing"; missing=1; fi; \
	echo "== Codex integration (optional for Sidq CLI) =="; \
	if command -v codex >/dev/null 2>&1; then echo "[ok] Codex CLI"; else echo "[optional missing] Codex CLI — required only to register and use Sidq from Codex"; fi; \
	exit $$missing

demo-up:
	$(DEMO_COMPOSE) up -d --wait postgres
	@$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c "ALTER ROLE sidq WITH PASSWORD 'sidq';" >/dev/null || { echo "ERROR: could not repair the controlled sidq demo role on the existing volume; no data was removed." >&2; exit 1; }
	@docker run --rm --network datahub_network --env PGPASSWORD=sidq postgres:16-alpine psql -v ON_ERROR_STOP=1 -h sidq-demo-postgres -U sidq -d warehouse -c "SELECT 1" >/dev/null || { echo "ERROR: remote demo PostgreSQL authentication failed after repairing the controlled role; inspect the stale demo volume. No data was removed." >&2; exit 1; }

demo-ingest:
	@docker run --rm --network datahub_network --env DATAHUB_TELEMETRY_ENABLED=false --env DATAHUB_GMS_TOKEN -v "$(CURDIR)/demo/ingest.dhub.yaml:/recipe.yml:ro" $(DEMO_INGEST_IMAGE) ingest -c /recipe.yml >/dev/null 2>&1; status=$$?; \
	  [ $$status -eq 0 ] || { echo "ERROR: demo ingestion failed; if DataHub authentication failed (401), export a valid DATAHUB_GMS_TOKEN from your secret manager. Container output was suppressed to prevent credential fragments from leaking." >&2; exit $$status; }; \
	  echo "Demo ingestion completed without exposing container logs or credentials."

demo-break:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c "DO \$$\$$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'raw' AND table_name = 'customers' AND column_name = 'email') THEN ALTER TABLE raw.customers RENAME COLUMN email TO email_address; END IF; END \$$\$$;"

demo-restore:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c "DO \$$\$$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'raw' AND table_name = 'customers' AND column_name = 'email_address') THEN ALTER TABLE raw.customers RENAME COLUMN email_address TO email; END IF; END \$$\$$;"

demo-down:
	$(DEMO_COMPOSE) down --volumes --remove-orphans
