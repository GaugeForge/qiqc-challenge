#!/usr/bin/env python3
"""Fresh trusted qsim state between adapters; call after verifier collection."""

import json
import time
from pathlib import Path

import httpx
from e2b_runtime import Sandbox, load_key, start_qsim


def reset():
    state = json.loads(Path("/tmp/qiqc-science-public-session.json").read_text())
    sb = Sandbox.connect(state["sandbox_id"], api_key=load_key())
    sb.commands.run(
        "pkill -f '^python -c from qiqcbench.qsim.server' || true", user="root"
    )
    sb.commands.run("rm -rf /qsim_logs && mkdir -p /qsim_logs", user="root")
    handle = start_qsim(sb, "tunable_coupler_cz_netzero")
    for _ in range(40):
        try:
            r = httpx.get(
                state["qsim_url"] + "/healthz",
                headers={"e2b-traffic-access-token": state["traffic_access_token"]},
                timeout=3,
            )
            if r.status_code == 200:
                return {"fresh_qsim": True, "pid": handle.pid}
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("qsim reset failed")


if __name__ == "__main__":
    print(json.dumps(reset()))
