<p align="center"><img src="docs/assets/logo-readme.png" alt="WeightPicks: over/under challenges" width="240"></p>

# WeightPicks

[![ci](https://github.com/lotus-infosec/weightpicks/actions/workflows/ci.yml/badge.svg)](https://github.com/lotus-infosec/weightpicks/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/lotus-infosec/weightpicks?include_prereleases&sort=semver)](https://github.com/lotus-infosec/weightpicks/releases)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Self-hosted, fake-money, pick'em-style betting on one person's Garmin weigh-ins and health stats. Friends and family bet against an automated house; lines come from math, bets settle from Garmin data, Discord carries the trash talk.

> **Status:** v1 release candidate. Feature-complete, security-reviewed, with an installer for Ubuntu and published multi-arch images.

## Quick start

On an Ubuntu 22.04/24.04 machine (amd64 or arm64), with a domain on Cloudflare:

```bash
sudo git clone https://github.com/lotus-infosec/weightpicks.git /opt/weightpicks
cd /opt/weightpicks && sudo git checkout "$(sudo git describe --tags --abbrev=0 --match 'v*')"   # latest release
sudo ./scripts/install.sh
```

The installer sets up Docker, the app and the Cloudflare Tunnel, then prints a one-time token for the first-run wizard at `https://<your hostname>/setup`. The full walkthrough, with the Cloudflare and Garmin steps, is in **[docs/self-host](docs/self-host/README.md)**.

## Built with AI — fully vibe-coded

This project is **entirely vibe-coded with [Claude Code](https://claude.com/claude-code)**. Claude writes the code, tests, configuration and docs; the owner steers, reviews, runs the checks and decides. Commits made with Claude carry a `Co-Authored-By: Claude` trailer. We'd rather be upfront about it: treat the code accordingly, and report anything that looks off (see `SECURITY.md`).

## What it does

- **Markets:** daily, weekly and monthly over/unders on weigh-ins and activity (steps, active and intensity minutes, calories, workouts), priced by a line engine (trend, noise, Monte Carlo) with vig. Props, futures and parlays; special-event pools.
- **Settlement:** automatic from Garmin data (GarminDB), never on stale data; a missed weigh-in pushes. Goal Reached settles or refunds everything and freezes the season; a new season carries balances over.
- **Players:** invite code, bankroll, daily allowance, busts and bailouts, leaderboard, stats.
- **Admin:** dashboard, markets, players, bank and economy, props and the AI review queue, special events, seasons, System (health, backups, restore, factory reset), Appearance, Integrations, audit log.
- **Integrations, all optional:** Discord webhooks, Cloudflare Workers AI (only packages props and writes copy; never sets odds or settles), SMTP for password reset.

## Guides

- **Self-hosting:** [overview](docs/self-host/README.md) · [install](docs/self-host/install.md) · [Cloudflare Tunnel](docs/self-host/cloudflared.md) · [Garmin](docs/self-host/garmin.md) · [upgrade](docs/self-host/upgrade.md) · [operations](docs/self-host/operations.md) · [troubleshooting](docs/self-host/troubleshooting.md) · [going live](docs/self-host/go-live.md)
- [Discord](docs/discord.md) · [Workers AI](docs/ai.md) · [Seasons and special events](docs/seasons.md) · [Backups, restore and reset](docs/backups.md)
- [Threat model](docs/threat-model.md) · [Security policy](SECURITY.md) · [Architecture decision](docs/adr/0001-single-image-sqlite-tick-loop.md) · [Line-engine calibration](docs/calibration/)

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [private vulnerability reporting](SECURITY.md). WeightPicks is [MIT-licensed](LICENSE); bundled third-party software is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The project's planning documents (concept, build plan, stage checklists, decision logs) and AI-agent instructions are kept outside version control.

## Layout

```text
app/                     The application: domain logic, services, web, worker, AI, notify
migrations/              Alembic migrations (SQLite)
tests/                   Unit, integration (incl. route matrix, load test), season simulations
                         (incl. chaos run, restore drill) and browser (e2e) tests
docker/                  Dockerfile and entrypoint; docker-compose*.yml at the root
scripts/                 install.sh, upgrade.sh; dev helpers (CSS build, e2e browser, brand icons)
docs/                    Guides, threat model, ADRs, calibration reports, logo
SECURITY.md              Vulnerability reporting + security baseline
.github/                 CI and release workflows, Dependabot, issue and PR templates
.pre-commit-config.yaml  Hygiene hooks, gitleaks, noreply-identity guard, ruff, mypy
```

## Status

Version 1 is released: the betting engine and ledger, automatic settlement from Garmin, Discord posts, AI-packaged props, special events, seasons, backups and the self-host installer. What changed in each version is on the [Releases](https://github.com/lotus-infosec/weightpicks/releases) page.
