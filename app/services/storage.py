"""Object storage for uploaded documents.

`Storage` is the interface the rest of the app depends on. `MinioStorage` talks to MinIO (or
any S3-compatible service); `InMemoryStorage` is used in tests.
"""

import io
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError, ProtocolError

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class ObjectNotFoundError(LookupError):
    """No object is stored under the requested key."""


class StorageUnavailableError(AppError):
    """The storage service could not be reached; the operation may succeed later."""

    status_code = 503
    code = "storage_unavailable"


class Storage(Protocol):
    def save(self, key: str, data: bytes, content_type: str) -> None: ...

    def read(self, key: str) -> bytes:
        """Return the object's bytes. Raises ObjectNotFoundError if the key does not exist."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object; a missing key is not an error."""
        ...


class MinioStorage:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def save(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), len(data), content_type=content_type
            )
        except (MaxRetryError, ProtocolError) as exc:
            raise StorageUnavailableError("File storage is unavailable; try again later.") from exc

    def read(self, key: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise ObjectNotFoundError(key) from exc
            raise
        except (MaxRetryError, ProtocolError) as exc:
            raise StorageUnavailableError("File storage is unavailable; try again later.") from exc
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        try:
            self._client.remove_object(self._bucket, key)
        except (MaxRetryError, ProtocolError) as exc:
            raise StorageUnavailableError("File storage is unavailable; try again later.") from exc


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


class InMemoryStorage:
    """Dict-backed storage for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    def save(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = StoredObject(data, content_type)

    def read(self, key: str) -> bytes:
        try:
            return self.objects[key].data
        except KeyError:
            raise ObjectNotFoundError(key) from None

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def build_storage(settings: Settings) -> MinioStorage:
    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        secure=settings.s3_secure,
    )
    return MinioStorage(client, settings.s3_bucket)


@lru_cache
def get_storage() -> Storage:
    """Process-wide storage client (the MinIO client keeps a connection pool)."""
    return build_storage(get_settings())
