PYTHON ?= .venv/bin/python
UVICORN_HOST ?= 127.0.0.1
UVICORN_PORT ?= 8000

.PHONY: help dev auth-newsletters auth-newsletter-tags auth-sai-email run-newsletters run-newsletter-tags run-sai-email privacy-scan test lint typecheck

help:
	@printf '%s\n' \
		'make dev                   Start the local API' \
		'make auth-newsletters      Authenticate Gmail for read-only newsletter classification' \
		'make auth-newsletter-tags  Authenticate Gmail for newsletter tagging' \
		'make auth-sai-email        Authenticate Gmail for the starter email interaction workflow' \
		'make run-newsletters       Run the read-only newsletter workflow' \
		'make run-newsletter-tags   Run the newsletter tagging workflow' \
		'make run-sai-email         Run the starter email interaction workflow' \
		'make privacy-scan          Search for likely private strings and local path leaks' \
		'make test                  Run pytest' \
		'make lint                  Run Ruff' \
		'make typecheck             Run mypy'

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

test:
	@$(PYTHON) -m pytest

lint:
	@$(PYTHON) -m ruff check .

typecheck:
	@$(PYTHON) -m mypy app tests
