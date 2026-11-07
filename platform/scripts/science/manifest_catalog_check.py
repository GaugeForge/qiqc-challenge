#!/usr/bin/env python3
"""Offline 49-task input-boundary audit: metadata only for private payloads."""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import tomllib
from pathlib import Path

import yaml
from task_manifest import (
    REVIEWED_QSIM_PRIVATE_INPUTS,
    ManifestError,
    canonical_relative,
    file_metadata,
    file_tree,
    identity,
    public_surface_inputs,
    secure_read,
    validate_entries,
    write_report,
)


class UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate compose keys instead of silently replacing a policy."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise ManifestError("duplicate_or_invalid_compose_key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


DEFAULT_ROOT = Path(__file__).resolve().parents[4] / "benchmark-assets/qiqcbench-runtime"
SAFE_NAMED_MOUNTS = {
    "qsim_public_job_results": {"/qsim_logs/public_job_results": True},
    "submission": {"/submission": False, "/submission_artifacts": True},
}


def main_mounts(root, task, main):
    inputs = []
    findings = []
    volumes = main.get("volumes", [])
    if not isinstance(volumes, list) or len(volumes) > 128:
        raise ManifestError("invalid_mount_collection")
    for raw in volumes:
        if not isinstance(raw, str):
            findings.append("unsupported_mount_syntax")
            continue
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            findings.append("invalid_mount_syntax")
            continue
        source, destination = parts[:2]
        readonly = len(parts) == 3 and parts[2] == "ro"
        if source in SAFE_NAMED_MOUNTS:
            expected = SAFE_NAMED_MOUNTS[source].get(destination)
            if expected is None or readonly != expected:
                findings.append("unsafe_named_mount")
            continue
        if not source.startswith(".") or "$" in source:
            findings.append("unreviewed_mount_source")
            continue
        relative = posixpath.normpath(f"harbor_tasks/{task}/environment/{source}")
        allowed = f"configs/task_materials/{task}/public"
        if relative != allowed or destination != "/task_materials" or not readonly:
            findings.append("main_mount_crosses_role_boundary")
            continue
        inputs.extend(file_tree(root, relative))
    return inputs, sorted(set(findings))


def declared_hidden_input(root, task, device, qsim, assigned):
    """Resolve only explicit Compose config inputs; never infer reads from names.

    This is metadata inspection, not whole-program dependency analysis. The
    qsim entrypoint's default is the device example. An explicit hidden path
    overrides that default; source existence and role assignment are separate.
    No hidden payload contents are read, and this function grants no permissions.
    """
    environment = qsim.get("environment", [])
    if isinstance(environment, dict):
        if any(
            not isinstance(key, str) or not isinstance(value, (str, int, bool))
            for key, value in environment.items()
        ):
            raise ManifestError("duplicate_or_invalid_environment")
        environment = [key + "=" + str(value) for key, value in environment.items()]
    if not isinstance(environment, list) or len(environment) > 256:
        raise ManifestError("duplicate_or_invalid_environment")
    values = {}
    for item in environment:
        if not isinstance(item, str) or "=" not in item:
            raise ManifestError("duplicate_or_invalid_environment")
        key, value = item.split("=", 1)
        if key in values:
            raise ManifestError("duplicate_or_invalid_environment")
        values[key] = value
    declared = values.get("QIQCBENCH_HIDDEN_PATH", "")
    result = {
        "environment_key": "QIQCBENCH_HIDDEN_PATH",
        "status": "unresolved_or_outside_current_task",
        "exists": None,
        "missing_from_candidate_manifest": None,
        "resolution_scope": "compose-literal-and-file-metadata-only",
        "full_runtime_dependency_closure_verified": False,
    }
    if not declared:
        relative = f"configs/devices/{device}.hidden.example.yaml"
        file_metadata(root, relative)
        result.update(
            status="default_device_example_in_manifest",
            relative_path=relative,
            exists=True,
            missing_from_candidate_manifest=relative not in assigned,
        )
        return result
    if declared == "/qsim_logs/private/runtime_hidden.yaml":
        result["status"] = "runtime_path_requires_generation_contract"
        return result
    if "$" in declared or "\\" in declared:
        return result
    resolved = []
    for volume in qsim.get("volumes", []):
        if not isinstance(volume, str):
            continue
        parts = volume.split(":")
        if len(parts) != 3 or parts[2] != "ro" or not parts[0].startswith("."):
            continue
        source, target, _ = parts
        host = posixpath.normpath(f"harbor_tasks/{task}/environment/{source}")
        if host != "configs" or target not in (
            "/app/configs",
            "/app/qiqcbench_configs",
        ):
            continue
        if declared.startswith(target + "/"):
            resolved.append("configs/" + declared[len(target) + 1 :])
    if len(resolved) != 1:
        return result
    relative = resolved[0]
    try:
        canonical_relative(relative)
    except ManifestError:
        return result
    allowed = (
        relative == f"configs/devices/{device}.hidden.example.yaml"
        or relative.startswith(
            (
                f"configs/task_materials/{task}/hidden/",
                f"configs/task_materials/{task}/runtime/",
            )
        )
    )
    if not allowed:
        return result
    try:
        file_metadata(root, relative)
    except ManifestError:
        # A symlink or other unsafe input is not merely an absent file.
        path = root / relative
        parents = [
            root.joinpath(*Path(relative).parts[:index])
            for index in range(1, len(Path(relative).parts) + 1)
        ]
        if any(parent.is_symlink() for parent in parents) or path.exists():
            raise
        result.update(
            status="declared_source_file_missing",
            relative_path=relative,
            exists=False,
            missing_from_candidate_manifest=True,
        )
        return result
    present = relative in assigned
    result.update(
        status="declared_input_in_manifest"
        if present
        else "present_but_not_assigned_to_qsim",
        relative_path=relative,
        exists=True,
        missing_from_candidate_manifest=not present,
    )
    return result


