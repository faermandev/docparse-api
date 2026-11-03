import contextlib
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import (
    ApiKey,
    DocumentType,
    ExtractionJob,
    JobStatus,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEventType,
)
from app.schemas.extraction import Resume, ResumeContact
from app.services import webhooks as webhook_service
from app.services.api_keys import create_api_key
from app.services.extraction import ExtractionError, ExtractionService
from app.services.processing import JobNotifier, process_job
from app.services.storage import InMemoryStorage
from app.services.webhooks import (
    EVENT_HEADER,
    ID_HEADER,
    SIGNATURE_HEADER,
    InvalidWebhookUrlError,
    TransientDeliveryError,
    deliver_webhook,
    find_stale_deliveries,
    record_job_webhook,
    signature_header,
    validate_destination,
    verify_signature,
)
from app.workers import tasks

PUBLIC_IP = "93.184.216.34"
HOOK_URL = "https://hooks.example.com/docs"
SECRET = "whsec_test-secret"
RESUME = Resume(
    full_name="Ana Silva",
    summary=None,
    contact=ResumeContact(email=None, phone=None, location=None, links=[]),
    experience=[],
    projects=[],
    education=[],
    skills=["Python"],
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real DNS in tests: every host resolves to a public address unless a test says so."""
    monkeypatch.setattr(webhook_service, "resolve_host", lambda host, port: [PUBLIC_IP])


def _resolver(*addresses: str) -> Callable[[str, int], list[str]]:
    return lambda host, port: list(addresses)


# Destination validation (SSRF)


def test_public_https_url_is_accepted() -> None:
    validate_destination(HOOK_URL, allow_insecure=False)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://hooks.example.com/x", "https"),
        ("ftp://hooks.example.com/x", "https"),
        ("https:///x", "no host"),
        ("https://user:pass@hooks.example.com/x", "credentials"),
        ("https://hooks.example.com:99999/x", "invalid port"),
        ("https://hooks.example.com/" + "a" * 2048, "longer than"),
    ],
)
def test_malformed_or_unencrypted_urls_are_rejected(url: str, message: str) -> None:
    with pytest.raises(InvalidWebhookUrlError, match=message):
        validate_destination(url, allow_insecure=False)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "172.18.0.5",  # private (e.g. a Docker network)
        "192.168.0.10",  # private
        "169.254.169.254",  # cloud metadata endpoint
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 unique local
        "fe80::1%en0",  # IPv6 link-local with zone
        "::ffff:10.0.0.1",  # IPv4-mapped private
    ],
)
def test_urls_resolving_to_non_public_addresses_are_rejected(address: str) -> None:
    with pytest.raises(InvalidWebhookUrlError, match="private, loopback or reserved"):
        validate_destination(HOOK_URL, allow_insecure=False, resolve=_resolver(address))


def test_any_non_public_address_among_several_is_rejected() -> None:
    with pytest.raises(InvalidWebhookUrlError):
        validate_destination(
            HOOK_URL, allow_insecure=False, resolve=_resolver(PUBLIC_IP, "10.0.0.1")
        )


def test_unresolvable_host_is_rejected() -> None:
    def fail(host: str, port: int) -> list[str]:
        raise OSError("nodename nor servname provided")

    with pytest.raises(InvalidWebhookUrlError, match="could not be resolved"):
        validate_destination(HOOK_URL, allow_insecure=False, resolve=fail)


def test_insecure_mode_allows_http_and_local_hosts_without_resolving() -> None:
    resolve = MagicMock()

    validate_destination("http://localhost:8099/hook", allow_insecure=True, resolve=resolve)

    resolve.assert_not_called()


# Signatures


def test_signature_round_trip() -> None:
    body = b'{"id":"1"}'
    header = signature_header(SECRET, body, timestamp=1_700_000_000)

    assert verify_signature(SECRET, header, body, now=1_700_000_010)


@pytest.mark.parametrize(
    ("secret", "body", "now"),
    [
        ("whsec_other", b'{"id":"1"}', 1_700_000_010),  # wrong secret
        (SECRET, b'{"id":"2"}', 1_700_000_010),  # tampered body
        (SECRET, b'{"id":"1"}', 1_700_001_000),  # too old (replay)
    ],
)
def test_signature_rejects_wrong_secret_tampering_and_replays(
    secret: str, body: bytes, now: int
) -> None:
    header = signature_header(SECRET, b'{"id":"1"}', timestamp=1_700_000_000)

    assert not verify_signature(secret, header, body, now=now)


@pytest.mark.parametrize("header", ["", "v1=abc", "t=notanumber,v1=abc", "garbage"])
def test_signature_rejects_malformed_headers(header: str) -> None:
    assert not verify_signature(SECRET, header, b"{}")


# Configuration API


def test_webhook_is_not_configured_by_default(client: TestClient) -> None:
    response = client.get("/webhook")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_register_change_and_remove_webhook(client: TestClient) -> None:
    created = client.put("/webhook", json={"url": HOOK_URL})
    assert created.status_code == 200
    config = created.json()
    assert config["url"] == HOOK_URL
    assert config["secret"].startswith("whsec_")
    assert config["events"] == ["job.completed", "job.failed"]
    assert client.get("/webhook").json() == config

    changed = client.put("/webhook", json={"url": "https://hooks.example.com/v2"}).json()
    assert changed["secret"] == config["secret"]  # changing the URL keeps the secret

    assert client.delete("/webhook").status_code == 204
    assert client.get("/webhook").status_code == 404

    recreated = client.put("/webhook", json={"url": HOOK_URL}).json()
    assert recreated["secret"] != config["secret"]  # re-registering issues a new secret


def test_register_rejects_unsafe_urls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_service, "resolve_host", _resolver("169.254.169.254"))

    response = client.put("/webhook", json={"url": HOOK_URL})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_webhook_url"
    assert client.get("/webhook").status_code == 404


def test_insecure_setting_allows_local_urls(client: TestClient) -> None:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_settings] = lambda: Settings(_env_file=None, webhook_allow_insecure_urls=True)

    response = client.put("/webhook", json={"url": "http://host.docker.internal:8099/hook"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/webhook"),
        ("PUT", "/webhook"),
        ("DELETE", "/webhook"),
        ("GET", "/webhook/deliveries"),
    ],
)
def test_webhook_endpoints_require_an_api_key(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={"url": HOOK_URL}, headers={"X-API-Key": ""})

    assert response.status_code == 401


# Outbox: deliveries recorded when jobs finish


@pytest.fixture
def owner(db_session: Session, api_key: tuple[ApiKey, str]) -> ApiKey:
    key = api_key[0]
    key.webhook_url, key.webhook_secret = HOOK_URL, SECRET
    db_session.commit()
    return key


def _job(db: Session, storage: InMemoryStorage, owner_id: uuid.UUID) -> ExtractionJob:
    job_id = uuid.uuid4()
    storage.save(f"{job_id}.pdf", b"%PDF-1.7", "application/pdf")
    job = ExtractionJob(
        id=job_id,
        api_key_id=owner_id,
        document_type=DocumentType.RESUME,
        original_filename="cv.pdf",
        storage_key=f"{job_id}.pdf",
    )
    db.add(job)
    db.commit()
    return job


def _process(
    db: Session, storage: InMemoryStorage, job: ExtractionJob, outcome: Any
) -> list[uuid.UUID]:
    service = MagicMock(spec=ExtractionService)
    service.extract_document.side_effect = outcome
    enqueued: list[uuid.UUID] = []
    process_job(
        db,
        storage,
        lambda: service,
        job.id,
        is_final_attempt=False,
        notifier=JobNotifier(publish=lambda *_: None, enqueue_webhook=enqueued.append),
    )
    return enqueued


def _deliveries(db: Session) -> list[WebhookDelivery]:
    return list(db.scalars(select(WebhookDelivery)))


def test_completed_job_records_and_enqueues_a_delivery(
    db_session: Session, storage: InMemoryStorage, owner: ApiKey
) -> None:
    job = _job(db_session, storage, owner.id)

    enqueued = _process(db_session, storage, job, lambda *_, **__: RESUME)

    (delivery,) = _deliveries(db_session)
    assert enqueued == [delivery.id]
    assert delivery.status == WebhookDeliveryStatus.PENDING
    assert delivery.event_type == WebhookEventType.JOB_COMPLETED
    assert delivery.url == HOOK_URL
    assert delivery.payload["id"] == str(delivery.id)
    assert delivery.payload["type"] == "job.completed"
    assert delivery.payload["data"]["job"]["result"] == RESUME.model_dump(mode="json")


def test_failed_job_records_a_job_failed_delivery(
    db_session: Session, storage: InMemoryStorage, owner: ApiKey
) -> None:
    job = _job(db_session, storage, owner.id)

    _process(db_session, storage, job, ExtractionError("bad output"))

    (delivery,) = _deliveries(db_session)
    assert delivery.event_type == WebhookEventType.JOB_FAILED
    assert delivery.payload["data"]["job"]["error_message"] == "bad output"


def test_no_delivery_without_a_webhook(
    db_session: Session, storage: InMemoryStorage, owner_id: uuid.UUID
) -> None:
    job = _job(db_session, storage, owner_id)

    enqueued = _process(db_session, storage, job, lambda *_, **__: RESUME)

    assert enqueued == []
    assert _deliveries(db_session) == []


def test_enqueue_failure_keeps_the_delivery_for_the_sweep(
    db_session: Session, storage: InMemoryStorage, owner: ApiKey
) -> None:
    job = _job(db_session, storage, owner.id)
    service = MagicMock(spec=ExtractionService)
    service.extract_document.return_value = RESUME

    def queue_down(_: uuid.UUID) -> None:
        raise ConnectionError("redis is down")

    status = process_job(
        db_session,
        storage,
        lambda: service,
        job.id,
        is_final_attempt=False,
        notifier=JobNotifier(publish=lambda *_: None, enqueue_webhook=queue_down),
    )

    assert status == JobStatus.COMPLETED
    (delivery,) = _deliveries(db_session)
    assert delivery.status == WebhookDeliveryStatus.PENDING


def test_record_job_webhook_is_a_no_op_for_keys_without_webhook(
    db_session: Session, storage: InMemoryStorage, owner_id: uuid.UUID
) -> None:
    assert record_job_webhook(db_session, _job(db_session, storage, owner_id)) is None


# Delivery


@pytest.fixture
def delivery(db_session: Session, storage: InMemoryStorage, owner: ApiKey) -> WebhookDelivery:
    job = _job(db_session, storage, owner.id)
    job.status = JobStatus.COMPLETED
    record = record_job_webhook(db_session, job)
    db_session.commit()
    assert record is not None
    return record


class Receiver:
    """Fake webhook endpoint: records requests and answers with scripted responses."""

    def __init__(self, *responses: int | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx2.Response(outcome, request=request)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


def _deliver(
    db: Session, delivery_id: uuid.UUID, receiver: Receiver, *, final: bool = False, **kw: Any
) -> WebhookDeliveryStatus | None:
    with receiver.client() as client:
        return deliver_webhook(
            db,
            delivery_id,
            client,
            is_final_attempt=final,
            retry_delay_seconds=30,
            allow_insecure=False,
            **kw,
        )


def test_successful_delivery_sends_a_signed_payload(
    db_session: Session, delivery: WebhookDelivery
) -> None:
    receiver = Receiver(200)

    status = _deliver(db_session, delivery.id, receiver)

    assert status == WebhookDeliveryStatus.DELIVERED
    assert delivery.status == WebhookDeliveryStatus.DELIVERED
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None
    (request,) = receiver.requests
    assert str(request.url) == HOOK_URL
    assert request.headers["content-type"] == "application/json"
    assert request.headers[ID_HEADER] == str(delivery.id)
    assert request.headers[EVENT_HEADER] == "job.completed"
    assert verify_signature(SECRET, request.headers[SIGNATURE_HEADER], request.content)
    assert json.loads(request.content) == delivery.payload


@pytest.mark.parametrize(
    "failure",
    [500, 503, 429, 408, httpx2.ConnectTimeout("slow"), httpx2.ConnectError("refused")],
    ids=str,
)
def test_transient_failures_are_retried(
    db_session: Session, delivery: WebhookDelivery, failure: int | Exception
) -> None:
    before = datetime.now(UTC)

    with pytest.raises(TransientDeliveryError):
        _deliver(db_session, delivery.id, Receiver(failure))

    assert delivery.status == WebhookDeliveryStatus.PENDING
    assert delivery.attempts == 1
    assert delivery.last_error
    assert delivery.next_attempt_at >= before + timedelta(seconds=29)


def test_transient_failure_on_final_attempt_gives_up(
    db_session: Session, delivery: WebhookDelivery
) -> None:
    status = _deliver(db_session, delivery.id, Receiver(500), final=True)

    assert status == WebhookDeliveryStatus.FAILED
    assert delivery.last_status_code == 500
    assert delivery.last_error == "Endpoint answered HTTP 500."


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (404, "Endpoint answered HTTP 404."),
        (401, "Endpoint answered HTTP 401."),
        (302, "Endpoint answered HTTP 302. Redirects are not followed."),
    ],
)
def test_client_errors_and_redirects_fail_without_retrying(
    db_session: Session, delivery: WebhookDelivery, code: int, error: str
) -> None:
    status = _deliver(db_session, delivery.id, Receiver(code))

    assert status == WebhookDeliveryStatus.FAILED
    assert delivery.last_error == error


def test_delivery_stops_if_the_webhook_was_removed(
    db_session: Session, delivery: WebhookDelivery, owner: ApiKey
) -> None:
    owner.webhook_url = owner.webhook_secret = None
    db_session.commit()
    receiver = Receiver(200)

    assert _deliver(db_session, delivery.id, receiver) == WebhookDeliveryStatus.FAILED
    assert receiver.requests == []
    assert delivery.last_error == "Webhook is no longer configured for this API key."


def test_delivery_rechecks_the_destination_before_sending(
    db_session: Session, delivery: WebhookDelivery
) -> None:
    """DNS may have changed since registration (DNS rebinding)."""
    receiver = Receiver(200)

    status = _deliver(db_session, delivery.id, receiver, resolve=_resolver("10.0.0.7"))

    assert status == WebhookDeliveryStatus.FAILED
    assert receiver.requests == []


def test_finished_or_unknown_deliveries_are_not_sent(
    db_session: Session, delivery: WebhookDelivery
) -> None:
    delivery.status = WebhookDeliveryStatus.DELIVERED
    db_session.commit()
    receiver = Receiver(200)

    assert _deliver(db_session, delivery.id, receiver) == WebhookDeliveryStatus.DELIVERED
    assert _deliver(db_session, uuid.uuid4(), receiver) is None
    assert receiver.requests == []


# Sweep of lost deliveries


def test_sweep_returns_only_overdue_pending_deliveries(
    db_session: Session, storage: InMemoryStorage, owner: ApiKey
) -> None:
    def make(status: WebhookDeliveryStatus, due_minutes_ago: int) -> WebhookDelivery:
        job = _job(db_session, storage, owner.id)
        job.status = JobStatus.COMPLETED
        record = record_job_webhook(db_session, job)
        assert record is not None
        record.status = status
        record.next_attempt_at = datetime.now(UTC) - timedelta(minutes=due_minutes_ago)
        db_session.commit()
        return record

    lost = make(WebhookDeliveryStatus.PENDING, 10)
    make(WebhookDeliveryStatus.PENDING, 0)  # just scheduled
    make(WebhookDeliveryStatus.DELIVERED, 10)
    make(WebhookDeliveryStatus.FAILED, 10)

    assert find_stale_deliveries(db_session, overdue_by=timedelta(minutes=2)) == [lost.id]
    # Re-enqueued ones get a fresh due time, so the next sweep leaves them alone.
    assert find_stale_deliveries(db_session, overdue_by=timedelta(minutes=2)) == []


# Deliveries API


def test_deliveries_are_listed_per_api_key(
    client: TestClient, db_session: Session, delivery: WebhookDelivery
) -> None:
    _, other_plaintext = create_api_key(db_session, "other-client")

    mine = client.get("/webhook/deliveries").json()["items"]
    theirs = client.get("/webhook/deliveries", headers={"X-API-Key": other_plaintext}).json()

    assert [item["id"] for item in mine] == [str(delivery.id)]
    assert mine[0]["status"] == "pending"
    assert mine[0]["event_type"] == "job.completed"
    assert theirs["items"] == []


# Celery tasks (run eagerly: retries execute inline and countdowns are ignored)


@pytest.fixture
def task_env(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    @contextlib.contextmanager
    def session_factory() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)


@pytest.mark.usefixtures("task_env")
def test_task_retries_until_the_endpoint_recovers(
    monkeypatch: pytest.MonkeyPatch, delivery: WebhookDelivery
) -> None:
    receiver = Receiver(500, 503, 200)
    monkeypatch.setattr(tasks, "http_client", receiver.client)

    tasks.deliver_webhook_task.apply(args=[str(delivery.id)])

    assert delivery.status == WebhookDeliveryStatus.DELIVERED
    assert delivery.attempts == 3
    # Every attempt carries the same id, so receivers can deduplicate.
    assert {request.headers[ID_HEADER] for request in receiver.requests} == {str(delivery.id)}


@pytest.mark.usefixtures("task_env")
def test_task_gives_up_after_max_retries(
    monkeypatch: pytest.MonkeyPatch, delivery: WebhookDelivery
) -> None:
    monkeypatch.setattr(tasks, "http_client", Receiver(500).client)

    tasks.deliver_webhook_task.apply(args=[str(delivery.id)])

    assert delivery.status == WebhookDeliveryStatus.FAILED
    assert delivery.attempts == tasks.deliver_webhook_task.max_retries + 1


@pytest.mark.usefixtures("task_env")
def test_sweep_task_enqueues_lost_deliveries(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, delivery: WebhookDelivery
) -> None:
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(minutes=10)
    db_session.commit()
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(tasks, "enqueue_webhook_delivery", enqueued.append)

    assert tasks.sweep_webhook_deliveries_task.apply().get() == 1
    assert enqueued == [delivery.id]


def test_webhook_tasks_use_their_own_queue() -> None:
    route = tasks.celery_app.amqp.router.route({}, "webhooks.deliver")

    assert route["queue"].name == "webhooks"
