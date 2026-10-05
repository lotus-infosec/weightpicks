from datetime import timedelta
from fractions import Fraction
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select

from app.cli import main
from app.core.clock import SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate
from app.models import (
    Account,
    InstanceSettingsRow,
    RegistrationCode,
    Season,
    Secret,
    SetupDraft,
    SetupToken,
    User,
)
from app.services import instance, secrets, setup
from app.services.setup import SetupError
from app.web.main import create_app
from tests.integration import web

KEY = "k" * 40
WEBHOOK = "https://discord.com/api/webhooks/123456/abcDEF-ghi_789"
STEP_FORMS: dict[int, dict[str, str]] = {
    1: {
        "email": "Admin@Example.invalid",
        "display_name": "Rho",
        "password": web.PASSWORD,
        "confirm": web.PASSWORD,
    },
    2: {"subject_name": "Rho", "unit": "lb", "start_weight": "212.4", "goal_weight": "185"},
    3: {
        "timezone": "America/Chicago",
        "daily_drop": "11:00",
        "bet_lock": "21:30",
        "weekly_drop_weekday": "6",
        "weekly_drop": "18:00",
        "monthly_drop": "18:00",
    },
    4: {
        "starting_bankroll": "2000",
        "daily_allowance": "25",
        "bailout": "500",
        "bailout_cooldown_days": "2",
        "vig": "-120",
        "max_bet": "300",
        "max_parlay_legs": "6",
        "high_roller": "250",
    },
    5: {"metric_steps": "on", "metric_workouts": "on"},
    6: {"ack": "1"},
    7: {"account_id": "f" * 32, "token": "fake" * 6},
    8: {"webhook_bets_placed": WEBHOOK},
    9: {"host": ""},
    10: {},
    11: {"app_name": "Rho Picks", "palette": "lagoon"},
}


@pytest.fixture
def keyed(settings: Settings) -> Settings:
    return settings.model_copy(update={"app_secret_key": SecretStr(KEY)})


