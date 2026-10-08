"""Admin → System (health, backups, restore, reset) and Admin → Appearance (name, palette,
logo upload re-encoded by Pillow) over HTTP (STAGE15, D-044)."""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import insert, select, update

from app.core.db import immediate
from app.models import AuditEntry, InstanceSettingsRow, SyncRun
from app.services import backups, maintenance, secrets
from app.web.main import create_app
from tests.integration import web
from tests.integration.world import World


@pytest.fixture
def admin(world: World) -> Iterator[tuple[TestClient, World, list[str]]]:
    world.settings = world.settings.model_copy(update={"app_secret_key": SecretStr("k" * 40)})
    app = create_app(world.settings, domain_clock=world.clock)
    restarts: list[str] = []
    app.state.restart = lambda: restarts.append("restart")
    c = web.client(app)
    c.__enter__()
    web.as_admin(c, world.engine)
    yield c, world, restarts
    c.__exit__(None, None, None)


def csrf(c: TestClient, page: str) -> str:
    return web.page_csrf(c, page)


def post(c: TestClient, page: str, action: str, data: dict[str, str] | None = None) -> Any:
    return c.post(
        action, data={"csrf_token": csrf(c, page), **(data or {})}, follow_redirects=False
    )


def upload(c: TestClient, action: str, payload: bytes, name: str, page: str, **fields: str) -> Any:
    return c.post(
        action,
        data={"csrf_token": csrf(c, page), "admin_password": web.PASSWORD, **fields},
        files={"file": (name, payload, "application/octet-stream")},
        follow_redirects=False,
    )


def image(fmt: str, size: tuple[int, int] = (300, 200), exif: bool = False) -> bytes:
    im = Image.new("RGBA" if fmt != "JPEG" else "RGB", size, (20, 140, 120))
    out = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if exif:
        e = Image.Exif()
        e[0x010E] = "secret GPS-ish description"  # ImageDescription
        kwargs["exif"] = e
    im.save(out, format=fmt, **kwargs)
    return out.getvalue()


def flags(w: World, **values: bool) -> None:
    with immediate(w.engine) as conn:
        current = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(current) | values))


def test_system_health_page(admin: tuple[TestClient, World, list[str]]) -> None:
    c, _w, _ = admin
    page = c.get("/admin/system").text
    assert 'data-testid="health"' in page and "Ledger check" in page
    assert 'data-testid="backups"' not in page  # backup_ui off: CLI only
    assert 'data-testid="reset-form"' in page


def test_system_page_while_a_sync_is_running(admin: tuple[TestClient, World, list[str]]) -> None:
    """Issue #31: a sync still running (or cut off by a crash) has no finished_at and
    must not be shown as the last failure."""
    c, w, _ = admin
    with immediate(w.engine) as conn:
        conn.execute(
            insert(SyncRun).values(
                provider="garmin", started_at=w.clock.now(), status="running", rows_new=0
            )
        )
    r = c.get("/admin/system")
    assert r.status_code == 200 and "last failure" not in r.text
    assert "sync running since" in r.text


def test_backup_download_and_restore_from_list(admin: tuple[TestClient, World, list[str]]) -> None:
    c, w, restarts = admin
    flags(w, backup_ui=True)
    assert (
        post(c, "/admin/system", "/admin/system/backup").headers["location"]
        == "/admin/system?ok=backup"
    )
    (made,) = backups.listing(w.settings)
    assert made.name in c.get("/admin/system").text
    wrong = post(
        c, "/admin/system", "/admin/system/download", {"name": made.name, "admin_password": "x"}
    )
    assert wrong.status_code == 403
    got = post(
        c,
        "/admin/system",
        "/admin/system/download",
        {"name": made.name, "admin_password": web.PASSWORD},
    )
    assert got.status_code == 200 and got.content[:2] == b"\x1f\x8b"  # gzip
    missing = post(
        c,
        "/admin/system",
        "/admin/system/download",
        {"name": "../app.db", "admin_password": web.PASSWORD},
    )
    assert missing.status_code == 404
    staged = c.post(
        "/admin/system/restore",
        data={
            "csrf_token": csrf(c, "/admin/system"),
            "admin_password": web.PASSWORD,
            "name": made.name,
        },
        files={"file": ("", b"", "application/octet-stream")},
        follow_redirects=False,
    )
    assert staged.status_code == 200 and 'data-testid="restarting"' in staged.text
    assert restarts == ["restart"]
    assert maintenance.pending_action(w.settings)["kind"] == "restore"  # type: ignore[index]
    with w.engine.connect() as conn:
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert {"backup.create", "backup.download", "maintenance.restore_staged"} <= set(actions)
    assert c.get("/admin/system").status_code == 503  # nothing touches the DB until restart


def test_restore_upload_refuses_junk(admin: tuple[TestClient, World, list[str]]) -> None:
    c, w, restarts = admin
    flags(w, backup_ui=True)
    r = upload(c, "/admin/system/restore", b"not a backup", "x.tar.gz", "/admin/system")
    assert r.status_code == 400 and "failed checks" in r.text and restarts == []
    assert maintenance.pending_action(w.settings) is None


