"""Frozen per-task role packaging and runtime metadata, without task execution."""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
import e2b_runtime as runtime
from task_release import load_release

# Retired multi-sandbox topology. The original Compose pipeline has its own
# native contract tests; do not reconstruct a deleted historical release.
try:
    TASKS = sorted(load_release(runtime.ROOT)["tasks"])
except Exception as error:
    from task_manifest import ManifestError
    if not isinstance(error, ManifestError):
        raise
    pytest.skip("Archived role-split release is absent; native Compose tests cover the active topology", allow_module_level=True)


@pytest.mark.parametrize("task", TASKS)
def test_all_task_data_boundaries_and_frozen_identity(task):
    release = load_release(runtime.ROOT)
    row = runtime.task_contract(task)
    for role in ("main", "qsim", "verifier"):
        files = runtime._single_task_files(task, role, release["files"])
        names = [path.relative_to(runtime.RUNTIME).as_posix() for path in files]
        assert names
        assert set(names) <= release["files"].keys()
        for name in names:
            if name.startswith("configs/tasks/"):
                assert name == f"configs/tasks/{task}.yaml"
            if name.startswith("configs/task_materials/"):
                assert name.startswith(f"configs/task_materials/{task}/")
            if name.startswith("harbor_tasks/"):
                assert name.startswith(f"harbor_tasks/{task}/")
            if name.startswith("qiqcbench/qsim/hidden_dynamics/"):
                tail = name.split("hidden_dynamics/", 1)[1]
                assert "/" not in tail or tail.startswith(runtime.SCIENTIFIC_CODE_FAMILY.get(task, task) + "/")
                # Explicit imports may require task-owned reference calculations.
                assert not name.endswith("/validate.py")
        data = {item["path"] for item in row["roles"][role]}
        assert data <= set(names)
        if role == "main":
            assert not any(name.startswith("qiqcbench/") for name in names)
            assert all("/public/" in name or name.endswith("/instruction.md") for name in names)
        if role == "qsim":
            assert not any("/tests/" in name or name.endswith("/scorer.example.yaml") for name in names)
    for role in ("qsim", "verifier"):
        env = runtime.runtime_env(task, role)
        assert env["QIQCBENCH_TASK_ID"] == task
        assert env["QSIM_DEVICE_ID"] == row["device_id"]
        assert env["QSIM_BACKEND_MODE"] == "simulator"
        assert env["QSIM_SNAPSHOT_HIDDEN_TRUTH"] == "0"
        assert not any("${" in value for value in env.values())


def test_main_archive_contains_only_authorized_public_materials():
    task = "time_budgeted_lindbladlearn_6q"
    with tarfile.open(fileobj=io.BytesIO(runtime.archive_runtime(task, role="main"))) as tf:
        manifest = json.load(tf.extractfile("runtime/deployment-manifest.json"))
        assert manifest["role"] == "main"
        assert manifest["task_id"] == task
        assert manifest["benchmark_release"]["task_execution_sha256"]
        assert len(tf.getmembers()) == len(manifest["files"]) + 1
    targets = dict(runtime.main_material_files(task))
    assert targets[f"harbor_tasks/{task}/instruction.md"] == "/task.md"
    assert all(target.startswith("/task_materials/") or target == "/task.md" for target in targets.values())


def test_generated_private_instance_has_no_cross_task_data():
    env = runtime.runtime_env("neutral_atom_loss_repair_control")
    assert env["QIQCBENCH_HIDDEN_PATH"] == "/qsim_logs/private/runtime_hidden.yaml"


def test_lindblad_verifier_uses_independent_supplied_copy():
    env = runtime.runtime_env("time_budgeted_lindbladlearn_6q", "verifier")
    assert env["QIQCBENCH_HIDDEN_DEVICE_PATH"].startswith("/opt/supplied-configs/")
    assert env["QIQCBENCH_LINDBLAD_EXPECTED_CONFIGS"] == "/opt/runtime/configs"


def test_trusted_scorer_explicit_reference_dependency_preserved():
    release = load_release(runtime.ROOT)
    task = "qccd_qaoa_shuttle_compilation"
    paths = runtime._single_task_files(task, "verifier", release["files"])
    assert runtime.RUNTIME / f"qiqcbench/qsim/hidden_dynamics/{task}/reference.py" in paths


def test_task_owned_circuit_source_is_preserved():
    release = load_release(runtime.ROOT)
    task = "clifford_t_tcount_optimization"
    paths = runtime._single_task_files(task, "verifier", release["files"])
    assert runtime.RUNTIME / f"qiqcbench/qsim/hidden_dynamics/{task}/sources/hwb10.qc" in paths


def test_noguide_shares_scientific_code_without_other_variant_data():
    release = load_release(runtime.ROOT)
    task = "femoco_mps_qpe_resources_noguide"
    for role in ("qsim", "verifier"):
        names = {p.relative_to(runtime.RUNTIME).as_posix() for p in runtime._single_task_files(task, role, release["files"])}
        assert "qiqcbench/qsim/hidden_dynamics/femoco_mps_qpe_resources/scorer.py" in names
        assert not any(name.startswith("configs/task_materials/femoco_mps_qpe_resources/") for name in names)
        assert "configs/tasks/femoco_mps_qpe_resources.yaml" not in names
        assert not any(name.startswith("harbor_tasks/femoco_mps_qpe_resources/") for name in names)


