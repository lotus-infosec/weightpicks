"""Backup archive rules. Pure: no I/O.

A backup is a gzip tar with exactly `manifest.json`, `app.db` and `uploads/<file>`
members. The manifest records the app version, the Alembic revision and a SHA-256 and
size per file. Verification refuses anything else: unknown or unsafe member names,
links, oversize members, hash or size mismatches, and a schema newer than this app.
"""

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

FORMAT = 1
MANIFEST = "manifest.json"
DB = "app.db"
UPLOADS = "uploads/"
MAX_DB_BYTES = 2 * 1024**3
MAX_UPLOAD_BYTES = 2 * 1024**2
MAX_MANIFEST_BYTES = 256 * 1024
MAX_MEMBERS = 64
_UPLOAD_NAME = re.compile(r"^uploads/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LABEL = re.compile(r"[^a-z0-9-]+")


class BadArchive(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileEntry:
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class Manifest:
    app_version: str
    revision: str | None
    created_at: str  # ISO 8601 UTC
    label: str | None = None
    kind: str = "manual"  # manual | auto
    files: Mapping[str, FileEntry] = field(default_factory=dict)
    format: int = FORMAT

    def to_json(self) -> str:
        data = {
            "format": self.format,
            "app_version": self.app_version,
            "revision": self.revision,
            "created_at": self.created_at,
            "label": self.label,
            "kind": self.kind,
            "files": {k: {"sha256": v.sha256, "size": v.size} for k, v in self.files.items()},
        }
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str | bytes) -> "Manifest":
        try:
            data: Any = json.loads(text)
            files = {
                str(name): FileEntry(str(entry["sha256"]), int(entry["size"]))
                for name, entry in dict(data["files"]).items()
            }
            manifest = cls(
                app_version=str(data["app_version"]),
                revision=None if data.get("revision") is None else str(data["revision"]),
                created_at=str(data["created_at"]),
                label=None if data.get("label") is None else str(data["label"]),
                kind=str(data.get("kind", "manual")),
                files=files,
                format=int(data["format"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise BadArchive("the manifest is unreadable") from exc
        if manifest.format != FORMAT:
            raise BadArchive(f"unsupported backup format {manifest.format}")
        return manifest


def clean_label(label: str | None) -> str | None:
    """'Pre 1.2.0!' -> 'pre-1-2-0'; None or empty -> None."""
    if not label:
        return None
    cleaned = _LABEL.sub("-", label.lower()).strip("-")[:32]
    return cleaned or None


def allowed_member(name: str) -> bool:
    return name in (MANIFEST, DB) or bool(_UPLOAD_NAME.match(name))


def size_cap(name: str) -> int:
    if name == DB:
        return MAX_DB_BYTES
    if name == MANIFEST:
        return MAX_MANIFEST_BYTES
    return MAX_UPLOAD_BYTES


def problems(
    manifest: Manifest,
    found: Mapping[str, FileEntry],
    known_revisions: Collection[str],
) -> list[str]:
    """Everything wrong with an archive's contents versus its manifest ([] = good).
    `found` maps each member (except the manifest) to its measured hash and size."""
    issues: list[str] = []
    if DB not in manifest.files:
        issues.append("the manifest lists no database")
    for name in sorted(set(manifest.files) | set(found)):
        expected, actual = manifest.files.get(name), found.get(name)
        if expected is None:
            issues.append(f"{name} is not in the manifest")
        elif actual is None:
            issues.append(f"{name} is missing")
        elif expected != actual:
            issues.append(f"{name} does not match its checksum")
    if manifest.revision is not None and manifest.revision not in known_revisions:
        issues.append(
            f"schema revision {manifest.revision} is unknown to this version (newer backup?)"
        )
    return issues
