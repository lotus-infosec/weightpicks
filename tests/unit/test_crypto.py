import pytest

from app.core.crypto import SecretKeyMissing, WrongSecretKey, decrypt, derive_fernet, encrypt

KEY = "k" * 40


def test_round_trip_and_determinism() -> None:
    a, b = derive_fernet(KEY), derive_fernet(KEY)
    token = encrypt(a, "https://discord.com/api/webhooks/1/abc")
    assert "discord" not in token
    assert decrypt(b, token) == "https://discord.com/api/webhooks/1/abc"  # same key, new object


def test_wrong_key_is_detected() -> None:
    token = encrypt(derive_fernet(KEY), "secret")
    with pytest.raises(WrongSecretKey):
        decrypt(derive_fernet("x" * 40), token)


@pytest.mark.parametrize("key", ["", "short"])
def test_missing_key(key: str) -> None:
    with pytest.raises(SecretKeyMissing):
        derive_fernet(key)
