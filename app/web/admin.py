"""Admin panel I (BUILD_PLAN §1.5, CONCEPT §8, D-037). Every route requires the admin;
destructive and money actions re-prompt for the admin's password."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.core.security import verify_password
from app.domain import setup as setup_steps
from app.domain.economy import VIG_PRESETS, Economy
from app.models import Command, Market, User
from app.notify import email as email_settings
from app.services import (
    admin,
    admin_ai,
    admin_views,
    ai_props,
    appearance,
    backups,
    board,
    events,
    instance,
    maintenance,
    pools,
    season,
)
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
    "approved": "AI prop approved: re-priced and posted.",
    "rejected": "AI proposal rejected.",
    "ai_mode": "AI publishing mode saved.",
    "ai_run": "AI run requested. The worker picks it up within a minute.",
    "workers_ai": "Workers AI settings saved.",
    "note": "Note saved.",
    "note_ended": "Note ended.",
    "pool": "Special event published. Players can enter until the night before.",
    "pool_void": "Special event voided and every buy-in refunded.",
    "instance_frozen": "Instance frozen: no bets or pool entries until you unfreeze.",
    "instance_unfrozen": "Instance unfrozen.",
    "goal": "Goal Reached: everything is settled or refunded and the instance is frozen.",
    "season": "New season started. Balances carried over; betting is open again.",
    "backup": "Backup created.",
    "appearance": "Appearance saved.",
    "logo": "Logo saved. It may take a refresh to show everywhere.",
    "logo_removed": "Logo removed: back to the default.",
    "secrets_cleared": "Unreadable secrets cleared. Enter the integrations again in Settings.",
    "smtp": "Email settings saved.",
    "smtp_test": "Test email queued to your address. It should arrive within a minute.",
}
REAUTH_FAILED_MESSAGE = "That's not your password. Nothing was changed."
MAX_RESTORE_BYTES = 512 * 1024 * 1024


def _tenths(raw: str) -> int | None:
    """'212.4' -> 2124 (tenths of the unit), or None."""
    try:
        value = Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0 or value > 1500:
        return None
    return int((value * 10).to_integral_value())


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
                        Market.origin.in_(("admin", "ai")),
                        Market.status.in_(("open", "locked")),
                    )
                    .order_by(Market.id.desc())
                    .limit(30)
                ).all()
            )
            ai_queue: list[dict[str, Any]] = []
            for proposal in ai_props.pending(conn, now):
                if config is None:
                    break
                try:
                    priced = props_service.preview(
                        conn, config, today, proposal.template, dict(proposal.form)
                    )
                except props_service.PropError:
                    priced = None
                ai_queue.append({"row": proposal, "preview": priced})
            last_run = conn.execute(
                select(Command.status, Command.result, Command.created_at)
                .where(Command.type == "ai_props_now")
                .order_by(Command.id.desc())
                .limit(1)
            ).one_or_none()
            ai_configured = admin_ai.workers_ai_configured(conn)
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
                "ai_queue": ai_queue,
                "ai_on": bool(config and config.flags.get("ai_props")),
                "ai_mode": config.ai_mode if config else "review",
                "ai_configured": ai_configured,
                "ai_last_run": last_run,
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

    # ---- AI review queue, mode, runs and notes (D-042) ------------------------------------

    @router.post("/props/ai/{proposal_id}/approve")
    async def ai_approve(request: Request, session: Admin, proposal_id: int) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                ai_props.approve,
                state.engine,
                state.domain_clock,
                actor(request, session),
                proposal_id,
            )
        except ai_props.ApprovalError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=409)
        return back("/admin/props", "approved")

    @router.post("/props/ai/{proposal_id}/reject")
    async def ai_reject(request: Request, session: Admin, proposal_id: int) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                ai_props.reject,
                state.engine,
                state.domain_clock,
                actor(request, session),
                proposal_id,
            )
        except ai_props.ApprovalError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=409)
        return back("/admin/props", "rejected")

    @router.post("/props/ai/mode")
    async def ai_mode(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "settings.ai_mode"):
            return props_page(request, "milestone_by", error=REAUTH_FAILED_MESSAGE, status=403)
        try:
            await run_in_threadpool(
                admin_ai.set_ai_mode,
                state.engine,
                state.auth_clock,
                actor(request, session),
                form.get("ai_mode", ""),
            )
        except AdminError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=400)
        return back("/admin/props", "ai_mode")

    @router.post("/props/ai/run")
    async def ai_run_now(request: Request, session: Admin) -> Response:
        state = request.app.state
        await run_in_threadpool(
            admin.request_command,
            state.engine,
            state.auth_clock,
            actor(request, session),
            "ai_props_now",
        )
        return back("/admin/props", "ai_run")

    @router.get("/ai")
    def ai_runs(request: Request, session: Admin) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            view = admin_ai.runs_view(
                conn, state.auth_clock.now(), state.settings.ai_daily_neuron_cap
            )
            configured = admin_ai.workers_ai_configured(conn)
        return render(
            request, "admin/ai.html", {"v": view, "configured": configured, "active": "ai"}
        )

    def notes_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
            tz = config.tz if config else state.settings.tz
            today = state.domain_clock.now().astimezone(tz).date()
            rows = admin_ai.notes(conn, today)
        return render(
            request,
            "admin/notes.html",
            {
                "notes": rows,
                "today": today.isoformat(),
                "max_to": (today + timedelta(days=admin_ai.NOTE_MAX_DAYS)).isoformat(),
                "error": error,
                "active": "notes",
            },
            status,
        )

    @router.get("/notes")
    def notes(request: Request, session: Admin) -> Response:
        return notes_page(request)

    @router.post("/notes")
    async def notes_add(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        try:
            start = date.fromisoformat(form.get("active_from", ""))
            end = date.fromisoformat(form.get("active_to", ""))
        except ValueError:
            return notes_page(request, "Pick a start and end date.", 400)
        try:
            await run_in_threadpool(
                admin_ai.add_note,
                state.engine,
                state.auth_clock,
                actor(request, session),
                form.get("text", ""),
                start,
                end,
            )
        except AdminError as exc:
            return notes_page(request, exc.message, 400)
        return back("/admin/notes", "note")

    @router.post("/notes/{note_id}/end")
    async def notes_end(request: Request, session: Admin, note_id: int) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
        tz = config.tz if config else state.settings.tz
        today = state.domain_clock.now().astimezone(tz).date()
        try:
            await run_in_threadpool(
                admin_ai.end_note,
                state.engine,
                state.auth_clock,
                actor(request, session),
                note_id,
                today,
            )
        except AdminError as exc:
            return notes_page(request, exc.message, 404)
        return back("/admin/notes", "note_ended")

    # ---- special events (pools, D-043) -----------------------------------------------------

    def events_page(
        request: Request,
        form: dict[str, str] | None = None,
        draft: Any = None,
        error: str | None = None,
        notes: tuple[str, ...] = (),
        status: int = 200,
    ) -> Response:
        state = request.app.state
        now = state.domain_clock.now()
        with state.engine.connect() as conn:
            config = instance.read(conn)
            tz = config.tz if config else state.settings.tz
            today = now.astimezone(tz).date()
            listing = admin_views.pools(conn)
            projected = (
                events.projection(conn, config, today, draft.target_date)
                if draft is not None and config is not None
                else None
            )
            ai_configured = admin_ai.workers_ai_configured(conn)
        economy = config.economy if config else None
        return render(
            request,
            "admin/events.html",
            {
                "enabled": bool(config and config.flags.get("special_events")),
                "ai_configured": ai_configured,
                "form": form or {},
                "draft": draft,
                "projected": projected,
                "notes": notes or (draft.notes if draft is not None else ()),
                "error": error,
                "pools": listing,
                "unit": config.unit if config else state.settings.wp_unit,
                "min_day": (today + timedelta(days=pools.MIN_DAYS)).isoformat(),
                "max_day": (today + timedelta(days=pools.MAX_DAYS)).isoformat(),
                "default_buyin": (economy.pool_buyin_cents / 100) if economy else 100,
                "max_buyin": (economy.max_pool_buyin_cents / 100) if economy else 500,
                "active": "events",
            },
            status,
        )

    def _event_raw(form: dict[str, str]) -> dict[str, Any]:
        cents = _cents(form.get("buy_in", ""))
        return {
            "title": form.get("title", ""),
            "question": form.get("question", ""),
            "target_date": form.get("target_date", ""),
            "buy_in_cents": cents if cents is not None else "bad",
        }

    @router.get("/events")
    def events_index(request: Request, session: Admin) -> Response:
        return events_page(request)

    @router.post("/events/draft")
    async def events_draft(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        result = await run_in_threadpool(
            events.draft_with_ai,
            state.engine,
            state.domain_clock,
            state.auth_clock,
            state.settings,
            form.get("text", ""),
        )
        if result.draft is None:
            raw = result.raw or {}
            refill = {
                "text": form.get("text", ""),
                "title": str(raw.get("title") or ""),
                "question": str(raw.get("question") or ""),
                "target_date": str(raw.get("target_date") or ""),
                "buy_in": f"{raw['buy_in_cents'] / 100:.2f}" if raw.get("buy_in_cents") else "",
            }
            return events_page(request, refill, error=result.error, status=400)
        d = result.draft
        filled = {
            "text": form.get("text", ""),
            "title": d.title,
            "question": d.question,
            "target_date": d.target_date.isoformat(),
            "buy_in": f"{d.buy_in_cents / 100:.2f}",
            "ai_run_id": str(result.run_id or ""),
        }
        return events_page(request, filled, draft=d)

    @router.post("/events/preview")
    async def events_preview(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
            names = ai_props.bettor_names(conn)
        if config is None:
            return events_page(request, form, error="Finish setup first.", status=400)
        try:
            d = pools.validate(config, state.domain_clock.now(), _event_raw(form), names=names)
        except pools.PoolError as exc:
            return events_page(request, form, error=exc.message, status=400)
        return events_page(request, form, draft=d)

    @router.post("/events/publish")
    async def events_publish(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        if not await reauth(request, session, form, "pool.create"):
            return events_page(request, form, error=REAUTH_FAILED_MESSAGE, status=403)
        with state.engine.connect() as conn:
            config = instance.read(conn)
            names = ai_props.bettor_names(conn)
        if config is None or not config.flags.get("special_events"):
            return events_page(
                request, form, error="Special events are turned off (Settings).", status=400
            )
        who = actor(request, session)
        run_id = int(form["ai_run_id"]) if form.get("ai_run_id", "").isdigit() else None
        try:
            d = pools.validate(config, state.domain_clock.now(), _event_raw(form), names=names)
            await run_in_threadpool(
                lambda: pools.create(
                    state.engine,
                    state.domain_clock,
                    d,
                    actor_id=who.user_id,
                    acted_at=who.acted_at,
                    ip=who.ip,
                    ai_run_id=run_id,
                )
            )
        except pools.PoolError as exc:
            return events_page(request, form, error=exc.message, status=400)
        return back("/admin/events", "pool")

    @router.post("/events/{pool_id}/void")
    async def events_void(request: Request, session: Admin, pool_id: int) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "pool.void"):
            return events_page(request, error=REAUTH_FAILED_MESSAGE, status=403)
        try:
            await run_in_threadpool(
                pools.void, state.engine, state.domain_clock, actor(request, session), pool_id
            )
        except pools.PoolError as exc:
            return events_page(request, error=exc.message, status=400)
        return back("/admin/events", "pool_void")

    # ---- season: freeze, Goal Reached, new season (D-011, D-043) ------------------------------

    def season_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        now = state.domain_clock.now()
        with state.engine.connect() as conn:
            config = instance.read(conn)
            tz = config.tz if config else state.settings.tz
            today = now.astimezone(tz).date()
            canon = canonical_weigh_ins(conn, today - timedelta(days=13), today)
            rows = season.history(conn)
            frozen = instance.current_state(conn) == instance.FROZEN
        current = next((r for r in rows if r.ended_at is None), None)
        return render(
            request,
            "admin/season.html",
            {
                "seasons": rows,
                "current": current,
                "frozen": frozen,
                "latest": f"{canon[-1].value / 10:.1f}" if canon else "",
                "unit": config.unit if config else state.settings.wp_unit,
                "error": error,
                "active": "season",
            },
            status,
        )

    @router.get("/season")
    def season_index(request: Request, session: Admin) -> Response:
        return season_page(request)

    @router.post("/season/{action}")
    async def season_action(request: Request, session: Admin, action: str) -> Response:
        form = await read_form(request)
        state = request.app.state
        if action not in ("freeze", "unfreeze", "goal", "new"):
            return season_page(request, "Unknown action.", 404)
        if not await reauth(request, session, form, f"season.{action}"):
            return season_page(request, REAUTH_FAILED_MESSAGE, 403)
        who = actor(request, session)
        try:
            if action in ("freeze", "unfreeze"):
                await run_in_threadpool(
                    season.set_frozen, state.engine, state.domain_clock, who, action == "freeze"
                )
                return back(
                    "/admin/season",
                    "instance_frozen" if action == "freeze" else "instance_unfrozen",
                )
            if action == "goal":
                if form.get("confirm", "").strip().upper() != "GOAL":
                    return season_page(request, "Type GOAL to confirm.", 400)
                done = await run_in_threadpool(
                    lambda: season.goal_reached(state.engine, state.domain_clock, actor=who)
                )
                if done is None:
                    return season_page(request, "Goal Reached already ran for this season.", 409)
                return back("/admin/season", "goal")
            start = _tenths(form.get("start_weight", ""))
            goal = _tenths(form.get("goal_weight", ""))
            if start is None or goal is None:
                return season_page(request, "Enter both weights, like 212.4.", 400)
            await run_in_threadpool(
                lambda: season.new_season(
                    state.engine, state.domain_clock, who, start_x10=start, goal_x10=goal
                )
            )
        except season.SeasonError as exc:
            return season_page(request, exc.message, 400)
        return back("/admin/season", "season")

    # ---- system: health, backups, restore, reset (BUILD_PLAN §1.5, §2.9) --------------------

    def system_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        settings = state.settings
        now = state.auth_clock.now()
        with state.engine.connect() as conn:
            health = admin_views.system_health(conn, now, settings.ai_daily_neuron_cap)
            config = instance.read(conn)
            key = secret_store.key_status(conn, settings.app_secret_key.get_secret_value())
        from app.core.migrations import current_revision, head_revision

        return render(
            request,
            "admin/system.html",
            {
                "h": health,
                "backups_on": bool(config and config.flags.get("backup_ui")),
                "backups": backups.listing(settings),
                "staged": maintenance.pending_action(settings),
                "version": backups.app_version(),
                "revision": current_revision(state.engine),
                "head": head_revision(),
                "key_status": key,
                "app_name": config.app_name if config else "WeightPicks",
                "error": error,
                "active": "system",
            },
            status,
        )

    @router.get("/system")
    def system(request: Request, session: Admin) -> Response:
        return system_page(request)

    @router.post("/system/backup")
    async def system_backup(request: Request, session: Admin) -> Response:
        state = request.app.state
        path = await run_in_threadpool(
            lambda: backups.create(state.engine, state.settings, state.auth_clock, label="admin")
        )
        await run_in_threadpool(
            admin_ai.audit_simple,
            state.engine,
            state.auth_clock,
            actor(request, session),
            "backup.create",
            {"file": path.name},
        )
        return back("/admin/system", "backup")

    @router.post("/system/download")
    async def system_download(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "backup.download"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        path = backups.resolve(state.settings, form.get("name", ""))
        if path is None:
            return system_page(request, "No such backup.", 404)
        await run_in_threadpool(
            admin_ai.audit_simple,
            state.engine,
            state.auth_clock,
            actor(request, session),
            "backup.download",
            {"file": path.name},
        )
        return FileResponse(path, media_type="application/gzip", filename=path.name)

    @router.post("/system/restore")
    async def system_restore(request: Request, session: Admin) -> Response:
        state = request.app.state
        form = await request.form(max_files=1, max_fields=10)
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        if not await reauth(request, session, fields, "maintenance.restore"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        upload = form.get("file")
        source: Path | None = None
        if upload is not None and not isinstance(upload, str) and upload.filename:
            folder = backups.backups_dir(state.settings)
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / f"wp-uploaded-{state.auth_clock.now():%Y%m%d-%H%M%S}-manual.tar.gz"
            written = 0
            with source.open("wb") as out:
                while chunk := await upload.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_RESTORE_BYTES:
                        out.close()
                        source.unlink(missing_ok=True)
                        return system_page(request, "That file is too large.", 413)
                    out.write(chunk)
        elif fields.get("name"):
            source = backups.resolve(state.settings, fields["name"])
        if source is None:
            return system_page(request, "Choose a backup to restore.", 400)
        try:
            await run_in_threadpool(
                maintenance.stage_restore,
                state.engine,
                state.settings,
                state.auth_clock,
                actor(request, session),
                source,
            )
        except maintenance.MaintenanceError as exc:
            return system_page(request, exc.message, 400)
        return render_restart(request, "restore")

    @router.post("/system/reset")
    async def system_reset(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "maintenance.reset"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                lambda: maintenance.stage_reset(
                    state.engine,
                    state.settings,
                    state.auth_clock,
                    actor(request, session),
                    confirm=form.get("confirm", ""),
                    wipe_garmin=form.get("wipe_garmin") == "on",
                )
            )
        except maintenance.MaintenanceError as exc:
            return system_page(request, exc.message, 400)
        return render_restart(request, "reset")

    def render_restart(request: Request, kind: str) -> Response:
        page = render(request, "admin/restarting.html", {"kind": kind, "active": "system"})
        page.background = BackgroundTask(request.app.state.restart)
        return page

    @router.post("/system/secrets/clear")
    async def system_clear_secrets(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "secrets.clear"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        await run_in_threadpool(
            admin_ai.clear_secrets, state.engine, state.auth_clock, actor(request, session)
        )
        return back("/admin/system", "secrets_cleared")

    # ---- appearance (D-044) ---------------------------------------------------------------

    def appearance_page(
        request: Request,
        errors: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
        return render(
            request,
            "admin/appearance.html",
            {
                "palettes": list(setup_steps.PALETTES.items()),
                "values": form
                or {
                    "app_name": config.app_name if config else "WeightPicks",
                    "palette": config.palette if config else "ember",
                },
                "errors": errors or {},
                "has_logo": appearance.has_logo(state.settings),
                "active": "appearance",
            },
            status,
        )

    @router.get("/appearance")
    def appearance_index(request: Request, session: Admin) -> Response:
        return appearance_page(request)

    @router.post("/appearance")
    async def appearance_save(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        errors = await run_in_threadpool(
            appearance.set_name_and_palette,
            state.engine,
            state.auth_clock,
            actor(request, session),
            form,
        )
        if errors:
            return appearance_page(request, errors, form, 400)
        return back("/admin/appearance", "appearance")

    @router.post("/appearance/logo")
    async def appearance_logo(request: Request, session: Admin) -> Response:
        state = request.app.state
        form = await request.form(max_files=1, max_fields=10)
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        if not await reauth(request, session, fields, "appearance.logo"):
            return appearance_page(request, {"logo": REAUTH_FAILED_MESSAGE}, status=403)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            return appearance_page(request, {"logo": "Choose an image file."}, status=400)
        data = await upload.read(appearance.MAX_BYTES + 1)
        try:
            await run_in_threadpool(
                appearance.save_logo,
                state.engine,
                state.settings,
                state.auth_clock,
                actor(request, session),
                data,
            )
        except appearance.AppearanceError as exc:
            return appearance_page(request, {"logo": exc.message}, status=400)
        return back("/admin/appearance", "logo")

    @router.post("/appearance/logo/remove")
    async def appearance_logo_remove(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "appearance.logo_removed"):
            return appearance_page(request, {"logo": REAUTH_FAILED_MESSAGE}, status=403)
        await run_in_threadpool(
            appearance.remove_logo,
            state.engine,
            state.settings,
            state.auth_clock,
            actor(request, session),
        )
        return back("/admin/appearance", "logo_removed")

    # ---- discord and flags -----------------------------------------------------------------------

    def discord_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            configured = {
                n.split(".", 1)[1] for n in secret_store.names(conn) if n.startswith("webhook.")
            }
            config = instance.read(conn)
            recent = admin_views.recent_outbox(conn)
            ai_configured = admin_ai.workers_ai_configured(conn)
            smtp = email_settings.stored(conn)
            smtp_password = "smtp.password" in secret_store.names(conn)
        return render(
            request,
            "admin/discord.html",
            {
                "categories": list(WEBHOOK_LABELS.items()),
                "labels": WEBHOOK_LABELS,
                "configured": configured,
                "flags": config.flags if config else {},
                "recent": recent,
                "ai_configured": ai_configured,
                "smtp": smtp,
                "smtp_password": smtp_password,
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

    @router.post("/discord/workers-ai")
    async def discord_workers_ai(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "integrations.workers_ai"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                admin_ai.set_workers_ai,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form.get("account_id", ""),
                form.get("token", ""),
                form.get("clear") == "on",
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        return back("/admin/discord", "workers_ai")

    @router.post("/discord/smtp")
    async def discord_smtp(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        if not await reauth(request, session, form, "integrations.smtp"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            errors = await run_in_threadpool(
                admin_ai.set_smtp,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form,
                form.get("clear") == "on",
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        if errors:
            return discord_page(request, " ".join(errors.values()), 400)
        return back("/admin/discord", "smtp")

    @router.post("/discord/smtp/test")
    async def discord_smtp_test(request: Request, session: Admin) -> Response:
        state = request.app.state
        queued = await run_in_threadpool(
            admin_ai.send_smtp_test, state.engine, state.auth_clock, actor(request, session)
        )
        if not queued:
            return discord_page(request, "Set up email first.", 400)
        return back("/admin/discord", "smtp_test")

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
