"""Gated live-eval checks (Stage 09). Skipped without explicit opt-in."""

import os

import pytest

pytestmark = pytest.mark.integration


def test_live_evals_require_explicit_gates() -> None:
    """Live evals never run paid models without explicit flags and budget."""

    if os.getenv("RUN_LIVE_EVALS") != "1":
        pytest.skip("live evals require RUN_LIVE_EVALS=1")
    assert os.getenv("EVAL_LIVE_CONFIRM") == "1", "EVAL_LIVE_CONFIRM=1 is required"
    assert float(os.getenv("EVAL_MAX_COST_USD", "0")) > 0, "EVAL_MAX_COST_USD>0 is required"
