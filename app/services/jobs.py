import uuid
from collections.abc import Callable, Sequence
from pathlib import PurePath
from typing import BinaryIO

import structlog
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
from app.services.storage import ObjectNotFoundError, Storage

logger = structlog.get_logger(__name__)

CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(CONTENT_TYPES)
MAX_FILENAME_LENGTH = 255


def detect_content_type(data: bytes) -> str | None:
    """Identify an allowed file type from its leading "magic" bytes."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def create_job(
    db: Session,
    storage: Storage,
    *,
    owner_id: uuid.UUID,
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

    # The extension is only a claim; the content must actually be that type.
    content_type = CONTENT_TYPES[extension]
    detected = detect_content_type(data)
    if detected != content_type:
        raise UnsupportedFileTypeError(
            f"File content does not match its {extension!r} extension.",
            details={"expected": content_type, "detected": detected},
        )

    job_id = uuid.uuid4()
    storage_key = f"{job_id}{extension}"
    storage.save(storage_key, data, content_type)

    job = ExtractionJob(
        id=job_id,
        api_key_id=owner_id,
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
    logger.info(
        "job_created",
        job_id=str(job.id),
        api_key_id=str(owner_id),
        document_type=document_type.value,
        size_bytes=len(data),
    )
    return job


def get_job(db: Session, job_id: uuid.UUID, *, owner_id: uuid.UUID) -> ExtractionJob:
    """Return the caller's job. Other clients' jobs are reported as not found."""
    job = db.scalar(
        select(ExtractionJob).where(
            ExtractionJob.id == job_id, ExtractionJob.api_key_id == owner_id
        )
    )
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job


def read_job_file(storage: Storage, job: ExtractionJob) -> tuple[bytes, str]:
    """Return the original upload and its content type."""
    try:
        data = storage.read(job.storage_key)
    except ObjectNotFoundError:
        raise NotFoundError(f"File for job {job.id} is no longer available.") from None
    extension = PurePath(job.storage_key).suffix.lower()
    return data, CONTENT_TYPES.get(extension, "application/octet-stream")


def list_jobs(
    db: Session,
    *,
    owner_id: uuid.UUID,
    status: JobStatus | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[ExtractionJob], int]:
    """Return a page of the caller's jobs (newest first) and the total matching the filter."""
    query = select(ExtractionJob).where(ExtractionJob.api_key_id == owner_id)
    if status is not None:
        query = query.where(ExtractionJob.status == status)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(
        query.order_by(ExtractionJob.created_at.desc(), ExtractionJob.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return items, total
