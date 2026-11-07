#!/usr/bin/env python3
"""Freeze upstream task bytes and phase-specific contracts, without execution.

These are task releases, not native runtime acceptance records. All archives are
private organizer inputs. No benchmark code is imported and no cloud API is used.

The paper's 49-task scope is split into 39 binary science gates, 7 racing
tasks reserved for the final Elo pass, and 3 infrastructure-only test cases.
The five public tasks remain a separate participant sample and are members of
the binary set; they are not the infrastructure tests.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import tomllib

from verify_restored_benchmark import verify as verify_source

SCHEMA = "qiqc-frozen-task-batches/v1"
BATCHES = {
    "formal-normal-39": ("competition", "full", "binary", 39, "upstream"),
    "formal-elo-7": ("competition", "elo", "racing", 7, "upstream"),
    "infra-test-3": ("practice", "infra", "infrastructure", 3, "restricted"),
    "formal-public-5": ("competition", "public", "public", 5, "upstream"),
}
LEGACY_BATCHES = {
    "formal-49": ("competition", "full", 49, "upstream"),
    "infra-public-5": ("practice", "infra", 5, "restricted"),
    "formal-public-5": ("competition", "public", 5, "upstream"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or str(p) != name:
        raise ValueError("unsafe archive path")
    return p


def archive_bytes(source, files):
    """Deterministic archives retain original modes, paths and internal symlinks."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, metadata in sorted(files.items()):
                path = source / safe_name(name)
                info = tarfile.TarInfo(name)
                info.mtime = 0
                if metadata["git_mode"] == "120000":
                    data = os.readlink(path).encode()
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode()
                    info.mode = 0o777
                else:
                    if path.is_symlink():
                        raise ValueError("source type changed")
                    data = path.read_bytes()
                    info.mode = 0o755 if metadata["git_mode"] == "100755" else 0o644
                    info.size = len(data)
                if sha(data) != metadata["sha256"] or len(data) != metadata["bytes"]:
                    raise ValueError("source changed during freeze")
                archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return buffer.getvalue()


def check_archive(data, files):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(files) or {m.name for m in members} != set(files):
            raise ValueError("archive file inventory mismatch")
        for member in members:
            safe_name(member.name)
            metadata = files[member.name]
            if metadata["git_mode"] == "120000":
                if not member.issym():
                    raise ValueError("archive symlink mismatch")
                data = member.linkname.encode()
            else:
                if not member.isfile() or member.mode != (0o755 if metadata["git_mode"] == "100755" else 0o644):
                    raise ValueError("archive file mode mismatch")
                data = archive.extractfile(member).read()
            if sha(data) != metadata["sha256"] or len(data) != metadata["bytes"]:
                raise ValueError("archive content mismatch")


def task_row(source, task, files):
    prefix = "harbor_tasks/" + task["task_id"] + "/"
    task_files = {k: v for k, v in files.items() if k.startswith(prefix)}
    config = tomllib.loads((source / prefix / "task.toml").read_text())
    env, agent, verifier = (config[k] for k in ("environment", "agent", "verifier"))
    # Preserve raw declarations, including newer network_mode and phase overrides.
    keys = ("allow_internet", "network_mode", "allowed_hosts")
    network = {k: {n: config[k][n] for n in keys if n in config[k]}
               for k in ("environment", "agent", "verifier")}
    network["verifier_environment"] = {n: verifier.get("environment", {})[n]
                                       for n in keys if n in verifier.get("environment", {})}
    return {"task_id": task["task_id"], "public": task["public"],
            "evaluation_role": task["evaluation_role"],
            "task_path": prefix.rstrip("/"), "files": task_files,
            "task_files_sha256": sha(canonical(task_files)),
            "native_limits": {"agent_seconds": agent["timeout_sec"],
                              "setup_seconds": agent["setup_timeout_sec"],
                              "build_seconds": env["build_timeout_sec"],
                              "verifier_seconds": verifier["timeout_sec"]},
            "upstream_network": network}


