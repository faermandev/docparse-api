from celery import Celery

from app.core.config import get_settings

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
)
