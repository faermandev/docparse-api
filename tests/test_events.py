import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import jobs as jobs_api
from app.api.deps import get_session_factory
from app.models import DocumentType, ExtractionJob, JobStatus
from app.schemas.events import JobEvent
from app.schemas.extraction import Resume, ResumeContact
from app.services.api_keys import create_api_key
from app.services.events import (
    InMemoryJobEventBroker,
    RedisJobEventBroker,
    _RedisSubscription,
    channel_for,
    get_event_broker,
    snapshot_event,
)
from app.services.extraction import ExtractionError, ExtractionService
from app.services.processing import JobNotifier, TransientJobError, process_job
from app.services.storage import InMemoryStorage


def _no_webhook(_: uuid.UUID) -> None:
    pass


PDF_BYTES = b"%PDF-1.7\n%fake\n%%EOF"
RESUME = Resume(
    full_name="Ana Silva",
    summary=None,
    contact=ResumeContact(email=None, phone=None, location=None, links=[]),
    experience=[],
    projects=[],
    education=[],
    skills=["Python"],
)


def parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse a text/event-stream body into {event, id, data} dicts (comments are skipped)."""
    events = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            fields[name] = json.loads(value) if name == "data" else value
        if fields:
            events.append(fields)
    return events


def _event(stage: str, status: JobStatus, **extra: Any) -> JobEvent:
    return JobEvent(stage=stage, status=status, attempt=1, **extra)  # type: ignore[arg-type]


@pytest.fixture
def broker() -> InMemoryJobEventBroker:
    return InMemoryJobEventBroker()


@pytest.fixture
def sse_client(
    client: TestClient, broker: InMemoryJobEventBroker, db_session: Session
) -> TestClient:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_event_broker] = lambda: broker
    overrides[get_session_factory] = lambda: lambda: contextlib.nullcontext(db_session)
    return client


def _create_job(client: TestClient, db_session: Session) -> ExtractionJob:
    body = client.post(
        "/jobs",
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
        data={"document_type": "resume"},
    ).json()
    job = db_session.get(ExtractionJob, uuid.UUID(body["id"]))
    assert job is not None
    return job


def _stream(client: TestClient, job_id: uuid.UUID, **kwargs: Any) -> list[dict[str, Any]]:
    response = client.get(f"/jobs/{job_id}/events", **kwargs)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


# GET /jobs/{id}/events


def test_finished_job_streams_one_final_event_and_closes(
    sse_client: TestClient, db_session: Session
) -> None:
    job = _create_job(sse_client, db_session)
    job.status, job.attempts, job.result = JobStatus.COMPLETED, 1, {"full_name": "Ana"}
    db_session.commit()

    (event,) = _stream(sse_client, job.id)

    assert event["event"] == "completed"
    assert event["id"] == "1"
    assert event["data"]["job"]["result"] == {"full_name": "Ana"}


def test_live_events_are_streamed_in_order_until_the_job_finishes(
    sse_client: TestClient, db_session: Session, broker: InMemoryJobEventBroker
) -> None:
    job = _create_job(sse_client, db_session)
    other_job_id = uuid.uuid4()

    def worker_progress(job_id: uuid.UUID) -> None:
        broker.publish(other_job_id, _event("failed", JobStatus.FAILED))  # not ours: ignored
        for stage in ("processing", "reading", "extracting"):
            broker.publish(job_id, _event(stage, JobStatus.PROCESSING))
        broker.publish(job_id, _event("completed", JobStatus.COMPLETED))

    broker.on_subscribe = worker_progress

    events = _stream(sse_client, job.id)

    assert [e["event"] for e in events] == [
        "queued",
        "processing",
        "reading",
        "extracting",
        "completed",
    ]
    assert [e["id"] for e in events] == ["1", "2", "3", "4", "5"]


def test_pending_job_after_a_failed_attempt_starts_as_retrying(
    sse_client: TestClient, db_session: Session, broker: InMemoryJobEventBroker
) -> None:
    job = _create_job(sse_client, db_session)
    job.attempts, job.error_message = 1, "Attempt 1 failed (TimeoutError); retrying."
    db_session.commit()
    broker.on_subscribe = lambda job_id: broker.publish(job_id, _event("failed", JobStatus.FAILED))

    first, _ = _stream(sse_client, job.id)

    assert first["event"] == "retrying"
    assert first["data"]["message"] == "Attempt 1 failed (TimeoutError); retrying."


def test_stream_rechecks_the_database_when_events_stop(
    sse_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the final event is lost (e.g. Redis restarted), the stream still ends."""
    job = _create_job(sse_client, db_session)
    monkeypatch.setattr(jobs_api, "SNAPSHOT_RECHECK_SECONDS", 0.05)
    loads = 0

    @contextlib.contextmanager
    def session_factory() -> Iterator[Session]:
        nonlocal loads
        loads += 1
        if loads == 2:  # the worker finished while its events went missing
            job.status = JobStatus.COMPLETED
            db_session.commit()
        yield db_session

    sse_client.app.dependency_overrides[get_session_factory] = lambda: session_factory  # type: ignore[attr-defined]

    events = _stream(sse_client, job.id)

    assert [e["event"] for e in events] == ["queued", "completed"]


def test_events_require_an_api_key(sse_client: TestClient, db_session: Session) -> None:
    job = _create_job(sse_client, db_session)

    response = sse_client.get(f"/jobs/{job.id}/events", headers={"X-API-Key": ""})

    assert response.status_code == 401