def start(c: TestClient, engine: Engine) -> None:
    token = setup.issue_token(engine, SystemClock())
    csrf = web.form_csrf(c, "/setup")
    response = c.post(
        "/setup/token", data={"csrf_token": csrf, "token": token}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def post_step(c: TestClient, number: int, form: dict[str, str], **extra: str) -> Any:
    csrf = web.form_csrf(c, f"/setup/step/{number}")
    return c.post(
        f"/setup/step/{number}", data={"csrf_token": csrf, **form, **extra}, follow_redirects=False
    )


def run_wizard(c: TestClient, engine: Engine) -> Any:
    start(c, engine)
    for number, form in STEP_FORMS.items():
        response = post_step(c, number, form)
        assert response.status_code == 303, (number, response.text[:500])
    csrf = web.form_csrf(c, "/setup/step/12")
    return c.post("/setup/finish", data={"csrf_token": csrf}, follow_redirects=False)


# ---- gate and token --------------------------------------------------------------------------


def test_fresh_instance_redirects_everything_to_setup(
    keyed: Settings, migrated_engine: Engine
) -> None:
    with web.client(create_app(keyed)) as c:
        for path in ("/", "/login", "/register", "/bets/feed", "/admin"):
            response = c.get(path, follow_redirects=False)
            assert (response.status_code, response.headers["location"]) == (303, "/setup"), path
        assert c.get("/healthz").status_code == 200
        assert c.get("/robots.txt").status_code == 200
        page = c.get("/setup")
        assert "SETUP TOKEN" in page.text and 'name="token"' in page.text
        web_post = c.post(
            "/api/auth/login",
            data={"csrf_token": web.form_csrf(c, "/setup")},
            follow_redirects=False,
        )
        assert web_post.headers["location"] == "/setup"


def test_startup_issues_a_hash_only_token(
    keyed: Settings, migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with web.client(create_app(keyed)):
        pass
    with migrated_engine.connect() as conn:
        stored = conn.execute(select(SetupToken.token_hash, SetupToken.expires_at)).all()
    assert len(stored) == 1 and len(stored[0][0]) == 64
    # A second start doesn't replace a live token.
    with web.client(create_app(keyed)):
        pass
    with migrated_engine.connect() as conn:
        assert conn.execute(select(SetupToken.token_hash)).scalars().all() == [stored[0][0]]


def test_cli_setup_token(
    keyed: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(keyed.data_dir))
    assert main(["setup-token"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SETUP TOKEN: ")
    token = out.split()[2]
    with migrated_engine.connect() as conn:
        hashes = conn.execute(select(SetupToken.token_hash)).scalars().all()
    assert len(hashes) == 1 and token not in hashes[0]


def test_wrong_token_is_rate_limited(keyed: Settings, migrated_engine: Engine) -> None:
    setup.issue_token(migrated_engine, SystemClock())
    with web.client(create_app(keyed)) as c:
        csrf = web.form_csrf(c, "/setup")
        for _ in range(5):
            bad = c.post("/setup/token", data={"csrf_token": csrf, "token": "nope"})
            assert bad.status_code == 400 and "isn&#39;t valid" in bad.text
        token = setup.issue_token(migrated_engine, SystemClock())
        limited = c.post("/setup/token", data={"csrf_token": csrf, "token": token})
        assert limited.status_code == 429


def test_expired_token_and_session(migrated_engine: Engine) -> None:
    clock = SimClock(SystemClock().now())
    token = setup.issue_token(migrated_engine, clock)
    clock.advance(timedelta(hours=24, seconds=1))
    assert setup.token_needed(migrated_engine, clock)
    with pytest.raises(SetupError, match="expired"):
        setup.start_session(migrated_engine, clock, token=token, ip="1.2.3.4")
    token = setup.issue_token(migrated_engine, clock)
    cookie = setup.start_session(migrated_engine, clock, token=token, ip="1.2.3.4")
    assert setup.resolve(migrated_engine, clock, cookie) is not None
    clock.advance(timedelta(hours=2, seconds=1))
    assert setup.resolve(migrated_engine, clock, cookie) is None
    assert setup.resolve(migrated_engine, clock, None) is None


# ---- the wizard ----------------------------------------------------------------------------------


def test_full_wizard_creates_the_instance(keyed: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(keyed)) as c:
        done = run_wizard(c, migrated_engine)
        assert (done.status_code, done.headers["location"]) == (303, "/login?welcome=1")
        assert "Setup is complete" in c.get("/login?welcome=1").text
        assert c.get("/setup").status_code == 404
        assert c.get("/setup/step/3").status_code == 404
        assert c.post("/setup/token", data={"token": "x"}).status_code == 404
        web.log_in(c, "admin@example.invalid")
        admin_page = c.get("/admin")
        assert admin_page.status_code == 200
        assert "Rho Picks" in admin_page.text and 'class="palette-lagoon"' in admin_page.text

    with migrated_engine.connect() as conn:
        admin = conn.execute(select(User).where(User.role == "admin")).one()
        season = conn.execute(select(Season)).one()
        row = conn.execute(select(InstanceSettingsRow)).one()
        config = instance.read(conn)
        assert secrets.get(conn, KEY, "webhook.bets_placed") == WEBHOOK
        assert secrets.get(conn, KEY, "workers_ai.token") == "fake" * 6
        stored = conn.execute(select(Secret.ciphertext)).scalars().all()
        accounts = (
            conn.execute(select(Account.kind).where(Account.season_id == season.id)).scalars().all()
        )
        assert conn.execute(select(func.count()).select_from(SetupToken)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(SetupDraft)).scalar_one() == 0
        assert conn.execute(select(RegistrationCode.active)).scalars().all() == [True]
    assert (admin.email, admin.display_name) == ("admin@example.invalid", "Rho")
    assert (season.number, season.start_weight_x10, season.goal_weight_x10, season.direction) == (
        1,
        2124,
        1850,
        "down",
    )
    assert sorted(accounts) == ["house", "mint"]
    assert row.setup_completed_at is not None and row.subject_name == "Rho"
    assert config is not None and config.setup_completed
    assert (config.timezone, config.unit, config.app_name, config.palette) == (
        "America/Chicago",
        "lb",
        "Rho Picks",
        "lagoon",
    )
    assert config.schedule.bet_lock.hour == 21 and config.schedule.bet_lock.minute == 30
    assert config.enabled_metrics == ("steps", "workouts")
    assert config.economy.hold == Fraction(1, 12) and config.economy.max_bet_cents == 30_000
    assert config.economy.starting_bankroll_cents == 200_000
    assert all("discord" not in c and "ai-token" not in c for c in stored)


def test_new_players_get_the_configured_bankroll(keyed: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(keyed)) as c:
        run_wizard(c, migrated_engine)
    from app.services import auth

    code = auth.rotate_registration_code(migrated_engine, SystemClock())
    with web.client(create_app(keyed)) as c:
        web.register(c, code)
        assert "$2,000.00" in c.get("/").text


def test_refresh_and_new_session_keep_progress(keyed: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(keyed)) as c:
        start(c, migrated_engine)
        post_step(c, 1, STEP_FORMS[1])
        post_step(c, 2, STEP_FORMS[2])
        page = c.get("/setup/step/2")
        assert 'value="212.4"' in page.text and 'value="Rho"' in page.text
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/setup/step/3"
    with web.client(create_app(keyed)) as other:  # a new browser, same token holder
        start(other, migrated_engine)
        assert other.get("/setup", follow_redirects=False).headers["location"] == "/setup/step/3"
        page = other.get("/setup/step/1")
        assert 'value="admin@example.invalid"' in page.text
        assert web.PASSWORD not in page.text
        # Leaving the password empty keeps the saved one.
        kept = post_step(other, 1, {"email": "admin@example.invalid", "display_name": "Rho R."})
        assert kept.status_code == 303
    with migrated_engine.connect() as conn:
        data = conn.execute(select(SetupDraft.data)).scalar_one()
    assert data["admin"]["display_name"] == "Rho R." and data["admin"]["password_hash"].startswith(
        "$argon2id$"
    )
    assert web.PASSWORD not in str(data)


def test_step_errors_are_shown_and_not_saved(keyed: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(keyed)) as c:
        start(c, migrated_engine)
        bad = post_step(c, 2, STEP_FORMS[2] | {"goal_weight": "212.4"})
        assert bad.status_code == 400 and "must differ" in bad.text
        bad_hook = post_step(c, 8, {"webhook_busts": "https://example.invalid/hook"})
        assert bad_hook.status_code == 400 and "Discord webhook URL" in bad_hook.text
        mismatch = post_step(c, 1, STEP_FORMS[1] | {"confirm": "something else!"})
        assert "don&#39;t match" in mismatch.text
        review = c.get("/setup/step/12")
        assert "Finish these steps first" in review.text
        early = c.post("/setup/finish", data={"csrf_token": web.form_csrf(c, "/setup/step/12")})
        assert "Finish these steps first" in early.text
    with migrated_engine.connect() as conn:
        data = conn.execute(select(SetupDraft.data)).scalar_one()
        assert data == {}  # nothing invalid was saved
        assert conn.execute(select(func.count()).select_from(User)).scalar_one() == 0


def test_secrets_need_the_app_secret_key(settings: Settings, migrated_engine: Engine) -> None:
    assert settings.app_secret_key.get_secret_value() == ""
    with web.client(create_app(settings)) as c:
        start(c, migrated_engine)
        page = c.get("/setup/step/7")
        assert "APP_SECRET_KEY isn't set" in page.text
        refused = post_step(c, 8, {"webhook_bets_placed": WEBHOOK})
        assert refused.status_code == 400 and "APP_SECRET_KEY" in refused.text
        assert post_step(c, 7, {}).status_code == 303  # skipping is fine


def test_regenerate_registration_code(keyed: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(keyed)) as c:
        start(c, migrated_engine)
        first = c.get("/setup/step/10").text
        post_step(c, 10, {}, action="regenerate")
        second = c.get("/setup/step/10").text

    def code(html: str) -> str:
        return html.split('data-testid="registration-code">')[1].split("<")[0]

    assert code(first) != code(second) and len(code(second)) == 14


def test_finish_refuses_a_database_with_data(keyed: Settings, migrated_engine: Engine) -> None:
    from app.services import ledger

    with web.client(create_app(keyed)) as c:
        start(c, migrated_engine)
        for number, form in STEP_FORMS.items():
            post_step(c, number, form)
        with immediate(migrated_engine) as conn:
            ledger.open_season(conn, SystemClock())
        failed = c.post("/setup/finish", data={"csrf_token": web.form_csrf(c, "/setup/step/12")})
        assert failed.status_code == 400 and "already has data" in failed.text
    with migrated_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(User)).scalar_one() == 0  # rolled back


def test_existing_admin_counts_as_set_up(keyed: Settings, migrated_engine: Engine) -> None:
    from app.services import auth

    auth.create_admin(
        migrated_engine,
        SystemClock(),
        email="a@example.invalid",
        display_name="A",
        password=web.PASSWORD,
    )
    with migrated_engine.connect() as conn:
        assert setup.is_complete(conn)
    with pytest.raises(SetupError, match="already complete"):
        setup.issue_token(migrated_engine, SystemClock())
    with web.client(create_app(keyed)) as c:
        assert c.get("/setup").status_code == 404
        assert c.get("/login").status_code == 200
