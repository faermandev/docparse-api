"""API key issuing and verification.

Keys are 256-bit random tokens, so a fast SHA-256 digest is enough to store them safely
(slow password hashes like bcrypt protect low-entropy secrets, which these are not). Lookup
is by digest, so comparisons never touch the plaintext.
"""

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiKey

KEY_PREFIX = "dex_"
_DISPLAY_PREFIX_LENGTH = 12


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_api_key(db: Session, name: str) -> tuple[ApiKey, str]:
    """Persist a new key and return it with its plaintext (shown to the caller only once)."""
    plaintext = generate_api_key()
    api_key = ApiKey(
        name=name,
        key_prefix=plaintext[:_DISPLAY_PREFIX_LENGTH],
        key_hash=hash_api_key(plaintext),
    )
    db.add(api_key)
    db.commit()
    return api_key, plaintext


def authenticate(db: Session, key: str) -> ApiKey | None:
    """Return the active ApiKey matching the plaintext key, or None."""
    return db.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(key), ApiKey.is_active.is_(True))
    )
