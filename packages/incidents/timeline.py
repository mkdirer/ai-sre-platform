"""Deterministic correlation of canonical evidence into a chronological timeline."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from packages.models.evidence import EvidenceItem, EvidenceTimelinePage, TimelineEvent
from packages.telemetry import redact_text, redact_value


def correlate_timeline(
    evidence: tuple[EvidenceItem, ...],
    *,
    limit: int,
    offset: int,
) -> EvidenceTimelinePage:
    """Expand known structured payloads and apply one stable total ordering."""

    events: list[TimelineEvent] = []
    for item in evidence:
        expanded = _expand_evidence(item)
        if not expanded:
            expanded = [
                _event(
                    item,
                    discriminator="evidence",
                    timestamp=item.observed_at,
                    summary=item.summary,
                    attributes={"query_template": item.query_template.value},
                )
            ]
        events.extend(expanded)
    events = list({event.id: event for event in events}.values())
    events.sort(
        key=lambda event: (
            event.timestamp,
            event.source.value,
            event.evidence_id,
            event.id,
        )
    )
    return EvidenceTimelinePage(
        items=events[offset : offset + limit],
        total=len(events),
        limit=limit,
        offset=offset,
    )


def _expand_evidence(item: EvidenceItem) -> list[TimelineEvent]:
    if item.status.value != "collected":
        return []
    if item.type.value == "log":
        return _expand_logs(item)
    if item.type.value == "deployment":
        return _expand_deployments(item)
    if item.type.value == "trace":
        return _expand_traces(item)
    return []


def _expand_logs(item: EvidenceItem) -> list[TimelineEvent]:
    raw_entries = item.payload.get("entries")
    if not isinstance(raw_entries, list):
        return []
    events: list[TimelineEvent] = []
    for raw_entry in raw_entries[:100]:
        if not isinstance(raw_entry, dict):
            continue
        timestamp = _timestamp(raw_entry.get("timestamp"), item.observed_at)
        severity = redact_text(str(raw_entry.get("severity", "UNKNOWN")))[:32]
        name = redact_text(str(raw_entry.get("event", "log event")))[:400]
        attributes = _attributes(
            {
                "trace_id": raw_entry.get("trace_id"),
                "span_id": raw_entry.get("span_id"),
                "request_id": raw_entry.get("request_id"),
            }
        )
        events.append(
            _event(
                item,
                discriminator=_content_discriminator("log", raw_entry),
                timestamp=timestamp,
                summary=f"{severity} {name}"[:512],
                attributes=attributes,
            )
        )
    return events


def _expand_deployments(item: EvidenceItem) -> list[TimelineEvent]:
    raw_deployments = item.payload.get("deployments")
    if not isinstance(raw_deployments, list):
        current = item.payload.get("current")
        raw_deployments = [current] if isinstance(current, dict) else []
    events: list[TimelineEvent] = []
    for raw_deployment in raw_deployments[:50]:
        if not isinstance(raw_deployment, dict):
            continue
        timestamp = _timestamp(raw_deployment.get("deployed_at"), item.observed_at)
        deployment_id = redact_text(str(raw_deployment.get("id", "unknown")))[:32]
        service = redact_text(str(raw_deployment.get("service", "unknown")))[:128]
        version = redact_text(str(raw_deployment.get("version", "unknown")))[:64]
        commit_sha = redact_text(str(raw_deployment.get("commit_sha", "unknown")))[:64]
        events.append(
            _event(
                item,
                discriminator=(
                    f"deployment:{deployment_id}"
                    if deployment_id != "unknown"
                    else _content_discriminator("deployment", raw_deployment)
                ),
                timestamp=timestamp,
                summary=f"Deployed {service} version {version} at commit {commit_sha[:12]}",
                attributes=_attributes(
                    {
                        "deployment_id": deployment_id,
                        "service": service,
                        "version": version,
                        "commit_sha": commit_sha,
                    }
                ),
            )
        )
    return events


def _expand_traces(item: EvidenceItem) -> list[TimelineEvent]:
    raw_traces = item.payload.get("traces")
    if not isinstance(raw_traces, list):
        return []
    events: list[TimelineEvent] = []
    for raw_trace in raw_traces[:50]:
        if not isinstance(raw_trace, dict):
            continue
        timestamp = _timestamp(raw_trace.get("timestamp"), item.observed_at)
        trace_id = redact_text(str(raw_trace.get("trace_id", "unknown")))[:64]
        duration = raw_trace.get("duration_ms")
        events.append(
            _event(
                item,
                discriminator=(
                    f"trace:{trace_id}"
                    if trace_id != "unknown"
                    else _content_discriminator("trace", raw_trace)
                ),
                timestamp=timestamp,
                summary=f"Trace {trace_id} duration {duration} ms"[:512],
                attributes=_attributes(
                    {
                        "trace_id": trace_id,
                        "duration_ms": duration,
                        "root_service": raw_trace.get("root_service"),
                    }
                ),
            )
        )
    return events


def _event(
    item: EvidenceItem,
    *,
    discriminator: str,
    timestamp: datetime,
    summary: str,
    attributes: dict[str, object],
) -> TimelineEvent:
    canonical = json.dumps(
        {
            "evidence_id": item.id,
            "discriminator": discriminator,
            "timestamp": timestamp.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return TimelineEvent(
        id=f"EVT-{digest[:24].upper()}",
        evidence_id=item.id,
        incident_id=item.incident_id,
        timestamp=timestamp,
        source=item.source,
        type=item.type,
        status=item.status,
        summary=redact_text(summary)[:512] or "Evidence event",
        attributes=attributes,
    )


def _content_discriminator(kind: str, payload: dict[object, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{kind}:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _timestamp(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    else:
        return fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return fallback
    return parsed.astimezone(UTC)


def _attributes(value: dict[str, object]) -> dict[str, object]:
    cleaned = {key: item for key, item in value.items() if item is not None}
    safe = redact_value(cleaned)
    return cast(dict[str, object], safe if isinstance(safe, dict) else {})
