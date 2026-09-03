"""Deterministic incident normalization and lifecycle services."""

from packages.incidents.normalization import (
    NormalizedAlert,
    alert_fingerprint,
    delivery_fingerprint,
    normalize_webhook,
)
from packages.incidents.scoping import IncidentScopeError, scope_incident
from packages.incidents.timeline import correlate_timeline
from packages.incidents.transitions import InvalidStatusTransition, StatusTransitionService

__all__ = [
    "IncidentScopeError",
    "InvalidStatusTransition",
    "NormalizedAlert",
    "StatusTransitionService",
    "alert_fingerprint",
    "correlate_timeline",
    "delivery_fingerprint",
    "normalize_webhook",
    "scope_incident",
]
