import random
import uuid
from functools import lru_cache

import structlog
from celery import Task
from kombu.exceptions import OperationalError

from app.core.config import get_settings
from app.core.errors import QueueUnavailableError
from app.db.session import SessionLocal
from app.services.events import get_event_broker
from app.services.extraction import ExtractionService, build_extraction_service
from app.services.processing import TransientJobError, process_job
from app.services.storage import get_storage
from app.workers.celery_app import celery_app

MAX_RETRY_DELAY_SECONDS = 600.0


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
                publish=get_event_broker().publish,
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
