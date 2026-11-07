"""Candidate identity isolation and evidence gate tests. No cloud/science runs."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCIENCE = Path(__file__).resolve().parents[1] / "scripts/science"
sys.path.insert(0, str(SCIENCE))
sys.path.insert(0, str(SCIENCE.parents[3] / "benchmark-assets/adapters/science-probe"))
from native_candidate import commands, identity, load_candidate
from native_freeze_gate import assess
from native_worker_bridge import stage_archive, validate_receipt, validate_request


def test_roster_bound_candidate_preserves_legacy_and_rejects_relabeling(tmp_path):
    from native_candidate import candidate_id_for_audit, IDENTITY_SCHEMA
    value, path = config(tmp_path)
    old = value["candidate_id"]
    audit = json.loads(Path(value["source_audit"]).read_text())
    value["candidate_identity_schema"] = IDENTITY_SCHEMA
    value["candidate_id"] = candidate_id_for_audit(audit, value["source_audit_sha256"])
    assert value["candidate_id"] != old
    path.write_text(json.dumps(value))
    assert load_candidate(path, verify_source=False)[0]["candidate_id"] == value["candidate_id"]
    audit["competition_task_ids"] = ["changed_roster"]
    Path(value["source_audit"]).write_text(json.dumps(audit))
    value["source_audit_sha256"] = hashlib.sha256(Path(value["source_audit"]).read_bytes()).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Candidate identity invalid"):
        load_candidate(path, verify_source=False)
    assert candidate_id_for_audit(audit, value["source_audit_sha256"]) != value["candidate_id"]
    assert candidate_id_for_audit(audit, value["source_audit_sha256"], None) == old


def config(tmp_path):
    audit = {"source_files_sha256": "a" * 64, "source_commit": "b" * 40}
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit))
    root = tmp_path / "candidate"
    root.mkdir()
    value = {
        "schema": "qiqc-native-candidate/v1",
        "source_root": str(tmp_path / "source"),
        "template_id": "native-template-fixture",
        "frozen": False,
        "candidate_root": str(root),
        "candidate_id": "native-candidate-" + "a" * 64,
        "source_files_sha256": "a" * 64,
        "source_commit": "b" * 40,
        "source_audit": str(audit_path),
        "source_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "data": str(root / "data"),
        "credentials": str(root / "credentials.json"),
        "binaries": str(root / "bin"),
        "url": "http://127.0.0.1:19890",
        "gateway_url": "http://127.0.0.1:19889",
        "project_root": str(tmp_path),
        "python": "/usr/bin/python3",
        "adapters": str(root / "adapters"),
        "adapter_manifest": str(root / "manifest.json"),
        "provider_env": str(root / "provider.env"),
        "review_key": str(root / "review.key"),
    }
    from native_controller import (
        BINARY_NAMES,
        HOST_FILES,
        REMOTE_FILES,
        digest,
        inventory,
    )

    for relative in set(REMOTE_FILES.values()) | set(HOST_FILES):
        fixture = tmp_path / relative
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("organizer fixture " + relative)
    bins = Path(value["binaries"])
    bins.mkdir()
    for name in BINARY_NAMES:
        (bins / name).write_text("binary fixture " + name)
    controller = inventory(tmp_path, bins)
    controller_path = root / "controller.json"
    controller_path.write_text(json.dumps(controller))
    value.update(
        controller_manifest=str(controller_path),
        controller_sha256=digest(controller),
        build_plan_sha256="e" * 64,
        build_receipt_sha256="f" * 64,
    )
    path = root / "candidate.json"
    path.write_text(json.dumps(value))
    return value, path


@pytest.mark.parametrize('family', ['auto', 'ipv4', 'ipv6'])
def test_gateway_address_family_is_explicit_and_bound_to_controller(tmp_path, family):
    from native_controller import digest, inventory

    value, path = config(tmp_path)
    assert commands(value)['gateway'][-2:] == ['-upstream-ip-family', 'auto']
    original = value['controller_sha256']
    value['upstream_ip_family'] = family
    assert commands(value)['gateway'][-2:] == ['-upstream-ip-family', family]
    assert '-upstream-ip-family' not in commands(value)['worker']
    assert '-upstream-ip-family' not in commands(value)['review']
    path.write_text(json.dumps(value))
    if family != 'auto':
        with pytest.raises(ValueError, match='candidate_controller_changed'):
            load_candidate(path, verify_source=False)
    manifest = inventory(value['project_root'], value['binaries'], upstream_ip_family=family)
    assert manifest['native_model_gateway_policy']['upstream_ip_family'] == family
    value['controller_sha256'] = digest(manifest)
    assert (value['controller_sha256'] == original) == (family == 'auto')
    Path(value['controller_manifest']).write_text(json.dumps(manifest))
    path.write_text(json.dumps(value))
    loaded, _ = load_candidate(path, verify_source=False)
    assert loaded['upstream_ip_family'] == family
    assert identity(loaded, 0)['controller_sha256'] == value['controller_sha256']


@pytest.mark.parametrize('family', ['', None, 'tcp4', 'IPv4', 'private-secret', 'ipv4\nprivate-secret'])
def test_invalid_gateway_address_family_rejected_without_echo(tmp_path, family):
    from native_controller import inventory

    value, path = config(tmp_path)
    value['upstream_ip_family'] = family
    path.write_text(json.dumps(value))
    for operation in (lambda: load_candidate(path, verify_source=False),
                      lambda: commands(value),
                      lambda: inventory(value['project_root'], value['binaries'], upstream_ip_family=family)):
        with pytest.raises(ValueError) as error:
            operation()
        assert str(error.value) == 'invalid_upstream_ip_family'


def test_candidate_cannot_reuse_staging_ports_paths_or_changed_audit(tmp_path):
    value, path = config(tmp_path)
    assert load_candidate(path, verify_source=False)[0] == value
    for key, bad in [
        ("url", "http://127.0.0.1:18890"),
        ("gateway_url", value["url"]),
        ("data", "/var/lib/qiqc-staging/data"),
        ("candidate_id", "native-candidate-short"),
        ("frozen", True),
    ]:
        path.write_text(json.dumps({**value, key: bad}))
        with pytest.raises(ValueError):
            load_candidate(path, verify_source=False)
    path.write_text(json.dumps(value))
    Path(value["source_audit"]).write_text("{}")
    with pytest.raises(ValueError, match="audit changed"):
        load_candidate(path, verify_source=False)


def test_candidate_worker_requires_exact_upload_source_and_own_data(tmp_path):
    value, _ = config(tmp_path)
    uploads = Path(value["data"]) / "uploads"
    uploads.mkdir(parents=True)
    raw = b"opaque uploaded zip"
    sha = hashlib.sha256(raw).hexdigest()
    path = uploads / (sha + ".zip")
    path.write_bytes(raw)
    Path(value["adapter_manifest"]).write_text(json.dumps({"codex": {"sha256": sha}}))
    request = {
        "science_release_id": value["candidate_id"],
        "agent_budget_seconds": 300,
        "science_task_index": 0,
        "gateway_url": value["gateway_url"],
        "data_root": value["data"],
        "run_id": "run1",
        "submission_path": str(path),
        "sha256": sha,
    }
    assert validate_request(request, value) == (0, raw)
    for key, bad in [
        ("science_release_id", "qiqc49-20261020-v1"),
        ("science_task_index", 49),
        ("data_root", "/var/lib/qiqc-staging/data"),
        ("gateway_url", "http://127.0.0.1:18889"),
    ]:
        with pytest.raises(ValueError):
            validate_request({**request, key: bad}, value)
    path.write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="sha_mismatch"):
        validate_request(request, value)
    assert commands(value)["worker"][-2:] == ["-model-gateway", value["gateway_url"]]
    assert (
        "--science-bridge" not in commands(value)["worker"]
    )  # Go single dash spelling
    assert "-science-bridge" in commands(value)["worker"]


def test_native_receipt_cannot_turn_adapter_failure_into_scientific_zero():
    request = {"run_id": "run", "sha256": "a" * 64}
    good = {
        "run_id": "run",
        "submission_sha256": "a" * 64,
        "infrastructure_ok": True,
        "standard_harbor_rollout": True,
        "reward": 0,
        "agent_budget_seconds": 300,
        "agent_exit": 124,
        "budget_exhausted": True,
        "agent_stop_reason": "budget_exhausted",
        "verifier_exit": 0,
        "runtime_seconds": 300,
    }
    validate_receipt(good, request)
    for changes in (
        {"agent_exit": 1},
        {"agent_stop_reason": "model_failed"},
        {"verifier_exit": 1},
        {"infrastructure_ok": False},
        {"submission_sha256": "b" * 64},
        {"runtime_seconds": float("nan")},
    ):
        with pytest.raises(ValueError):
            validate_receipt({**good, **changes}, request)


def write_pair(root, value, index, *, parity=True):
    browser = {
        "status": "passed",
        "catalog_index": index,
        "benchmark_release": identity(value, index),
        "batch_id": "batch" + str(index),
        "r2_archive_sha256": "b" * 64,
        "submission_sha256": "c" * 64,
    }
    for key in (
        "admin_target_bound_via_browser",
        "invitation_login_password_change",
        "result_visible_in_browser",
        "own_hidden_trace_denied",
        "other_identity_trace_denied",
        "r2_index_written",
        "all_execution_sandboxes_destroyed",
        "local_rollout_cache_removed",
        "nonranking",
    ):
        browser[key] = True
    path = root / f"task-{index:02}.json"
    path.write_text(json.dumps(browser))
    evidence = {
        "schema": "qiqc-native-candidate-evidence/v1",
        "status": "passed",
        "catalog_index": index,
        "benchmark_release": identity(value, index),
        "batch_id": browser["batch_id"],
        "archive_sha256": browser["r2_archive_sha256"],
        "submission_sha256": browser["submission_sha256"],
        "browser_report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_runtime_parity_verified": parity,
        "acceptance_scope": "bounded-five-minute",
        "source_runtime_software_verified": True,
        "original_container_limits_verified": True,
        "full_host_capacity_verified": True,
        "bounded_functional_errors": [],
        "observation_policy": "observe-300s-no-readiness-stop/v1",
        "execution_duration_class": "budget_300s_observed",
        "agent_elapsed_seconds": 300.2,
        "harbor_agent_timing_verified": True,
        "terminal_runtime_verified": True,
        "terminal_runtime_sha256": "e"*64,
        "agent_execution_started_at": "2026-10-20T13:00:00+00:00",
        "agent_execution_finished_at": "2026-10-20T13:05:00.200000+00:00",
        "agent_budget_seconds": 300,
        "agent_exit": 124,
        "qsim_successful_interaction_verified": True,
        "qsim_progress_sha256": "d" * 64,
        "reward": 0,
        "agent_stop_reason": "budget_exhausted",
        "budget_exhausted": True,
        "acceptance_early_stop_verified": False,
        "acceptance_readiness_sha256": None,
        "snapshot_build_inputs_verified": True,
        "controller_sha256": value["controller_sha256"],
        "model_response_evidence": {
            "complete_vs_incomplete_distinction_verified": True,
            "gateway_request_link_verified": True,
        },
    }
    for key in (
        "actual_uploaded_zip_executed",
        "native_compose_execution_verified",
        "private_network_verified",
        "standard_harbor_rollout",
    ):
        evidence[key] = True
    evidence_path = root / f"task-{index:02}-r2.json"
    evidence_path.write_text(json.dumps(evidence))
    return path, evidence_path


def test_freeze_requires_all49_same_native_source_and_parity_not_just_labels(tmp_path):
    value, _ = config(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for i in range(49):
        write_pair(evidence, value, i)
    assert assess(value, evidence)["status"] == "eligible_for_explicit_freeze"
    _, report = write_pair(evidence, value, 7, parity=False)
    result = assess(value, evidence)
    assert result["status"] == "blocked" and result["passed_tasks"] == 48
    # status remains 'passed', but absence of runtime parity still blocks freeze.
    assert (
        "source_runtime_parity_claim_inconsistent"
        in result["rows"][7]["attempts"][0]["errors"]
    )
    path, _ = write_pair(evidence, value, 7)
    raw = json.loads(path.read_text())
    raw["batch_id"] = "altered"
    path.write_text(json.dumps(raw))
    assert assess(value, evidence)["passed_tasks"] == 48
    raw = json.loads(report.read_text())
    raw["benchmark_release"]["source_files_sha256"] = "d" * 64
    report.write_text(json.dumps(raw))
    assert assess(value, evidence)["passed_tasks"] == 48
    assert result["frozen"] is False and result["platform_switched"] is False


def test_staged_archive_matches_go_durable_upload_contract(tmp_path):
    request = {"data_root": str(tmp_path), "lease": {"run_id": "run1", "attempt": 1}}
    result = stage_archive(
        request, b"archive bytes", {"reward": 0.0, "runtime_seconds": 1.25}
    )
    pending = json.loads((tmp_path / "cache/run1/1/staged.json").read_text())
    assert pending["seconds"] == "1.250000" and type(pending["reward"]) is int
    assert (
        pending["bundle_sha256"]
        == result["sha256"]
        == hashlib.sha256(b"archive bytes").hexdigest()
    )
    assert result["pending_upload"] is True and result["readback_verified"] is False


def test_r2_inspection_checks_executed_zip_and_actual_reward_not_report_label(
    tmp_path, monkeypatch
):
    import io
    import tarfile

    import verify_native_candidate_evidence as verifier

    value, _ = config(tmp_path)
    expected = identity(value, 0)
    receipt = {
        "run_id": "run",
        "submission_sha256": "c" * 64,
        "infrastructure_ok": True,
        "standard_harbor_rollout": True,
        "reward": 0,
        "agent_budget_seconds": 300,
        "agent_exit": 124,
        "observation_policy": "observe-300s-no-readiness-stop/v1",
        "execution_duration_class": "budget_300s_observed",
        "budget_exhausted": True,
        "agent_stop_reason": "budget_exhausted",
        "verifier_exit": 0,
        "runtime_seconds": 300.2,
        "candidate_identity": expected,
        "controller_sha256": value["controller_sha256"],
        "build_input_digests": {
            "build_plan_sha256": value["build_plan_sha256"],
            "build_receipt_sha256": value["build_receipt_sha256"],
        },
        "execution_host_network": {
            "allow_public_traffic": False,
            "deny_out": ["0.0.0.0/0"],
            "external_tcp_denied": True,
            "probe_count": 2,
        },
        "security_evidence": {
            "agent": {"firewall": {"passed": True}},
            "verifier": {"network_mode": "none"},
        },
    }
    from test_native_host_network import fixture as host_guard_fixture

    receipt["host_output_guard"] = {
        **host_guard_fixture(),
        "host_output_reject_verified": True,
    }
    receipt["guard_sdk_roundtrip_verified"] = True
    result = {
        "agent_execution": {"started_at": "2026-10-20T00:00:00+00:00", "finished_at": "2026-10-20T00:05:00.2+00:00"},
        "agent_result": {
            "metadata": {
                "submission_sha256": "c" * 64,
                "execution_kind": "uploaded-native-zip",
            }
        },
        "verifier_result": {"rewards": {"reward": 0}},
    }
    event = {
        "status": 200,
        "response_contract": {
            "schema": "qiqc-model-response/v2",
            "success": True,
            "terminal": True,
            "category": "completed",
        },
        "gateway_request_id": "1" * 32,
        "request_correlation": "1" * 32,
        "adapter_request_sha256": "d" * 64,
        "forwarded_request_sha256": "d" * 64,
    }
    containers = {role: {"Id": letter*64, "Image": "sha256:"+letter*64,
                         "RestartCount": 0,
                         "State": {"OOMKilled": False, "Dead": False, "Restarting": False,
                                   "Error": "", "Running": role != "main",
                                   "Status": "exited" if role == "main" else "running"}}
                  for role, letter in (("main", "b"), ("qsim", "a"), ("verifier", "c"))}
    receipt["runtime_observation"] = {"container_inspects": containers}
    receipt["security_evidence"]["main_stopped_before_verifier"] = True
    receipt["security_evidence"]["main_before_planned_stop"] = {**containers["main"], "State": {**containers["main"]["State"], "Running": True, "Status": "running"}}
    receipt["final_container_evidence"] = {
        "schema": "qiqc-native-final-containers/v1", "containers": containers,
        "observed_before_cleanup": {role: True for role in containers},
        "collected_at": {role: 1000.0 for role in containers}, "capture_errors": [], "verified": True}
    qsim = {
        "schema": "qiqc-native-qsim-progress/v1",
        "run_id": "run",
        "attempt": 1,
        "submission_sha256": "c" * 64,
        "candidate_identity": expected,
        "qsim_container_id": "a" * 64,
        "success_count": 1,
        "event_sha256": "b" * 64,
        "log_prefix_sha256": "e" * 64,
        "log_bytes": 100,
        "observed_at": 1000.0,
    }
    files = {
        "qsim-progress.json": json.dumps(qsim).encode(),
        "result.json": json.dumps(result).encode(),
        "native-receipt.json": json.dumps(receipt).encode(),
        "controller-manifest.json": Path(value["controller_manifest"]).read_bytes(),
        "benchmark-release.json": json.dumps(expected).encode(),
        "model-evidence.json": json.dumps({"events": [event]}).encode(),
        "verifier/reward.txt": b"0",
        "verifier/test-stdout.txt": b"scientific verifier completed",
    }

    def archive():
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            for name, data in files.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
        return output.getvalue()

    monkeypatch.setattr(verifier, "build_plan", lambda *args: {})
    monkeypatch.setattr(
        verifier,
        "validate_security_receipt",
        lambda value: [] if value.get("passed") is True else ["missing"],
    )
    monkeypatch.setattr(
        verifier,
        "runtime_verification",
        lambda *args: {
            "source_runtime_software_verified": True,
            "original_container_limits_verified": True,
            "full_host_capacity_verified": False,
            "source_runtime_parity_verified": False,
            "bounded_functional_errors": [],
            "full_host_capacity_errors": ["outer_sandbox_below_task_memory"],
        },
    )
    browser = {
        "catalog_index": 0,
        "benchmark_release": expected,
        "execution_topology": "e2b-native-compose",
    }
    index = {
        "batch_id": "batch",
        "run_id": "run",
        "attempt": 1,
        "sha256": "",
        "submission_sha256": "c" * 64,
        "reward": 0,
    }

    def inspect():
        raw = archive()
        index["sha256"] = hashlib.sha256(raw).hexdigest()
        return verifier.inspect(
            raw,
            index,
            browser,
            value,
            {"competition_task_ids": [str(i) for i in range(49)]},
            [
                {
                    "request_id": "1" * 32,
                    "request_correlation": "1" * 32,
                    "upstream_attempt": 1,
                    "status": 200,
                }
            ],
        )

    checked = inspect()
    assert (
        checked["status"] == "passed"
        and checked["source_runtime_parity_verified"] is False
    )
    receipt["guard_sdk_roundtrip_verified"] = False
    files["native-receipt.json"] = json.dumps(receipt).encode()
    with pytest.raises(verifier.EvidenceError, match="host_output_guard_not_verified"):
        inspect()
    receipt["guard_sdk_roundtrip_verified"] = True
    files["native-receipt.json"] = json.dumps(receipt).encode()
    files["verifier/reward.txt"] = b"1"
    with pytest.raises(verifier.EvidenceError, match="score_mismatch"):
        inspect()
    files["verifier/reward.txt"] = b"0"
    result["agent_result"]["metadata"]["submission_sha256"] = "f" * 64
    files["result.json"] = json.dumps(result).encode()
    with pytest.raises(verifier.EvidenceError, match="uploaded_execution_not_proven"):
        inspect()


@pytest.mark.parametrize("reachable", [False, True])
def test_host_egress_probe_requires_actual_failed_connections(monkeypatch, reachable):
    import socket

    from native_worker_bridge import REMOTE_NETWORK_PROBE

    class Connection:
        def close(self):
            pass

    def connect(*args, **kwargs):
        if reachable:
            return Connection()
        raise TimeoutError("test-only denied transport")

    monkeypatch.setattr(socket, "create_connection", connect)
    if reachable:
        with pytest.raises(AssertionError):
            exec(REMOTE_NETWORK_PROBE, {})  # noqa: S102 — fixed organizer probe; network mocked.
    else:
        exec(REMOTE_NETWORK_PROBE, {})  # noqa: S102 — fixed organizer probe; network mocked.


@pytest.mark.parametrize(
    "relative",
    [
        "benchmark-assets/adapters/science-probe/native_model_mailbox.py",
        "competition-platform/platform/scripts/science/native_security.py",
    ],
)
def test_candidate_controller_change_fails_before_cloud(tmp_path, relative):
    value, path = config(tmp_path)
    changed = Path(value["project_root"]) / relative
    changed.write_text("different execution code")
    with pytest.raises(ValueError, match="controller_changed"):
        load_candidate(path, verify_source=False)


def test_gate_cannot_mix_49_old_controller_passes_with_current_controller(tmp_path):
    value, _ = config(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for i in range(49):
        write_pair(evidence, value, i)
    changed = {**value, "controller_sha256": "9" * 64}
    result = assess(changed, evidence)
    assert result["passed_tasks"] == 0 and result["status"] == "blocked"
    assert "controller_version_mismatch" in result["rows"][0]["attempts"][0]["errors"]


def test_native_mailbox_preserves_adapter_parameters_and_terminal_contract(monkeypatch):
    import native_model_mailbox as mailbox

    body = {
        "model": "tencent/hy4-preview",
        "max_output_tokens": 16000,
        "reasoning": {"effort": "high"},
        "input": [],
    }
    request = {"id": "a" * 32, "path": "/v1/responses", "body": body}
    monkeypatch.setattr(
        mailbox, "read_main_bytes", lambda *args, **kwargs: json.dumps(request).encode()
    )

    class Files:
        def write(self, *args, **kwargs):
            pass

        def rename(self, *args, **kwargs):
            pass

    class Sandbox:
        files = Files()

    calls = []

    def model_call(endpoint, received, token):
        calls.append((endpoint, received, token))
        return {
            "status": 200,
            "type": "application/json",
            "body": json.dumps(
                {"status": "completed", "object": "response", "output": []}
            ).encode(),
        }

    events = []
    assert mailbox.poll_model_mailbox(
        Sandbox(), {}, events, model_call, "scoped", catalog_diagnostic=True
    )
    assert calls == [("/v1/responses", body, "scoped")]
    assert events[0]["adapter_request_sha256"] == events[0]["forwarded_request_sha256"]
    assert events[0]["response_contract"]["schema"] == "qiqc-model-response/v2"


def test_native_create_slot_serializes_independent_callers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from native_worker_bridge import create_slot

    def create():
        with create_slot(tmp_path / "science", interval_seconds=0.04):
            return time.monotonic()

    with ThreadPoolExecutor(max_workers=3) as pool:
        starts = sorted(pool.map(lambda _: create(), range(3)))
    assert all(right - left >= 0.035 for left, right in zip(starts, starts[1:]))
    path = tmp_path / "science/native-create.lock"
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        with create_slot(tmp_path / "science", interval_seconds=0.01):
            pytest.fail("symlinked coordination file accepted")


def test_five_minute_receipt_rejects_early_or_naturally_short_execution():
    request = {"run_id": "run", "sha256": "a" * 64}
    good = {"run_id": "run", "submission_sha256": "a" * 64,
            "candidate_identity": {"acceptance_scope": "bounded-five-minute"},
            "infrastructure_ok": True, "standard_harbor_rollout": True,
            "reward": 0, "agent_budget_seconds": 300, "agent_exit": 124,
            "budget_exhausted": True, "agent_stop_reason": "budget_exhausted",
            "verifier_exit": 0, "runtime_seconds": 300.1,
            "observation_policy": "observe-300s-no-readiness-stop/v1",
            "execution_duration_class": "budget_300s_observed"}
    validate_receipt(good, request)
    for change in ({"runtime_seconds": 30}, {"runtime_seconds": float("nan")},
                   {"execution_duration_class": "natural_completion_before_budget"},
                   {"agent_exit": 0}, {"observation_policy": None},
                   {"acceptance_early_stop_verified": True}):
        with pytest.raises(ValueError, match="five_minute"):
            validate_receipt({**good, **change}, request)


def test_harbor_real_agent_timing_rejects_naive_or_artificial_duration():
    from verify_native_candidate_evidence import verified_agent_timing, EvidenceError
    result = {"agent_execution": {"started_at": "2026-10-20T00:00:00Z", "finished_at": "2026-10-20T00:05:00Z"}}
    assert verified_agent_timing(result, {"runtime_seconds": 300.1})["harbor_agent_timing_verified"]
    for end in ("2026-10-20T00:01:00Z", "2026-10-20T00:05:00", None):
        with pytest.raises(EvidenceError):
            verified_agent_timing({"agent_execution": {**result["agent_execution"], "finished_at": end}}, {"runtime_seconds": 300})


def test_task_slots_are_independent_of_benchmark_task_count(tmp_path):
    value, path = config(tmp_path)
    for slots in (1, 4, 49, 50, 200, 10000):
        path.write_text(json.dumps({**value, "functional_slots": slots}))
        assert load_candidate(path, verify_source=False)[0]["functional_slots"] == slots
    for slots in (0, 10001, True, "49"):
        path.write_text(json.dumps({**value, "functional_slots": slots}))
        with pytest.raises(ValueError, match="functional slots"):
            load_candidate(path, verify_source=False)


def test_model_cap_is_independent_and_bound_by_controller(tmp_path):
    from native_candidate import commands
    from native_controller import inventory, digest
    value, path = config(tmp_path)
    value.update(functional_slots=200, model_max_concurrent=7)
    manifest = inventory(value['project_root'], value['binaries'], model_max_concurrent=7)
    Path(value['controller_manifest']).write_text(json.dumps(manifest))
    value['controller_sha256'] = digest(manifest)
    path.write_text(json.dumps(value))
    loaded, _ = load_candidate(path, verify_source=False)
    argv = commands(loaded)
    assert argv['worker'][argv['worker'].index('-slots') + 1] == '200'
    assert argv['gateway'][argv['gateway'].index('-max-concurrent') + 1] == '7'
    path.write_text(json.dumps({**value, 'model_max_concurrent': 8}))
    with pytest.raises(ValueError, match='candidate_controller_changed'):
        load_candidate(path, verify_source=False)
    for invalid in (0, True, '7', 10001):
        path.write_text(json.dumps({**value, 'model_max_concurrent': invalid}))
        with pytest.raises(ValueError, match='model concurrency'):
            load_candidate(path, verify_source=False)


def test_natural_completion_publishes_science_but_does_not_satisfy_five_minute_gate():
    request = {"run_id": "run", "sha256": "a" * 64}
    value = {"run_id": "run", "submission_sha256": "a" * 64,
             "candidate_identity": {"acceptance_scope": "bounded-five-minute"},
             "infrastructure_ok": True, "standard_harbor_rollout": True,
             "reward": 1, "agent_budget_seconds": 300, "agent_exit": 0,
             "budget_exhausted": False, "agent_stop_reason": "completed",
             "verifier_exit": 0, "runtime_seconds": 121.2,
             "observation_policy": "observe-300s-no-readiness-stop/v1",
             "execution_duration_class": "natural_completion_before_budget"}
    validate_receipt(value, request, allow_natural_completion=True)
    with pytest.raises(ValueError, match="five_minute"):
        validate_receipt(value, request)
    for changes in ({"agent_exit": 1}, {"infrastructure_ok": False},
                    {"execution_duration_class": "budget_300s_observed"},
                    {"verifier_exit": 2}):
        with pytest.raises(ValueError):
            validate_receipt({**value, **changes}, request, allow_natural_completion=True)


def test_partial_failure_archive_survives_total_remote_loss_without_false_success(monkeypatch):
    import io
    import tarfile
    import native_worker_bridge as bridge
    from types import SimpleNamespace
    def unavailable(*args, **kwargs):
        raise RuntimeError("secret-provider-url-must-not-leak")
    monkeypatch.setattr(bridge, "read_remote_bytes", unavailable)
    request = {"run_id": "run", "sha256": "a" * 64}
    data = bridge.fallback_failure_archive(SimpleNamespace(files=object()), request, [],
                                          ValueError("native_model_unrecovered"),
                                          RuntimeError("secret"), None)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        assert set(archive.getnames()) == {"model-failure-evidence.json", "diagnostic-completeness.json"}
        evidence = json.load(archive.extractfile("model-failure-evidence.json"))
        incomplete = json.load(archive.extractfile("diagnostic-completeness.json"))
    assert evidence["failure_code"] == "native_model_unrecovered"
    assert evidence["recovery"]["all_failures_recovered"] is False
    assert incomplete["complete_rollout"] is False
    assert len(incomplete["unavailable_files"]) == 5
    assert "secret" not in json.dumps([evidence, incomplete])


def test_archive_retry_recovers_only_transport_and_preserves_byte_bound(monkeypatch):
    import native_worker_bridge as bridge
    calls, delays = [], []
    def transient(files, path, limit, **kwargs):
        calls.append((path, limit, kwargs))
        if len(calls) < 3:
            raise bridge.RemoteFileError("Remote file transfer failed")
        return b"complete immutable archive"
    monkeypatch.setattr(bridge, "read_remote_bytes", transient)
    assert bridge.read_archive_bytes(object(), "/trusted/archive", 64, expected_sha256=hashlib.sha256(b"complete immutable archive").hexdigest(), delay=delays.append) == b"complete immutable archive"
    assert len(calls) == 3 and delays == [0.5, 1.0]
    assert all(row[0:2] == ("/trusted/archive", 64) and row[2]["user"] == "root" and row[2]["request_timeout"] == 10 for row in calls)
    assert len({row[2]["deadline"] for row in calls}) == 1
    for reason in ("Remote file missing", "Remote file rejected", "Remote file exceeds byte limit"):
        calls.clear()
        def refused(*args, **kwargs):
            calls.append(1)
            raise bridge.RemoteFileError(reason)
        monkeypatch.setattr(bridge, "read_remote_bytes", refused)
        with pytest.raises(bridge.RemoteFileError, match=reason):
            bridge.read_archive_bytes(object(), "/trusted/archive", 64, expected_sha256=hashlib.sha256(b"complete immutable archive").hexdigest(), delay=delays.append)
        assert len(calls) == 1


def test_archive_changed_bytes_fail_without_retry(monkeypatch):
    import native_worker_bridge as bridge
    calls = []
    def changed(*args, **kwargs):
        calls.append(1)
        return b"changed after pack"
    monkeypatch.setattr(bridge, "read_remote_bytes", changed)
    with pytest.raises(ValueError, match="native_archive_digest_mismatch"):
        bridge.read_archive_bytes(object(), "/trusted/archive", 64,
                                  expected_sha256=hashlib.sha256(b"original").hexdigest(),
                                  delay=lambda _: pytest.fail("content mismatch retried"))
    assert len(calls) == 1


def test_free_diagnostic_model_is_explicit_and_cannot_mix_official_identity(tmp_path):
    from native_candidate import FREE_DIAGNOSTIC_MODEL, model_id
    from native_release_record import validate_binding
    value, path = config(tmp_path)
    official = identity(value, 0)
    assert official['model_id'] == 'tencent/hy4-preview'
    assert official['diagnostic_model'] == ''
    value['diagnostic_model'] = FREE_DIAGNOSTIC_MODEL
    path.write_text(json.dumps(value))
    assert load_candidate(path, verify_source=False)[0]['diagnostic_model'] == FREE_DIAGNOSTIC_MODEL
    free = identity(value, 0)
    assert free != official and free['model_id'] == FREE_DIAGNOSTIC_MODEL
    for role in ('gateway', 'worker', 'review'):
        args = commands(value)[role]
        assert args[args.index('-diagnostic-model')+1] == FREE_DIAGNOSTIC_MODEL
    with pytest.raises(ValueError, match='binding_mismatch'):
        validate_binding(value, {'payload': {**value, 'diagnostic_model': ''}})
    for invalid in ('other/paid-model', 'minimax/minimax-m3.1-flash', None):
        with pytest.raises(ValueError, match='unsupported_diagnostic_model'):
            model_id({**value, 'diagnostic_model': invalid})


def test_organizer_profile_model_binding_and_file_change_rejected(tmp_path):
    from native_provider_profile import profile_identity
    value,path=config(tmp_path)
    full=json.loads((Path(__file__).resolve().parents[1]/'test-fixtures/provider-profile-canonical.json').read_text())['profile']
    full={**full,'id':'other_vendor','base_url':'https://api.example.com/v1','model':'future-model',
          'billing':'paid','requests_per_minute':100,'requests_per_day':10000}
    profile_path=tmp_path/'approved-profile.json';profile_path.write_text(json.dumps(full));profile_path.chmod(0o640)
    value.update(provider_profile=profile_identity(full),provider_profile_path=str(profile_path))
    path.write_text(json.dumps(value))
    assert load_candidate(path,verify_source=False)[0]['provider_profile']['model']=='future-model'
    assert identity(value,0)['model_id']=='future-model'
    assert '-provider-profile' in commands(value)['worker']
    profile_path.write_text(json.dumps({**full,'base_url':'https://other.example.com/v1'}))
    with pytest.raises(ValueError,match='provider_profile_changed'):
        load_candidate(path,verify_source=False)


def test_stage_and_direct_launcher_model_environment_derive_from_profile():
    import ast
    stage=SCIENCE.parents[2]/'platform-go/deploy/native_candidate_stage.py'
    text=stage.read_text()
    assert 'Environment=QIQC_MODEL={model_id(config)}' in text
    source=ast.parse((SCIENCE/'native_candidate.py').read_text())
    assignments=[node.value for node in ast.walk(source) if isinstance(node,ast.keyword) and node.arg=='QIQC_MODEL']
    assert len(assignments)==1
    assert ast.unparse(assignments[0])=='model_id(config)'
