import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiKey
from app.services.api_keys import (
    KEY_PREFIX,
    authenticate,
    create_api_key,
    generate_api_key,
    hash_api_key,
)

PDF_BYTES = b"%PDF-1.7\n%fake\n%%EOF"


def _upload(client: TestClient, headers: dict[str, str] | None = None) -> dict[str, str]:
    response = client.post(
        "/jobs",
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
        data={"document_type": "invoice"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()


# API key service


def test_generated_keys_are_prefixed_random_tokens() -> None:
    first, second = generate_api_key(), generate_api_key()

    assert first.startswith(KEY_PREFIX)
    assert len(first) > 40
    assert first != second


def test_only_the_hash_is_stored(db_session: Session) -> None:
    api_key, plaintext = create_api_key(db_session, "acme")

    assert api_key.key_hash == hash_api_key(plaintext)
    assert plaintext not in (api_key.key_hash, api_key.key_prefix)
    assert plaintext.startswith(api_key.key_prefix)


def test_authenticate_accepts_active_keys_only(db_session: Session) -> None:
    api_key, plaintext = create_api_key(db_session, "acme")

    assert authenticate(db_session, plaintext) == api_key
    assert authenticate(db_session, plaintext + "x") is None

    api_key.is_active = False
    db_session.commit()
    assert authenticate(db_session, plaintext) is None


# Authentication on the API


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/jobs"), ("GET", f"/jobs/{uuid.uuid4()}"), ("GET", f"/jobs/{uuid.uuid4()}/file")],
)
def test_job_endpoints_require_an_api_key(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, headers={"X-API-Key": ""})

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "unauthorized",
        "message": "Missing X-API-Key header.",
        "details": None,
    }


def test_upload_requires_an_api_key(client: TestClient) -> None:
    response = client.post(
        "/jobs",
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
        data={"document_type": "invoice"},
        headers={"X-API-Key": ""},
    )

    assert response.status_code == 401


def test_invalid_key_is_rejected(client: TestClient) -> None:
    response = client.get("/jobs", headers={"X-API-Key": "dex_not-a-real-key"})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid or revoked API key."


def test_revoked_key_is_rejected(
    client: TestClient, db_session: Session, api_key: tuple[ApiKey, str]
) -> None:
    api_key[0].is_active = False
    db_session.commit()

    assert client.get("/jobs").status_code == 401


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health", headers={"X-API-Key": ""}).status_code == 200


# Isolation between clients


@pytest.fixture
def other_headers(db_session: Session) -> dict[str, str]:
    _, plaintext = create_api_key(db_session, "other-client")
    return {"X-API-Key": plaintext}


def test_clients_cannot_see_each_others_jobs(
    client: TestClient, other_headers: dict[str, str]
) -> None:
    mine = _upload(client)
    theirs = _upload(client, headers=other_headers)

    listed = client.get("/jobs").json()
    assert [item["id"] for item in listed["items"]] == [mine["id"]]
    assert listed["total"] == 1

    # Someone else's job is indistinguishable from a job that does not exist.
    for path in (f"/jobs/{theirs['id']}", f"/jobs/{theirs['id']}/file"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    assert client.get(f"/jobs/{theirs['id']}", headers=other_headers).status_code == 200
