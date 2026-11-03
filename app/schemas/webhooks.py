import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import WebhookDeliveryStatus, WebhookEventType


class WebhookConfigUpdate(BaseModel):
    url: str = Field(
        max_length=2048,
        description="HTTPS endpoint that will receive job.completed and job.failed events.",
        examples=["https://example.com/hooks/docs-extraction"],
    )


class WebhookConfigRead(BaseModel):
    url: str
    secret: str = Field(description="Use it to verify the X-Webhook-Signature header.")
    events: list[WebhookEventType] = Field(default_factory=lambda: list(WebhookEventType))


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    event_type: WebhookEventType
    url: str
    status: WebhookDeliveryStatus
    attempts: int
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None


class WebhookDeliveryList(BaseModel):
    items: list[WebhookDeliveryRead]
