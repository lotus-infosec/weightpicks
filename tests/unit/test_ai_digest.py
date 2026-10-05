"""The code-built digest and menu (BUILD_PLAN §1.4.4 step 2)."""

import json
from datetime import date, timedelta

from app.ai.digest import Menu, Snapshot, build, crowd_storylines, streak

TODAY = date(2026, 10, 14)


def falling(days: int = 30, start: float = 213.0, per_day: float = -0.15) -> dict[date, int]:
    return {
        TODAY - timedelta(days=k): round((start + per_day * (days - k)) * 10)
        for k in range(days, -1, -1)
    }


def steps(days: int = 28, value: int = 9000, yesterday: int | None = None) -> dict[date, int]:
    out = {TODAY - timedelta(days=k): value + (k % 3) * 300 for k in range(1, days + 1)}
    if yesterday is not None:
        out[TODAY - timedelta(days=1)] = yesterday
    return out


def snap(**kw: object) -> Snapshot:
    base: dict[str, object] = {
        "today": TODAY,
        "unit": "lb",
        "direction": "down",
        "weigh_ins": falling(),
        "daily_totals": {"steps": steps()},
        "metrics": ("steps", "kcal"),
    }
    return Snapshot(**(base | kw))  # type: ignore[arg-type]


def test_streaks() -> None:
    w = falling(5)
    assert streak(w, TODAY, "weigh_in") == 6
    assert streak(w, TODAY, "down") == 5  # the first day has no day before it
    del w[TODAY - timedelta(days=2)]
    assert streak(w, TODAY, "weigh_in") == 2
    w[TODAY] = w[TODAY - timedelta(days=1)]  # equal is not lower
    assert streak(w, TODAY, "down") == 0
    assert streak({}, TODAY, "weigh_in") == 0


def test_menu_ranges_follow_the_data() -> None:
    d = build(snap())
    m = d.menu
    assert m.templates == (
        "milestone_by",
        "streak_reaches",
        "beat_last_week",
        "future_total_change",
    )
    level = d.data["weight"]["latest"]
    assert all(t < level for t in m.thresholds) and len(m.thresholds) == 3
    assert m.thresholds == tuple(sorted(m.thresholds, reverse=True))
    assert m.deadlines == (TODAY + timedelta(2), TODAY + timedelta(10))
    assert m.future_days == (TODAY + timedelta(14), TODAY + timedelta(60))
    assert m.metrics == ("steps",)  # kcal has no history
    assert m.streak_n["weigh_in"] == (33, 38)  # 31-day streak + 2 ... + 7
    assert set(json.loads(json.dumps(d.payload()))["menu"]) == set(m.templates)


def test_up_direction_offers_thresholds_above() -> None:
    d = build(snap(direction="up", weigh_ins=falling(per_day=0.15)))
    assert all(t > d.data["weight"]["latest"] for t in d.menu.thresholds)


def test_no_recent_weigh_ins_means_metric_props_only() -> None:
    old = {TODAY - timedelta(days=k): 2100 for k in range(20, 40)}
    m = build(snap(weigh_ins=old)).menu
    assert m.templates == ("beat_last_week",)
    assert "weight" not in build(snap(weigh_ins={})).data


def test_provisional_fit_offers_no_weight_props() -> None:
    few = {TODAY - timedelta(days=k): 2100 for k in range(3)}
    m = build(snap(weigh_ins=few)).menu
    assert "milestone_by" not in m.templates and "streak_reaches" not in m.templates


def test_down_streak_needs_todays_weigh_in() -> None:
    w = falling()
    del w[TODAY]
    assert build(snap(weigh_ins=w)).menu.streak_kinds == ("weigh_in",)


def test_triggers() -> None:
    near = {TODAY - timedelta(days=k): 2112 - k for k in range(20)}  # 211.2, 1.2 from 210
    d = build(snap(weigh_ins=near, daily_totals={"steps": steps(yesterday=16000)}))
    text = " | ".join(d.triggers)
    assert "within 1.2 lb of 210" in text
    assert "20-day weigh-in streak" in text
    assert "steps" in text and "above normal" in text
    calm = build(snap(weigh_ins={TODAY: 2134, TODAY - timedelta(1): 2140}))
    assert calm.triggers == ()


def test_notes_and_storylines_pass_through() -> None:
    d = build(snap(notes=("Traveling Thu-Sun",), storylines=("The crowd is 70% on Under",)))
    assert d.data["admin_notes"] == ["Traveling Thu-Sun"]
    assert d.data["storylines"] == ["The crowd is 70% on Under"]


