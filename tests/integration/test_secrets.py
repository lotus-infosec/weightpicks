import pytest
from sqlalchemy import Engine, select

from app.core.clock import SimClock
from app.core.crypto import SecretKeyMissing, WrongSecretKey
from app.core.db import immediate
from app.models import Secret
from app.services import secrets

KEY = "k" * 40


def test_put_get_and_ciphertext_only(migrated_engine: Engine, clock: SimClock) -> None:
    url = "https://discord.com/api/webhooks/123/abc"
    with immediate(migrated_engine) as conn:
        secrets.put(conn, clock, KEY, "webhook.bets_placed", url)
        secrets.put(conn, clock, KEY, "webhook.bets_placed", url + "2")  # update in place
    with migrated_engine.connect() as conn:
        assert secrets.get(conn, KEY, "webhook.bets_placed") == url + "2"
        assert secrets.get(conn, KEY, "nothing") is None
        assert secrets.names(conn) == ["webhook.bets_placed"]
        stored = conn.execute(select(Secret.ciphertext)).scalars().all()
    assert all("discord" not in c for c in stored) and len(stored) == 2  # + key check


def test_wrong_or_missing_key(
    migrated_engine: Engine, clock: SimClock, caplog: pytest.LogCaptureFixture
) -> None:
    with immediate(migrated_engine) as conn:
        secrets.put(conn, clock, KEY, "workers_ai.token", "tok-123")
    with migrated_engine.connect() as conn:
        assert secrets.get(conn, "y" * 40, "workers_ai.token") is None  # unusable, not a crash
        assert secrets.get(conn, "", "workers_ai.token") is None
        with pytest.raises(WrongSecretKey):
            secrets.check_key(conn, "y" * 40)
    with immediate(migrated_engine) as conn:
        with pytest.raises(WrongSecretKey):
            secrets.put(conn, clock, "y" * 40, "smtp.password", "pw")
        with pytest.raises(SecretKeyMissing):
            secrets.put(conn, clock, "", "smtp.password", "pw")
        with pytest.raises(ValueError, match="reserved"):
            secrets.put(conn, clock, KEY, "__key_check__", "x")
        secrets.remove(conn, "workers_ai.token")
    with migrated_engine.connect() as conn:
        assert secrets.names(conn) == []
