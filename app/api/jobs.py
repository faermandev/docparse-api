import itertools
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import (
    CurrentApiKey,
    DbSession,
    EnqueuerDep,
    EventBrokerDep,
    OwnedJob,
    SessionFactory,
    SessionFactoryDep,
    SettingsDep,
    StorageDep,
)
from app.models import DocumentType, JobStatus
from app.schemas.errors import ErrorResponse
from app.schemas.events import JobEvent
from app.schemas.jobs import JobList, JobRead
from app.services import jobs as job_service
from app.services.events import snapshot_event
from app.services.jobs import CONTENT_TYPES

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Re-read the job from the database after this long without events, in case events were
# lost (worker crash, Redis restart) and the job has in fact finished.
SNAPSHOT_RECHECK_SECONDS = 30.0

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **_ERROR_RESPONSES,
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_411_LENGTH_REQUIRED: {"model": ErrorResponse},
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
def create_job(
    api_key: CurrentApiKey,
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
        owner_id=api_key.id,
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
def get_job(api_key: CurrentApiKey, db: DbSession, job_id: uuid.UUID) -> JobRead:
    """Return a job's current status and, once completed, its extraction result."""
    return JobRead.model_validate(job_service.get_job(db, job_id, owner_id=api_key.id))


@router.get(
    "/{job_id}/file",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {content_type: {} for content_type in set(CONTENT_TYPES.values())},
            "description": "The originally uploaded file.",
        },
        **_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
def download_job_file(
    api_key: CurrentApiKey, db: DbSession, storage: StorageDep, job_id: uuid.UUID
) -> Response:
    """Download the original file uploaded for a job."""
    job = job_service.get_job(db, job_id, owner_id=api_key.id)
    data, content_type = job_service.read_job_file(storage, job)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": _attachment_header(job.original_filename)},
    )


@router.get(
    "/{job_id}/events",
    response_class=EventSourceResponse,
    responses={**_ERROR_RESPONSES, status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def stream_job_events(
    job: OwnedJob, broker: EventBrokerDep, session_factory: SessionFactoryDep
) -> AsyncIterator[ServerSentEvent]:
    """Stream a job's progress as Server-Sent Events until it completes or fails.

    The first event is the job's current state; later events report each processing step
    (`processing`, `reading`, `transcribing`, `extracting`, `retrying`). The final `completed`
    or `failed` event carries the full job, including the result, and then the stream ends.
    """
    job_id, owner_id = job.id, job.api_key_id
    sequence = itertools.count(1)

    def sse(event: JobEvent) -> ServerSentEvent:
        return ServerSentEvent(event=event.stage, data=event, id=str(next(sequence)))

    # Subscribe before reading the snapshot, so no event can slip in between the two.
    async with broker.subscribe(job_id) as subscription:
        event = await run_in_threadpool(_load_snapshot, session_factory, job_id, owner_id)
        yield sse(event)
        while not event.is_terminal:
            received = await subscription.get(timeout=SNAPSHOT_RECHECK_SECONDS)
            if received is None:
                snapshot = await run_in_threadpool(
                    _load_snapshot, session_factory, job_id, owner_id
                )
                if not snapshot.is_terminal:
                    continue
                received = snapshot
            event = received
            yield sse(event)


def _load_snapshot(
    session_factory: SessionFactory, job_id: uuid.UUID, owner_id: uuid.UUID
) -> JobEvent:
    with session_factory() as db:
        job = job_service.get_job(db, job_id, owner_id=owner_id)
        db.refresh(job)
        return snapshot_event(job)


def _attachment_header(filename: str) -> str:
    quoted = quote(filename)
    if quoted == filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=utf-8''{quoted}"  # RFC 5987, for non-ASCII names


@router.get("", response_model=JobList, responses=_ERROR_RESPONSES)
def list_jobs(
    api_key: CurrentApiKey,
    db: DbSession,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    """List your jobs, newest first, optionally filtered by status."""
    items, total = job_service.list_jobs(
        db, owner_id=api_key.id, status=status_filter, limit=limit, offset=offset
    )
    return JobList(
        items=[JobRead.model_validate(job) for job in items],
        total=total,
        limit=limit,
        offset=offset,
    )
