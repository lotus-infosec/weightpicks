"""Request-level security: client IP, form parsing, session cookies, CSRF and roles
(BUILD_PLAN §1.6, D-034).

CSRF: every non-GET/HEAD/OPTIONS request must carry a token in the `X-CSRF-Token`
header or a `csrf_token` form field. It must match either the session's synchronizer
token or the `wp_csrf` double-submit cookie (login and register, before a session
exists). The check is a global FastAPI dependency, so no route can forget it.
"""

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, Response

from app.core.security import new_token, same
from app.services import auth
from app.services.auth import SessionInfo

SESSION_COOKIE = "wp_session"
CSRF_COOKIE = "wp_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
MAX_FORM_BYTES = 16 * 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LoginRequired(Exception):
    """No valid session: HTML gets a redirect to /login, HTMX/JSON a 401."""


class Forbidden(Exception):
    """Signed in, wrong role."""


def client_ip(request: Request) -> str:
    """Cloudflare's header when present. Safe: the only ingress is the loopback tunnel."""
    forwarded = request.headers.get("CF-Connecting-IP", "").strip()
    if forwarded:
        return forwarded[:64]
    return request.client.host if request.client else "unknown"


async def read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="form too large")
    return {k: v[-1] for k, v in parse_qs(body.decode(errors="replace")).items()}


def wants_html(request: Request) -> bool:
    return request.method == "GET" and "HX-Request" not in request.headers


def set_session_cookie(response: Response, request: Request, token: str, role: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.ttl(role).total_seconds()),
        httponly=True,
        secure=request.app.state.settings.wp_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=request.app.state.settings.wp_cookie_secure,
        samesite="lax",
    )


def csrf_cookie_token(request: Request) -> tuple[str, bool]:
    """The double-submit token for forms shown before login, and whether it is new."""
    token = request.cookies.get(CSRF_COOKIE)
    if token and len(token) <= 100:
        return token, False
    return new_token(), True


def set_csrf_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        secure=request.app.state.settings.wp_cookie_secure,
        samesite="lax",
        path="/",
    )


def current_session(request: Request) -> SessionInfo | None:
    if not hasattr(request.state, "session"):
        state = request.app.state
        request.state.session = auth.resolve(
            state.engine, state.auth_clock, request.cookies.get(SESSION_COOKIE)
        )
    session: SessionInfo | None = request.state.session
    return session


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    submitted = request.headers.get(CSRF_HEADER)
    if not submitted and request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        submitted = (await read_form(request)).get(CSRF_FIELD)
    if not submitted:
        raise HTTPException(status_code=403, detail="missing CSRF token")
    expected = [request.cookies.get(CSRF_COOKIE) or ""]
    session = current_session(request)
    if session is not None:
        expected.append(session.csrf_token)
    if not any(token and same(submitted, token) for token in expected):
        raise HTTPException(status_code=403, detail="bad CSRF token")


def require_session(request: Request) -> SessionInfo:
    session = current_session(request)
    if session is None:
        raise LoginRequired
    return session


def require_player(session: Annotated[SessionInfo, Depends(require_session)]) -> SessionInfo:
    if session.role != "player":
        raise Forbidden
    return session


def require_admin(session: Annotated[SessionInfo, Depends(require_session)]) -> SessionInfo:
    if session.role != "admin":
        raise Forbidden
    return session


Player = Annotated[SessionInfo, Depends(require_player)]
Admin = Annotated[SessionInfo, Depends(require_admin)]
