import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (registers all tables on Base.metadata)
from app.api.deps import get_job_enqueuer, get_storage
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import ApiKey
from app.services.api_keys import create_api_key
from app.services.storage import InMemoryStorage


def _test_database_url() -> URL:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test")


def _create_database_if_missing(url: URL) -> None:
    admin_engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin_engine.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_database_url()
    _create_database_if_missing(url)
    test_engine = create_engine(url)
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Session bound to an outer transaction that is rolled back after each test.

    Application commits become SAVEPOINT releases, so tests stay isolated.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def enqueued() -> list[uuid.UUID]:
    """Job ids the API tried to enqueue (replaces Celery/Redis in API tests)."""
    return []


@pytest.fixture
def api_key(db_session: Session) -> tuple[ApiKey, str]:
    """An active API key and its plaintext; the default client authenticates with it."""
    return create_api_key(db_session, "test-client")


@pytest.fixture
def owner_id(api_key: tuple[ApiKey, str]) -> uuid.UUID:
    return api_key[0].id


@pytest.fixture
def client(
    db_session: Session,
    storage: InMemoryStorage,
    enqueued: list[uuid.UUID],
    api_key: tuple[ApiKey, str],
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_job_enqueuer] = lambda: enqueued.append
    with TestClient(app, headers={"X-API-Key": api_key[1]}) as test_client:
        yield test_client
