import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.api.deps import DbSession, EnqueuerDep, SettingsDep, StorageDep
from app.models import DocumentType, JobStatus
from app.schemas.errors import ErrorResponse
from app.schemas.jobs import JobList, JobRead
from app.services import jobs as job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **_ERROR_RESPONSES,
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
def create_job(
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    enqueue: EnqueuerDep,
    file: Annotated[UploadFile, File(description="PDF or image (PNG, JPEG, WEBP).")],
    document_type: Annotated[DocumentType, Form()],
) -> JobRead:
    """Upload a document and queue it for asynchronous extraction."""
    job = job_service.create_job(
        db,
        storage,
        file=file.file,
        filename=file.filename or "",
        document_type=document_type,
        max_size_bytes=settings.max_upload_size_bytes,
        enqueue=enqueue,
    )
    return JobRead.model_validate(job)


@router.get(
    "/{job_id}",
    response_model=JobRead,
    responses={**_ERROR_RESPONSES, status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def get_job(db: DbSession, job_id: uuid.UUID) -> JobRead:
    """Return a job's current status and, once completed, its extraction result."""
    return JobRead.model_validate(job_service.get_job(db, job_id))


@router.get("", response_model=JobList, responses=_ERROR_RESPONSES)
def list_jobs(
    db: DbSession,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    """List jobs, newest first, optionally filtered by status."""
    items, total = job_service.list_jobs(db, status=status_filter, limit=limit, offset=offset)
    return JobList(
        items=[JobRead.model_validate(job) for job in items],
        total=total,
        limit=limit,
        offset=offset,
    )
