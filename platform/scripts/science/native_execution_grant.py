"""Read-only authentication of normal scientific runs against trusted SQL state."""

import hashlib
import json
import sqlite3
import time
import tomllib
from pathlib import Path


def validate_execution_grant(request, config):
    lease = request.get("lease") or {}
    if lease.get("mode") != "scientific" or lease.get("kind") not in (
        "infra",
        "public",
        "full",
        "elo",
    ):
        raise ValueError("scientific_execution_mode_invalid")
    database = Path(config["data"]) / "platform.db"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute(
            """SELECT r.id AS run_id,r.batch_id,r.ordinal,r.public,r.attempt,r.owner,r.lease_until,
          b.team_id,b.version_id,b.kind,b.mode,v.sha256,v.status AS version_status,
          t.catalog_sha256,t.submission_sha256,t.task_index,t.task_id,t.public AS target_public,
          t.release_id,t.agent_budget_seconds,c.document,bc.catalog_sha256 AS batch_catalog
          FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
          JOIN attempts a ON a.run_id=r.id AND a.number=r.attempt
          JOIN run_scientific_targets t ON t.run_id=r.id
          JOIN scientific_task_sets c ON c.sha256=t.catalog_sha256
          JOIN batch_scientific_task_sets bc ON bc.batch_id=b.id
          WHERE r.id=? AND a.id=? AND a.state='running' AND r.state='running'
          AND b.status='testing' """,
            (request["run_id"], lease.get("attempt_id")),
        ).fetchone()
        if (
            row is None
            or row["version_status"] != "approved"
            or row["lease_until"] <= time.time()
        ):
            raise ValueError("scientific_execution_grant_unavailable")
        for key in (
            "run_id",
            "batch_id",
            "ordinal",
            "public",
            "attempt",
            "owner",
            "team_id",
            "version_id",
            "kind",
            "mode",
            "sha256",
        ):
            if row[key] != lease.get(key):
                raise ValueError("scientific_execution_lease_mismatch")
        if (
            row["sha256"] != request["sha256"]
            or row["submission_sha256"] != request["sha256"]
            or row["task_index"] != request.get("science_task_index")
            or row["task_id"] != request.get("science_task_id")
            or row["release_id"] != request.get("science_release_id")
            or row["agent_budget_seconds"] != request.get("agent_budget_seconds")
            or row["catalog_sha256"] != request.get("catalog_sha256")
            or row["batch_catalog"] != row["catalog_sha256"]
            or row["target_public"] != row["public"]
        ):
            raise ValueError("scientific_execution_target_mismatch")
        if (
            hashlib.sha256(row["document"].encode()).hexdigest()
            != row["catalog_sha256"]
        ):
            raise ValueError("scientific_task_set_digest_invalid")
        catalog = json.loads(row["document"])
        audit = json.loads(Path(config["source_audit"]).read_text())
        ids = sorted(audit["competition_task_ids"])
        tasks = catalog.get("tasks")
        if (
            catalog.get("schema")
            not in ("qiqc-scientific-task-set/v1", "qiqc-scientific-task-set/v2")
            or not isinstance(tasks, list)
            or len(tasks) != 49
            or [t.get("task_id") for t in tasks] != ids
            or [t.get("task_index") for t in tasks] != list(range(49))
            or any(type(t.get("public")) is not bool for t in tasks)
            or catalog.get("release_id") != row["release_id"]
            or (
                catalog.get("schema") == "qiqc-scientific-task-set/v1"
                and catalog.get("agent_budget_seconds") != row["agent_budget_seconds"]
            )
        ):
            raise ValueError("scientific_task_set_source_mismatch")
        if catalog.get("schema") == "qiqc-scientific-task-set/v2" and any(t.get("evaluation_role") for t in tasks):
            roles = [t.get("evaluation_role", "") for t in tasks]
            if any(role not in ("binary", "racing", "infrastructure") for role in roles) or {
                role: roles.count(role) for role in ("binary", "racing", "infrastructure")
            } != {"binary": 39, "racing": 7, "infrastructure": 3}:
                raise ValueError("scientific_task_role_partition_invalid")
        task = tasks[row["task_index"]]
        if task["task_id"] != row["task_id"] or task["public"] != bool(row["public"]):
            raise ValueError("scientific_task_visibility_mismatch")
        has_roles = any(t.get("evaluation_role") for t in tasks)
        selected = []
        for task in tasks:
            role = task.get("evaluation_role")
            if row["kind"] == "full":
                if not has_roles or role == "binary":
                    selected.append(task)
            elif row["kind"] == "elo":
                if has_roles and role == "racing":
                    selected.append(task)
            elif row["kind"] == "public":
                if task["public"]:
                    selected.append(task)
            elif row["kind"] == "infra":
                if has_roles:
                    if role == "infrastructure":
                        selected.append(task)
                elif catalog.get("schema") == "qiqc-scientific-task-set/v2":
                    if task["public"]:
                        selected.append(task)
                else:
                    if task["task_index"] == catalog.get("infra_task_index"):
                        selected.append(task)
        task = tasks[row["task_index"]]
        if (
            row["ordinal"] < 0
            or row["ordinal"] >= len(selected)
            or selected[row["ordinal"]] != task
            or (row["kind"] == "public" and len(selected) != 5)
            or (
                row["kind"] == "infra"
                and (
                    len(selected)
                    != (
                        3
                        if has_roles
                        else 5
                        if catalog.get("schema") == "qiqc-scientific-task-set/v2"
                        else 1
                    )
                    or (not has_roles and not task["public"])
                )
            )
            or (row["kind"] == "elo" and len(selected) != 7)
        ):
            raise ValueError("scientific_task_selection_mismatch")
        if catalog.get("schema") == "qiqc-scientific-task-set/v2":
            policyrow = db.execute(
                "SELECT policy_json,policy_sha256 FROM batch_policies WHERE batch_id=?",
                (row["batch_id"],),
            ).fetchone()
            if (
                policyrow is None
                or hashlib.sha256(policyrow[0].encode()).hexdigest() != policyrow[1]
            ):
                raise ValueError("invalid_execution_policy")
            policy = json.loads(policyrow[0])
            formal = policy.get("phase") == "competition"
            root = Path(config["source_root"]).resolve()
            path = root / "harbor_tasks" / row["task_id"] / "task.toml"
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("invalid_original_task_path")
            raw = path.read_bytes()
            original = tomllib.loads(raw.decode())
            limits = task["limits"]
            actual = {
                "agent_seconds": original["agent"]["timeout_sec"],
                "setup_seconds": original["agent"]["setup_timeout_sec"],
                "build_seconds": original["environment"]["build_timeout_sec"],
                "verifier_seconds": original["verifier"]["timeout_sec"],
                "cpus": original["environment"]["cpus"],
                "memory_mb": original["environment"]["memory_mb"],
                "source_sha256": hashlib.sha256(raw).hexdigest(),
            }
            if (
                limits != actual
                or request.get("native_limits") != actual
                or request.get("formal_execution") is not formal
            ):
                raise ValueError("original_task_limits_mismatch")
            if formal:
                model = policy.get("formal_execution") or {}
                if request.get("gateway_url") != model.get(
                    "gateway_url"
                ) or request.get("model_id") != model.get("model"):
                    raise ValueError("formal_model_policy_mismatch")
            budget = (
                limits["agent_seconds"]
                if (row["kind"] != "infra" if policy.get("budget_policy") == "task-native-except-infra/v1" else formal)
                else min(limits["agent_seconds"], catalog["agent_budget_seconds"])
            )
            if budget != row["agent_budget_seconds"]:
                raise ValueError("native_budget_policy_mismatch")
    return {
        "task_id": row["task_id"],
        "public": bool(row["public"]),
        "catalog_sha256": row["catalog_sha256"],
    }


def formal_runtime_config(request, config):
    """Resolve stage-specific model identity only from the authenticated batch."""
    if request.get("formal_execution") is not True:
        return config
    validate_execution_grant(request, config)
    with sqlite3.connect(
        (Path(config["data"]) / "platform.db").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        raw, digest = db.execute(
            "SELECT policy_json,policy_sha256 FROM batch_policies WHERE batch_id=?",
            (request["lease"]["batch_id"],),
        ).fetchone()
    if hashlib.sha256(raw.encode()).hexdigest() != digest:
        raise ValueError("formal_policy_digest_invalid")
    policy = json.loads(raw)["formal_execution"]
    from native_provider_profile import load_profile

    profile, identity = load_profile(policy["profile_path"])
    if (
        identity["sha256"] != policy["profile_sha256"]
        or profile["model"] != policy["model"]
    ):
        raise ValueError("formal_provider_policy_changed")
    result = dict(config)
    result.update(
        gateway_url=policy["gateway_url"],
        model_id=policy["model"],
        diagnostic_model="",
        provider_profile=identity,
        provider_profile_path=policy["profile_path"],
    )
    return result
