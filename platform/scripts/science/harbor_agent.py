"""Trusted Harbor adapter for pinned native Codex, DSH and Pi on E2B."""

from __future__ import annotations

import asyncio
import json
import sys
import shlex
from typing import Literal

from e2b_runtime import ROOT
from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions

PROBE = ROOT / "benchmark-assets/adapters/science-probe"
sys.path.insert(0, str(PROBE))
from executor import bootstrap_main, execute_main
from model_response_contract import recovery_summary

# Callbacks and scoped credentials remain controller-memory-only. Trial configs
# hold a random opaque lookup handle, never callback code or a provider API key.
REGISTERED_RUNS = {}


class NativeOptions(AgentOptions):
    adapter: Literal["codex", "pi-agent", "deepseek-harness"] = "codex"
    run_handle: str


class OfficialNativeSmoke(BaseAgent):
    options_model = NativeOptions

    @staticmethod
    def name():
        return "qiqc-official-native-smoke"

    def version(self):
        kind = self.options.adapter
        lock = {"codex": "codex", "pi-agent": "pi", "deepseek-harness": "deepseek"}[
            kind
        ]
        package = {
            "codex": "@openai/codex",
            "pi-agent": "@earendil-works/pi-coding-agent",
            "deepseek-harness": "@deepseek-ai/dsh",
        }[kind]
        data = json.loads((PROBE / "locks" / (lock + "-package-lock.json")).read_text())
        return data["packages"]["node_modules/" + package]["version"]

    async def setup(self, environment):
        self.binpath = await asyncio.to_thread(
            bootstrap_main, environment.main, self.options.adapter
        )

    async def run(self, instruction, environment, context):
        run = REGISTERED_RUNS[self.options.run_handle]
        run["redaction_secrets"] = [
            run["model_token"],
            environment.qsim.traffic_access_token,
        ]
        model_audit = {}
        try:
            result = await asyncio.to_thread(
                execute_main,
                environment.main,
                environment.qsim_url,
                environment.qsim.traffic_access_token,
                run["submission_zip"],
                self.options.adapter,
                run["model_call"],
                token=run["model_token"],
                binpath=self.binpath,
                check=run["check"],
                progress=run["progress"],
                task_id=run["task_id"],
                agent_budget_seconds=run["agent_budget_seconds"],
                generic_task=run["generic_task"],
                qsim_sandbox=environment.qsim,
                model_audit=model_audit,
            )
        except Exception:
            events = model_audit.get("model_calls", [])
            model_audit["model_recovery"] = recovery_summary(events) if events else None
            model_audit["model_stop_reason"] = (
                events[-1].get("model_stop_reason") if events else None
            ) or ("unrecovered_model_response_failure" if events and not model_audit["model_recovery"]["all_failures_recovered"]
                  else "execution_failed_before_successful_completion")
            context.metadata = {**model_audit, "benchmark_release": run["benchmark_release"],
                                "submission_sha256": run["submission_sha256"],
                                "smoke_only": True, "leaderboard_eligible": False}
            # Controller-owned evidence survives a failed trial; no raw model body.
            await environment.exec(
                "cp /home/user/trajectory.jsonl /logs/agent/trajectory.jsonl; cp /home/user/adapter.stderr /logs/agent/stderr.txt",
                user="root",
            )
            raise
        await environment.exec(
            "cp /home/user/trajectory.jsonl /logs/agent/trajectory.jsonl && cp /home/user/adapter.stderr /logs/agent/stderr.txt && cp /home/user/adapter.stdout /logs/agent/stdout.txt",
            user="root",
        )
        probe_path = (
            ROOT / "competition-platform/platform/scripts/science/agent_boundary_probe.py"
        )
        await asyncio.to_thread(
            environment.main.files.write,
            "/home/user/agent_boundary_probe.py",
            probe_path.read_text(),
        )
        checked = await environment.exec(
            "python3 /home/user/agent_boundary_probe.py --task " + shlex.quote(run["task_id"]), user="user", timeout_sec=40
        )
        if checked.return_code != 0:
            raise RuntimeError(
                "Boundary probe command failed (exit " + str(checked.return_code) + ")"
            )
        boundary = json.loads(checked.stdout)
        valid = (
            boundary["private_paths_absent"]
            and boundary["master_credentials_absent"]
            and boundary["qsim_remote_capability_absent"]
            and result["qsim_transport"] == "controller-file-relay"
            and result["main_network"] == "all-egress-disabled"
            and len(result["qsim_relay_calls"]) >= (3 if run["generic_task"] else 4)
            and (run["generic_task"] or all(event["success"] for event in result["qsim_relay_calls"]))
        )
        for destination in ("public_internet", "e2b_management"):
            valid = valid and boundary[destination].get("status") is None
        if not valid:
            raise RuntimeError("Agent boundary smoke failed")
        result["boundary_probe"] = boundary
        context.metadata = {
            **result,
            "benchmark_release": run["benchmark_release"],
            "submission_sha256": run["submission_sha256"],
            "smoke_only": True,
            "leaderboard_eligible": False,
        }
