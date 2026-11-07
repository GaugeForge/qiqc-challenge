"""Run with Harbor's interpreter; validates the restricted provider contract."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("harbor") is None,
    reason="Run this gate with the installed Harbor Python environment",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/science"))


def environment(task="tunable_coupler_cz_netzero", cache="/dev/shm/qiqc-test"):
    from harbor_e2b import RUNTIME, CZEnvironment

    obj = object.__new__(CZEnvironment)
    obj.environment_name = task
    obj.session_id = task + "__env"
    obj.environment_dir = RUNTIME / "harbor_tasks" / task / "environment"
    obj.trial_paths = SimpleNamespace(trial_dir=Path(cache))
    return obj


def test_reviewed_compose_supported():
    obj = environment()
    obj._validate_definition()
    assert obj.capabilities.docker_compose
    assert obj.main is None and obj.qsim is None


def test_second_frozen_task_supported():
    obj = environment(task="bell2_fidelity")
    obj._validate_definition()
    assert obj.task_id == "bell2_fidelity"


def test_unknown_task_fails_closed():
    with pytest.raises(ValueError):
        environment(task="not_in_frozen_catalog")._validate_definition()


def test_rollout_disk_cache_rejected():
    with pytest.raises(ValueError, match="/dev/shm"):
        environment(cache="/tmp/qiqc-test")._validate_definition()


def test_unknown_service_rejected():
    obj = environment()
    obj._validate_definition()
    with pytest.raises(ValueError, match="Unsupported service"):
        obj._service("other-task")


def test_hamlearn_grading_preserves_original_nonroot_identity(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import harbor_e2b
    calls = []
    obj = environment(task="time_budgeted_hamlearn_10q")
    obj.session_id += "__verifier"
    obj._validate_definition()
    obj.default_user = None
    obj._merge_env = lambda env: env
    monkeypatch.setattr(harbor_e2b, "runtime_env", lambda *args, **kwargs: {})
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=lambda command, **kwargs: calls.append(kwargs) or SimpleNamespace(stdout="", stderr="", exit_code=0)))
    asyncio.run(obj._exec(sandbox, "bash /tests/test.sh", user="root"))
    assert calls[-1]["user"] == "verifier"
    assert calls[-2]["user"] == "root"
    asyncio.run(obj._exec(sandbox, "mkdir -p /tests", user="root"))
    assert calls[-1]["user"] == "root"


def test_qsim_startup_failure_log_is_private_not_exception_text(tmp_path):
    import asyncio
    import json
    obj = environment(task="binomial_code_qec_advantage", cache=str(tmp_path))
    obj.task_id = "binomial_code_qec_advantage"
    obj.qsim = object()
    calls = []
    async def collect(sandbox, source, destination):
        calls.append((source, destination))
        destination.write_text("synthetic-private-startup-payload")
    obj._download_file = collect
    asyncio.run(obj._capture_startup_failure({"http_status": 502, "transport_error_class": None}))
    report = json.loads((tmp_path / "artifacts/qsim-startup-diagnostics.json").read_text())
    assert report["private_log_collected"] is True
    assert "synthetic-private" not in json.dumps(report)
    assert calls[0][0] == "/tmp/qsim.log"
    assert calls[0][1].name == "private-qsim-startup.log"


def test_startup_failure_collection_error_is_class_only(tmp_path):
    import asyncio
    import json
    obj = environment(task="binomial_code_qec_advantage", cache=str(tmp_path))
    obj.task_id = "binomial_code_qec_advantage"
    obj.qsim = object()
    async def collect(*args):
        raise ValueError("synthetic-secret-do-not-include")
    obj._download_file = collect
    asyncio.run(obj._capture_startup_failure({"http_status": None, "transport_error_class": "ConnectError"}))
    report = json.loads((tmp_path / "artifacts/qsim-startup-diagnostics.json").read_text())
    assert report["private_log_collected"] is False
    assert report["collection_error_class"] == "ValueError"
    assert "synthetic-secret" not in json.dumps(report)
