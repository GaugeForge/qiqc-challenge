#!/usr/bin/env python3
"""Read-only Harbor capability audit. Science runtime probes are separate.

This script imports installed provider classes only, never benchmark/agent code.
It does not infer live deployment from capability metadata.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT.parent / "benchmark-assets/qiqcbench-runtime"
OUT = ROOT / "platform/test-results/e2b-topology-check.json"
HARBOR = Path("/root/.local/share/uv/tools/harbor/bin")


def main() -> int:
    result = {
        "checked_at": time.time(),
        "provider": "e2b",
        "compose_supported": False,
        "scientific_e2e_allowed": False,
        "live_compose_tested": False,
    }
    code = """import importlib.metadata,json
from harbor.environments.e2b import E2BEnvironment
cap=E2BEnvironment.capabilities.fget(object.__new__(E2BEnvironment))
print(json.dumps({'harbor_version':importlib.metadata.version('harbor'),'capabilities':cap.model_dump()}))"""
    try:
        process = subprocess.run(
            [str(HARBOR / "python"), "-c", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        result.update(json.loads(process.stdout))
        result["compose_supported"] = result["capabilities"]["docker_compose"]
    except Exception as exc:  # noqa: BLE001 — record failure, never a passing verdict
        result["inspection_error_type"] = type(exc).__name__
    tasks = sorted(p for p in (RUNTIME / "harbor_tasks").iterdir() if p.is_dir())
    result["task_count"] = len(tasks)
    result["compose_task_count"] = sum(
        (p / "environment/docker-compose.yaml").is_file() for p in tasks
    )
    result["verifier_entry_count"] = sum((p / "tests/test.sh").is_file() for p in tasks)
    result["qsim_dockerfile_present"] = (RUNTIME / "docker/qsim/Dockerfile").is_file()
    result["reason"] = (
        "Installed provider advertises docker_compose=false. Separate E2B science probes do not change the Harbor provider capability."
        if not result["compose_supported"]
        else "Capability advertised; live per-service and isolation acceptance still required."
    )
    result["separate_probe_reports"] = [
        "science-catalog-e2b.json",
        "science-verifier-topology.json",
    ]
    result["release_requirements"] = [
        "Per-task scoped materials",
        "Agent/qsim/verifier process and filesystem separation",
        "Authenticated task-only communication",
        "Verifier-owned reward",
        "Network exfiltration negative tests",
        "Complete standard Harbor rollout and controller integration",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
