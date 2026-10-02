# WeightPicks

Self-hosted, fake-money, pick'em-style betting on one person's Garmin weigh-ins and health stats. Friends and family bet against an automated house; lines come from math, bets settle from Garmin data, Discord carries the trash talk.

> **Status:** Pre-build (STAGE00 complete). Environment and repository tooling are in place; application code begins in STAGE01.

## Built with AI — fully vibe-coded

This project is **entirely vibe-coded with [Claude Code](https://claude.com/claude-code)**. Claude writes the code, tests, configuration and docs; the owner steers, reviews, runs the checks and decides. Commits made with Claude carry a `Co-Authored-By: Claude` trailer. We'd rather be upfront about it: treat the code accordingly, and report anything that looks off (see `SECURITY.md`).

## Project documents

Planning documents (concept, build plan, stage checklists, progress, decision and troubleshooting logs) and AI-agent instructions are kept **outside version control** on the owner's machine. The repository holds the project's tooling and, from STAGE01 on, the application.

## Layout

```text
SECURITY.md              Vulnerability reporting + security baseline
docs/adr/                Architecture decision records (from STAGE01)
docs/calibration/        Line-engine calibration reports (from STAGE04)
.github/                 Dependabot config, PR template (CI arrives in STAGE01)
.pre-commit-config.yaml  Hygiene hooks, gitleaks, noreply-identity guard
```

## Workflow in one paragraph

Work one stage at a time on a branch `stage/NN-name`. Every commit is signed and uses your GitHub noreply address. Open a PR, let CI pass, squash-merge, tick the stage's sign-off list and tag `stage-NN-done`.

## Roadmap

STAGE00 setup → STAGE01–07 core engine (ledger, lines, settlement, accounts) → STAGE08 first deploy (**R1**) → STAGE10 real Garmin dry run (**R2**) → STAGE11 private beta (**R3**) → STAGE15 full v1 (**R4**) → STAGE17 self-host release (**R5**).
