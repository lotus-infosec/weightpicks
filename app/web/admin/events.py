"""Special events and the season: freeze, Goal Reached, new season (D-011, D-043)."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.domain.money import parse_cents
from app.domain.units import parse_tenths
from app.services import (
    admin_ai,
    admin_views,
    ai_props,
    events,
    instance,
    pools,
    season,
)
from app.services.observations import canonical_weigh_ins
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form


def register(router: APIRouter) -> None:
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
        cents = parse_cents(form.get("buy_in", ""))
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
                    actor=who,
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
            start = parse_tenths(form.get("start_weight", ""))
            goal = parse_tenths(form.get("goal_weight", ""))
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
