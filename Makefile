.PHONY: install test test-pg test-all lint format up down logs smoke showcase record-demos migrate demo-history load evals evals-offline

PG_TEST_CONTAINER ?= opspilot-test-pg
PG_TEST_PORT ?= 5435
PG_TEST_URL = postgresql+psycopg://test:test@127.0.0.1:$(PG_TEST_PORT)/opspilot_test

install:
	pip install -e ".[dev]"

test:
	pytest -q

# Throwaway Postgres for the queue/API/worker tests; the container is always stopped.
# pg_isready uses TCP because the image's init server listens only on the Unix socket.
test-pg:
	@trap 'docker stop $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	trap 'exit 130' INT TERM; \
	docker run --rm -d --name $(PG_TEST_CONTAINER) -p 127.0.0.1:$(PG_TEST_PORT):5432 \
		-e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=opspilot_test \
		postgres:16-alpine >/dev/null || exit 1; \
	echo "waiting for postgres on port $(PG_TEST_PORT)..."; \
	for i in $$(seq 1 30); do \
		docker exec $(PG_TEST_CONTAINER) pg_isready -q -h 127.0.0.1 -U test -d opspilot_test \
			&& break; \
		[ $$i -eq 30 ] && { echo "postgres did not become ready"; exit 1; }; \
		sleep 1; \
	done; \
	TEST_DATABASE_URL=$(PG_TEST_URL) pytest -q -rs

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

smoke:
	python -m scripts.smoke

# Showcase site on http://localhost:8090 with live demo runs (needs `make up`)
showcase:
	python -m scripts.showcase_server

# Re-record the demos the public site replays (about 15 minutes, needs `make up`)
record-demos:
	python -m scripts.record_demos

migrate:
	docker compose run --rm migrate

demo-history:
	python -m scripts.deploy_history init

load:
	python -m scripts.load --rate 8 --duration 120

evals:
	python -m evals.runner live

evals-offline:
	python -m evals.runner offline
