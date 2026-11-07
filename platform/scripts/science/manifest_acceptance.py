#!/usr/bin/env python3
"""One offline command: 49 metadata manifests plus actual attack fixture tests."""

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from manifest_catalog_check import DEFAULT_ROOT, audit_catalog
from task_manifest import write_report


def main():
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parents[4]
    parser.add_argument(
        "--out",
        type=Path,
        default=project
        / "competition-platform/platform/test-results/science-49-input-manifests.json",
    )
    args = parser.parse_args()
    report = audit_catalog(DEFAULT_ROOT)
    tests = project / "competition-platform/platform/tests"
    with tempfile.TemporaryDirectory(prefix="qiqc-manifest-acceptance-") as temporary:
        junit = Path(temporary) / "attack-fixtures.xml"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(tests / "test_manifest_attacks.py"),
                str(tests / "test_science_task_manifest.py"),
                "--junitxml",
                str(junit),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        result = {
            "executed": True,
            "exit_code": completed.returncode,
            "framework": "pytest",
            "existing_public_CZ_bundle_bytes_read_for_packaging_regression": True,
            "cloud_calls": 0,
            "passed": False,
        }
        if junit.is_file():
            suites = ET.parse(junit).getroot()
            children = [suites] if suites.tag == "testsuite" else list(suites)
            for key in ("tests", "failures", "errors", "skipped"):
                result[key] = sum(int(suite.attrib.get(key, 0)) for suite in children)
            result["passed"] = (
                completed.returncode == 0
                and result["tests"] > 0
                and not any(result[key] for key in ("failures", "errors", "skipped"))
            )
        else:
            result["failure_code"] = "test_runner_did_not_emit_results"
        report["attack_fixture_acceptance"] = result
    report["offline_acceptance_passed"] = (
        report["all_49_static_manifest_passed"] and result["passed"]
    )
    report["temporary_test_artifacts_cleaned"] = True
    write_report(args.out, report)
    print(
        f"static={report['static_passed']}/{report['task_count']}, attack_tests={result.get('tests', 0)}, offline_passed={report['offline_acceptance_passed']}, real_E2B_final_isolation=false"
    )
    if not report["offline_acceptance_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
