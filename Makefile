DEMO_COMPOSE := docker compose -f demo/docker-compose.yml
DEMO_INGEST_IMAGE := acryldata/datahub-ingestion:v1.5.0.6

.PHONY: demo-up demo-ingest demo-break demo-restore demo-down

demo-up:
	$(DEMO_COMPOSE) up -d --wait

demo-ingest:
	docker run --rm --network datahub_network -e DATAHUB_TELEMETRY_ENABLED=false -v "$(CURDIR)/demo/ingest.dhub.yaml:/ingest.dhub.yaml:ro" $(DEMO_INGEST_IMAGE) ingest run -c /ingest.dhub.yaml --no-spinner --no-progress

demo-break:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c 'ALTER TABLE raw.customers RENAME COLUMN email TO email_address;'

demo-restore:
	$(DEMO_COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sidq -d warehouse -c 'ALTER TABLE raw.customers RENAME COLUMN email_address TO email;'

demo-down:
	$(DEMO_COMPOSE) down --volumes --remove-orphans
