"""The /setup wizard. Mounted always; the gate middleware in
app.web.main sends everything here until setup completes and 404s it afterwards."""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from app.core.crypto import SecretKeyMissing
from app.domain import setup as steps
from app.domain.economy import VIG_PRESETS, Economy
from app.domain.markets import COUNT_MARKET_METRICS, Schedule
from app.services import setup
from app.services.secrets import WEBHOOK_CATEGORIES
from app.services.setup import SetupError
from app.web import format as fmt
from app.web.security import client_ip, csrf_cookie_token, read_form, set_csrf_cookie

SETUP_COOKIE = "wp_setup"
WEBHOOK_LABELS = {
    "bets_placed": "Bets placed",
    "high_roller": "High-roller bets",
    "bet_results": "Bet results",
    "parlay_results": "Parlay results",
    "market_settlements": "Market settlements",
    "new_markets": "New markets and props",
    "special_events": "Special events",
    "hype": "Stat updates and hype",
    "weekly_standings": "Weekly standings",
    "busts": "Busts and badges",
    "goal_reached": "Goal reached",
    "admin_alerts": "Admin alerts (private)",
}
NO_KEY = (
    "APP_SECRET_KEY isn't set on this instance, so secrets can't be saved. "
    "Leave this empty for now."
)


def form_values(key: str, draft: dict[str, Any], request: Request) -> dict[str, Any]:
    """What to show in a step's fields: the draft, else sensible defaults."""
    settings = request.app.state.settings
    saved = draft.get(key, {})
    if key == "admin":
        return {"email": saved.get("email", ""), "display_name": saved.get("display_name", "")}
    if key == "subject":
        if not saved:
            return {
                "subject_name": "",
                "unit": settings.wp_unit,
                "start_weight": "",
                "goal_weight": "",
            }
        return {
            "subject_name": saved["subject_name"],
            "unit": saved["unit"],
            "start_weight": fmt.tenths(saved["start_weight_x10"]),
            "goal_weight": fmt.tenths(saved["goal_weight_x10"]),
        }
    if key == "schedule":
        if saved:
            return dict(saved)
        d = Schedule()
        return {
            "timezone": settings.wp_timezone,
            "daily_drop": f"{d.daily_drop:%H:%M}",
            "bet_lock": f"{d.bet_lock:%H:%M}",
            "weekly_drop_weekday": d.weekly_drop_weekday,
            "weekly_drop": f"{d.weekly_drop:%H:%M}",
            "monthly_drop": f"{d.monthly_drop:%H:%M}",
        }
    if key == "economy":
        e = Economy.from_json(saved) if saved else Economy()
        return {
            "starting_bankroll": steps.money_text(e.starting_bankroll_cents),
            "daily_allowance": steps.money_text(e.daily_allowance_cents),
            "bailout": steps.money_text(e.bailout_cents),
            "bailout_cooldown_days": e.bailout_cooldown_days,
            "vig": e.vig_preset,
            "max_bet": steps.money_text(e.max_bet_cents),
            "max_parlay_legs": e.max_parlay_legs,
            "high_roller": steps.money_text(e.high_roller_cents),
        }
    if key == "stats":
        chosen = saved.get("enabled_metrics", list(COUNT_MARKET_METRICS))
        return {"enabled": set(chosen)}
    if key == "smtp":
        return {k: saved.get(k, "") for k in ("host", "port", "username", "from_address")}
    if key == "appearance":
        return {
            "app_name": saved.get("app_name", "WeightPicks"),
            "palette": saved.get("palette", "ember"),
        }
    return dict(saved)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/setup", include_in_schema=False)

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        csrf, new = csrf_cookie_token(request)
        base = {"form_csrf": csrf, "csrf": "", "session": None, "steps": steps.STEPS}
        page: Response = request.app.state.templates.TemplateResponse(
            request, name, base | context, status_code=status
        )
        if new:
            set_csrf_cookie(page, request, csrf)
        return page

    def draft_of(request: Request) -> setup.Draft | None:
        state = request.app.state
        return setup.resolve(state.engine, state.auth_clock, request.cookies.get(SETUP_COOKIE))

    def restart() -> Response:
        return RedirectResponse("/setup", status_code=303)

    @router.get("")
    def start(request: Request) -> Response:
        draft = draft_of(request)
        if draft is None:
            return render(request, "setup/token.html", {"error": None})
        missing = steps.missing_steps(draft.data)
        target = missing[0].number if missing else steps.STEPS[-1].number
        return RedirectResponse(f"/setup/step/{target}", status_code=303)

    @router.post("/token")
    async def token(request: Request) -> Response:
        form = await read_form(request)
        state = request.app.state
        try:
            cookie = await run_in_threadpool(
                setup.start_session,
                state.engine,
                state.auth_clock,
                token=form.get("token", ""),
                ip=client_ip(request),
            )
        except SetupError as exc:
            status = 429 if exc.code == "rate_limited" else 400
            return render(request, "setup/token.html", {"error": exc.message}, status)
        response = RedirectResponse("/setup", status_code=303)
        response.set_cookie(
            SETUP_COOKIE,
            cookie,
            max_age=int(setup.SESSION_TTL.total_seconds()),
            httponly=True,
            secure=state.settings.wp_cookie_secure,
            samesite="lax",
            path="/setup",
        )
        return response

    @router.get("/step/{number}")
    async def show(request: Request, number: int) -> Response:
        draft = draft_of(request)
        if draft is None:
            return restart()
        try:
            step = steps.step(number)
        except KeyError:
            return RedirectResponse("/setup", status_code=303)
        data = draft.data
        if step.key == "registration" and "registration" not in data:
            data = await run_in_threadpool(
                setup.save,
                request.app.state.engine,
                request.app.state.auth_clock,
                request.cookies.get(SETUP_COOKIE, ""),
                "registration",
                {"code": setup.new_code()},
            )
        return _step_page(request, step, data, {})

    def _step_page(
        request: Request, step: steps.Step, data: dict[str, Any], errors: dict[str, str]
    ) -> Response:
        settings = request.app.state.settings
        context = {
            "step": step,
            "values": form_values(step.key, data, request),
            "errors": errors,
            "draft": data,
            "secret_names": set(data.get("secrets", {})),
            "can_store_secrets": len(settings.app_secret_key.get_secret_value()) >= 32,
            "missing": steps.missing_steps(data),
            "zones": steps.COMMON_ZONES,
            "weekdays": list(enumerate(steps.WEEKDAYS)),
            "metrics": steps.metric_choices(),
            "metric_labels": dict(steps.metric_choices()),
            "vigs": list(VIG_PRESETS),
            "palettes": steps.PALETTES,
            "webhooks": [(c, WEBHOOK_LABELS[c]) for c in WEBHOOK_CATEGORIES],
            "economy": Economy.from_json(data["economy"]) if "economy" in data else None,
        }
        return render(request, f"setup/step_{step.key}.html", context, 400 if errors else 200)

    @router.post("/step/{number}")
    async def submit(request: Request, number: int) -> Response:
        draft = draft_of(request)
        if draft is None:
            return restart()
        try:
            step = steps.step(number)
        except KeyError:
            return restart()
        form = await read_form(request)
        cookie = request.cookies.get(SETUP_COOKIE, "")
        state = request.app.state
        key_text = state.settings.app_secret_key.get_secret_value()
        data = draft.data
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        secrets_map: dict[str, str] = dict(data.get("secrets", {}))

        def keep_or_set(name: str, raw: str, clear: bool) -> None:
            if clear:
                secrets_map.pop(name, None)
            elif raw:
                try:
                    secrets_map[name] = setup.encrypt_for_draft(key_text, raw)
                except SecretKeyMissing:
                    errors[name] = NO_KEY

        if step.key == "admin":
            values, errors = await run_in_threadpool(
                setup.admin_values,
                form.get("email", ""),
                form.get("display_name", ""),
                form.get("password", ""),
                form.get("confirm", ""),
                data.get("admin", {}).get("password_hash"),
            )
        elif step.key == "ai":
            account_id, token = form.get("account_id", "").strip(), form.get("token", "").strip()
            clear = form.get("clear") == "on"
            problems = {} if clear else steps.workers_ai_problem(account_id, token)
            errors.update(problems)
            if not problems:
                keep_or_set("workers_ai.account_id", account_id, clear)
                keep_or_set("workers_ai.token", token, clear)
            values = {"configured": "workers_ai.token" in secrets_map}
        elif step.key == "discord":
            for category in WEBHOOK_CATEGORIES:
                url = form.get(f"webhook_{category}", "").strip()
                problem = steps.webhook_problem(url)
                if problem:
                    errors[f"webhook_{category}"] = problem
                    continue
                keep_or_set(f"webhook.{category}", url, form.get(f"clear_{category}") == "on")
            values = {
                "categories": sorted(
                    n.split(".", 1)[1] for n in secrets_map if n.startswith("webhook.")
                )
            }
        elif step.key == "smtp":
            values, errors = steps.smtp(form)
            if not errors:
                if values.get("configured"):
                    keep_or_set("smtp.password", form.get("password", ""), False)
                else:
                    secrets_map.pop("smtp.password", None)
        elif step.key == "registration":
            values = (
                {"code": setup.new_code()}
                if form.get("action") == "regenerate"
                else data.get("registration", {"code": setup.new_code()})
            )
        elif step.key == "review":
            return RedirectResponse("/setup/step/12", status_code=303)
        else:
            values, errors = steps.VALIDATORS[step.key](form)

        if errors:
            preview = data | ({step.key: values} if values else {})
            return _step_page(request, step, preview, errors)
        try:
            await run_in_threadpool(
                setup.save, state.engine, state.auth_clock, cookie, step.key, values
            )
            if secrets_map != data.get("secrets", {}):
                await run_in_threadpool(
                    setup.save, state.engine, state.auth_clock, cookie, "secrets", secrets_map
                )
        except SetupError:
            return restart()
        if form.get("action") == "regenerate":
            return RedirectResponse(f"/setup/step/{number}", status_code=303)
        if form.get("action") == "back":
            return RedirectResponse(f"/setup/step/{max(1, number - 1)}", status_code=303)
        return RedirectResponse(f"/setup/step/{min(number + 1, len(steps.STEPS))}", status_code=303)

    @router.post("/finish")
    async def finish(request: Request) -> Response:
        state = request.app.state
        cookie = request.cookies.get(SETUP_COOKIE, "")
        if (await read_form(request)).get("action") == "back":
            return RedirectResponse(f"/setup/step/{len(steps.STEPS) - 1}", status_code=303)
        try:
            await run_in_threadpool(
                setup.finish,
                state.engine,
                state.auth_clock,
                cookie,
                app_secret_key=state.settings.app_secret_key.get_secret_value(),
            )
        except SetupError as exc:
            draft = draft_of(request)
            if draft is None:
                return restart()
            page = _step_page(request, steps.STEPS[-1], draft.data, {"finish": exc.message})
            return page
        state.setup_done = True
        response = RedirectResponse("/login?welcome=1", status_code=303)
        response.delete_cookie(SETUP_COOKIE, path="/setup")
        return response

    return router
