import json
import stat
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.providers import garmin_login

PASSWORD = "correct horse battery staple"


class FakeGarmin:
    def __init__(self, *, email: str, password: str, prompt_mfa: Any, writes: bool = True) -> None:
        self.email, self.password, self.prompt_mfa = email, password, prompt_mfa
        self.display_name = "subject42"
        self.writes = writes

    def login(self, tokenstore: str) -> None:
        assert self.prompt_mfa() == "123456"
        if self.writes:
            Path(tokenstore).write_text('{"di_token": "opaque"}')


def answers(prompt: str) -> str:
    if prompt.startswith("Sign in with"):
        return ""
    return {"Garmin Connect email: ": " me@example.invalid ", "MFA code from Garmin: ": "123456"}[
        prompt
    ]


def test_login_stores_a_token_and_never_the_password(tmp_path: Path) -> None:
    config_dir = tmp_path / ".GarminDb"
    (config_dir).mkdir()
    (config_dir / "garmin_tokens.json").write_text("old")
    name = garmin_login.run(
        config_dir, FakeGarmin, answers, lambda _p: PASSWORD, today=date(2026, 10, 3)
    )
    assert name == "subject42"
    token = config_dir / "garmin_tokens.json"
    assert token.read_text() == '{"di_token": "opaque"}'  # old token replaced
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    config_file = config_dir / "GarminConnectConfig.json"
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    config = json.loads(config_file.read_text())
    assert config["credentials"]["user"] == "" and config["credentials"]["password"] == ""
    assert config["data"]["weight_start_date"] == "06/05/2026"  # 120 days back
    assert config["enabled_stats"]["monitoring"] and not config["enabled_stats"]["weight"]
    assert not config["enabled_stats"]["sleep"]
    for path in config_dir.iterdir():
        text = path.read_text()
        assert PASSWORD not in text and "me@example.invalid" not in text


def test_login_without_a_token_fails(tmp_path: Path) -> None:
    def no_token(**kwargs: Any) -> FakeGarmin:
        return FakeGarmin(**kwargs, writes=False)

    with pytest.raises(SystemExit, match="reusable token"):
        garmin_login.run(tmp_path / "c", no_token, answers, lambda _p: PASSWORD)
    assert not (tmp_path / "c" / "GarminConnectConfig.json").exists()


def test_login_needs_email_and_password(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="both required"):
        garmin_login.run(tmp_path / "c", FakeGarmin, answers, lambda _p: "")


def test_main_reports_failures_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("401 from Garmin")

    monkeypatch.setattr(garmin_login, "run", boom)
    monkeypatch.setitem(__import__("sys").modules, "garminconnect", type("M", (), {"Garmin": 1}))
    assert garmin_login.main() == 1
    assert "Garmin login failed: RuntimeError: 401 from Garmin" in capsys.readouterr().err


class FakeClient:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def _exchange_service_ticket(self, ticket: str, service_url: str) -> None:
        self.state["exchanged"] = (ticket, service_url)

    def dump(self, path: str) -> None:
        Path(path).write_text('{"di_token": "from-ticket"}')


class FakeTicketGarmin:
    state: ClassVar[dict[str, Any]] = {}

    def __init__(self) -> None:
        self.client = FakeClient(self.state)
        self.display_name = "subject42"

    def login(self, tokenstore: str) -> None:
        self.state["verified"] = Path(tokenstore).read_text()


def test_browser_ticket_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def ask(prompt: str) -> str:
        assert prompt.startswith("Sign in with")
        return "2"

    url = "https://mobile.integration.garmin.com/gcm/ios?ticket=ST-0123-abcXYZ-sso"
    name = garmin_login.run(tmp_path / "c", FakeTicketGarmin, ask, lambda _p: url)
    assert name == "subject42"
    assert FakeTicketGarmin.state["exchanged"] == (
        "ST-0123-abcXYZ-sso",
        garmin_login.BROWSER_SERVICE,
    )
    assert FakeTicketGarmin.state["verified"] == '{"di_token": "from-ticket"}'
    assert stat.S_IMODE((tmp_path / "c" / "garmin_tokens.json").stat().st_mode) == 0o600
    assert "ST-0123" not in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No ticket"):
        garmin_login.run(tmp_path / "c", FakeTicketGarmin, ask, lambda _p: "nothing here")
