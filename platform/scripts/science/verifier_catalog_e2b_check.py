#!/usr/bin/env python3
"""Run verifier entry probes in the existing trusted catalog sandbox."""

import argparse
import json
import time
from pathlib import Path

from e2b_runtime import ROOT, Sandbox, load_key
from task_release import identity, verify_release


def run_verifier_catalog(sb, *, output=None):
    actual_release = json.loads(sb.files.read("/opt/runtime/benchmark-release.json"))
    if actual_release != identity(verify_release(ROOT)):
        raise RuntimeError("verifier-catalog-benchmark-release-mismatch")
    for script in ("build_controller_root.py", "verifier_catalog_probe.py"):
        sb.files.write(
            "/root/" + script, Path(__file__).with_name(script).read_text(), user="root"
        )
    sb.commands.run(
        "mkdir -p /app && ln -sfn /opt/runtime/configs /app/configs && ln -sfn /opt/runtime/configs /app/qiqcbench_configs",
        user="root",
    )
    sb.commands.run("python /root/build_controller_root.py", timeout=120, user="root")
    sb.commands.run("python /root/verifier_catalog_probe.py", timeout=1200, user="root")
    result = json.loads(sb.files.read("/tmp/verifier-catalog-result.json", user="root"))
    result.update(
        checked_at=time.time(),
        provider="e2b",
        scope="verifier-startup-no-answer",
        separate_sandbox_per_task=False,
        agent_executed=False,
        eval_overlay=None,
        runtime_provenance="benchmark-assets/qiqcbench-runtime/PROVENANCE.json",
        benchmark_release=actual_release,
        benchmark_release_verified=True,
    )
    result["passed"] = sum(x["infrastructure_ok"] for x in result["tasks"])
    out = output or (
        ROOT / "competition-platform/platform/test-results/science-verifier-catalog-e2b.json"
    )
    out.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", required=True)
    args = parser.parse_args()
    result = run_verifier_catalog(Sandbox.connect(args.sandbox, api_key=load_key()))
    print(json.dumps({"total": len(result["tasks"]), "passed": result["passed"]}))


if __name__ == "__main__":
    main()
