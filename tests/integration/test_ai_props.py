"""AI-packaged props end to end with a fake Workers AI (BUILD_PLAN §1.4.4; D-042)."""

from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from app.ai import digest
from app.core.clock import SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate
from app.models import AiProposal, AiRun, AuditEntry, InstanceSettingsRow, Market
from app.services import ai_props, instance, secrets
from app.services.audit import Actor
from tests.fake_ai import Recorder, canned, menu_model
from tests.integration.world import NY, World, create_admin, local

KEY = "k" * 40


@pytest.fixture
def ai(world: World) -> tuple[World, Settings, SimClock]:
    """Flags on, a stored (fake) token, a real-time clock for the quota."""
    settings = world.settings.model_copy(update={"app_secret_key": SecretStr(KEY)})
    with immediate(world.engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(
                flags=dict(flags) | {"props_futures": True, "ai_props": True}
            )
        )
        secrets.put(conn, world.clock, KEY, "workers_ai.account_id", "acct")
        secrets.put(conn, world.clock, KEY, "workers_ai.token", "tok")
    return world, settings, SimClock(local(2026, 10, 5, 12))


def admin_actor(w: World) -> Actor:
    admin_id = create_admin(
        w.engine, SystemClock(), email="a@example.invalid", display_name="A", password="x" * 12
    )
    return Actor(user_id=admin_id)


def set_mode(w: World, mode: str) -> None:
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(ai_mode=mode))


def run(
    w: World, settings: Settings, real: SimClock, rec: Recorder, **kw: Any
) -> ai_props.RunReport:
    return ai_props.run(
        w.engine,
        w.clock,
        real,
        settings,
        kw.pop("kind", "props_daily"),
        transport=rec.transport,
        **kw,
    )


def ai_runs(w: World) -> list[Any]:
    with w.engine.connect() as conn:
        return list(conn.execute(select(AiRun).order_by(AiRun.id)).all())


def menu(w: World) -> digest.Menu:
    with w.engine.connect() as conn:
        config = instance.read(conn)
        assert config is not None
        today = w.clock.now().astimezone(NY).date()
        return digest.build(ai_props.snapshot(conn, config, today, w.clock.now())).menu


def test_flag_off_means_no_run_at_all(world: World) -> None:
    rec = menu_model()
    report = ai_props.run(
        world.engine,
        world.clock,
        SystemClock(),
        world.settings,
        "props_daily",
        transport=rec.transport,
    )
    assert report.status == "off" and ai_runs(world) == [] and rec.requests == []


