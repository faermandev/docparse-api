import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.services.storage import LocalFileStorage
from app.workers.tasks import enqueue_process_job

JobEnqueuer = Callable[[uuid.UUID], None]


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> LocalFileStorage:
    return LocalFileStorage(settings.storage_dir)


def get_job_enqueuer() -> JobEnqueuer:
    return enqueue_process_job


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[LocalFileStorage, Depends(get_storage)]
EnqueuerDep = Annotated[JobEnqueuer, Depends(get_job_enqueuer)]
