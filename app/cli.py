"""`wp` command line: migrations, health checks, ledger verification and dev seeding."""

import argparse
import sys
from datetime import timedelta

import httpx
import structlog
from sqlalchemy import select

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.logging import configure_logging
from app.core.migrations import current_revision, upgrade_to_head
from app.domain.economy import DEFAULT_ECONOMY
from app.domain.money import Money
from app.models import Heartbeat
from app.services import ledger
from app.services.users import ensure_player

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


def _ledger_verify(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        with engine.connect() as conn:  # one read transaction = one consistent snapshot
            report = ledger.verify(conn)
    finally:
        engine.dispose()
    if report.ok:
        print(f"ledger OK: {report.accounts_checked} accounts, {report.txns_checked} txns")
        return 0
    print("ledger MISMATCH")
    for m in report.mismatches:
        print(
            f"  account {m.account_id}: balance stored {m.stored_balance_cents} "
            f"computed {m.computed_balance_cents}; pnl stored {m.stored_pnl_cents} "
            f"computed {m.computed_pnl_cents}"
        )
    for txn_id in report.unbalanced_txn_ids:
        print(f"  txn {txn_id}: entries do not sum to zero")
    for account_id in report.negative_player_account_ids:
        print(f"  account {account_id}: player balance below zero")
    return 1


def _seed(settings: Settings, users: int) -> int:
    if not settings.is_dev:
        print("refusing to seed: APP_ENV must be 'dev'")
        return 2
    clock = SystemClock()
    grant = DEFAULT_ECONOMY.starting_bankroll_cents
    engine = make_engine(settings.db_url)
    try:
        with immediate(engine) as conn:
            season_id = ledger.active_season_id(conn) or ledger.open_season(conn, clock)
            for n in range(1, users + 1):
                user_id = ensure_player(
                    conn, clock, f"player{n:02d}@example.invalid", f"Player {n:02d}"
                )
                account_id = ledger.open_player_account(conn, clock, season_id, user_id)
                ledger.grant_starting(
                    conn,
                    clock,
                    account_id,
                    grant,
                    idempotency_key=f"seed:grant:season{season_id}:user{user_id}",
                )
    finally:
        engine.dispose()
    print(f"seeded {users} players in season {season_id}, {Money(grant)} each")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wp", description="WeightPicks command line")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the database to the latest schema")
    health = commands.add_parser("health", help="container health checks")
    target = health.add_mutually_exclusive_group(required=True)
    target.add_argument("--web", action="store_true", help="GET /healthz on the web container")
    target.add_argument("--worker", action="store_true", help="worker heartbeat is < 3 min old")
    ledger_cmd = commands.add_parser("ledger", help="ledger maintenance")
    ledger_sub = ledger_cmd.add_subparsers(dest="ledger_command", required=True)
    ledger_sub.add_parser("verify", help="recompute balances and P&L from entries")
    seed = commands.add_parser("seed", help="dev only: fake players with starting bankrolls")
    seed.add_argument("--users", type=int, default=10, help="number of players (default 10)")
    args = parser.parse_args(argv)

    if args.command == "health" and args.web:
        return _health_web()
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.command == "migrate":
        return _migrate(settings)
    if args.command == "ledger":
        return _ledger_verify(settings)
    if args.command == "seed":
        return _seed(settings, args.users)
    return _health_worker(settings)


if __name__ == "__main__":
    sys.exit(main())
