"""Freeze enforcement, including mutation between preflight and tar reads."""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
import e2b_runtime
import task_release
from task_manifest import ManifestError


@pytest.fixture
def frozen(tmp_path):
    runtime = tmp_path / "benchmark-assets/qiqcbench-runtime"
    records = {}
    for name in (
        "qiqcbench/eval/score_report.py",
        "qiqcbench/eval/verifier/legacy_elo.py",
    ):
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# trusted fixture\n")
        records[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": 0o644,
        }
    (runtime / "PROVENANCE.json").write_text("{}")
    (runtime / "MANIFEST.sha256").write_text("")
    release = {
        "release_id": "fixture-v1",
        "runtime_sha256": task_release.digest(records),
        "files": records,
        "provenance_sha256": hashlib.sha256(b"{}").hexdigest(),
        "checksum_index_sha256": hashlib.sha256(b"").hexdigest(),
    }
    data = json.dumps(release).encode()
    target = tmp_path / "release.json"
    target.write_bytes(data)
    pin = tmp_path / "pin.json"
    pin.write_text(
        json.dumps(
            {
                "release_id": "fixture-v1",
                "path": "release.json",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    )
    return tmp_path, runtime, pin


def test_unchanged_release_without_original_repository(frozen):
    root, runtime, pin = frozen
    assert task_release.verify_release(root, runtime, pin)["release_id"] == "fixture-v1"


@pytest.mark.parametrize(
    "mutation", ["content", "missing", "extra", "index", "release", "link"]
)
def test_frozen_change_is_rejected(frozen, mutation):
    root, runtime, pin = frozen
    target = runtime / "qiqcbench/eval/score_report.py"
    if mutation == "content":
        target.write_text("changed")
    elif mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        (runtime / "extra.py").write_text("extra")
    elif mutation == "index":
        (runtime / "PROVENANCE.json").write_text(
            '{"new":"self-consistent-index-cannot-replace-pin"}'
        )
    elif mutation == "release":
        (root / "release.json").write_text("{}")
    else:
        outside = root / "outside"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    with pytest.raises(ManifestError):
        task_release.verify_release(root, runtime, pin)


def test_release_creation_never_overwrites_active_pin(frozen):
    root, _runtime, pin = frozen
    before = pin.read_bytes()
    with pytest.raises(ManifestError, match="pin-already-exists"):
        task_release.create_release(root, "fixture-v2", pin)
    assert pin.read_bytes() == before


def test_tar_reads_recheck_frozen_bytes_and_exclude_bytecode(frozen, monkeypatch):
    root, runtime, pin = frozen
    (runtime / "untrusted.pyc").write_bytes(b"not executed")
    monkeypatch.setattr(e2b_runtime, "ROOT", root)
    monkeypatch.setattr(e2b_runtime, "RUNTIME", runtime)
    monkeypatch.setattr(
        e2b_runtime,
        "verify_release",
        lambda root, runtime: task_release.verify_release(root, runtime, pin),
    )
    with tarfile.open(fileobj=io.BytesIO(e2b_runtime.archive_runtime())) as archive:
        assert "runtime/untrusted.pyc" not in archive.getnames()
        assert (
            json.load(archive.extractfile("runtime/benchmark-release.json"))[
                "release_id"
            ]
            == "fixture-v1"
        )

    def mutate_after_preflight(root, runtime):
        release = task_release.verify_release(root, runtime, pin)
        (runtime / "qiqcbench/eval/score_report.py").write_text(
            "changed after preflight"
        )
        return release

    monkeypatch.setattr(e2b_runtime, "verify_release", mutate_after_preflight)
    with pytest.raises(ManifestError, match="frozen-file-mismatch"):
        e2b_runtime.archive_runtime()


def test_deployment_read_permissions_do_not_change_archive_mode(frozen):
    root, runtime, pin = frozen
    (runtime / "qiqcbench/eval/score_report.py").chmod(0o640)
    assert (
        task_release.verify_release(root, runtime, pin)["files"][
            "qiqcbench/eval/score_report.py"
        ]["mode"]
        == 0o644
    )