def test_events_of_another_clients_job_are_not_found(
    sse_client: TestClient, db_session: Session
) -> None:
    job = _create_job(sse_client, db_session)
    job.api_key_id = create_api_key(db_session, "someone-else")[0].id
    db_session.commit()

    response = sse_client.get(f"/jobs/{job.id}/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# Snapshots


@pytest.mark.parametrize(
    ("status", "attempts", "stage"),
    [
        (JobStatus.PENDING, 0, "queued"),
        (JobStatus.PENDING, 2, "retrying"),
        (JobStatus.PROCESSING, 1, "processing"),
        (JobStatus.COMPLETED, 1, "completed"),
        (JobStatus.FAILED, 1, "failed"),
    ],
)
def test_snapshot_event_maps_database_state(
    sse_client: TestClient,
    db_session: Session,
    status: JobStatus,
    attempts: int,
    stage: str,
) -> None:
    job = _create_job(sse_client, db_session)
    job.status, job.attempts = status, attempts

    event = snapshot_event(job)

    assert event.stage == stage
    assert (event.job is not None) == event.is_terminal


# Events published by process_job


@pytest.fixture
def job(db_session: Session, storage: InMemoryStorage, owner_id: uuid.UUID) -> ExtractionJob:
    job_id = uuid.uuid4()
    storage.save(f"{job_id}.pdf", PDF_BYTES, "application/pdf")
    job = ExtractionJob(
        id=job_id,
        api_key_id=owner_id,
        document_type=DocumentType.RESUME,
        original_filename="cv.pdf",
        storage_key=f"{job_id}.pdf",
    )
    db_session.add(job)
    db_session.commit()
    return job


def _service(side_effect: Any) -> MagicMock:
    service = MagicMock(spec=ExtractionService)
    service.extract_document.side_effect = side_effect
    return service


def _process(
    db: Session,
    storage: InMemoryStorage,
    service: MagicMock,
    job: ExtractionJob,
    broker: InMemoryJobEventBroker,
) -> JobStatus | None:
    return process_job(
        db,
        storage,
        lambda: service,
        job.id,
        is_final_attempt=False,
        notifier=JobNotifier(publish=broker.publish, enqueue_webhook=_no_webhook),
    )


def _stages(broker: InMemoryJobEventBroker) -> list[str]:
    return [event.stage for _, event in broker.published]


def test_successful_processing_publishes_each_step(
    db_session: Session, storage: InMemoryStorage, job: ExtractionJob
) -> None:
    broker = InMemoryJobEventBroker()

    def extract(*_: Any, on_progress: Any) -> Resume:
        on_progress("reading")
        on_progress("extracting")
        return RESUME

    _process(db_session, storage, _service(extract), job, broker)

    assert _stages(broker) == ["processing", "reading", "extracting", "completed"]
    assert {job_id for job_id, _ in broker.published} == {job.id}
    final = broker.published[-1][1]
    assert final.job is not None
    assert final.job.result == RESUME.model_dump(mode="json")
    assert broker.published[0][1].message is None


def test_permanent_failure_publishes_failed_with_message(
    db_session: Session, storage: InMemoryStorage, job: ExtractionJob
) -> None:
    broker = InMemoryJobEventBroker()

    _process(db_session, storage, _service(ExtractionError("bad output")), job, broker)

    assert _stages(broker) == ["processing", "failed"]
    failed = broker.published[-1][1]
    assert failed.message == "bad output"
    assert failed.job is not None


def test_transient_failure_publishes_retrying(
    db_session: Session, storage: InMemoryStorage, job: ExtractionJob
) -> None:
    broker = InMemoryJobEventBroker()

    with pytest.raises(TransientJobError):
        _process(db_session, storage, _service(TimeoutError()), job, broker)

    assert _stages(broker) == ["processing", "retrying"]
    assert broker.published[-1][1].message == "Attempt 1 failed (TimeoutError); retrying."


def test_publishing_problems_do_not_affect_the_job(
    db_session: Session, storage: InMemoryStorage, job: ExtractionJob
) -> None:
    def broken_publish(*_: Any) -> None:
        raise RuntimeError("redis is down")

    status = process_job(
        db_session,
        storage,
        lambda: _service(lambda *_, **__: RESUME),
        job.id,
        is_final_attempt=False,
        notifier=JobNotifier(publish=broken_publish, enqueue_webhook=_no_webhook),
    )

    assert status == JobStatus.COMPLETED
    assert job.status == JobStatus.COMPLETED


# RedisJobEventBroker (clients mocked)


def test_redis_broker_publishes_json_on_the_job_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_, **__: client)
    job_id = uuid.uuid4()
    event = _event("reading", JobStatus.PROCESSING)

    RedisJobEventBroker("redis://test").publish(job_id, event)

    channel, payload = client.publish.call_args.args
    assert channel == channel_for(job_id) == f"job-events:{job_id}"
    assert JobEvent.model_validate_json(payload) == event


def test_redis_broker_swallows_publish_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.publish.side_effect = redis.ConnectionError("down")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_, **__: client)

    RedisJobEventBroker("redis://test").publish(
        uuid.uuid4(), _event("reading", JobStatus.PROCESSING)
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_redis_subscription_decodes_messages_and_times_out() -> None:
    event = _event("extracting", JobStatus.PROCESSING)
    pending = [{"type": "message", "data": event.model_dump_json().encode()}]

    async def get_message(*, ignore_subscribe_messages: bool, timeout: float) -> Any:
        if pending:
            return pending.pop()
        await asyncio.sleep(timeout)  # like redis: wait up to `timeout`, then None
        return None

    pubsub = MagicMock()
    pubsub.get_message = get_message
    subscription = _RedisSubscription(pubsub)

    assert await subscription.get(timeout=1) == event
    assert await subscription.get(timeout=0.01) is None
