#!/usr/bin/env python3
"""Read-only gate: all 49 real browser/R2/parity records before any new freeze.

Writes a safe assessment only; never freezes source or switches platform state.
Old relay results, mixed candidate sources, edited browser results, duplicated
batches and missing runtime parity cannot satisfy this gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from native_candidate import identity, load_candidate
from native_release_record import acceptance_evidence_errors, concurrency_evidence, ACCEPTANCE_FIELDS


def runtime_evidence_errors(value):
    """Bounded acceptance never implies full physical capacity or performance."""
    if value.get('acceptance_scope') in ('bounded-functional','bounded-five-minute','bounded-three-minute'):
        errors=[key+'_missing' for key in ('source_runtime_software_verified','original_container_limits_verified') if value.get(key) is not True]
        if type(value.get('full_host_capacity_verified')) is not bool:
            errors.append('full_host_capacity_evidence_missing')
        expected=(value.get('source_runtime_software_verified') is True and
                  value.get('original_container_limits_verified') is True and
                  value.get('full_host_capacity_verified') is True)
        if value.get('source_runtime_parity_verified') is not expected:
            errors.append('source_runtime_parity_claim_inconsistent')
        if value.get('bounded_functional_errors')!=[]:
            errors.append('bounded_runtime_errors_missing_or_nonempty')
        return errors
    if value.get('acceptance_scope') not in (None,'full-runtime'):
        return ['unknown_runtime_acceptance_scope']
    return [] if value.get('source_runtime_parity_verified') is True else ['source_runtime_parity_verified_missing']


def assess(config, evidence_root):
    root = Path(evidence_root).resolve()
    rows = []
    batch_ids = set()
    for ordinal in range(49):
        attempts = []
        for file in sorted(root.rglob("*-r2.json")):
            value = json.loads(file.read_text())
            if value.get("catalog_index") != ordinal:
                continue
            browser_path = file.with_name(file.name[: -len("-r2.json")] + ".json")
            errors = []
            if not browser_path.is_file():
                errors.append("browser_report_missing")
                browser = {}
            else:
                browser = json.loads(browser_path.read_text())
                if hashlib.sha256(browser_path.read_bytes()).hexdigest() != value.get(
                    "browser_report_sha256"
                ):
                    errors.append("browser_report_changed")
            for data in (browser, value):
                if data.get("status") != "passed":
                    errors.append("attempt_failed")
                if data.get("benchmark_release") != identity(config, ordinal):
                    errors.append("source_identity_mismatch")
            if value.get("schema") != "qiqc-native-candidate-evidence/v1":
                errors.append("non_native_evidence")
            if value.get('acceptance_scope')!='bounded-five-minute':
                errors.append('five_minute_acceptance_scope_required')
            errors.extend(runtime_evidence_errors(value))
            errors.extend(acceptance_evidence_errors(value))
            for key in (
                "snapshot_build_inputs_verified",
                "actual_uploaded_zip_executed",
                "native_compose_execution_verified",
                "private_network_verified",
                "standard_harbor_rollout",
            ):
                if value.get(key) is not True:
                    errors.append(key + "_missing")
            if value.get("controller_sha256") != config["controller_sha256"]:
                errors.append("controller_version_mismatch")
            for key in (
                "admin_target_bound_via_browser",
                "invitation_login_password_change",
                "result_visible_in_browser",
                "own_hidden_trace_denied",
                "other_identity_trace_denied",
                "r2_index_written",
                "all_execution_sandboxes_destroyed",
                "local_rollout_cache_removed",
                "nonranking",
            ):
                if browser.get(key) is not True:
                    errors.append("browser_" + key + "_missing")
            if (
                browser.get("batch_id") != value.get("batch_id")
                or browser.get("r2_archive_sha256") != value.get("archive_sha256")
                or browser.get("submission_sha256") != value.get("submission_sha256")
            ):
                errors.append("batch_archive_submission_link_mismatch")
            if (value.get("model_response_evidence") or {}).get(
                "complete_vs_incomplete_distinction_verified"
            ) is not True:
                errors.append("model_terminal_contract_missing")
            if (value.get("model_response_evidence") or {}).get(
                "gateway_request_link_verified"
            ) is not True:
                errors.append("gateway_request_link_missing")
            attempts.append(
                {
                    "evidence": file.relative_to(root).as_posix(),
                    "evidence_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                    "errors": sorted(set(errors)),
                    "batch_id": value.get("batch_id"),
                    "archive_sha256": value.get("archive_sha256"),
                    "acceptance_proof": {key:value.get(key) for key in ACCEPTANCE_FIELDS},
                }
            )
        # Every selected passing attempt is retained with immutable content hash;
        # failures remain visible and are not relabelled by choosing a later run.
        passing = [a for a in attempts if not a["errors"]]
        accepted = passing[-1] if passing else None
        if accepted and accepted["batch_id"] in batch_ids:
            accepted = None
        if accepted:
            batch_ids.add(accepted["batch_id"])
        rows.append(
            {
                "task_index": ordinal,
                "passed": accepted is not None,
                "selected": accepted,
                "attempts": attempts,
            }
        )
    passed = sum(row["passed"] for row in rows)
    overlap=None;concurrency_errors=[]
    if passed==49:
        try:overlap=concurrency_evidence([row['selected']['acceptance_proof'] for row in rows])
        except ValueError as exc:concurrency_errors.append(str(exc))
    else:concurrency_errors.append('concurrency_requires_all49')
    return {
        "schema": "qiqc-native-freeze-gate/v1",
        "candidate_id": config["candidate_id"],
        "source_files_sha256": config["source_files_sha256"],
        "controller_sha256": config["controller_sha256"],
        "status": "eligible_for_explicit_freeze" if passed == 49 and not concurrency_errors else "blocked",
        "concurrency_evidence": overlap,
        "concurrency_errors": concurrency_errors,
        "passed_tasks": passed,
        "required_tasks": 49,
        "acceptance_scope": "49-browser-bounded-five-minute",
        "competition_production_ready": False,
        "frozen": False,
        "platform_switched": False,
        "rows": rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--evidence-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    config, _ = load_candidate(args.candidate)
    if args.output.resolve().is_relative_to(args.evidence_root.resolve()):
        raise SystemExit("Assessment must be outside original evidence tree")
    report = assess(config, args.evidence_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "status",
                    "passed_tasks",
                    "required_tasks",
                    "frozen",
                    "platform_switched",
                )
            }
        )
    )
    return 0 if report["status"] == "eligible_for_explicit_freeze" else 1


if __name__ == "__main__":
    raise SystemExit(main())