@pytest.mark.parametrize("role", ["main", "qsim", "verifier"])
def test_controller_root_preparation_is_confined_to_scientific_roles(role):
    from types import SimpleNamespace
    commands = []
    writes = []
    sb = SimpleNamespace(files=SimpleNamespace(write=lambda *args, **kwargs: writes.append(args[0])), commands=SimpleNamespace(run=lambda command, **kwargs: commands.append(command)))
    runtime.provision(sb, b"nonempty-fixture-archive", task_id=runtime.PUBLIC_TASK, role=role)
    assert (runtime.SCIENCE_PYTHON + " /root/build_controller_root.py" in commands) == (role != "main")
    assert ("/root/build_controller_root.py" in writes) == (role != "main")


@pytest.mark.parametrize("task", ["femoco_mps_qpe_resources", "femoco_mps_qpe_resources_noguide"])
def test_femoco_verifier_gets_separate_runner_with_only_scientific_dependencies(task):
    from types import SimpleNamespace
    commands, writes = [], {}
    sb = SimpleNamespace(files=SimpleNamespace(write=lambda path, value, **kwargs: writes.update({path:value})), commands=SimpleNamespace(run=lambda command, **kwargs: commands.append(command)))
    runtime.provision(sb, b"fixture-archive", task_id=task, role="verifier")
    assert runtime.SCIENCE_PYTHON + " /root/build_femoco_root.py" in commands
    from build_femoco_root import PACKAGES
    assert PACKAGES == ("numpy==2.4.4", "scipy==1.17.1")
    assert "--no-deps" in writes["/root/build_femoco_root.py"]
    assert 'root = Path("/runner_root")' in writes["/root/build_femoco_root.py"]


def test_hamlearn_verifier_preserves_authoritative_private_path_and_user():
    from types import SimpleNamespace
    commands, writes = [], {}
    sb = SimpleNamespace(files=SimpleNamespace(write=lambda path, data, **kwargs: writes.update({path:data})), commands=SimpleNamespace(run=lambda command, **kwargs: commands.append(command)))
    task = "time_budgeted_hamlearn_10q"
    runtime.provision(sb, b"fixture-archive", task_id=task, role="verifier")
    assert any("--uid 10001" in cmd and "/verifier/private/hidden_scorer.yaml" in cmd and "chmod 0400" in cmd for cmd in commands)
    assert any("--require-hashes --no-build" in cmd for cmd in commands)
    assert writes['/opt/qiqc-bootstrap/requirements.txt'] == (runtime.RUNTIME / 'harbor_tasks/time_budgeted_hamlearn_10q/tests/requirements.txt').read_bytes()
    assert runtime.runtime_env(task, "verifier")["QIQCBENCH_HIDDEN_SCORER_PATH"] == "/verifier/private/hidden_scorer.yaml"


def test_binomial_startup_public_surface_dependencies_are_role_scoped():
    release = load_release(runtime.ROOT)
    task = "binomial_code_qec_advantage"
    required = {"configs/devices/binomial_cavity_v0.notebook.yaml", "configs/qtypes/bosonic_cavity_qec.agent_surface.yaml"}
    for role in ("qsim", "verifier"):
        names = {p.relative_to(runtime.RUNTIME).as_posix() for p in runtime._single_task_files(task, role, release["files"])}
        assert required <= names
        assert "configs/devices/gkp_cavity_v0.notebook.yaml" not in names
        assert "configs/qtypes/neutral_atom_logical_processor.agent_surface.yaml" not in names
    names = {p.relative_to(runtime.RUNTIME).as_posix() for p in runtime._single_task_files(task, "main", release["files"])}
    assert not names.intersection(required)


def test_public_surface_permissions_reject_other_device_and_missing_surface():
    from task_manifest import classify_entry, public_surface_inputs, validate_entries, ManifestError
    task, device = "binomial_code_qec_advantage", "binomial_cavity_v0"
    allowed = public_surface_inputs(device)
    assert all(classify_entry(task, device, "qsim", path) for path in allowed)
    assert not classify_entry(task, device, "main", allowed[0])
    assert not classify_entry(task, device, "qsim", "configs/devices/gkp_cavity_v0.notebook.yaml")
    row = runtime.task_contract(task)
    paths = [item["path"] for item in row["roles"]["qsim"] if item["path"] != allowed[0]]
    with pytest.raises(ManifestError, match="required_input_missing"):
        validate_entries(runtime.RUNTIME, task, device, "qsim", paths)


def test_reviewed_surface_bindings_match_frozen_public_device_types():
    import yaml
    from task_manifest import REVIEWED_DEVICE_PUBLIC_SURFACES, public_surface_inputs
    release = load_release(runtime.ROOT)
    for device, qtype in REVIEWED_DEVICE_PUBLIC_SURFACES.items():
        public = yaml.safe_load((runtime.RUNTIME / f"configs/devices/{device}.public.yaml").read_text())
        assert public["qtype"] == qtype
        assert set(public_surface_inputs(device)) <= release["files"].keys()
