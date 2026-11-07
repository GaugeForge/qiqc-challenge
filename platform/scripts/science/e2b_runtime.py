"""E2B-only science smoke helpers; never imports or runs benchmark code on the host."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tomllib
import tarfile
from pathlib import Path

from e2b import Sandbox
from task_manifest import ManifestError, file_metadata, secure_read
from manifest_catalog_check import task_record, UniqueSafeLoader
import yaml
from task_release import identity, verified_bytes, verify_release
from runtime_contract import install as install_runtime_contract, PYTHON as SCIENCE_PYTHON

ROOT = Path(
    os.environ.get("QIQC_PROJECT_ROOT", str(Path(__file__).resolve().parents[4]))
).resolve()
RUNTIME = ROOT / "benchmark-assets/qiqcbench-runtime"


def load_key():
    key_name = os.environ.get("QIQC_E2B_KEY_NAME", "E2B_API_KEY")
    direct = os.environ.get(key_name)
    if direct:
        return direct
    path = Path(os.environ.get("QIQC_E2B_KEY_FILE", str(ROOT / ".env")))
    text = path.read_text().strip()
    if "=" not in text and "\n" not in text:
        return text
    for line in text.splitlines():
        if line.strip().startswith(key_name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("E2B key missing")


def create(timeout=1800):
    return Sandbox.create(
        api_key=load_key(),
        timeout=timeout,
        network={"allow_public_traffic": False},
        metadata={"purpose": "qiqc-science-topology-smoke"},
    )


PUBLIC_TASK = "tunable_coupler_cz_netzero"
PUBLIC_DEVICE = "tunable_coupler_cz_v0"
# These two benchmark variants deliberately share one scientific implementation.
# The alias permits code only; every config/public/private data path still uses
# the actual task identity validated by task_contract.
SCIENTIFIC_CODE_FAMILY = {
    "femoco_mps_qpe_resources_noguide": "femoco_mps_qpe_resources",
}


def task_contract(task_id):
    """Validate the frozen task identity and role data policy without execution."""
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", task_id):
        raise ValueError("Invalid task identity")
    row = task_record(RUNTIME, task_id)
    if not row["static_manifest_passed"]:
        raise ManifestError("task_role_contract_invalid")
    dependency = row["qsim_declared_hidden_input"]
    if dependency["status"] not in {
        "default_device_example_in_manifest", "declared_input_in_manifest",
        "runtime_path_requires_generation_contract",
    }:
        raise ManifestError("task_private_dependency_unresolved")
    return row


def main_material_files(task_id):
    row = task_contract(task_id)
    prefix = f"configs/task_materials/{task_id}/public/"
    return [
        (item["path"], "/task.md" if item["path"].endswith("/instruction.md")
         else "/task_materials/" + item["path"][len(prefix):])
        for item in row["roles"]["main"]
    ]


def _single_task_files(task_id, role, frozen_files):
    if role not in {"main", "qsim", "verifier"}:
        raise ValueError("Unsupported task role")
    row = task_contract(task_id)
    code_family = SCIENTIFIC_CODE_FAMILY.get(task_id, task_id)
    names = {item["path"] for item in row["roles"][role]}
    if role != "main":
        hidden_root = "qiqcbench/qsim/hidden_dynamics/"
        for name in sorted(frozen_files):
            if not name.startswith("qiqcbench/"):
                continue
            own_asset = name.startswith(hidden_root + code_family + "/") and Path(name).suffix in {".qc", ".txt"}
            if not name.endswith(".py") and not own_asset:
                continue
            relative = Path(name)
            if any(part in relative.parts for part in ("__pycache__", "evolve", "runs")):
                continue
            # No foreign task-specific dynamics or reference solvers. The
            # task's package can import its scorer from __init__; this code is
            # confined to trusted qsim/verifier, never sent into main.
            if name.startswith(hidden_root):
                tail = name[len(hidden_root):]
                if "/" in tail and not tail.startswith(code_family + "/"):
                    continue
                if relative.name in {"reference_solver.py", "reference.py", "validate.py"}:
                    continue
                if task_id == PUBLIC_TASK and role == "qsim":
                    continue
            if role == "qsim" and task_id == PUBLIC_TASK and "eval" in relative.parts:
                continue
            names.add(name)
    # Some trusted scorers import their own reference calculations. Preserve
    # explicit dependencies, without shipping unrelated reference programs.
    own_prefix = f"qiqcbench/qsim/hidden_dynamics/{code_family}/"
    pending = [name for name in names if name.startswith(own_prefix) and name.endswith(".py")]
    inspected = set()
    while pending:
        name = pending.pop()
        if name in inspected:
            continue
        inspected.add(name)
        source = secure_read(RUNTIME, name)
        if hashlib.sha256(source).hexdigest() != frozen_files[name]["sha256"]:
            raise ManifestError("frozen-code-dependency-mismatch")
        package = name[:-3].replace("/", ".").rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(source)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1]) + ("." + base if base else "")
                modules = [base, *[base + "." + alias.name for alias in node.names]]
            for module in modules:
                for dependency in (module.replace(".", "/") + ".py", module.replace(".", "/") + "/__init__.py"):
                    if dependency.startswith(own_prefix) and dependency in frozen_files and dependency not in names:
                        names.add(dependency)
                        pending.append(dependency)
    for name in names:
        if name not in frozen_files:
            raise ManifestError("unfrozen_role_input")
        file_metadata(RUNTIME, name)
    return [RUNTIME / name for name in sorted(names)]


def archive_runtime(task_id=None, *, role="catalog"):
    if task_id is not None and role not in {"main", "qsim", "verifier"}:
        raise ValueError("Unsupported task role")
    if task_id is None and role != "catalog":
        raise ValueError("Full archive is restricted to explicit catalog mode")
    release = verify_release(ROOT, RUNTIME)
    if task_id is not None:
        files = _single_task_files(task_id, role, release["files"])
        manifest = {
            "task_id": task_id,
            "role": role,
            "files": [],
            "benchmark_release": identity(release, task_id),
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path in files:
                relative = path.relative_to(RUNTIME).as_posix()
                content = verified_bytes(RUNTIME, relative, release)
                manifest["files"].append(
                    {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}
                )
                info = tarfile.TarInfo("runtime/" + relative)
                info.size = len(content)
                info.mode = release["files"][relative]["mode"]
                archive.addfile(info, io.BytesIO(content))
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("runtime/deployment-manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()
    if role != "catalog":
        raise ValueError("Full archive is restricted to explicit catalog mode")
    # Fail during preparation, before spending a sandbox or misclassifying an
    # omitted scoring dependency as seven scientific failures.
    for required in (
        "qiqcbench/eval/score_report.py",
        "qiqcbench/eval/verifier/legacy_elo.py",
    ):
        file_metadata(RUNTIME, required)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for relative in sorted(release["files"]):
            if relative.split("/", 1)[0] in {"qiqcbench", "configs", "harbor_tasks"}:
                content = verified_bytes(RUNTIME, relative, release)
                info = tarfile.TarInfo("runtime/" + relative)
                info.size = len(content)
                info.mode = release["files"][relative]["mode"]
                tf.addfile(info, io.BytesIO(content))
        metadata = json.dumps(identity(release), sort_keys=True).encode()
        info = tarfile.TarInfo("runtime/benchmark-release.json")
        info.size = len(metadata)
        tf.addfile(info, io.BytesIO(metadata))
        # eval now comes from the same original Benchmark as the other runtime
        # files. Never overlay a different checkout/version during packaging.
    return buf.getvalue()


def provision(sb, archive=None, *, task_id=None, role="catalog"):
    import shlex

    sb.files.write(
        "/tmp/qiqc-runtime.tar.gz", archive or archive_runtime(task_id, role=role)
    )
    sb.commands.run(
        "mkdir -p /opt/ /app /submission_artifacts /submission /task_materials "
        "/qsim_logs/public_job_results /logs/verifier /logs/artifacts && "
        "tar -xzf /tmp/qiqc-runtime.tar.gz -C /opt/ && "
        "ln -sfn /opt/runtime/configs /opt/configs && "
        "ln -sfn /opt/runtime/configs /app/configs && "
        "ln -sfn /opt/runtime/configs /app/qiqcbench_configs",
        user="root", timeout=60,
    )
    install_runtime_contract(sb, ROOT, RUNTIME, role=role, task_id=task_id)
    if role == "main":
        sb.commands.run("chown user:user /submission && chmod 0700 /submission && chmod 0755 /qsim_logs /qsim_logs/public_job_results /task_materials", user="root", timeout=30)
        for relative, target in main_material_files(task_id):
            sb.commands.run(
                "mkdir -p " + shlex.quote(str(Path(target).parent)) +
                " && cp " + shlex.quote("/opt/runtime/" + relative) + " " + shlex.quote(target),
                user="root", timeout=30,
            )
        return
    if role in {"qsim", "verifier"}:
        # Original qsim/verifier images provide this restricted Python runtime for
        # submitted controllers. The trusted preparer runs only inside E2B.
        sb.files.write("/root/build_controller_root.py", Path(__file__).with_name("build_controller_root.py").read_text(), user="root")
        sb.commands.run(SCIENCE_PYTHON + " /root/build_controller_root.py", user="root", timeout=120)
        if role == "verifier" and task_id == "time_budgeted_lindbladlearn_6q":
            sb.commands.run("cp -a /opt/runtime/configs /opt/supplied-configs", user="root", timeout=30)
        if role == "verifier" and task_id in {"femoco_mps_qpe_resources", "femoco_mps_qpe_resources_noguide"}:
            sb.files.write("/root/build_femoco_root.py", Path(__file__).with_name("build_femoco_root.py").read_text(), user="root")
            sb.commands.run(SCIENCE_PYTHON + " /root/build_femoco_root.py", user="root", timeout=180)
        if role == "verifier" and task_id == "time_budgeted_hamlearn_10q":
            tests = "/opt/runtime/harbor_tasks/time_budgeted_hamlearn_10q/tests"
            sb.commands.run("useradd --system --uid 10001 --no-create-home verifier && mkdir -p /verifier/private /tests && cp " + tests + "/private/hidden_scorer.yaml /verifier/private/hidden_scorer.yaml && chmod 0400 /verifier/private/hidden_scorer.yaml && chown -R verifier:verifier /verifier /tests /qsim_logs /task_materials /logs", user="root", timeout=30)


def runtime_env(task, role="qsim"):
    row = task_contract(task)
    release = verify_release(ROOT, RUNTIME)
    compose = yaml.load(verified_bytes(RUNTIME, f"harbor_tasks/{task}/environment/docker-compose.yaml", release), Loader=UniqueSafeLoader)
    values = compose["services"]["qsim"].get("environment", [])
    if isinstance(values, dict):
        values = [str(key) + "=" + str(value) for key, value in values.items()]
    env = {}
    for entry in values:
        key, _, value = entry.partition("=")
        if not key.startswith(("QSIM_", "QIQCBENCH_")):
            continue
        value = re.sub(r"\$\{[^}:]+:-([^}]*)\}", lambda m: m.group(1), value)
        if "$" in value:
            raise ManifestError("unsupported_task_environment_interpolation")
        env[key] = value.replace("/app/qiqcbench_configs", "/opt/runtime/configs").replace("/app/configs", "/opt/runtime/configs")
    if role == "verifier":
        config = tomllib.loads(verified_bytes(RUNTIME, f"harbor_tasks/{task}/task.toml", release).decode())
        for key, value in config.get("verifier", {}).get("env", {}).items():
            if key.startswith("QIQCBENCH_") and isinstance(value, str):
                env[key] = value.replace("/app/configs", "/opt/runtime/configs")
        env.update(QIQCBENCH_QSIM_LOGS_DIR="/qsim_logs", QIQCBENCH_VERIFIER_ARTIFACTS_DIR="/logs/artifacts", QIQCBENCH_VERIFIER_DIR="/logs/verifier", QIQCBENCH_SUBMISSION_DIR="/submission_artifacts")
        if task == "time_budgeted_lindbladlearn_6q":
            env.update(QIQCBENCH_HIDDEN_DEVICE_PATH="/opt/supplied-configs/devices/" + row["device_id"] + ".hidden.example.yaml", QIQCBENCH_PUBLIC_DEVICE_PATH="/opt/supplied-configs/devices/" + row["device_id"] + ".public.yaml", QIQCBENCH_TASK_PUBLIC_DIR=f"/opt/supplied-configs/task_materials/{task}/public", QIQCBENCH_LINDBLAD_EXPECTED_CONFIGS="/opt/runtime/configs")
    env.update(PATH="/opt/qiqc-python/bin:/usr/local/bin:/usr/bin:/bin", PYTHONPATH="/opt/runtime", QIQCBENCH_CONFIGS="/opt/runtime/configs", QIQCBENCH_TASK_ID=task, QSIM_DEVICE_ID=row["device_id"], QSIM_SNAPSHOT_HIDDEN_TRUTH="0", QSIM_BACKEND_MODE="simulator", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    return env


def start_qsim(sb, task):
    import shlex
    env = runtime_env(task)
    script = ("import os; from qiqcbench.qsim.hidden_dynamics.runtime_construction import construct_runtime_instance; "
              f"construct_runtime_instance({task!r},os.environ.get('QIQCBENCH_HIDDEN_PATH','')); "
              "from qiqcbench.qsim.server import serve_sidecar; "
              f"serve_sidecar(task_id={task!r},log_dir='/qsim_logs')")
    # CZ has no runtime-generated private apparatus and its minimal qsim
    # package deliberately excludes hidden_dynamics.
    if task == PUBLIC_TASK:
        script = "from qiqcbench.qsim.server import serve_sidecar; " + f"serve_sidecar(task_id={task!r},log_dir='/qsim_logs')"
    command = SCIENCE_PYTHON + " -c " + shlex.quote(script) + " > /tmp/qsim.log 2>&1"
    return sb.commands.run(command, envs=env, background=True, timeout=0, user="root")
