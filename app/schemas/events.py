from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import JobStatus
from app.schemas.jobs import JobRead

# Steps reported from inside document reading/extraction.
ProgressStage = Literal["reading", "transcribing", "extracting"]
# Every stage a client can observe, in roughly the order they happen.
JobStage = Literal[
    "queued",
    "processing",
    "reading",
    "transcribing",
    "extracting",
    "retrying",
    "completed",
    "failed",
]
TERMINAL_STAGES: frozenset[str] = frozenset({"completed", "failed"})


class JobEvent(BaseModel):
    """A progress update for one job, streamed to clients over Server-Sent Events."""

    stage: JobStage
    status: JobStatus
    attempt: int = Field(description="Processing attempt this event belongs to (0 = not started).")
    message: str | None = Field(default=None, description="Error or retry details, if any.")
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    job: JobRead | None = Field(
        default=None, description="Full job, including the result, on completed/failed events."
    )

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES
