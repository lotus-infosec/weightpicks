"""Edge cases for staged maintenance, backups and password reset."""

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.core.clock import SimClock, SystemClock
from app.core.db import immediate
from app.models import Heartbeat
from app.services import backups, maintenance, password_reset
from tests.integration.world import World


def staged_dir(w: World) -> Path:
    return Path(w.settings.data_dir) / "pending"


def test_unknown_or_corrupt_action_is_cleared(world: World) -> None:
    w = world
    folder = staged_dir(w)
    folder.mkdir()
    (folder / maintenance.ACTION).write_text("{not json")
    assert maintenance.pending_action(w.settings) == {"kind": "unknown"}
    (folder / maintenance.ACK).write_text("x")
    w.engine.dispose()
    assert maintenance.apply(w.settings, wait_seconds=1, poll=0.01).status == "failed"
    assert not folder.exists()
    assert maintenance.cancel(w.settings) is False


def test_archive_tampered_after_staging_is_refused_at_apply(world: World) -> None:
    w = world
    path = backups.create(w.engine, w.settings, w.clock)
    maintenance.stage_restore(w.engine, w.settings, w.clock, None, path)
    (staged_dir(w) / maintenance.RESTORE).write_bytes(b"swapped")
    maintenance.worker_should_stop(w.settings)
    w.engine.dispose()
    result = maintenance.apply(w.settings, wait_seconds=1, poll=0.01)
    assert result.status == "failed"
    assert (Path(w.settings.data_dir) / "app.db").exists()  # nothing was swapped


def test_apply_proceeds_when_the_worker_heartbeat_is_stale(world: World) -> None:
    w = world
    with immediate(w.engine) as conn:
        conn.execute(
            insert(Heartbeat).values(
                component="worker", beat_at=datetime.now(UTC) - timedelta(minutes=10)
            )
        )
    maintenance.stage_reset(
        w.engine, w.settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
    )
    w.engine.dispose()
    assert maintenance.apply(w.settings, wait_seconds=1, poll=0.01).status == "reset"


def test_record_done_ignores_garbage(world: World) -> None:
    w = world
    (Path(w.settings.data_dir) / maintenance.DONE).write_text("{bad")
    assert maintenance.record_done(w.engine, w.settings, SystemClock()) is None


def test_backup_edge_cases(world: World) -> None:
    w = world
    good = backups.create(w.engine, w.settings, w.clock)
    twice = good.parent / "wp-twice.tar.gz"
    with tarfile.open(good, "r:gz") as src, tarfile.open(twice, "w:gz") as out:
        for m in src.getmembers():
            data = src.extractfile(m).read()  # type: ignore[union-attr]
            for _ in (1, 2) if m.name == "app.db" else (1,):
                info = tarfile.TarInfo(m.name)
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
    assert "app.db appears twice" in backups.verify(twice).problems
    (good.parent / "notes.txt").write_text("not a backup")
    (good.parent / ".hidden").write_text("x")
    assert {b.name for b in backups.listing(w.settings)} >= {good.name}
    assert "notes.txt" not in {b.name for b in backups.listing(w.settings)}
    with tarfile.open(good) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
    assert manifest["kind"] == "manual" and manifest["files"]["app.db"]["size"] > 0


def test_reset_disabled_without_smtp(world: World) -> None:
    w = world
    with pytest.raises(password_reset.ResetError, match="isn't available"):
        password_reset.request(
            w.engine, SimClock(SystemClock().now()), w.settings, address="a@b.c", ip="1.1.1.1"
        )
    assert password_reset.check(w.engine, SystemClock(), "nothing") is False
