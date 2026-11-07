"""Verified local DB/ZIP snapshot bundles, deliberately excluding real rollout data.

Requires quiesced application writes. This is not encryption, off-site backup,
point-in-time recovery, or a substitute for a production restore exercise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time

SCHEMA = "qiqc-local-snapshot/v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Snapshot root must be a real directory")
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Snapshot links are forbidden")
        mode = path.stat().st_mode
        if stat.S_ISREG(mode):
            found.append(path)
        elif not stat.S_ISDIR(mode):
            raise ValueError("Snapshot special files are forbidden")
    return found


def _safe_key(key: str) -> None:
    path = PurePosixPath(key)
    if not key or path.is_absolute() or "\\" in key or any(p in {"", ".", ".."} for p in key.split("/")):
        raise ValueError("Unsafe snapshot manifest path")
    if path.parts[0] not in {"uploads", "objects", "platform.db"}:
        raise ValueError("Unexpected snapshot component")
    if path.parts[0] == "platform.db" and len(path.parts) != 1:
        raise ValueError("Invalid database path")


def _db_check(path: Path) -> None:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign key check failed")


def verify(bundle: Path) -> dict:
    bundle = Path(bundle).absolute()
    files = _files(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA or manifest.get("encrypted") is not False:
        raise ValueError("Unknown snapshot manifest")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or "platform.db" not in entries:
        raise ValueError("Missing snapshot file inventory")
    for key in entries:
        _safe_key(key)
    actual = {str(p.relative_to(bundle)) for p in files if p.name != "manifest.json" or p.parent != bundle}
    if actual != set(entries):
        raise ValueError("Snapshot file set differs from manifest")
    for key, meta in entries.items():
        path = bundle / key
        if path.stat().st_size != meta["size"] or sha256(path) != meta["sha256"]:
            raise ValueError("Snapshot file digest mismatch")
    _db_check(bundle / "platform.db")
    return manifest


def create(source: Path, destination: Path, *, quiesced: bool = False, synthetic_objects: bool = False) -> dict:
    if not quiesced:
        raise ValueError("Application writes must be quiesced before snapshot")
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Snapshot destination must not exist")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Snapshot source and destination must be separate")
    if source.is_symlink() or (source / "platform.db").is_symlink():
        raise ValueError("Source links are forbidden")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stage = Path(tempfile.mkdtemp(prefix=".qiqc-snapshot-", dir=destination.parent))
    try:
        with sqlite3.connect((source / "platform.db").as_uri() + "?mode=ro", uri=True) as db:
            with sqlite3.connect(stage / "platform.db") as target:
                db.backup(target)
        components = ["uploads"] + (["objects"] if synthetic_objects else [])
        for name in components:
            src = source / name
            (stage / name).mkdir(mode=0o700)
            for path in _files(src):
                out = stage / name / path.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(path, out)
                if sha256(path) != sha256(out):
                    raise ValueError("Source changed while snapshotting")
        entries = {}
        for path in _files(stage):
            path.chmod(0o600)
            entries[str(path.relative_to(stage))] = {"size": path.stat().st_size, "sha256": sha256(path)}
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        manifest = {"schema": SCHEMA, "created_unix": time.time(), "encrypted": False,
                    "offsite": False, "synthetic_objects": synthetic_objects, "files": entries}
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.chmod(0o600)
        with manifest_path.open("rb") as stream:
            os.fsync(stream.fileno())
        verify(stage)
        stage.rename(destination)
        return {"files": len(entries), "seconds": round(time.monotonic() - started, 6),
                "manifest_sha256": sha256(destination / "manifest.json"), "offsite": False, "encrypted": False}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore(bundle: Path, destination: Path) -> dict:
    bundle, destination = Path(bundle).absolute(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination must not exist")
    if destination.is_relative_to(bundle) or bundle.is_relative_to(destination):
        raise ValueError("Restore destination must be separate")
    manifest = verify(bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stage = Path(tempfile.mkdtemp(prefix=".qiqc-restore-", dir=destination.parent))
    try:
        for key in manifest["files"]:
            target = stage / key
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(bundle / key, target)
            target.chmod(0o600)
        shutil.copyfile(bundle / "manifest.json", stage / "manifest.json")
        (stage / "manifest.json").chmod(0o600)
        verify(stage)
        stage.rename(destination)
        return {"files": len(manifest["files"]), "seconds": round(time.monotonic() - started, 6),
                "verified": True, "offsite": False}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    backup.add_argument("--writes-quiesced", action="store_true", required=True)
    recovery = commands.add_parser("restore")
    recovery.add_argument("bundle", type=Path)
    recovery.add_argument("destination", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    args = parser.parse_args()
    if args.command == "create":
        result = create(args.source, args.destination, quiesced=args.writes_quiesced)
    elif args.command == "restore":
        result = restore(args.bundle, args.destination)
    else:
        result = {"verified": True, "files": len(verify(args.bundle)["files"])}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
