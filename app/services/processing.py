"""Runs one extraction job and records the outcome on the ExtractionJob row.

State flow: pending → processing → completed | failed. A transient failure that will be
retried moves the job back to pending until the next attempt.
"""

import uuid
from collections.abc import Callable

import structlog
from sqlalchemy.orm import Session

from app.models import ExtractionJob, JobStatus
from app.schemas.events import JobEvent, JobStage
from app.schemas.jobs import JobRead
from app.services.document_reader import DocumentReadError
from app.services.events import EventPublisher
from app.services.extraction import ExtractionError, ExtractionService
from app.services.llm import is_transient_llm_error
from app.services.storage import ObjectNotFoundError, Storage, StorageUnavailableError

logger = structlog.get_logger(__name__)

_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


class TransientJobError(Exception):
    """The attempt failed for a reason that may go away; the caller should retry."""


def process_job(
    db: Session,
    storage: Storage,
    get_service: Callable[[], ExtractionService],
    job_id: uuid.UUID,
    *,
    is_final_attempt: bool,
    publish: EventPublisher,
) -> JobStatus | None:
    """Process a job once. Returns the resulting status, or None if the job does not exist.

    Every state change and processing step is also published as a JobEvent for live progress.
    Raises TransientJobError when the attempt should be retried.
    """
    job = db.get(ExtractionJob, job_id)
    if job is None:
        logger.warning("job_not_found")
        return None
    if job.status in _TERMINAL_STATUSES:
        logger.info("job_already_finished", status=job.status.value)
        return job.status

    def emit(stage: JobStage) -> None:
        _publish(publish, job, stage)

    job.status = JobStatus.PROCESSING
    job.attempts += 1
    db.commit()
    emit("processing")

    try:
        data = storage.read(job.storage_key)
        result = get_service().extract_document(
            data, job.original_filename, job.document_type, on_progress=emit
        )
    except ObjectNotFoundError:
        return _fail(db, job, "Uploaded file is missing from storage.", publish)
    except (DocumentReadError, ExtractionError) as exc:
        return _fail(db, job, str(exc), publish)
    except Exception as exc:
        if not _is_transient(exc):
            logger.exception("job_unexpected_error")
            return _fail(db, job, "Internal error while processing the document.", publish)
        if is_final_attempt:
            return _fail(
                db,
                job,
                f"Service temporarily unavailable after {job.attempts} attempt(s) "
                f"({type(exc).__name__}).",
                publish,
            )
        job.status = JobStatus.PENDING
        job.error_message = f"Attempt {job.attempts} failed ({type(exc).__name__}); retrying."
        db.commit()
        emit("retrying")
        logger.warning("job_attempt_failed", error=type(exc).__name__, attempts=job.attempts)
        raise TransientJobError(str(exc)) from exc

    job.status = JobStatus.COMPLETED
    job.result = result.model_dump(mode="json")
    job.error_message = None
    db.commit()
    emit("completed")
    logger.info("job_completed", attempts=job.attempts)
    return JobStatus.COMPLETED


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, StorageUnavailableError) or is_transient_llm_error(exc)


def _publish(publish: EventPublisher, job: ExtractionJob, stage: JobStage) -> None:
    """Publish a progress event; never lets a publishing problem affect the job."""
    try:
        message = job.error_message if stage in ("retrying", "failed") else None
        event = JobEvent(stage=stage, status=job.status, attempt=job.attempts, message=message)
        if event.is_terminal:
            event.job = JobRead.model_validate(job)
        publish(job.id, event)
    except Exception:
        logger.warning("job_event_publish_failed", stage=stage, exc_info=True)


def _fail(db: Session, job: ExtractionJob, message: str, publish: EventPublisher) -> JobStatus:
    job.status = JobStatus.FAILED
    job.error_message = message
    db.commit()
    _publish(publish, job, "failed")
    logger.warning("job_failed", error_message=message, attempts=job.attempts)
    return JobStatus.FAILED
