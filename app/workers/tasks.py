import random
import uuid
from functools import lru_cache

from celery import Task
from kombu.exceptions import OperationalError

from app.core.config import get_settings
from app.core.errors import QueueUnavailableError
from app.db.session import SessionLocal
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
def process_job_task(self: Task, job_id: str) -> str | None:
    settings = get_settings()
    retries = self.request.retries
    with SessionLocal() as db:
        try:
            status = process_job(
                db,
                get_storage(),
                get_extraction_service,
                uuid.UUID(job_id),
                is_final_attempt=retries >= (self.max_retries or 0),
            )
        except TransientJobError as exc:
            countdown = retry_countdown(retries, settings.job_retry_backoff_seconds)
            raise self.retry(exc=exc, countdown=countdown) from exc
    return status.value if status else None


def enqueue_process_job(job_id: uuid.UUID) -> None:
    try:
        process_job_task.delay(str(job_id))
    except OperationalError as exc:
        raise QueueUnavailableError("Job queue is unavailable; try again later.") from exc
