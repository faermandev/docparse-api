"""Webhook notifications for finished jobs, configured per API key.

Flow:
1. When a job completes or fails, `record_job_webhook` adds a WebhookDelivery row in the same
   transaction (transactional outbox), so the notification cannot be lost after the job is
   marked done.
2. A Celery task calls `deliver_webhook`, which POSTs the signed payload and retries
   transient failures with backoff.
3. A periodic sweep re-enqueues pending deliveries whose task was lost.

Delivery is at-least-once: receivers should deduplicate on the `X-Webhook-Id` header.
"""

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx2
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import (
    ApiKey,
    ExtractionJob,
    JobStatus,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEventType,
)
from app.schemas.jobs import JobRead

logger = structlog.get_logger(__name__)

SIGNATURE_HEADER = "X-Webhook-Signature"
ID_HEADER = "X-Webhook-Id"
EVENT_HEADER = "X-Webhook-Event"
USER_AGENT = "docs-extraction-webhooks/1"
SECRET_PREFIX = "whsec_"
MAX_URL_LENGTH = 2048
_RETRYABLE_STATUS_CODES = frozenset({408, 429})

Resolver = Callable[[str, int], Sequence[str]]


class InvalidWebhookUrlError(AppError):
    status_code = 422
    code = "invalid_webhook_url"


class TransientDeliveryError(Exception):
    """The attempt failed for a reason that may go away; the caller should retry."""


# Destination checks (SSRF protection)


def resolve_host(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def validate_destination(
    url: str, *, allow_insecure: bool, resolve: Resolver | None = None
) -> None:
    """Reject URLs that are malformed, unencrypted, or that reach non-public addresses.

    Without this check a client could make the server call internal services (MinIO,
    PostgreSQL, the cloud metadata endpoint). It runs on registration and again before
    every delivery, since DNS can change in between.
    """
    if len(url) > MAX_URL_LENGTH:
        raise InvalidWebhookUrlError(f"Webhook URL is longer than {MAX_URL_LENGTH} characters.")
    parts = urlsplit(url)
    allowed_schemes = {"https", "http"} if allow_insecure else {"https"}
    if parts.scheme not in allowed_schemes:
        raise InvalidWebhookUrlError("Webhook URL must use https://.")
    if not parts.hostname:
        raise InvalidWebhookUrlError("Webhook URL has no host.")
    if parts.username or parts.password:
        raise InvalidWebhookUrlError("Webhook URL must not contain credentials.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise InvalidWebhookUrlError("Webhook URL has an invalid port.") from None
    if allow_insecure:
        return

    try:
        addresses = (resolve or resolve_host)(parts.hostname, port)
    except OSError:
        raise InvalidWebhookUrlError(f"Host {parts.hostname!r} could not be resolved.") from None
    if not addresses:
        raise InvalidWebhookUrlError(f"Host {parts.hostname!r} could not be resolved.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise InvalidWebhookUrlError(
                "Webhook URL points to a private, loopback or reserved address."
            )


# Configuration (per API key)


def configure_webhook(
    db: Session, api_key: ApiKey, url: str, *, allow_insecure: bool, resolve: Resolver | None = None
) -> ApiKey:
    """Set the key's webhook URL. A signing secret is created the first time and then kept."""
    validate_destination(url, allow_insecure=allow_insecure, resolve=resolve)
    api_key.webhook_url = url
    if api_key.webhook_secret is None:
        api_key.webhook_secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    db.commit()
    logger.info("webhook_configured", api_key_id=str(api_key.id))
    return api_key


def remove_webhook(db: Session, api_key: ApiKey) -> None:
    """Stop notifications; pending deliveries are abandoned. Re-adding creates a new secret."""
    api_key.webhook_url = None
    api_key.webhook_secret = None
    db.commit()
    logger.info("webhook_removed", api_key_id=str(api_key.id))


def list_deliveries(db: Session, api_key: ApiKey, *, limit: int) -> Sequence[WebhookDelivery]:
    return db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.api_key_id == api_key.id)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
        .limit(limit)
    ).all()


# Signatures


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return f"t={timestamp},v1={compute_signature(secret, timestamp, body)}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """Receiver-side check: valid HMAC and a recent timestamp (limits replays)."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
        signature = fields["v1"]
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    return hmac.compare_digest(signature, compute_signature(secret, timestamp, body))


# Outbox


