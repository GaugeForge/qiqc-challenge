"""Executable offline attack fixtures: payload code is never executed."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
import task_manifest
from manifest_catalog_check import main_mounts
from task_manifest import (
    ManifestError,
    file_metadata,
    identity,
    secure_read,
    validate_entries,
)

TASK = "fixture_task"
DEVICE = "fixture_device"


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    files = {
        f"harbor_tasks/{TASK}/instruction.md": "Public instruction only",
        f"harbor_tasks/{TASK}/tests/test.sh": "# trusted verifier fixture, never executed",
        f"configs/tasks/{TASK}.yaml": f"schema_version: 1\ntask_id: {TASK}\ndevice_id: {DEVICE}\n",
        f"configs/devices/{DEVICE}.public.yaml": "public: true",
        f"configs/devices/{DEVICE}.hidden.example.yaml": "SYNTHETIC-TRUTH-DO-NOT-READ",
        f"configs/task_materials/{TASK}/public/input.json": "{}",
        f"configs/task_materials/{TASK}/hidden/scorer.yaml": "SYNTHETIC-PRIVATE-SCORER",
        "harbor_tasks/other_task/instruction.md": "Other task forbidden",
        "configs/tasks/other_task.yaml": "Other task configuration forbidden",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    return root


def base(role):
    if role == "main":
        return [f"harbor_tasks/{TASK}/instruction.md"]
    if role == "verifier":
        return [f"harbor_tasks/{TASK}/tests/test.sh"]
    return [
        f"configs/tasks/{TASK}.yaml",
        f"configs/devices/{DEVICE}.public.yaml",
        f"configs/devices/{DEVICE}.hidden.example.yaml",
    ]


@pytest.mark.parametrize(
    "role,path",
    [
        ("main", f"configs/devices/{DEVICE}.hidden.example.yaml"),
        ("main", f"harbor_tasks/{TASK}/tests/test.sh"),
        ("main", f"configs/task_materials/{TASK}/hidden/scorer.yaml"),
        ("main", "harbor_tasks/other_task/instruction.md"),
        ("qsim", f"harbor_tasks/{TASK}/tests/test.sh"),
        ("qsim", f"configs/task_materials/{TASK}/hidden/scorer.yaml"),
        ("qsim", "configs/tasks/other_task.yaml"),
        ("verifier", "configs/tasks/other_task.yaml"),
    ],
)
def test_cross_task_truth_and_verifier_injection_rejected(runtime, role, path):
    with pytest.raises(ManifestError, match="role_boundary_violation"):
        validate_entries(runtime, TASK, DEVICE, role, [*base(role), path])


@pytest.mark.parametrize(
    "relative",
    [
        "../secret",
        "/etc/passwd",
        "a/../../secret",
        "a//b",
        "a/./b",
        "C:\\secret",
        "a\x00b",
    ],
)
def test_noncanonical_and_escape_paths_fail_before_open(runtime, relative):
    with pytest.raises(ManifestError):
        file_metadata(runtime, relative)


def test_parent_symlink_cannot_smuggle_truth_into_public(runtime):
    public = runtime / f"configs/task_materials/{TASK}/public"
    (public / "alias").symlink_to(public.parent / "hidden", target_is_directory=True)
    with pytest.raises(ManifestError, match="missing_or_linked_input"):
        validate_entries(
            runtime,
            TASK,
            DEVICE,
            "main",
            [*base("main"), f"configs/task_materials/{TASK}/public/alias/scorer.yaml"],
        )


def test_leaf_symlink_cannot_smuggle_truth_into_public(runtime):
    public = runtime / f"configs/task_materials/{TASK}/public"
    (public / "linked.yaml").symlink_to(public.parent / "hidden/scorer.yaml")
    with pytest.raises(ManifestError):
        validate_entries(
            runtime,
            TASK,
            DEVICE,
            "main",
            [*base("main"), f"configs/task_materials/{TASK}/public/linked.yaml"],
        )


def test_hardlink_alias_rejected(runtime):
    name = f"configs/task_materials/{TASK}/public/hardlinked.yaml"
    os.link(
        runtime / f"configs/task_materials/{TASK}/hidden/scorer.yaml", runtime / name
    )
    with pytest.raises(ManifestError, match="hardlinked_input"):
        validate_entries(runtime, TASK, DEVICE, "main", [*base("main"), name])


def test_fifo_and_missing_file_fail_without_blocking(runtime):
    fifo = f"configs/task_materials/{TASK}/public/input.fifo"
    os.mkfifo(runtime / fifo)
    with pytest.raises(ManifestError, match="nonregular_input"):
        file_metadata(runtime, fifo)
    with pytest.raises(ManifestError, match="missing_or_linked_input"):
        secure_read(runtime, "does-not-exist")


def test_missing_and_duplicate_required_inputs_fail_closed(runtime):
    with pytest.raises(ManifestError, match="required_input_missing"):
        validate_entries(runtime, TASK, DEVICE, "main", [])
    with pytest.raises(ManifestError, match="duplicate_entry"):
        validate_entries(runtime, TASK, DEVICE, "main", base("main") * 2)


def test_input_modified_during_read_rejected(runtime, monkeypatch):
    relative = f"harbor_tasks/{TASK}/instruction.md"
    original = os.read
    first = True

    def replace(fd, count):
        nonlocal first
        if first:
            first = False
            (runtime / relative).write_text("attacker changed inode content")
        return original(fd, count)

    monkeypatch.setattr(task_manifest.os, "read", replace)
    with pytest.raises(ManifestError, match="input_changed_during_read"):
        secure_read(runtime, relative)


def test_identity_mismatch_is_not_guessed(runtime):
    (runtime / f"configs/tasks/{TASK}.yaml").write_text(
        f"task_id: other_task\ndevice_id: {DEVICE}\n"
    )
    with pytest.raises(ManifestError, match="task_identity_mismatch"):
        identity(runtime, TASK)


@pytest.mark.parametrize(
    "mount",
    [
        "../../../configs:/task_materials:ro",
        "../../../configs/task_materials/other_task/public:/task_materials:ro",
        "../../../configs/task_materials/fixture_task/hidden:/task_materials:ro",
        "../../../configs/task_materials/fixture_task/public:/task_materials",
        "qsim_logs:/qsim_logs:ro",
        "qsim_public_job_results:/qsim_logs/public_job_results",
        "/var/run/docker.sock:/var/run/docker.sock:ro",
    ],
)
def test_source_mount_attacks_are_detected_not_deployed(runtime, mount):
    inputs, findings = main_mounts(runtime, TASK, {"volumes": [mount]})
    assert not inputs
    assert findings


def test_original_readonly_public_mount_is_allowed(runtime):
    inputs, findings = main_mounts(
        runtime,
        TASK,
        {
            "volumes": [
                f"../../../configs/task_materials/{TASK}/public:/task_materials:ro"
            ]
        },
    )
    assert inputs == [f"configs/task_materials/{TASK}/public/input.json"]
    assert findings == []


def test_duplicate_compose_policy_cannot_override_restriction():
    import yaml
    from manifest_catalog_check import UniqueSafeLoader

    with pytest.raises(ManifestError, match="duplicate_or_invalid_compose_key"):
        yaml.load(
            "services:\n  main:\n    volumes: []\n    volumes: ['qsim_logs:/qsim_logs:ro']\n",
            Loader=UniqueSafeLoader,
        )


def test_other_device_truth_cannot_be_rebound_to_current_task(runtime):
    with pytest.raises(ManifestError, match="device_binding_mismatch"):
        validate_entries(
            runtime,
            TASK,
            "other_device",
            "qsim",
            [
                f"configs/tasks/{TASK}.yaml",
                "configs/devices/other_device.public.yaml",
                "configs/devices/other_device.hidden.example.yaml",
            ],
        )


def test_symlink_in_configured_root_ancestry_is_rejected(runtime, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ManifestError, match="missing_or_linked_input"):
        file_metadata(alias / "harbor_tasks", f"{TASK}/instruction.md")
