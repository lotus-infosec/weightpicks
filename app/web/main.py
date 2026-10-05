"""FastAPI app factory: security headers, CSRF, sessions, pages and health checks."""

import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head
from app.services import instance, maintenance
from app.services import setup as setup_service
from app.services.instance import InstanceConfig
from app.web import format as fmt
from app.web.security import BodyLimit, Forbidden, LoginRequired, csrf_protect

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
LOCAL_CSS = STATIC_DIR / "build" / "app.css"  # scripts/build-css.sh (git-ignored)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CSP,
}
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

log = structlog.get_logger()


class LazyAppClock:
    """The domain clock (persisted SimClock in dev), resolved on first use so the app
    can start before migrations finish."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self._settings, self._engine = settings, engine
        self._clock: Clock | None = None

    def now(self) -> datetime:
        if self._clock is None:
            from app.services.sim import app_clock

            self._clock = app_clock(self._settings, self._engine)
        return self._clock.now()


class LiveInstance:
    """The settings row as templates see it, refreshed once per request (D-036)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config: InstanceConfig | None = None

    @property
    def tz(self) -> ZoneInfo:
        return self.config.tz if self.config else self.settings.tz

    @property
    def unit(self) -> str:
        return self.config.unit if self.config else self.settings.wp_unit

    @property
    def app_name(self) -> str:
        return self.config.app_name if self.config else "WeightPicks"

    @property
    def palette(self) -> str:
        return self.config.palette if self.config else "ember"

    @property
    def brand_version(self) -> str:
        """Changes when an Appearance logo is uploaded or removed (cache-busting)."""
        logo = self.settings.data_dir / "uploads" / "logo.png"
        try:
            return str(int(logo.stat().st_mtime))
        except OSError:
            return "default"

    @property
    def frozen(self) -> bool:
        return bool(self.config and self.config.state == "frozen")


def build_templates(live: LiveInstance) -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    env = templates.env
    env.filters["money"] = fmt.money
    env.filters["signed_money"] = fmt.signed_money
    env.filters["odds"] = fmt.odds
    env.filters["local_time"] = lambda ts: fmt.local_time(ts, live.tz)
    env.filters["line"] = lambda x10, metric: fmt.line(x10, metric, live.unit)
    env.globals["instance"] = live
    env.globals["csrf"] = ""  # pages without a session (errors) still render
    env.globals["session"] = None
    return templates


def _is_setup_path(path: str) -> bool:
    return path == "/setup" or path.startswith("/setup/")


MAINTENANCE_PAGE = (
    "<!doctype html><html lang=en><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width, initial-scale=1'>"
    "<meta name=robots content='noindex, nofollow'><title>Back soon</title>"
    "<body style='font-family:system-ui;background:#0e1116;color:#e6e6e6;padding:2rem'>"
    "<h1>Back in a minute</h1><p>The app is restoring a backup or resetting. "
    "This page will work again shortly.</p></body></html>"
)
UNGATED_PATHS = frozenset({"/healthz", "/robots.txt", "/manifest.webmanifest"})
# /brand/<name> -> the default file in static/brand (an Appearance upload overrides it).
BRAND_FILES = {
    "logo.png": "logo-512.png",
    "logo-192.png": "logo-192.png",
    "favicon.png": "favicon-32.png",  # names match services/appearance.BRAND_SIZES
    "apple-touch-icon.png": "apple-touch-icon.png",
    "mark.png": "mark-64.png",
}
FROZEN_BLOCKED = ("/api/bets", "/api/pools")


def restart_container() -> None:
    """Stop this container so the restart policy brings it back and the entrypoint applies
    the staged restore/reset. PID 1 is the server (the entrypoint `exec`s it). Outside a
    container this only logs: restart the app yourself."""
    import os
    import signal
    import time

    if Path("/.dockerenv").exists():
        time.sleep(1)  # let the response reach the browser first
        log.warning("restarting_for_maintenance")
        os.kill(1, signal.SIGTERM)
    else:
        log.warning("restart_required", hint="restart the app to apply the staged action")


