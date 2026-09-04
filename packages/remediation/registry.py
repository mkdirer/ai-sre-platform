"""Closed action registry: typed parameter validation, separate from execution."""

from packages.models.evidence import EvidenceService
from packages.models.investigation import RecommendationAction
from packages.models.remediation import (
    ForbiddenRemediationAction,
    RemediationActionName,
    RollbackDeploymentParams,
)


def action_name_for(action: RecommendationAction) -> RemediationActionName:
    """Resolve the one executable action name or reject as forbidden."""

    if action == RecommendationAction.ROLLBACK_DEPLOYMENT:
        return "rollback_payment_deployment"
    raise ForbiddenRemediationAction(
        "forbidden_action",
        f"action {action.value} is not in the execution registry",
    )


def validate_rollback_params(
    *,
    action: RecommendationAction,
    target: EvidenceService,
    parameters: dict[str, object],
) -> RollbackDeploymentParams:
    """Validate recommendation parameters into typed rollback inputs.

    Accepts the canonical investigator shape (previous deployment identity
    from deployment evidence). Fault scope is never model-controlled:
    execution deterministically disables every payment-service fault.
    """

    action_name_for(action)
    if target != EvidenceService.PAYMENT:
        raise ForbiddenRemediationAction(
            "forbidden_action",
            f"rollback target {target.value} is outside the registry",
        )
    try:
        return RollbackDeploymentParams.model_validate({"service": target.value, **parameters})
    except ValueError as error:
        raise ForbiddenRemediationAction(
            "forbidden_action", f"rollback parameters failed validation: {error}"
        ) from error
