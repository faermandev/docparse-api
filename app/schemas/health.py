from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: Literal["ok"] = "ok"
