DEMO_COMPOSE := docker compose -f demo/docker-compose.yml
DEMO_INGEST_IMAGE := acryldata/datahub-ingestion:v1.5.0.6

VENV ?= .venv

DATAHUB_GMS_URL ?= http://localhost:8080
AUDIT_BUDGET ?= 5
# The most-consumed asset in DataHub's own showcase-ecommerce sample, so the
# budget above always reaches it and the readback below always has a receipt.
RECEIPT_URN ?= urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)
UNAUDITED_URN ?= urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.warehouses,PROD)

REPAIR_BUDGET ?= 15

.PHONY: check regen regen-check gate-demo live-loop converge-demo repair-demo repair-reset demo-up demo-ingest demo-break demo-restore demo-down

check:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy src/
	$(VENV)/bin/pytest -q

# Published artifacts are generated, and `make check` fails when a committed copy
# no longer matches the engine. Editing the policy is the usual cause: it changes
# the policy hash, which every published artifact quotes. Run `make regen` and
# commit the result — never hand-edit the artifacts back into agreement.
regen:
	$(VENV)/bin/python scripts/regenerate_example_01.py
	$(VENV)/bin/python scripts/measure_reconcile.py
	$(VENV)/bin/python scripts/train_preflight.py
	$(VENV)/bin/python scripts/eval_preflight.py

regen-check:
	$(VENV)/bin/python scripts/regenerate_example_01.py --check
	$(VENV)/bin/python scripts/measure_reconcile.py --check
	$(VENV)/bin/python scripts/train_preflight.py --check
	$(VENV)/bin/python scripts/eval_preflight.py --check

# The command the landing page tells a judge to run. It existed only on the page
# until 2026-07-30, which meant the one instruction on the first surface a judge
# opens did not work. It runs the flagship change through the real engine against
# the committed graph recording and prints the verdict, so it produces the same
# answer on any machine with no DataHub, no network, and no credentials.
gate-demo:
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
# reaches the same conclusion. Step 4 is the other half: an asset the audit never
# reached must come back NOT VERIFIED, because "we did not check" and "we checked
# and it passed" are the two answers this project exists to keep apart.
live-loop:
	@echo "== 1+2. read via official MCP, decide, write receipts via official MCP =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }
	@echo
	@echo "== 3. a separate process reads the receipt back and judges it itself =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq verify '$(RECEIPT_URN)' 2>/dev/null
	@echo
	@echo "== 4. and an asset the audit never reached is NOT reported as clean =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq verify '$(UNAUDITED_URN)' 2>/dev/null; \
	  status=$$?; [ $$status -eq 1 ] || { echo "expected NOT VERIFIED, got exit $$status"; exit 1; }

# The audit that resumes. Run one spends the budget worst-first and writes
# receipts; run two reads those receipts back and spends the *same* budget on
# assets run one never reached. No state file is written anywhere in between —
# the memory is the catalog itself, so this is two agents cooperating through
# receipts alone. Watch the `vouched` line appear and `NOT examined` fall.
converge-demo:
	@echo "== run 1: spend the budget worst-first, write receipts =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }
	@echo
	@echo "== run 2: same budget — the receipts vouch, the budget moves on =="
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq audit --via-mcp --budget $(AUDIT_BUDGET) --write-receipts --resume 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "audit could not read the catalog"; exit 1; }

# The repair agent, on live DataHub. It proposes only from catalog evidence, then
# re-runs the deterministic engine against the catalog each repair *would* create
# and keeps what survives. On the showcase sample the interesting part is what it
# refuses: tagging just the column named in the finding resolves that finding and
# immediately creates a new one downstream, so the proposal it offers instead
# covers the whole field-lineage closure — 7 columns across dbt, Snowflake and
# Looker, in one MCP call.
#
# Dry run. `sidq repair --via-mcp --apply` writes it; `make repair-reset` restores
# the sample afterwards so the demonstration can be run again.
repair-demo:
	@DATAHUB_GMS_URL=$(DATAHUB_GMS_URL) DATAHUB_TELEMETRY_ENABLED=false \
	  $(VENV)/bin/sidq repair --via-mcp --budget $(REPAIR_BUDGET) 2>/dev/null; \
	  status=$$?; [ $$status -le 1 ] || { echo "repair could not read the catalog"; exit 1; }

repair-reset:
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
