"""Helpers for driving the web app like a browser: real forms, real cookies, CSRF."""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.core.clock import SystemClock
from app.core.db import immediate
from app.models import InstanceSettingsRow
from app.services import auth

PASSWORD = "correct horse battery"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
_META = re.compile(r'<meta name="csrf-token" content="([^"]*)"')


def client(app: FastAPI) -> TestClient:
    """HTTPS base URL so the Secure session cookie is sent back, like a real browser."""
    return TestClient(app, base_url="https://testserver")


def open_registration(engine: Engine) -> None:
    """Registration is closed by default (flag `registration_open`, BUILD_PLAN §4.2)."""
    with immediate(engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one_or_none()
        if flags is not None:
            conn.execute(
                update(InstanceSettingsRow).values(flags=dict(flags) | {"registration_open": True})
            )


def form_csrf(c: TestClient, path: str) -> str:
    if path == "/register":
        open_registration(c.app.state.engine)  # type: ignore[attr-defined]
    page = c.get(path)
    match = _CSRF.search(page.text)
    assert match, f"no csrf field on {path}"
    return match.group(1)


def page_csrf(c: TestClient, path: str = "/") -> str:
    match = _META.search(c.get(path).text)
    assert match and match.group(1), f"no csrf meta on {path}"
    return match.group(1)


def log_in(c: TestClient, email: str, password: str = PASSWORD) -> None:
    token = form_csrf(c, "/login")
    response = c.post(
        "/api/auth/login",
        data={"csrf_token": token, "email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def as_admin(c: TestClient, engine: Engine, email: str = "admin@example.invalid") -> None:
    auth.create_admin(engine, SystemClock(), email=email, display_name="Admin", password=PASSWORD)
    log_in(c, email)


def register(c: TestClient, code: str, n: int = 1, password: str = PASSWORD) -> None:
    token = form_csrf(c, "/register")
    response = c.post(
        "/api/auth/register",
        data={
            "csrf_token": token,
            "code": code,
            "email": f"player{n}@example.invalid",
            "display_name": f"Player {n}",
            "password": password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
