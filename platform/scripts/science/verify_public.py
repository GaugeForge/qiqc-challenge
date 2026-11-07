#!/usr/bin/env python3
"""Transfer trusted qsim evidence in memory to an isolated E2B verifier.

Only public acceptance metadata is retained locally. No raw rollout is persisted
on VPS. This is an integration probe, not Harbor rollout or production grading.
"""

import argparse
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

from e2b_runtime import ROOT, Sandbox, load_key, runtime_env

TASK = "tunable_coupler_cz_netzero"


from archive_validation import validate_archive


def verify(label):
    q = Sandbox.connect(
        json.loads(Path("/tmp/qiqc-science-public-session.json").read_text())[
            "sandbox_id"
        ],
        api_key=load_key(),
    )
    v = Sandbox.connect(
        json.loads(Path("/tmp/qiqc-science-verifier-session.json").read_text())[
            "sandbox_id"
        ],
        api_key=load_key(),
    )
    q.commands.run(
        "tar -czf /root/qiqc-evidence.tar.gz -C /qsim_logs .", user="root", timeout=30
    )
    data = bytes(
        q.files.read("/root/qiqc-evidence.tar.gz", format="bytes", user="root")
    )
    validate_archive(data)
    v.files.write("/root/qiqc-evidence.tar.gz", data, user="root")
    v.commands.run(
        "rm -rf /qsim_logs /logs/artifacts /logs/verifier && mkdir -p /qsim_logs /logs/artifacts /logs/verifier && tar -xzf /root/qiqc-evidence.tar.gz -C /qsim_logs",
        user="root",
        timeout=30,
    )
    result = {
        "task_id": TASK,
        "adapter": label,
        "checked_at": time.time(),
        "topology": "separate-main-qsim-verifier-E2B",
        "evidence_sha256": hashlib.sha256(data).hexdigest(),
        "evidence_bytes": len(data),
        "raw_evidence_persisted_on_vps": False,
        "reward": None,
        "verifier_exit": None,
        "infrastructure_ok": False,
    }
    try:
        r = v.commands.run(
            "bash /opt/runtime/harbor_tasks/" + TASK + "/tests/test.sh",
            envs=runtime_env(TASK),
            user="root",
            timeout=180,
        )
        result["verifier_exit"] = r.exit_code
        reward = v.files.read("/logs/verifier/reward.txt", user="root").strip()
        report = v.files.read("/logs/artifacts/score_report.json", user="root")
        result["reward"] = int(reward) if reward in ("0", "1") else None
        result["score_report_sha256"] = hashlib.sha256(report.encode()).hexdigest()
        result["infrastructure_ok"] = result["verifier_exit"] == 0 and result[
            "reward"
        ] in (0, 1)
    except Exception as exc:  # noqa: BLE001 — record failure, never a passing verdict
        result["failure_type"] = type(exc).__name__
        result["verifier_exit"] = getattr(exc, "exit_code", None)
    out = (
        ROOT
        / "competition-platform/platform/test-results"
        / ("science-verifier-" + label + ".json")
    )
    out.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--label", choices=["topology", "codex", "deepseek", "pi"], required=True
    )
    print(json.dumps(verify(p.parse_args().label)))
