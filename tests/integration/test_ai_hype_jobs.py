"""Stat-update hype (flag-gated, template fallback) and the AI jobs (D-042)."""

from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.models import AiRun, Command, InstanceSettingsRow, JobRun, OutboxMessage
from app.notify import embeds
from app.services import ai_hype, instance, secrets
from app.worker.jobs.ai import AiHypeJob, AiPropsJob
from app.worker.jobs.commands import CommandsJob
from app.worker.registry import run_due
from tests.fake_ai import Recorder, canned, menu_model
from tests.integration.world import World, local, sync_sim

KEY = "k" * 40


def flags(w: World, **values: bool) -> None:
    with immediate(w.engine) as conn:
        current = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(current) | values))


def keyed(w: World, token: bool = True) -> Settings:
    settings = w.settings.model_copy(update={"app_secret_key": SecretStr(KEY)})
    if token:
        with immediate(w.engine) as conn:
            secrets.put(conn, w.clock, KEY, "workers_ai.account_id", "acct")
            secrets.put(conn, w.clock, KEY, "workers_ai.token", "tok")
    return settings


@pytest.fixture
def eventful(world: World) -> World:
    """Oct 11, 12:00: a new season low that also crosses under 220 lb."""
    world.clock.set(local(2026, 10, 11, 12))
    sync_sim(world.engine, world.clock)
    return world


def hype_rows(w: World) -> list[tuple[str, dict[str, object]]]:
    with w.engine.connect() as conn:
        return [
            (r.dedupe_key, r.payload)
            for r in conn.execute(
                select(OutboxMessage.dedupe_key, OutboxMessage.payload)
                .where(OutboxMessage.category == "hype")
                .order_by(OutboxMessage.id)
            )
        ]


def hype(w: World, settings: Settings, rec: Recorder | None = None) -> int:
    real = SimClock(local(2026, 10, 11, 12))
    return ai_hype.run(w.engine, w.clock, real, settings, transport=rec.transport if rec else None)


def test_hype_is_off_without_the_flag(eventful: World) -> None:
    w = eventful
    rec = menu_model()
    assert hype(w, keyed(w), rec) == 0
    assert hype_rows(w) == [] and rec.requests == []


def test_no_token_posts_the_template(eventful: World) -> None:
    w = eventful
    flags(w, ai_hype=True)
    assert hype(w, keyed(w, token=False)) == 2
    rows = hype_rows(w)
    assert [k for k, _ in rows] == ["hype:2026-10-11:season_best", "hype:2026-10-11:milestone_220"]
    assert rows[0][1] == {
        "title": "Stat update",
        "text": "New season low: 219.4 lb today.",
        "ai": False,
        "event": "season_best",
    }
    with w.engine.connect() as conn:
        assert [(r.kind, r.skip_reason) for r in conn.execute(select(AiRun))] == [
            ("hype", "no_token")
        ]
    assert hype(w, keyed(w, token=False)) == 0  # one post per event per day


def test_ai_rewrite_keeps_the_numbers(eventful: World) -> None:
    w = eventful
    flags(w, ai_hype=True)
    settings = keyed(w)
    rec = menu_model()
    assert hype(w, settings, rec) == 2
    texts = [p["text"] for _, p in hype_rows(w)]
    assert texts[0] == "Big news: New season low: 219.4 lb today."
    assert all(p["ai"] for _, p in hype_rows(w))
    body = rec.body(0)
    assert body["messages"][-1]["content"] == "New season low: 219.4 lb today."
    assert "response_format" not in body
    assert rec.requests[0].url.path.endswith(settings.ai_model_text)
    with w.engine.connect() as conn:
        runs = conn.execute(select(AiRun.kind, AiRun.status, AiRun.model)).all()
    assert runs == [("hype", "ok", settings.ai_model_text)] * 2


@pytest.mark.parametrize(
    "reply",
    [
        "Wow, a new low today!",  # dropped the number
        "New season low: 219.4 lb today @everyone",
        "x" * 300,
    ],
)
def test_bad_rewrites_fall_back_to_the_template(eventful: World, reply: str) -> None:
    w = eventful
    flags(w, ai_hype=True)
    assert hype(w, keyed(w), canned(reply)) == 2
    assert [p["ai"] for _, p in hype_rows(w)] == [False, False]
    assert hype_rows(w)[0][1]["text"] == "New season low: 219.4 lb today."
    with w.engine.connect() as conn:
        assert conn.execute(select(AiRun.errors)).scalars().first() == [
            {"reason": "rewrite_rejected"}
        ]