def select_batch(index, phase, kind):
    """Use the immutable admission phase, never the current wall clock on retry."""
    # The five public tasks are one immutable binary subset.  Practice and
    # competition use the same task bytes; the platform calendar controls the
    # phase-specific admission and scoring policy, so practice public samples
    # reuse the public subset artifact instead of creating a second archive.
    if (phase, kind) == ("practice", "public"):
        return index["batches"]["formal-public-5"]
    for name, (expected_phase, expected_kind, _, _, _) in BATCHES.items():
        if (phase, kind) == (expected_phase, expected_kind):
            return index["batches"][name]
    raise ValueError("evaluation is not admitted in this phase")


def verify_release(root):
    root = Path(root)
    index = json.loads((root / "release.json").read_bytes())
    payload = {k: v for k, v in index.items() if k != "release_id"}
    if index["schema"] != SCHEMA or index["release_id"] != "task-batches-" + sha(canonical(payload)):
        raise ValueError("task release identity mismatch")
    if set(index["batches"]) == set(BATCHES):
        definitions, modern = BATCHES, True
    elif index["schema"] == SCHEMA and set(index["batches"]) == set(LEGACY_BATCHES):
        # Historical releases remain verifiable after the new partition is
        # introduced; their bytes and identities are never rewritten.
        definitions, modern = LEGACY_BATCHES, False
    else:
        raise ValueError("task batch names mismatch")
    for path, digest in index["files"].items():
        if sha((root / safe_name(path)).read_bytes()) != digest:
            raise ValueError("frozen file changed: " + path)
    audit = json.loads((root / "source-audit.json").read_bytes())
    check_archive((root / "source.tar.gz").read_bytes(), audit["files"])
    summaries = {}
    for name, definition in definitions.items():
        if modern:
            phase, kind, role, count, network = definition
        else:
            phase, kind, count, network = definition
            role = None
        batch = json.loads((root / name / "batch.json").read_bytes())
        identity = "task-batch-" + sha(canonical(batch))
        if index["batches"][name] != identity or batch["admission"] != {"phase": phase, "kind": kind}:
            raise ValueError("batch selection mismatch")
        if len(batch["tasks"]) != count or batch["network_policy"] != network:
            raise ValueError("batch contract mismatch")
        if modern and batch.get("evaluation_role") != role:
            raise ValueError("batch evaluation role mismatch")
        if modern and role == "public":
            if any(not task["public"] or task.get("evaluation_role") != "binary" for task in batch["tasks"]):
                raise ValueError("public sample must be a binary-task subset")
        elif modern and any(task.get("evaluation_role") != role for task in batch["tasks"]):
            raise ValueError("batch contains a task from another evaluation role")
        files = {}
        for task in batch["tasks"]:
            if sha(canonical(task["files"])) != task["task_files_sha256"]:
                raise ValueError("task digest mismatch")
            if any(audit["files"].get(k) != v for k, v in task["files"].items()):
                raise ValueError("task differs from upstream")
            files.update(task["files"])
        check_archive((root / name / "tasks.tar.gz").read_bytes(), files)
        summaries[name] = {"batch_id": identity, "tasks": count, "network_policy": network}
    return {"release_id": index["release_id"], "verified": True,
            "source_files": len(audit["files"]), "batches": summaries,
            "runtime_activated": False, "cloud_execution_tested": False}


