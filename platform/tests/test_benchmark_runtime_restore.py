"""A source restoration must not combine revisions or follow links."""

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
from restore_benchmark_runtime import restore
from task_manifest import ManifestError


def fixture(tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    for prefix in (source / "src/qiqcbench", runtime / "qiqcbench"):
        prefix.mkdir(parents=True)
        (prefix / "__init__.py").write_text("# original\n")
    for name in ("score_report.py", "verifier/legacy_elo.py"):
        path = source / "src/qiqcbench/eval" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original scoring\n")
    return source, runtime


def test_restoration_is_exact_and_idempotent(tmp_path):
    source, runtime = fixture(tmp_path)
    first = restore(source, runtime)
    assert first["retained_files_verified"] == 1
    assert len(first["restored_files"]) == 2
    assert restore(source, runtime)["restored_files"] == []


def test_mixed_source_version_rejected_before_copy(tmp_path):
    source, runtime = fixture(tmp_path)
    (runtime / "qiqcbench/__init__.py").write_text("# different revision\n")
    with pytest.raises(ManifestError, match="runtime-source-mismatch"):
        restore(source, runtime)
    assert not (runtime / "qiqcbench/eval").exists()


def test_symlink_destination_rejected(tmp_path):
    source, runtime = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime / "qiqcbench/eval").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError):
        restore(source, runtime)
    assert list(outside.iterdir()) == []


def test_catalog_uses_runtime_scoring_without_cross_checkout_overlay():
    from e2b_runtime import RUNTIME, archive_runtime

    with tarfile.open(fileobj=io.BytesIO(archive_runtime())) as archive:
        names = archive.getnames()
        assert len(names) == len(set(names))
        for relative in (
            "qiqcbench/eval/score_report.py",
            "qiqcbench/eval/verifier/legacy_elo.py",
        ):
            assert (
                archive.extractfile("runtime/" + relative).read()
                == (RUNTIME / relative).read_bytes()
            )


def test_catalog_refuses_omitted_scoring_package(tmp_path, monkeypatch):
    import e2b_runtime

    monkeypatch.setattr(e2b_runtime, "RUNTIME", tmp_path)
    with pytest.raises(ManifestError):
        e2b_runtime.archive_runtime()
