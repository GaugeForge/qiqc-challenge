#!/usr/bin/env python3
"""Restore omitted scoring sources without executing Benchmark code on the host.

Verify every retained runtime file against the original before adding anything.
The original checkout is read only. No Quantum-Harbor compatibility overlay is
used. This is a catalog service bundle, never a participant/Agent bundle.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from task_manifest import ManifestError, secure_read

ROOT = Path(__file__).resolve().parents[4]
MAPPING = {
    "qiqcbench": "src/qiqcbench",
    "configs": "configs",
    "harbor_tasks": "harbor_tasks",
}


def restore(source: Path, runtime: Path):
    retained = {}
    # Complete preflight before mutation: a mismatched version must never be
    # silently combined with scoring code from another revision.
    for target_prefix, source_prefix in MAPPING.items():
        for path in sorted((runtime / target_prefix).rglob("*")):
            if "__pycache__" in path.parts or path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(runtime).as_posix()
            original = str(
                Path(source_prefix) / path.relative_to(runtime / target_prefix)
            )
            data = secure_read(runtime, relative)
            if data != secure_read(source, original):
                raise ManifestError("runtime-source-mismatch: " + relative)
            retained[relative] = hashlib.sha256(data).hexdigest()
    pending = {}
    for path in sorted((source / "src/qiqcbench/eval").rglob("*.py")):
        relative = path.relative_to(source / "src").as_posix()
        pending[relative] = secure_read(source, "src/" + relative)
    required = {
        "qiqcbench/eval/score_report.py",
        "qiqcbench/eval/verifier/legacy_elo.py",
    }
    if not required <= pending.keys():
        raise ManifestError("original-scoring-package-incomplete")
    # Refuse symlink parents even when the destination file does not yet exist.
    for relative in pending:
        current = runtime
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ManifestError("runtime-destination-symlink")
    added = []
    for relative, data in pending.items():
        path = runtime / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            added.append(relative)
    return {
        "scope": "source-exact-catalog-runtime-restoration",
        "benchmark_executed_on_host": False,
        "retained_files_verified": len(retained),
        "restored_files": added,
        "eval_file_count": len(pending),
        "eval_sha256": {
            p: hashlib.sha256(data).hexdigest() for p, data in pending.items()
        },
        "retained_sha256": retained,
        "cross_checkout_overlay": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "benchmark-assets/qq_main/QIQCBench")
    parser.add_argument("--runtime", type=Path, default=ROOT / "benchmark-assets/qiqcbench-runtime")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(args.source), "status", "--porcelain"], text=True
    ).strip()
    if dirty:
        raise ManifestError("original-checkout-must-be-clean")
    report = restore(args.source, args.runtime)
    report.update(source=str(args.source), source_commit=commit, source_clean=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verified": report["retained_files_verified"],
                "restored": len(report["restored_files"]),
                "source_commit": commit,
            }
        )
    )


if __name__ == "__main__":
    main()
