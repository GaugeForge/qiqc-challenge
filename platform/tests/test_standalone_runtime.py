"""Integrity checks require no original checkout or scientific execution."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
from standalone_runtime import verify
from task_manifest import ManifestError


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "task.txt"
    path.write_text("fixture task\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    files = {
        "task.txt": {
            "sha256": sha,
            "mode": path.stat().st_mode & 0o777,
            "source_path": "removed/task.txt",
        }
    }
    (tmp_path / "PROVENANCE.json").write_text(
        json.dumps({"source_commit": "frozen", "task_ids": ["fixture"], "files": files})
    )
    (tmp_path / "MANIFEST.sha256").write_text(f"{sha}  task.txt\n")
    return tmp_path


def test_verify_without_source_checkout(bundle):
    assert verify(bundle)["passed"]
    assert verify(bundle)["source_checkout_required"] is False


@pytest.mark.parametrize(
    "mutation", ["content", "missing", "extra", "mode", "index", "symlink", "hardlink"]
)
def test_tampering_rejected(bundle, mutation):
    path = bundle / "task.txt"
    if mutation == "content":
        path.write_text("changed")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        (bundle / "unexpected.txt").write_text("extra")
    elif mutation == "mode":
        path.chmod(0o700)
    elif mutation == "index":
        (bundle / "MANIFEST.sha256").write_text("wrong")
    else:
        external = bundle.parent / (bundle.name + "-external")
        external.write_bytes(path.read_bytes())
        path.unlink()
        if mutation == "symlink":
            path.symlink_to(external)
        else:
            os.link(external, path)
    with pytest.raises(ManifestError):
        verify(bundle)
