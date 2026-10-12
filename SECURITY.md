# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's vulnerability
reporting: **Security tab → Report a vulnerability** on this repository.
Do not open a public issue or pull request for a security bug.

Include what you found, how to reproduce it, and what you think the impact
is.  You will get an acknowledgement, and a fix or a decision, as quickly as
a one-person project can manage; please allow a reasonable window before
disclosing publicly.

> Repository owner: private reporting has to be switched on once, under
> **Settings → Code security and analysis → Private vulnerability reporting**.

## Scope

- `cloud/` — the FastAPI backend deployed on Railway (licensing, trials,
  anonymous device registry).  Its security model is documented in
  [`cloud/README.md`](cloud/README.md#security-model).
- The Windows desktop app in the repository root.

## What the backend knows

The backend is built to hold nothing that identifies a person: hardware
fingerprints, emails, license keys and IP addresses are stored only as
peppered HMACs, and client IPs are scrubbed from logs before they leave the
process.  Anyone can verify the complete server-side record for a license at
`GET /v1/licenses/{id}/public`.

## Supported versions

The current `main` branch of the backend, and the latest stable release of
the desktop app.
