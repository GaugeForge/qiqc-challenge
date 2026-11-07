#!/usr/bin/env python3
"""Read back a candidate browser run from private R2; pure evidence inspection.

This command never executes benchmark code or changes the database. A passing
bounded run is distinct from source/runtime parity and the all-49 freeze gate.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import re
import sqlite3
import sys
import tarfile
from pathlib import Path

from archive_validation import validate_archive
from native_candidate import identity, load_candidate
from native_contract import build_plan, runtime_verification
from native_controller import digest as controller_digest
from native_controller import verify as verify_controller
from native_final_containers import validate_final_containers
from native_host_network import validate_receipt as validate_host_output_guard
from native_security import validate_receipt as validate_security_receipt
from verify_cloud_relay_evidence import (
    EvidenceError,
    fetch_archive,
    lookup_run,
    read_local_json,
    read_r2_config,
)

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
from model_response_contract import native_recovery_summary
from native_readiness import (
    IDENTITY_KEYS,
    MODEL_KEYS,
    validate_progress,
)
from native_readiness import (
    digest as readiness_digest,
)
from native_worker_bridge import validate_receipt


def verify_model_ledger(events, usage, expected_stop=None):
    """Correlate every real provider attempt, including pre-header cancellation."""
    groups = {}
    request_ids = set()
    for row in usage:
        request_id, correlation = row.get("request_id"), row.get("request_correlation")
        if any(
            not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{32}", v)
            for v in (request_id, correlation)
        ):
            raise EvidenceError("candidate_gateway_request_identity_missing")
        groups.setdefault(correlation, []).append(row)
    seen = set()
    for event in events:
        correlation = event.get("request_correlation")
        if correlation in seen or correlation not in groups:
            raise EvidenceError("candidate_gateway_request_link_mismatch")
        seen.add(correlation)
        rows = groups[correlation]
        row_ids = {row["request_id"] for row in rows}
        if len(row_ids) != 1 or request_ids & row_ids:
            raise EvidenceError("candidate_gateway_request_identity_ambiguous")
        request_ids.update(row_ids)
        if event.get("gateway_request_id") not in row_ids and not (
            event.get("gateway_request_id") is None and event.get("status") == 499
        ):
            raise EvidenceError("candidate_gateway_request_link_mismatch")
        queued_cancel = rows[-1].get("error_class") == "client_cancelled_before_upstream"
        if queued_cancel and (event.get("status") != 499
                or rows[-1].get("status") != 499
                or type(rows[-1].get("upstream_attempt")) is not int
                or rows[-1]["upstream_attempt"] != 0
                or any(type(rows[-1].get(key)) is not int or rows[-1][key] != 0
                       for key in ("input_tokens", "output_tokens", "cached_tokens"))):
            raise EvidenceError("candidate_queue_cancel_not_proven")
        provider_rows = rows[:-1] if queued_cancel else rows
        if [row["upstream_attempt"] for row in provider_rows] != list(range(1, len(provider_rows) + 1)):
            raise EvidenceError("candidate_gateway_attempt_sequence_mismatch")
        if any(row["status"] not in (200, 429, 499, 502, 503, 504) for row in rows):
            raise EvidenceError("candidate_gateway_nontransient_failure")
        final = rows[-1]["status"]
        completed = (event.get("response_contract") or {}).get("success") is True
        if event["status"] == 499:
            if (
                final != 499
                or rows[-1].get("error_class") not in ("client_cancelled", "client_cancelled_before_upstream")
                # Only pre-response HTTP failures can be retried within this
                # same gateway request. A streamed/body failure is terminal;
                # cancellation must never conceal that prior failure.
                or any(row["status"] not in (429, 502, 503, 504)
                       or row.get("error_class") != "provider_http_error"
                       for row in rows[:-1])
            ):
                raise EvidenceError("candidate_budget_cancel_not_proven_in_ledger")
        elif (
            (completed and final != 200)
            or final == 499
            or (event["status"] != 200 and final != event["status"])
        ):
            raise EvidenceError("candidate_gateway_response_status_mismatch")
    if seen != set(groups) or not seen:
        raise EvidenceError("candidate_gateway_unaccounted_attempt")
    recovery = native_recovery_summary(events, expected_stop=expected_stop)
    if not recovery["all_failures_recovered"]:
        raise EvidenceError("candidate_model_response_not_verified")
    return {
        "gateway_request_link_verified": True,
        "gateway_logical_requests": len(seen),
        "gateway_provider_attempts": sum(row["upstream_attempt"] != 0 for row in usage),
        "gateway_cancelled_before_upstream": sum(row.get("error_class") == "client_cancelled_before_upstream" for row in usage),
        "gateway_failed_provider_attempts": sum(
            row["status"] not in (200, 499) for row in usage
        ),
        "gateway_cancelled_provider_attempts": sum(
            row["status"] == 499 and row["upstream_attempt"] != 0 for row in usage
        ),
    }


def verified_agent_timing(result, receipt):
    timing = result.get("agent_execution") or {}
    try:
        start = datetime.datetime.fromisoformat(timing["started_at"])
        finish = datetime.datetime.fromisoformat(timing["finished_at"])
        if start.tzinfo is None or finish.tzinfo is None:
            raise ValueError("timezone required")
        elapsed = (finish-start).total_seconds()
        budget = receipt.get("agent_budget_seconds", 300)
        if type(budget) is not int or budget not in (180, 300):
            raise ValueError("invalid budget")
        if not budget-1 <= elapsed < budget+60 or abs(elapsed-receipt["runtime_seconds"]) > 30:
            raise ValueError("duration mismatch")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError("candidate_harbor_agent_timing_invalid") from exc
    return {"agent_execution_started_at": start.astimezone(datetime.UTC).isoformat(),
            "agent_execution_finished_at": finish.astimezone(datetime.UTC).isoformat(),
            "harbor_agent_timing_verified": True}


def inspection_identity(config, ordinal, supplied):
    """Derive native identity from trusted catalog/original TOML, not report claims."""
    expected = identity(config, ordinal, supplied['release_id'])
    if supplied.get('acceptance_scope') == 'practice-native':
        from task_catalog import upgrade_catalog
        catalog = json.loads(Path(config['science_task_set_path']).read_text())
        if (catalog.get('schema') != 'qiqc-scientific-task-set/v2' or
                catalog.get('release_id') != supplied['release_id'] or
                upgrade_catalog(catalog, config['source_root']) != catalog):
            raise EvidenceError('candidate_native_catalog_mismatch')
        task = catalog['tasks'][ordinal]
        budget = task['limits']['agent_seconds'] if task.get('evaluation_role') != 'infrastructure' else min(catalog['agent_budget_seconds'], task['limits']['agent_seconds'])
        expected.update(native_limits=task['limits'], agent_budget_seconds=budget,
                        acceptance_scope='practice-native')
    return expected


def archived_qsim_summary(selected):
    from native_acceptance_stop import qsim_summary
    raw_log = selected.get('artifacts/qsim_logs/experiment_log.jsonl')
    verifier_copy = selected.get('verifier/artifacts/experiment_log.jsonl')
    if raw_log is None or verifier_copy != raw_log:
        raise EvidenceError('candidate_archived_qsim_interaction_not_verified')
    return qsim_summary(raw_log)


def inspect(archive, index, browser, config, audit, usage, *, recorded_controller=False):
    ordinal = browser.get("catalog_index")
    expected = inspection_identity(config, ordinal, browser['benchmark_release'])
    if (
        browser.get("benchmark_release") != expected
        or browser.get("execution_topology") != "e2b-native-compose"
    ):
        raise EvidenceError("candidate_browser_identity_mismatch")
    if hashlib.sha256(archive).hexdigest() != index["sha256"]:
        raise EvidenceError("candidate_archive_digest_mismatch")
    validate_archive(archive)
    required = {
        "result.json",
        "native-receipt.json",
        "controller-manifest.json",
        "benchmark-release.json",
        "model-evidence.json",
        "qsim-progress.json",
        "verifier/reward.txt",
        "verifier/test-stdout.txt",
    }
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            if member.name in required | {"acceptance-readiness.json", "artifacts/qsim_logs/experiment_log.jsonl", "verifier/artifacts/experiment_log.jsonl"}:
                if (
                    member.name in selected
                    or not member.isfile()
                    or member.size > (16 if member.name.endswith('experiment_log.jsonl') else 4) * 1024 * 1024
                ):
                    raise EvidenceError("candidate_invalid_evidence_member")
                selected[member.name] = source.extractfile(member).read()
    if not required <= selected.keys():
        raise EvidenceError("candidate_missing_standard_evidence")
    receipt = json.loads(selected["native-receipt.json"])
    validate_receipt(
        receipt, {"run_id": index["run_id"], "sha256": index["submission_sha256"],
                  "agent_budget_seconds": expected.get("agent_budget_seconds", 300)}
    )
    if (
        receipt.get("candidate_identity") != expected
        or json.loads(selected["benchmark-release.json"]) != expected
    ):
        raise EvidenceError("candidate_archived_source_mismatch")
    controller = json.loads(selected["controller-manifest.json"])
    # Historical readback checks the executed immutable manifest. It does not
    # certify later workspace bytes as the executed release. Runtime admission
    # and the default inspector still verify current source and binary bytes.
    required_controller = (json.loads(Path(config['controller_manifest']).read_text())
                           if recorded_controller else verify_controller(config))
    if (
        controller != required_controller
        or controller_digest(controller) != config["controller_sha256"]
        or receipt.get("controller_sha256") != config["controller_sha256"]
    ):
        raise EvidenceError("candidate_controller_identity_mismatch")
    pinned_build = {
        name: config.get(name) for name in ("build_plan_sha256", "build_receipt_sha256")
    }
    build_verified = (
        all(isinstance(v, str) and len(v) == 64 for v in pinned_build.values())
        and receipt.get("build_input_digests") == pinned_build
    )
    result = json.loads(selected["result.json"])
    timing_evidence = verified_agent_timing(result, receipt) if expected["acceptance_scope"] in ("bounded-five-minute", "bounded-three-minute") else {}
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if (
        result.get("exception_info")
        or metadata.get("submission_sha256") != index["submission_sha256"]
        or metadata.get("execution_kind") != "uploaded-native-zip"
    ):
        raise EvidenceError("candidate_uploaded_execution_not_proven")
    if (
        reward != receipt["reward"]
        or reward != index["reward"]
        or float(selected["verifier/reward.txt"]) != reward
    ):
        raise EvidenceError("candidate_verifier_score_mismatch")
    model = json.loads(selected["model-evidence.json"])
    events = model.get("events")
    if not isinstance(events, list) or not events or len(events) > 100:
        raise EvidenceError("candidate_model_evidence_missing")
    expected_stop = {
        "run_id": index["run_id"],
        "attempt": index["attempt"],
        "submission_sha256": index["submission_sha256"],
        "candidate_identity": expected,
    }
    recovery = native_recovery_summary(events, expected_stop=expected_stop)
    if not recovery["all_failures_recovered"] or any(
        (e.get("response_contract") or {}).get("schema") != "qiqc-model-response/v2"
        or e.get("status") not in (200, 429, 499, 502, 503, 504)
        for e in events
    ):
        raise EvidenceError("candidate_model_response_not_verified")
    for event in events:
        if not event.get("adapter_request_sha256") or event[
            "adapter_request_sha256"
        ] != event.get("forwarded_request_sha256"):
            raise EvidenceError("candidate_model_request_mutated")
    qsim_progress = json.loads(selected["qsim-progress.json"])
    observed_qsim = (
        (receipt.get("runtime_observation") or {}).get("container_inspects") or {}
    ).get("qsim") or {}
    qsim_origin = 'live-progress-receipt'
    if (expected.get('acceptance_scope') == 'practice-native' and
            qsim_progress == {'schema': 'qiqc-native-qsim-observation/v1', 'observed': False, 'reason': 'no_progress_receipt'}):
        # Earlier native releases omitted the progress poller. Recheck the
        # original service log inside the hash-verified immutable archive;
        # never rewrite the archive or pretend a live receipt existed.
        qsim_progress = archived_qsim_summary(selected)
        qsim_origin = 'archived-native-service-log-with-identical-verifier-copy'
    elif (expected.get('acceptance_scope') == 'practice-native' and
          qsim_progress.get('schema') == 'qiqc-native-qsim-progress/v1' and
          qsim_progress.get('success_count') == 0 and
          all(qsim_progress.get(k) == expected_stop[k] for k in IDENTITY_KEYS) and
          qsim_progress.get('qsim_container_id') == observed_qsim.get('Id')):
        qsim_progress = archived_qsim_summary(selected)
        qsim_origin = 'archived-native-service-log-with-identical-verifier-copy'
    elif not validate_progress(qsim_progress, expected_stop) or qsim_progress.get(
            "qsim_container_id") != observed_qsim.get("Id"):
        raise EvidenceError("candidate_qsim_interaction_not_verified")
    if expected.get("diagnostic_model") or expected.get("provider_profile"):
        selected_model = expected["model_id"]
        if (receipt.get("model_id") != selected_model
                or receipt.get("diagnostic_model") != expected.get("diagnostic_model", "")
                or any(event.get("model_id") != selected_model for event in events)
                or any(row.get("model") != selected_model for row in usage)):
            raise EvidenceError("candidate_diagnostic_model_mismatch")
    if expected.get("provider_profile") and any(event.get("provider_profile") != expected["provider_profile"] for event in events):
        raise EvidenceError("candidate_provider_profile_mismatch")
    ledger = verify_model_ledger(events, usage, expected_stop)
    early = receipt.get("agent_stop_reason") == "acceptance_early_stop"
    if early:
        proof_raw = selected.get("acceptance-readiness.json")
        if not proof_raw or hashlib.sha256(proof_raw).hexdigest() != receipt.get(
            "acceptance_readiness_sha256"
        ):
            raise EvidenceError("candidate_early_readiness_digest_mismatch")
        proof = json.loads(proof_raw)
        qsim = proof.get("qsim") or {}
        qsim_container = (
            (receipt.get("runtime_observation") or {}).get("container_inspects") or {}
        ).get("qsim") or {}
        if (
            proof.get("schema") != "qiqc-native-acceptance-readiness/v1"
            or any(proof.get(k) != expected_stop[k] for k in IDENTITY_KEYS)
            or not validate_progress(qsim, expected_stop)
            or readiness_digest(qsim) != proof.get("qsim_progress_sha256")
            or qsim.get("qsim_container_id") != qsim_container.get("Id")
            or not any(
                proof.get("model") == {k: event.get(k) for k in MODEL_KEYS}
                and event.get("status") == 200
                and (event.get("response_contract") or {}).get("success") is True
                for event in events
            )
        ):
            raise EvidenceError("candidate_early_readiness_not_verified")
    for event in events:
        if event.get("status") == 499:
            marker = event.get("planned_cancellation") or {}
            if marker.get("agent_stop_reason") == "acceptance_early_stop":
                compact = (
                    {
                        "model_gateway_request_id": proof["model"][
                            "gateway_request_id"
                        ],
                        "model_adapter_request_sha256": proof["model"][
                            "adapter_request_sha256"
                        ],
                        "qsim_progress_sha256": proof["qsim_progress_sha256"],
                        "qsim_success_count": proof["qsim"]["success_count"],
                    }
                    if early
                    else None
                )
                if (
                    not early
                    or marker.get("acceptance_readiness_sha256")
                    != receipt.get("acceptance_readiness_sha256")
                    or marker.get("readiness") != compact
                    or receipt.get("acceptance_early_stop_verified") is not True
                ):
                    raise EvidenceError("candidate_early_stop_proof_link_mismatch")
            main = (
                (receipt.get("runtime_observation") or {}).get("container_inspects")
                or {}
            ).get("main") or {}
            if (
                marker.get("main_container_id") != main.get("Id")
                or (receipt.get("security_evidence") or {}).get(
                    "main_stopped_before_verifier"
                )
                is not True
            ):
                raise EvidenceError("candidate_budget_stop_container_not_verified")
    security = receipt.get("security_evidence") or {}
    firewall = (security.get("agent") or {}).get("firewall") or {}
    if (
        bool(validate_security_receipt(firewall))
        or (security.get("verifier") or {}).get("network_mode") != "none"
    ):
        raise EvidenceError("candidate_network_isolation_not_verified")
    if receipt.get("execution_host_network") != {
        "allow_public_traffic": False,
        "deny_out": ["0.0.0.0/0"],
        "external_tcp_denied": True,
        "probe_count": 2,
    }:
        raise EvidenceError("candidate_outer_host_egress_not_verified")
    if (
        validate_host_output_guard(receipt.get("host_output_guard") or {})
        or (receipt.get("host_output_guard") or {}).get("host_output_reject_verified")
        is not True
        or receipt.get("guard_sdk_roundtrip_verified") is not True
    ):
        raise EvidenceError("candidate_host_output_guard_not_verified")
    terminal = receipt.get("final_container_evidence") or {}
    if validate_final_containers(terminal, receipt.get("runtime_observation") or {},
                                 receipt.get("security_evidence") or {}, receipt.get("verifier_exit")):
        raise EvidenceError("candidate_terminal_runtime_not_verified")
    plan = build_plan(Path(config["source_root"]), Path(config["source_audit"]))
    verification = runtime_verification(
        plan,
        sorted(audit["competition_task_ids"])[ordinal],
        receipt.get("runtime_observation") or {},
    )
    if verification["bounded_functional_errors"]:
        raise EvidenceError("candidate_original_runtime_not_verified")
    return {
        **timing_evidence,
        "schema": "qiqc-native-candidate-evidence/v1",
        "status": "passed",
        "catalog_index": ordinal,
        "candidate_id": config["candidate_id"],
        "source_files_sha256": config["source_files_sha256"],
        "benchmark_release": expected,
        "model_id": expected.get("model_id", "tencent/hy4-preview"),
        "diagnostic_model": expected.get("diagnostic_model", ""),
        "official_model_equivalence_verified": False,
        "batch_id": index["batch_id"],
        "run_id": index["run_id"],
        "archive_sha256": index["sha256"],
        "submission_sha256": index["submission_sha256"],
        "actual_uploaded_zip_executed": True,
        "native_compose_execution_verified": True,
        "terminal_runtime_verified": True,
        "terminal_runtime_sha256": hashlib.sha256(json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "private_network_verified": True,
        "standard_harbor_rollout": True,
        # A normal submitted program may finish without calling qsim. Keep
        # that absence visible; it is not an infrastructure error or proof of
        # successful tool use. Legacy diagnostic/freezing gates still require
        # this flag to be true, and native acceptance reports inspect it.
        "qsim_successful_interaction_verified": qsim_progress['success_count'] > 0,
        "qsim_evidence_origin": qsim_origin,
        "qsim_success_count": qsim_progress['success_count'],
        "qsim_progress_sha256": readiness_digest(qsim_progress),
        "reward": int(reward),
        "agent_stop_reason": receipt["agent_stop_reason"],
        "budget_exhausted": receipt.get("budget_exhausted") is True,
        "model_response_evidence": {
            **recovery,
            **ledger,
            "complete_vs_incomplete_distinction_verified": True,
        },
        "acceptance_scope": expected["acceptance_scope"],
        "observation_policy": receipt.get("observation_policy"),
        "execution_duration_class": receipt.get("execution_duration_class"),
        "agent_elapsed_seconds": receipt["runtime_seconds"],
        "agent_exit": receipt["agent_exit"],
        "agent_budget_seconds": receipt["agent_budget_seconds"],
        **verification,
        "acceptance_early_stop_verified": early,
        "acceptance_readiness_sha256": receipt.get("acceptance_readiness_sha256"),
        "controller_sha256": config["controller_sha256"],
        "controller_verification": "recorded-execution-manifest" if recorded_controller else "current-source-and-binaries",
        "build_input_digests": receipt.get("build_input_digests"),
        "snapshot_build_inputs_verified": build_verified,
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite evidence")
    report = {"schema": "qiqc-native-candidate-evidence/v1", "status": "failed"}
    try:
        config, audit = load_candidate(args.candidate)
        browser = read_local_json(args.report)
        dbpath = Path(config["data"]) / "platform.db"
        index = lookup_run(dbpath, browser, catalog=True)
        with sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True) as db:
            target = db.execute(
                "SELECT task_index,agent_budget_seconds,release_id,submission_sha256 FROM batch_scientific_targets WHERE batch_id=?",
                (index["batch_id"],),
            ).fetchone()
            db.row_factory = sqlite3.Row
            usage = [
                dict(r)
                for r in db.execute(
                    "SELECT request_id,request_correlation,upstream_attempt,status,error_class,model,input_tokens,output_tokens,cached_tokens FROM usage WHERE run_id=? AND attempt=? ORDER BY rowid",
                    (index["run_id"], index["attempt"]),
                )
            ]
        if target != (
            browser["catalog_index"],
            config.get("agent_budget_seconds", 300),
            browser["benchmark_release"]["release_id"],
            index["submission_sha256"],
        ):
            raise EvidenceError("candidate_persisted_target_mismatch")
        archive = fetch_archive(read_r2_config(Path(config["r2_env"])), index)
        report = inspect(archive, index, browser, config, audit, usage)
        report["browser_report_sha256"] = hashlib.sha256(
            args.report.read_bytes()
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 — fixed public-safe failure classification.
        report["failure_code"] = (
            str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
