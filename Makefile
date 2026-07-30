DEMO_COMPOSE := docker compose -f demo/docker-compose.yml
DEMO_INGEST_IMAGE := acryldata/datahub-ingestion:v1.5.0.6

VENV ?= .venv

.PHONY: check regen regen-check demo-up demo-ingest demo-break demo-restore demo-down

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

regen-check:
	$(VENV)/bin/python scripts/regenerate_example_01.py --check
	$(VENV)/bin/python scripts/measure_reconcile.py --check

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
