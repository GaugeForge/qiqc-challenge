"""Role-specific science bundles contain no other task's data or test files."""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
from e2b_runtime import PUBLIC_TASK, archive_runtime


@pytest.mark.parametrize("role", ["qsim", "verifier"])
def test_only_cz_config_and_role_tests(role):
    with tarfile.open(
        fileobj=io.BytesIO(archive_runtime(PUBLIC_TASK, role=role))
    ) as tar:
        names = tar.getnames()
        configs = [name for name in names if "/configs/" in name]
        assert len(configs) == (3 if role == "qsim" else 4)
        assert all("tunable_coupler_cz_" in name for name in configs)
        if role == "qsim":
            assert not any(
                "/harbor_tasks/" in name
                or "/hidden_dynamics/" in name
                or "/eval/" in name
                for name in names
            )
        else:
            assert any(name.endswith("/tests/score_cz.py") for name in names)
            assert all(
                PUBLIC_TASK in name for name in names if "/harbor_tasks/" in name
            )
        manifest = json.load(tar.extractfile("runtime/deployment-manifest.json"))
        assert manifest["role"] == role
        assert len(manifest["files"]) == len(names) - 1


def test_unknown_task_cannot_use_single_task_bundle():
    with pytest.raises(ValueError):
        archive_runtime("other-task", role="qsim")


def test_single_task_archive_missing_required_truth_fails_closed(tmp_path, monkeypatch):
    import e2b_runtime
    from task_manifest import ManifestError

    root = tmp_path / "runtime"
    (root / "qiqcbench").mkdir(parents=True)
    monkeypatch.setattr(e2b_runtime, "RUNTIME", root)
    with pytest.raises(ManifestError):
        e2b_runtime.archive_runtime(e2b_runtime.PUBLIC_TASK, role="qsim")


def test_single_task_archive_parent_symlink_fails_closed(tmp_path, monkeypatch):
    import e2b_runtime
    from task_manifest import ManifestError

    root = tmp_path / "runtime"
    external = tmp_path / "external"
    (root / "qiqcbench").mkdir(parents=True)
    (external / "tasks").mkdir(parents=True)
    (external / "tasks" / (e2b_runtime.PUBLIC_TASK + ".yaml")).write_text(
        "synthetic private data"
    )
    (root / "configs").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(e2b_runtime, "RUNTIME", root)
    with pytest.raises(ManifestError):
        e2b_runtime.archive_runtime(e2b_runtime.PUBLIC_TASK, role="qsim")
