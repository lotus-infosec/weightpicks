import pytest

from app.domain import setup as s


def test_subject_parses_weights_to_tenths() -> None:
    clean, errors = s.subject(
        {"subject_name": "  Rho  ", "unit": "lb", "start_weight": "212.44", "goal_weight": "185"}
    )
    assert errors == {}
    assert clean == {
        "subject_name": "Rho",
        "unit": "lb",
        "start_weight_x10": 2124,
        "goal_weight_x10": 1850,
        "direction": "down",
    }
    gain, _ = s.subject(
        {"subject_name": "A", "unit": "kg", "start_weight": "60", "goal_weight": "65.05"}
    )
    assert gain["direction"] == "up" and gain["goal_weight_x10"] == 651  # half-up


@pytest.mark.parametrize(
    ("form", "field"),
    [
        (
            {"subject_name": "", "unit": "lb", "start_weight": "200", "goal_weight": "180"},
            "subject_name",
        ),
        ({"subject_name": "A", "unit": "st", "start_weight": "200", "goal_weight": "180"}, "unit"),
        (
            {"subject_name": "A", "unit": "lb", "start_weight": "abc", "goal_weight": "180"},
            "start_weight",
        ),
        (
            {"subject_name": "A", "unit": "lb", "start_weight": "2000", "goal_weight": "180"},
            "start_weight",
        ),
        (
            {"subject_name": "A", "unit": "lb", "start_weight": "NaN", "goal_weight": "180"},
            "start_weight",
        ),
        (
            {"subject_name": "A", "unit": "lb", "start_weight": "200", "goal_weight": "200.0"},
            "goal_weight",
        ),
    ],
)
def test_subject_errors(form: dict[str, str], field: str) -> None:
    clean, errors = s.subject(form)
    assert clean == {} and field in errors


def schedule_form(**kw: str) -> dict[str, str]:
    return {
        "timezone": "America/Chicago",
        "daily_drop": "11:00",
        "bet_lock": "22:00",
        "weekly_drop_weekday": "6",
        "weekly_drop": "18:00",
        "monthly_drop": "18:00",
    } | kw


def test_schedule() -> None:
    clean, errors = s.schedule(schedule_form())
    assert errors == {} and clean["timezone"] == "America/Chicago" and clean["bet_lock"] == "22:00"
    assert "timezone" in s.schedule(schedule_form(timezone="Mars/Olympus"))[1]
    assert "daily_drop" in s.schedule(schedule_form(daily_drop="noon"))[1]
    assert "weekly_drop_weekday" in s.schedule(schedule_form(weekly_drop_weekday="9"))[1]
    assert "schedule" in s.schedule(schedule_form(daily_drop="23:00"))[1]  # after the lock


def economy_form(**kw: str) -> dict[str, str]:
    return {
        "starting_bankroll": "1,000",
        "daily_allowance": "50",
        "bailout": "500",
        "bailout_cooldown_days": "2",
        "vig": "-110",
        "max_bet": "",
        "max_parlay_legs": "6",
        "high_roller": "$500.00",
    } | kw


def test_economy() -> None:
    clean, errors = s.economy(economy_form())
    assert errors == {}
    assert clean["starting_bankroll_cents"] == 100_000 and clean["hold"] == "1/22"
    assert clean["max_bet_cents"] is None
    assert s.economy(economy_form(max_bet="250"))[0]["max_bet_cents"] == 25_000
    assert "daily_allowance" in s.economy(economy_form(daily_allowance="1.005"))[1]
    assert "vig" in s.economy(economy_form(vig="-150"))[1]
    assert "max_bet" in s.economy(economy_form(max_bet="lots"))[1]
    assert "economy" in s.economy(economy_form(max_parlay_legs="20"))[1]
    assert "bailout_cooldown_days" in s.economy(economy_form(bailout_cooldown_days="x"))[1]


def test_stats_appearance_smtp_and_webhooks() -> None:
    assert s.stats({"metric_steps": "on", "metric_kcal": "on", "metric_sleep": "on"})[0] == {
        "enabled_metrics": ["steps", "kcal"]
    }
    assert s.appearance({"app_name": " Rho  Picks ", "palette": "grove"})[0] == {
        "app_name": "Rho Picks",
        "palette": "grove",
    }
    assert set(s.appearance({"app_name": "", "palette": "neon"})[1]) == {"app_name", "palette"}
    assert s.smtp({"host": ""})[0] == {"configured": False}
    ok, errs = s.smtp(
        {"host": "smtp.example.invalid", "port": "587", "from_address": "a@example.invalid"}
    )
    assert errs == {} and ok["port"] == 587
    assert set(s.smtp({"host": "h", "port": "0", "from_address": "x"})[1]) == {
        "port",
        "from_address",
    }
    assert s.webhook_problem("") is None
    assert s.webhook_problem("https://discord.com/api/webhooks/123/abc-DEF_9") is None
    assert s.webhook_problem("http://discord.com/api/webhooks/1/a") is not None
    assert s.webhook_problem("https://evil.example/api/webhooks/1/a") is not None


def test_step_lookup_and_missing() -> None:
    assert s.step(1).key == "admin" and s.step(12).key == "review"
    with pytest.raises(KeyError):
        s.step(13)
    assert [m.key for m in s.missing_steps({"admin": {}, "subject": {}})] == [
        "schedule",
        "economy",
        "stats",
        "registration",
        "appearance",
    ]
    assert s.money_text(100_050) == "1000.50" and s.money_text(None) == ""
