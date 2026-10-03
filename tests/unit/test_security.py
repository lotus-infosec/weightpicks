import re

import pytest
from argon2 import PasswordHasher

from app.core.config import Settings
from app.core.security import (
    code_hash,
    hash_password,
    needs_rehash,
    new_registration_code,
    new_token,
    password_problem,
    same,
    token_hash,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "correct horse batterY")
    assert not verify_password(None, "anything at all")
    assert not verify_password("not-a-hash", "anything at all")
    assert not needs_rehash(hashed)


def test_old_parameters_need_a_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("correct horse battery")
    assert verify_password(weak, "correct horse battery")
    assert needs_rehash(weak)


@pytest.mark.parametrize(
    ("password", "ok"), [("short", False), ("x" * 9, False), ("x" * 10, True), ("x" * 257, False)]
)
def test_password_rules(password: str, ok: bool) -> None:
    assert (password_problem(password) is None) is ok


def test_tokens_are_random_and_hashed() -> None:
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert token_hash(a) == token_hash(a) and len(token_hash(a)) == 64
    assert same(a, a) and not same(a, b)


def test_registration_codes() -> None:
    code = new_registration_code()
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code)
    # Typing it in lower case or without dashes still matches.
    assert code_hash(code.lower().replace("-", " ")) == code_hash(code)
    assert len({new_registration_code() for _ in range(200)}) == 200


def test_insecure_cookies_only_in_dev() -> None:
    assert Settings(app_env="dev", wp_cookie_secure=False).wp_cookie_secure is False
    with pytest.raises(ValueError, match="WP_COOKIE_SECURE"):
        Settings(app_env="production", app_secret_key="k" * 40, wp_cookie_secure=False)
