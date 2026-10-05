"""STAGE16 security review: one regression test per finding (GitHub issues #18-#24)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.clock import SystemClock
from app.services import auth
from app.web.main import create_app
from app.web.security import CSRF_COOKIE, MAX_BODY_BYTES, UPLOAD_LIMITS
from tests.integration import web
from tests.integration.world import World


@pytest.fixture
def player(world: World) -> Iterator[TestClient]:
    code = auth.rotate_registration_code(world.engine, SystemClock())
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    with c:
        web.register(c, code)
        yield c


@pytest.fixture
def admin(world: World) -> Iterator[TestClient]:
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    with c:
        web.as_admin(c, world.engine)
        yield c


def chunks(total: int, size: int = 4096) -> Iterator[bytes]:
    """A body without Content-Length (chunked), like a client that lies about nothing."""
    sent = 0
    while sent < total:
        yield b"x" * size
        sent += size


# ---- S1 (#18): body limits before anything reads the body -----------------------------------


def test_oversized_json_is_refused_while_streaming(player: TestClient) -> None:
    token = web.page_csrf(player)
    r = player.post(
        "/api/bets",
        content=chunks(MAX_BODY_BYTES * 4),
        headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    claimed = player.post(
        "/api/bets",
        content=b"{}",
        headers={"X-CSRF-Token": token, "Content-Length": str(MAX_BODY_BYTES + 1)},
    )
    assert claimed.status_code == 413


def test_oversized_form_is_refused_before_login(world: World) -> None:
    with web.client(create_app(world.settings, domain_clock=world.clock)) as c:
        r = c.post(
            "/api/auth/login",
            content=chunks(MAX_BODY_BYTES * 4),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert r.status_code == 413


def test_uploads_need_a_length_and_the_admin(world: World, player: TestClient) -> None:
    path = "/admin/system/restore"
    multipart = {"Content-Type": "multipart/form-data; boundary=x"}
    no_length = player.post(path, content=chunks(1024), headers=multipart)
    assert no_length.status_code == 411
    too_big = player.post(
        path, content=b"--x--", headers=multipart | {"Content-Length": str(UPLOAD_LIMITS[path] + 1)}
    )
    assert too_big.status_code == 413
    # A player (or anyone without the admin's session) never gets the multipart parsed,
    # even with a matching double-submit cookie.
    body = b'--x\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\nt\r\n--x--\r\n'
    player.cookies.set(CSRF_COOKIE, "t", domain="testserver.local")
    refused = player.post(path, content=body, headers=multipart)
    assert refused.status_code == 403 and "admin" in refused.text


def test_logo_upload_cap(admin: TestClient) -> None:
    path = "/admin/appearance/logo"
    r = admin.post(
        path,
        content=b"--x--",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(UPLOAD_LIMITS[path] + 1),
        },
    )
    assert r.status_code == 413


# ---- S2 (#19): CSRF with a session takes only the session's token ---------------------------


def test_planted_csrf_cookie_does_not_work_with_a_session(player: TestClient) -> None:
    player.cookies.set(CSRF_COOKIE, "planted-by-a-sibling", domain="testserver.local")
    r = player.post(
        "/api/auth/logout", data={"csrf_token": "planted-by-a-sibling"}, follow_redirects=False
    )
    assert r.status_code == 403
    assert player.get("/", follow_redirects=False).status_code == 200  # still logged in


def test_cross_site_and_sibling_site_posts_are_refused(player: TestClient) -> None:
    token = web.page_csrf(player)
    for site in ("cross-site", "same-site"):
        r = player.post(
            "/api/auth/logout",
            data={"csrf_token": token},
            headers={"Sec-Fetch-Site": site},
            follow_redirects=False,
        )
        assert r.status_code == 403, site
    ok = player.post(
        "/api/auth/logout",
        data={"csrf_token": token},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
