"""Shared teardown limits and primary-error preservation; local fakes only."""
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
sys.path.insert(0, str(ROOT / "competition-platform/platform/scripts/science"))
import native_worker_bridge as bridge


def test_archive_stops_before_cleanup_allowance(monkeypatch):
    monkeypatch.setattr(bridge, "read_remote_bytes", lambda *a, **kw: pytest.fail("unexpected remote read"))
    with pytest.raises(bridge.RemoteFileError, match="deadline exceeded"):
        bridge.read_archive_bytes(object(), "/archive", 100, expected_sha256="a" * 64,
                                  deadline=110, clock=lambda: 100)


def test_archive_shared_budget_does_not_restart_after_transient_failure(monkeypatch):
    now = [100.0]
    calls = []
    def read(*a, **kw):
        calls.append(kw)
        now[0] = 101.8
        raise bridge.RemoteFileError("Remote file transfer failed")
    monkeypatch.setattr(bridge, "read_remote_bytes", read)
    with pytest.raises(bridge.RemoteFileError, match="deadline exceeded"):
        bridge.read_archive_bytes(object(), "/archive", 100, expected_sha256="a" * 64,
                                  deadline=112, clock=lambda: now[0],
                                  delay=lambda _: pytest.fail("retry exceeded remaining budget"))
    assert len(calls) == 1
    assert calls[0]["request_timeout"] == 2
    assert calls[0]["deadline"] == 102


def test_archive_success_keeps_hash_and_shared_read_deadline(monkeypatch):
    body = b"public synthetic bounded archive"
    def read(*a, **kw):
        assert kw["deadline"] == 103
        assert kw["request_timeout"] == 3
        return body
    monkeypatch.setattr(bridge, "read_remote_bytes", read)
    assert bridge.read_archive_bytes(object(), "/archive", 100,
            expected_sha256=hashlib.sha256(body).hexdigest(), deadline=113, clock=lambda: 100) == body


def test_partial_evidence_expired_budget_keeps_primary_without_remote_reads(monkeypatch):
    monkeypatch.setattr(bridge, "read_remote_bytes", lambda *a, **kw: pytest.fail("remote read after budget"))
    data = bridge.fallback_failure_archive(SimpleNamespace(files=object()),
        {"run_id": "run", "sha256": "a" * 64}, [], ValueError("native_model_unrecovered"),
        RuntimeError("private-capture-detail"), None, deadline=110, clock=lambda: 100,
        failure_phase="packing_rollout")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        original = json.load(archive.extractfile("model-failure-evidence.json"))
        partial = json.load(archive.extractfile("diagnostic-completeness.json"))
    assert original["failure_code"] == "native_model_unrecovered"
    assert original["failure_phase"] == "packing_rollout"
    assert partial["complete_rollout"] is False
    assert len(partial["unavailable_files"]) == 5
    assert set(partial["unavailable_files"].values()) == {"FinalizationReadDeadlineExceeded"}
    assert "private-capture-detail" not in json.dumps(partial)


def test_partial_archive_keeps_distinct_primary_and_capture_file_diagnostics(monkeypatch):
    monkeypatch.setattr(bridge, "read_remote_bytes", lambda *a, **kw: pytest.fail("remote read after budget"))
    primary = {"operation": "read", "underlying_class": "ReadError", "bytes_received": 3500000,
               "elapsed_ms": 1500, "request_timeout_seconds": 10}
    capture = {**primary, "bytes_received": 3600000, "elapsed_ms": 1600}
    data = bridge.fallback_failure_archive(SimpleNamespace(files=object()),
        {"run_id": "run", "sha256": "a" * 64}, [],
        bridge.RemoteFileError("Remote file transfer failed", diagnostic=primary),
        bridge.RemoteFileError("Remote file transfer failed", diagnostic=capture), None,
        deadline=110, clock=lambda: 100, failure_phase="downloading_rollout")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        original = json.load(archive.extractfile("model-failure-evidence.json"))
        partial = json.load(archive.extractfile("diagnostic-completeness.json"))
    assert original["remote_file_diagnostic"] == primary
    assert original["failure_phase"] == "downloading_rollout"
    assert partial["remote_file_diagnostic"] == capture
    assert partial["complete_rollout"] is False


