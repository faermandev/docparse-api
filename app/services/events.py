"""Job progress events: published by the worker, consumed by the API's SSE endpoint.

Redis pub/sub carries the events. It is fire-and-forget (events published while nobody is
listening are dropped), so the API always starts a stream from a database snapshot; the
database stays the source of truth and events only add live detail.
"""

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import lru_cache
from typing import Protocol

import redis
import redis.asyncio as aioredis
import structlog

from app.core.config import get_settings
from app.models import ExtractionJob, JobStatus
from app.schemas.events import JobEvent, JobStage
from app.schemas.jobs import JobRead

logger = structlog.get_logger(__name__)

EventPublisher = Callable[[uuid.UUID, JobEvent], None]


def channel_for(job_id: uuid.UUID) -> str:
    return f"job-events:{job_id}"


def snapshot_event(job: ExtractionJob) -> JobEvent:
    """Describe a job's current database state as an event."""
    stages: dict[JobStatus, JobStage] = {
        JobStatus.PENDING: "retrying" if job.attempts else "queued",
        JobStatus.PROCESSING: "processing",
        JobStatus.COMPLETED: "completed",
        JobStatus.FAILED: "failed",
    }
    event = JobEvent(
        stage=stages[job.status],
        status=job.status,
        attempt=job.attempts,
        message=job.error_message,
    )
    if event.is_terminal:
        event.job = JobRead.model_validate(job)
    return event


class JobEventSubscription(Protocol):
    async def get(self, timeout: float) -> JobEvent | None:
        """Wait for the next event; None if nothing arrived within `timeout` seconds."""
        ...


class JobEventBroker(Protocol):
    def publish(self, job_id: uuid.UUID, event: JobEvent) -> None:
        """Best effort: failures are logged, never raised."""
        ...

    def subscribe(self, job_id: uuid.UUID) -> AbstractAsyncContextManager[JobEventSubscription]:
        """The subscription is active once the context manager has been entered."""
        ...


class RedisJobEventBroker:
    def __init__(self, url: str) -> None:
        self._url = url
        self._publisher = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)

    def publish(self, job_id: uuid.UUID, event: JobEvent) -> None:
        try:
            self._publisher.publish(channel_for(job_id), event.model_dump_json())
        except redis.RedisError as exc:
            logger.warning("job_event_publish_failed", stage=event.stage, error=str(exc))

    @asynccontextmanager
    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[JobEventSubscription]:
        # A client per subscription: async clients are bound to the event loop that made them.
        client = aioredis.Redis.from_url(self._url)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel_for(job_id))
        try:
            yield _RedisSubscription(pubsub)
        finally:
            await pubsub.aclose()
            await client.aclose()


class _RedisSubscription:
    def __init__(self, pubsub: aioredis.client.PubSub) -> None:
        self._pubsub = pubsub

    async def get(self, timeout: float) -> JobEvent | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=remaining
            )
            if message is not None and message["type"] == "message":
                return JobEvent.model_validate_json(message["data"])
        return None


class InMemoryJobEventBroker:
    """Broker for tests. Thread-safe: events may be published from outside the event loop."""

    def __init__(self) -> None:
        self.published: list[tuple[uuid.UUID, JobEvent]] = []
        # Called right after a subscription becomes active (lets tests publish "live" events).
        self.on_subscribe: Callable[[uuid.UUID], None] | None = None
        self._subscribers: dict[
            uuid.UUID, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[JobEvent]]]
        ] = {}
        self._lock = threading.Lock()

    def publish(self, job_id: uuid.UUID, event: JobEvent) -> None:
        with self._lock:
            self.published.append((job_id, event))
            subscribers = list(self._subscribers.get(job_id, []))
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    @asynccontextmanager
    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[JobEventSubscription]:
        entry = (asyncio.get_running_loop(), asyncio.Queue[JobEvent]())
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(entry)
        try:
            if self.on_subscribe is not None:
                self.on_subscribe(job_id)
            yield _QueueSubscription(entry[1])
        finally:
            with self._lock:
                self._subscribers[job_id].remove(entry)


class _QueueSubscription:
    def __init__(self, queue: asyncio.Queue[JobEvent]) -> None:
        self._queue = queue

    async def get(self, timeout: float) -> JobEvent | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None


@lru_cache
def get_event_broker() -> JobEventBroker:
    return RedisJobEventBroker(get_settings().redis_url)
