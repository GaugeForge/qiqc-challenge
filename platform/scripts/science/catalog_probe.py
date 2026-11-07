"""Runs inside a trusted E2B qsim sandbox only. Emits no task data or truth."""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path("/opt/runtime")
rows = []
for task in sorted((ROOT / "harbor_tasks").iterdir()):
    if not task.is_dir():
        continue
    name = task.name
    logs = Path("/tmp/catalog") / name
    if logs.exists():
        shutil.rmtree(logs)
    logs.mkdir(parents=True, exist_ok=True)
    qsim_mount = Path("/qsim_logs")
    if qsim_mount.is_symlink():
        qsim_mount.unlink()
    elif qsim_mount.exists():
        shutil.rmtree(qsim_mount)
    qsim_mount.symlink_to(logs)
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT),
        QIQCBENCH_CONFIGS=str(ROOT / "configs"),
        QSIM_SNAPSHOT_HIDDEN_TRUTH="0",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    compose = YAML(typ="safe").load(
        (task / "environment/docker-compose.yaml").read_text()
    )
    for entry in compose["services"]["qsim"].get("environment", []):
        key, _, value = entry.partition("=")
        if not key.startswith(("QSIM_", "QIQCBENCH_")):
            continue
        value = re.sub(r"\$\{[^}:]+:-([^}]*)\}", lambda m: m.group(1), value)
        env[key] = value.replace("/app/configs", str(ROOT / "configs"))
    command = [
        "python",
        "-c",
        f"import os; from qiqcbench.qsim.hidden_dynamics.runtime_construction import construct_runtime_instance; construct_runtime_instance({name!r},os.environ.get('QIQCBENCH_HIDDEN_PATH','')); from qiqcbench.qsim.server import serve_sidecar; serve_sidecar(task_id={name!r},log_dir={str(logs)!r})",
    ]
    with (logs / "process.log").open("w") as out:
        proc = subprocess.Popen(command, env=env, stdout=out, stderr=out)
        row = {
            "task_id": name,
            "qsim_health": False,
            "mcp_initialized": False,
            "tool_list_available": False,
            "backend_mode": env.get("QSIM_BACKEND_MODE"),
            "compose_environment_applied": True,
        }
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        "http://localhost:8123/healthz", timeout=2
                    ) as r:
                        row["qsim_health"] = r.status == 200
                    break
                except OSError:
                    time.sleep(0.2)
            if row["qsim_health"]:
                import httpx

                with httpx.Client(timeout=10, trust_env=False) as client:
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    }
                    r = client.post(
                        "http://localhost:8123/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "qiqc-smoke", "version": "1"},
                            },
                        },
                    )
                    row["mcp_initialized"] = (
                        r.status_code == 200 and "serverInfo" in r.text
                    )
                    if r.headers.get("mcp-session-id"):
                        headers["mcp-session-id"] = r.headers["mcp-session-id"]
                    client.post(
                        "http://localhost:8123/mcp",
                        headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                    r = client.post(
                        "http://localhost:8123/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        },
                    )
                    row["tool_list_available"] = (
                        r.status_code == 200 and "list_devices" in r.text
                    )
            else:
                # Only exception type, never hidden values, is included in the public report.
                lines = (logs / "process.log").read_text().splitlines()
                row["failure_type"] = next(
                    (s.split(":")[0] for s in reversed(lines) if "Error:" in s),
                    "startup_timeout",
                )
        except Exception as exc:  # noqa: BLE001 — record failure, never a passing verdict
            row["failure_type"] = type(exc).__name__
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    rows.append(row)
    Path("/tmp/catalog-result.json").write_text(json.dumps({"tasks": rows}, indent=2))
    print(json.dumps(row), flush=True)
