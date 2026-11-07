#!/usr/bin/env python3
"""Pinned task releases. Static hashing only; never execute Benchmark code."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from standalone_runtime import inventory, verify
from task_manifest import ManifestError, secure_read

ROOT = Path(__file__).resolve().parents[4]
PIN = Path(__file__).with_name("task-release-pin.json")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def release_document(runtime, release_id):
    verify(runtime)
    provenance_bytes = secure_read(runtime, "PROVENANCE.json")
    provenance = json.loads(provenance_bytes)
    tasks = provenance["task_ids"]
    if len(tasks) != 49 or len(set(tasks)) != 49:
        raise ManifestError("release-requires-49-distinct-tasks")
    files = {
        name: {"sha256": row["sha256"], "mode": row["mode"]}
        for name, row in provenance["files"].items()
    }
    assigned = set()
    task_files = {}
    for task in tasks:
        prefixes = (
            f"harbor_tasks/{task}/",
            f"configs/task_materials/{task}/",
            f"configs/replay/{task}/",
        )
        own = {
            name: row
            for name, row in files.items()
            if name.startswith(prefixes) or name == f"configs/tasks/{task}.yaml"
        }
        if (
            not {
                f"harbor_tasks/{task}/task.toml",
                f"harbor_tasks/{task}/tests/test.sh",
                f"configs/tasks/{task}.yaml",
            }
            <= own.keys()
        ):
            raise ManifestError("incomplete-task-release")
        task_files[task] = own
        assigned.update(own)
    shared = {name: row for name, row in files.items() if name not in assigned}
    shared_sha = digest(shared)
    rows = {}
    for task, own in task_files.items():
        bundle_sha = digest(own)
        rows[task] = {
            "files": own,
            "bundle_sha256": bundle_sha,
            "execution_sha256": digest(
                {
                    "task_id": task,
                    "bundle_sha256": bundle_sha,
                    "shared_sha256": shared_sha,
                }
            ),
        }
    return {
        "schema": "qiqc-task-release/v1",
        "release_id": release_id,
        "source_commit": provenance["source_commit"],
        "provenance_sha256": hashlib.sha256(provenance_bytes).hexdigest(),
        "checksum_index_sha256": hashlib.sha256(
            secure_read(runtime, "MANIFEST.sha256")
        ).hexdigest(),
        "runtime_sha256": digest(files),
        "shared_sha256": shared_sha,
        "shared_files": shared,
        "tasks": rows,
        "files": files,
        "task_count": len(tasks),
        "file_count": len(files),
        "scope": "frozen-source-and-data-not-model-image-or-final-isolation-certification",
    }


def create_release(project_root, release_id, pin_path=PIN):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", release_id):
        raise ManifestError("invalid-release-id")
    if pin_path.exists():
        raise ManifestError("active-release-pin-already-exists")
    relative = f"benchmark-assets/benchmark-releases/{release_id}.json"
    path = project_root / relative
    if path.exists():
        raise ManifestError("release-id-already-exists")
    record = release_document(project_root / "benchmark-assets/qiqcbench-runtime", release_id)
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(data)
    path.chmod(0o444)
    with pin_path.open("x") as target:
        json.dump(
            {
                "release_id": release_id,
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            target,
            indent=2,
        )
        target.write("\n")
    return record


def load_release(project_root, pin_path=PIN):
    pin = json.loads(secure_read(pin_path.parent, pin_path.name))
    data = secure_read(project_root, pin["path"])
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ManifestError("release-pin-mismatch")
    record = json.loads(data)
    if record["release_id"] != pin["release_id"]:
        raise ManifestError("release-identity-mismatch")
    return record


def verified_bytes(runtime, relative, record):
    data = secure_read(runtime, relative)
    expected = record["files"].get(relative)
    if expected is None or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ManifestError("frozen-file-mismatch: " + relative)
    return data


def verify_release(project_root, runtime=None, pin_path=PIN):
    runtime = runtime or project_root / "benchmark-assets/qiqcbench-runtime"
    record = load_release(project_root, pin_path)
    if inventory(runtime) != sorted(record["files"]):
        raise ManifestError("frozen-inventory-mismatch")
    for name in record["files"]:
        verified_bytes(runtime, name, record)
    for name, field in (
        ("PROVENANCE.json", "provenance_sha256"),
        ("MANIFEST.sha256", "checksum_index_sha256"),
    ):
        if hashlib.sha256(secure_read(runtime, name)).hexdigest() != record[field]:
            raise ManifestError("frozen-index-mismatch")
    # Local access modes can be restricted to 0640/0750 during deployment.
    # Archive modes are derived from the pinned original manifest, never from
    # mutable host modes. standalone_runtime.verify checks original copy modes.
    return record


def identity(record, task=None):
    result = {
        "release_id": record["release_id"],
        "runtime_sha256": record["runtime_sha256"],
    }
    if task is not None:
        result["task_execution_sha256"] = record["tasks"][task]["execution_sha256"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument(
        "--create",
        help="One-time initial release; refuses an existing pin or release ID",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    record = (
        create_release(args.project_root, args.create)
        if args.create
        else verify_release(args.project_root)
    )
    result = {
        "passed": True,
        **identity(record),
        "task_count": record["task_count"],
        "file_count": record["file_count"],
    }
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
