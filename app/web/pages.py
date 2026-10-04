"""Player pages and the auth/bet API (BUILD_PLAN §1.5 page map).

HTML forms post to `/api/auth/*` and get 303 redirects (errors re-render the form).
The bet slip posts JSON to `/api/bets`. CSRF is enforced globally (app.web.security).
"""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import auth, board, instance
from app.services.auth import AuthError, SessionInfo
from app.services.bets import BetRejected, place_bet
from app.web.security import (
    SESSION_COOKIE,
    Player,
    clear_session_cookie,
    client_ip,
    csrf_cookie_token,
    current_session,
    read_form,
    set_csrf_cookie,
    set_session_cookie,
)

TABS = ("daily", "weekly", "monthly")
BET_MESSAGES = {
    "locked": "This market just locked.",
    "stale_odds": "The odds changed. Refresh the board and try again.",
    "side_not_offered": "That side isn't offered.",
    "below_minimum": "The minimum bet is $1.",
    "above_maximum": "That's over the maximum bet.",
    "insufficient_funds": "You don't have enough balance for that stake.",
    "user_inactive": "Your account can't bet right now.",
    "instance_frozen": "Betting is paused.",
    "no_account": "You don't have an account in this season yet.",
    "unknown_selection": "That market isn't available.",
    "client_key_conflict": "That slip was already used. Refresh and try again.",
    "invalid_stake": "Enter a stake in dollars and cents.",
    "invalid_client_key": "Refresh the page and try again.",
}


def _home_for(session: SessionInfo) -> str:
    return "/admin" if session.role == "admin" else "/"


