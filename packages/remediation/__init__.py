"""Approval-gated remediation execution (Stage 10).

Only the registry is re-exported here so persistence can validate
parameters without importing the service layer (which depends back on
persistence). Import adapter/service modules by full path.
"""

from packages.remediation.registry import action_name_for, validate_rollback_params

__all__ = ["action_name_for", "validate_rollback_params"]
