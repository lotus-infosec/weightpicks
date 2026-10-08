"""Request-level security: client IP, form parsing, session cookies, CSRF and roles
.

CSRF: every non-GET/HEAD/OPTIONS request must carry a token in the `X-CSRF-Token`
header or a `csrf_token` form field. With a session it must match the session's
synchronizer token; only before a session exists (login, register, reset) does the
`wp_csrf` double-submit cookie count, so a cookie planted by a sibling subdomain can't
stand in for it. Browsers that say a request is `cross-site` or `same-site` (another
subdomain) are refused outright. The check is a global FastAPI dependency, so no route
can forget it.

Body size: `BodyLimit` (ASGI middleware) counts bytes as they stream in, before any
route, form parser or auth check reads them (security review S1, issue #18).
"""

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.security import new_token, same
from app.services import auth
from app.services.auth import SessionInfo

SESSION_COOKIE = "wp_session"
CSRF_COOKIE = "wp_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
MAX_BODY_BYTES = 16 * 1024  # every form and JSON request
# Multipart uploads go only to these admin routes, each with its own cap. A restore
# upload is spooled through the RAM-backed /tmp, so it stays well under the web
# container's memory limit; bigger backups are restored from the CLI (docs/backups.md).
UPLOAD_LIMITS = {
    "/admin/appearance/logo": 2 * 1024 * 1024,
    "/admin/system/restore": 256 * 1024 * 1024,
}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FOREIGN_SITES = frozenset({"cross-site", "same-site"})


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


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Refuse request bodies over the route's cap while they stream in: by Content-Length
    up front, and by counting chunks (chunked uploads have no length)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit = UPLOAD_LIMITS.get(path, MAX_BODY_BYTES)
        length = dict(scope["headers"]).get(b"content-length")
        if length is None and path in UPLOAD_LIMITS and scope["method"] not in SAFE_METHODS:
            await _plain(send, 411, "uploads need a Content-Length")
            return
        if length is not None and (not length.isdigit() or int(length) > limit):
            await _plain(send, 413, "request body too large")
            return
        received, started = 0, False

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except* _TooLarge:  # may arrive wrapped by the http middlewares' task groups
            if not started:
                await _plain(send, 413, "request body too large")


async def _plain(send: Send, status: int, text: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"connection", b"close")],
        }
    )
    await send({"type": "http.response.body", "body": text.encode()})


async def read_form(request: Request) -> dict[str, str]:
    body = await request.body()  # capped by BodyLimit
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
    if request.headers.get("sec-fetch-site") in FOREIGN_SITES:
        raise HTTPException(status_code=403, detail="cross-site request refused")
    session = current_session(request)
    submitted = request.headers.get(CSRF_HEADER)
    content_type = request.headers.get("content-type", "")
    if not submitted and content_type.startswith("application/x-www-form-urlencoded"):
        submitted = (await read_form(request)).get(CSRF_FIELD)
    elif not submitted and content_type.startswith("multipart/form-data"):
        if request.url.path not in UPLOAD_LIMITS:
            raise HTTPException(status_code=415, detail="uploads aren't accepted here")
        if session is None or session.role != "admin":
            raise HTTPException(status_code=403, detail="uploads need the admin")  # unparsed
        form = await request.form(max_files=1, max_fields=10)
        field = form.get(CSRF_FIELD)
        submitted = field if isinstance(field, str) else None
    if not submitted:
        raise HTTPException(status_code=403, detail="missing CSRF token")
    expected = session.csrf_token if session else request.cookies.get(CSRF_COOKIE, "")
    if not (expected and same(submitted, expected)):
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
