"""`wp` command line: migrations and container health checks."""

import argparse
import sys
from datetime import timedelta

import httpx
import structlog
from sqlalchemy import select

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import current_revision, upgrade_to_head
from app.models import Heartbeat

WEB_HEALTH_URL = "http://127.0.0.1:8000/healthz"
WORKER_MAX_HEARTBEAT_AGE = timedelta(minutes=3)

log = structlog.get_logger()


def _migrate(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        upgrade_to_head(engine, settings.data_dir / ".migrate.lock")
        log.info("migrated", revision=current_revision(engine))
    finally:
        engine.dispose()
    return 0


def _health_web() -> int:
    try:
        response = httpx.get(WEB_HEALTH_URL, timeout=3)
    except httpx.HTTPError as exc:
        print(f"web unhealthy: {exc.__class__.__name__}")
        return 1
    print(f"web {response.status_code}")
    return 0 if response.status_code == 200 else 1


def _health_worker(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        with engine.connect() as conn:
            beat_at = conn.execute(
                select(Heartbeat.beat_at).where(Heartbeat.component == "worker")
            ).scalar_one_or_none()
    except Exception as exc:
        print(f"worker unhealthy: {exc.__class__.__name__}")
        return 1
    finally:
        engine.dispose()
    if beat_at is None:
        print("worker unhealthy: no heartbeat yet")
        return 1
    age = SystemClock().now() - beat_at
    print(f"worker heartbeat age {int(age.total_seconds())}s")
    return 0 if age < WORKER_MAX_HEARTBEAT_AGE else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wp", description="WeightPicks command line")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the database to the latest schema")
    health = commands.add_parser("health", help="container health checks")
    target = health.add_mutually_exclusive_group(required=True)
    target.add_argument("--web", action="store_true", help="GET /healthz on the web container")
    target.add_argument("--worker", action="store_true", help="worker heartbeat is < 3 min old")
    args = parser.parse_args(argv)

    if args.command == "health" and args.web:
        return _health_web()
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.command == "migrate":
        return _migrate(settings)
    return _health_worker(settings)


if __name__ == "__main__":
    sys.exit(main())
