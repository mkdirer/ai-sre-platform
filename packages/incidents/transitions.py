"""Single authority for incident lifecycle transitions."""

from packages.models.incidents import IncidentStatus


class InvalidStatusTransition(ValueError):
    """Raised when code attempts a lifecycle movement not in the allowlist."""


_ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.QUEUED: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.INVESTIGATION_FAILED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.WAITING_FOR_APPROVAL,
            IncidentStatus.INSUFFICIENT_EVIDENCE,
            IncidentStatus.INVESTIGATION_FAILED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.WAITING_FOR_APPROVAL: frozenset(
        {
            IncidentStatus.REMEDIATING,
            IncidentStatus.REJECTED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.REMEDIATING: frozenset(
        {IncidentStatus.VERIFYING, IncidentStatus.INVESTIGATION_FAILED}
    ),
    IncidentStatus.VERIFYING: frozenset(
        {IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATION_FAILED}
    ),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.QUEUED, IncidentStatus.CLOSED}),
    IncidentStatus.INSUFFICIENT_EVIDENCE: frozenset({IncidentStatus.QUEUED, IncidentStatus.CLOSED}),
    IncidentStatus.INVESTIGATION_FAILED: frozenset({IncidentStatus.QUEUED, IncidentStatus.CLOSED}),
    IncidentStatus.REJECTED: frozenset({IncidentStatus.CLOSED, IncidentStatus.QUEUED}),
    IncidentStatus.CLOSED: frozenset({IncidentStatus.QUEUED}),
}


class StatusTransitionService:
    """Validate explicit status changes while allowing idempotent reapplication."""

    def transition(
        self,
        current: IncidentStatus,
        target: IncidentStatus,
    ) -> IncidentStatus:
        """Return the target or raise for an invalid movement."""

        if current == target:
            return current
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise InvalidStatusTransition(f"Cannot transition incident from {current} to {target}")
        return target

    def can_transition(self, current: IncidentStatus, target: IncidentStatus) -> bool:
        """Return whether a movement is idempotent or allowlisted."""

        return current == target or target in _ALLOWED_TRANSITIONS[current]
