DEMO_COMPOSE := docker compose -f demo/docker-compose.yml
DEMO_INGEST_IMAGE := acryldata/datahub-ingestion:v1.5.0.6

VENV ?= .venv

DATAHUB_GMS_URL ?= http://localhost:8080
AUDIT_BUDGET ?= 5
# The most-consumed asset in DataHub's own showcase-ecommerce sample, so the
# budget above always reaches it and the readback below always has a receipt.
RECEIPT_URN ?= urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)

REPAIR_BUDGET ?= 15

.PHONY: check regen regen-check gate-demo live-loop converge-demo swarm-demo repair-demo repair-reset demo-up demo-ingest demo-break demo-restore demo-down

# The runbook's first row promises a clone and `make` are enough, and until
# 2026-07-31 that promise was false: a fresh clone had no virtualenv and the
# first command a judge types died with a path error. This rule keeps the
# promise — the first target that needs the venv builds it, once, with exactly
# the dev extras `make check` runs. Order-only (`|`) everywhere below, so an
# existing venv is never rebuilt behind anyone's back. The live rows still
# need DataHub and the isolated MCP server from docs/SETUP.md; that boundary
# is theirs, not this rule's.
$(VENV)/bin/python:
	@echo "first run: building $(VENV) (about a minute) =="
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install --quiet --upgrade pip
	$(VENV)/bin/python -m pip install --quiet --editable '.[dev]'

check: | $(VENV)/bin/python
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy src/
	$(VENV)/bin/pytest -q

# Published artifacts are generated, and `make check` fails when a committed copy
# no longer matches the engine. Editing the policy is the usual cause: it changes
# the policy hash, which every published artifact quotes. Run `make regen` and
# commit the result — never hand-edit the artifacts back into agreement.
regen: | $(VENV)/bin/python
	$(VENV)/bin/python scripts/regenerate_example_01.py
	$(VENV)/bin/python scripts/measure_reconcile.py
	$(VENV)/bin/python scripts/train_preflight.py
	$(VENV)/bin/python scripts/eval_preflight.py

regen-check: | $(VENV)/bin/python
	$(VENV)/bin/python scripts/regenerate_example_01.py --check
	$(VENV)/bin/python scripts/measure_reconcile.py --check
	$(VENV)/bin/python scripts/train_preflight.py --check
	$(VENV)/bin/python scripts/eval_preflight.py --check

# The command the landing page tells a judge to run. It existed only on the page
# until 2026-07-30, which meant the one instruction on the first surface a judge
# opens did not work. It runs the flagship change through the real engine against
# the committed graph recording and prints the verdict, so it produces the same
# answer on any machine with no DataHub, no network, and no credentials.
gate-demo: | $(VENV)/bin/python
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
live-loop: | $(VENV)/bin/python
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
converge-demo: | $(VENV)/bin/python
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
swarm-demo: | $(VENV)/bin/python
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
repair-demo: | $(VENV)/bin/python
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq repair --via-mcp --budget $(REPAIR_BUDGET) 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "repair could not read the catalog"; exit 1; }

repair-reset: | $(VENV)/bin/python
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/python scripts/reset_repair_demo.py 2>/dev/null

demo-up:
	$(DEMO_COMPOSE) up -d --wait postgres

demo-ingest:
	docker run --rm --network datahub_network -e DATAHUB_TELEMETRY_ENABLED=false -v "$(CURDIR)/demo/ingest.dhub.yaml:/recipe.yml:ro" $(DEMO_INGEST_IMAGE) ingest -c /recipe.yml

demo-break:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c "DO \$$\$$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'raw' AND table_name = 'customers' AND column_name = 'email') THEN ALTER TABLE raw.customers RENAME COLUMN email TO email_address; END IF; END \$$\$$;"

demo-restore:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c "DO \$$\$$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'raw' AND table_name = 'customers' AND column_name = 'email_address') THEN ALTER TABLE raw.customers RENAME COLUMN email_address TO email; END IF; END \$$\$$;"

demo-down:
	$(DEMO_COMPOSE) down --volumes --remove-orphans
