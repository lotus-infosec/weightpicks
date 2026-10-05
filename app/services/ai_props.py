"""AI-packaged props (BUILD_PLAN §1.4.4; D-042).

Code builds the digest and menu, Workers AI proposes `{template, params, title, blurb}`,
and code validates each proposal on its own: on the menu, settleable and priced by the
engine (the same path as the admin prop form), fair probability 0.08-0.92, not a
duplicate, under the open-prop cap, and clean text. Review mode queues proposals for the
admin; auto mode publishes them. The market title is always code-built; the AI blurb is
flavour only. Settlement never reads anything here.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import structlog
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, func, insert, select, update

from app.ai import digest, runs
from app.ai.client import from_store
from app.ai.schemas import MAX_PROPOSALS, PROPOSALS_SCHEMA, Proposal
from app.ai.text import problem
from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.markets import COUNT_MARKET_METRICS, MarketStatus, Timeframe
from app.domain.schedule import local_date
from app.models import AdminNote, AiProposal, AiRun, Bet, BetLeg, Market, Season, Selection, User
from app.services import audit, instance, props
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.markets import pricing_data

log = structlog.get_logger()

PROMPT_VERSION = "props_v1"
MAX_TOKENS = 600
P_MIN, P_MAX = 0.08, 0.92
OPEN_PROP_CAP = 8
PROP_TIMEFRAMES = (Timeframe.PROP.value, Timeframe.FUTURE.value)


@dataclass(slots=True)
class RunReport:
    run_id: int | None = None
    status: str = "off"  # off | skipped | error | ok
    reason: str | None = None
    created: list[int] = field(default_factory=list)  # market ids (auto mode)
    queued: list[int] = field(default_factory=list)  # proposal ids (review mode)
    dropped: list[dict[str, Any]] = field(default_factory=list)


class ApprovalError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---- reads ------------------------------------------------------------------------------


def bettor_names(conn: Connection) -> list[str]:
    return list(conn.execute(select(User.display_name).where(User.role == "player")).scalars())


def _storylines(conn: Connection, now: datetime) -> tuple[str, ...]:
    """Nameless crowd context from open daily markets and recent settled single bets."""
    splits: list[tuple[str, int, int]] = []
    open_markets = conn.execute(
        select(Market.id, Market.title)
        .where(Market.status == MarketStatus.OPEN.value, Market.lock_at > now)
        .order_by(Market.lock_at, Market.id)
        .limit(3)
    ).all()
    for market_id, title in open_markets:
        stakes = dict(
            conn.execute(
                select(Selection.side, func.coalesce(func.sum(Bet.stake_cents), 0))
                .join(BetLeg, BetLeg.selection_id == Selection.id)
                .join(Bet, Bet.id == BetLeg.bet_id)
                .where(Selection.market_id == market_id, Bet.kind == "single")
                .group_by(Selection.side)
            ).all()
        )
        a = int(stakes.get("over", 0) + stakes.get("yes", 0))
        b = int(stakes.get("under", 0) + stakes.get("no", 0))
        splits.append((title, a, b))
    recent = conn.execute(
        select(Bet.user_id, Bet.status)
        .where(Bet.status.in_(("won", "lost")), Bet.kind == "single")
        .order_by(Bet.settled_at.desc(), Bet.id.desc())
        .limit(500)
    ).all()
    runs_by_user: dict[int, tuple[str, int, bool]] = {}  # user -> (result, length, closed)
    for user_id, status in recent:
        result, length, closed = runs_by_user.get(user_id, (status, 0, False))
        if closed:
            continue
        if status == result:
            runs_by_user[user_id] = (result, length + 1, False)
        else:
            runs_by_user[user_id] = (result, length, True)
    streaks = [(r, n) for r, n, _ in runs_by_user.values()]
    return digest.crowd_storylines(splits, streaks)


def snapshot(
    conn: Connection, config: InstanceConfig, today: date, now: datetime
) -> digest.Snapshot:
    data = pricing_data(conn, today)
    season_id = active_season_id(conn)
    direction = (
        conn.execute(select(Season.direction).where(Season.id == season_id)).scalar_one_or_none()
        if season_id
        else None
    ) or "down"
    notes = tuple(
        conn.execute(
            select(AdminNote.text)
            .where(AdminNote.active_from <= today, AdminNote.active_to >= today)
            .order_by(AdminNote.id)
            .limit(5)
        ).scalars()
    )
    metrics = tuple(m for m in config.enabled_metrics if m in COUNT_MARKET_METRICS)
    return digest.Snapshot(
        today=today,
        unit=config.unit,
        direction=direction,
        weigh_ins=data.weigh_ins,
        daily_totals=data.daily_totals,
        metrics=metrics,
        notes=notes,
        storylines=_storylines(conn, now),
    )


def open_prop_count(conn: Connection, now: datetime) -> int:
    markets = conn.execute(
        select(func.count()).where(
            Market.timeframe.in_(PROP_TIMEFRAMES), Market.status == MarketStatus.OPEN.value
        )
    ).scalar_one()
    pending = conn.execute(
        select(func.count()).where(AiProposal.status == "pending", AiProposal.expires_at > now)
    ).scalar_one()
    return int(markets) + int(pending)


def expire(conn: Connection, now: datetime) -> int:
    return conn.execute(
        update(AiProposal)
        .where(AiProposal.status == "pending", AiProposal.expires_at <= now)
        .values(status="expired", reason="not approved before the bet lock", decided_at=now)
    ).rowcount


# ---- validation -------------------------------------------------------------------------


def _check_priced(
    conn: Connection, config: InstanceConfig, today: date, template: str, form: dict[str, str]
) -> props.Preview | str:
    try:
        p = props.preview(conn, config, today, template, form)
    except props.PropError:
        return "not_priceable"
    if not P_MIN <= p.pricing.p_over <= P_MAX:
        return "probability_out_of_range"
    return p


MAX_OPTIONS = 4


def _spread(items: list[dict[str, str]], k: int) -> list[dict[str, str]]:
    if len(items) <= k:
        return items
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def priced_menu(
    conn: Connection, config: InstanceConfig, today: date, menu: digest.Menu
) -> digest.Menu:
    """Price the weight-prop candidates and keep up to MAX_OPTIONS of each template that
    the engine would actually offer in the 0.08-0.92 band."""
    kept: dict[str, list[dict[str, str]]] = {}
    for template in ("milestone_by", "streak_reaches"):
        ok = [
            form
            for form in menu.candidates(template)
            if not isinstance(_check_priced(conn, config, today, template, form), str)
        ]
        kept[template] = _spread(ok, MAX_OPTIONS)
    return menu.with_options(kept["milestone_by"], kept["streak_reaches"])


def _is_duplicate(conn: Connection, key: str, now: datetime, skip_id: int | None = None) -> bool:
    if conn.execute(select(Market.id).where(Market.dedupe_key == key)).first():
        return True
    query = select(AiProposal.id).where(
        AiProposal.dedupe_key == key, AiProposal.status == "pending", AiProposal.expires_at > now
    )
    if skip_id is not None:
        query = query.where(AiProposal.id != skip_id)
    return conn.execute(query).first() is not None


def _parse(data: Any) -> list[Any]:
    items = data.get("proposals") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


# ---- the run ----------------------------------------------------------------------------


def run(
    engine: Engine,
    clock: Clock,
    real_clock: Clock,
    settings: Settings,
    kind: str,
    want: int | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> RunReport:
    """One AI prop cycle. Never raises for AI problems; core markets never wait on it.
    `want` defaults to the cycle's size (`wanted`)."""
    report = RunReport()
    now = clock.now()
    built: digest.Digest | None = None
    menu = digest.Menu(today=now.date())
    client = None
    names: list[str] = []
    with engine.connect() as conn:
        config = instance.read(conn)
        if config is None or not config.flags.get("ai_props"):
            return report  # off: no AI run at all
        if not config.flags.get("props_futures"):
            report.status, report.reason = "skipped", "props_off"
        elif instance.current_state(conn) == instance.FROZEN or active_season_id(conn) is None:
            report.status, report.reason = "skipped", "no_season"
        else:
            today = local_date(now, config.tz)
            built = digest.build(snapshot(conn, config, today, now))
            menu = priced_menu(conn, config, today, built.menu)
            if not menu.templates:
                report.status, report.reason = "skipped", "empty_menu"
            elif open_prop_count(conn, now) >= OPEN_PROP_CAP:
                report.status, report.reason = "skipped", "prop_cap"
            client = from_store(conn, settings, transport)
            names = bettor_names(conn)
    if report.status == "skipped" or built is None:
        report.run_id = runs.record_skip(engine, real_clock, kind, report.reason or "skipped")
        report.status = "skipped"
        return report
    want = max(1, min(want or wanted(kind, len(built.triggers)), MAX_PROPOSALS))

    messages = [
        {"role": "system", "content": runs.prompt(PROMPT_VERSION).format(want=want)},
        {"role": "user", "content": json.dumps(built.payload(menu), separators=(",", ":"))},
    ]
    outcome = runs.call(
        engine,
        real_clock,
        client,
        kind=kind,
        model=settings.ai_model_json,
        prompt_version=PROMPT_VERSION,
        messages=messages,
        max_tokens=MAX_TOKENS,
        cap=settings.ai_daily_neuron_cap,
        json_schema=PROPOSALS_SCHEMA,
    )
    report.run_id, report.status, report.reason = outcome.run_id, outcome.status, outcome.reason
    if outcome.result is None:
        return report

    items = _parse(outcome.result.data)
    if not items:
        report.dropped.append({"index": None, "reason": "no_proposals"})
    with immediate(engine) as conn:
        now = clock.now()
        config = instance.read(conn) or config
        expire(conn, now)
        seen: set[str] = set()
        for index, item in enumerate(items):
            reason, template = _consider(
                conn, clock, config, today, menu, names, item, seen, outcome.run_id, report
            )
            if reason:
                report.dropped.append({"index": index, "template": template, "reason": reason})
            if len(report.created) + len(report.queued) >= want:
                break
        conn.execute(update(AiRun).where(AiRun.id == outcome.run_id).values(errors=report.dropped))
    log.info(
        "ai_props_run",
        kind=kind,
        ai_run_id=outcome.run_id,
        created=len(report.created),
        queued=len(report.queued),
        dropped=[d["reason"] for d in report.dropped],
    )
    return report


