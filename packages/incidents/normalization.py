"""Deterministic, bounded normalization for untrusted Alertmanager webhooks."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from packages.models.alerts import AlertmanagerAlert, AlertmanagerWebhook, AlertStatus
from packages.models.incidents import IncidentSeverity
from packages.telemetry import redact_text

_DEFAULT_WINDOW_BEFORE = timedelta(minutes=10)
_DEFAULT_WINDOW_AFTER = timedelta(minutes=5)
_ALLOWED_SEVERITIES = {severity.value: severity for severity in IncidentSeverity}
_SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class NormalizedAlert:
    """Canonical alert input used by persistence and fingerprinting code."""

    status: AlertStatus
    alert_fingerprint: str
    delivery_fingerprint: str
    alert_name: str
    title: str
    service: str
    affected_services: tuple[str, ...]
    severity: IncidentSeverity
    starts_at: datetime
    ends_at: datetime | None
    investigation_window_start: datetime
    investigation_window_end: datetime
    labels: dict[str, str]
    annotations: dict[str, str]
    source_fingerprint: str | None


def alert_fingerprint(labels: dict[str, str]) -> str:
    """Hash the canonical label set; input ordering cannot change alert identity."""

    return _sha256({"labels": sorted(labels.items())})


def delivery_fingerprint(
    *,
    fingerprint: str,
    status: AlertStatus,
    starts_at: datetime,
    ends_at: datetime | None,
) -> str:
    """Identify one firing/resolved state update independently of annotations."""

    return _sha256(
        {
            "alert_fingerprint": fingerprint,
            "status": status,
            "starts_at": _as_utc(starts_at).isoformat(),
            "ends_at": _as_utc(ends_at).isoformat() if ends_at is not None else None,
        }
    )


def normalize_webhook(webhook: AlertmanagerWebhook) -> tuple[NormalizedAlert, ...]:
    """Normalize every alert or reject missing repository-owned identity labels."""

    return tuple(_normalize_alert(alert) for alert in webhook.alerts)


def _normalize_alert(alert: AlertmanagerAlert) -> NormalizedAlert:
    labels = {key: redact_text(value) for key, value in alert.labels.items()}
    annotations = {key: redact_text(value) for key, value in alert.annotations.items()}
    alert_name = _required_label(labels, "alertname")
    service = _required_label(labels, "service")
    if _SERVICE_PATTERN.fullmatch(service) is None:
        raise ValueError("Alertmanager service label must be a safe service name")
    started_at = _as_utc(alert.starts_at)
    ends_at = None if alert.status == "firing" else _normalized_end(alert.ends_at)
    computed_fingerprint = alert_fingerprint(labels)
    severity = _ALLOWED_SEVERITIES.get(
        labels.get("severity", IncidentSeverity.WARNING.value).casefold(),
        IncidentSeverity.WARNING,
    )
    summary = annotations.get("summary") or annotations.get("description") or alert_name
    title = redact_text(summary).strip()[:256] or alert_name
    affected_services = tuple(
        sorted(
            {
                part.strip()
                for part in labels.get("affected_services", service).split(",")
                if part.strip()
            }
        )
    )
    if not affected_services:
        affected_services = (service,)
    if any(_SERVICE_PATTERN.fullmatch(item) is None for item in affected_services):
        raise ValueError("Alertmanager affected_services contains an invalid service name")
    normalized_status = alert.status
    return NormalizedAlert(
        status=normalized_status,
        alert_fingerprint=computed_fingerprint,
        delivery_fingerprint=delivery_fingerprint(
            fingerprint=computed_fingerprint,
            status=normalized_status,
            starts_at=started_at,
            ends_at=ends_at,
        ),
        alert_name=alert_name,
        title=title,
        service=service,
        affected_services=affected_services[:32],
        severity=severity,
        starts_at=started_at,
        ends_at=ends_at,
        investigation_window_start=started_at - _DEFAULT_WINDOW_BEFORE,
        investigation_window_end=(ends_at or started_at) + _DEFAULT_WINDOW_AFTER,
        labels=labels,
        annotations=annotations,
        source_fingerprint=redact_text(alert.fingerprint)[:256] or None,
    )


def _required_label(labels: dict[str, str], name: str) -> str:
    value = labels.get(name, "").strip()
    if not value:
        raise ValueError(f"Alertmanager alert is missing required label: {name}")
    return value[:128]


def _normalized_end(value: datetime) -> datetime | None:
    normalized = _as_utc(value)
    if normalized.year <= 1:
        return None
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Alertmanager timestamps must include a timezone")
    return value.astimezone(UTC)


def _sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
