"""Controller callable: actual Harbor trial, three E2B roles, private R2 callback."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from e2b_runtime import ROOT, RUNTIME
from task_release import identity, verify_release

sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
from executor import Cancelled, ProbeError, RolloutUploadError, validate_submission
from harbor.models.trial.config import (
    AgentConfig,
    EnvironmentConfig,
    TaskConfig,
    TrialConfig,
    VerifierConfig,
)
from harbor.trial.trial import Trial
from harbor_agent import REGISTERED_RUNS
from harbor_e2b import RESOURCE_EVENTS, RUN_CHECKS, VERIFIER_EXITS
from public_view import public_view_json, trajectory_view
from verify_public import TASK

# tempfile.tempdir is process-global. A dedicated worker invokes one smoke at a
# time; serialize calls here as a second guard so temp evidence never hits disk.
_EXECUTOR_LOCK = threading.Lock()


def classify_verifier(trial_path, exit_code, reward):
    """Classify the actual isolated verifier; never infer zero from an error."""
    from verifier_catalog_probe import INFRA_MARKERS, read_regular, MAX_PROCESS_LOG
    result = {"verifier_exit": exit_code, "reward": reward, "infrastructure_ok": False,
              "fresh_report_present": False, "verifier_scientific_rejection": False}
    if exit_code not in (0, 1) or type(reward) not in (int, float) or reward not in (0, 1):
        return result
    try:
        log = read_regular(trial_path / "verifier/test-stdout.txt", MAX_PROCESS_LOG)
        result["process_log_sha256"] = hashlib.sha256(log).hexdigest()
        if any(marker in log for marker in INFRA_MARKERS):
            return result
        reports = []
        for relative in ("artifacts/score_report.json", "verifier/score_report.json"):
            path = trial_path / relative
            if path.exists() or path.is_symlink():
                raw = read_regular(path, 1024 * 1024)
                report = json.loads(raw)
                if not isinstance(report, dict) or not report:
                    return result
                reports.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})
        result["reports"] = reports
        result["fresh_report_present"] = bool(reports)
        # Every trial uses a new tmpfs directory and fresh verifier sandbox.
        # A natural exit=1 is accepted only with actual zero AND a fresh report.
        if exit_code == 1 and (reward != 0 or not reports):
            return result
        result["verifier_scientific_rejection"] = exit_code == 1
        result["infrastructure_ok"] = True
    except (OSError, ValueError, RecursionError):
        pass
    return result


def run_harbor_scientific_probe(
    *,
    submission_zip: bytes,
    expected_sha256: str,
    run_id: str,
    model_call,
    upload_rollout,
    cancelled=lambda: False,
    progress=lambda phase: None,
    task_id=TASK,
    timeout_seconds=900,
    model_token=None,
    resource_event=lambda event: None,
    upload_failure=None,
    agent_budget_seconds=300,
    generic_task=False,
):
    kind = validate_submission(submission_zip, expected_sha256)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
        raise ProbeError("Safe run IDs required")
    release = verify_release(ROOT, RUNTIME)
    if task_id not in release["tasks"] or (task_id != TASK and not generic_task):
        raise ProbeError("Unknown or unapproved diagnostic task")
    if type(agent_budget_seconds) is not int or not 30 <= agent_budget_seconds <= 300:
        raise ProbeError("Invalid diagnostic agent budget")
    if timeout_seconds < 60 or timeout_seconds > 1800:
        raise ProbeError("Invalid total timeout")
    with _EXECUTOR_LOCK:
        return _run(
            kind=kind,
            task_id=task_id,
            generic_task=generic_task,
            agent_budget_seconds=agent_budget_seconds,
            submission_zip=submission_zip,
            expected_sha256=expected_sha256,
            run_id=run_id,
            model_call=model_call,
            upload_rollout=upload_rollout,
            cancelled=cancelled,
            progress=progress,
            timeout_seconds=timeout_seconds,
            model_token=model_token,
            resource_event=resource_event,
            upload_failure=upload_failure,
        )


def _run(
    *,
    kind,
    task_id,
    generic_task,
    agent_budget_seconds,    submission_zip,
    expected_sha256,
    run_id,
    model_call,
    upload_rollout,
    cancelled,
    progress,
    timeout_seconds,
    model_token,
    resource_event,
    upload_failure,
):
    started = time.monotonic()
    deadline = started + timeout_seconds

    def check():
        if cancelled():
            raise Cancelled("Harbor science smoke cancelled")
        if time.monotonic() > deadline:
            raise ProbeError("Harbor science smoke deadline exceeded")

    check()
    benchmark_release = identity(verify_release(ROOT, RUNTIME), task_id)
    handle = secrets.token_hex(20)
    token = model_token or secrets.token_urlsafe(32)
    if not isinstance(token, str) or len(token) < 24:
        raise ProbeError("Scoped model token too short")
    cache = Path(tempfile.mkdtemp(prefix="qiqc-harbor-", dir="/dev/shm"))
    cache.chmod(0o700)
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(cache)
    trial_path = cache / run_id
    RESOURCE_EVENTS[str(trial_path)] = resource_event
    RUN_CHECKS[str(trial_path)] = check
    REGISTERED_RUNS[handle] = {
        "benchmark_release": benchmark_release,
        "task_id": task_id,
        "generic_task": generic_task,
        "agent_budget_seconds": agent_budget_seconds,
        "submission_zip": submission_zip,
        "submission_sha256": expected_sha256,
        "model_call": model_call,
        "model_token": token,
        "check": check,
        "progress": progress,
    }
    try:
        config = TrialConfig(
            task=TaskConfig(path=RUNTIME / "harbor_tasks" / task_id),
            trial_name=run_id,
            trials_dir=cache,
            agent=AgentConfig(
                import_path="harbor_agent:OfficialNativeSmoke",
                model_name="tencent/hy4-preview",
                override_timeout_sec=480,
                override_setup_timeout_sec=480,
                kwargs={"adapter": kind, "run_handle": handle},
            ),
            environment=EnvironmentConfig(
                import_path="harbor_e2b:CZEnvironment",
                cpu_enforcement_policy="ignore",
                memory_enforcement_policy="ignore",
                delete=True,
            ),
            verifier=VerifierConfig(override_timeout_sec=240),
            environment_build_timeout_multiplier=8,
        )

        async def run_trial():
            trial = await Trial.create(config)
            return await trial.run()

        progress("provisioning")
        result = asyncio.run(run_trial())
        check()
        dumped = result.model_dump(mode="json")
        verdict = dumped.get("verifier_result") or {}
        reward = (verdict.get("rewards") or {}).get("reward")
        exit_code = VERIFIER_EXITS.get(str(trial_path))
        verifier_classification = classify_verifier(trial_path, exit_code, reward)
        (trial_path / "verifier/classification.json").write_text(json.dumps(verifier_classification, sort_keys=True) + "\n")
        if dumped.get("exception_info") or not verifier_classification["infrastructure_ok"] or (not generic_task and exit_code != 0):
            failure = (dumped.get("exception_info") or {}).get(
                "exception_type", "verifier-invalid"
            )
            message = (dumped.get("exception_info") or {}).get("exception_message", "")
            # Only our trusted executor's fixed, credential-free messages leave
            # the private trial; provider text remains private.
            detail = message if failure == "ProbeError" else ""
            if failure == "RuntimeError" and re.fullmatch(
                r"Boundary probe command failed \(exit [0-9]+\)|Agent boundary smoke failed",
                message,
            ):
                detail = message
            raise ProbeError(
                "Harbor trial failed: " + failure + (": " + detail if detail else "")
            )
        agent = (dumped.get("agent_result") or {}).get("metadata") or {}
        # AgentContext is normally serialized under agent_result; don't invent
        # model usage or runtime when the installed Harbor schema differs.
        if not agent:
            context_file = trial_path / "result.json"
            raw = json.loads(context_file.read_text())
            agent = (raw.get("agent_result") or {}).get("metadata") or {}
        bounded_stop = (generic_task and agent.get("budget_exhausted") is True and agent.get("agent_stop_reason") == "budget_exhausted" and agent.get("agent_exit") in (124, 137))
        if (agent.get("agent_exit") != 0 and not bounded_stop) or not agent.get("model_calls"):
            raise ProbeError("Missing native agent metadata or model-call evidence")
        required = [
            "config.json",
            "result.json",
            "agent/trajectory.jsonl",
            "verifier/reward.txt",
            "artifacts/qsim_logs/experiment_log.jsonl",
        ]
        if not all((trial_path / name).is_file() for name in required):
            raise ProbeError("Incomplete standard Harbor trial tree")
        execution = dumped.get("agent_execution") or {}
        from datetime import datetime

        runtime = round(
            (
                datetime.fromisoformat(execution["finished_at"])
                - datetime.fromisoformat(execution["started_at"])
            ).total_seconds(),
            6,
        )
        view = {
            "mode": "scientific-smoke",
            "standard_harbor_rollout": True,
            "task_id": task_id,
            "reward": int(reward),
            "agent_exit": agent.get("agent_exit", 0),
            "verifier_exit": exit_code,
            "verifier_scientific_rejection": verifier_classification["verifier_scientific_rejection"],
            "runtime_seconds": runtime,
            "model_calls": len(agent.get("model_calls", [])),
            "summary": "Actual Harbor trial with native Harness, Hy4, qsim and independent verifier. Diagnostic run, not leaderboard eligible.",
        }
        if not generic_task:
            view.update(
                trajectory_view(
                trial_path / "agent/trajectory.jsonl",
                    secrets=REGISTERED_RUNS[handle].get("redaction_secrets", [token]),
                )
            )
        else:
            # Catalog acceptance may include hidden tasks: no title or trajectory
            # is exported to the team-facing public-view artifact.
            view.pop("task_id", None)
            view["trajectory_available"] = False
            view["execution_outcome"] = agent.get("execution_outcome")
        serialized_view = public_view_json(view)
        (trial_path / "public-view.json").write_text(serialized_view)
        (trial_path / "public-view.txt").write_text(serialized_view)
        (trial_path / "benchmark-release.json").write_text(
            json.dumps(benchmark_release, sort_keys=True) + "\n"
        )
        from harbor_e2b import ROLE_MANIFESTS, ROLE_DEPLOYMENT_CHECKS
        role_manifests = ROLE_MANIFESTS.get(str(trial_path), {})
        deployment_checks = ROLE_DEPLOYMENT_CHECKS.get(str(trial_path), {})
        if generic_task and (set(role_manifests) != {"main", "qsim", "verifier"} or set(deployment_checks) != {"main", "qsim", "verifier"}):
            raise ProbeError("Incomplete isolated role deployment evidence")
        (trial_path / "artifacts/role-deployment-checks.json").write_text(json.dumps(deployment_checks, sort_keys=True) + "\n")
        (trial_path / "artifacts/role-manifests.json").write_text(json.dumps(role_manifests, sort_keys=True) + "\n")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
            for path in sorted(trial_path.rglob("*")):
                if path.is_symlink():
                    raise ProbeError("Unexpected symlink in Harbor rollout")
                if path.is_file():
                    tf.add(
                        path, arcname=str(path.relative_to(trial_path)), recursive=False
                    )
        archive = buffer.getvalue()
        sha = hashlib.sha256(archive).hexdigest()
        output = {
            "benchmark_release": benchmark_release,
            "mode": "scientific-smoke",
            "task_id": task_id,
            "run_id": run_id,
            "submission_sha256": expected_sha256,
            "adapter": kind,
            "reward": int(reward),
            "agent_exit": agent.get("agent_exit", 0),
            "verifier_exit": exit_code,
            "verifier_scientific_rejection": verifier_classification["verifier_scientific_rejection"],
            "infrastructure_ok": True,
            "agent_budget_seconds": agent_budget_seconds,
            "budget_exhausted": agent.get("budget_exhausted", False),
            "agent_stop_reason": agent.get("agent_stop_reason", "completed"),
            "execution_outcome": agent.get("execution_outcome", "completed"),
            "tool_contract_sha256": agent.get("tool_contract_sha256"),
            "tool_contract_count": agent.get("tool_contract_count"),
            "qsim_relay_calls": agent.get("qsim_relay_calls"),
            "runtime_seconds": runtime,
            "model_calls": agent.get("model_calls", []),
            "model_recovery": agent.get("model_recovery"),
            "model_contract": agent.get("model_contract"),
            "standard_harbor_rollout": True,
            "leaderboard_eligible": False,
            "harbor_version": "0.23.0",
            "required_standard_files_present": True,
            "boundary_probe": agent.get("boundary_probe"),
            "native_runtime_seconds": agent.get("runtime_seconds"),
            "timing_includes_boundary_probe": True,
            "role_manifests": role_manifests,
            "role_deployment_checks": deployment_checks,
        }
        key = f"rollouts/scientific-smoke/{run_id}/{sha}.tar.gz"
        metadata = {
            "sha256": sha,
            "run_id": run_id,
            "task_id": task_id,
            "submission_sha256": expected_sha256,
            "mode": "scientific-smoke",
            "format": "harbor-trial-0.23",
            "reward": str(int(reward)),
            "seconds": format(runtime, ".6f"),
        }
        progress("uploading_rollout")
        for attempt in range(3):
            try:
                output["rollout"] = upload_rollout(key, archive, metadata)
                break
            except Exception:  # noqa: BLE001 — retry then retain archive in typed failure
                if attempt == 2:
                    raise RolloutUploadError(archive, metadata)
                time.sleep(attempt + 1)
        output["total_seconds"] = round(time.monotonic() - started, 3)
        progress("completed")
        return output
    finally:
        failure = sys.exc_info()[1]
        preserve_failed_cache = False
        if failure is not None and upload_failure is not None:
            # Complete actual Harbor files, never synthesized scoring evidence.
            # The callback must durably retain bytes before it returns.
            try:
                archive, failure_metadata = failure_archive(
                    trial_path, run_id, expected_sha256, type(failure).__name__
                )
                upload_failure(archive, failure_metadata)
            except Exception:  # noqa: BLE001 — retain private evidence on any spool failure
                preserve_failed_cache = True
                print(
                    "failure-rollout-spool-error; private cache retained=" + str(cache),
                    file=sys.stderr,
                )
        REGISTERED_RUNS.pop(handle, None)
        VERIFIER_EXITS.pop(str(trial_path), None)
        RESOURCE_EVENTS.pop(str(trial_path), None)
        RUN_CHECKS.pop(str(trial_path), None)
        from harbor_e2b import ROLE_MANIFESTS, ROLE_DEPLOYMENT_CHECKS
        ROLE_MANIFESTS.pop(str(trial_path), None)
        ROLE_DEPLOYMENT_CHECKS.pop(str(trial_path), None)
        tempfile.tempdir = previous_tempdir
        if preserve_failed_cache or (
            os.environ.get("QIQC_KEEP_FAILED_TMPFS") == "1" and failure is not None
        ):
            print("private-failed-trial-cache=" + str(cache), file=sys.stderr)
        else:
            shutil.rmtree(cache)


def failure_archive(trial_path, run_id, submission_sha256, failure_class):
    """Private failure artifact: raw standard files plus safe envelope, no score."""
    metadata = {
        "mode": "scientific-smoke-failure",
        "run_id": run_id,
        "submission_sha256": submission_sha256,
        "error_class": failure_class,
        "format": "harbor-trial-0.23"
        if (trial_path / "config.json").is_file()
        else "controller-diagnostic",
        "scoring_eligible": "false",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(trial_path.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.add(
                    path, arcname=str(path.relative_to(trial_path)), recursive=False
                )
        envelope = json.dumps(metadata, indent=2).encode()
        info = tarfile.TarInfo("failure-envelope.json")
        info.size = len(envelope)
        archive.addfile(info, io.BytesIO(envelope))
    data = buffer.getvalue()
    metadata["sha256"] = hashlib.sha256(data).hexdigest()
    return data, metadata