def _consider(
    conn: Connection,
    clock: Clock,
    config: InstanceConfig,
    today: date,
    menu: digest.Menu,
    names: list[str],
    item: Any,
    seen: set[str],
    run_id: int,
    report: RunReport,
) -> tuple[str | None, str | None]:
    """Validate one proposal and queue or publish it. Returns (drop reason, template)."""
    try:
        proposal = Proposal.model_validate(item)
    except ValidationError:
        return "bad_shape", item.get("template") if isinstance(item, dict) else None
    template = proposal.template
    for text in (proposal.title, proposal.blurb):
        bad = problem(text, names)
        if bad:
            return bad, template
    form = menu.form(template, proposal.params)
    if isinstance(form, str):
        return form, template
    priced = _check_priced(conn, config, today, template, form)
    if isinstance(priced, str):
        return priced, template
    key = priced.spec.dedupe_key
    now = clock.now()
    if key in seen or _is_duplicate(conn, key, now):
        return "duplicate", template
    if open_prop_count(conn, now) >= OPEN_PROP_CAP:
        return "prop_cap", template
    if priced.spec.lock_at <= now:
        return "lock_passed", template
    seen.add(key)
    blurb = proposal.blurb.strip()
    if config.ai_mode == "auto":
        try:
            market_id = props.insert_prop(
                conn, clock, config, priced, origin="ai", ai_run_id=run_id, blurb=blurb
            )
        except props.PropError:
            return "not_insertable", template
        audit.record(
            conn,
            clock,
            None,
            action="market.create_ai_prop",
            target=("market", market_id),
            after={"template": template, "params": priced.spec.params, "ai_run_id": run_id},
        )
        report.created.append(market_id)
    else:
        proposal_id = conn.execute(
            insert(AiProposal)
            .values(
                ai_run_id=run_id,
                template=template,
                form=form,
                title=priced.spec.title,
                blurb=blurb,
                dedupe_key=key,
                status="pending",
                created_at=now,
                expires_at=priced.spec.lock_at,
            )
            .returning(AiProposal.id)
        ).scalar_one()
        report.queued.append(proposal_id)
    return None, template


