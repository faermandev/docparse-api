import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_job_enqueuer
from app.core.config import Settings, get_settings
from app.core.errors import QueueUnavailableError
from app.models import DocumentType, ExtractionJob, JobStatus
from app.services.storage import InMemoryStorage, StorageUnavailableError, StoredObject

PDF_BYTES = b"%PDF-1.7\n%fake pdf content\n%%EOF"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def _upload(
    client: TestClient,
    *,
    filename: str = "invoice.pdf",
    content: bytes = PDF_BYTES,
    document_type: str = "invoice",
) -> Any:
    return client.post(
        "/jobs",
        files={"file": (filename, content, "application/octet-stream")},
        data={"document_type": document_type},
    )


def _make_job(
    db: Session, owner_id: uuid.UUID, *, status: JobStatus, created_at: datetime
) -> ExtractionJob:
    job_id = uuid.uuid4()
    job = ExtractionJob(
        id=job_id,
        api_key_id=owner_id,
        status=status,
        document_type=DocumentType.INVOICE,
        original_filename="doc.pdf",
        storage_key=f"{job_id}.pdf",
        created_at=created_at,
    )
    db.add(job)
    db.flush()
    return job


# POST /jobs


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("invoice.pdf", PDF_BYTES, "application/pdf"),
        ("scan.PNG", PNG_BYTES, "image/png"),
        ("photo.jpeg", JPEG_BYTES, "image/jpeg"),
        ("photo.jpg", JPEG_BYTES, "image/jpeg"),
        ("page.webp", WEBP_BYTES, "image/webp"),
    ],
)
def test_create_job_accepts_supported_files(
    client: TestClient,
    storage: InMemoryStorage,
    owner_id: uuid.UUID,
    db_session: Session,
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    response = _upload(client, filename=filename, content=content)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["document_type"] == "invoice"
    assert body["original_filename"] == filename
    assert body["result"] is None
    assert body["attempts"] == 0
    assert "storage_key" not in body
    assert "api_key_id" not in body

    extension = filename.rsplit(".", 1)[1].lower()
    assert storage.objects[f"{body['id']}.{extension}"] == StoredObject(content, content_type)
    assert db_session.get(ExtractionJob, uuid.UUID(body["id"])).api_key_id == owner_id  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("filename", "content", "detected"),
    [("invoice.pdf", PNG_BYTES, "image/png"), ("photo.png", b"MZ\x90\x00 binary", None)],
)
def test_create_job_rejects_content_that_does_not_match_extension(
    client: TestClient,
    storage: InMemoryStorage,
    filename: str,
    content: bytes,
    detected: str | None,
) -> None:
    response = _upload(client, filename=filename, content=content)

    assert response.status_code == 415
    error = response.json()["error"]
    assert error["code"] == "unsupported_file_type"
    assert error["details"]["detected"] == detected
    assert storage.objects == {}


