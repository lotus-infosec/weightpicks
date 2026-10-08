"""Backups: create, verify (and refuse tampered or hostile archives),
list, prune, extract, and the CLI."""

import io
import json
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from app.cli import main
from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.migrations import head_revision
from app.domain import backup as rules
from app.models import Account
from app.services import backups
from tests.integration.world import World, local


def make(w: World, **kw: object) -> Path:
    return backups.create(w.engine, w.settings, w.clock, **kw)  # type: ignore[arg-type]


def rebuild(src: Path, dest: Path, edit) -> Path:  # type: ignore[no-untyped-def]
    """Copy an archive member by member, letting `edit(name, data)` change or drop each
    (returning None drops it); `edit("extra", None)` may return extra members."""
    members: dict[str, bytes] = {}
    with tarfile.open(src, "r:gz") as tar:
        for m in tar.getmembers():
            handle = tar.extractfile(m)
            assert handle is not None
            members[m.name] = handle.read()
    with tarfile.open(dest, "w:gz") as out:
        for name, data in [*members.items(), *(edit("extra", None) or [])]:
            new = edit(name, data) if name != "extra" else data
            if new is None:
                continue
            if isinstance(new, tuple):
                name, new = new
            info = tarfile.TarInfo(name)
            info.size = len(new)
            out.addfile(info, io.BytesIO(new))
    return dest


def test_create_verify_and_contents(world: World) -> None:
    w = world
    uploads = Path(w.settings.data_dir) / "uploads"
    uploads.mkdir()
    (uploads / "logo.png").write_bytes(b"\x89PNG logo")
    (uploads / ".hidden").write_bytes(b"skip me")
    path = make(w, label="Pre 1.2.0!")
    assert path.name.endswith("-manual-pre-1-2-0.tar.gz") and path.parent.name == "backups"
    result = backups.verify(path)
    assert result.ok and result.problems == []
    m = result.manifest
    assert m is not None and m.revision == head_revision() and m.label == "pre-1-2-0"
    assert set(m.files) == {"app.db", "uploads/logo.png"}
    out = Path(w.settings.data_dir) / "restored"
    backups.extract(path, out)
    assert (out / "uploads" / "logo.png").read_bytes() == b"\x89PNG logo"
    copy = make_engine(f"sqlite:///{out / 'app.db'}")
    with copy.connect() as conn, w.engine.connect() as live:
        assert (
            conn.execute(select(Account.id)).scalars().all()
            == live.execute(select(Account.id)).scalars().all()
        )
    copy.dispose()
    assert not list(path.parent.glob(".tmp-*"))  # temp folders cleaned up


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda n, d: d + b"x" if n == "app.db" else d, "app.db does not match its checksum"),
        (lambda n, d: None if n == "app.db" else d, "app.db is missing"),
        (
            lambda n, d: [("uploads/evil.sh", b"rm -rf")] if n == "extra" else d,
            "not in the manifest",
        ),
        (lambda n, d: [("../escape", b"x")] if n == "extra" else d, "not allowed"),
        (lambda n, d: [("/etc/passwd", b"x")] if n == "extra" else d, "not allowed"),
        (lambda n, d: [("uploads/.env", b"x")] if n == "extra" else d, "not allowed"),
        (lambda n, d: None if n == "manifest.json" else d, "the manifest is missing"),
        (lambda n, d: b"{not json" if n == "manifest.json" else d, "unreadable"),
        (
            lambda n, d: (
                json.dumps(json.loads(d) | {"revision": "9999"}).encode()
                if n == "manifest.json"
                else d
            ),
            "newer backup",
        ),
        (
            lambda n, d: (
                json.dumps(json.loads(d) | {"format": 2}).encode() if n == "manifest.json" else d
            ),
            "unsupported backup format",
        ),
    ],
)
def test_tampered_archives_are_refused(world: World, edit, problem: str) -> None:  # type: ignore[no-untyped-def]
    good = make(world)
    bad = rebuild(good, good.parent / "wp-bad.tar.gz", edit)
    result = backups.verify(bad)
    assert not result.ok and any(problem in p for p in result.problems), result.problems
    with pytest.raises(rules.BadArchive):
        backups.extract(bad, Path(world.settings.data_dir) / "nope")


def test_links_oversize_and_garbage_are_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = make(world)
    linked = good.parent / "wp-link.tar.gz"
    with tarfile.open(linked, "w:gz") as tar:
        info = tarfile.TarInfo("app.db")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    assert "app.db is not a plain file" in backups.verify(linked).problems
    garbage = good.parent / "wp-garbage.tar.gz"
    garbage.write_bytes(b"definitely not gzip")
    assert "not a readable backup" in backups.verify(garbage).problems[0]
    monkeypatch.setattr(rules, "MAX_DB_BYTES", 10)
    assert "app.db is too large" in backups.verify(good).problems


def test_listing_resolve_and_prune(world: World) -> None:
    w = world
    for hours in range(9):
        w.clock.set(local(2026, 10, 5, 12) + timedelta(hours=hours))
        make(w, kind="auto")
    manual = make(w, label="keep")
    (manual.parent / "pre-restore-20261005.db").write_bytes(b"old")
    names = [b.name for b in backups.listing(w.settings)]
    assert len(names) == 11 and names[:2] == [
        "pre-restore-20261005.db",
        manual.name,
    ]  # newest first
    assert backups.resolve(w.settings, manual.name) == manual
    for bad in ("../app.db", ".tmp", "pre-restore-20261005.db", "nope.tar.gz"):
        assert backups.resolve(w.settings, bad) is None
    removed = backups.prune(w.settings, keep=7)
    assert len(removed) == 2
    kinds = [b.kind for b in backups.listing(w.settings)]
    assert kinds.count("auto") == 7 and "manual" in kinds and "pre-restore" in kinds


def test_cli(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert main(["backup", "create", "--label", "cli"]) == 0
    assert "created wp-" in capsys.readouterr().out
    assert main(["backup", "list"]) == 0
    assert "-manual-cli.tar.gz" in capsys.readouterr().out
    assert main(["backup", "verify", "--latest"]) == 0
    assert capsys.readouterr().out.startswith("OK wp-")
    assert main(["backup", "verify", "missing.tar.gz"]) == 1


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "console")
    return tmp_path


def test_auto_backup_job(world: World) -> None:
    from zoneinfo import ZoneInfo

    from app.worker.jobs.backup import AutoBackupJob
    from app.worker.registry import run_due

    w = world
    job = AutoBackupJob(w.settings, ZoneInfo("America/New_York"))
    real = SimClock(local(2026, 10, 5, 4))
    assert run_due([job], w.engine, real) == ["auto_backup"]
    assert run_due([job], w.engine, real) == []  # once per day
    real.set(local(2026, 10, 6, 3, 31))
    assert run_due([job], w.engine, real) == ["auto_backup"]
    assert [b.kind for b in backups.listing(w.settings)] == ["auto", "auto"]


def test_vacuum_snapshot_is_consistent_while_writing(world: World, settings: Settings) -> None:
    """VACUUM INTO reads a consistent snapshot even with a writer mid-transaction."""
    engine: Engine = world.engine
    path = make(world)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE scratch (x INTEGER)"))
        path2 = backups.create(engine, world.settings, world.clock, label="during")
        conn.execute(text("DROP TABLE scratch"))
    assert backups.verify(path).ok and backups.verify(path2).ok