# ---- review queue -----------------------------------------------------------------------


def approve(engine: Engine, clock: Clock, actor: Any, proposal_id: int) -> int:
    """Re-build and re-price from today's data, re-check every rule, publish."""
    with immediate(engine) as conn:
        now = clock.now()
        expire(conn, now)
        row = conn.execute(select(AiProposal).where(AiProposal.id == proposal_id)).one_or_none()
        if row is None or row.status != "pending":
            raise ApprovalError("That proposal is no longer waiting for review.")
        config = instance.read(conn)
        if config is None:
            raise ApprovalError("Finish setup first.")
        today = local_date(now, config.tz)
        priced = _check_priced(conn, config, today, row.template, dict(row.form))
        if isinstance(priced, str):
            raise ApprovalError(
                "The engine won't price this any more (data moved or it's near certain)."
            )
        if _is_duplicate(conn, priced.spec.dedupe_key, now, skip_id=row.id):
            raise ApprovalError("That prop already exists.")
        if open_prop_count(conn, now) - 1 >= OPEN_PROP_CAP:
            raise ApprovalError(f"There are already {OPEN_PROP_CAP} open props.")
        try:
            market_id = props.insert_prop(
                conn, clock, config, priced, origin="ai", ai_run_id=row.ai_run_id, blurb=row.blurb
            )
        except props.PropError as exc:
            raise ApprovalError(exc.message) from exc
        conn.execute(
            update(AiProposal)
            .where(AiProposal.id == row.id)
            .values(status="approved", market_id=market_id, decided_at=now)
        )
        audit.record(
            conn,
            clock,
            actor,
            action="ai_proposal.approve",
            target=("market", market_id),
            after={"proposal_id": row.id, "template": row.template, "params": priced.spec.params},
        )
    return market_id


def reject(engine: Engine, clock: Clock, actor: Any, proposal_id: int) -> None:
    with immediate(engine) as conn:
        now = clock.now()
        changed = conn.execute(
            update(AiProposal)
            .where(AiProposal.id == proposal_id, AiProposal.status == "pending")
            .values(status="rejected", reason="rejected by the admin", decided_at=now)
        ).rowcount
        if not changed:
            raise ApprovalError("That proposal is no longer waiting for review.")
        audit.record(
            conn,
            clock,
            actor,
            action="ai_proposal.reject",
            target=("ai_proposal", proposal_id),
        )


def pending(conn: Connection, now: datetime) -> list[Any]:
    return list(
        conn.execute(
            select(AiProposal)
            .where(AiProposal.status == "pending", AiProposal.expires_at > now)
            .order_by(AiProposal.id)
        ).all()
    )


def wanted(kind: str, triggers: int) -> int:
    """How many proposals a cycle asks for: daily 1-2 (+1 on an event), weekly 2-3."""
    if kind == "props_weekly":
        return 3
    return 3 if triggers else 2
