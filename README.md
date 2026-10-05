<p align="center"><img src="docs/assets/logo-readme.png" alt="WeightPicks: over/under challenges" width="240"></p>

# WeightPicks

Self-hosted, fake-money, pick'em-style betting on one person's Garmin weigh-ins and health stats. Friends and family bet against an automated house; lines come from math, bets settle from Garmin data, Discord carries the trash talk.

> **Status:** In development, running locally. Core betting, real Garmin data, Discord, AI props, special events, Goal Reached and seasons are built; backups, appearance and email are next. Self-host instructions arrive with the release.

## Built with AI — fully vibe-coded

This project is **entirely vibe-coded with [Claude Code](https://claude.com/claude-code)**. Claude writes the code, tests, configuration and docs; the owner steers, reviews, runs the checks and decides. Commits made with Claude carry a `Co-Authored-By: Claude` trailer. We'd rather be upfront about it: treat the code accordingly, and report anything that looks off (see `SECURITY.md`).

## Project documents

Planning documents (concept, build plan, stage checklists, progress, decision and troubleshooting logs) and AI-agent instructions are kept **outside version control** on the owner's machine. The repository holds the project's tooling and, from STAGE01 on, the application.

## Layout

```text
app/                     The application: domain logic, services, web, worker, AI, notify
migrations/              Alembic migrations (SQLite)
tests/                   Unit, integration, season simulations and browser (e2e) tests
docker/                  Dockerfile and entrypoint; docker-compose*.yml at the root
scripts/                 Dev helpers (CSS build, browser for e2e, brand icons)
docs/                    Guides (Garmin, Discord, AI, seasons), ADRs, calibration, logo
SECURITY.md              Vulnerability reporting + security baseline
.github/                 CI workflow, Dependabot config, PR template
.pre-commit-config.yaml  Hygiene hooks, gitleaks, noreply-identity guard, ruff, mypy
```

## Workflow in one paragraph

Work one stage at a time on a branch `stage/NN-name`. Every commit is signed and uses your GitHub noreply address. Open a PR, let CI pass, squash-merge, tick the stage's sign-off list and tag `stage-NN-done`.

## Roadmap

STAGE00 setup → STAGE01–07 core engine (ledger, lines, settlement, accounts) → STAGE08 first deploy (**R1**) → STAGE10 real Garmin dry run (**R2**) → STAGE11 private beta (**R3**) → STAGE15 full v1 (**R4**) → STAGE17 self-host release (**R5**).
