## What changed

-

## Why

<!-- Link the issue if there is one. -->

## How it was tested

<!-- Paste real command output: ruff, mypy, pytest, season suite; docker compose ps and curl /healthz if the stack changed. -->

## Checklist

- [ ] Money stays integer cents, the ledger append-only, settlement never on stale data, AI never prices or settles
- [ ] Tests added or updated; bug fixes include a test that fails before the fix
- [ ] Commits are signed and use a GitHub noreply address
- [ ] No secrets, tokens, webhook URLs, `.env`, database files or real health data in the diff
- [ ] Docs updated where behaviour changed
