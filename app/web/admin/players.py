"""Dashboard, markets, players, the bank and registration codes."""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.domain import setup as setup_steps
from app.domain.economy import VIG_PRESETS, Economy
from app.domain.money import parse_cents
from app.services import (
    admin,
    admin_views,
    board,
    instance,
)
from app.services.admin import AdminError
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form


def register(router: APIRouter) -> None:
    # ---- dashboard ------------------------------------------------------------------------

    @router.get("")
    def dashboard(request: Request, session: Admin) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            data = admin_views.dashboard(conn, state.auth_clock.now())
        return render(request, "admin/dashboard.html", {"d": data, "active": "dashboard"})

    @router.post("/sync")
    async def sync_now(request: Request, session: Admin) -> Response:
        state = request.app.state
        await run_in_threadpool(
            admin.request_sync, state.engine, state.auth_clock, actor(request, session)
        )
        return back("/admin", "sync")

    # ---- markets ------------------------------------------------------------------------------

    @router.get("/markets")
    def markets(request: Request, session: Admin, status: str = "") -> Response:
        with request.app.state.engine.connect() as conn:
            rows = admin_views.markets(conn, status or None)
        statuses = ("", "open", "locked", "settled", "voided")
        return render(
            request,
            "admin/markets.html",
            {"rows": rows, "status": status, "statuses": statuses, "active": "markets"},
        )

    @router.get("/markets/{market_id}")
    def market(
        request: Request, session: Admin, market_id: int, error: str | None = None
    ) -> Response:
        with request.app.state.engine.connect() as conn:
            found = board.market(conn, market_id)
            bets = board.market_bets(conn, market_id) if found else []
        if found is None:
            return render(request, "error.html", {"message": "No such market."}, 404)
        card, outcome = found
        return render(
            request,
            "admin/market.html",
            {"card": card, "outcome": outcome, "bets": bets, "error": error, "active": "markets"},
        )

    @router.post("/markets/{market_id}/void")
    async def void(request: Request, session: Admin, market_id: int) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "market.void"):
            return market(request, session, market_id, REAUTH_FAILED_MESSAGE)
        try:
            await run_in_threadpool(
                admin.void_market,
                state.engine,
                state.domain_clock,
                actor(request, session),
                market_id,
                form.get("reason", ""),
            )
        except AdminError as exc:
            return market(request, session, market_id, exc.message)
        return back(f"/admin/markets/{market_id}", "voided")

    # ---- users --------------------------------------------------------------------------------

    @router.get("/users")
    def users(request: Request, session: Admin) -> Response:
        with request.app.state.engine.connect() as conn:
            rows = admin_views.players(conn)
        return render(request, "admin/users.html", {"rows": rows, "active": "users"})

    def user_page(
        request: Request,
        user_id: int,
        error: str | None = None,
        temporary: str | None = None,
        status: int = 200,
    ) -> Response:
        with request.app.state.engine.connect() as conn:
            row = next(iter(admin_views.players(conn, user_id)), None)
            if row is None:
                return render(request, "error.html", {"message": "No such player."}, 404)
            bets = board.my_bets(conn, user_id)
            history = admin_views.ledger_history(conn, user_id)
        return render(
            request,
            "admin/user.html",
            {
                "p": row,
                "bets": bets,
                "history": history,
                "error": error,
                "temporary": temporary,
                "active": "users",
            },
            status,
        )

    @router.get("/users/{user_id}")
    def user(request: Request, session: Admin, user_id: int) -> Response:
        return user_page(request, user_id)

    @router.post("/users/{user_id}/{action}")
    async def user_action(request: Request, session: Admin, user_id: int, action: str) -> Response:
        form = await read_form(request)
        state = request.app.state
        who = actor(request, session)
        try:
            if action in ("freeze", "unfreeze"):
                await run_in_threadpool(
                    admin.set_frozen,
                    state.engine,
                    state.auth_clock,
                    who,
                    user_id,
                    action == "freeze",
                )
                return back(
                    f"/admin/users/{user_id}", "frozen" if action == "freeze" else "unfrozen"
                )
            if action not in ("ban", "reset-password"):
                return user_page(request, user_id, "Unknown action.", status=404)
            if not await reauth(request, session, form, f"user.{action}"):
                return user_page(request, user_id, REAUTH_FAILED_MESSAGE, status=403)
            if action == "ban":
                await run_in_threadpool(
                    admin.ban,
                    state.engine,
                    state.domain_clock,
                    who,
                    user_id,
                    form.get("reason", ""),
                )
                return back(f"/admin/users/{user_id}", "banned")
            temporary = await run_in_threadpool(
                admin.reset_password, state.engine, state.auth_clock, who, user_id
            )
            return user_page(request, user_id, temporary=temporary)
        except AdminError as exc:
            return user_page(request, user_id, exc.message, status=400)

    # ---- bank ---------------------------------------------------------------------------------

    def bank_page(
        request: Request,
        error: str | None = None,
        economy_errors: dict[str, str] | None = None,
        economy_values: dict[str, Any] | None = None,
        status: int = 200,
    ) -> Response:
        with request.app.state.engine.connect() as conn:
            rows = admin_views.players(conn)
            busted = [(r, admin_views.bailout_ready_at(conn, r)) for r in rows if r.active_bust_at]
            economy = instance.economy(conn)
        values = economy_values or {
            "starting_bankroll": setup_steps.money_text(economy.starting_bankroll_cents),
            "daily_allowance": setup_steps.money_text(economy.daily_allowance_cents),
            "bailout": setup_steps.money_text(economy.bailout_cents),
            "bailout_cooldown_days": economy.bailout_cooldown_days,
            "vig": economy.vig_preset,
            "max_bet": setup_steps.money_text(economy.max_bet_cents),
            "max_parlay_legs": economy.max_parlay_legs,
            "high_roller": setup_steps.money_text(economy.high_roller_cents),
            "pool_buyin": setup_steps.money_text(economy.pool_buyin_cents),
        }
        return render(
            request,
            "admin/bank.html",
            {
                "players": [r for r in rows if r.status != "banned"],
                "busted": busted,
                "economy": economy,
                "values": values,
                "errors": economy_errors or {},
                "error": error,
                "vigs": list(VIG_PRESETS),
                "now": request.app.state.domain_clock.now(),
                "active": "bank",
            },
            status,
        )

    @router.get("/bank")
    def bank(request: Request, session: Admin) -> Response:
        return bank_page(request)

    @router.post("/bank/{action}")
    async def bank_action(request: Request, session: Admin, action: str) -> Response:
        form = await read_form(request)
        state = request.app.state
        who = actor(request, session)
        if action not in ("bailout", "adjust", "economy"):
            return bank_page(request, "Unknown action.", status=404)
        if action == "economy":
            values, errors = setup_steps.economy(form)
            if errors:
                return bank_page(
                    request, economy_errors=errors, economy_values=dict(form), status=400
                )
        if not await reauth(request, session, form, f"bank.{action}"):
            return bank_page(request, REAUTH_FAILED_MESSAGE, status=403)
        try:
            if action == "bailout":
                await run_in_threadpool(
                    admin.bailout,
                    state.engine,
                    state.domain_clock,
                    who,
                    int(form.get("user_id", "0")),
                )
                return back("/admin/bank", "bailout")
            if action == "adjust":
                amount = parse_cents(form.get("amount", ""))
                if amount is None:
                    return bank_page(request, "Enter an amount like 25 or -12.50.", status=400)
                await run_in_threadpool(
                    admin.adjust,
                    state.engine,
                    state.domain_clock,
                    who,
                    int(form.get("user_id", "0")),
                    amount,
                    form.get("reason", ""),
                )
                return back("/admin/bank", "adjusted")
            await run_in_threadpool(
                admin.update_economy, state.engine, state.auth_clock, who, Economy.from_json(values)
            )
            return back("/admin/bank", "economy")
        except (AdminError, ValueError) as exc:
            return bank_page(
                request, getattr(exc, "message", "Check the form and try again."), status=400
            )

    # ---- registration ---------------------------------------------------------------------------

    @router.get("/registration")
    def registration(request: Request, session: Admin) -> Response:
        return render(
            request,
            "admin/registration.html",
            {"code": None, "error": None, "active": "registration"},
        )

    @router.post("/registration")
    async def rotate(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "registration.rotate"):
            return render(
                request,
                "admin/registration.html",
                {"code": None, "error": REAUTH_FAILED_MESSAGE, "active": "registration"},
                403,
            )
        code = await run_in_threadpool(
            admin.rotate_registration_code, state.engine, state.auth_clock, actor(request, session)
        )
        return render(
            request,
            "admin/registration.html",
            {"code": code, "error": None, "active": "registration"},
        )
