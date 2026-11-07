"""Evidence correlation must reject relabeling and cross-run proof reuse."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts/science/catalog_acceptance_summary.py"
)
spec = importlib.util.spec_from_file_location("catalog_acceptance_summary", PATH)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def pair(index=0, *, completed=False, source="c"):
    release = {
        "release_id": "qiqc49-20261020-v1",
        "runtime_sha256": "a" * 64,
        "task_execution_sha256": "b" * 64,
    }
    browser = dict.fromkeys(summary.BROWSER_CHECKS, True)
    browser.update(
        status="passed",
        catalog_index=index,
        batch_id="a" * 32,
        r2_archive_sha256="e" * 64,
        checked_at="2026-10-20T08:00:00+00:00",
        agent_budget_seconds=300,
        active_run_tokens=0,
        quota_debits=1,
        javascript_errors=0,
        public_trace_readback=False,
        model_usage_rows=2,
        ai_review={"real_advice_available": True, "manual_approval_preserved": True},
        benchmark_release=release,
        score={"passed": 0, "total": 1},
        deployed_binaries_sha256={
            key: "d" * 64 for key in ("api", "worker", "gateway")
        },
        deployed_science_sha256={"private-name-must-not-be-emitted.py": source * 64},
        driver_sha256="f" * 64,
    )
    r2 = {
        "status": "passed",
        "schema": "qiqc-catalog-role-evidence/v1",
        "catalog_index": index,
        "batch_id": "a" * 32,
        "archive_sha256": "e" * 64,
        "agent_budget_seconds": 300,
        "benchmark_release_verified": True,
        "benchmark_release": release,
        "private_boundary_probes_passed": True,
        "fixed_network_probes_passed": True,
        "role_manifests_verified": {
            role: {"file_count": 1, "manifest_sha256": "c" * 64}
            for role in ("main", "qsim", "verifier")
        },
        "qsim_call_count": 2,
        "qsim_success_count": 2,
        "verifier_reward": 0,
        "verifier_exit": 0,
        "verifier_scientific_rejection": False,
        "budget_exhausted": not completed,
        "agent_exit": 0 if completed else 124,
        "agent_stop_reason": "completed" if completed else "budget_exhausted",
    }
    r2["model_response_evidence"] = {
        "complete_vs_incomplete_distinction_verified": True,
        "agent_visible_request_count": 3,
        "agent_visible_http_failure_count": 0,
        "terminal_evidence_source": "gateway_terminal_status_correlated_one_to_one",
        "successful_responses": 2,
        "failed_responses": 1,
        "recovered_failed_responses": 1,
        "unrecovered_failed_responses": 0,
        "all_failures_recovered": True,
        "gateway_attempt_count": 3,
        "gateway_attempt_status_counts": {"200": 2, "502": 1},
        "extra_gateway_attempts": 0,
    }
    return browser, r2


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def evidence(directory, index=0, **kwargs):
    b, r = pair(index, **kwargs)
    name = f"task-{index:02d}-attempt-01.json"
    bsha = write(directory / name, b)
    rname = name[:-5] + "-r2.json"
    rsha = write(directory / rname, r)
    row = {
        "catalog_index": index,
        "attempt": 1,
        "browser_report": name,
        "r2_report": rname,
        "browser_sha256": bsha,
        "r2_sha256": rsha,
        "batch_id": b["batch_id"],
        "state": "passed",
    }
    write(
        directory / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "passed",
            "updated_at": b["checked_at"],
            "attempts": [row],
        },
    )
    return name, b, r, row


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("batch_id", "b" * 32, "batch_id_mismatch"),
        ("archive_sha256", "f" * 64, "archive_sha_mismatch"),
        ("catalog_index", 1, "task_index_mismatch"),
        ("private_boundary_probes_passed", False, "boundary_checks_incomplete"),
        ("role_manifests_verified", {}, "role_checks_incomplete"),
        ("agent_stop_reason", "infra_timeout", "invalid_budget_stop"),
        ("verifier_exit", 1, "verifier_failure"),
    ],
)
def test_cross_run_relabel_and_unclassified_failure_rejected(field, value, code):
    b, r = pair()
    r[field] = value
    with pytest.raises(summary.EvidenceError, match=code):
        summary.validate_pair(b, r, 0)


def test_only_status_labels_never_suffice(tmp_path):
    write(
        tmp_path / "task-00-attempt-01.json", {"status": "passed", "catalog_index": 0}
    )
    write(tmp_path / "task-00-attempt-01-r2.json", {"status": "passed"})
    result = summary.summarize(tmp_path)
    assert result["counts"] == {
        "passed": 0,
        "failed": 1,
        "needs_reverification": 0,
        "running": 0,
        "not_run": 48,
    }


def test_fixed_checker_requires_preserved_failure_and_matching_archive(tmp_path):
    directory = tmp_path / "canary"
    name, _browser, r2, row = evidence(directory, 48)
    original = {"status": "failed", "failure_code": "deployed_role_hash_mismatch"}
    row["r2_sha256"] = write(directory / row["r2_report"], original)
    write(
        directory / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "failed_requires_diagnosis",
            "attempts": [row],
        },
    )
    fixed = name[:-5] + "-r2-fixed.json"
    write(directory / fixed, r2)
    resolution = {
        "status": "passed_after_evidence_driver_fix",
        "catalog_index": 48,
        "browser_report": name,
        "first_r2_failure_preserved": row["r2_report"],
        "verified_r2_report": fixed,
        "harness_rerun": False,
        "actual_role_hashes_match": True,
    }
    write(directory / "resolution.json", resolution)
    result = summary.summarize(tmp_path)
    item = result["tasks"][48]["latest_attempt"]
    assert item["status"] == "passed"
    assert item["failure_history"][0]["report"]["sha256"] == row["r2_sha256"]
    assert item["completion"] == "budget_exhausted"
    r2["archive_sha256"] = "f" * 64
    write(directory / fixed, r2)
    result = summary.summarize(tmp_path)
    assert result["tasks"][48]["status"] == "failed"
    assert (
        result["tasks"][48]["latest_attempt"]["failure_code"] == "archive_sha_mismatch"
    )


def test_suite_hash_detects_original_report_edit(tmp_path):
    name, browser, _, _ = evidence(tmp_path)
    browser["untrusted_extra_label"] = "passed"
    write(tmp_path / name, browser)
    item = summary.summarize(tmp_path)["tasks"][0]["latest_attempt"]
    assert item["status"] == "failed"
    assert item["failure_code"] == "browser_report_changed"


def test_latest_running_does_not_erase_prior_pass_and_versions_are_separate(tmp_path):
    evidence(tmp_path / "old", 0, completed=True, source="c")
    evidence(tmp_path / "other-version", 1, source="d")
    row = {
        "catalog_index": 0,
        "attempt": 1,
        "browser_report": "task-00-attempt-01.json",
        "r2_report": "task-00-attempt-01-r2.json",
        "state": "browser_running",
    }
    write(
        tmp_path / "new" / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "running",
            "updated_at": "2026-10-20T09:00:00+00:00",
            "attempts": [row],
        },
    )
    result = summary.summarize(tmp_path)
    assert result["tasks"][0]["status"] == "running"
    assert (
        result["tasks"][0]["last_verified_pass"]["completion"]
        == "completed_before_budget"
    )
    assert len(result["source_versions"]) == 2
    assert result["all_49_latest_pass_same_deployment"] is False
    text = summary.markdown(result, tmp_path, tmp_path.parent / "report.md")
    assert "private-name-must-not-be-emitted" not in text
    assert "private-name-must-not-be-emitted" not in json.dumps(result)


def test_resolution_cannot_read_outside_evidence_root(tmp_path):
    name, _, _, row = evidence(tmp_path)
    write(tmp_path / row["r2_report"], {"status": "failed"})
    row.pop("r2_sha256")
    write(
        tmp_path / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "failed_requires_diagnosis",
            "attempts": [row],
        },
    )
    write(
        tmp_path / "resolution.json",
        {
            "status": "passed_after_evidence_driver_fix",
            "catalog_index": 0,
            "browser_report": name,
            "first_r2_failure_preserved": row["r2_report"],
            "verified_r2_report": "../sensitive.json",
            "harness_rerun": False,
            "actual_role_hashes_match": True,
        },
    )
    item = summary.summarize(tmp_path)["tasks"][0]["latest_attempt"]
    assert item["status"] == "failed"
    assert item["failure_code"] == "invalid_resolution_reference"


def test_empty_root_has_49_unrun_and_no_acceptance_claim(tmp_path):
    result = summary.summarize(tmp_path)
    assert result["status"] == "incomplete"
    assert result["counts"]["not_run"] == 49
    assert result["formal_hidden_isolation_accepted"] is False


def test_cli_refuses_to_overwrite_evidence(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    original = b'{"immutable":"evidence"}'
    path = root / "suite.json"
    path.write_bytes(original)
    monkeypatch.setattr(
        "sys.argv",
        [
            "summary",
            "--evidence-root",
            str(root),
            "--output-json",
            str(path),
            "--output-md",
            str(tmp_path / "summary.md"),
        ],
    )
    with pytest.raises(
        summary.EvidenceError, match="outputs_must_be_outside_evidence_root"
    ):
        summary.main()
    assert path.read_bytes() == original
    assert not (tmp_path / "summary.md").exists()


def test_input_extra_private_fields_not_copied(tmp_path):
    name, browser, r2, row = evidence(tmp_path)
    browser["do_not_copy"] = "sensitive-extra-value"
    browser["benchmark_release"]["do_not_copy"] = "sensitive-extra-value"
    r2["benchmark_release"]["do_not_copy"] = "sensitive-extra-value"
    row["browser_sha256"] = write(tmp_path / name, browser)
    row["r2_sha256"] = write(tmp_path / row["r2_report"], r2)
    write(
        tmp_path / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "passed",
            "attempts": [row],
        },
    )
    result = summary.summarize(tmp_path)
    assert result["tasks"][0]["status"] == "passed"
    assert "sensitive-extra-value" not in json.dumps(result)


def test_model_strict_recheck_preferred_and_bound_to_original_archive(tmp_path):
    name, _, r2, row = evidence(tmp_path)
    strict = name[:-5] + "-r2-model-strict.json"
    write(tmp_path / strict, r2)
    del r2["model_response_evidence"]
    row["r2_sha256"] = write(tmp_path / row["r2_report"], r2)
    write(
        tmp_path / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "passed",
            "attempts": [row],
        },
    )
    result = summary.summarize(tmp_path)
    item = result["tasks"][0]["latest_attempt"]
    assert item["status"] == "passed"
    assert item["r2_report"]["path"] == strict
    assert item["model_response_evidence"]["recovered_failed_responses"] == 1
    strict_evidence = json.loads((tmp_path / strict).read_text())
    strict_evidence["batch_id"] = "b" * 32
    write(tmp_path / strict, strict_evidence)
    assert (
        summary.summarize(tmp_path)["tasks"][0]["latest_attempt"]["failure_code"]
        == "batch_id_mismatch"
    )


def test_old_http_only_pass_requires_reverification(tmp_path):
    _, _, r2, row = evidence(tmp_path)
    del r2["model_response_evidence"]
    row["r2_sha256"] = write(tmp_path / row["r2_report"], r2)
    write(
        tmp_path / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "passed",
            "attempts": [row],
        },
    )
    result = summary.summarize(tmp_path)
    assert result["tasks"][0]["status"] == "needs_reverification"
    assert result["counts"]["passed"] == 0
    assert result["source_versions"][0]["pending_model_indices"] == [0]
    assert result["source_versions"][0]["verified_indices"] == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("unrecovered_failed_responses", 1, "unrecovered_model_failure"),
        ("recovered_failed_responses", 0, "unrecovered_model_failure"),
        ("successful_responses", 3, "model_counter_mismatch"),
        ("terminal_evidence_source", "http_200_only", "invalid_model_terminal_source"),
        ("gateway_attempt_status_counts", {"200": 3}, "model_gateway_counter_mismatch"),
    ],
)
def test_model_recovery_labels_cannot_override_counters(field, value, code):
    _, r2 = pair()
    r2["model_response_evidence"][field] = value
    with pytest.raises(summary.EvidenceError, match=code):
        summary.validate_model_evidence(r2)


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_legacy_contract_pass_requires_complete_incomplete_reverification(
    tmp_path, flag
):
    _, _, r2, row = evidence(tmp_path)
    model = r2["model_response_evidence"]
    if flag is None:
        model.pop("complete_vs_incomplete_distinction_verified")
    else:
        model["complete_vs_incomplete_distinction_verified"] = flag
    row["r2_sha256"] = write(tmp_path / row["r2_report"], r2)
    write(
        tmp_path / "suite.json",
        {
            "schema": "qiqc-catalog-browser-suite/v1",
            "status": "passed",
            "attempts": [row],
        },
    )
    result = summary.summarize(tmp_path)
    item = result["tasks"][0]["latest_attempt"]
    assert item["status"] == "needs_reverification"
    assert item["failure_code"] == "model_complete_incomplete_distinction_unverified"
    assert result["counts"]["passed"] == 0
    assert result["source_versions"][0]["pending_model_indices"] == [0]
    assert json.loads((tmp_path / row["r2_report"]).read_text())["status"] == "passed"


def test_verified_v2_contract_marker_is_preserved(tmp_path):
    evidence(tmp_path)
    result = summary.summarize(tmp_path)
    item = result["tasks"][0]["latest_attempt"]
    assert item["status"] == "passed"
    assert (
        item["model_response_evidence"]["complete_vs_incomplete_distinction_verified"]
        is True
    )


def test_all_bounded_runs_pass_still_not_original_runtime_parity(tmp_path):
    for index in range(49):
        evidence(tmp_path / f"case-{index:02d}", index)
    result = summary.summarize(tmp_path)
    assert result["counts"]["passed"] == 49
    assert result["bounded_browser_gate_status"] == "passed"
    assert result["status"] == "incomplete"
    assert result["source_runtime_parity"]["status"] == "not_accepted"
    assert result["source_runtime_parity"]["verified"] is False
    text = summary.markdown(result, tmp_path, tmp_path.parent / "report.md")
    assert "source runtime parity" in text
    assert "整体验收尚未通过" in text
