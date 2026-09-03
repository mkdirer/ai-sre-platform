"""Side-effect-free Celery application factory with bounded Redis behavior."""

from celery import Celery  # type: ignore[import-untyped]

from packages.config import Settings


def create_celery_app(settings: Settings, *, include_worker: bool = False) -> Celery:
    """Create a JSON-only Celery app; construction does not contact Redis."""

    include = ["apps.investigator_worker.celery_app"] if include_worker else None
    app = Celery(
        "ai_sre_incidents",
        broker=settings.celery_broker_url.get_secret_value(),
        backend=settings.celery_result_backend_url.get_secret_value(),
        include=include,
        strict_typing=True,
    )
    visibility_timeout = settings.celery_visibility_timeout_seconds
    app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": visibility_timeout,
            "socket_connect_timeout": settings.queue_publish_timeout_seconds,
            "socket_timeout": settings.queue_publish_timeout_seconds,
        },
        result_backend_transport_options={
            "visibility_timeout": visibility_timeout,
            "socket_connect_timeout": settings.queue_publish_timeout_seconds,
            "socket_timeout": settings.queue_publish_timeout_seconds,
        },
        result_expires=3_600,
        result_serializer="json",
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_default_queue="incidents",
        task_reject_on_worker_lost=True,
        task_routes={
            "incident.collect_evidence": {"queue": "incidents"},
            "incident.process_no_ai_placeholder": {"queue": "incidents"},
        },
        task_serializer="json",
        task_track_started=True,
        worker_prefetch_multiplier=1,
    )
    return app
