.PHONY: install test test-all lint format up down logs migrate demo-history load evals evals-offline

install:
	pip install -e ".[dev]"

test:
	pytest -q

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
