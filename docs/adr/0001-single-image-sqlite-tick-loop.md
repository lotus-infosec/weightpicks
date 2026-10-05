# ADR 0001 — One image, SQLite, and a tick-loop worker

- **Status:** Accepted
- **Date:** 2026-10-02

## Context

WeightPicks is a self-hosted hobby app for about 20 players on one small Ubuntu host. It must be easy to install, cheap to run, crash-safe and simple to debug. The only deployment method is Docker Compose on Ubuntu, with a host-level Cloudflare Tunnel as the only ingress.

## Decision

1. **One image, two long-running services.** A single image runs as `web` (FastAPI + Jinja/HTMX under uvicorn) and `worker` (scheduler and background work). `docker/entrypoint.sh` picks the role. A one-off `garmin-login` service lives under the Compose `tools` profile.
2. **SQLite in WAL mode is the database and the only channel between services.** `web` and `worker` share `app.db` on the `wp_data` volume. Commands from web to worker and outgoing side effects (Discord, email) are rows (`commands`, `outbox`) written in the same transaction as the business change. There is no broker, no Redis and no HTTP between containers. Every write is one short `BEGIN IMMEDIATE` transaction (`busy_timeout=10000`; within a process, writers also queue on a lock, see `app/core/db.py`).
3. **A 60-second tick loop instead of cron or a job queue.** Each job declares `due(now) -> period_key | None`. The runner claims `(job, period_key)` in `job_runs` (unique) before running, so a period runs exactly once, and after downtime the next tick catches up naturally. All time comes from an injected `Clock`, so the simulator can fast-forward a season through the same code path.
4. **Stack:** Python 3.12, FastAPI, Jinja2 + HTMX + Alpine.js, SQLAlchemy 2 + Alembic (`render_as_batch=True`), Pydantic v2, structlog, uv.

## Alternatives considered

- **Separate images for web and worker:** more build surface and possible version skew between them.
- **PostgreSQL:** another service to run, back up and upgrade; unnecessary at this write volume.
- **Celery/RQ + Redis, or APScheduler:** extra moving parts; exactly-once and catch-up would still need app-level bookkeeping.

## Consequences

- Backup is one consistent SQLite snapshot (`VACUUM INTO`) plus uploads.
- Both containers must run on the same host against a local volume, never NFS/SMB.
- Long network calls (AI, Discord, Garmin sync) must stay outside write transactions to avoid lock contention.
- Horizontal scaling is out of scope by design.