def record_job_webhook(db: Session, job: ExtractionJob) -> WebhookDelivery | None:
    """Queue a notification for a finished job, if its API key has a webhook.

    Call before committing the job's final state, so both are saved atomically.
    """
    api_key = db.get(ApiKey, job.api_key_id)
    if api_key is None or api_key.webhook_url is None:
        return None
    event_type = (
        WebhookEventType.JOB_COMPLETED
        if job.status == JobStatus.COMPLETED
        else WebhookEventType.JOB_FAILED
    )
    delivery_id = uuid.uuid4()
    delivery = WebhookDelivery(
        id=delivery_id,
        api_key_id=api_key.id,
        job_id=job.id,
        event_type=event_type,
        url=api_key.webhook_url,
        payload={
            "id": str(delivery_id),
            "type": event_type.value,
            "created_at": datetime.now(UTC).isoformat(),
            "data": {"job": JobRead.model_validate(job).model_dump(mode="json")},
        },
    )
    db.add(delivery)
    return delivery


def find_stale_deliveries(
    db: Session, *, overdue_by: timedelta, limit: int = 100
) -> list[uuid.UUID]:
    """Pending deliveries well past their due time, i.e. whose task was probably lost.

    Their due time is pushed to now, so the next sweep does not pick them again at once.
    """
    now = datetime.now(UTC)
    deliveries = db.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == WebhookDeliveryStatus.PENDING,
            WebhookDelivery.next_attempt_at < now - overdue_by,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for delivery in deliveries:
        delivery.next_attempt_at = now
    db.commit()
    return [delivery.id for delivery in deliveries]


# Delivery


def deliver_webhook(
    db: Session,
    delivery_id: uuid.UUID,
    client: httpx2.Client,
    *,
    is_final_attempt: bool,
    retry_delay_seconds: float,
    allow_insecure: bool,
    resolve: Resolver | None = None,
) -> WebhookDeliveryStatus | None:
    """Attempt one delivery. Returns the resulting status (None if the delivery is unknown).

    Raises TransientDeliveryError when the attempt should be retried.
    """
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        logger.warning("webhook_delivery_not_found", delivery_id=str(delivery_id))
        return None
    if delivery.status != WebhookDeliveryStatus.PENDING:
        return delivery.status
    log = logger.bind(delivery_id=str(delivery.id), job_id=str(delivery.job_id))

    api_key = db.get(ApiKey, delivery.api_key_id)
    if api_key is None or not api_key.is_active or api_key.webhook_secret is None:
        return _give_up(db, delivery, "Webhook is no longer configured for this API key.")
    try:
        validate_destination(delivery.url, allow_insecure=allow_insecure, resolve=resolve)
    except InvalidWebhookUrlError as exc:
        return _give_up(db, delivery, exc.message)

    body = json.dumps(delivery.payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        ID_HEADER: str(delivery.id),
        EVENT_HEADER: delivery.event_type.value,
        SIGNATURE_HEADER: signature_header(api_key.webhook_secret, body),
    }
    delivery.attempts += 1
    status_code: int | None = None
    try:
        response = client.post(delivery.url, content=body, headers=headers)
    except httpx2.TimeoutException:
        error, retryable = "Request timed out.", True
    except httpx2.TransportError as exc:
        error, retryable = f"Connection failed ({type(exc).__name__}).", True
    else:
        status_code = response.status_code
        if 200 <= status_code < 300:
            delivery.status = WebhookDeliveryStatus.DELIVERED
            delivery.last_status_code = status_code
            delivery.last_error = None
            delivery.delivered_at = datetime.now(UTC)
            db.commit()
            log.info("webhook_delivered", status_code=status_code, attempts=delivery.attempts)
            return WebhookDeliveryStatus.DELIVERED
        retryable = status_code in _RETRYABLE_STATUS_CODES or status_code >= 500
        error = f"Endpoint answered HTTP {status_code}."
        if 300 <= status_code < 400:
            error += " Redirects are not followed."

    delivery.last_status_code = status_code
    delivery.last_error = error
    if retryable and not is_final_attempt:
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
        db.commit()
        log.warning("webhook_attempt_failed", error=error, attempts=delivery.attempts)
        raise TransientDeliveryError(error)
    return _give_up(db, delivery, error)


def _give_up(db: Session, delivery: WebhookDelivery, error: str) -> WebhookDeliveryStatus:
    delivery.status = WebhookDeliveryStatus.FAILED
    delivery.last_error = error
    db.commit()
    logger.warning(
        "webhook_failed", delivery_id=str(delivery.id), error=error, attempts=delivery.attempts
    )
    return WebhookDeliveryStatus.FAILED
