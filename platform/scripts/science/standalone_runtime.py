#!/usr/bin/env python3
"""Freeze/verify a private runtime independent of the original Git checkout.

Only static file operations are performed. No scientific module is imported.
"""

import argparse
import hashlib
import json
import stat
import subprocess
from pathlib import Path

from task_manifest import ManifestError, secure_read

ROOT = Path(__file__).resolve().parents[4]
EXCLUDED_TASK = "time_budgeted_shadow_surrogate_60q"
GENERATED = {"PROVENANCE.json", "MANIFEST.sha256"}


def inventory(root):
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if "__pycache__" not in p.parts
        and p.suffix != ".pyc"
        and not (p.is_dir() and not p.is_symlink())
        and p.relative_to(root).as_posix() not in GENERATED
    )


def freeze(source, runtime):
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip():
        raise ManifestError("original-checkout-must-be-clean")
    tasks = sorted(p.name for p in (runtime / "harbor_tasks").iterdir() if p.is_dir())
    expected = sorted(
        p.name
        for p in (source / "harbor_tasks").iterdir()
        if p.is_dir() and p.name != EXCLUDED_TASK
    )
    if tasks != expected or len(tasks) != 49:
        raise ManifestError("unexpected-task-catalog")
    mapping = {}
    trees = {"src/qiqcbench": "src/qiqcbench", "qiqcbench": "src/qiqcbench"}
    trees.update(
        {
            name: name
            for name in (
                "configs/devices",
                "configs/qtypes",
                "configs/replay",
                "configs/evaluation",
                "docker/qsim",
                "docker/agent",
            )
        }
    )
    for task in tasks:
        trees[f"harbor_tasks/{task}"] = f"harbor_tasks/{task}"
        if (source / "configs/task_materials" / task).is_dir():
            trees[f"configs/task_materials/{task}"] = f"configs/task_materials/{task}"
        mapping[f"configs/tasks/{task}.yaml"] = f"configs/tasks/{task}.yaml"
    for target, origin in trees.items():
        for path in sorted((source / origin).rglob("*")):
            if "__pycache__" not in path.parts and not (
                path.is_dir() and not path.is_symlink()
            ):
                mapping[str(Path(target) / path.relative_to(source / origin))] = (
                    path.relative_to(source).as_posix()
                )
    mapping.update(
        {
            name: name
            for name in ("pyproject.toml", "uv.lock", "LICENSE", ".dockerignore")
        }
    )
    mapping["UPSTREAM-README.md"] = "README.md"
    for tool in ("build_qsim_images.py", "record_agent_image.py"):
        mapping["tools/" + tool] = "tools/" + tool
    # Preflight the entire copy before mutation. Never overwrite divergent data.
    payloads = {}
    for target, origin in mapping.items():
        data = secure_read(source, origin)
        path = runtime / target
        if (path.exists() or path.is_symlink()) and secure_read(
            runtime, target
        ) != data:
            raise ManifestError("local-source-mismatch: " + target)
        for parent in path.parents:
            if parent == runtime.parent:
                break
            if parent.is_symlink():
                raise ManifestError("linked-destination")
        payloads[target] = data
    for target, data in payloads.items():
        path = runtime / target
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        path.chmod(stat.S_IMODE((source / mapping[target]).stat().st_mode))
    files = {}
    for relative in inventory(runtime):
        files[relative] = {
            "sha256": hashlib.sha256(secure_read(runtime, relative)).hexdigest(),
            "mode": stat.S_IMODE((runtime / relative).stat().st_mode),
            "source_path": mapping.get(relative),
        }
    result = {
        "schema": "qiqc-private-standalone-runtime/v1",
        "source_commit": commit,
        "source_checkout_required_at_runtime": False,
        "task_ids": tasks,
        "excluded_task": EXCLUDED_TASK,
        "files": files,
    }
    (runtime / "PROVENANCE.json").write_text(json.dumps(result, indent=2) + "\n")
    (runtime / "MANIFEST.sha256").write_text(
        "".join(f"{v['sha256']}  {p}\n" for p, v in files.items())
    )
    return verify(runtime)


def verify(runtime):
    manifest = json.loads(secure_read(runtime, "PROVENANCE.json"))
    files = manifest["files"]
    if inventory(runtime) != sorted(files):
        raise ManifestError("runtime-inventory-changed")
    for relative, record in files.items():
        if (
            hashlib.sha256(secure_read(runtime, relative)).hexdigest()
            != record["sha256"]
        ):
            raise ManifestError("runtime-file-changed: " + relative)
        if stat.S_IMODE((runtime / relative).stat().st_mode) != record["mode"]:
            raise ManifestError("runtime-file-mode-changed: " + relative)
    checksums = "".join(f"{v['sha256']}  {p}\n" for p, v in files.items())
    if secure_read(runtime, "MANIFEST.sha256").decode() != checksums:
        raise ManifestError("checksum-index-mismatch")
    return {
        "passed": True,
        "task_count": len(manifest["task_ids"]),
        "verified_files": len(files),
        "source_commit": manifest["source_commit"],
        "source_checkout_required": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / "benchmark-assets/qiqcbench-runtime")
    parser.add_argument(
        "--freeze-from",
        type=Path,
        help="One-time trusted source; omit to verify without the original repository",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = (
        freeze(args.freeze_from, args.runtime)
        if args.freeze_from
        else verify(args.runtime)
    )
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
