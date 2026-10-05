<p align="center"><img src="docs/assets/logo-readme.png" alt="WeightPicks: over/under challenges" width="240"></p>

# WeightPicks

Self-hosted, fake-money, pick'em-style betting on one person's Garmin weigh-ins and health stats. Friends and family bet against an automated house; lines come from math, bets settle from Garmin data, Discord carries the trash talk.

> **Status:** Feature-complete for v1 and security-reviewed (STAGE16), running locally. Self-host instructions arrive with the release (STAGE17).

## Built with AI — fully vibe-coded

This project is **entirely vibe-coded with [Claude Code](https://claude.com/claude-code)**. Claude writes the code, tests, configuration and docs; the owner steers, reviews, runs the checks and decides. Commits made with Claude carry a `Co-Authored-By: Claude` trailer. We'd rather be upfront about it: treat the code accordingly, and report anything that looks off (see `SECURITY.md`).

## What it does

- **Markets:** daily, weekly and monthly over/unders on weigh-ins and activity (steps, active and intensity minutes, calories, workouts), priced by a line engine (trend, noise, Monte Carlo) with vig. Props, futures and parlays; special-event pools.
- **Settlement:** automatic from Garmin data (GarminDB), never on stale data; a missed weigh-in pushes. Goal Reached settles or refunds everything and freezes the season; a new season carries balances over.
- **Players:** invite code, bankroll, daily allowance, busts and bailouts, leaderboard, stats.
- **Admin:** dashboard, markets, players, bank and economy, props and the AI review queue, special events, seasons, System (health, backups, restore, factory reset), Appearance, Integrations, audit log.
- **Integrations, all optional:** Discord webhooks, Cloudflare Workers AI (only packages props and writes copy; never sets odds or settles), SMTP for password reset.

## Guides

- [Garmin](docs/garmin.md) · [Discord](docs/discord.md) · [Workers AI](docs/ai.md) · [Seasons and special events](docs/seasons.md) · [Backups, restore and reset](docs/backups.md)
- [Threat model](docs/threat-model.md) · [Security policy](SECURITY.md) · [Architecture decision](docs/adr/0001-single-image-sqlite-tick-loop.md) · [Line-engine calibration](docs/calibration/)

## Project documents

Planning documents (concept, build plan, stage checklists, progress, decision and troubleshooting logs) and AI-agent instructions are kept **outside version control** on the owner's machine. The repository holds the project's tooling and, from STAGE01 on, the application.

## Layout

```text
app/                     The application: domain logic, services, web, worker, AI, notify
migrations/              Alembic migrations (SQLite)
tests/                   Unit, integration (incl. route matrix, load test), season simulations
                         (incl. chaos run, restore drill) and browser (e2e) tests
docker/                  Dockerfile and entrypoint; docker-compose*.yml at the root
scripts/                 Dev helpers (CSS build, browser for e2e, brand icons)
docs/                    Guides, threat model, ADRs, calibration reports, logo
SECURITY.md              Vulnerability reporting + security baseline
.github/                 CI workflow, Dependabot config, PR template
.pre-commit-config.yaml  Hygiene hooks, gitleaks, noreply-identity guard, ruff, mypy
```

## Workflow in one paragraph

Work one stage at a time on a branch `stage/NN-name`. Every commit is signed and uses your GitHub noreply address. Open a PR, let CI pass, squash-merge, tick the stage's sign-off list and tag `stage-NN-done`.

## Roadmap

STAGE00 setup → STAGE01–07 core engine (ledger, lines, settlement, accounts) → STAGE08 first deploy (**R1**) → STAGE10 real Garmin dry run (**R2**) → STAGE11 private beta (**R3**) → STAGE15 full v1 (**R4**) → STAGE16 hardening and security review → STAGE17 self-host release (**R5**).
