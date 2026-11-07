#!/usr/bin/env python3
"""Real E2B acceptance of CZ role manifests, with no agent/model execution."""

import hashlib
import json
import time

import httpx
from e2b_runtime import PUBLIC_TASK, ROOT, create, provision, runtime_env, start_qsim
from verify_public import validate_archive


def run():
    result = {
        "task_id": PUBLIC_TASK,
        "qsim_health": False,
        "mcp_initialized": False,
        "tool_list_available": False,
        "verifier_exit": None,
        "reward": None,
        "separate_sandboxes": True,
        "model_calls": 0,
        "manifests": {},
    }
    qsim = create(timeout=600)
    try:
        provision(qsim, task_id=PUBLIC_TASK, role="qsim")
        result["manifests"]["qsim"] = json.loads(
            qsim.files.read("/opt/runtime/deployment-manifest.json", user="root")
        )
        start_qsim(qsim, PUBLIC_TASK)
        url = "https://" + qsim.get_host(8123)
        headers = {
            "e2b-traffic-access-token": qsim.traffic_access_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        with httpx.Client(timeout=10, trust_env=False) as client:
            for _ in range(50):
                try:
                    if client.get(url + "/healthz", headers=headers).status_code == 200:
                        result["qsim_health"] = True
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.4)
            if not result["qsim_health"]:
                # SDK output can contain config details; retain only exception class.
                log = qsim.files.read("/tmp/qsim.log", user="root")
                for line in log.splitlines():
                    if line.startswith(("ModuleNotFoundError:", "ImportError:")):
                        result["startup_import_error"] = line
                raise RuntimeError("single-task qsim startup failed")
            response = client.post(
                url + "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "qiqc-manifest-check", "version": "1"},
                    },
                },
            )
            result["mcp_initialized"] = (
                response.status_code == 200 and "serverInfo" in response.text
            )
            if response.headers.get("mcp-session-id"):
                headers["mcp-session-id"] = response.headers["mcp-session-id"]
            response = client.post(
                url + "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            result["tool_list_available"] = (
                response.status_code == 200 and "run_flux_pulse" in response.text
            )
        qsim.commands.run("tar -czf /root/evidence.tar.gz -C /qsim_logs .", user="root")
        evidence = bytes(
            qsim.files.read("/root/evidence.tar.gz", format="bytes", user="root")
        )
        validate_archive(evidence)
        result["evidence_sha256"] = hashlib.sha256(evidence).hexdigest()
    finally:
        qsim.kill()
        result["qsim_destroyed"] = True
        _save(result)
    verifier = create(timeout=600)
    try:
        provision(verifier, task_id=PUBLIC_TASK, role="verifier")
        result["manifests"]["verifier"] = json.loads(
            verifier.files.read("/opt/runtime/deployment-manifest.json", user="root")
        )
        verifier.files.write("/root/evidence.tar.gz", evidence, user="root")
        verifier.commands.run(
            "tar -xzf /root/evidence.tar.gz -C /qsim_logs", user="root"
        )
        completed = verifier.commands.run(
            "bash /opt/runtime/harbor_tasks/" + PUBLIC_TASK + "/tests/test.sh",
            user="root",
            envs=runtime_env(PUBLIC_TASK),
            timeout=180,
        )
        result["verifier_exit"] = completed.exit_code
        result["reward"] = int(
            verifier.files.read("/logs/verifier/reward.txt", user="root").strip()
        )
        result["infrastructure_ok"] = (
            all(
                result[k]
                for k in ("qsim_health", "mcp_initialized", "tool_list_available")
            )
            and completed.exit_code == 0
            and result["reward"] in (0, 1)
        )
    finally:
        verifier.kill()
        result["verifier_destroyed"] = True
        _save(result)
    print(json.dumps({k: v for k, v in result.items() if k != "manifests"}))


def _save(result):
    (
        ROOT / "competition-platform/platform/test-results/science-single-task-e2b.json"
    ).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    run()
