import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "qiqc_pack_failure_recovery", Path(__file__).with_name("pack_failure_recovery.py")
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def receipt(**overrides):
    value = {
        "run_id": "run",
        "submission_sha256": "a" * 64,
        "infrastructure_ok": True,
        "standard_harbor_rollout": True,
        "reward": 0,
        "runtime_seconds": 10800.5,
        "agent_exit": 124,
        "verifier_exit": 1,
        "agent_stop_reason": "budget_exhausted",
        "budget_exhausted": True,
        "verifier_scientific_rejection": True,
        "agent_budget_seconds": 10800,
        "controller_error_class": "native_pack_expanded_limit",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "code",
    [
        "native_pack_entry_limit",
        "native_pack_expanded_limit",
        "native_pack_compressed_limit",
        "native_pack_deadline",
    ],
)
def test_pack_limit_with_trusted_receipt_is_recoverable(code):
    reward, seconds = recovery.validate_receipt(
        receipt(controller_error_class=code), {}, "run", "a" * 64
    )
    assert reward == 0 and seconds == "10800.500000"


def test_budget_terminal_model_failure_is_recoverable():
    value = receipt(controller_error_class="native_model_unrecovered")
    assert recovery.validate_receipt(value, {}, "run", "a" * 64)[0] == 0


def test_natural_completion_model_failure_is_not_recovered():
    value = receipt(
        controller_error_class="native_model_unrecovered",
        agent_exit=0,
        verifier_exit=0,
        agent_stop_reason="completed",
        budget_exhausted=False,
        verifier_scientific_rejection=False,
    )
    with pytest.raises(ValueError, match="model_failure_without_terminal_result"):
        recovery.validate_receipt(value, {}, "run", "a" * 64)


def test_foreign_receipt_is_rejected():
    value = receipt(controller_error_class="native_pack_expanded_limit", run_id="other")
    with pytest.raises(ValueError, match="receipt_run_mismatch"):
        recovery.validate_receipt(value, {}, "run", "a" * 64)