def freeze(source, audit_path, roster_path, releases_root, practice_seconds=180):
    source, releases_root = Path(source), Path(releases_root)
    audit = json.loads(Path(audit_path).read_bytes())
    roster = json.loads(Path(roster_path).read_bytes())
    verify_source(source, audit)
    tasks = sorted(roster["tasks"], key=lambda t: t["task_id"])
    if len(tasks) != 49 or {t["task_id"] for t in tasks} != set(audit["competition_task_ids"]):
        raise ValueError("paper49 roster mismatch")
    if sum(t["public"] for t in tasks) != 5 or type(practice_seconds) is not int or practice_seconds <= 0:
        raise ValueError("invalid public tasks or practice budget")
    role_counts = {role: sum(task.get("evaluation_role") == role for task in tasks)
                   for role in ("binary", "racing", "infrastructure")}
    if role_counts != {"binary": 39, "racing": 7, "infrastructure": 3}:
        raise ValueError("paper roster must contain the 39/7/3 evaluation partition")
    rows = [task_row(source, task, audit["files"]) for task in tasks]
    files = {"source-audit.json": canonical(audit) + b"\n",
             "paper-task-roster.json": canonical(roster) + b"\n",
             "source.tar.gz": archive_bytes(source, audit["files"])}
    batches = {}
    for name, (phase, kind, role, count, network) in BATCHES.items():
        if role == "public":
            selected = [r for r in rows if r["public"]]
        else:
            selected = [r for r in rows if r.get("evaluation_role") == role]
        if len(selected) != count:
            raise ValueError(f"{name} selection expected {count} tasks, got {len(selected)}")
        task_files = {k: v for row in selected for k, v in row["files"].items()}
        package = archive_bytes(source, task_files)
        batch = {"schema": "qiqc-frozen-task-batch/v1", "name": name,
                 "freeze_scope": "task-source-and-execution-policy", "frozen": True,
                 "admission": {"phase": phase, "kind": kind},
                 "source_commit": audit["source_commit"],
                 "source_files_sha256": audit["source_files_sha256"],
                 "source_archive_sha256": sha(files["source.tar.gz"]),
                 "tasks_archive_sha256": sha(package), "tasks": selected,
                 "evaluation_role": role,
                 "network_policy": network,
                 "network_override": None if network == "upstream" else {
                     "external_internet": False,
                     "allowed_internal_services": ["current-task-qsim", "competition-model-gateway"],
                     "verifier_external_internet": False},
                 "task_format": "original-harbor", "task_file_modifications": [],
                 "agent_budget_policy": "native" if phase == "competition" or kind == "public" else "min-native-and-practice",
                 "practice_agent_cap_seconds": practice_seconds if phase == "practice" else None,
                 "competition_attempts_per_team": {"full": 1, "public": 3}.get(kind),
                 "leaderboard_eligible": kind == "full",
                 "post_competition_elo": kind == "elo",
                 "infrastructure_acceptance": kind == "infra",
                 "runtime_acceptance": "not-asserted-by-task-freeze"}
        batches[name] = "task-batch-" + sha(canonical(batch))
        files[name + "/batch.json"] = canonical(batch) + b"\n"
        files[name + "/tasks.tar.gz"] = package
    index = {"schema": SCHEMA, "freeze_scope": "task-source-and-execution-policy",
             "source_commit": audit["source_commit"], "batches": batches,
             "files": {name: sha(data) for name, data in sorted(files.items())}}
    index["release_id"] = "task-batches-" + sha(canonical(index))
    files["release.json"] = canonical(index) + b"\n"
    releases_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = releases_root / index["release_id"]
    if target.exists():
        return target, verify_release(target)
    temporary = Path(tempfile.mkdtemp(prefix=".freeze-", dir=releases_root))
    try:
        for name, data in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        report = verify_release(temporary)
        for path in temporary.rglob("*"):
            path.chmod(0o500 if path.is_dir() else 0o400)
        temporary.chmod(0o500)
        temporary.rename(target)
        fd = os.open(releases_root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return target, report
    finally:
        if temporary.exists():
            for path in temporary.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            temporary.chmod(0o700)
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("freeze")
    for name in ("source", "audit", "roster", "output"):
        create.add_argument("--" + name, type=Path, required=True)
    create.add_argument("--practice-seconds", type=int, default=180)
    check = sub.add_parser("verify")
    check.add_argument("release", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify_release(args.release), indent=2))
    else:
        path, report = freeze(args.source, args.audit, args.roster, args.output, args.practice_seconds)
        print(json.dumps({"path": str(path), **report}, indent=2))


if __name__ == "__main__":
    main()
