import contextlib
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx2
import openai
import pytest
from sqlalchemy.orm import Session

from app.models import DocumentType, ExtractionJob, JobStatus
from app.schemas.extraction import Resume, ResumeContact
from app.services.document_reader import DocumentReadError
from app.services.events import InMemoryJobEventBroker
from app.services.extraction import ExtractionError, ExtractionService
from app.services.llm import is_transient_llm_error
from app.services.processing import JobNotifier, TransientJobError, process_job
from app.services.storage import InMemoryStorage, StorageUnavailableError
from app.workers import tasks
from app.workers.tasks import process_job_task, retry_countdown


def _no_webhook(_: uuid.UUID) -> None:
    pass


RESUME = Resume(
    full_name="Ana Silva",
    summary=None,
    contact=ResumeContact(email=None, phone=None, location=None, links=[]),
    experience=[],
    projects=[],
    education=[],
    skills=["Python"],
)
_REQUEST = httpx2.Request("POST", "https://llm.example.com/v1")


def _status_error(sdk: Any, code: int) -> Exception:
    """Build a real SDK status error (openai.* or anthropic.*) for a given HTTP code."""
    error: Exception = sdk("error", response=httpx2.Response(code, request=_REQUEST), body=None)
    return error


@pytest.fixture
def job(db_session: Session, storage: InMemoryStorage, owner_id: uuid.UUID) -> ExtractionJob:
    job_id = uuid.uuid4()
    storage.save(f"{job_id}.pdf", b"%PDF-fake", "application/pdf")
    job = ExtractionJob(
        id=job_id,
        api_key_id=owner_id,
        status=JobStatus.PENDING,
        document_type=DocumentType.RESUME,
        original_filename="cv.pdf",
        storage_key=f"{job_id}.pdf",
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.fixture
def service() -> MagicMock:
    mock = MagicMock(spec=ExtractionService)
    mock.extract_document.return_value = RESUME
    return mock


def _run(
    db: Session,
    storage: InMemoryStorage,
    service: MagicMock,
    job_id: uuid.UUID,
    *,
    is_final_attempt: bool = False,
) -> JobStatus | None:
    return process_job(
        db,
        storage,
        lambda: service,
        job_id,
        is_final_attempt=is_final_attempt,
        notifier=JobNotifier(publish=lambda *_: None, enqueue_webhook=_no_webhook),
    )


# process_job: state flow


def test_successful_job_is_completed_with_result(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    status = _run(db_session, storage, service, job.id)

    assert status == JobStatus.COMPLETED
    assert job.status == JobStatus.COMPLETED
    assert job.result == RESUME.model_dump(mode="json")
    assert job.error_message is None
    assert job.attempts == 1
    assert service.extract_document.call_args.args == (b"%PDF-fake", "cv.pdf", DocumentType.RESUME)


@pytest.mark.parametrize(
    "error",
    [DocumentReadError("PDF has no pages."), ExtractionError("Document has no text.")],
)
def test_permanent_errors_fail_the_job_with_their_message(
    db_session: Session,
    storage: InMemoryStorage,
    service: MagicMock,
    job: ExtractionJob,
    error: Exception,
) -> None:
    service.extract_document.side_effect = error

    status = _run(db_session, storage, service, job.id)

    assert status == JobStatus.FAILED
    assert job.error_message == str(error)
    assert job.result is None


def test_missing_file_fails_the_job(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    storage.delete(job.storage_key)

    assert _run(db_session, storage, service, job.id) == JobStatus.FAILED
    assert job.error_message == "Uploaded file is missing from storage."


def test_transient_error_returns_job_to_pending_and_asks_for_retry(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    service.extract_document.side_effect = TimeoutError("slow provider")

    with pytest.raises(TransientJobError):
        _run(db_session, storage, service, job.id)

    assert job.status == JobStatus.PENDING
    assert job.attempts == 1
    assert job.error_message == "Attempt 1 failed (TimeoutError); retrying."


def test_storage_outage_is_retried(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    def unavailable(_: str) -> bytes:
        raise StorageUnavailableError("File storage is unavailable; try again later.")

    storage.read = unavailable  # type: ignore[method-assign]

    with pytest.raises(TransientJobError):
        _run(db_session, storage, service, job.id)

    assert job.status == JobStatus.PENDING


def test_transient_error_on_final_attempt_fails_the_job(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    service.extract_document.side_effect = _status_error(openai.RateLimitError, 429)

    status = _run(db_session, storage, service, job.id, is_final_attempt=True)

    assert status == JobStatus.FAILED
    assert (
        job.error_message == "Service temporarily unavailable after 1 attempt(s) (RateLimitError)."
    )


def test_unexpected_error_fails_the_job_without_leaking_details(
    db_session: Session, storage: InMemoryStorage, service: MagicMock, job: ExtractionJob
) -> None:
    service.extract_document.side_effect = ValueError("secret internal detail")

    assert _run(db_session, storage, service, job.id) == JobStatus.FAILED
    assert job.error_message == "Internal error while processing the document."


@pytest.mark.parametrize("terminal", [JobStatus.COMPLETED, JobStatus.FAILED])
def test_finished_jobs_are_not_reprocessed(
    db_session: Session,
    storage: InMemoryStorage,
    service: MagicMock,
    job: ExtractionJob,
    terminal: JobStatus,
) -> None:
    job.status = terminal
    db_session.commit()

    assert _run(db_session, storage, service, job.id) == terminal
    assert job.attempts == 0
    service.extract_document.assert_not_called()


def test_unknown_job_is_skipped(
    db_session: Session, storage: InMemoryStorage, service: MagicMock
) -> None:
    assert _run(db_session, storage, service, uuid.uuid4()) is None


# Error classification


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (_status_error(openai.RateLimitError, 429), True),
        (_status_error(openai.InternalServerError, 503), True),
        (_status_error(anthropic.RateLimitError, 429), True),
        (_status_error(anthropic.InternalServerError, 529), True),
        (openai.APITimeoutError(request=_REQUEST), True),
        (anthropic.APIConnectionError(request=_REQUEST), True),
        (TimeoutError(), True),
        (_status_error(openai.AuthenticationError, 401), False),
        (_status_error(openai.BadRequestError, 400), False),
        (_status_error(anthropic.NotFoundError, 404), False),
        (ValueError(), False),
    ],
)
def test_is_transient_llm_error(error: Exception, transient: bool) -> None:
    assert is_transient_llm_error(error) is transient


@pytest.mark.parametrize("retries", [0, 1, 2, 10])
def test_retry_countdown_grows_exponentially_with_jitter_and_cap(retries: int) -> None:
    delay = retry_countdown(retries, base_seconds=10)

    expected = min(10 * 2**retries, tasks.MAX_RETRY_DELAY_SECONDS)
    assert expected <= delay <= expected + 10


# Celery task (run eagerly: retries execute inline and countdowns are ignored)


@pytest.fixture
def task_env(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    storage: InMemoryStorage,
    service: MagicMock,
) -> None:
    @contextlib.contextmanager
    def session_factory() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setattr(tasks, "get_extraction_service", lambda: service)
    monkeypatch.setattr(tasks, "get_event_broker", InMemoryJobEventBroker)


@pytest.mark.usefixtures("task_env")
def test_task_retries_transient_errors_then_completes(
    service: MagicMock, job: ExtractionJob
) -> None:
    service.extract_document.side_effect = [TimeoutError(), RESUME]

    process_job_task.apply(args=[str(job.id)])

    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 2


@pytest.mark.usefixtures("task_env")
def test_task_gives_up_after_max_retries(service: MagicMock, job: ExtractionJob) -> None:
    service.extract_document.side_effect = TimeoutError()

    process_job_task.apply(args=[str(job.id)])

    max_attempts = process_job_task.max_retries + 1
    assert job.status == JobStatus.FAILED
    assert job.attempts == max_attempts
    assert service.extract_document.call_count == max_attempts
    assert job.error_message == (
        f"Service temporarily unavailable after {max_attempts} attempt(s) (TimeoutError)."
    )


@pytest.mark.usefixtures("task_env")
def test_task_does_not_retry_permanent_errors(service: MagicMock, job: ExtractionJob) -> None:
    service.extract_document.side_effect = ExtractionError("bad output")

    process_job_task.apply(args=[str(job.id)])

    assert job.status == JobStatus.FAILED
    assert service.extract_document.call_count == 1
