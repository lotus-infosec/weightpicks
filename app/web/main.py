"""FastAPI app factory: security headers, CSRF, sessions, pages and health checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head
from app.services import instance
from app.services import setup as setup_service
from app.services.instance import InstanceConfig
from app.web import format as fmt
from app.web.security import Forbidden, LoginRequired, csrf_protect

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


UNGATED_PATHS = frozenset({"/healthz", "/robots.txt"})


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("web_started", app_env=settings.app_env)
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
        if path.startswith("/static/") or path in UNGATED_PATHS:
            return await call_next(request)
        done = await run_in_threadpool(refresh)
        if done is None:
            return await call_next(request)
        if _is_setup_path(path) and done:
            return PlainTextResponse("Not found", status_code=404)
        if not _is_setup_path(path) and not done:
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from app.web.pages import build_router
    from app.web.setup import build_router as build_setup_router

    app.include_router(build_router())
    app.include_router(build_setup_router())
    if settings.is_dev and settings.data_provider == "simulated":
        from app.web.dev import build_router as build_dev_router

        app.include_router(build_dev_router(settings, engine))
    return app
