import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiKey(Base):
    """A client credential. Only a SHA-256 hash of the key is stored, never the key itself."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    # First characters of the plaintext key, so operators can tell keys apart in listings.
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Where to POST job.completed / job.failed notifications, and the secret used to sign
    # them. The secret must be readable (it is an HMAC key), so unlike the API key it is not
    # hashed.
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    webhook_secret: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} prefix={self.key_prefix} active={self.is_active}>"