def task_record(root, task):
    row = {
        "task_id": task,
        "payload_kind": "candidate-task-data-inputs-only",
        "static_manifest_passed": False,
        "runtime_dependency_closure_verified": False,
        "real_e2b_role_isolation_verified_by_this_check": False,
        "content_semantic_classification_verified": False,
        "roles": {},
        "pending": [],
    }
    try:
        device = identity(root, task)
        row["device_id"] = device
        compose_bytes = secure_read(
            root, f"harbor_tasks/{task}/environment/docker-compose.yaml"
        )
        if len(compose_bytes) > 512 * 1024:
            raise ManifestError("oversized_compose")
        compose = yaml.load(compose_bytes, Loader=UniqueSafeLoader)
        services = compose.get("services", {})
        if not isinstance(services, dict) or not {"main", "qsim"}.issubset(services):
            raise ManifestError("missing_service_contract")
        row["compose_sha256"] = hashlib.sha256(compose_bytes).hexdigest()
        public_mount_files, findings = main_mounts(root, task, services["main"])
        row["source_main_mount_findings"] = findings
        if findings:
            row["pending"].append("source_compose_main_mount_review")
        common = [
            f"configs/tasks/{task}.yaml",
            f"configs/devices/{device}.public.yaml",
            f"configs/devices/{device}.hidden.example.yaml",
            *public_surface_inputs(device),
        ]
        public_dir = f"configs/task_materials/{task}/public"
        public_files = (
            file_tree(root, public_dir) if (root / public_dir).exists() else []
        )
        private_dir = f"configs/task_materials/{task}/hidden"
        private_files = (
            file_tree(root, private_dir) if (root / private_dir).exists() else []
        )
        tests = file_tree(root, f"harbor_tasks/{task}/tests")
        role_paths = {
            "main": [f"harbor_tasks/{task}/instruction.md", *public_mount_files],
            "qsim": [*common, *public_files],
            "verifier": [*common, *public_files, *private_files, *tests],
        }
        dependency = declared_hidden_input(
            root, task, device, services["qsim"], role_paths["qsim"]
        )
        reviewed = REVIEWED_QSIM_PRIVATE_INPUTS.get(task)
        if (
            dependency["status"] == "present_but_not_assigned_to_qsim"
            and reviewed
            and reviewed == (device, dependency.get("relative_path"))
        ):
            role_paths["qsim"].append(dependency["relative_path"])
            dependency = declared_hidden_input(
                root, task, device, services["qsim"], role_paths["qsim"]
            )
            dependency["assignment_basis"] = (
                "reviewed_exact_task_device_and_compose_path"
            )
        for role, paths in role_paths.items():
            row["roles"][role] = validate_entries(root, task, device, role, paths)
        row["private_task_materials_not_assigned_to_qsim"] = len(
            set(private_files) - set(role_paths["qsim"])
        )
        row["private_task_material_inventory"] = {
            "file_count": len(private_files),
            "presence_implies_missing_dependency": False,
            "presence_implies_qsim_requires_all_files": False,
            "private_payload_contents_read": False,
        }
        row["qsim_declared_hidden_input"] = dependency
        if dependency["status"] == "present_but_not_assigned_to_qsim":
            row["pending"].append("review_and_assign_explicit_qsim_private_input")
        elif dependency["status"] == "declared_source_file_missing":
            row["pending"].append("supply_declared_qsim_source_input")
        elif dependency["status"] == "runtime_path_requires_generation_contract":
            row["pending"].append(
                "verify_existing_runtime_generated_hidden_input_contract"
            )
        elif dependency["status"] == "unresolved_or_outside_current_task":
            row["pending"].append(
                "resolve_explicit_qsim_hidden_path_without_broadening_access"
            )
        row["pending"].append("verify_role_code_dependencies_and_template_images")
        row["pending"].append("run_real_sandbox_network_and_filesystem_attacks")
        qsim_mounts = services["qsim"].get("volumes", [])
        broad_config_mount = any(
            isinstance(v, str) and "../../../configs:" in v for v in qsim_mounts
        )
        row["source_qsim_mount_contains_full_catalog"] = broad_config_mount
        if broad_config_mount:
            row["pending"].append(
                "replace_broad_source_qsim_config_mount_with_role_manifest"
            )
        config = tomllib.loads(
            secure_read(root, f"harbor_tasks/{task}/task.toml").decode()
        )
        row["source_task_allows_internet"] = (
            config.get("environment", {}).get("allow_internet") is True
        )
        if row["source_task_allows_internet"]:
            row["pending"].append("apply_event_network_restriction_overlay")
        row["source_separate_verifier_declared"] = (
            config.get("verifier", {}).get("environment_mode") == "separate"
        )
        if not row["source_separate_verifier_declared"]:
            row["pending"].append("supply_independent_trusted_verifier")
        row["static_manifest_passed"] = not findings
    except ManifestError as exc:
        row["failure_code"] = exc.code
    except (ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        row["failure_code"] = "invalid_task_metadata"
    return row


def audit_catalog(root):
    tasks = sorted(
        path.name for path in (root / "harbor_tasks").iterdir() if path.is_dir()
    )
    rows = [task_record(root, task) for task in tasks]
    return {
        "schema": "qiqc-task-data-manifest-audit/v2",
        "scope": "offline-static-path-and-role-policy-only",
        "task_count": len(rows),
        "expected_task_count": 49,
        "all_49_static_manifest_passed": len(rows) == 49
        and all(r["static_manifest_passed"] for r in rows),
        "static_passed": sum(r["static_manifest_passed"] for r in rows),
        "private_material_summary": {
            "tasks_with_private_material_files": sum(
                r.get("private_task_material_inventory", {}).get("file_count", 0) > 0
                for r in rows
            ),
            "reviewed_private_device_inputs_assigned": sum(
                r.get("qsim_declared_hidden_input", {}).get("assignment_basis")
                == "reviewed_exact_task_device_and_compose_path"
                for r in rows
            ),
            "explicit_qsim_inputs_present_but_unassigned": sum(
                r.get("qsim_declared_hidden_input", {}).get("status")
                == "present_but_not_assigned_to_qsim"
                for r in rows
            ),
            "explicit_qsim_source_files_missing": sum(
                r.get("qsim_declared_hidden_input", {}).get("status")
                == "declared_source_file_missing"
                for r in rows
            ),
            "runtime_generation_contracts_pending": sum(
                r.get("qsim_declared_hidden_input", {}).get("status")
                == "runtime_path_requires_generation_contract"
                for r in rows
            ),
            "unresolved_explicit_qsim_paths": sum(
                r.get("qsim_declared_hidden_input", {}).get("status")
                == "unresolved_or_outside_current_task"
                for r in rows
            ),
            "dynamic_dependencies_not_inferred_from_directory_presence": True,
        },
        "real_e2b_final_isolation_passed": False,
        "cloud_calls": 0,
        "task_code_executed": False,
        "catalog_audit_hidden_payload_contents_read": False,
        "metadata_identity_headers_and_compose_toml_read": True,
        "private_file_hashes": "not_computed_to_avoid_reading_private_payloads",
        "warnings": [
            "Public classification follows explicit author public mounts; file contents were not semantically audited.",
            "Candidate manifests are data boundaries, not runnable 49-task images or a replacement for scientific dependency closure.",
            "Original source compose may mount full catalogs and permit internet; this audit does not deploy those files.",
        ],
        "tasks": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_ROOT.parents[1]
        / "competition-platform/platform/test-results/science-49-input-manifests.json",
    )
    args = parser.parse_args()
    report = audit_catalog(args.runtime)
    write_report(args.out, report)
    print(
        f"static manifests {report['static_passed']}/{report['task_count']}; real E2B final isolation NOT VERIFIED; cloud calls 0"
    )
    if not report["all_49_static_manifest_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