def build_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def templates(request: Request) -> Jinja2Templates:
        t: Jinja2Templates = request.app.state.templates
        return t

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        session = current_session(request)
        base = {"session": session, "csrf": session.csrf_token if session else ""}
        return templates(request).TemplateResponse(
            request, name, base | context, status_code=status
        )

    def anon_form(
        request: Request, name: str, context: dict[str, Any], status: int = 200
    ) -> Response:
        csrf, new = csrf_cookie_token(request)
        page = render(request, name, {"form_csrf": csrf} | context, status)
        if new:
            set_csrf_cookie(page, request, csrf)
        return page

    # ---- auth -------------------------------------------------------------------------

    @router.get("/login")
    def login_page(request: Request) -> Response:
        session = current_session(request)
        if session is not None:
            return RedirectResponse(_home_for(session), status_code=303)
        welcome = request.query_params.get("welcome") == "1"
        return anon_form(request, "login.html", {"error": None, "email": "", "welcome": welcome})

    @router.post("/api/auth/login")
    async def login(request: Request) -> Response:
        form = await read_form(request)
        state = request.app.state
        try:
            new = await run_in_threadpool(
                auth.login,
                state.engine,
                state.auth_clock,
                email=form.get("email", ""),
                password=form.get("password", ""),
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        except AuthError as exc:
            status = 429 if exc.code == "rate_limited" else 400
            return anon_form(
                request,
                "login.html",
                {"error": exc.message, "email": form.get("email", "")},
                status,
            )
        response = RedirectResponse(_home_for(new.info), status_code=303)
        set_session_cookie(response, request, new.token, new.info.role)
        return response

    def registration_open(request: Request) -> bool:
        with request.app.state.engine.connect() as conn:
            config = instance.read(conn)
        return bool(config and config.flags.get("registration_open"))

    @router.get("/register")
    def register_page(request: Request) -> Response:
        session = current_session(request)
        if session is not None:
            return RedirectResponse(_home_for(session), status_code=303)
        context: dict[str, Any] = {
            "error": None,
            "form": {},
            "closed": not registration_open(request),
        }
        return anon_form(request, "register.html", context)

    @router.post("/api/auth/register")
    async def register(request: Request) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not registration_open(request):  # BUILD_PLAN §4.2: invite closed
            return anon_form(
                request, "register.html", {"error": None, "form": {}, "closed": True}, 403
            )
        ip = client_ip(request)
        try:
            await run_in_threadpool(
                auth.register,
                state.engine,
                state.auth_clock,
                email=form.get("email", ""),
                display_name=form.get("display_name", ""),
                password=form.get("password", ""),
                code=form.get("code", ""),
                ip=ip,
            )
            new = await run_in_threadpool(
                auth.login,
                state.engine,
                state.auth_clock,
                email=form.get("email", ""),
                password=form.get("password", ""),
                ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
        except AuthError as exc:
            status = 429 if exc.code == "rate_limited" else 400
            keep = {k: form.get(k, "") for k in ("email", "display_name")}
            return anon_form(request, "register.html", {"error": exc.message, "form": keep}, status)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, request, new.token, new.info.role)
        return response

    @router.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        state = request.app.state
        await run_in_threadpool(auth.logout, state.engine, request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response, request)
        return response

    # ---- player pages -------------------------------------------------------------------

    @router.get("/")
    def board_page(request: Request, tab: str = "daily") -> Response:
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if session.role == "admin":
            return RedirectResponse("/admin", status_code=303)
        tab = tab if tab in TABS else "daily"
        state = request.app.state
        with state.engine.connect() as conn:
            cards = board.open_markets(conn, tab, state.domain_clock.now())
            wallet = board.wallet(conn, session.user_id)
        context = {"tab": tab, "tabs": TABS, "cards": cards, "wallet": wallet}
        if "HX-Request" in request.headers:
            return render(request, "_board_tab.html", context)
        return render(request, "board.html", context)

    @router.get("/markets/{market_id}")
    def market_page(request: Request, market_id: int, session: Player) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            found = board.market(conn, market_id)
            bets = board.market_bets(conn, market_id) if found else []
            wallet = board.wallet(conn, session.user_id)
        if found is None:
            return render(request, "error.html", {"message": "No such market."}, 404)
        card, outcome = found
        return render(
            request,
            "market.html",
            {
                "card": card,
                "outcome": outcome,
                "bets": bets,
                "wallet": wallet,
                "open": card.status == "open" and card.lock_at > state.domain_clock.now(),
            },
        )

    @router.get("/bets/mine")
    def my_bets(request: Request, session: Player) -> Response:
        with request.app.state.engine.connect() as conn:
            bets = board.my_bets(conn, session.user_id)
            wallet = board.wallet(conn, session.user_id)
        name = "_my_bets.html" if "HX-Request" in request.headers else "bets_mine.html"
        return render(request, name, {"bets": bets, "wallet": wallet})

    @router.get("/bets/feed")
    def feed(request: Request, session: Player) -> Response:
        with request.app.state.engine.connect() as conn:
            bets = board.feed(conn)
            wallet = board.wallet(conn, session.user_id)
        name = "_feed.html" if "HX-Request" in request.headers else "feed.html"
        return render(request, name, {"bets": bets, "wallet": wallet})

    @router.post("/api/bets")
    async def place(request: Request, session: Player) -> Response:
        try:
            body = await request.json()
            args = {
                "selection_id": int(body["selection_id"]),
                "odds_version_id": int(body["odds_version_id"]),
                "stake_cents": body["stake_cents"],
                "client_key": str(body["client_key"]),
            }
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "reason": "invalid", "message": "Bad request."}, 400)
        state = request.app.state
        try:
            placed = await run_in_threadpool(
                lambda: place_bet(state.engine, state.domain_clock, user_id=session.user_id, **args)
            )
        except BetRejected as exc:
            message = BET_MESSAGES.get(exc.reason, "That bet couldn't be placed.")
            return JSONResponse({"ok": False, "reason": exc.reason, "message": message}, 409)
        return JSONResponse(
            {
                "ok": True,
                "bet_id": placed.bet_id,
                "american": placed.american,
                "stake_cents": placed.stake_cents,
                "potential_payout_cents": placed.potential_payout_cents,
                "replayed": placed.replayed,
            }
        )

    return router