def test_factory_reset_needs_name_and_password(admin: tuple[TestClient, World, list[str]]) -> None:
    c, w, restarts = admin
    bad = post(
        c,
        "/admin/system",
        "/admin/system/reset",
        {"confirm": "nope", "admin_password": web.PASSWORD},
    )
    assert bad.status_code == 400 and "exactly" in bad.text
    assert (
        post(
            c,
            "/admin/system",
            "/admin/system/reset",
            {"confirm": "WeightPicks", "admin_password": "x"},
        ).status_code
        == 403
    )
    ok = post(
        c,
        "/admin/system",
        "/admin/system/reset",
        {"confirm": "WeightPicks", "admin_password": web.PASSWORD, "wipe_garmin": "on"},
    )
    assert ok.status_code == 200 and "setup token" in ok.text and restarts == ["restart"]
    action = maintenance.pending_action(w.settings)
    assert action is not None and (action["kind"], action["wipe_garmin"]) == ("reset", True)


def test_unreadable_secrets_can_be_cleared(admin: tuple[TestClient, World, list[str]]) -> None:
    c, w, _ = admin
    with immediate(w.engine) as conn:
        secrets.put(conn, w.clock, "x" * 40, "workers_ai.token", "t")  # another instance's key
    page = c.get("/admin/system").text
    assert 'data-testid="key-wrong"' in page
    ok = post(c, "/admin/system", "/admin/system/secrets/clear", {"admin_password": web.PASSWORD})
    assert ok.headers["location"] == "/admin/system?ok=secrets_cleared"
    with w.engine.connect() as conn:
        assert secrets.names(conn) == []
    assert 'data-testid="key-wrong"' not in c.get("/admin/system").text


def test_appearance_name_palette(admin: tuple[TestClient, World, list[str]]) -> None:
    c, _w, _ = admin
    bad = post(c, "/admin/appearance", "/admin/appearance", {"app_name": "", "palette": "ember"})
    assert bad.status_code == 400
    ok = post(
        c, "/admin/appearance", "/admin/appearance", {"app_name": "Scale Wars", "palette": "lagoon"}
    )
    assert ok.headers["location"] == "/admin/appearance?ok=appearance"
    page = c.get("/admin/appearance").text
    assert 'class="palette-lagoon"' in page and "Scale Wars" in page


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_logo_upload_reencodes(admin: tuple[TestClient, World, list[str]], fmt: str) -> None:
    c, w, _ = admin
    payload = image(fmt, exif=True)
    r = upload(c, "/admin/appearance/logo", payload, f"logo.{fmt.lower()}", "/admin/appearance")
    assert r.headers["location"] == "/admin/appearance?ok=logo"
    uploads = Path(w.settings.data_dir) / "uploads"
    sizes = {p.name: Image.open(p).size for p in uploads.glob("*.png")}
    assert sizes == {
        "logo.png": (512, 512),
        "logo-192.png": (192, 192),
        "apple-touch-icon.png": (180, 180),
        "mark.png": (64, 64),
        "favicon.png": (32, 32),
    }
    stored = (uploads / "logo.png").read_bytes()
    assert b"secret GPS-ish" not in stored and stored != payload
    assert c.get("/brand/favicon.png").content == (uploads / "favicon.png").read_bytes()
    removed = post(
        c, "/admin/appearance", "/admin/appearance/logo/remove", {"admin_password": web.PASSWORD}
    )
    assert removed.headers["location"] == "/admin/appearance?ok=logo_removed"
    assert not list(uploads.glob("*.png"))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "PNG, JPEG or WebP",
        ),
        (b"GIF89a" + b"\x00" * 100, "PNG, JPEG or WebP"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 600_000, "512 KB"),
        (b"", "Choose an image"),
    ],
)
def test_logo_upload_refusals(
    admin: tuple[TestClient, World, list[str]], payload: bytes, message: str
) -> None:
    c, w, _ = admin
    r = upload(c, "/admin/appearance/logo", payload, "logo.png", "/admin/appearance")
    assert r.status_code == 400 and message in r.text
    assert not (Path(w.settings.data_dir) / "uploads" / "logo.png").exists()


def test_logo_bomb_and_polyglot(admin: tuple[TestClient, World, list[str]]) -> None:
    c, w, _ = admin
    bomb = image("PNG", size=(6000, 6000))  # 36 MP of one colour compresses tiny
    assert len(bomb) < 512 * 1024
    r = upload(c, "/admin/appearance/logo", bomb, "big.png", "/admin/appearance")
    assert r.status_code == 400 and "too large" in r.text
    polyglot = image("PNG") + b"<html><script>alert(1)</script></html>"
    ok = upload(c, "/admin/appearance/logo", polyglot, "poly.png", "/admin/appearance")
    assert ok.status_code == 303
    assert b"<script>" not in (Path(w.settings.data_dir) / "uploads" / "logo.png").read_bytes()
    wrong = c.post(
        "/admin/appearance/logo",
        data={"csrf_token": csrf(c, "/admin/appearance"), "admin_password": "nope"},
        files={"file": ("l.png", image("PNG"), "image/png")},
        follow_redirects=False,
    )
    assert wrong.status_code == 403


def test_multipart_only_where_expected(admin: tuple[TestClient, World, list[str]]) -> None:
    c, _w, _ = admin
    r = c.post(
        "/admin/notes",
        data={"csrf_token": csrf(c, "/admin/notes"), "text": "x"},
        files={"file": ("a.txt", b"x", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 415
