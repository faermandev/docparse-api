import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.db.session import get_db
from app.models import ApiKey
from app.services.api_keys import authenticate
from app.services.storage import Storage, get_storage
from app.workers.tasks import enqueue_process_job

JobEnqueuer = Callable[[uuid.UUID], None]


_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Your API key, e.g. `dex_...`."
)


def get_current_api_key(
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Security(_api_key_header)],
) -> ApiKey:
    """Authenticate the request from its X-API-Key header."""
    if not key:
        raise UnauthorizedError("Missing X-API-Key header.")
    api_key = authenticate(db, key)
    if api_key is None:
        raise UnauthorizedError("Invalid or revoked API key.")
    return api_key


def get_job_enqueuer() -> JobEnqueuer:
    return enqueue_process_job


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[Storage, Depends(get_storage)]
EnqueuerDep = Annotated[JobEnqueuer, Depends(get_job_enqueuer)]
CurrentApiKey = Annotated[ApiKey, Depends(get_current_api_key)]
