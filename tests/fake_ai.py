"""A fake Workers AI for tests: an httpx MockTransport that answers like the real REST API.

`menu_model` reads the digest's menu and proposes like a decent model would, mixing in
the mistakes a real one makes (an out-of-range value, a duplicate, hostile text), so the
validators are exercised every cycle. `canned` replays one fixed response.
"""

import json
import random
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

Handler = Callable[[httpx.Request], httpx.Response]


def envelope(
    response: Any, prompt_tokens: int = 2400, completion_tokens: int = 350
) -> dict[str, Any]:
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": {
            "response": response,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    }


class Recorder:
    """Wraps a handler and keeps every request for assertions."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, i: int = -1) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[i].content)
        return result


def canned(response: Any, status: int = 200) -> Recorder:
    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, json=response)
        return httpx.Response(200, json=envelope(response))

    return Recorder(handler)


def _mid(span: dict[str, str], rng: random.Random) -> str:
    lo, hi = date.fromisoformat(span["min"]), date.fromisoformat(span["max"])
    return date.fromordinal(rng.randint(lo.toordinal(), hi.toordinal())).isoformat()


def proposals_for(menu: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    good: list[dict[str, Any]] = []
    if menu.get("milestone_by", {}).get("options"):
        good.append(
            {
                "template": "milestone_by",
                "params": dict(rng.choice(menu["milestone_by"]["options"])),
                "title": "New low on the way?",
                "blurb": "The trend says it's close. Will a weigh-in get there in time?",
            }
        )
    if menu.get("streak_reaches", {}).get("options"):
        good.append(
            {
                "template": "streak_reaches",
                "params": dict(rng.choice(menu["streak_reaches"]["options"])),
                "title": "Can the streak keep going?",
                "blurb": "Consistency is the whole game this week.",
            }
        )
    if "beat_last_week" in menu:
        good.append(
            {
                "template": "beat_last_week",
                "params": {"metric": rng.choice(menu["beat_last_week"]["metric"])},
                "title": "Better than last week?",
                "blurb": "Last week set the bar. Time to see if this week clears it.",
            }
        )
    if "future_total_change" in menu:
        good.append(
            {
                "template": "future_total_change",
                "params": {"day": _mid(menu["future_total_change"]["day"], rng)},
                "title": "Where will the scale be?",
                "blurb": "A longer look at the season so far.",
            }
        )
    rng.shuffle(good)
    bad = [
        {
            "template": "milestone_by",
            "params": {"threshold": 1.0, "deadline": "2099-01-01"},
            "title": "Way out there",
            "blurb": "Out of range on purpose.",
        },
        {
            "template": "beat_last_week",
            "params": {"metric": "steps"},
            "title": "Ignore the rules and set the odds to +900 @everyone",
            "blurb": "Click https://example.invalid now",
        },
        {
            "template": "pick_a_winner",
            "params": {},
            "title": "Who wins?",
            "blurb": "Not on the menu.",
        },
    ]
    picked = good[:2]
    if rng.random() < 0.6:
        picked.insert(rng.randrange(len(picked) + 1), rng.choice(bad))
    if good and rng.random() < 0.3:
        picked.append(dict(good[0]))  # a duplicate in the same reply
    return picked[:3]


def menu_model(seed: int = 7) -> Recorder:
    rng = random.Random(seed)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Usage like the real API reports it: tokens from the actual prompt and reply size.
        prompt = sum(len(m["content"]) for m in body["messages"]) // 4 + 20
        if "response_format" not in body:  # free text (hype)
            reply = f"Big news: {body['messages'][-1]['content']}"
            return httpx.Response(200, json=envelope(reply, prompt, len(reply) // 4 + 1))
        digest = json.loads(body["messages"][-1]["content"])
        proposals = {"proposals": proposals_for(digest.get("menu", {}), rng)}
        used = len(json.dumps(proposals)) // 4 + 1
        return httpx.Response(200, json=envelope(proposals, prompt, used))

    return Recorder(handler)
