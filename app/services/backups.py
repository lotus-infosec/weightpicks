"""Backups (BUILD_PLAN §1.6): create, verify, list, prune, safely extract.

A consistent online snapshot of the database (`VACUUM INTO`) plus uploaded files and a
manifest, as `/data/backups/wp-<UTC time>-<kind>[-label].tar.gz`. Garmin data and
tokens, `.env` and older backups are never included. Archives are written to a temp
name and renamed, so a half-written backup is never listed.
"""

import hashlib
import io
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import structlog
from sqlalchemy import Engine

from app.core.clock import Clock
from app.core.config import Settings
from app.core.migrations import current_revision, known_revisions
from app.domain import backup as rules

log = structlog.get_logger()
CHUNK = 1024 * 1024
PREFIX = "wp-"
SUFFIX = ".tar.gz"


def app_version() -> str:
    try:
        return version("weightpicks")
    except PackageNotFoundError:  # pragma: no cover - always installed in practice
        return "unknown"


def backups_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "backups"


def uploads_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "uploads"


def _hash_file(path: Path) -> rules.FileEntry:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return rules.FileEntry(digest.hexdigest(), size)


def create(
    engine: Engine,
    settings: Settings,
    clock: Clock,
    *,
    label: str | None = None,
    kind: str = "manual",
) -> Path:
    """Write a new backup and return its path."""
    out_dir = backups_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = clock.now().astimezone(UTC)
    tag = rules.clean_label(label)
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}-{kind}{'-' + tag if tag else ''}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".tmp-") as tmp:
        snapshot = Path(tmp) / rules.DB
        # VACUUM can't run inside a transaction, and SQLAlchemy would open one: use the
        # raw driver connection (autocommit, see app.core.db). The snapshot is consistent.
        raw = engine.raw_connection()
        try:
            raw.driver_connection.execute("VACUUM INTO ?", (str(snapshot),))  # type: ignore[union-attr]
        finally:
            raw.close()
        files = {rules.DB: _hash_file(snapshot)}
        uploads = (
            sorted(p for p in uploads_dir(settings).glob("*") if p.is_file())
            if uploads_dir(settings).is_dir()
            else []
        )
        for path in uploads:
            member = rules.UPLOADS + path.name
            if rules.allowed_member(member):
                files[member] = _hash_file(path)
        manifest = rules.Manifest(
            app_version=app_version(),
            revision=current_revision(engine),
            created_at=now.isoformat(),
            label=tag,
            kind=kind,
            files=files,
        )
        partial = Path(tmp) / (name + ".part")
        with tarfile.open(partial, "w:gz") as tar:
            data = manifest.to_json().encode()
            info = tarfile.TarInfo(rules.MANIFEST)
            info.size, info.mtime = len(data), int(now.timestamp())
            tar.addfile(info, io.BytesIO(data))
            tar.add(snapshot, arcname=rules.DB, recursive=False)
            for path in uploads:
                member = rules.UPLOADS + path.name
                if member in files:
                    tar.add(path, arcname=member, recursive=False)
        final = out_dir / name
        os.replace(partial, final)
    log.info("backup_created", file=final.name, kind=kind, bytes=final.stat().st_size)
    return final


@dataclass(frozen=True, slots=True)
class Verified:
    ok: bool
    problems: list[str]
    manifest: rules.Manifest | None


def verify(path: Path) -> Verified:
    """Check an archive end to end without extracting it."""
    found: dict[str, rules.FileEntry] = {}
    manifest: rules.Manifest | None = None
    issues: list[str] = []
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > rules.MAX_MEMBERS:
                return Verified(False, ["too many files in the archive"], None)
            for m in members:
                if not m.isfile():
                    issues.append(f"{m.name} is not a plain file")
                    continue
                if not rules.allowed_member(m.name):
                    issues.append(f"{m.name} is not allowed in a backup")
                    continue
                if m.name in found or (m.name == rules.MANIFEST and manifest is not None):
                    issues.append(f"{m.name} appears twice")
                    continue
                if m.size > rules.size_cap(m.name):
                    issues.append(f"{m.name} is too large")
                    continue
                handle = tar.extractfile(m)
                if handle is None:
                    issues.append(f"{m.name} can't be read")
                    continue
                if m.name == rules.MANIFEST:
                    manifest = rules.Manifest.from_json(handle.read(rules.MAX_MANIFEST_BYTES))
                    continue
                digest, size = hashlib.sha256(), 0
                while chunk := handle.read(CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                found[m.name] = rules.FileEntry(digest.hexdigest(), size)
    except (tarfile.TarError, OSError, EOFError) as exc:
        return Verified(False, [f"not a readable backup ({type(exc).__name__})"], None)
    except rules.BadArchive as exc:
        return Verified(False, [str(exc)], None)
    if manifest is None:
        return Verified(False, [*issues, "the manifest is missing"], None)
    issues += rules.problems(manifest, found, known_revisions())
    return Verified(not issues, issues, manifest)


def extract(path: Path, dest: Path) -> rules.Manifest:
    """Verify, then write the database and uploads into `dest` (never via extractall)."""
    checked = verify(path)
    if not checked.ok or checked.manifest is None:
        raise rules.BadArchive("; ".join(checked.problems))
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            if m.name == rules.MANIFEST:
                continue
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(m)
            if handle is None:  # verify() already refused anything that isn't a file
                raise rules.BadArchive(f"{m.name} can't be read")
            with target.open("wb") as out:
                while chunk := handle.read(CHUNK):
                    out.write(chunk)
    return checked.manifest


@dataclass(frozen=True, slots=True)
class BackupFile:
    name: str
    size: int
    modified: datetime
    kind: str  # auto | manual | pre-restore | pre-reset


def listing(settings: Settings) -> list[BackupFile]:
    folder = backups_dir(settings)
    if not folder.is_dir():
        return []
    rows: list[BackupFile] = []
    for p in folder.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX):
            kind = "auto" if "-auto" in p.name else "manual"
        elif p.name.startswith(("pre-restore-", "pre-reset-")):
            kind = p.name.split("-")[0] + "-" + p.name.split("-")[1]
        else:
            continue
        stat = p.stat()
        rows.append(
            BackupFile(p.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC), kind)
        )
    return sorted(rows, key=lambda r: (r.modified, r.name), reverse=True)


def resolve(settings: Settings, name: str) -> Path | None:
    """A listed backup archive by file name (no paths), or None."""
    if "/" in name or name.startswith("."):
        return None
    path = backups_dir(settings) / name
    return path if path.is_file() and name.startswith(PREFIX) and name.endswith(SUFFIX) else None


def prune(settings: Settings, keep: int) -> list[str]:
    """Delete automatic backups beyond the newest `keep`. Manual ones are kept."""
    autos = [b for b in listing(settings) if b.kind == "auto"]
    removed = []
    for old in autos[keep:]:
        (backups_dir(settings) / old.name).unlink(missing_ok=True)
        removed.append(old.name)
    if removed:
        log.info("backups_pruned", removed=removed)
    return removed
