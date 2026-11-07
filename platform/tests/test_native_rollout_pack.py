"""Trusted archive closure under filesystem/transport faults; synthetic files only."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
sys.path.insert(0, str(ROOT / "competition-platform/platform/scripts/science"))
import bounded_files
import native_controller
import native_rollout_pack as pack
import native_worker_bridge as bridge
from archive_validation import validate_archive


def fixture_tree(tmp_path):
    source = tmp_path / "rollout"
    source.mkdir()
    (source / "result.json").write_text('{"synthetic":true}')
    return source, tmp_path / "archive.tar.gz"


def test_pack_round_trip_matches_validator_and_publishes_only_completed_job(tmp_path):
    source, target = fixture_tree(tmp_path)
    (source / "agent").mkdir()
    (source / "agent" / ("轨迹" * 40 + ".json")).write_text('{"public-fixture":true}')
    (source / ".control-private").mkdir()
    (source / ".control-private" / "excluded").write_text("not rollout")
    result = pack.pack_job(source, target, "a" * 64)
    raw = target.read_bytes()
    validate_archive(raw)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        assert len(archive.getnames()) == 2
        assert not any(".control-" in name for name in archive.getnames())
        assert json.load(archive.extractfile("result.json")) == {"synthetic": True}
    assert result["status"] == "completed"
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["compressed_size"] == len(raw)
    assert result["gzip_level"] == 1
    assert result["header_count"] == 3  # Unicode long path has one PAX header.
    assert json.loads(Path(str(target) + ".status.json").read_text()) == result
    assert not Path(str(target) + ".partial").exists()


@pytest.mark.parametrize("kind", ["symlink_file", "symlink_directory", "fifo"])
def test_pack_rejects_special_entries_without_publishing_archive(tmp_path, kind):
    source, target = fixture_tree(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "private").write_text("not permitted")
    if kind == "fifo":
        os.mkfifo(source / "bad")
    else:
        (source / "bad").symlink_to(external if kind == "symlink_directory" else external / "private")
    with pytest.raises(ValueError, match="unsafe_entry"):
        pack.pack_job(source, target, "a" * 64)
    assert not target.exists()
    assert not Path(str(target) + ".partial").exists()
    assert json.loads(Path(str(target) + ".status.json").read_text())["failure_code"] == "native_pack_unsafe_entry"


@pytest.mark.parametrize("kind", ["traversal", "physical_headers", "expanded_with_padding", "compressed"])
def test_pack_limits_match_reader_including_metadata_and_padding(tmp_path, kind):
    source, target = fixture_tree(tmp_path)
    limits = {}
    if kind == "traversal":
        for i in range(4):
            (source / f"dir{i}").mkdir()
        limits["entry_limit"] = 3
        expected = "entry_limit"
    elif kind == "physical_headers":
        (source / "result.json").unlink()
        (source / ("长" * 50)).write_text("fixture")
        limits["entry_limit"] = 1
        expected = "entry_limit"
    elif kind == "expanded_with_padding":
        (source / "result.json").write_bytes(b"a" * 19400)
        limits["expanded_limit"] = 20480
        expected = "expanded_limit"
    else:
        limits["compressed_limit"] = 32
        expected = "compressed_limit"
    with pytest.raises(ValueError, match=expected):
        pack.pack_job(source, target, "b" * 64, **limits)
    assert not target.exists()
    assert not Path(str(target) + ".partial").exists()


def test_pack_repeated_launch_cannot_recompress_or_replace_existing_result(tmp_path):
    source, target = fixture_tree(tmp_path)
    result = pack.pack_job(source, target, "c" * 64)
    (source / "result.json").write_text("changed after completion")
    assert pack.pack_job(source, target, "c" * 64) == {"status": "already_started"}
    assert hashlib.sha256(target.read_bytes()).hexdigest() == result["sha256"]


def test_pack_detects_mutation_during_archive_read(tmp_path, monkeypatch):
    source, target = fixture_tree(tmp_path)
    original = pack.BoundedWriter.write
    changed = False
    def write(self, data):
        nonlocal changed
        result = original(self, data)
        if self.code == "native_pack_expanded_limit" and not changed:
            changed = True
            (source / "result.json").write_text("changed and expanded synthetic input")
        return result
    monkeypatch.setattr(pack.BoundedWriter, "write", write)
    with pytest.raises(ValueError, match="file_changed"):
        pack.pack_job(source, target, "d" * 64)
    assert not target.exists()


def test_pack_deadline_and_target_inside_source_fail_closed(tmp_path):
    source, target = fixture_tree(tmp_path)
    with pytest.raises(ValueError, match="deadline"):
        pack.pack_job(source, target, "e" * 64, timeout=-1)
    assert not target.exists()
    with pytest.raises(ValueError, match="invalid_job"):
        pack.pack_job(source, source / "would-pack-itself.tar.gz", "e" * 64)


def completed_marker(root, target):
    return json.dumps({"schema": "qiqc-native-pack-job/v1", "status": "completed",
        "job_id": hashlib.sha256(json.dumps([root, target], separators=(",", ":")).encode()).hexdigest(),
        "sha256": "f" * 64, "compressed_size": 100, "expanded_size": 10240, "header_count": 1}).encode()


def test_remote_launch_timeout_recovers_completion_without_second_launch(monkeypatch):
    root, target = "/trusted/input", "/trusted/output.tar.gz"
    now = [100.0]
    calls, reads = [], []
    TimeoutException = type("TimeoutException", (Exception,), {})
    def command(text, **kw):
        calls.append((text, kw))
        raise TimeoutException("private transport detail")
    def read(files, path, limit, **kw):
        reads.append(kw)
        return None if len(reads) < 3 else completed_marker(root, target)
    monkeypatch.setattr(bounded_files, "read_remote_bytes", read)
    result = pack.remote_pack(SimpleNamespace(files=object()), command, "/python", "/module", root, target,
                              deadline=110, clock=lambda: now[0], delay=lambda n: now.__setitem__(0, now[0]+n))
    assert result["status"] == "completed"
    assert len(calls) == 1 and len(reads) == 3
    assert all(row["deadline"] <= 110 and row["request_timeout"] <= 2 for row in reads)


def test_remote_uncertain_launch_has_one_shared_deadline_and_no_relaunch(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr(bounded_files, "read_remote_bytes", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="native_pack_deadline"):
        pack.remote_pack(SimpleNamespace(files=object()), lambda *a, **kw: calls.append(kw), "/python", "/module",
                         "/input", "/output", deadline=101, clock=lambda: now[0], delay=lambda n: now.__setitem__(0, now[0]+n))
    assert len(calls) == 1 and now[0] == 101


@pytest.mark.parametrize("change", [{"job_id": "0" * 64}, {"compressed_size": pack.MAX_COMPRESSED+1},
                                   {"header_count": 10001}, {"status": "running"}, {"sha256": "untrusted"}])
def test_remote_completion_marker_must_be_bound_and_bounded(monkeypatch, change):
    value = json.loads(completed_marker("/input", "/output"))
    value.update(change)
    monkeypatch.setattr(bounded_files, "read_remote_bytes", lambda *a, **kw: json.dumps(value).encode())
    with pytest.raises(ValueError, match="invalid_job"):
        pack.remote_pack(SimpleNamespace(files=object()), lambda *a, **kw: None, "/python", "/module",
                         "/input", "/output", deadline=time.monotonic()+5)


def test_command_primary_and_capture_failures_never_overwrite_each_other():
    written = {}
    files = SimpleNamespace(write=lambda path, value, **kw: written.__setitem__(Path(path).name, json.loads(value)))
    TimeoutException = type("TimeoutException", (Exception,), {})
    recorder = bridge.CommandFailureRecorder()
    recorder.record(files, "original command", TimeoutException("private original detail"), phase="packing_rollout")
    recorder.record(files, "recovery command", TimeoutException("private capture detail"), phase="pack_complete_failure_rollout", capture=True)
    recorder.record(files, "another command", RuntimeError("private later detail"), phase="packing_rollout")
    assert written["trusted-command-failure.json"]["command_sha256"] == hashlib.sha256(b"original command").hexdigest()
    assert written["trusted-capture-command-failure.json"]["command_sha256"] == hashlib.sha256(b"recovery command").hexdigest()
    assert recorder.primary["failure_code"] == "native_e2b_timeout"
    assert recorder.capture["phase"] == "pack_complete_failure_rollout"
    assert "private" not in json.dumps(written)


def test_pack_module_is_bound_and_tampering_invalidates_controller(tmp_path):
    project, bins = tmp_path / "project", tmp_path / "bin"
    bins.mkdir()
    for name in native_controller.BINARY_NAMES:
        (bins / name).write_bytes(b"synthetic trusted binary fixture")
    for relative in set(native_controller.REMOTE_FILES.values()) | set(native_controller.HOST_FILES):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    assert native_controller.REMOTE_FILES["native_rollout_pack.py"].endswith("scripts/science/native_rollout_pack.py")
    manifest = native_controller.inventory(project, bins)
    path = tmp_path / "controller.json"
    path.write_text(json.dumps(manifest))
    config = {"project_root": str(project), "binaries": str(bins), "controller_manifest": str(path),
              "controller_sha256": native_controller.digest(manifest)}
    native_controller.verify(config)
    (project / native_controller.REMOTE_FILES["native_rollout_pack.py"]).write_text("changed")
    with pytest.raises(ValueError, match="controller_changed"):
        native_controller.verify(config)


@pytest.mark.parametrize("spool_fails,cleanup_fails", [(False, False), (True, False), (True, True)])
def test_controller_uncertain_pack_never_starts_recovery_pack_or_loses_primary(tmp_path, monkeypatch, spool_fails, cleanup_fails):
    """Drive the real controller's terminal path with local fake dependencies."""
    import e2b
    from contextlib import nullcontext
    events, writes, pack_calls, spools = [], {}, [], []
    candidate = {"model_id": "tencent/hy4-preview", "diagnostic_model": "", "acceptance_scope": "bounded-five-minute"}
    receipt = {"candidate_identity": candidate, "model_id": candidate["model_id"], "diagnostic_model": "",
               "execution_duration_class": "budget_300s_observed", "reward": 0, "runtime_seconds": 300.1}
    request = {"run_id": "synthetic-run", "sha256": "a"*64, "science_release_id": "fixture", "model_token": "fixture-scope",
               "lease": {"run_id": "synthetic-run", "attempt": 1}, "data_root": str(tmp_path)}
    config = {"source_root": str(tmp_path), "source_audit": str(tmp_path/"audit"), "data": str(tmp_path),
              "remote_python": "/fixture/python", "remote_source_root": "/fixture/source", "template_id": "fixture",
              "candidate_id": "fixture", "controller_sha256": "b"*64}
    budget = {"account_window_supported": True, "sandbox_ttl_seconds": 600, "provisioning_setup_seconds": 60, "runner_deadline_seconds": 600}
    def commands(text, **kwargs):
        stdout = '{"host_output_reject_verified":true}' if "native_host_network.py" in text else '{}'
        return SimpleNamespace(stdout=stdout, exit_code=0)
    def kill(**kwargs):
        if cleanup_fails:
            raise RuntimeError("private cleanup error")
        return True
    sandbox = SimpleNamespace(sandbox_id="fixture-sandbox", commands=SimpleNamespace(run=commands), kill=kill,
                              files=SimpleNamespace(write=lambda path,value,**kw: writes.__setitem__(path,value)))
    create_options = []
    def create(**kwargs):
        create_options.append(kwargs)
        return sandbox
    monkeypatch.setattr(e2b, "Sandbox", SimpleNamespace(create=create))
    monkeypatch.setattr(bridge, "validate_request", lambda *a: (0, b"opaque synthetic zip"))
    monkeypatch.setattr(bridge, "identity", lambda *a: candidate)
    monkeypatch.setattr(bridge, "verify_controller", lambda *a: {"remote_files": {}})
    monkeypatch.setattr(bridge, "build_plan", lambda *a: {})
    monkeypatch.setattr(bridge, "execution_budget", lambda *a,**kw: budget)
    monkeypatch.setattr(bridge, "create_slot", lambda *a: nullcontext())
    monkeypatch.setattr(bridge, "load_key", lambda: "synthetic-key")
    monkeypatch.setattr(bridge, "emit", events.append)
    monkeypatch.setattr(bridge, "validate_host_output_guard", lambda *a: [])
    monkeypatch.setattr(bridge, "validate_receipt", lambda *a,**kw: None)
    monkeypatch.setattr(bridge, "validate_progress", lambda *a: True)
    monkeypatch.setattr(bridge, "read_budget_stop", lambda *a: None)
    monkeypatch.setattr(bridge, "STOPPED", False)
    monkeypatch.setattr(bridge, "poll_model_mailbox", lambda sandbox,seen,model_events,*a,**kw: model_events.append({"synthetic": True}))
    monkeypatch.setattr(bridge, "native_recovery_summary", lambda *a,**kw: {"all_failures_recovered": True})
    def read(files, path, *args, **kwargs):
        if path.endswith("sdk-after-guard.txt"):
            return b"qiqc-sdk-roundtrip"
        if path.endswith("native-receipt.json"):
            return json.dumps(receipt).encode()
        if path.endswith("runner.exit"):
            return b"0"
        if path.endswith("qsim-progress.json") or "build-" in path:
            return b"{}"
        return None
    monkeypatch.setattr(bridge, "read_remote_bytes", read)
    monkeypatch.setattr(bridge, "read_remote_bytes_retry", read)
    def pack_remote(*args, **kwargs):
        pack_calls.append((args[-1], kwargs["deadline"]))
        raise ValueError("native_pack_deadline")
    monkeypatch.setattr(bridge, "remote_pack", pack_remote)
    def spool(request, data):
        spools.append(data)
        if spool_fails:
            raise OSError("private disk failure")
    monkeypatch.setattr(bridge, "spool_failure_archive", spool)
    with pytest.raises(ValueError, match="^native_pack_deadline$"):
        bridge.run(request, config, {"competition_task_ids": ["synthetic-task"]})
    assert len(pack_calls) == 1 and pack_calls[0][0] == "/tmp/qiqc-native-rollout.tar.gz"
    assert len(create_options) == 1
    assert create_options[0]["http_version"] == "1.1"
    assert len(spools) == 1
    with tarfile.open(fileobj=io.BytesIO(spools[0]), mode="r:gz") as archive:
        failure = json.load(archive.extractfile("model-failure-evidence.json"))
        completeness = json.load(archive.extractfile("diagnostic-completeness.json"))
    assert failure["failure_code"] == "native_pack_deadline"
    assert failure["failure_phase"] == "packing_rollout"
    assert completeness["capture_error_code"] == "native_pack_primary_job_unconfirmed"
    assert completeness["complete_rollout"] is False
    assert not any(event.get("type") == "result" for event in events)
    assert "private" not in json.dumps(events)
    assert any(event.get("state") == ("cleanup_required" if cleanup_fails else "destroyed") for event in events)
