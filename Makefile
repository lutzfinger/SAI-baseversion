PYTHON ?= .venv/bin/python
UVICORN_HOST ?= 127.0.0.1
UVICORN_PORT ?= 8000

.PHONY: help install dev auth-newsletters auth-newsletter-tags auth-sai-email run-newsletters run-newsletter-tags run-sai-email privacy-scan test lint typecheck log-maintenance log-maintenance-dry compose-up compose-down compose-pull-model compose-logs compose-shell

help:
	@printf '%s\n' \
		'make install               Editable install + dev extras (re-run after pyproject.toml changes)' \
		'make dev                   Start the local API' \
		'make compose-up            Start sai + ollama in docker compose' \
		'make compose-pull-model    Pull qwen2.5:7b into the compose ollama service' \
		'make compose-down          Stop containers (keep volumes)' \
		'make compose-logs          Tail compose logs (sai + ollama)' \
		'make compose-shell         Open a bash shell in the running sai container' \
		'make auth-newsletters      Authenticate Gmail for read-only newsletter classification' \
		'make auth-newsletter-tags  Authenticate Gmail for newsletter tagging' \
		'make auth-sai-email        Authenticate Gmail for the starter email interaction workflow' \
		'make run-newsletters       Run the read-only newsletter workflow' \
		'make run-newsletter-tags   Run the newsletter tagging workflow' \
		'make run-sai-email         Run the starter email interaction workflow' \
		'make privacy-scan          Search for likely private strings and local path leaks' \
		'make log-maintenance       Run nightly log maintenance once (rotate audit, prune checkpoints, rotate ollama)' \
		'make log-maintenance-dry   Dry-run all three log-maintenance steps without making changes' \
		'make test                  Run pytest' \
		'make lint                  Run Ruff' \
		'make typecheck             Run mypy'

install:
	@$(PYTHON) -m pip install -e '.[dev]'

dev:
	@$(PYTHON) -m uvicorn app.main:app --reload --host $(UVICORN_HOST) --port $(UVICORN_PORT)

auth-newsletters:
	@$(PYTHON) scripts/auth_gmail.py --workflow-id newsletter-identification-gmail

auth-newsletter-tags:
	@$(PYTHON) scripts/auth_gmail.py --workflow-id newsletter-identification-gmail-tagging

auth-sai-email:
	@$(PYTHON) scripts/auth_gmail.py --workflow-id starter-email-interaction

run-newsletters:
	@$(PYTHON) scripts/run_workflow.py --workflow-id newsletter-identification-gmail

run-newsletter-tags:
	@$(PYTHON) scripts/run_workflow.py --workflow-id newsletter-identification-gmail-tagging

run-sai-email:
	@$(PYTHON) scripts/run_workflow.py --workflow-id starter-email-interaction

privacy-scan:
	@find . -type f \( -name '.env' -o -name '*.db' -o -name '*.sqlite' -o -name '*.pyc' \) -print
	@user_path='/Use''rs/'; home_path='/ho''me/'; rg --hidden -n -S "$$user_path|$$home_path" . || true

log-maintenance:
	@/bin/sh scripts/run_log_maintenance.sh && echo "log-maintenance complete; tail $(HOME)/Library/Logs/SAI/log-maintenance.log for details"

log-maintenance-dry:
	@$(PYTHON) scripts/rotate_audit_log.py --dry-run
	@$(PYTHON) scripts/prune_langgraph_checkpoints.py --dry-run
	@/bin/sh scripts/rotate_ollama_log.sh

test:
	@$(PYTHON) -m pytest

lint:
	@$(PYTHON) -m ruff check .

typecheck:
	@$(PYTHON) -m mypy app tests

# ─── docker compose helpers ────────────────────────────────────────────────
# A user with Docker installed can run SAI end-to-end without installing
# Python or Ollama on the host. See README "Quickstart with Docker".

compose-up:
	@docker compose up -d --build
	@echo "sai control plane: http://localhost:8000"
	@echo "to load a local model: make compose-pull-model"

compose-pull-model:
	@docker compose exec ollama ollama pull qwen2.5:7b

compose-down:
	@docker compose down

compose-logs:
	@docker compose logs -f

compose-shell:
	@docker compose exec sai bash