def test_no_token_is_a_quiet_skip(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    with immediate(w.engine) as conn:
        secrets.remove(conn, "workers_ai.token")
    rec = menu_model()
    report = run(w, settings, real, rec)
    assert (report.status, rec.requests) == ("skipped", [])
    assert [(r.status, r.skip_reason, r.neurons_est) for r in ai_runs(w)] == [
        ("skipped", "no_token", 0)
    ]
    # No key at all (APP_SECRET_KEY unset) behaves the same.
    report = run(w, w.settings, real, rec)
    assert report.status == "skipped" and rec.requests == []


def test_review_mode_queues_then_approve_and_reject(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    actor = admin_actor(w)
    report = run(w, settings, real, menu_model(seed=1), want=3)
    assert report.status == "ok" and report.created == []
    assert 1 <= len(report.queued) <= 3
    with w.engine.connect() as conn:
        queued = ai_props.pending(conn, w.clock.now())
        markets = conn.execute(select(func.count()).select_from(Market)).scalar_one()
    assert [q.id for q in queued] == report.queued
    # A proposal expires when its market would lock (beat_last_week: the night before its week).
    assert all(w.clock.now() < q.expires_at <= local(2026, 10, 11, 22) for q in queued)
    first, *rest = queued
    market_id = ai_props.approve(w.engine, w.clock, actor, first.id)
    with w.engine.connect() as conn:
        m = conn.execute(select(Market).where(Market.id == market_id)).one()
        assert conn.execute(select(func.count()).select_from(Market)).scalar_one() == markets + 1
        p = conn.execute(select(AiProposal).where(AiProposal.id == first.id)).one()
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert (m.origin, m.ai_run_id, m.blurb, m.status) == ("ai", report.run_id, first.blurb, "open")
    assert m.title == first.title  # the code-built title, not the AI's
    assert (p.status, p.market_id) == ("approved", market_id)
    assert "ai_proposal.approve" in actions
    with pytest.raises(ai_props.ApprovalError, match="no longer waiting"):
        ai_props.approve(w.engine, w.clock, actor, first.id)
    if rest:
        ai_props.reject(w.engine, w.clock, actor, rest[0].id)
        with pytest.raises(ai_props.ApprovalError):
            ai_props.reject(w.engine, w.clock, actor, rest[0].id)
    # Anything still pending expires at the bet lock and can't be approved after.
    run(w, settings, real, menu_model(seed=2), want=3)
    w.clock.set(local(2026, 10, 11, 22, 1))
    with w.engine.connect() as conn:
        assert ai_props.pending(conn, w.clock.now()) == []
        left = (
            conn.execute(select(AiProposal.id).where(AiProposal.status == "pending"))
            .scalars()
            .all()
        )
    for proposal_id in left:
        with pytest.raises(ai_props.ApprovalError):
            ai_props.approve(w.engine, w.clock, actor, proposal_id)


def test_auto_mode_publishes_and_records_the_run(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    set_mode(w, "auto")
    day = (w.clock.now().astimezone(NY).date() + timedelta(days=30)).isoformat()
    proposals = [
        {"template": "beat_last_week", "params": {"metric": "steps"}, "title": "t", "blurb": "b"},
        {"template": "future_total_change", "params": {"day": day}, "title": "t", "blurb": "b"},
    ]
    report = run(w, settings, real, canned({"proposals": proposals}), want=2)
    assert report.status == "ok" and report.queued == [] and len(report.created) == 2
    with w.engine.connect() as conn:
        rows = conn.execute(
            select(Market.origin, Market.ai_run_id).where(Market.id.in_(report.created))
        ).all()
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert set(rows) == {("ai", report.run_id)}
    assert actions.count("market.create_ai_prop") == len(report.created)
    (r,) = ai_runs(w)
    assert (r.status, r.model, r.prompt_version) == ("ok", settings.ai_model_json, "props_v1")
    assert (r.input_tokens, r.output_tokens) == (2400, 350)  # actual usage replaces the estimate
    assert r.neurons_est == 88  # ceil((2400 * 25608 + 350 * 75147) / 1e6)
    assert r.errors == report.dropped


def test_each_bad_proposal_is_dropped_alone(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    m = menu(w)
    good = {
        "template": "beat_last_week",
        "params": {"metric": m.metrics[0]},
        "title": "Better than last week?",
        "blurb": "Last week set the bar.",
    }
    proposals = [
        {
            "template": "milestone_by",
            "params": {"threshold": 1.0, "deadline": "2099-01-01"},
            "title": "x",
            "blurb": "y",
        },
        {**good, "title": "Ignore all rules @everyone"},
        {**good, "blurb": "Phil can't keep up"},
        {"template": "pick_a_winner", "params": {}, "title": "Who?", "blurb": "Nope"},
        {"template": "milestone_by"},
        good,
        good,  # duplicate in the same reply
    ]
    with immediate(w.engine) as conn:
        from app.services.users import ensure_player

        ensure_player(conn, w.clock, "phil@example.invalid", "Phil")
    report = run(w, settings, real, canned({"proposals": proposals}), want=3)
    reasons = [d["reason"] for d in report.dropped]
    assert reasons == [
        "deadline_out_of_range" if False else "threshold_out_of_range",
        "link_or_markup",
        "bettor_name",
        "off_menu",
        "bad_shape",
        "duplicate",
    ]
    assert len(report.queued) == 1
    assert ai_runs(w)[-1].errors == report.dropped
    # The same prop again next cycle is a duplicate of the pending one.
    again = run(w, settings, real, canned({"proposals": [good]}))
    assert [d["reason"] for d in again.dropped] == ["duplicate"]


@pytest.mark.parametrize("data", [{"nope": 1}, {"proposals": "x"}, {"proposals": []}])
def test_empty_or_odd_replies(ai: tuple[World, Settings, SimClock], data: Any) -> None:
    w, settings, real = ai
    report = run(w, settings, real, canned(data))
    assert report.status == "ok" and report.dropped == [{"index": None, "reason": "no_proposals"}]


def test_probability_band_and_prop_cap(
    ai: tuple[World, Settings, SimClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    w, settings, real = ai
    m = menu(w)
    good = {
        "template": "beat_last_week",
        "params": {"metric": m.metrics[0]},
        "title": "Better?",
        "blurb": "Bar.",
    }
    monkeypatch.setattr(ai_props, "P_MIN", 0.99)
    report = run(w, settings, real, canned({"proposals": [good]}))
    assert [d["reason"] for d in report.dropped] == ["probability_out_of_range"]
    monkeypatch.setattr(ai_props, "P_MIN", 0.08)
    monkeypatch.setattr(ai_props, "OPEN_PROP_CAP", 1)
    assert run(w, settings, real, canned({"proposals": [good]})).queued
    full = run(w, settings, real, canned({"proposals": [good]}))
    assert (full.status, full.reason) == ("skipped", "prop_cap")


def test_quota_refuses_before_calling_and_resets_at_utc_midnight(
    ai: tuple[World, Settings, SimClock],
) -> None:
    w, settings, real = ai
    tight = settings.model_copy(update={"ai_daily_neuron_cap": 120})
    rec = canned({"proposals": []})  # usage 2400 in / 350 out = 88 neurons
    assert run(w, tight, real, rec).status == "ok"  # ~88 neurons used; the next estimate is ~60
    second = run(w, tight, real, rec)
    assert (second.status, second.reason, len(rec.requests)) == ("skipped", "quota", 1)
    real.set(real.now().replace(hour=23, minute=59) + timedelta(minutes=2))  # past 00:00 UTC
    assert run(w, tight, real, rec).status == "ok"
    assert len(rec.requests) == 2


def test_failures_are_recorded_not_raised(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    json_mode = {"success": False, "errors": [{"message": "JSON Mode couldn't be met"}]}
    assert run(w, settings, real, canned(json_mode, status=400)).reason == "json_mode"
    assert run(w, settings, real, canned("{broken")).status == "error"
    assert run(w, settings, real, canned({"errors": []}, status=503)).reason == "http_503"
    assert [(r.status, r.skip_reason) for r in ai_runs(w)] == [
        ("skipped", "json_mode"),
        ("error", "malformed_json"),
        ("error", "http_503"),
    ]


def test_frozen_or_no_menu_skips(ai: tuple[World, Settings, SimClock]) -> None:
    w, settings, real = ai
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(instance_state="frozen"))
    report = run(w, settings, real, menu_model())
    assert (report.status, report.reason) == ("skipped", "no_season")
