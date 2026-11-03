from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentApiKey, DbSession, SettingsDep
from app.core.errors import NotFoundError
from app.models import ApiKey
from app.schemas.errors import ErrorResponse
from app.schemas.webhooks import (
    WebhookConfigRead,
    WebhookConfigUpdate,
    WebhookDeliveryList,
    WebhookDeliveryRead,
)
from app.services import webhooks as webhook_service

router = APIRouter(prefix="/webhook", tags=["webhooks"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


def _config(api_key: ApiKey) -> WebhookConfigRead:
    if api_key.webhook_url is None or api_key.webhook_secret is None:
        raise NotFoundError("No webhook is configured for this API key.")
    return WebhookConfigRead(url=api_key.webhook_url, secret=api_key.webhook_secret)


@router.get(
    "",
    response_model=WebhookConfigRead,
    responses={**_ERROR_RESPONSES, status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def get_webhook(api_key: CurrentApiKey) -> WebhookConfigRead:
    """Return this API key's webhook URL and signing secret."""
    return _config(api_key)


@router.put("", response_model=WebhookConfigRead, responses=_ERROR_RESPONSES)
def set_webhook(
    body: WebhookConfigUpdate, api_key: CurrentApiKey, db: DbSession, settings: SettingsDep
) -> WebhookConfigRead:
    """Register (or change) the URL notified when this key's jobs complete or fail.

    The signing secret is created on first registration and kept when the URL changes.
    The URL must use HTTPS and resolve to a public address.
    """
    webhook_service.configure_webhook(
        db, api_key, body.url, allow_insecure=settings.webhook_allow_insecure_urls
    )
    return _config(api_key)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=_ERROR_RESPONSES)
def delete_webhook(api_key: CurrentApiKey, db: DbSession) -> Response:
    """Stop sending notifications. Registering again later creates a new secret."""
    webhook_service.remove_webhook(db, api_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/deliveries", response_model=WebhookDeliveryList, responses=_ERROR_RESPONSES)
def list_webhook_deliveries(
    api_key: CurrentApiKey,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WebhookDeliveryList:
    """Recent notifications for this key, newest first, with their delivery status."""
    deliveries = webhook_service.list_deliveries(db, api_key, limit=limit)
    return WebhookDeliveryList(items=[WebhookDeliveryRead.model_validate(d) for d in deliveries])
