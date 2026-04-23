.PHONY: up down logs reset-db test lint format demo-fairness demo-reclaim demo-reset wait-healthy

COMPOSE := docker compose

up:
	$(COMPOSE) up -d --build
	./scripts/wait-for-health.sh

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f app

reset-db:
	$(COMPOSE) exec -T postgres psql -U nurix -d nurix -c \
		"DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	$(COMPOSE) exec -T postgres psql -U nurix -d nurix -f /docker-entrypoint-initdb.d/01-schema.sql

test:
	. .venv/bin/activate && pytest tests/

lint:
	. .venv/bin/activate && ruff check . && ruff format --check . && mypy app/

format:
	. .venv/bin/activate && ruff format . && ruff check --fix .

demo-fairness:
	. .venv/bin/activate && python scripts/demo_seed_fairness.py

demo-reclaim:
	. .venv/bin/activate && python scripts/demo_seed_reclaim.py

demo-reset:
	. .venv/bin/activate && python scripts/demo_seed_reset.py