def test_create_job_returns_503_when_storage_is_down(
    client: TestClient,
    storage: InMemoryStorage,
    enqueued: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_: object) -> None:
        raise StorageUnavailableError("File storage is unavailable; try again later.")

    monkeypatch.setattr(storage, "save", unavailable)

    response = _upload(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert enqueued == []


def test_create_job_enqueues_job_for_processing(
    client: TestClient, enqueued: list[uuid.UUID]
) -> None:
    body = _upload(client).json()

    assert enqueued == [uuid.UUID(body["id"])]


def test_create_job_returns_503_and_marks_job_failed_when_queue_is_down(
    client: TestClient, db_session: Session
) -> None:
    def unavailable(_: uuid.UUID) -> None:
        raise QueueUnavailableError("Job queue is unavailable; try again later.")

    client.app.dependency_overrides[get_job_enqueuer] = lambda: unavailable  # type: ignore[attr-defined]

    response = _upload(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_unavailable"
    (job,) = db_session.query(ExtractionJob).all()
    assert job.status == JobStatus.FAILED
    assert job.error_message == "Could not enqueue the job for processing."


def test_create_job_strips_directories_from_filename(client: TestClient) -> None:
    response = _upload(client, filename="../../etc/contract.pdf", document_type="contract")

    assert response.status_code == 202
    assert response.json()["original_filename"] == "contract.pdf"


def test_create_job_rejects_unsupported_extension(
    client: TestClient, enqueued: list[uuid.UUID]
) -> None:
    response = _upload(client, filename="notes.txt")

    assert response.status_code == 415
    assert enqueued == []
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_create_job_rejects_empty_file(client: TestClient) -> None:
    response = _upload(client, content=b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_create_job_rejects_file_over_size_limit(client: TestClient) -> None:
    client.app.dependency_overrides[get_settings] = lambda: Settings(max_upload_size_mb=1)  # type: ignore[attr-defined]

    response = _upload(client, content=b"x" * (1024 * 1024 + 1))

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "file_too_large"
    assert error["details"] == {"max_size_bytes": 1024 * 1024}


def test_create_job_rejects_invalid_document_type(client: TestClient) -> None:
    response = _upload(client, document_type="passport")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["body", "document_type"]


def test_create_job_requires_file(client: TestClient) -> None:
    response = client.post("/jobs", data={"document_type": "invoice"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "file"]


# GET /jobs/{id}


def test_get_job_returns_created_job(client: TestClient) -> None:
    created = _upload(client).json()

    response = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_job_returns_404_for_unknown_id(client: TestClient) -> None:
    response = client.get(f"/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_job_returns_422_for_malformed_id(client: TestClient) -> None:
    response = client.get("/jobs/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# GET /jobs/{id}/file


def test_download_returns_original_file(client: TestClient) -> None:
    created = _upload(client, filename="nota.pdf").json()

    response = client.get(f"/jobs/{created['id']}/file")

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="nota.pdf"'


def test_download_encodes_non_ascii_filenames(client: TestClient) -> None:
    created = _upload(
        client, filename="currículo joão.png", content=PNG_BYTES, document_type="resume"
    ).json()

    response = client.get(f"/jobs/{created['id']}/file")

    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=utf-8''curr%C3%ADculo%20jo%C3%A3o.png"
    )


def test_download_returns_404_for_unknown_job(client: TestClient) -> None:
    response = client.get(f"/jobs/{uuid.uuid4()}/file")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_download_returns_404_when_file_is_gone(
    client: TestClient, storage: InMemoryStorage
) -> None:
    created = _upload(client).json()
    storage.objects.clear()

    response = client.get(f"/jobs/{created['id']}/file")

    assert response.status_code == 404
    assert "no longer available" in response.json()["error"]["message"]


# GET /jobs


def test_list_jobs_paginates_newest_first(
    client: TestClient, db_session: Session, owner_id: uuid.UUID
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = [
        _make_job(
            db_session, owner_id, status=JobStatus.PENDING, created_at=base + timedelta(minutes=i)
        )
        for i in range(5)
    ]
    newest_first = [str(job.id) for job in reversed(jobs)]

    first_page = client.get("/jobs", params={"limit": 2}).json()
    second_page = client.get("/jobs", params={"limit": 2, "offset": 2}).json()

    assert first_page["total"] == 5
    assert (first_page["limit"], first_page["offset"]) == (2, 0)
    assert [item["id"] for item in first_page["items"]] == newest_first[:2]
    assert [item["id"] for item in second_page["items"]] == newest_first[2:4]


def test_list_jobs_filters_by_status(
    client: TestClient, db_session: Session, owner_id: uuid.UUID
) -> None:
    now = datetime.now(UTC)
    _make_job(db_session, owner_id, status=JobStatus.PENDING, created_at=now)
    failed = _make_job(db_session, owner_id, status=JobStatus.FAILED, created_at=now)

    body = client.get("/jobs", params={"status": "failed"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [str(failed.id)]


def test_list_jobs_empty(client: TestClient) -> None:
    assert client.get("/jobs").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "unknown"}]
)
def test_list_jobs_rejects_invalid_query(client: TestClient, params: dict[str, Any]) -> None:
    response = client.get("/jobs", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
