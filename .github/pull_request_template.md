## What

<!-- One or two sentences.  Link the issue if there is one. -->

## Why

## How it was verified

- [ ] `make check` passes locally (ruff + pytest)
- [ ] DB-backed tests ran (`make test-db`, or CI's Postgres job is green)
- [ ] Tried against a running instance (`make dev` or `docker compose up`)

## Backend checklist (skip if `cloud/` is untouched)

- [ ] No secrets, tokens, raw IPs, emails or hardware IDs in code, tests, fixtures or log lines
- [ ] New request models set `max_length` / bounds on every string and number
- [ ] Anything identifying that is stored is a peppered HMAC (`security.py`), never plaintext
- [ ] `schema.sql` changes are idempotent (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) and only add
- [ ] New endpoints have a test in `cloud/tests/`
- [ ] New env vars are in `settings.py`, `.env.example` and `DEPLOY.md`
