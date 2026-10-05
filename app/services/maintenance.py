"""Staged restore and factory reset with a worker handshake (BUILD_PLAN §1.5, §1.6).

Nothing is ever swapped under a live connection:

1. The admin stages an action: `pending/action.json` (plus a verified copy of the backup
   for a restore). The web app answers 503 from then on and restarts its container.
2. The worker notices on its next tick, does the reset's Garmin wipe if asked (only it
   mounts the Garmin volumes), writes `pending/worker-stopped` and exits.
3. On start, the web entrypoint runs `wp maintenance apply`: it waits for the worker's
   acknowledgement (or a stale heartbeat), archives the current database, puts the
   restored one in place (or leaves none, for a reset), clears `pending/`, and then
   `wp migrate` runs. The worker entrypoint runs `wp maintenance wait` until that's done.
"""

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Engine

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import immediate
from app.domain import backup as rules
from app.services import audit, backups, instance

log = structlog.get_logger()

ACTION = "action.json"
RESTORE = "restore.tar.gz"
ACK = "worker-stopped"
DONE = "maintenance-done.json"  # read once by the web app after a swap, for the audit log
WAIT_SECONDS = 300
STALE_HEARTBEAT = timedelta(minutes=3)


class MaintenanceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def pending_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "pending"


def pending_action(settings: Settings) -> dict[str, Any] | None:
    path = pending_dir(settings) / ACTION
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"kind": "unknown"}
    return data


def _write_action(settings: Settings, action: dict[str, Any]) -> None:
    folder = pending_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ACK).unlink(missing_ok=True)
    tmp = folder / (ACTION + ".tmp")
    tmp.write_text(json.dumps(action, indent=2))
    tmp.replace(folder / ACTION)  # the action appears atomically, after everything else


def stage_restore(
    engine: Engine, settings: Settings, clock: Clock, actor: audit.Actor | None, archive: Path
) -> rules.Manifest:
    """Verify a backup and stage it; it's applied at the next container start."""
    if pending_action(settings) is not None:
        raise MaintenanceError("busy", "Another restore or reset is already waiting.")
    checked = backups.verify(archive)
    if not checked.ok or checked.manifest is None:
        raise MaintenanceError(
            "bad_backup", "That backup failed checks: " + "; ".join(checked.problems)
        )
    folder = pending_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive, folder / RESTORE)
    if not backups.verify(folder / RESTORE).ok:  # the copy must check out too
        (folder / RESTORE).unlink(missing_ok=True)
        raise MaintenanceError("bad_backup", "The staged copy failed checks.")
    m = checked.manifest
    details = {"source": archive.name, "created_at": m.created_at, "revision": m.revision}
    audit.record_alone(engine, clock, actor, action="maintenance.restore_staged", after=details)
    _write_action(
        settings,
        {"kind": "restore", "staged_at": SystemClock().now().isoformat(), **details},
    )
    log.warning("restore_staged", source=archive.name)
    return m


def stage_reset(
    engine: Engine,
    settings: Settings,
    clock: Clock,
    actor: audit.Actor | None,
    *,
    confirm: str,
    wipe_garmin: bool,
) -> None:
    """Stage a factory reset. `confirm` must be the instance name, exactly."""
    if pending_action(settings) is not None:
        raise MaintenanceError("busy", "Another restore or reset is already waiting.")
    with engine.connect() as conn:
        config = instance.read(conn)
    name = config.app_name if config else "WeightPicks"
    if confirm.strip() != name:
        raise MaintenanceError("confirm", f'Type the instance name "{name}" exactly to confirm.')
    audit.record_alone(
        engine, clock, actor, action="maintenance.reset_staged", after={"wipe_garmin": wipe_garmin}
    )
    _write_action(
        settings,
        {"kind": "reset", "wipe_garmin": wipe_garmin, "staged_at": SystemClock().now().isoformat()},
    )
    log.warning("reset_staged", wipe_garmin=wipe_garmin)


def cancel(settings: Settings) -> bool:
    """Drop a staged action before it's applied (CLI only)."""
    folder = pending_dir(settings)
    if not (folder / ACTION).exists():
        return False
    shutil.rmtree(folder)
    return True


# ---- worker side -------------------------------------------------------------------------


def _wipe_garmin(settings: Settings) -> int:
    """Delete GarminDB data and tokens under GARMIN_HOME (children only: mount points stay)."""
    home = Path(settings.garmin_home).resolve()
    if not home.is_dir() or home == Path("/") or len(home.parts) < 2:
        return 0
    removed = 0
    for child in [*home.iterdir()]:
        targets = [*child.iterdir()] if child.is_dir() and child.is_mount() else [child]
        for target in targets:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            removed += 1
    return removed


