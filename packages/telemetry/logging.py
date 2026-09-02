"""Secret-safe structured JSON logging for demo services."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from packages.telemetry.context import current_span_id, current_trace_id, get_request_id

_MAX_STRING_LENGTH = 1_024
_MAX_COLLECTION_ITEMS = 32
_MAX_DEPTH = 4
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "idempotencykey",
        "password",
        "passwd",
        "privatekey",
        "proxyauthorization",
        "secret",
        "setcookie",
        "token",
    }
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|password|passwd|secret|token|api[-_.]?key)\s*[:=]\s*[^\s,;]+"
)


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def redact_text(value: str) -> str:
    """Redact common credential forms and bound untrusted string length."""

    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    if len(redacted) > _MAX_STRING_LENGTH:
        return f"{redacted[:_MAX_STRING_LENGTH]}…"
    return redacted


def redact_value(value: object, *, key: str | None = None, depth: int = 0) -> object:
    """Return a bounded JSON-safe representation with secret-valued keys removed."""

    if key is not None and _normalized_key(key) in _SECRET_KEYS:
        return "[REDACTED]"
    if depth >= _MAX_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        redacted_mapping: dict[str, object] = {}
        for index, (nested_key, nested_value) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                redacted_mapping["_truncated"] = True
                break
            string_key = str(nested_key)[:128]
            redacted_mapping[string_key] = redact_value(
                nested_value,
                key=string_key,
                depth=depth + 1,
            )
        return redacted_mapping
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(item, depth=depth + 1) for item in value[:_MAX_COLLECTION_ITEMS]]
    return redact_text(str(value))


class JsonLogFormatter(logging.Formatter):
    """Render one stable JSON object per line without arbitrary record fields."""

    def __init__(self, *, service_name: str, service_version: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._service_version = service_version
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        """Format a record with correlation/resource fields and allowlisted attributes."""

        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        structured = getattr(record, "structured", {})
        attributes = redact_value(structured) if isinstance(structured, Mapping) else {}
        payload = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "severity": record.levelname,
            "event": redact_text(record.getMessage()),
            "service": self._service_name,
            "version": self._service_version,
            "service.name": self._service_name,
            "service.version": self._service_version,
            "deployment.environment": self._environment,
            "trace_id": current_trace_id(),
            "span_id": current_span_id(),
            "request_id": get_request_id(),
            "attributes": attributes,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
