"""Secret-safe structured JSON logging for demo services."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from packages.telemetry.context import (
    current_span_id,
    current_trace_id,
    get_incident_id,
    get_request_id,
)

_MAX_STRING_LENGTH = 1_024
_MAX_COLLECTION_ITEMS = 32
_MAX_DEPTH = 4
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "dbpassword",
        "idempotencykey",
        "password",
        "passwd",
        "privatekey",
        "proxyauthorization",
        "pwd",
        "secret",
        "setcookie",
        "token",
    }
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
# Key coverage intentionally overlaps scripts/redact_secrets.py (pwd,
# client-secret, quoted JSON keys) but the two are not identical by design:
# hot-path logs bound values at ,/; and normalize the separator to =, while
# the CI script preserves separators and consumes wider values. Keep both
# green via tests/unit/test_telemetry.py and tests/unit/test_redact_secrets.py
# rather than sharing a pattern across the service/CI boundary.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)"
    r'("?)\b([A-Za-z_]*(?:authorization|password|passwd|pwd|token|api[-_.]?key|secret|client[-_.]?secret))\b("?)'
    r'(\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_URL_CREDENTIALS_PATTERN = re.compile(r"(://[^:/\s]+:)[^\s]*(@)")


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def redact_text(value: str) -> str:
    """Redact common credential forms and bound untrusted string length."""

    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}=[REDACTED]", redacted
    )
    redacted = _URL_CREDENTIALS_PATTERN.sub(r"\1[REDACTED]\2", redacted)
    if len(redacted) > _MAX_STRING_LENGTH:
        return f"{redacted[:_MAX_STRING_LENGTH]}…"
    return redacted


def redact_value(
    value: object,
    *,
    key: str | None = None,
    depth: int = 0,
    max_depth: int = _MAX_DEPTH,
    max_collection_items: int = _MAX_COLLECTION_ITEMS,
) -> object:
    """Return a bounded JSON-safe representation with secret-valued keys removed."""

    if key is not None and _normalized_key(key) in _SECRET_KEYS:
        return "[REDACTED]"
    if depth >= max_depth:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        redacted_mapping: dict[str, object] = {}
        for index, (nested_key, nested_value) in enumerate(value.items()):
            if index >= max_collection_items:
                redacted_mapping["_truncated"] = True
                break
            string_key = str(nested_key)[:128]
            redacted_mapping[string_key] = redact_value(
                nested_value,
                key=string_key,
                depth=depth + 1,
                max_depth=max_depth,
                max_collection_items=max_collection_items,
            )
        return redacted_mapping
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [
            redact_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_collection_items=max_collection_items,
            )
            for item in value[:max_collection_items]
        ]
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
            "incident_id": get_incident_id(),
            "attributes": attributes,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
