"""E2B-only verifier entry smoke on genuine qsim startup evidence, no answers.

A zero is scientific rejection only when the verifier exits under its documented
contract. No missing input is fabricated, and stale reports cannot carry over.
Importing this module only exposes pure/fixture-tested helpers; execution stays
behind main() and an explicit E2B runtime boundary check.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path("/opt/runtime")
MAX_PROCESS_LOG = 1024 * 1024
INFRA_MARKERS = (
    b"Traceback (most recent call last):",
    b"ModuleNotFoundError:",
    b"ImportError:",
    b"Verifier infrastructure failure",
    b"verifier infrastructure failure",
    b"InfrastructureError:",
    b"EvidenceInfrastructureError:",
)


def read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid_or_large_evidence_file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError("invalid_or_large_evidence_file")
        return data
    finally:
        os.close(fd)


def assess_result(returncode, work):
    """No scientific imports/execution. Classify only fresh verifier outputs."""
    result = {
        "verifier_exit": returncode,
        "reward": None,
        "infrastructure_ok": False,
        "failure_type": None,
        "fresh_report_present": False,
    }
    if returncode not in (0, 1):
        result["failure_type"] = "VerifierProcessExit"
        return result
    try:
        process_log = read_regular(work / "process.log", MAX_PROCESS_LOG)
        if any(marker in process_log for marker in INFRA_MARKERS):
            result["failure_type"] = "VerifierInfrastructureDiagnostic"
            return result
        raw = read_regular(work / "verifier/reward.txt", 16).strip()
        if raw not in (b"0", b"1"):
            result["failure_type"] = "InvalidReward"
            return result
        result["reward"] = int(raw)
        reports = [
            work / "artifacts/score_report.json",
            work / "verifier/score_report.json",
        ]
        for report in reports:
            if report.exists() or report.is_symlink():
                value = json.loads(read_regular(report, 1024 * 1024))
                if not isinstance(value, dict) or not value:
                    raise ValueError("invalid_score_report")
                result["fresh_report_present"] = True
        if returncode == 1 and (
            result["reward"] != 0 or not result["fresh_report_present"]
        ):
            result["failure_type"] = "RejectionWithoutFreshReport"
            return result
        result["infrastructure_ok"] = True
        result["failure_type"] = None
    except (OSError, ValueError, RecursionError) as exc:
        result["failure_type"] = type(exc).__name__
    return result


def require_startup_evidence(log):
    if log.is_symlink() or not log.is_dir():
        raise ValueError("missing_qsim_startup_directory")
    # Empty regular experiment logs are allowed by task no-submission contracts.
    # An absent log is lost infrastructure evidence, not a model rejection.
    read_regular(log / "experiment_log.jsonl", 16 * 1024 * 1024)


def run_one(task, root=ROOT):
    log = Path("/tmp/catalog") / task.name
    work = Path("/tmp/verifier-catalog") / task.name
    row = {
        "task_id": task.name,
        "verifier_started": False,
        "verifier_exit": None,
        "reward": None,
        "infrastructure_ok": False,
        "input": "genuine-qsim-startup-evidence-no-answer",
        "fresh_report_present": False,
    }
    try:
        if not re.fullmatch("[a-z0-9_]+", task.name):
            raise ValueError("invalid_task_identity")
        require_startup_evidence(log)
        # A rerun cannot inherit old reward/report/submission artifacts.
        if work.is_symlink():
            raise ValueError("unsafe_work_directory")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        tests_link = Path("/tests")
        if tests_link.is_symlink():
            tests_link.unlink()
        tests_link.symlink_to(task / "tests")
        (work / "submission").mkdir()
        for dest, target in [
            ("/qsim_logs", log),
            ("/logs/artifacts", work / "artifacts"),
            ("/logs/verifier", work / "verifier"),
        ]:
            if target != log:
                target.mkdir(parents=True)
            path = Path(dest)
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        env = dict(
            os.environ,
            PYTHONPATH=str(root),
            QIQCBENCH_CONFIGS=str(root / "configs"),
            QIQCBENCH_QSIM_LOGS_DIR=str(log),
            QIQCBENCH_VERIFIER_ARTIFACTS_DIR=str(work / "artifacts"),
            QIQCBENCH_VERIFIER_DIR=str(work / "verifier"),
            QIQCBENCH_SUBMISSION_DIR=str(work / "submission"),
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
        )
        if task.name == "time_budgeted_lindbladlearn_6q":
            supplied = work / "supplied-configs"
            shutil.copytree(root / "configs", supplied)
            env.update(
                QIQCBENCH_HIDDEN_DEVICE_PATH=str(
                    supplied / "devices/lindblad_6q_chain_v1.hidden.example.yaml"
                ),
                QIQCBENCH_PUBLIC_DEVICE_PATH=str(
                    supplied / "devices/lindblad_6q_chain_v1.public.yaml"
                ),
                QIQCBENCH_TASK_PUBLIC_DIR=str(
                    supplied / "task_materials" / task.name / "public"
                ),
                QIQCBENCH_LINDBLAD_EXPECTED_CONFIGS=str(root / "configs"),
            )
        row["verifier_started"] = True
        with (work / "process.log").open("w") as out:
            process = subprocess.run(
                ["bash", str(task / "tests/test.sh")],
                env=env,
                stdout=out,
                stderr=out,
                timeout=90,
                check=False,
            )
        row.update(assess_result(process.returncode, work))
    except Exception as exc:  # noqa: BLE001 — fail closed, no exception content in report
        row["failure_type"] = type(exc).__name__
        row["infrastructure_ok"] = False
    return row


def main():
    if os.path.exists("/root/QQ-Challenge") or not (ROOT / "harbor_tasks").is_dir():
        raise RuntimeError("catalog verifier probe must run only inside E2B")
    rows = []
    for task in sorted((ROOT / "harbor_tasks").iterdir()):
        if not task.is_dir():
            continue
        row = run_one(task)
        rows.append(row)
        Path("/tmp/verifier-catalog-result.json").write_text(
            json.dumps(
                {
                    "tasks": rows,
                    "acceptance_contract": "fresh-verdict-infra-diagnostics-v2",
                },
                indent=2,
            )
        )
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
