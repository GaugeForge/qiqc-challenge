#!/usr/bin/env python3
"""Compile native per-task limits from reviewed TOML; no task code is run."""

import argparse
import hashlib
import json
from pathlib import Path
import tomllib


def upgrade_catalog(catalog, source):
    result = json.loads(json.dumps(catalog))
    if (
        len(result["tasks"]) != 49
        or sum(t["public"] is True for t in result["tasks"]) != 5
    ):
        raise ValueError("Exactly 49 tasks and five public tasks required")
    roles = [task.get("evaluation_role", "") for task in result["tasks"]]
    # Historical catalogs did not carry role metadata. Keep them readable and
    # compile their original limits with the old five-public-task semantics.
    # A role-bearing catalog must opt into the complete partition; partial
    # metadata is ambiguous and rejected instead of being guessed.
    if not any(roles):
        if result.get("schema") not in ("qiqc-scientific-task-set/v1", "qiqc-scientific-task-set/v2"):
            raise ValueError("invalid scientific task catalog schema")
    else:
        if any(role not in ("binary", "racing", "infrastructure") for role in roles):
            raise ValueError("Every task must have a valid evaluation role")
        if "" in roles or {role: roles.count(role) for role in ("binary", "racing", "infrastructure")} != {
            "binary": 39,
            "racing": 7,
            "infrastructure": 3,
        }:
            raise ValueError("Task roles must contain exactly 39 binary, 7 racing and 3 infrastructure tasks")
    source = Path(source).resolve()
    for task in result["tasks"]:
        task_id = task["task_id"]
        if not task_id or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in task_id
        ):
            raise ValueError("Invalid task ID")
        path = source / "harbor_tasks" / task_id / "task.toml"
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("Untrusted task configuration path")
        raw = path.read_bytes()
        cfg = tomllib.loads(raw.decode())
        limits = {
            "agent_seconds": cfg["agent"]["timeout_sec"],
            "setup_seconds": cfg["agent"]["setup_timeout_sec"],
            "build_seconds": cfg["environment"]["build_timeout_sec"],
            "verifier_seconds": cfg["verifier"]["timeout_sec"],
            "cpus": cfg["environment"]["cpus"],
            "memory_mb": cfg["environment"]["memory_mb"],
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        }
        if any(
            type(v) is not int or v <= 0
            for k, v in limits.items()
            if k != "source_sha256"
        ):
            raise ValueError("Explicit positive integer limits required")
        task["limits"] = limits
    result["schema"] = "qiqc-scientific-task-set/v2"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    result = upgrade_catalog(json.loads(a.catalog.read_text()), a.source)
    with a.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
