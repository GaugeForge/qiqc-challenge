"""Offline native-call regressions; no real DNS, remote hosts or cloud calls."""

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

import agent_boundary_probe as probe
import pytest


def test_python_signal_handler_is_delayed_by_retrying_native_wait():
    # libc system() waits/retries in C. It reproduces the signal-delivery
    # assumption that Python sleep() did NOT cover in the first fix's tests.
    code = """import ctypes,signal,time,json
start=time.monotonic()
def handler(*_):
 print(json.dumps({'elapsed':time.monotonic()-start}));raise SystemExit(0)
signal.signal(signal.SIGALRM,handler)
signal.setitimer(signal.ITIMER_REAL,.02)
ctypes.CDLL(None).system(b'sleep .15')
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    assert json.loads(result.stdout)["elapsed"] >= 0.1


def test_hard_parent_deadline_kills_native_wait_and_descendants():
    # No Python handler in the blocked child is required. The parent also kills
    # the shell/sleep descendants, so no acceptance process is orphaned.
    command = [
        sys.executable,
        "-c",
        "import ctypes;ctypes.CDLL(None).system(b'sleep 10')",
    ]
    started = time.monotonic()
    for _ in range(2):
        assert probe.run_fixed_child(command, timeout_seconds=0.05) == {
            "transport": "BoundaryProbeTimeout"
        }
    assert time.monotonic() - started < 1


def test_probe_dispatch_is_fixed_and_does_not_invoke_shell(monkeypatch):
    observed = []

    def fake(command, **kwargs):
        observed.append((command, kwargs))
        return {"transport": "BoundaryProbeTimeout"}

    monkeypatch.setattr(probe, "run_fixed_child", fake)
    assert probe.probe_url("https://example.com") == {
        "transport": "BoundaryProbeTimeout"
    }
    command, kwargs = observed[0]
    assert command[-2:] == ["--network-child", "public_internet"]
    assert kwargs == {"timeout_seconds": 6}
    with pytest.raises(ValueError):
        probe.probe_url("https://attacker.invalid")


def test_http_rejection_is_reachable_not_timeout(monkeypatch):
    def denied(*_args, **_kwargs):
        raise HTTPError("https://example.com", 403, "private body", {}, None)

    monkeypatch.setattr(probe, "urlopen", denied)
    assert probe.child_network_probe("public_internet") == {
        "status": 403,
        "transport": "response",
    }


def test_child_failure_does_not_become_successful_blocking():
    with pytest.raises(RuntimeError, match="boundary_probe_child_failed"):
        probe.run_fixed_child(
            [sys.executable, "-c", "raise SystemExit(2)"], timeout_seconds=1
        )


def test_gate_still_40_seconds():
    source = (Path(__file__).parent / "harbor_agent.py").read_text()
    assert "timeout_sec=40" in source
    assert (
        "timeout_seconds=6"
        in (Path(__file__).parent / "agent_boundary_probe.py").read_text()
    )


def test_current_public_runtime_is_allowed_without_payload_reads(tmp_path):
    task='current_task'
    public=tmp_path/'configs/task_materials'/task/'public'
    public.mkdir(parents=True)
    (public/'input.bin').write_bytes(b'public input')
    instruction=tmp_path/'harbor_tasks'/task
    instruction.mkdir(parents=True)
    (instruction/'instruction.md').write_text('current instruction')
    (tmp_path/'deployment-manifest.json').write_text('{}')
    assert probe.runtime_public_only(task,tmp_path)


@pytest.mark.parametrize('path',['qiqcbench/score.py','configs/devices/device.hidden.yaml','configs/tasks/other.yaml','configs/task_materials/current_task/hidden/truth.json','configs/task_materials/current_task/runtime/input.bin','configs/task_materials/other_task/public/input.json','harbor_tasks/current_task/tests/score.py','harbor_tasks/other_task/instruction.md','probe.py'])
def test_private_or_other_task_runtime_is_rejected(tmp_path,path):
    target=tmp_path/path
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text('never read')
    assert not probe.runtime_public_only('current_task',tmp_path)


def test_public_runtime_symlink_is_rejected(tmp_path):
    path=tmp_path/'configs/task_materials/current_task/public'
    path.mkdir(parents=True)
    (path/'escape').symlink_to('/etc/passwd')
    assert not probe.runtime_public_only('current_task',tmp_path)