def worker_should_stop(settings: Settings) -> bool:
    """Called by the worker every tick. True: acknowledge and stop now."""
    action = pending_action(settings)
    if action is None:
        return False
    if action.get("kind") == "reset" and action.get("wipe_garmin"):
        removed = _wipe_garmin(settings)
        log.warning("garmin_wiped", entries=removed)
    (pending_dir(settings) / ACK).write_text(SystemClock().now().isoformat())
    log.warning("worker_stopping_for_maintenance", kind=action.get("kind"))
    return True


def wait_until_clear(settings: Settings, poll: float = 2.0, timeout: float | None = None) -> bool:
    """Worker entrypoint: block while an action is pending. False on timeout."""
    started = time.monotonic()
    while pending_action(settings) is not None:
        if (pending_dir(settings) / ACK).exists() is False:
            (pending_dir(settings) / ACK).write_text(SystemClock().now().isoformat())
        if timeout is not None and time.monotonic() - started > timeout:
            return False
        time.sleep(poll)
    return True


# ---- web entrypoint: apply ---------------------------------------------------------------


def _worker_gone(settings: Settings, db: Path) -> bool:
    if (pending_dir(settings) / ACK).exists():
        return True
    if not db.exists():
        return True
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = conn.execute("SELECT beat_at FROM heartbeats WHERE component = 'worker'").fetchone()
        conn.close()
    except sqlite3.Error:
        return False
    if row is None:
        return True
    beat = datetime.fromisoformat(str(row[0]))
    beat = beat if beat.tzinfo else beat.replace(tzinfo=UTC)
    return SystemClock().now() - beat > STALE_HEARTBEAT


def _checkpoint(db: Path) -> None:
    """Fold the WAL into the database file so moving it loses nothing."""
    if db.exists():
        conn = sqlite3.connect(db, timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()


def _set_aside(db: Path, uploads: Path, archive_dir: Path, prefix: str) -> str:
    stamp = SystemClock().now().strftime("%Y%m%d-%H%M%S")
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{stamp}.db"
    if db.exists():
        _checkpoint(db)
        shutil.move(db, archive_dir / name)
    for extra in ("-wal", "-shm"):
        Path(str(db) + extra).unlink(missing_ok=True)
    if uploads.is_dir() and any(uploads.iterdir()):
        shutil.move(uploads, archive_dir / f"{prefix}-{stamp}-uploads")
    return name


@dataclass(frozen=True, slots=True)
class Applied:
    status: str  # none | restored | reset | timeout | failed
    detail: str = ""


def apply(settings: Settings, *, wait_seconds: float = WAIT_SECONDS, poll: float = 2.0) -> Applied:
    """Apply a staged action (web entrypoint, before migrations)."""
    action = pending_action(settings)
    if action is None:
        return Applied("none")
    db = Path(settings.db_url.removeprefix("sqlite:///"))
    deadline = time.monotonic() + wait_seconds
    while not _worker_gone(settings, db):
        if time.monotonic() > deadline:
            log.error("maintenance_waiting_for_worker", kind=action.get("kind"))
            return Applied("timeout", "the worker didn't stop; nothing was changed")
        time.sleep(poll)
    folder = pending_dir(settings)
    uploads = backups.uploads_dir(settings)
    archive_dir = backups.backups_dir(settings)
    kind = action.get("kind")
    if kind == "restore":
        staging = folder / "extracted"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            manifest = backups.extract(folder / RESTORE, staging)
        except rules.BadArchive as exc:
            log.error("restore_refused", reason=str(exc))
            shutil.rmtree(folder, ignore_errors=True)
            return Applied("failed", str(exc))
        kept = _set_aside(db, uploads, archive_dir, "pre-restore")
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staging / rules.DB, db)
        if (staging / "uploads").is_dir():
            shutil.move(staging / "uploads", uploads)
        note = {
            "kind": "restore",
            "source": action.get("source"),
            "kept": kept,
            "created_at": manifest.created_at,
            "revision": manifest.revision,
        }
        status = "restored"
    elif kind == "reset":
        kept = _set_aside(db, uploads, archive_dir, "pre-reset")
        note = {"kind": "reset", "kept": kept, "wipe_garmin": bool(action.get("wipe_garmin"))}
        status = "reset"
    else:
        shutil.rmtree(folder, ignore_errors=True)
        return Applied("failed", "unknown action; cleared")
    shutil.rmtree(folder, ignore_errors=True)
    (Path(settings.data_dir) / DONE).write_text(json.dumps(note))
    log.warning("maintenance_applied", **note)
    return Applied(status, kept)


def record_done(engine: Engine, settings: Settings, clock: Clock) -> dict[str, Any] | None:
    """After a swap, the web app writes the audit entry into the new database once."""
    path = Path(settings.data_dir) / DONE
    try:
        note: dict[str, Any] = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    with immediate(engine) as conn:
        audit.record(
            conn,
            clock,
            None,
            action=f"maintenance.{note.get('kind')}_applied",
            after=note,
        )
    path.unlink(missing_ok=True)
    return note
