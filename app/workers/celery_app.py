from typing import Any

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery("docs_extraction", broker=settings.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Job state lives in PostgreSQL, so Celery results are not stored.
    task_ignore_result=True,
    # Acknowledge after the task finishes so a crashed worker's job is redelivered.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Fail fast when publishing if Redis is down, instead of blocking the HTTP request.
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.5},
    broker_connection_retry_on_startup=True,
    # Webhook deliveries get their own queue so slow receivers never delay extractions.
    task_routes={"webhooks.*": {"queue": "webhooks"}},
    # Run by `celery worker -B` (embedded beat) in this project.
    beat_schedule={
        "sweep-webhook-deliveries": {"task": "webhooks.sweep", "schedule": 60.0},
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    """Use our structlog setup instead of Celery's default logging."""
    configure_logging(settings.log_level, settings.log_format)
