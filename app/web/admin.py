"""Admin panel I (BUILD_PLAN §1.5, CONCEPT §8, D-037). Every route requires the admin;
destructive and money actions re-prompt for the admin's password."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core.security import verify_password
from app.domain import setup as setup_steps
from app.domain.economy import VIG_PRESETS, Economy
from app.models import Market, User
from app.services import admin, admin_views, board, instance
from app.services import props as props_service
from app.services import secrets as secret_store
from app.services import stats as stats_service
from app.services.admin import Actor, AdminError
from app.services.auth import SessionInfo
from app.services.observations import canonical_weigh_ins
from app.web.security import Admin, client_ip, read_form, require_admin
from app.web.setup import WEBHOOK_LABELS

MESSAGES = {
    "voided": "Market voided and every open stake refunded.",
    "frozen": "Player frozen: they can log in but not bet.",
    "unfrozen": "Player unfrozen.",
    "banned": "Player removed. Their open bets were refunded and their email is blocked.",
    "bailout": "Bailout paid.",
    "adjusted": "Balance adjusted.",
    "economy": "Economy settings saved.",
    "sync": "Sync requested. The worker picks it up within a minute.",
    "webhook": "Webhook saved.",
    "test": "Test post queued. It should appear in Discord within a minute.",
    "flags": "Settings saved.",
    "prop": "Prop posted. Players can bet on it until tonight's lock.",
}
REAUTH_FAILED_MESSAGE = "That's not your password. Nothing was changed."


def _cents(raw: str) -> int | None:
    """'-12.50' -> -1250 (signed, whole cents)."""
    try:
        value = Decimal(raw.replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        return None
    return int(value * 100)


def build_router() -> APIRouter:
    router = APIRouter(
        prefix="/admin", include_in_schema=False, dependencies=[Depends(require_admin)]
    )

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        session: SessionInfo = request.state.session
        base = {
            "session": session,
            "csrf": session.csrf_token,
            "admin_nav": True,
            "message": MESSAGES.get(request.query_params.get("ok", "")),
        }
        page: Response = request.app.state.templates.TemplateResponse(
            request, name, base | context, status_code=status
        )
        return page

    def actor(request: Request, session: SessionInfo) -> Actor:
        return Actor(session.user_id, client_ip(request), request.app.state.auth_clock.now())

    async def reauth(
        request: Request, session: SessionInfo, form: dict[str, str], action: str
    ) -> bool:
        state = request.app.state
        with state.engine.connect() as conn:
            stored = conn.execute(
                select(User.password_hash).where(User.id == session.user_id)
            ).scalar_one()
        ok = await run_in_threadpool(verify_password, stored, form.get("admin_password", ""))
        if not ok:
            await run_in_threadpool(
                admin.reauth_failed, state.engine, state.auth_clock, actor(request, session), action
            )
        return bool(ok)

    def back(path: str, ok: str) -> Response:
        return RedirectResponse(f"{path}?ok={ok}", status_code=303)

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
            row = next((r for r in admin_views.players(conn) if r.user_id == user_id), None)
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
                amount = _cents(form.get("amount", ""))
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

    # ---- props and futures (D-041) ---------------------------------------------------------

    def props_page(
        request: Request,
        template: str,
        form: dict[str, str] | None = None,
        preview: Any = None,
        error: str | None = None,
        status: int = 200,
    ) -> Response:
        state = request.app.state
        template = template if template in props_service.PROP_TEMPLATES else "milestone_by"
        now = state.domain_clock.now()
        with state.engine.connect() as conn:
            config = instance.read(conn)
            tz = config.tz if config else state.settings.tz
            today = now.astimezone(tz).date()
            canon = canonical_weigh_ins(conn, today - timedelta(days=13), today)
            open_props = list(
                conn.execute(
                    select(Market.id.label("market_id"), Market.title, Market.status)
                    .where(
                        Market.origin == "admin",
                        Market.status.in_(("open", "locked")),
                    )
                    .order_by(Market.id.desc())
                    .limit(30)
                ).all()
            )
        unit = config.unit if config else state.settings.wp_unit
        latest = canon[-1].value / 10 if canon else None
        metrics = [
            (m, stats_service.METRIC_LABELS[m]) for m in (config.enabled_metrics if config else ())
        ]
        return render(
            request,
            "admin/props.html",
            {
                "enabled": bool(config and config.flags.get("props_futures")),
                "templates": list(props_service.LABELS.items()),
                "labels": props_service.LABELS,
                "short": {
                    "milestone_by": "Milestone",
                    "streak_reaches": "Streak",
                    "beat_last_week": "Week vs week",
                    "future_total_change": "Future",
                },
                "template": template,
                "form": form or {},
                "preview": preview,
                "error": error,
                "unit": unit,
                "trend_text": f"latest weigh-in {latest:.1f} {unit}"
                if latest
                else "no recent weigh-ins",
                "suggest_threshold": f"{(latest or 0) - 1:.1f}" if latest else "",
                "suggest_deadline": (today + timedelta(days=7)).isoformat(),
                "min_day": (today + timedelta(days=2)).isoformat(),
                "max_day": (today + timedelta(days=28)).isoformat(),
                "suggest_future": (today + timedelta(days=30)).isoformat(),
                "min_future": (today + timedelta(days=2)).isoformat(),
                "max_future": (today + timedelta(days=120)).isoformat(),
                "metrics": metrics,
                "open_props": open_props,
                "active": "props",
            },
            status,
        )

    @router.get("/props")
    def props(request: Request, session: Admin, template: str = "milestone_by") -> Response:
        return props_page(request, template)

    @router.post("/props/preview")
    async def props_preview(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        template = form.get("template", "")
        try:
            with state.engine.connect() as conn:
                config = instance.read(conn)
                if config is None:
                    raise props_service.PropError("Finish setup first.")
                today = state.domain_clock.now().astimezone(config.tz).date()
                result = props_service.preview(conn, config, today, template, dict(form))
        except props_service.PropError as exc:
            return props_page(request, template, dict(form), error=exc.message, status=400)
        return props_page(request, template, dict(form), preview=result)

    @router.post("/props/create")
    async def props_create(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        template = form.get("template", "")
        if not await reauth(request, session, form, "market.create_prop"):
            return props_page(
                request, template, dict(form), error=REAUTH_FAILED_MESSAGE, status=403
            )
        try:
            await run_in_threadpool(
                props_service.create,
                state.engine,
                state.domain_clock,
                actor(request, session),
                template,
                dict(form),
            )
        except props_service.PropError as exc:
            return props_page(request, template, dict(form), error=exc.message, status=400)
        return RedirectResponse(f"/admin/props?template={template}&ok=prop", status_code=303)

    # ---- discord and flags -----------------------------------------------------------------------

    def discord_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            configured = {
                n.split(".", 1)[1] for n in secret_store.names(conn) if n.startswith("webhook.")
            }
            config = instance.read(conn)
            recent = admin_views.recent_outbox(conn)
        return render(
            request,
            "admin/discord.html",
            {
                "categories": list(WEBHOOK_LABELS.items()),
                "labels": WEBHOOK_LABELS,
                "configured": configured,
                "flags": config.flags if config else {},
                "recent": recent,
                "can_store": bool(state.settings.app_secret_key.get_secret_value()),
                "error": error,
                "active": "discord",
            },
            status,
        )

    @router.get("/discord")
    def discord(request: Request, session: Admin) -> Response:
        return discord_page(request)

    @router.post("/discord/webhook")
    async def discord_webhook(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "discord.webhook"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                admin.set_webhook,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form.get("category", ""),
                form.get("url", ""),
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        return back("/admin/discord", "webhook")

    @router.post("/discord/test/{category}")
    async def discord_test(request: Request, session: Admin, category: str) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                admin.send_test, state.engine, state.domain_clock, actor(request, session), category
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 404)
        return back("/admin/discord", "test")

    @router.post("/discord/flags")
    async def discord_flags(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        for flag in admin.ADMIN_FLAGS:
            await run_in_threadpool(
                admin.set_flag,
                state.engine,
                state.auth_clock,
                actor(request, session),
                flag,
                form.get(flag) == "on",
            )
        return back("/admin/discord", "flags")

    # ---- audit ----------------------------------------------------------------------------------

    @router.get("/audit")
    def audit(request: Request, session: Admin, page: int = 0) -> Response:
        page = max(page, 0)
        with request.app.state.engine.connect() as conn:
            rows = admin_views.audit_page(conn, page)
        return render(
            request,
            "admin/audit.html",
            {
                "rows": rows[: admin_views.PAGE],
                "page": page,
                "more": len(rows) > admin_views.PAGE,
                "active": "audit",
            },
        )

    return router
