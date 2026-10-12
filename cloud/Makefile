# Vispora-Cloud developer shortcuts.  `make help` lists them.
# Uses `>` instead of a tab as the recipe prefix so copy/paste can't break it.
.RECIPEPREFIX = >
SHELL := /bin/sh

PY    ?= python3
VENV  ?= .venv
BIN   := $(VENV)/bin
DB_URL ?= postgresql://vispora:vispora@127.0.0.1:5432/vispora

.PHONY: help venv install dev test test-db lint fmt audit compile peppers db-up db-down check

help:            ## Show this help
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

venv:            ## Create the virtualenv
> test -d $(VENV) || $(PY) -m venv $(VENV)
> $(BIN)/pip install --quiet --upgrade pip

install: venv    ## Install runtime + dev dependencies (hash-verified)
> $(BIN)/pip install --require-hashes -r requirements.txt -r requirements-dev.txt

dev:             ## Run the API locally with auto-reload (needs DATABASE_URL or .env)
> $(BIN)/uvicorn main:app --reload --port 8000

test:            ## Unit + middleware tests (no database needed)
> $(BIN)/pytest

test-db: db-up   ## Everything, including the Postgres-backed route tests
> TEST_DATABASE_URL=$(DB_URL) $(BIN)/pytest

lint:            ## ruff check + format check (what CI runs)
> $(BIN)/ruff check .
> $(BIN)/ruff format --check .

fmt:             ## Auto-fix lint findings and reformat
> $(BIN)/ruff check --fix .
> $(BIN)/ruff format .

audit:           ## Audit installed packages for known vulnerabilities
> $(BIN)/pip-audit --strict --desc on

compile:         ## Regenerate requirements*.txt (pinned + hashed) from requirements*.in
> $(BIN)/pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in
> $(BIN)/pip-compile --quiet --generate-hashes --strip-extras --allow-unsafe -o requirements-dev.txt requirements-dev.in

peppers:         ## Print four fresh peppers to paste into Railway → Variables
> $(BIN)/python -m security

db-up:           ## Start the local Postgres (docker compose)
> docker compose up -d --wait db

db-down:         ## Stop the local Postgres
> docker compose down

check: lint test ## What a PR must pass
