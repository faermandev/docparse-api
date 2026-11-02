"""Runs one extraction job and records the outcome on the ExtractionJob row.

State flow: pending → processing → completed | failed. A transient failure that will be
retried moves the job back to pending until the next attempt.
"""

import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import ExtractionJob, JobStatus
from app.services.document_reader import DocumentReadError
from app.services.extraction import ExtractionError, ExtractionService
from app.services.llm import is_transient_llm_error
from app.services.storage import LocalFileStorage

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


class TransientJobError(Exception):
    """The attempt failed for a reason that may go away; the caller should retry."""


def process_job(
    db: Session,
    storage: LocalFileStorage,
    get_service: Callable[[], ExtractionService],
    job_id: uuid.UUID,
    *,
    is_final_attempt: bool,
) -> JobStatus | None:
    """Process a job once. Returns the resulting status, or None if the job does not exist.

    Raises TransientJobError when the attempt should be retried.
    """
    job = db.get(ExtractionJob, job_id)
    if job is None:
        logger.warning("Job %s not found; skipping", job_id)
        return None
    if job.status in _TERMINAL_STATUSES:
        logger.info("Job %s already %s; skipping", job_id, job.status)
        return job.status

    job.status = JobStatus.PROCESSING
    job.attempts += 1
    db.commit()

    try:
        data = storage.read(job.storage_key)
        result = get_service().extract_document(data, job.original_filename, job.document_type)
    except FileNotFoundError:
        return _fail(db, job, "Uploaded file is missing from storage.")
    except (DocumentReadError, ExtractionError) as exc:
        return _fail(db, job, str(exc))
    except Exception as exc:
        if not is_transient_llm_error(exc):
            logger.exception("Unexpected error processing job %s", job_id)
            return _fail(db, job, "Internal error while processing the document.")
        if is_final_attempt:
            return _fail(
                db,
                job,
                f"LLM provider unavailable after {job.attempts} attempt(s) ({type(exc).__name__}).",
            )
        job.status = JobStatus.PENDING
        job.error_message = f"Attempt {job.attempts} failed ({type(exc).__name__}); retrying."
        db.commit()
        raise TransientJobError(str(exc)) from exc

    job.status = JobStatus.COMPLETED
    job.result = result.model_dump(mode="json")
    job.error_message = None
    db.commit()
    logger.info("Job %s completed after %d attempt(s)", job_id, job.attempts)
    return JobStatus.COMPLETED


def _fail(db: Session, job: ExtractionJob, message: str) -> JobStatus:
    job.status = JobStatus.FAILED
    job.error_message = message
    db.commit()
    logger.warning("Job %s failed: %s", job.id, message)
    return JobStatus.FAILED
