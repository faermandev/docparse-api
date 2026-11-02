import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models import DocumentType, JobStatus


class JobRead(BaseModel):
    """Public representation of an extraction job (internal fields such as storage_key omitted)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    document_type: DocumentType
    original_filename: str
    result: dict[str, Any] | None
    error_message: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class JobList(BaseModel):
    items: list[JobRead]
    total: int
    limit: int
    offset: int