def test_menu_form_accepts_only_menu_values() -> None:
    m = build(snap()).menu
    t = m.thresholds[0]
    ok = m.form("milestone_by", {"threshold": t, "deadline": (TODAY + timedelta(5)).isoformat()})
    assert ok == {"threshold": f"{t:g}", "deadline": (TODAY + timedelta(5)).isoformat()}
    bad = {
        ("pick_winner", "{}"): "off_menu",
        ("milestone_by", '{"threshold": 150, "deadline": "2026-10-19"}'): "threshold_out_of_range",
        (
            "milestone_by",
            f'{{"threshold": {t}, "deadline": "2026-10-15"}}',
        ): "deadline_out_of_range",
        ("milestone_by", f'{{"threshold": {t}, "deadline": 5}}'): "deadline_out_of_range",
        ("milestone_by", f'{{"threshold": {t}}}'): "bad_params",
        ("milestone_by", f'{{"threshold": {t}, "deadline": "soon"}}'): "bad_params",
        ("streak_reaches", '{"kind": "up", "n": 5, "deadline": "2026-10-20"}'): "kind_out_of_range",
        (
            "streak_reaches",
            '{"kind": "weigh_in", "n": 3, "deadline": "2026-10-20"}',
        ): "n_out_of_range",
        ("streak_reaches", '{"kind": "weigh_in", "n": true, "deadline": "2026-10-20"}'): (
            "n_out_of_range"
        ),
        ("streak_reaches", '{"kind": "weigh_in", "n": 34, "deadline": "2026-11-20"}'): (
            "deadline_out_of_range"
        ),
        ("beat_last_week", '{"metric": "kcal"}'): "metric_out_of_range",
        ("future_total_change", '{"day": "2026-10-20"}'): "day_out_of_range",
    }
    for (template, params), reason in bad.items():
        assert m.form(template, json.loads(params)) == reason, (template, params)
    assert m.form("streak_reaches", {"kind": "weigh_in", "n": 34, "deadline": "2026-10-20"}) == {
        "kind": "weigh_in",
        "n": "34",
        "deadline": "2026-10-20",
    }
    assert m.form("beat_last_week", {"metric": "steps"}) == {"metric": "steps"}
    assert m.form("future_total_change", {"day": "2026-11-20"}) == {"day": "2026-11-20"}
    assert Menu(today=TODAY).templates == ()


def test_storylines_are_nameless() -> None:
    lines = crowd_storylines(
        [("Weight tonight", 7000, 3000), ("Steps", 500, 500), ("Kcal", 0, 0)],
        [("lost", 5), ("won", 2)],
    )
    assert lines == (
        "The crowd is 70% on Over/Yes for 'Weight tonight'.",
        "One bettor has lost 5 bets in a row.",
    )
    assert crowd_storylines([], [("won", 2)]) == ()


def test_candidates_and_priced_options() -> None:
    m = build(snap()).menu
    milestones = m.candidates("milestone_by")
    assert len(milestones) == 3 * 4  # thresholds x {2, 4, 7, 10} days
    assert milestones[0] == {"threshold": f"{m.thresholds[0]:g}", "deadline": "2026-10-16"}
    streaks = m.candidates("streak_reaches")
    assert {"kind": "weigh_in", "n": "33", "deadline": "2026-10-16"} in streaks  # 31 + 2 days
    assert {"kind": "weigh_in", "n": "33", "deadline": "2026-10-18"} in streaks  # 2 days slack
    assert all(m.form("streak_reaches", {**c, "n": int(c["n"])}) == c for c in streaks)
    assert Menu(today=TODAY).candidates("milestone_by") == []

    priced = m.with_options(milestones[:1], [])
    assert priced.templates == ("milestone_by", "beat_last_week", "future_total_change")
    one = milestones[0]
    params = {"threshold": float(one["threshold"]), "deadline": one["deadline"]}
    assert priced.form("milestone_by", params) == one
    other = {"threshold": float(milestones[1]["threshold"]), "deadline": milestones[1]["deadline"]}
    assert priced.form("milestone_by", other) == "not_an_option"
    assert priced.form(
        "streak_reaches", {"kind": "weigh_in", "n": 33, "deadline": "2026-10-16"}
    ) == ("off_menu")
    assert priced.as_json()["milestone_by"] == {"options": [params]}
    with_streak = m.with_options([], streaks[:1])
    s = streaks[0]
    assert with_streak.as_json()["streak_reaches"] == {
        "options": [{"kind": s["kind"], "n": int(s["n"]), "deadline": s["deadline"]}]
    }
    assert with_streak.form("streak_reaches", {**s, "n": int(s["n"])}) == s
    assert with_streak.form("streak_reaches", {**streaks[1], "n": int(streaks[1]["n"])}) == (
        "not_an_option"
    )