def log_request(request: Request, status: int, started: float, request_id: str) -> None:
    """One JSON line per request (uvicorn's access log is off). The route *template* is
    logged, never the raw path, so tokens in URLs (/reset/{token}) stay out of the logs."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or (
        "/static" if request.url.path.startswith("/static/") else "<unmatched>"
    )
    level = log.debug if path in ("/healthz", "/static") else log.info
    level(
        "request",
        method=request.method,
        route=path,
        status=status,
        ms=round((time.perf_counter() - started) * 1000, 1),
        request_id=request_id,
    )


def announce_setup_token(engine: Engine, clock: Clock) -> None:
    """On a fresh instance, issue the one-time setup token and print it to the logs
    (the plan's channel: `docker compose logs web | grep SETUP`). Hash-only in the DB."""
    try:
        if not setup_service.token_needed(engine, clock):
            return
        token = setup_service.issue_token(engine, clock)
    except OperationalError:
        return  # schema not migrated yet (tests); the entrypoint migrates before serving
    log.warning("setup_required", banner=f"SETUP TOKEN: {token}", expires_in_hours=24)


def create_app(
    settings: Settings | None = None,
    *,
    auth_clock: Clock | None = None,
    domain_clock: Clock | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.db_url)
    live = LiveInstance(settings)
    templates = build_templates(live)
    templates.env.globals["dev_tools"] = settings.sim_clock

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("web_started", app_env=settings.app_env)
        # After a restore/reset, log it in the new database (no schema yet in some tests).
        with contextlib.suppress(OperationalError):
            maintenance.record_done(engine, settings, app.state.auth_clock)
        announce_setup_token(engine, app.state.auth_clock)
        yield
        engine.dispose()

    app = FastAPI(
        title="WeightPicks",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.templates = templates
    # Sessions and rate limits run on real time; markets and bets on the app clock.
    app.state.auth_clock = auth_clock or SystemClock()
    app.state.domain_clock = domain_clock or LazyAppClock(settings, engine)
    app.state.live = live
    app.state.setup_done = False  # cached once true; only a factory reset undoes it
    app.state.restart = restart_container  # after staging a restore/reset (tests replace it)
    app.state.tunnel_warned = False

    def refresh() -> bool | None:
        """Load the settings row; None if the schema isn't ready yet."""
        try:
            with engine.connect() as conn:
                live.config = instance.read(conn)
                if not app.state.setup_done:
                    app.state.setup_done = setup_service.is_complete(conn)
        except OperationalError:
            return None
        return bool(app.state.setup_done)

    # Registered before the header middleware so the headers wrap its redirects and 404s.
    @app.middleware("http")
    async def setup_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith(("/static/", "/brand/")) or path in UNGATED_PATHS:
            return await call_next(request)
        if maintenance.pending_action(settings) is not None:
            # A restore or reset is staged: nothing touches the database until it's applied
            # at the next start (BUILD_PLAN §1.5).
            return HTMLResponse(MAINTENANCE_PAGE, status_code=503, headers={"Retry-After": "30"})
        done = await run_in_threadpool(refresh)
        if done is None:
            return await call_next(request)
        if _is_setup_path(path) and done:
            return PlainTextResponse("Not found", status_code=404)
        if not _is_setup_path(path) and not done:
            return RedirectResponse("/setup", status_code=303)
        if live.frozen and request.method != "GET" and path.startswith(FROZEN_BLOCKED):
            # Freeze middleware (D-043): money-moving player routes stop here; the
            # services refuse too, as the backstop. History stays viewable.
            return JSONResponse(
                {"ok": False, "reason": "instance_frozen", "message": "Betting is paused."},
                status_code=423,
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            warn_without_tunnel(request)
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers.update(SECURITY_HEADERS)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith(("/static/", "/brand/")):
            response.headers["Cache-Control"] = "no-store"
        log_request(request, response.status_code, started, request_id)
        return response

    def warn_without_tunnel(request: Request) -> None:
        """R13: behind cloudflared every request carries CF-Connecting-IP. Without it, client
        IPs (and so the rate limits) are the proxy's, and the header could be forged by
        whoever can reach the port. Logged once per process; the health check is exempt."""
        if (
            settings.is_dev
            or app.state.tunnel_warned
            or request.url.path == "/healthz"
            or "CF-Connecting-IP" in request.headers
        ):
            return
        app.state.tunnel_warned = True
        log.warning(
            "cf_header_missing",
            hint="serve the app only through the Cloudflare tunnel (WP_BIND=127.0.0.1:8000)",
        )

    app.add_middleware(BodyLimit)  # outermost: caps bodies before anything reads them

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc: LoginRequired) -> Response:
        if request.method == "GET" and "HX-Request" not in request.headers:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            {"ok": False, "reason": "login_required"},
            status_code=401,
            headers={"HX-Redirect": "/login"},
        )

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, _exc: Forbidden) -> Response:
        if request.method == "GET" and "HX-Request" not in request.headers:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": "You don't have access to this page."},
                status_code=403,
            )
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        try:
            schema_ok = is_at_head(engine)
            templates.get_template("login.html")
        except Exception:
            log.exception("healthz_failed")
            return JSONResponse({"status": "error"}, status_code=503)
        if not schema_ok:
            return JSONResponse({"status": "migrating"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    @app.get("/static/app.css", include_in_schema=False)
    def stylesheet() -> Response:
        for candidate in (settings.wp_static_build_dir / "app.css", LOCAL_CSS):
            if candidate.is_file():
                return FileResponse(candidate, media_type="text/css")
        return Response(
            "/* stylesheet not built: run scripts/build-css.sh */\n", media_type="text/css"
        )

    @app.get("/brand/{name}", include_in_schema=False)
    def brand(name: str) -> Response:
        """The instance's uploaded logo and icons (Appearance), else the project defaults."""
        default = BRAND_FILES.get(name)
        if default is None:
            return PlainTextResponse("Not found", status_code=404)
        uploaded = settings.data_dir / "uploads" / name
        path = uploaded if uploaded.is_file() else STATIC_DIR / "brand" / default
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": live.app_name,
                "short_name": live.app_name[:12],
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0e1116",
                "theme_color": "#0e1116",
                "icons": [
                    {"src": "/brand/logo-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/brand/logo.png", "sizes": "512x512", "type": "image/png"},
                ],
            },
            media_type="application/manifest+json",
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from app.web.admin import build_router as build_admin_router
    from app.web.pages import build_router
    from app.web.setup import build_router as build_setup_router

    app.include_router(build_router())
    app.include_router(build_admin_router())
    app.include_router(build_setup_router())
    if settings.sim_clock:
        from app.web.dev import build_router as build_dev_router

        app.include_router(build_dev_router(settings, engine))
    return app
