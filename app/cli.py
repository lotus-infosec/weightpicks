"""`wp` command line: migrations, health checks, ledger verification, bootstrap and dev tools."""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import structlog
from sqlalchemy import func, select

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.logging import configure_logging
from app.core.migrations import current_revision, upgrade_to_head
from app.domain.money import Money
from app.models import Heartbeat, Observation, SyncRun
from app.services import auth, instance, ledger, setup, sim
from app.services.observations import canonical_weigh_ins, latest_complete_through
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
    engine = make_engine(settings.db_url)
    try:
        with immediate(engine) as conn:
            season_id = ledger.active_season_id(conn) or ledger.open_season(conn, clock)
            grant = instance.economy(conn).starting_bankroll_cents
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


def _sim_status(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        state = sim.load_state(engine, settings)
        with engine.connect() as conn:
            counts = dict(
                conn.execute(
                    select(Observation.metric, func.count()).group_by(Observation.metric)
                ).all()
            )
            canonical = canonical_weigh_ins(conn, state.anchor_date, state.sim_now.date())
            complete = latest_complete_through(conn)
            syncs = dict(
                conn.execute(select(SyncRun.status, func.count()).group_by(SyncRun.status)).all()
            )
    finally:
        engine.dispose()
    local = state.sim_now.astimezone(settings.tz)
    manual = sum(c.source == "manual" for c in canonical)
    print(f"sim_now      {state.sim_now:%Y-%m-%d %H:%M} UTC ({local:%Y-%m-%d %H:%M %Z})")
    print(f"simulator    preset={state.preset} seed={state.seed} anchor={state.anchor_date}")
    print(f"syncs        {syncs}")
    print(f"observations {counts}")
    print(f"canonical    {len(canonical)} weigh-ins ({manual} manual)")
    print(f"complete     { ({k: str(v) for k, v in sorted(complete.items())}) }")
    return 0


def _sim(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.sim_clock:
        print("refusing: the simulator needs APP_ENV=dev and DATA_PROVIDER=simulated")
        return 2
    if args.sim_command == "status":
        return _sim_status(settings)
    engine = make_engine(settings.db_url)
    try:
        if args.sim_command == "advance":
            delta = timedelta(days=args.days, hours=args.hours)
            result = sim.advance(engine, settings, delta)
            print(
                f"advanced {result.start:%Y-%m-%d %H:%M} -> {result.end:%Y-%m-%d %H:%M} UTC: "
                f"{result.ticks} ticks, jobs {dict(result.jobs_run)} in {result.seconds:.2f}s"
            )
        elif args.sim_command == "set":
            sim.set_now(engine, settings, datetime.fromisoformat(args.to))
        elif args.sim_command == "reseed":
            sim.reseed(engine, settings, preset=args.preset, seed=args.seed)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    finally:
        engine.dispose()
    return _sim_status(settings)


def _calibrate(settings: Settings, args: argparse.Namespace) -> int:
    from app import calibration

    report = calibration.run(
        args.preset, days=args.days, seeds=args.seeds, tz=settings.tz, unit=settings.wp_unit
    )
    text = calibration.render_markdown(report)
    if args.out == "-":
        print(text)
    else:
        path = Path(args.out) / f"{args.preset}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path}: {report.status} ({report.samples:,} samples)")
    return {"PASS": 0, "FAIL": 1}.get(report.status, 2)


def _registration_code(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        code = auth.rotate_registration_code(engine, SystemClock())
    finally:
        engine.dispose()
    print(f"new registration code (the previous one no longer works): {code}")
    return 0


def _setup_token(settings: Settings) -> int:
    engine = make_engine(settings.db_url)
    try:
        token = setup.issue_token(engine, SystemClock())
    except setup.SetupError as exc:
        print(f"refused: {exc.message}")
        return 1
    finally:
        engine.dispose()
    print(f"SETUP TOKEN: {token}  (valid 24 hours; any previous token no longer works)")
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
    sim_cmd = commands.add_parser("sim", help="dev only: the simulated clock and data")
    sim_sub = sim_cmd.add_subparsers(dest="sim_command", required=True)
    sim_sub.add_parser("status", help="show the simulation clock and ingested data")
    adv = sim_sub.add_parser("advance", help="run every worker tick up to now + N")
    adv.add_argument("--days", type=int, default=0)
    adv.add_argument("--hours", type=int, default=0)
    set_cmd = sim_sub.add_parser("set", help="jump forward to an ISO time (no ticks run)")
    set_cmd.add_argument("--to", required=True, help="e.g. 2026-11-01T12:00:00+00:00")
    reseed_cmd = sim_sub.add_parser("reseed", help="change the simulator preset and seed")
    reseed_cmd.add_argument("--preset", required=True)
    reseed_cmd.add_argument("--seed", type=int, required=True)
    cal = commands.add_parser("calibrate", help="line-engine calibration report")
    cal.add_argument("--preset", required=True, help="simulator preset, e.g. steady-loser")
    cal.add_argument("--days", type=int, default=365)
    cal.add_argument("--seeds", type=int, default=20)
    cal.add_argument("--out", default="docs/calibration", help="directory, or - for stdout")
    code_cmd = commands.add_parser("registration-code", help="registration codes")
    code_sub = code_cmd.add_subparsers(dest="code_command", required=True)
    code_sub.add_parser("new", help="replace the registration code and print the new one")
    commands.add_parser("setup-token", help="print a fresh one-time /setup token")
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
    if args.command == "sim":
        return _sim(settings, args)
    if args.command == "calibrate":
        return _calibrate(settings, args)
    if args.command == "registration-code":
        return _registration_code(settings)
    if args.command == "setup-token":
        return _setup_token(settings)
    return _health_worker(settings)


if __name__ == "__main__":
    sys.exit(main())
