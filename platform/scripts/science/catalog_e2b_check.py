#!/usr/bin/env python3
"""Provision isolated E2B, probe the 49 qsim/MCP services, collect safe evidence."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from e2b_runtime import ROOT, Sandbox, archive_runtime, create, load_key, provision
from task_release import identity, verify_release


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reuse-sandbox")
    p.add_argument("--with-verifiers", action="store_true")
    p.add_argument("--report-prefix", default="science-catalog")
    args = p.parse_args()
    if Path(args.report_prefix).name != args.report_prefix:
        p.error("report prefix must be a filename component")
    if args.with_verifiers and args.reuse_sandbox:
        p.error("combined acceptance requires a freshly provisioned sandbox")
    expected_release = identity(verify_release(ROOT))
    archive = archive_runtime() if not args.reuse_sandbox else None
    sb = (
        Sandbox.connect(args.reuse_sandbox, api_key=load_key())
        if args.reuse_sandbox
        else create()
    )
    try:
        if not args.reuse_sandbox:
            provision(sb, archive)
        else:
            sb.commands.run("ln -sfn /opt/runtime/configs /opt/configs", user="root")
        actual_release = json.loads(sb.files.read("/opt/runtime/benchmark-release.json"))
        if actual_release != expected_release:
            raise RuntimeError("catalog-benchmark-release-mismatch")
        sb.commands.run(
            "rm -f /tmp/catalog_probe.py /tmp/catalog-result.json", user="root"
        )
        sb.files.write(
            "/tmp/catalog_probe.py",
            Path(__file__).with_name("catalog_probe.py").read_text(),
        )
        sb.commands.run("python /tmp/catalog_probe.py", timeout=1000, user="root")
        result = json.loads(sb.files.read("/tmp/catalog-result.json"))
        result.update(
            checked_at=time.time(),
            provider="e2b",
            topology="trusted-qsim-catalog-smoke",
            agent_executed=False,
            harbor_compose=False,
            task_count=len(result["tasks"]),
            runtime_archive_sha256=hashlib.sha256(archive).hexdigest()
            if archive
            else None,
            runtime_provenance="benchmark-assets/qiqcbench-runtime/PROVENANCE.json",
            benchmark_release=actual_release,
            benchmark_release_verified=True,
        )
        result["passed"] = sum(
            all(
                x.get(k)
                for k in ("qsim_health", "mcp_initialized", "tool_list_available")
            )
            for x in result["tasks"]
        )
        out = ROOT / (
            "competition-platform/platform/test-results/" + args.report_prefix + "-e2b.json"
        )
        out.write_text(json.dumps(result, indent=2) + "\n")
        if args.with_verifiers:
            from verifier_catalog_e2b_check import run_verifier_catalog

            verifier_output = out.with_name(args.report_prefix + "-verifier-e2b.json")
            verifier = run_verifier_catalog(sb, output=verifier_output)
            result["verifier_passed"] = verifier["passed"]
            result["verifier_report"] = str(verifier_output)
            result["acceptance_passed"] = (
                result["passed"]
                == result["task_count"]
                == verifier["passed"]
                == len(verifier["tasks"])
                == 49
            )
            out.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "total": result["task_count"],
                    "passed": result["passed"],
                    "report": str(out),
                }
            )
        )
    finally:
        if not args.reuse_sandbox:
            sb.kill()
            if "result" in locals() and "out" in locals():
                result["sandbox_kill_acknowledged"] = True
                out.write_text(json.dumps(result, indent=2) + "\n")
    if args.with_verifiers and not result.get("acceptance_passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
