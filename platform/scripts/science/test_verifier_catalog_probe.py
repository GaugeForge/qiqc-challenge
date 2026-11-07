"""Fixed local artifact fixtures only. No benchmark import or execution."""

import json
from pathlib import Path

import pytest
from verifier_catalog_probe import assess_result, main, require_startup_evidence


def files(tmp_path, *, reward="0\n", report=True, log="Verifier rejected submission"):
    (tmp_path / "verifier").mkdir()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "verifier/reward.txt").write_text(reward)
    (tmp_path / "process.log").write_text(log)
    if report:
        (tmp_path / "artifacts/score_report.json").write_text(
            json.dumps({"passed": False})
        )
    return tmp_path


@pytest.mark.parametrize("status", [0, 1])
def test_genuine_no_submit_is_not_an_infra_failure(tmp_path, status):
    result = assess_result(status, files(tmp_path))
    assert result["infrastructure_ok"] and result["reward"] == 0
    assert result["fresh_report_present"]


def test_python_crash_exit1_reward0_cannot_pass(tmp_path):
    result = assess_result(1, files(tmp_path, report=False))
    assert not result["infrastructure_ok"]
    assert result["failure_type"] == "RejectionWithoutFreshReport"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Traceback (most recent call last):",
        "ModuleNotFoundError: private-module",
        "Verifier infrastructure failure: test",
        "ImportError: test",
    ],
)
def test_even_masked_exit0_cannot_hide_infra_diagnostics(tmp_path, diagnostic):
    result = assess_result(0, files(tmp_path, log=diagnostic))
    assert not result["infrastructure_ok"]
    assert result["failure_type"] == "VerifierInfrastructureDiagnostic"
    assert "private-module" not in json.dumps(result)


@pytest.mark.parametrize("status", [2, 127, -9, 124])
def test_process_failures_cannot_be_scientific_zero(tmp_path, status):
    assert not assess_result(status, files(tmp_path))["infrastructure_ok"]


def test_exit1_with_passing_reward_is_invalid(tmp_path):
    assert not assess_result(1, files(tmp_path, reward="1"))["infrastructure_ok"]


@pytest.mark.parametrize("content", ["{}", "[]", "not json"])
def test_empty_malformed_or_nonobject_report_fails(tmp_path, content):
    files(tmp_path)
    (tmp_path / "artifacts/score_report.json").write_text(content)
    assert not assess_result(1, tmp_path)["infrastructure_ok"]


def test_symlink_reward_fails(tmp_path):
    files(tmp_path)
    reward = tmp_path / "verifier/reward.txt"
    reward.unlink()
    (tmp_path / "fake.txt").write_text("0")
    reward.symlink_to(tmp_path / "fake.txt")
    assert not assess_result(0, tmp_path)["infrastructure_ok"]


def test_missing_qsim_input_is_never_fabricated(tmp_path):
    absent = tmp_path / "missing"
    with pytest.raises(ValueError):
        require_startup_evidence(absent)
    assert not absent.exists()
    with pytest.raises(FileNotFoundError):
        require_startup_evidence(tmp_path)
    (tmp_path / "experiment_log.jsonl").write_text("")
    require_startup_evidence(tmp_path)


def test_entrypoint_refuses_current_vps():
    if not Path("/root/QQ-Challenge").exists():
        pytest.skip("repository boundary check applies on project VPS")
    with pytest.raises(RuntimeError, match="only inside E2B"):
        main()