def test_partial_evidence_keeps_in_memory_primary_and_capture_after_sdk_write_failure(monkeypatch):
    monkeypatch.setattr(bridge, "read_remote_bytes", lambda *a, **kw: pytest.fail("remote read after budget"))
    primary = {"command_sha256": "a" * 64, "error_class": "TimeoutException"}
    capture = {"command_sha256": "b" * 64, "error_class": "ReadTimeout"}
    data = bridge.fallback_failure_archive(SimpleNamespace(files=object()),
        {"run_id": "run", "sha256": "a" * 64}, [], ValueError("native_model_unrecovered"),
        RuntimeError("private-capture-detail"), None, deadline=110, clock=lambda: 100,
        command_diagnostics={"trusted-command-failure.json": primary,
                             "trusted-capture-command-failure.json": capture,
                             "../../outside": {"ignored": True}})
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        assert "../../outside" not in archive.getnames()
        assert json.load(archive.extractfile("trusted-command-failure.json")) == primary
        assert json.load(archive.extractfile("trusted-capture-command-failure.json")) == capture
        partial = json.load(archive.extractfile("diagnostic-completeness.json"))
    assert len(partial["unavailable_files"]) == 3


def test_expanded_diagnostic_tail_cannot_drop_primary_command_identity(monkeypatch):
    monkeypatch.setattr(bridge, "read_remote_bytes", lambda *a, **kw: pytest.fail("remote read after budget"))
    primary = {"command_sha256": "a" * 64, "error_class": "TimeoutException", "stdout": "\0" * 1048576}
    data = bridge.fallback_failure_archive(SimpleNamespace(files=object()),
        {"run_id": "run", "sha256": "a" * 64}, [], ValueError("native_model_unrecovered"),
        RuntimeError("capture"), None, deadline=110, clock=lambda: 100,
        command_diagnostics={"trusted-command-failure.json": primary})
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        evidence = json.load(archive.extractfile("trusted-command-failure.json"))
    assert evidence["command_sha256"] == primary["command_sha256"]
    assert evidence["diagnostic_truncated"] is True
    assert len(evidence["stdout"]) == 65536


@pytest.mark.parametrize("already_absent", [False, True])
def test_cleanup_confirms_deleted_or_absent(monkeypatch, already_absent):
    events, calls = [], []
    def kill(**kw):
        calls.append(kw)
        return not already_absent
    monkeypatch.setattr(bridge, "emit", events.append)
    sandbox = SimpleNamespace(sandbox_id="synthetic-vm", kill=kill)
    assert bridge.cleanup_native_sandbox(sandbox, deadline=105, clock=lambda: 100)
    assert calls == [{"request_timeout": 5}]
    assert events[-1]["state"] == "destroyed"


def test_cleanup_failure_preserves_primary_and_requires_reconciliation(monkeypatch):
    events = []
    def kill(**kw):
        raise RuntimeError("private-provider-detail")
    monkeypatch.setattr(bridge, "emit", events.append)
    sandbox = SimpleNamespace(sandbox_id="synthetic-vm", kill=kill)
    primary = ValueError("native_model_unrecovered")
    with pytest.raises(ValueError) as caught:
        try:
            raise primary
        finally:
            assert bridge.cleanup_native_sandbox(sandbox, primary_error=sys.exc_info()[1]) is False
    assert caught.value is primary
    assert events[0]["state"] == "cleanup_required"
    assert "private-provider-detail" not in json.dumps(events)


def test_cleanup_expiry_does_not_call_provider_or_publish_success(monkeypatch):
    events = []
    monkeypatch.setattr(bridge, "emit", events.append)
    sandbox = SimpleNamespace(sandbox_id="synthetic-vm", kill=lambda **kw: pytest.fail("late provider call"))
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        bridge.cleanup_native_sandbox(sandbox, deadline=100, clock=lambda: 100)
    assert events[0]["state"] == "cleanup_required"


def test_cleanup_unknown_result_is_not_confirmed(monkeypatch):
    events = []
    monkeypatch.setattr(bridge, "emit", events.append)
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        bridge.cleanup_native_sandbox(SimpleNamespace(sandbox_id="synthetic-vm", kill=lambda **kw: None))
    assert events[0]["state"] == "cleanup_required"


def test_cleanup_broken_event_pipe_cannot_replace_primary(monkeypatch):
    def broken_event(_):
        raise BrokenPipeError("private-event-error")
    def failed_kill(**kw):
        raise RuntimeError("private-provider-error")
    monkeypatch.setattr(bridge, "emit", broken_event)
    primary = ValueError("native_model_unrecovered")
    for kill in (failed_kill, lambda **kw: True):
        with pytest.raises(ValueError) as caught:
            try:
                raise primary
            finally:
                assert bridge.cleanup_native_sandbox(
                    SimpleNamespace(sandbox_id="synthetic-vm", kill=kill),
                    primary_error=sys.exc_info()[1]) is False
        assert caught.value is primary
