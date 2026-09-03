"""Bounded Celery publisher and direct Redis readiness dependency."""

import asyncio
import inspect
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from kombu.exceptions import OperationalError  # type: ignore[import-untyped]
from redis.asyncio import Redis
from redis.exceptions import RedisError

from packages.config import Settings
from packages.task_queue.celery_factory import create_celery_app


class JobPublishError(Exception):
    """Redis/Celery did not durably accept a job within the configured timeout."""


class CeleryIncidentPublisher:
    """Publish only the canonical incident ID; job identity stays in task metadata."""

    def __init__(self, settings: Settings, *, celery_app: Celery | None = None) -> None:
        self._timeout = settings.queue_publish_timeout_seconds
        self._celery = celery_app or create_celery_app(settings)

    async def publish(self, *, job_id: UUID, incident_id: str) -> None:
        """Publish one JSON task without embedding an alert payload."""

        try:
            sender = self._celery.send_task
            options = {
                "args": [incident_id],
                "task_id": str(job_id),
                "queue": "incidents",
            }
            if inspect.iscoroutinefunction(sender):
                await asyncio.wait_for(
                    sender("incident.collect_evidence", **options),
                    timeout=self._timeout,
                )
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        sender,
                        "incident.collect_evidence",
                        **options,
                    ),
                    timeout=self._timeout,
                )
        except (TimeoutError, OSError, OperationalError, RedisError) as error:
            raise JobPublishError("incident queue publication failed") from error


class RedisDependency:
    """Lifecycle-owned Redis probe used by Incident API readiness."""

    def __init__(self, settings: Settings, *, client: Redis | None = None) -> None:
        self._client = client or Redis.from_url(
            settings.celery_broker_url.get_secret_value(),
            socket_connect_timeout=settings.queue_publish_timeout_seconds,
            socket_timeout=settings.queue_publish_timeout_seconds,
            decode_responses=True,
        )

    async def is_ready(self) -> bool:
        """Check only direct broker availability."""

        try:
            return bool(await self._client.ping())
        except (OSError, RedisError):
            return False

    async def close(self) -> None:
        """Close Redis connections and their owned pool."""

        await self._client.aclose()
