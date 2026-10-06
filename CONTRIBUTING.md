# Contributing

Thanks for helping. WeightPicks is small and opinionated; please open an issue before a large change.

## Ground rules

- **No secrets or real health data**, ever: no `.env`, tokens, webhook URLs, Garmin credentials, database files or backups. gitleaks runs on every commit and in CI.
- **Money is integer cents**, the ledger is append-only, settlement never runs on stale data, and AI never sets odds or settles bets. Changes to those rules need an issue first.
- **Original branding only:** no real sportsbook names, logos or assets.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` ...). Signed commits are required on `main`; use your GitHub noreply address as the commit email.

## Development

```bash
uv sync
./scripts/init-dev-env.sh                      # dev settings outside the repo
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
pre-commit install --hook-type pre-commit --hook-type pre-push
```

Development uses simulated Garmin data and a simulated clock (`uv run wp sim advance --days 7`, or Admin -> Dev clock).

## Before opening a PR

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run pytest -q
uv run pytest tests/season -q
pre-commit run --all-files
```

Bug fixes come with a test that fails before the fix. New routes follow the checklist in `docs/threat-model.md` (CSRF, auth, body limits); `tests/integration/test_route_matrix.py` must stay green.

## Reporting security issues

Privately, through [Report a vulnerability](https://github.com/lotus-infosec/weightpicks/security/advisories/new). See `SECURITY.md`.
