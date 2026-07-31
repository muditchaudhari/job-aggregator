.DEFAULT_GOAL := help
.PHONY: help run results report status reset autostart autostart-remove start stop restart tail demo-page install dev up down logs migrate db-upgrade db-downgrade revision test test-cov lint fmt typecheck check check-llm api worker beat shell psql clean

start: ## (later) run api+worker+scheduler in the background
	@bash scripts/stack.sh start

stop: ## (later) stop the background processes
	@bash scripts/stack.sh stop

restart: ## (later) restart the background processes
	@bash scripts/stack.sh restart

tail: ## (later) follow background logs
	@bash scripts/stack.sh logs

run: ## Scan every portal in config/portals.txt and print the results
	@.venv/bin/python -m app.cli run

results: ## Show your matched jobs (no scanning). e.g. make results MIN=0.5
	@.venv/bin/python -m app.cli results $(if $(MIN),--min $(MIN),) $(if $(WHERE),--location $(WHERE),) --why

report: ## Write matches to results.html and open it
	@.venv/bin/python -m app.cli results --limit 5000 --all --html results.html
	@open results.html 2>/dev/null || true

status: ## What is registered and what each portal returned
	@.venv/bin/python -m app.cli status

reset: ## Clear scanned jobs and portals (add ALL=1 to drop learned selectors)
	@.venv/bin/python -m app.cli reset $(if $(ALL),--all,)

autostart: ## Start automatically at login (macOS launchd)
	@bash scripts/install-autostart.sh

autostart-remove: ## Stop starting automatically
	@bash scripts/install-autostart.sh remove

demo-page: ## Serve the sample non-ATS careers page on :8080
	@cd demo && python3 -m http.server 8080

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create a venv and install the package with dev extras
	python3.12 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev,gemini,anthropic,openai]"
	.venv/bin/playwright install chromium

check-llm: ## Verify the configured LLM end to end against the live API
	.venv/bin/python scripts/check_llm.py

dev: install ## Install and seed a .env
	@test -f .env || cp .env.example .env
	@echo "Edit .env, then: make up && make migrate"

up: ## Start the full stack
	docker compose up -d --build

down: ## Stop the stack (volumes retained)
	docker compose down

logs: ## Tail application logs
	docker compose logs -f api worker beat

migrate: ## Apply migrations inside the Docker stack
	docker compose run --rm migrate

db-upgrade: ## Apply migrations against the DATABASE_URL in .env (no Docker)
	.venv/bin/alembic upgrade head

db-downgrade: ## Roll back one migration (no Docker)
	.venv/bin/alembic downgrade -1

revision: ## Autogenerate a migration: make revision m="add x"
	.venv/bin/alembic revision --autogenerate -m "$(m)"

test: ## Run the unit and integration suites
	.venv/bin/pytest

test-cov: ## Run tests with a coverage report
	.venv/bin/pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Lint
	.venv/bin/ruff check app tests

fmt: ## Format and autofix
	.venv/bin/ruff format app tests
	.venv/bin/ruff check --fix app tests

typecheck: ## Type-check
	.venv/bin/mypy app

check: lint typecheck test ## Everything CI runs

api: ## Run the API locally with reload
	.venv/bin/uvicorn app.main:app --reload --port 8000

worker: ## Run a Celery worker locally
	.venv/bin/celery -A app.scheduler.celery_app.celery_app worker --loglevel=info --concurrency=2

beat: ## Run Celery Beat locally
	.venv/bin/celery -A app.scheduler.celery_app.celery_app beat --loglevel=info

psql: ## Open a database shell
	docker compose exec postgres psql -U jobs -d jobs

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
