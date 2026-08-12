"""
manual section 16: "Encrypt secrets."

HONEST SCOPING NOTE: as of this module, no model in this codebase
actually stores a secret in the database -- broker credentials
(ANGEL_ONE_*) and the news API key live in .env, which is itself the
standard, correct place for deploy-time secrets (never committed,
loaded at process start, not queryable/dumpable via the ORM or admin).
There is currently nothing to encrypt in a database column.

This module exists as ready-to-use infrastructure for the day that
changes -- e.g. if this platform ever needs to store a broker
credential per-user in the DB (multiple trading accounts, credentials
entered via the UI instead of a shared .env) rather than one shared
.env. At that point, whatever model holds that field should encrypt it
with encrypt_secret()/decrypt_secret() below rather than storing it in
plaintext, and this module is where that logic already lives and is
tested, instead of being invented ad-hoc at that point.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _get_fernet():
    # Imported lazily so `cryptography` is only required if this is
    # actually used -- same lazy-dependency pattern as
    # apps.market_data.broker_client's SmartConnect import and
    # apps.news.finbert's transformers import.
    from cryptography.fernet import Fernet

    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set -- generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and put it in .env. "
            "Never reuse the same key across environments (dev/staging/prod) -- "
            "a leaked dev key should not be able to decrypt production secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    """Returns a string safe to store in a database column."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """
    Raises cryptography.fernet.InvalidToken if the ciphertext is
    corrupted, was encrypted with a different key, or has expired
    (if a TTL was used at encrypt time) -- deliberately NOT caught and
    swallowed here, since silently returning a wrong/empty value for a
    secret that failed to decrypt is far more dangerous than a loud
    exception forcing whoever's calling this to notice and handle it.
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()