def test_ai_failure_falls_back_to_the_template(eventful: World) -> None:
    w = eventful
    flags(w, ai_hype=True)
    assert hype(w, keyed(w), canned({"errors": []}, status=500)) == 2
    assert [p["ai"] for _, p in hype_rows(w)] == [False, False]


def test_hype_events_are_capped(eventful: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w = eventful
    flags(w, ai_hype=True)
    many = [ai_hype.Event(f"e{i}", f"Event {i}.") for i in range(6)]
    monkeypatch.setattr(ai_hype, "events", lambda *a: many[: ai_hype.MAX_EVENTS])
    assert hype(w, keyed(w, token=False)) == 4


def test_hype_embed() -> None:
    ctx = embeds.Context("WP", "https://wp.example", local(2026, 10, 11, 12), str, str)
    out = embeds.build("hype", {"title": "Stat update", "text": "Under 220 @everyone"}, ctx)
    embed = out["embeds"][0]
    assert embed["title"] == "Stat update"
    assert "@everyone" not in embed["description"]
    assert out["allowed_mentions"] == {"parse": []}


# ---- jobs ----------------------------------------------------------------------------------


def jobs(w: World, settings: Settings) -> list[object]:
    with w.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    real = SimClock(local(2026, 10, 11, 12))
    return [
        AiPropsJob("props_daily", settings, config, real),
        AiPropsJob("props_weekly", settings, config, real),
        AiHypeJob(settings, config, real),
    ]


def claimed(w: World) -> list[tuple[str, str]]:
    with w.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(select(JobRun.job, JobRun.period_key))]  # type: ignore[misc]


def test_jobs_do_nothing_while_flags_are_off(world: World) -> None:
    run_due(jobs(world, world.settings), world.engine, world.clock)  # type: ignore[arg-type]
    assert claimed(world) == []


def test_jobs_run_once_per_period_and_skip_after_the_lock(world: World) -> None:
    w = world
    flags(w, ai_props=True, props_futures=True, ai_hype=True)
    settings = keyed(w, token=False)
    run_due(jobs(w, settings), w.engine, w.clock)  # type: ignore[arg-type]
    run_due(jobs(w, settings), w.engine, w.clock)  # type: ignore[arg-type]
    assert sorted(claimed(w)) == [
        ("ai_hype", "2026-10-05"),
        ("ai_props_daily", "2026-10-05"),
        ("ai_props_weekly", "2026-10-04"),  # last Sunday's drop: catch-up is skipped
    ]
    with w.engine.connect() as conn:
        kinds = conn.execute(select(AiRun.kind, AiRun.skip_reason)).all()
    assert kinds == [("props_daily", "no_token")]  # weekly was a catch-up; no hype events
    # Restarting after tonight's lock: tomorrow is a new period, but tonight's is skipped.
    w.clock.set(local(2026, 10, 6, 22, 30))
    run_due(jobs(w, settings), w.engine, w.clock)  # type: ignore[arg-type]
    with w.engine.connect() as conn:
        assert len(conn.execute(select(AiRun.id)).all()) == 1


def test_run_now_command(world: World) -> None:
    w = world
    flags(w, ai_props=True, props_futures=True)
    settings = keyed(w, token=False)
    with immediate(w.engine) as conn:
        conn.execute(
            insert(Command).values(
                type="ai_props_now", args={}, status="pending", created_at=w.clock.now()
            )
        )
    job = CommandsJob(settings, w.clock)
    run_due([job], w.engine, SimClock(w.clock.now() + timedelta(seconds=1)))
    with w.engine.connect() as conn:
        status, result = conn.execute(select(Command.status, Command.result)).one()
        kind = conn.execute(select(AiRun.kind)).scalar_one()
    assert status == "done" and result["ai_status"] == "skipped"
    assert result["ai_reason"] == "no_token" and kind == "props_manual"
