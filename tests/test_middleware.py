import logging
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.errors import QueueUnavailableError, register_error_handlers
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.services.jobs import detect_content_type
from app.workers import tasks

# Request id


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    request_id = client.get("/health").headers["X-Request-ID"]

    assert len(request_id) == 32
    int(request_id, 16)  # a uuid4 hex string


def test_valid_incoming_request_id_is_reused(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.123"})

    assert response.headers["X-Request-ID"] == "trace-abc.123"


@pytest.mark.parametrize("bad", ["has spaces", "x" * 65, 'quote"inject'])
def test_malformed_incoming_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": bad})

    assert response.headers["X-Request-ID"] != bad


def test_error_responses_carry_the_request_id(client: TestClient) -> None:
    response = client.get("/jobs", headers={"X-API-Key": "", "X-Request-ID": "req-401"})

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "req-401"


def test_each_request_is_logged_with_its_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        client.get("/health", headers={"X-Request-ID": "req-log"})

    events = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    (completed,) = [e for e in events if e["event"] == "request_completed"]
    assert completed["request_id"] == "req-log"
    assert completed["method"] == "GET"
    assert completed["path"] == "/health"
    assert completed["status_code"] == 200
    assert completed["duration_ms"] >= 0


# Body size limit


@pytest.fixture
def limited_client() -> Iterator[TestClient]:
    async def echo(request: Any) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    app = BodySizeLimitMiddleware(
        RequestContextMiddleware(
            FastAPI(routes=[Route("/upload", echo, methods=["POST"])])  # type: ignore[arg-type]
        ),
        max_body_bytes=10,
    )
    with TestClient(app) as test_client:  # type: ignore[arg-type]
        yield test_client


def test_body_within_limit_passes(limited_client: TestClient) -> None:
    response = limited_client.post("/upload", content=b"x" * 10)

    assert response.status_code == 200
    assert response.text == "10"


def test_body_over_limit_is_rejected_before_reading(limited_client: TestClient) -> None:
    response = limited_client.post("/upload", content=b"x" * 11)

    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"max_body_bytes": 10}


def test_body_without_content_length_is_rejected(limited_client: TestClient) -> None:
    response = limited_client.post("/upload", content=iter([b"chunked"]))

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "length_required"


def test_get_requests_are_not_limited(limited_client: TestClient) -> None:
    assert limited_client.get("/upload").status_code == 405  # reached the router


# Unhandled errors


def test_unhandled_errors_use_the_error_envelope() -> None:
    def boom() -> None:
        raise RuntimeError("secret detail")

    app = FastAPI()
    register_error_handlers(app)
    app.add_api_route("/boom", boom)

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Internal server error.",
        "details": None,
    }


# File signatures


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7 ...", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xdb....", "image/jpeg"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),
        (b"PK\x03\x04 zip", None),
        (b"", None),
    ],
)
def test_detect_content_type(data: bytes, expected: str | None) -> None:
    assert detect_content_type(data) == expected


# Request id propagation to the worker


def test_enqueue_passes_current_request_id_to_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_async = MagicMock()
    monkeypatch.setattr(tasks.process_job_task, "apply_async", apply_async)
    job_id = uuid.uuid4()

    with structlog.contextvars.bound_contextvars(request_id="req-xyz"):
        tasks.enqueue_process_job(job_id)

    apply_async.assert_called_once_with(args=[str(job_id)], kwargs={"request_id": "req-xyz"})


def test_enqueue_maps_broker_errors_to_queue_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tasks.process_job_task, "apply_async", MagicMock(side_effect=OperationalError("down"))
    )

    with pytest.raises(QueueUnavailableError):
        tasks.enqueue_process_job(uuid.uuid4())
