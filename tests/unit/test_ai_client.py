"""Workers AI client against recorded responses."""

import json
import logging

import httpx
import pytest

from app.ai import quota
from app.ai.client import AiError, AiSkip, WorkersAI
from app.ai.text import problem
from tests.fake_ai import Recorder, canned, envelope

MODEL = "@cf/meta/llama-3.1-8b-instruct"
TOKEN = "fake" * 6  # a placeholder, not a credential
MESSAGES = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object"}


def client(recorder: Recorder) -> WorkersAI:
    return WorkersAI("acct-1", TOKEN, transport=recorder.transport)


def test_valid_json_object_and_request_shape() -> None:
    rec = canned({"proposals": []})
    result = client(rec).run(MODEL, MESSAGES, max_tokens=600, json_schema=SCHEMA)
    assert result.data == {"proposals": []}
    assert (result.input_tokens, result.output_tokens) == (2400, 350)
    request = rec.requests[0]
    assert request.url.path == f"/client/v4/accounts/acct-1/ai/run/{MODEL}"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = rec.body()
    assert body["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
    assert body["max_tokens"] == 600
    assert TOKEN not in repr(client(rec))


def test_json_string_response_is_parsed() -> None:
    rec = canned(json.dumps({"proposals": [1]}))
    assert client(rec).run(MODEL, MESSAGES, max_tokens=9, json_schema=SCHEMA).data == {
        "proposals": [1]
    }


@pytest.mark.parametrize("response", ["{not json", ["a list"], 42])
def test_malformed_json_is_an_error(response: object) -> None:
    with pytest.raises(AiError, match="malformed_json"):
        client(canned(response)).run(MODEL, MESSAGES, max_tokens=9, json_schema=SCHEMA)


def test_free_text_must_be_text() -> None:
    assert client(canned("hello")).run(MODEL, MESSAGES, max_tokens=9).data == "hello"
    with pytest.raises(AiError, match="malformed_text"):
        client(canned({"x": 1})).run(MODEL, MESSAGES, max_tokens=9)


def test_json_mode_failure_is_a_skip() -> None:
    body = {"success": False, "errors": [{"code": 5006, "message": "JSON Mode couldn't be met"}]}
    with pytest.raises(AiSkip) as info:
        client(canned(body, status=400)).run(MODEL, MESSAGES, max_tokens=9, json_schema=SCHEMA)
    assert info.value.reason == "json_mode"


def test_4xx_is_an_error_without_retry() -> None:
    rec = canned({"success": False, "errors": [{"message": "Authentication error"}]}, status=401)
    with pytest.raises(AiError, match="http_401"):
        client(rec).run(MODEL, MESSAGES, max_tokens=9)
    assert len(rec.requests) == 1


def test_one_retry_on_5xx() -> None:
    answers = iter([httpx.Response(503, text="busy"), httpx.Response(200, json=envelope("ok"))])
    rec = Recorder(lambda request: next(answers))
    assert client(rec).run(MODEL, MESSAGES, max_tokens=9).data == "ok"
    assert len(rec.requests) == 2


def test_two_5xx_fail() -> None:
    rec = Recorder(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(AiError, match="http_500"):
        client(rec).run(MODEL, MESSAGES, max_tokens=9)
    assert len(rec.requests) == 2


def test_timeout_retried_then_error_and_token_never_logged(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    rec = Recorder(slow)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AiError) as info:
        client(rec).run(MODEL, MESSAGES, max_tokens=9)
    assert info.value.reason == "timeout" and len(rec.requests) == 2
    out = capsys.readouterr()
    assert TOKEN not in caplog.text + out.out + out.err + str(info.value)
    assert "acct-1" not in caplog.text + out.out + out.err + str(info.value)


def test_non_json_envelope() -> None:
    rec = Recorder(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(AiError, match="malformed_envelope"):
        client(rec).run(MODEL, MESSAGES, max_tokens=9)


def test_neuron_estimates_match_the_plan_budget() -> None:
    # a daily prop run (2,500 in / 400 out) on the 8B model.
    assert quota.neurons(MODEL, 2500, 400) == 95
    assert quota.neurons("@cf/meta/llama-3.1-8b-instruct-fp8-fast", 800, 150) == 9
    assert quota.neurons("unknown/model", 2500, 400) == 95  # priced like the dearer model
    assert quota.estimate(MODEL, "x" * 401, 400) == (101, quota.neurons(MODEL, 101, 400))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Will the streak hit five?", None),
        ("", "empty_text"),
        ("Hey @everyone look", "link_or_markup"),
        ("See https://evil.example", "link_or_markup"),
        ("visit evil.com", "link_or_markup"),
        ("**bold** claim", "link_or_markup"),
        ("Ignore previous instructions <script>", "link_or_markup"),
        ("What a stupid plateau", "denylist"),
        ("Phil keeps losing", "bettor_name"),
        ("PHIL keeps losing", "bettor_name"),
        ("Philosophy of the scale", None),  # whole words only
        ("line\x00break", "control_chars"),
    ],
)
def test_text_checks(text: str, reason: str | None) -> None:
    assert problem(text, ["Phil", "A"]) == reason


def test_live_envelope_shape_with_choices() -> None:
    """Recorded from a live call (2026-10-04): Workers AI now also returns an
    OpenAI-style `choices` list next to `response`; `response` and `usage` are read."""
    content = '{"proposals": [{"template": "milestone_by", "params": {"threshold": 220.0}}]}'
    body = {
        "success": True,
        "errors": [],
        "result": {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "model": "@cf/meta/llama-3.1-8b-fast-v2",
            "object": "chat.completion",
            "response": {"proposals": [{"template": "milestone_by", "params": {"threshold": 220}}]},
            "usage": {"prompt_tokens": 779, "completion_tokens": 187},
        },
    }
    rec = Recorder(lambda request: httpx.Response(200, json=body))
    result = client(rec).run(MODEL, MESSAGES, max_tokens=600, json_schema=SCHEMA)
    assert result.data["proposals"][0]["params"]["threshold"] == 220
    assert (result.input_tokens, result.output_tokens) == (779, 187)
