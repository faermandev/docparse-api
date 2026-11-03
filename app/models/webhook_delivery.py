import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookEventType(StrEnum):
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"


class WebhookDelivery(Base):
    """One webhook notification (transactional outbox).

    The row is written in the same transaction that finishes the job, so a notification is
    never lost between "job done" and "webhook sent"; a separate task delivers it, with retries.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[WebhookEventType] = mapped_column(
        str_enum(WebhookEventType, "webhook_event_type")
    )
    # The URL at the time of the event; the payload is a snapshot sent as-is on every attempt.
    url: Mapped[str] = mapped_column(String(2048))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        str_enum(WebhookDeliveryStatus, "webhook_delivery_status"),
        default=WebhookDeliveryStatus.PENDING,
        server_default=WebhookDeliveryStatus.PENDING.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # When the next attempt is due; lets a periodic sweep re-enqueue deliveries whose task
    # was lost (e.g. Redis was down when it should have been queued).
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<WebhookDelivery id={self.id} {self.event_type} status={self.status}>"
