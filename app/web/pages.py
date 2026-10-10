"""Player pages and the auth/bet API.

HTML forms post to `/api/auth/*` and get 303 redirects (errors re-render the form).
The bet slip posts JSON to `/api/bets`. CSRF is enforced globally (app.web.security).
"""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.domain.economy import Economy
from app.services import auth, board, instance, leaderboard, password_reset, pools, stats
from app.services.auth import AuthError, SessionInfo
from app.services.bets import BetRejected, place_bet, place_parlay
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

TABS = ("all", "daily", "weekly", "monthly")  # "all" first: the default (issue #45)
PROP_TABS = ("prop", "future")  # shown while the props_futures flag is on
EVENT_TABS = ("events",)  # shown while the special_events flag is on
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
    "parlays_off": "Parlays aren't open yet.",
    "leg_count": "A parlay needs between 2 legs and the maximum allowed.",
    "same_market": "A parlay can't use the same market twice.",
    "correlated": "Two of those legs depend on the same weigh-in or day; they can't be combined.",
    "over_cap": "That parlay would pay more than 100x the stake. Use fewer or safer legs.",
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
        return anon_form(
            request,
            "login.html",
            {
                "error": None,
                "email": "",
                "welcome": welcome,
                "reset_done": request.query_params.get("reset") == "1",
                "can_reset": reset_enabled(request),
            },
        )

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
                {
                    "error": exc.message,
                    "email": form.get("email", ""),
                    "can_reset": reset_enabled(request),
                },
                status,
            )
        response = RedirectResponse(_home_for(new.info), status_code=303)
        set_session_cookie(response, request, new.token, new.info.role)
        return response

    # ---- password reset by email (only while SMTP is set up) -----------------------------

    def reset_enabled(request: Request) -> bool:
        state = request.app.state
        with state.engine.connect() as conn:
            return password_reset.enabled(conn, state.settings)

    @router.get("/reset")
    def reset_request_page(request: Request) -> Response:
        if not reset_enabled(request):
            return PlainTextResponse("Not found", status_code=404)
        return anon_form(request, "reset_request.html", {"sent": False, "error": None})

    @router.post("/api/auth/reset")
    async def reset_request(request: Request) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not reset_enabled(request):
            return PlainTextResponse("Not found", status_code=404)
        try:
            await run_in_threadpool(
                lambda: password_reset.request(
                    state.engine,
                    state.auth_clock,
                    state.settings,
                    address=form.get("email", ""),
                    ip=client_ip(request),
                )
            )
        except password_reset.ResetError as exc:
            return anon_form(
                request, "reset_request.html", {"sent": False, "error": exc.message}, 429
            )
        return anon_form(request, "reset_request.html", {"sent": True, "error": None})

    @router.get("/reset/{token}")
    def reset_form(request: Request, token: str) -> Response:
        state = request.app.state
        if not reset_enabled(request):
            return PlainTextResponse("Not found", status_code=404)
        ok = password_reset.check(state.engine, state.auth_clock, token)
        return anon_form(
            request,
            "reset_password.html",
            {"token": token, "usable": ok, "error": None},
            200 if ok else 410,
        )

    @router.post("/api/auth/reset/complete")
    async def reset_complete(request: Request) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not reset_enabled(request):
            return PlainTextResponse("Not found", status_code=404)
        token = form.get("token", "")
        try:
            await run_in_threadpool(
                lambda: password_reset.complete(
                    state.engine,
                    state.auth_clock,
                    token=token,
                    password=form.get("password", ""),
                    confirm=form.get("confirm", ""),
                )
            )
        except password_reset.ResetError as exc:
            usable = exc.code != "expired"
            return anon_form(
                request,
                "reset_password.html",
                {"token": token, "usable": usable, "error": exc.message},
                400,
            )
        return RedirectResponse("/login?reset=1", status_code=303)

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
        if not registration_open(request):  # invites closed
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

    def board_context(request: Request, user_id: int, tab: str) -> dict[str, Any]:
        """The board page or one of its tabs: the tabs the flags allow, the cards, the wallet."""
        state = request.app.state
        config = state.live.config
        flags = config.flags if config else {}
        tabs = (
            TABS
            + (PROP_TABS if flags.get("props_futures") else ())
            + (EVENT_TABS if flags.get("special_events") else ())
        )
        tab = tab if tab in tabs else "all"
        now = state.domain_clock.now()
        timeframes = [t for t in tabs if t not in ("all", "events")]
        with state.engine.connect() as conn:
            groups = [
                (t, board.open_markets(conn, t, now))
                for t in (timeframes if tab == "all" else [tab] if tab != "events" else [])
            ]
            return {
                "tab": tab,
                "tabs": tabs,
                "groups": [(t, cards) for t, cards in groups if cards or tab != "all"],
                "pools": board.pools(conn, user_id, now) if tab == "events" else [],
                "wallet": board.wallet(conn, user_id),
                "parlays": bool(flags.get("parlays")),
                "max_legs": config.economy.max_parlay_legs if config else Economy().max_parlay_legs,
                "max_bet": (config.economy if config else Economy()).max_bet_cents,
            }

    @router.get("/")
    def board_page(request: Request, tab: str = "all") -> Response:
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        if session.role == "admin":
            return RedirectResponse("/admin", status_code=303)
        context = board_context(request, session.user_id, tab)
        if "HX-Request" in request.headers:
            return render(request, "_board_tab.html", context)
        return render(request, "board.html", context)

    @router.get("/markets/{market_id}")
    def market_page(request: Request, market_id: int, session: Player) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            found = board.market(conn, market_id)
            bets = board.market_bets(conn, market_id, session.user_id) if found else []
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
                "max_bet": (
                    state.live.config.economy if state.live.config else Economy()
                ).max_bet_cents,
                "open": card.status == "open" and card.lock_at > state.domain_clock.now(),
            },
        )

    @router.get("/bets/mine")
    def my_bets(request: Request, session: Player, season: int | None = None) -> Response:
        with request.app.state.engine.connect() as conn:
            options = leaderboard.seasons(conn)
            ids = {o.season_id for o in options}
            current = next((o.season_id for o in options if o.current), None)
            chosen = season if season in ids else current
            bets = board.my_bets(conn, session.user_id, chosen)
            wallet = board.wallet(conn, session.user_id)
        name = "_my_bets.html" if "HX-Request" in request.headers else "bets_mine.html"
        return render(
            request,
            name,
            {"bets": bets, "wallet": wallet, "season_options": options, "season_id": chosen},
        )

    @router.get("/bets/feed")
    def feed(request: Request, session: Player) -> Response:
        with request.app.state.engine.connect() as conn:
            bets = board.feed(conn, session.user_id)
            wallet = board.wallet(conn, session.user_id)
        name = "_feed.html" if "HX-Request" in request.headers else "feed.html"
        return render(request, name, {"bets": bets, "wallet": wallet})

    @router.get("/leaderboard")
    def leaderboard_page(request: Request, session: Player, season: int | None = None) -> Response:
        with request.app.state.engine.connect() as conn:
            options = leaderboard.seasons(conn)
            ids = {o.season_id for o in options}
            current = next((o.season_id for o in options if o.current), None)
            chosen = (
                season if season in ids else current or (options[0].season_id if options else None)
            )
            rows = leaderboard.standings(conn, chosen)
            wallet = board.wallet(conn, session.user_id)
        return render(
            request,
            "leaderboard.html",
            {"rows": rows, "season_options": options, "season_id": chosen, "wallet": wallet},
        )

    @router.get("/stats")
    def stats_page(request: Request, session: Player, days: int = 30) -> Response:
        state = request.app.state
        live = state.live
        metrics = live.config.enabled_metrics if live.config else ()
        as_of = state.domain_clock.now().astimezone(live.tz).date()
        with state.engine.connect() as conn:
            data = stats.build(conn, as_of, days, live.unit, metrics)
            wallet = board.wallet(conn, session.user_id)
        return render(request, "stats.html", {"data": data, "wallet": wallet})

    @router.post("/api/pools/{pool_id}/enter")
    async def enter_pool(request: Request, pool_id: int, session: Player) -> Response:
        """Join a pool or change your guess (HTMX form; re-renders the Events tab)."""
        form = await read_form(request)
        state = request.app.state
        message, ok, status = "", False, 200
        try:
            guess = pools.parse_guess(form.get("guess", ""))
            new = await run_in_threadpool(
                lambda: pools.enter(
                    state.engine,
                    state.domain_clock,
                    user_id=session.user_id,
                    pool_id=pool_id,
                    guess_x10=guess,
                )
            )
            message, ok = ("You're in. Good luck!" if new else "Guess updated."), True
        except pools.PoolError as exc:
            message, status = exc.message, 409
        context = board_context(request, session.user_id, "events") | {
            "pool_message": message,
            "pool_ok": ok,
            "pool_id": pool_id,
        }
        name = "_board_tab.html" if "HX-Request" in request.headers else "board.html"
        return render(request, name, context, status if name == "board.html" else 200)

    def _balance(state: Any, user_id: int) -> int | None:
        """The player's balance after a bet, so the header can update without a reload."""
        with state.engine.connect() as conn:
            wallet = board.wallet(conn, user_id)
        return wallet.balance_cents if wallet else None

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
                "balance_cents": await run_in_threadpool(_balance, state, session.user_id),
            }
        )

    @router.post("/api/bets/parlay")
    async def place_parlay_route(request: Request, session: Player) -> Response:
        try:
            body = await request.json()
            legs = [(int(leg["selection_id"]), int(leg["odds_version_id"])) for leg in body["legs"]]
            stake, key = body["stake_cents"], str(body["client_key"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "reason": "invalid", "message": "Bad request."}, 400)
        state = request.app.state
        try:
            placed = await run_in_threadpool(
                lambda: place_parlay(
                    state.engine,
                    state.domain_clock,
                    user_id=session.user_id,
                    legs=legs,
                    stake_cents=stake,
                    client_key=key,
                )
            )
        except BetRejected as exc:
            message = BET_MESSAGES.get(exc.reason, "That parlay couldn't be placed.")
            return JSONResponse({"ok": False, "reason": exc.reason, "message": message}, 409)
        return JSONResponse(
            {
                "ok": True,
                "bet_id": placed.bet_id,
                "american": placed.combined_american,
                "stake_cents": placed.stake_cents,
                "potential_payout_cents": placed.potential_payout_cents,
                "replayed": placed.replayed,
                "balance_cents": await run_in_threadpool(_balance, state, session.user_id),
            }
        )

    return router
