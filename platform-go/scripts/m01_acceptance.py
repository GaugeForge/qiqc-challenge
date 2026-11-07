#!/usr/bin/env python3
"""Reproduce M01 local acceptance; never accesses staging or cloud credentials."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

MODULE = Path(__file__).resolve().parents[1]
REQUIRED = {
    "TestM01TwentyTeamHTTPIsolationAndRestart",
    "TestM01PasswordReuseCannotBypassFirstChange",
    "TestM01AccountAndIPLimitsPersistAndSessionsExpire",
    "TestM01LegacyPythonHashAndSecureCookieContract",
    "TestSessionPasswordRotationPersistsAndRevokesOthers",
    "TestRateLimitPersistsAcrossRestartAndExpires",
    "TestHTTPAuthCSRFBodyLimitAndNoSecretFields",
    "TestAdminResetIsAtomicAuditedAndRevokesSessions",
    "TestAccountIssuanceIsAuthorizedAtomicAndNeverPersistsPlaintext",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=MODULE.parent / "platform/test-results/m01-acceptance.json")
    args = parser.parse_args()
    command = ["go", "test", "-race", "-json", "-count=1", "-timeout=10m",
               "./internal/platform", "-run",
               "^(TestM01|TestSessionPassword|TestRateLimit|TestHTTPAuth|TestAdminReset|TestAccountIssuance)"]
    started = time.monotonic()
    try:
        process = subprocess.run(command, cwd=MODULE, capture_output=True, text=True,
                                 timeout=660, check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        process = subprocess.CompletedProcess(command, 124, output,
                                               "M01 acceptance exceeded 660 seconds\n")
    except OSError as exc:
        process = subprocess.CompletedProcess(command, 127, "",
                                               f"Cannot start local Go test runner: {exc}\n")
    passed: set[str] = set()
    failed: set[str] = set()
    observations: list[str] = []
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        test = event.get("Test")
        if test and event.get("Action") == "pass":
            passed.add(test)
        if test and event.get("Action") == "fail":
            failed.add(test)
        output = event.get("Output", "")
        if "M01 teams=" in output:
            observations.append(output.strip())
    missing = sorted(REQUIRED - passed)
    successful = process.returncode == 0 and not missing and not failed
    report = {
        "schema": "qiqc-m01-local-acceptance/v1",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "passed" if successful else "failed",
        "command": command,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_code": process.returncode,
        "passed_tests": sorted(passed),
        "failed_tests": sorted(failed),
        "missing_required_tests": missing,
        "observations": observations,
        "scope": {
            "real": ["20 concurrent team HTTP sessions and password rotations", "scrypt",
                     "SQLite transactions", "HTTP listener and database close/reopen",
                     "1140 cross-team denied reads/cancellations", "persistent rate limits"],
            "fixtures": ["submission scanner", "scientific results", "local rollout store"],
            "not_tested": ["production email delivery/bounces", "HTTPS certificate/domain",
                           "human administrator identity verification", "whole-host reboot",
                           "cloud capacity", "full Python/Go API differential coverage"],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.output.with_suffix(".jsonl").write_text(process.stdout)
    if process.stderr:
        args.output.with_suffix(".stderr.txt").write_text(process.stderr)
    print(json.dumps({"status": report["status"], "passed_tests": len(passed),
                      "missing_required_tests": missing, "report": str(args.output)},
                     ensure_ascii=False))
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
