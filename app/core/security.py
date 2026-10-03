"""Password hashing, tokens and registration codes (BUILD_PLAN §1.6).

argon2id with argon2-cffi's defaults (rehash on login when they change). Session
tokens are 256 random bits; only their SHA-256 is stored, so a database leak gives
no usable session. Registration codes are short enough to type, long enough
(60 bits) that a 5-per-hour limit makes guessing hopeless.
"""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easy to read aloud
CODE_GROUPS, CODE_GROUP_LEN = 3, 4

_hasher = PasswordHasher()
# Verified against unknown emails so a miss costs the same time as a hit.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: a missing hash still runs a full argon2 verify."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_registration_code() -> str:
    groups = (
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN))
        for _ in range(CODE_GROUPS)
    )
    return "-".join(groups)


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def code_hash(code: str) -> str:
    return token_hash(normalize_code(code))
