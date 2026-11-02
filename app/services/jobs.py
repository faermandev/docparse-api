import uuid
from collections.abc import Callable, Sequence
from pathlib import PurePath
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    EmptyFileError,
    FileTooLargeError,
    NotFoundError,
    QueueUnavailableError,
    UnsupportedFileTypeError,
)
from app.models import DocumentType, ExtractionJob, JobStatus
from app.services.storage import LocalFileStorage

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp"})
MAX_FILENAME_LENGTH = 255


def create_job(
    db: Session,
    storage: LocalFileStorage,
    *,
    file: BinaryIO,
    filename: str,
    document_type: DocumentType,
    max_size_bytes: int,
    enqueue: Callable[[uuid.UUID], None],
) -> ExtractionJob:
    """Validate the upload, persist the file, create a pending job, and enqueue it."""
    safe_name = PurePath(filename).name[:MAX_FILENAME_LENGTH]
    extension = PurePath(safe_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension {extension or '(none)'!r}.",
            details={"allowed": sorted(ALLOWED_EXTENSIONS)},
        )

    # Read one byte past the limit so oversize uploads are detected without loading them fully.
    data = file.read(max_size_bytes + 1)
    if not data:
        raise EmptyFileError("Uploaded file is empty.")
    if len(data) > max_size_bytes:
        raise FileTooLargeError(
            "Uploaded file exceeds the maximum allowed size.",
            details={"max_size_bytes": max_size_bytes},
        )

    job_id = uuid.uuid4()
    storage_key = f"{job_id}{extension}"
    storage.save(storage_key, data)

    job = ExtractionJob(
        id=job_id,
        status=JobStatus.PENDING,
        document_type=document_type,
        original_filename=safe_name,
        storage_key=storage_key,
    )
    try:
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(storage_key)
        raise

    # Enqueue only after commit, so the worker always finds the row.
    try:
        enqueue(job.id)
    except QueueUnavailableError:
        job.status = JobStatus.FAILED
        job.error_message = "Could not enqueue the job for processing."
        db.commit()
        raise
    db.refresh(job)
    return job


def get_job(db: Session, job_id: uuid.UUID) -> ExtractionJob:
    job = db.get(ExtractionJob, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job


def list_jobs(
    db: Session, *, status: JobStatus | None, limit: int, offset: int
) -> tuple[Sequence[ExtractionJob], int]:
    """Return a page of jobs (newest first) and the total count matching the filter."""
    query = select(ExtractionJob)
    if status is not None:
        query = query.where(ExtractionJob.status == status)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(
        query.order_by(ExtractionJob.created_at.desc(), ExtractionJob.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return items, total
