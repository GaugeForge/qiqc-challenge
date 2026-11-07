"""Metadata-only compose dependency resolution; no benchmark code execution."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
import manifest_catalog_check as catalog
from task_manifest import ManifestError


@pytest.fixture
def runtime(tmp_path):
    task = "fixture_task"
    device = "fixture_device"
    files = {
        f"harbor_tasks/{task}/instruction.md": "public",
        f"harbor_tasks/{task}/tests/test.sh": "# not executed",
        f"harbor_tasks/{task}/task.toml": '[verifier]\nenvironment_mode="separate"\n',
        f"harbor_tasks/{task}/environment/docker-compose.yaml": """services:
  main: {}
  qsim:
    volumes: ["../../../configs:/app/configs:ro"]
""",
        f"configs/tasks/{task}.yaml": f"task_id: {task}\ndevice_id: {device}\n",
        f"configs/devices/{device}.public.yaml": "public",
        f"configs/devices/{device}.hidden.example.yaml": "DO-NOT-READ-DEVICE-TRUTH",
        f"configs/task_materials/{task}/hidden/scorer.yaml": "DO-NOT-READ-PRIVATE-SCORER",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def service(path):
    return {
        "volumes": ["../../../configs:/app/configs:ro"],
        "environment": ["QIQCBENCH_HIDDEN_PATH=" + path],
    }


def test_private_directory_presence_is_not_missing_qsim_dependency(
    runtime, monkeypatch
):
    original = catalog.secure_read

    def guarded(root, relative):
        assert "/hidden/" not in relative and ".hidden." not in relative
        return original(root, relative)

    monkeypatch.setattr(catalog, "secure_read", guarded)
    record = catalog.task_record(runtime, "fixture_task")
    assert record["private_task_material_inventory"]["file_count"] == 1
    assert (
        record["qsim_declared_hidden_input"]["status"]
        == "default_device_example_in_manifest"
    )
    assert (
        record["qsim_declared_hidden_input"]["missing_from_candidate_manifest"] is False
    )
    assert (
        "decide_which_current_task_private_materials_qsim_requires_without_shipping_scorer"
        not in record["pending"]
    )
    assert record["runtime_dependency_closure_verified"] is False


def test_literal_private_dependency_present_but_not_authorized_is_precise(runtime):
    path = "configs/task_materials/fixture_task/hidden/scorer.yaml"
    result = catalog.declared_hidden_input(
        runtime, "fixture_task", "fixture_device", service("/app/" + path), []
    )
    assert result["status"] == "present_but_not_assigned_to_qsim"
    assert result["relative_path"] == path and result["exists"] is True
    assert result["missing_from_candidate_manifest"] is True


def test_missing_declared_file_differs_from_unassigned_existing_file(runtime):
    result = catalog.declared_hidden_input(
        runtime,
        "fixture_task",
        "fixture_device",
        service("/app/configs/task_materials/fixture_task/hidden/missing.yaml"),
        [],
    )
    assert result["status"] == "declared_source_file_missing"
    assert result["exists"] is False


def test_runtime_generated_path_is_not_claimed_missing_source(runtime):
    result = catalog.declared_hidden_input(
        runtime,
        "fixture_task",
        "fixture_device",
        service("/qsim_logs/private/runtime_hidden.yaml"),
        [],
    )
    assert result["status"] == "runtime_path_requires_generation_contract"
    assert (
        result["exists"] is None and result["missing_from_candidate_manifest"] is None
    )


@pytest.mark.parametrize(
    "path",
    [
        "/app/configs/../secret",
        "/app/configs/task_materials/other/hidden/file",
        "/app/configs/devices/other.hidden.yaml",
        "${UNREVIEWED_PATH}",
        "/etc/passwd",
    ],
)
def test_unknown_cross_task_and_noncanonical_override_fail_closed(runtime, path):
    result = catalog.declared_hidden_input(
        runtime, "fixture_task", "fixture_device", service(path), []
    )
    assert result["status"] == "unresolved_or_outside_current_task"
    assert result["exists"] is None


def test_duplicate_environment_key_is_rejected(runtime):
    qsim = service("/app/configs/devices/fixture_device.hidden.example.yaml")
    qsim["environment"].append("QIQCBENCH_HIDDEN_PATH=/different")
    with pytest.raises(ManifestError, match="duplicate_or_invalid_environment"):
        catalog.declared_hidden_input(
            runtime, "fixture_task", "fixture_device", qsim, []
        )


def test_symlink_private_dependency_is_rejected_not_reported_present(runtime):
    path = runtime / "configs/task_materials/fixture_task/hidden/link.yaml"
    path.symlink_to("scorer.yaml")
    with pytest.raises(ManifestError, match="missing_or_linked_input"):
        catalog.declared_hidden_input(
            runtime,
            "fixture_task",
            "fixture_device",
            service("/app/configs/task_materials/fixture_task/hidden/link.yaml"),
            [],
        )


def test_changed_mount_mapping_does_not_assume_host_path(runtime):
    qsim = service("/app/configs/task_materials/fixture_task/hidden/scorer.yaml")
    qsim["volumes"] = ["../../../other:/app/configs:ro"]
    result = catalog.declared_hidden_input(
        runtime, "fixture_task", "fixture_device", qsim, []
    )
    assert result["status"] == "unresolved_or_outside_current_task"


def test_dangling_parent_symlink_is_unsafe_not_a_missing_material(runtime):
    hidden = runtime / "configs/task_materials/fixture_task/hidden"
    (hidden / "dangling").symlink_to("/does/not/exist", target_is_directory=True)
    with pytest.raises(ManifestError, match="missing_or_linked_input"):
        catalog.declared_hidden_input(
            runtime,
            "fixture_task",
            "fixture_device",
            service(
                "/app/configs/task_materials/fixture_task/hidden/dangling/truth.yaml"
            ),
            [],
        )


def test_environment_mapping_supports_literal_path(runtime):
    qsim = service("/app/configs/devices/fixture_device.hidden.example.yaml")
    qsim["environment"] = {
        "QIQCBENCH_HIDDEN_PATH": "/app/configs/devices/fixture_device.hidden.example.yaml"
    }
    result = catalog.declared_hidden_input(
        runtime,
        "fixture_task",
        "fixture_device",
        qsim,
        ["configs/devices/fixture_device.hidden.example.yaml"],
    )
    assert result["status"] == "declared_input_in_manifest"


REVIEWED = {
    "distributed_nv_weighted_field_metrology": (
        "nv_network_8_v2",
        "nv_network_8_v2.hidden.generated.yaml",
    ),
    "nonmarkovian_ghz_frequency_metrology": (
        "trapped_ion_16q_v0",
        "trapped_ion_16q_v0.hidden.generated.yaml",
    ),
    "structured_control_margin": ("spin_chain_control_v0", "hidden_device.json"),
}


@pytest.mark.parametrize("task", sorted(REVIEWED))
def test_reviewed_private_device_added_but_scorer_stays_out(tmp_path, task):
    device, filename = REVIEWED[task]
    private = f"configs/task_materials/{task}/hidden/{filename}"
    files = {
        f"configs/tasks/{task}.yaml": f"task_id: {task}\ndevice_id: {device}\n",
        f"configs/devices/{device}.public.yaml": "public",
        f"configs/devices/{device}.hidden.example.yaml": "SYNTHETIC-PRIVATE",
        private: "SYNTHETIC-PRIVATE-DEVICE",
        f"configs/task_materials/{task}/hidden/scorer.example.yaml": "SYNTHETIC-PRIVATE-SCORER",
        f"harbor_tasks/{task}/instruction.md": "public",
        f"harbor_tasks/{task}/tests/test.sh": "# never executed",
        f"harbor_tasks/{task}/task.toml": '[verifier]\nenvironment_mode="separate"\n',
        f"harbor_tasks/{task}/environment/docker-compose.yaml": 'services:\n  main: {}\n  qsim:\n    volumes: ["../../../configs:/app/configs:ro"]\n    environment: ["QIQCBENCH_HIDDEN_PATH=/app/'
        + private
        + '"]\n',
    }
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    row = catalog.task_record(tmp_path, task)
    assert row["static_manifest_passed"] is True
    hidden = [
        entry["path"] for entry in row["roles"]["qsim"] if "/hidden/" in entry["path"]
    ]
    assert hidden == [private]
    assert row["qsim_declared_hidden_input"]["status"] == "declared_input_in_manifest"
    assert (
        row["qsim_declared_hidden_input"]["assignment_basis"]
        == "reviewed_exact_task_device_and_compose_path"
    )
    assert row["qsim_declared_hidden_input"]["missing_from_candidate_manifest"] is False
    assert row["private_task_materials_not_assigned_to_qsim"] == 1
    assert row["runtime_dependency_closure_verified"] is False
    assert all("/hidden/" not in entry["path"] for entry in row["roles"]["main"])
    compose = tmp_path / f"harbor_tasks/{task}/environment/docker-compose.yaml"
    compose.write_text(compose.read_text().replace(filename, "scorer.example.yaml"))
    changed = catalog.task_record(tmp_path, task)
    assert all("/hidden/" not in entry["path"] for entry in changed["roles"]["qsim"])
    assert (
        changed["qsim_declared_hidden_input"]["status"]
        == "present_but_not_assigned_to_qsim"
    )


def test_private_device_allowlist_requires_matching_task_and_device():
    from task_manifest import classify_entry

    task = "distributed_nv_weighted_field_metrology"
    path = f"configs/task_materials/{task}/hidden/nv_network_8_v2.hidden.generated.yaml"
    assert classify_entry(task, "nv_network_8_v2", "qsim", path)
    assert not classify_entry(task, "other_device", "qsim", path)
    assert not classify_entry("other_task", "nv_network_8_v2", "qsim", path)
    assert not classify_entry(task, "nv_network_8_v2", "main", path)
