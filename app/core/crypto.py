"""Encryption for integration secrets (BUILD_PLAN §1.6).

The Fernet key is derived from APP_SECRET_KEY with HKDF-SHA256, so `.env` holds only
one secret. A key-check value (an encrypted constant stored with the secrets) detects
a wrong key, e.g. a backup restored on a host with a different APP_SECRET_KEY.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MIN_KEY_LENGTH = 32
_SALT = b"weightpicks/secrets/salt/v1"
_INFO = b"weightpicks secrets v1"
KEY_CHECK_NAME = "__key_check__"
KEY_CHECK_PLAINTEXT = "weightpicks-key-check-v1"


class SecretKeyMissing(Exception):
    """APP_SECRET_KEY is not set (or too short): secrets can't be stored or read."""


class WrongSecretKey(Exception):
    """Stored secrets were encrypted with a different APP_SECRET_KEY."""


def derive_fernet(app_secret_key: str) -> Fernet:
    if len(app_secret_key) < MIN_KEY_LENGTH:
        raise SecretKeyMissing(f"APP_SECRET_KEY must be at least {MIN_KEY_LENGTH} characters")
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(
        app_secret_key.encode()
    )
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(fernet: Fernet, plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(fernet: Fernet, ciphertext: str) -> str:
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise WrongSecretKey("secret could not be decrypted with this APP_SECRET_KEY") from exc
