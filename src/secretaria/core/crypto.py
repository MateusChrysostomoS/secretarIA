"""Symmetric encryption for tenant secrets at rest (Fernet / AES-128-CBC + HMAC).

Used to protect the Google Calendar `refresh_token` stored in
`tenant_credentials`. The key lives in settings.ENCRYPTION_KEY and never in the
database. Ciphertext is what persists; plaintext only exists transiently in
memory while a Calendar call is being made.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from secretaria.config import get_settings


class EncryptionError(RuntimeError):
    """Raised when encryption/decryption cannot be performed."""


@lru_cache
def _fernet() -> Fernet:
    """Build the process-wide Fernet from settings.ENCRYPTION_KEY.

    Cached so we validate the key once. Raises EncryptionError (not a bare
    ValueError) so callers can distinguish a config problem from a data problem.
    """
    key = get_settings().ENCRYPTION_KEY
    if not key:
        raise EncryptionError(
            "ENCRYPTION_KEY is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and put it in .env.'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:  # noqa: BLE001 - normalise to our error type
        raise EncryptionError("ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt(plaintext: str) -> str:
    """Encrypt `plaintext`, returning a urlsafe-base64 ciphertext string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a ciphertext produced by `encrypt`. Raises EncryptionError if
    the token was tampered with or was encrypted under a different key."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError(
            "Failed to decrypt a tenant secret. The ENCRYPTION_KEY may have "
            "changed since the value was stored."
        ) from exc
