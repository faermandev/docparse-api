from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "postgresql+psycopg://docs:docs@localhost:5432/docs_extraction"

    storage_dir: Path = Path("storage")
    max_upload_size_mb: int = Field(default=10, gt=0)

    llm_provider: Literal["anthropic", "openai"] = "openai"
    llm_model: str = "gpt-6-luna"
    llm_api_key: SecretStr | None = None
    llm_max_tokens: int = Field(default=16000, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    prompt_version: str = Field(default="v1", pattern=r"^v\d+$")

    pdf_max_pages: int = Field(default=50, gt=0)
    vision_max_pages: int = Field(default=10, gt=0)

    redis_url: str = "redis://localhost:6379/0"
    job_max_retries: int = Field(default=3, ge=0)
    job_retry_backoff_seconds: float = Field(default=10.0, gt=0)

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (one per process)."""
    return Settings()
