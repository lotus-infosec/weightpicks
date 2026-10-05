"""STAGE16 security review: one regression test per finding (GitHub issues #18-#24)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.clock import SystemClock
from app.domain import setup as steps
from app.domain.units import parse_tenths
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


# ---- S3 (#20): request logs carry route templates, never raw paths -------------------------


def test_request_log_uses_the_route_template(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "reset-secret-" + "q" * 30
    with web.client(create_app(world.settings, domain_clock=world.clock)) as c:
        capsys.readouterr()
        r = c.get(f"/reset/{secret}")
        c.get(f"/no/such/{secret}")
    out = capsys.readouterr().out
    assert r.headers["X-Request-ID"] in out
    assert "/reset/{token}" in out and "<unmatched>" in out
    assert secret not in out


def test_entrypoint_turns_off_the_raw_access_log() -> None:
    assert "--no-access-log" in Path("docker/entrypoint.sh").read_text()


# ---- S5 (#22): warn once when requests bypass the tunnel (R13) ------------------------------


def test_missing_tunnel_header_is_logged_once_in_production(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = world.settings.model_copy(update={"app_env": "production"})
    with web.client(create_app(prod, domain_clock=world.clock)) as c:
        c.get("/healthz")
        c.get("/login", headers={"CF-Connecting-IP": "203.0.113.5"})
        assert "cf_header_missing" not in capsys.readouterr().out
        c.get("/login")
        c.get("/login")
    assert capsys.readouterr().out.count("cf_header_missing") == 1
    with web.client(create_app(world.settings, domain_clock=world.clock)) as dev:
        dev.get("/login")
    assert "cf_header_missing" not in capsys.readouterr().out


# ---- S4 (#21): an account-wide login failure limit across IPs ------------------------------


def test_rotating_ips_does_not_buy_more_guesses(world: World, player: TestClient) -> None:
    email = "player1@example.invalid"
    clock = SystemClock()
    for n in range(auth.LOGIN_FAILS_PER_ACCOUNT):
        with pytest.raises(auth.AuthError):
            auth.login(
                world.engine,
                clock,
                email=email,
                password="wrong" * 3,
                ip=f"198.51.100.{n}",
                user_agent=None,
            )
    with pytest.raises(auth.AuthError) as refused:
        auth.login(
            world.engine,
            clock,
            email=email,
            password=web.PASSWORD,
            ip="192.0.2.77",
            user_agent=None,
        )
    assert refused.value.code == "rate_limited"


# ---- S6 (#23): integration inputs ------------------------------------------------------------


def test_workers_ai_and_webhook_inputs_are_strict() -> None:
    assert steps.workers_ai_problem("f" * 32, "fake" * 6) == {}
    for bad in ("acct/../x", "f" * 31, "f" * 32 + "?x=1", "g" * 32):
        assert "workers_ai.account_id" in steps.workers_ai_problem(bad, "")
    assert "workers_ai.token" in steps.workers_ai_problem("", "has space in it fakefake")
    good = "https://discord.com/api/webhooks/1/abc-DEF_1"
    assert steps.webhook_problem(good) is None
    assert steps.webhook_problem(good + "\n") is not None


# ---- S7 (#24): bounded money and weight parsing --------------------------------------------


def test_weight_parsing_is_plain_decimal_only() -> None:
    assert parse_tenths("212.45") == 2125 and parse_tenths("1,212.4") == 12124
    for bad in ("2e2", "1e300", "NaN", "-5", "", "12345", "inf"):
        assert parse_tenths(bad) is None, bad


def test_huge_admin_adjustment_is_a_form_error_not_a_500(world: World, admin: TestClient) -> None:
    r = admin.post(
        "/admin/bank/adjust",
        data={
            "csrf_token": web.page_csrf(admin, "/admin/bank"),
            "user_id": "2",
            "amount": "1e30",
            "reason": "x",
            "admin_password": web.PASSWORD,
        },
    )
    assert r.status_code == 400 and "Enter an amount" in r.text
