from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.services.storage import LocalFileStorage


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> LocalFileStorage:
    return LocalFileStorage(settings.storage_dir)


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[LocalFileStorage, Depends(get_storage)]
