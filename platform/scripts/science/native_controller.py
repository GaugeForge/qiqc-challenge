"""Immutable candidate execution inputs. Pure file metadata; no cloud calls.

Remote uploads are explicit organizer modules, never an entire legacy tree.
Host relay/model contracts and Go binaries join the same digest so 49 records
cannot silently combine different controller or gateway implementations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCIENCE = "competition-platform/platform/scripts/science/"
PROBE = "benchmark-assets/adapters/science-probe/"
REMOTE_FILES = {
    name: SCIENCE + name
    for name in (
        "native_compose_runner.py",
        "native_acceptance_stop.py",
        "native_compose_agent.py",
        "native_runtime_bundles.py",
        "native_compose_environment.py",
        "native_compose_contract.py",
        "native_compose_observation.py",
        "native_contract.py",
        "native_provider_profile.py",
        "native_rollout_pack.py",
        "native_security.py",
        "native_host_network.py",
        "native_final_containers.py",
        "native_build_receipt.py",
        "verify_restored_benchmark.py",
        "verifier_catalog_probe.py",
    )
}
REMOTE_FILES["relay_sandbox.py"] = PROBE + "relay_sandbox.py"
# DeepSeek's sdk-jsonrpc-server plugin declares this protocol package only as an
# undeclared runtime import. Ship it as an explicit, digest-bound compatibility
# overlay so older runtime bundles and future partial builds keep working.
REMOTE_FILES["dsh-sdk-protocol.tgz"] = PROBE + "dsh-sdk-protocol-0.2.1-rc.2.tgz"
REMOTE_FILES["harness_archive.py"] = "competition-platform/platform/qiqc/archive.py"
REMOTE_FILES["native_public_view.py"] = SCIENCE + "native_public_view.py"
REMOTE_FILES["public_view.py"] = SCIENCE + "public_view.py"
HOST_FILES = [
    PROBE + name
    for name in (
        "native_worker_bridge.py",
        "native_model_mailbox.py",
        "native_model_http.py",
        "native_readiness.py",
        "bounded_files.py",
        "model_request_contract.py",
        "model_response_contract.py",
    )
] + [
    SCIENCE + "native_candidate.py",
    SCIENCE + "native_execution_grant.py",
    SCIENCE + "native_release_record.py",
    SCIENCE + "native_controller.py",
    SCIENCE + "verify_native_candidate_evidence.py",
    SCIENCE + "verify_cloud_relay_evidence.py",
    SCIENCE + "archive_validation.py",
    SCIENCE + "native_freeze_gate.py",
    "competition-platform/platform/qiqc/archive.py",
]
BINARY_NAMES = ("api", "worker", "gateway", "review")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("controller_requires_regular_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(project, binaries, *, upstream_ip_family="auto", agent_budget_seconds=300, model_max_concurrent=49):
    if type(agent_budget_seconds) is not int or agent_budget_seconds not in (180, 300):
        raise ValueError("invalid_agent_budget_seconds")
    if upstream_ip_family not in ("auto", "ipv4", "ipv6"):
        raise ValueError("invalid_upstream_ip_family")
    if type(model_max_concurrent) is not int or not 1 <= model_max_concurrent <= 10000:
        raise ValueError('invalid_model_concurrency')
    root = Path(project)
    bins = Path(binaries)
    remote_files = dict(REMOTE_FILES)
    from native_runtime_bundles import RELATIVE_MANIFEST, read_manifest
    if (root / RELATIVE_MANIFEST).exists():
        read_manifest(root / RELATIVE_MANIFEST)
        remote_files["native-runtime-bundles.json"] = RELATIVE_MANIFEST
    return {
        "schema": "qiqc-native-controller/v1",
        **({"native_agent_budget_seconds": 180} if agent_budget_seconds == 180 else {}),
        "native_e2b_transport_policy": {"http_version": "1.1"},
        "native_model_gateway_policy": {
            "upstream_ip_family": upstream_ip_family,
            "max_concurrent": model_max_concurrent,
            "allowed_free_diagnostic_model": "minimax/minimax-m3.1-flash:free",
            "free_diagnostic_shared_limits": "durable-platform-db-rolling-minute-utc-day/v1",
            "free_diagnostic_paid_fallback": False,
            "upstream_transport": "http1-per-request-connection/v1",
            "upstream_max_connections_per_host": 49,
            "diagnostic_continuous_limit_seconds": 3600,
            "request_timeout_seconds": 360,
            "usage_record_timeout_seconds": 35,
            "server_write_timeout_seconds": 405,
            "http_client_reply_timeout_seconds": 425,
            "relay_reply_timeout_seconds": 435,
        },
        "remote_files": {
            name: {"source": relative, "sha256": file_hash(root / relative)}
            for name, relative in sorted(remote_files.items())
        },
        "host_files": {
            relative: file_hash(root / relative) for relative in sorted(HOST_FILES)
        },
        "binaries": {name: file_hash(bins / name) for name in BINARY_NAMES},
    }


def verify(config):
    expected = json.loads(Path(config["controller_manifest"]).read_text())
    if (
        digest(expected) != config["controller_sha256"]
        or inventory(config["project_root"], config["binaries"],
                     upstream_ip_family=config.get("upstream_ip_family", "auto"),
                     agent_budget_seconds=config.get("agent_budget_seconds", 300),
                     model_max_concurrent=config.get('model_max_concurrent', 49)) != expected
    ):
        raise ValueError("candidate_controller_changed")
    return expected
