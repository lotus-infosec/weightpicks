## Stage

STAGE<NN> — <title> (`stages/STAGE<NN>.md`)

## What changed

-

## How it was tested

<!-- Paste real command output: ruff, mypy, pytest, season suite, docker compose ps, curl /healthz -->

## Checklist

- [ ] Follows the hard rules in `AGENTS.md` §4 (money = int cents, ledger append-only, AI never prices/settles, Clock only, no secrets)
- [ ] Tests added/updated; bug fixes include a failing-first test
- [ ] All commits signed (**Verified**) and authored with the GitHub noreply address
- [ ] No secrets, tokens, webhook URLs, `.env`, DB files or real health data in the diff
- [ ] `PROGRESS.md` updated; new decisions in `DECISIONS.md`; new issues in `TROUBLESHOOTING.md`
- [ ] Docs updated where behaviour changed
