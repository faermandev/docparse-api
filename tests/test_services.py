import io
import uuid
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from minio import Minio
from minio.error import S3Error
from sqlalchemy.exc import OperationalError
from urllib3.exceptions import MaxRetryError

from app.models import DocumentType
from app.services.jobs import create_job
from app.services.storage import (
    InMemoryStorage,
    MinioStorage,
    ObjectNotFoundError,
    StorageUnavailableError,
    StoredObject,
)


def _s3_error(code: str) -> S3Error:
    return S3Error(MagicMock(), code, "message", "resource", "request-id", "host-id")


# InMemoryStorage


def test_in_memory_storage_round_trip() -> None:
    storage = InMemoryStorage()

    storage.save("a.pdf", b"data", "application/pdf")
    assert storage.read("a.pdf") == b"data"
    assert storage.objects["a.pdf"] == StoredObject(b"data", "application/pdf")

    storage.delete("a.pdf")
    storage.delete("a.pdf")  # deleting a missing key is fine
    with pytest.raises(ObjectNotFoundError):
        storage.read("a.pdf")


# MinioStorage (client mocked; the real service is exercised via docker compose)


@pytest.fixture
def client() -> MagicMock:
    return MagicMock(spec=Minio)


def test_minio_save_uploads_bytes_with_content_type(client: MagicMock) -> None:
    MinioStorage(client, "docs").save("k.png", b"img", "image/png")

    bucket, key, body, length = client.put_object.call_args.args
    assert (bucket, key, body.read(), length) == ("docs", "k.png", b"img", 3)
    assert client.put_object.call_args.kwargs == {"content_type": "image/png"}


def test_minio_read_returns_bytes_and_releases_connection(client: MagicMock) -> None:
    response = client.get_object.return_value
    response.read.return_value = b"data"

    assert MinioStorage(client, "docs").read("k.pdf") == b"data"
    client.get_object.assert_called_once_with("docs", "k.pdf")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_minio_read_maps_missing_key(client: MagicMock) -> None:
    client.get_object.side_effect = _s3_error("NoSuchKey")

    with pytest.raises(ObjectNotFoundError):
        MinioStorage(client, "docs").read("missing.pdf")


def test_minio_read_propagates_other_s3_errors(client: MagicMock) -> None:
    client.get_object.side_effect = _s3_error("AccessDenied")

    with pytest.raises(S3Error):
        MinioStorage(client, "docs").read("k.pdf")


_OPERATIONS: dict[str, Callable[[MinioStorage], object]] = {
    "save": lambda storage: storage.save("k.pdf", b"x", "application/pdf"),
    "read": lambda storage: storage.read("k.pdf"),
    "delete": lambda storage: storage.delete("k.pdf"),
}


@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_minio_connection_failures_become_storage_unavailable(
    client: MagicMock, operation: str
) -> None:
    outage = MaxRetryError(MagicMock(), "/docs/k.pdf")
    client.put_object.side_effect = outage
    client.get_object.side_effect = outage
    client.remove_object.side_effect = outage

    with pytest.raises(StorageUnavailableError):
        _OPERATIONS[operation](MinioStorage(client, "docs"))


# create_job


def test_create_job_removes_stored_file_when_db_commit_fails() -> None:
    storage = InMemoryStorage()
    db = MagicMock()
    db.commit.side_effect = OperationalError("INSERT", {}, Exception("db down"))

    with pytest.raises(OperationalError):
        create_job(
            db,
            storage,
            owner_id=uuid.uuid4(),
            file=io.BytesIO(b"%PDF-1.4"),
            filename="x.pdf",
            document_type=DocumentType.INVOICE,
            max_size_bytes=1024,
            enqueue=MagicMock(),
        )

    db.rollback.assert_called_once()
    assert storage.objects == {}
