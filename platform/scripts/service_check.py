#!/usr/bin/env python3
"""Check the local systemd boundary without exposing configuration secrets.

The default mode is read-only. ``--restart`` is an explicit local recovery
exercise: it restarts the Demo units and waits for the API health check.
No credentials, environment values, or response bodies are printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx


SERVICES = (
    "qiqc-demo-api",
    "qiqc-demo-worker",
    "qiqc-demo-gateway",
    "qiqc-go-edge",
)
API = "http://127.0.0.1:8765/healthz"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=15)


def _unit(service: str) -> dict:
    active = _run("systemctl", "is-active", service)
    show = _run(
        "systemctl",
        "show",
        service,
        "--property=ActiveState,SubState,Restart,NoNewPrivileges,UMask,User,WorkingDirectory",
    )
    fields = {}
    for line in show.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return {
        "name": service,
        "active": active.returncode == 0 and active.stdout.strip() == "active",
        "properties": {
            "ActiveState": fields.get("ActiveState"),
            "SubState": fields.get("SubState"),
            "Restart": fields.get("Restart"),
            "NoNewPrivileges": fields.get("NoNewPrivileges"),
            "UMask": fields.get("UMask"),
            "User": fields.get("User"),
            "WorkingDirectory": fields.get("WorkingDirectory"),
        },
        "stderr_present": bool(show.stderr.strip()),
    }


def _health() -> dict:
    try:
        response = httpx.get(API, timeout=10, trust_env=False)
        # Do not retain or print the response body. The status is sufficient.
        return {"status_code": response.status_code, "ok": response.status_code == 200}
    except httpx.HTTPError as exc:
        return {"status_code": None, "ok": False, "error_type": type(exc).__name__}


def run(restart: bool = False) -> dict:
    before = [_unit(service) for service in SERVICES]
    restarted = False
    if restart:
        for service in SERVICES:
            result = _run("systemctl", "restart", service)
            if result.returncode != 0:
                raise RuntimeError(f"could not restart {service}")
        restarted = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _health()["ok"] and all(_unit(service)["active"] for service in SERVICES):
                break
            time.sleep(0.5)
    after = [_unit(service) for service in SERVICES]
    health = _health()
    hardening = all(
        item["active"]
        and item["properties"]["Restart"] == "on-failure"
        and item["properties"]["NoNewPrivileges"] == "yes"
        and item["properties"]["UMask"] in {"0077", "63"}
        for item in after
    )
    result = {
        "status": "passed" if health["ok"] and all(x["active"] for x in after) and hardening else "failed",
        "services": after,
        "health": health,
        "hardening": hardening,
        "restart_exercised": restarted,
        "secrets_printed": False,
    }
    if restart:
        result["before"] = before
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart", action="store_true", help="restart only the local demo units")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parents[1] / "test-results" / "service-check.json",
    )
    args = parser.parse_args()
    result = run(args.restart)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
