import random
import uuid
from datetime import timedelta
from functools import lru_cache

import httpx2
import structlog
from celery import Task
from kombu.exceptions import OperationalError

from app.core.config import get_settings
from app.core.errors import QueueUnavailableError
from app.db.session import SessionLocal
from app.services.events import get_event_broker
from app.services.extraction import ExtractionService, build_extraction_service
from app.services.processing import JobNotifier, TransientJobError, process_job
from app.services.storage import get_storage
from app.services.webhooks import (
    TransientDeliveryError,
    deliver_webhook,
    find_stale_deliveries,
)
from app.workers.celery_app import celery_app

MAX_RETRY_DELAY_SECONDS = 600.0
# A pending delivery this far past its due time is assumed to have lost its task.
STALE_DELIVERY_AFTER = timedelta(minutes=2)


@lru_cache
def get_extraction_service() -> ExtractionService:
    """One service (and LLM client) per worker process."""
    return build_extraction_service(get_settings())


def retry_countdown(retries: int, base_seconds: float) -> float:
    """Exponential backoff with jitter: base·2^n (capped) plus up to `base` seconds."""
    return min(base_seconds * 2**retries, MAX_RETRY_DELAY_SECONDS) + random.uniform(0, base_seconds)


@celery_app.task(bind=True, name="jobs.process_job", max_retries=get_settings().job_max_retries)
def process_job_task(self: Task, job_id: str, request_id: str | None = None) -> str | None:
    settings = get_settings()
    retries = self.request.retries
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        job_id=job_id, task_id=self.request.id, request_id=request_id, attempt=retries + 1
    )
    with SessionLocal() as db:
        try:
            status = process_job(
                db,
                get_storage(),
                get_extraction_service,
                uuid.UUID(job_id),
                is_final_attempt=retries >= (self.max_retries or 0),
                notifier=JobNotifier(
                    publish=get_event_broker().publish,
                    enqueue_webhook=enqueue_webhook_delivery,
                ),
            )
        except TransientJobError as exc:
            countdown = retry_countdown(retries, settings.job_retry_backoff_seconds)
            raise self.retry(exc=exc, countdown=countdown) from exc
    return status.value if status else None


def enqueue_process_job(job_id: uuid.UUID) -> None:
    """Queue a job, carrying the current request id so API and worker logs can be joined."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    try:
        process_job_task.apply_async(args=[str(job_id)], kwargs={"request_id": request_id})
    except OperationalError as exc:
        raise QueueUnavailableError("Job queue is unavailable; try again later.") from exc


# Webhooks (routed to their own queue, so slow receivers never delay extractions)


def http_client() -> httpx2.Client:
    # Redirects are not followed: a redirect could point at an internal address.
    return httpx2.Client(timeout=get_settings().webhook_timeout_seconds, follow_redirects=False)


@celery_app.task(bind=True, name="webhooks.deliver", max_retries=get_settings().webhook_max_retries)
def deliver_webhook_task(self: Task, delivery_id: str) -> str | None:
    settings = get_settings()
    retries = self.request.retries
    countdown = retry_countdown(retries, settings.webhook_retry_backoff_seconds)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        delivery_id=delivery_id, task_id=self.request.id, attempt=retries + 1
    )
    with SessionLocal() as db, http_client() as client:
        try:
            status = deliver_webhook(
                db,
                uuid.UUID(delivery_id),
                client,
                is_final_attempt=retries >= (self.max_retries or 0),
                retry_delay_seconds=countdown,
                allow_insecure=settings.webhook_allow_insecure_urls,
            )
        except TransientDeliveryError as exc:
            raise self.retry(exc=exc, countdown=countdown) from exc
    return status.value if status else None


@celery_app.task(name="webhooks.sweep")
def sweep_webhook_deliveries_task() -> int:
    """Re-enqueue pending deliveries whose task was lost. Scheduled by Celery beat."""
    with SessionLocal() as db:
        delivery_ids = find_stale_deliveries(db, overdue_by=STALE_DELIVERY_AFTER)
    for delivery_id in delivery_ids:
        enqueue_webhook_delivery(delivery_id)
    return len(delivery_ids)


def enqueue_webhook_delivery(delivery_id: uuid.UUID) -> None:
    deliver_webhook_task.apply_async(args=[str(delivery_id)])
