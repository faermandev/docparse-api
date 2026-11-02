import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.models import DocumentType
from app.services.jobs import create_job
from app.services.storage import LocalFileStorage


def test_storage_saves_and_deletes(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)

    storage.save("a/b.pdf", b"data")
    assert (tmp_path / "a" / "b.pdf").read_bytes() == b"data"

    storage.delete("a/b.pdf")
    assert not (tmp_path / "a" / "b.pdf").exists()


def test_storage_rejects_keys_outside_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes"):
        storage.save("../outside.pdf", b"data")


def test_create_job_removes_stored_file_when_db_commit_fails(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    db = MagicMock()
    db.commit.side_effect = OperationalError("INSERT", {}, Exception("db down"))

    with pytest.raises(OperationalError):
        create_job(
            db,
            storage,
            file=io.BytesIO(b"%PDF"),
            filename="x.pdf",
            document_type=DocumentType.INVOICE,
            max_size_bytes=1024,
        )

    db.rollback.assert_called_once()
    assert list(tmp_path.iterdir()) == []
