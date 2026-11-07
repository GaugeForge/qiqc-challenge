#!/usr/bin/env python3
"""Candidate identity and isolated local process commands. Never freezes/deploys.

prepare verifies original source and writes a new immutable candidate config.
run-role executes one selected trusted local service with candidate-only data;
no cloud task is created until an operator submits a browser evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from verify_restored_benchmark import verify

SCHEMA = "qiqc-native-candidate/v1"
IDENTITY_SCHEMA = "qiqc-native-candidate/source-audit-v2"
FREE_DIAGNOSTIC_MODEL = "minimax/minimax-m3.1-flash:free"
OFFICIAL_MODEL = "tencent/hy4-preview"


def upstream_ip_family(config):
    selected = config.get("upstream_ip_family", "auto")
    if selected not in ("auto", "ipv4", "ipv6"):
        raise ValueError("invalid_upstream_ip_family")
    return selected


def model_id(config):
    selected = config.get("diagnostic_model", "")
    if selected not in ("", FREE_DIAGNOSTIC_MODEL):
        raise ValueError("unsupported_diagnostic_model")
    if config.get("provider_profile") is not None:
        from native_provider_profile import validate_profile_identity

        profile = validate_profile_identity(config["provider_profile"])
        if selected and (profile["model"] != selected or profile["billing"] != "free"):
            raise ValueError("diagnostic_profile_model_mismatch")
        return profile["model"]
    return selected or OFFICIAL_MODEL


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candidate_id_for_audit(audit, audit_sha256, schema=IDENTITY_SCHEMA):
    """Bind new candidates to the exact source AND task selection audit.

    Missing schema retains the historical source-only identity, so archived
    evidence can still be checked without relabeling any previous execution.
    """
    source = audit.get("source_files_sha256", "")
    if not re.fullmatch("[a-f0-9]{64}", source):
        raise ValueError("Candidate source digest invalid")
    if schema is None:
        return "native-candidate-" + source
    if schema != IDENTITY_SCHEMA or not re.fullmatch("[a-f0-9]{64}", audit_sha256):
        raise ValueError("Candidate identity schema invalid")
    material = {"schema": schema, "source_files_sha256": source,
                "source_audit_sha256": audit_sha256}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "native-candidate-" + digest


def load_candidate(path, *, verify_source=True, verify_controller=True):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    upstream_ip_family(config)
    model_id(config)
    if config.get("provider_profile_path"):
        from native_provider_profile import load_profile

        _, approved_profile = load_profile(config["provider_profile_path"])
        if approved_profile != config.get("provider_profile"):
            raise ValueError("provider_profile_changed")
    if config.get("diagnostic_model") and (
        type(config.get("diagnostic_rpm", 10)) is not int
        or not 1 <= config.get("diagnostic_rpm", 10) <= 20
        or type(config.get("diagnostic_daily_cap", 50)) is not int
        or not 1 <= config.get("diagnostic_daily_cap", 50) <= 1000
    ):
        raise ValueError("invalid_free_diagnostic_limits")
    if config.get("schema") != SCHEMA or config.get("frozen") is not False:
        raise ValueError("Not an unfrozen native candidate")
    digest = config.get("source_files_sha256", "")
    audit_path = Path(config["source_audit"])
    if sha(audit_path) != config["source_audit_sha256"]:
        raise ValueError("Source audit changed")
    audit = json.loads(audit_path.read_text())
    if config.get("candidate_id") != candidate_id_for_audit(
        audit, config["source_audit_sha256"], config.get("candidate_identity_schema")
    ):
        raise ValueError("Candidate identity invalid")
    if (
        audit["source_files_sha256"] != digest
        or config["source_commit"] != audit["source_commit"]
    ):
        raise ValueError("Candidate source mismatch")
    if verify_source:
        verify(Path(config["source_root"]), audit)
    root = Path(config["candidate_root"])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("Absolute independent candidate root required")
    for key in ("data", "binaries", "credentials"):
        p = Path(config[key])
        if not p.is_absolute() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("Candidate path escapes independent root")
    protected = [
        Path("/var/lib/qiqc-staging"),
        Path("/opt/qiqc-staging"),
        Path("/etc/qiqc-staging"),
    ]
    if any(
        root.resolve().is_relative_to(p) or p.is_relative_to(root.resolve())
        for p in protected
    ):
        raise ValueError("Current staging paths forbidden")
    ports = []
    for key in ("url", "gateway_url"):
        u = urlsplit(config[key])
        if (
            u.scheme != "http"
            or u.hostname != "127.0.0.1"
            or u.path
            or u.query
            or u.fragment
            or u.username
            or not u.port
            or u.port in (18890, 18889, 8765, 8789, 8790)
        ):
            raise ValueError("Dedicated loopback candidate ports required")
        ports.append(u.port)
    if ports[0] == ports[1]:
        raise ValueError("Candidate ports must differ")
    if (
        type(config.get("functional_slots", 1)) is not int
        or not 1 <= config.get("functional_slots", 1) <= 10000
    ):
        raise ValueError("Native functional slots must be 1 through 10000")
    if (type(config.get('model_max_concurrent', 49)) is not int or
            not 1 <= config.get('model_max_concurrent', 49) <= 10000):
        raise ValueError('Native model concurrency must be 1 through 10000')
    agent_budget_seconds(config)
    if verify_controller:
        from native_controller import verify as verify_controller_files

        verify_controller_files(config)
    return config, audit


def active_release(config):
    """Read the optional immutable release pointer; candidate history stays intact."""
    pointer = Path(config["candidate_root"]) / "active-release.json"
    if not pointer.exists():
        return None
    from native_release_record import load_release, read_regular, validate_binding

    record = json.loads(read_regular(pointer, 16384))
    release_path = Path(record["release_path"])
    if not release_path.is_absolute() or sha(release_path) != record["release_sha256"]:
        raise ValueError("active_release_pointer_invalid")
    release = validate_binding(config, load_release(release_path))
    if release["release_id"] != record["release_id"]:
        raise ValueError("active_release_pointer_mismatch")
    return release


def admission_release_id(config):
    release = active_release(config)
    return release["release_id"] if release else config["candidate_id"]


def agent_budget_seconds(config):
    value = config.get("agent_budget_seconds", 300)
    if type(value) is not int or value not in (180, 300):
        raise ValueError("Native observation budget must be 180 or 300 seconds")
    return value


def identity(config, ordinal, release_id=None):
    if type(ordinal) is not int or not 0 <= ordinal < 49:
        raise ValueError("Invalid ordinal")
    selected = release_id or config["candidate_id"]
    frozen = selected != config["candidate_id"]
    if frozen:
        release = active_release(config)
        if not release or release["release_id"] != selected:
            raise ValueError("unregistered_native_release")
    return {
        "release_id": selected,
        "diagnostic_model": config.get("diagnostic_model", ""),
        "provider_profile": config.get("provider_profile"),
        "model_id": model_id(config),
        "diagnostic_rpm": config.get("diagnostic_rpm", 10)
        if config.get("diagnostic_model")
        else None,
        "diagnostic_daily_cap": config.get("diagnostic_daily_cap", 50)
        if config.get("diagnostic_model")
        else None,
        "source_files_sha256": config["source_files_sha256"],
        "source_audit_sha256": config["source_audit_sha256"],
        "source_commit": config["source_commit"],
        "task_index": ordinal,
        "frozen": frozen,
        "execution_topology": "e2b-native-compose",
        "acceptance_scope": "bounded-three-minute" if agent_budget_seconds(config) == 180 else "bounded-five-minute",
        **({"agent_budget_seconds": 180} if agent_budget_seconds(config) == 180 else {}),
        "execution_template_id": config["template_id"],
        "controller_sha256": config["controller_sha256"],
        "build_plan_sha256": config.get("build_plan_sha256"),
        "build_receipt_sha256": config.get("build_receipt_sha256"),
    }


def commands(config):
    bins = Path(config["binaries"])
    project = Path(config["project_root"])
    data = config["data"]
    app = project / "competition-platform"
    bridge = project / "benchmark-assets/adapters/science-probe/native_worker_bridge.py"
    result = {
        "api": [
            str(bins / "api"),
            "-standalone",
            "-listen",
            urlsplit(config["url"]).netloc,
            "-data",
            data,
            "-site",
            str(app / "dist"),
            "-web",
            str(app / "platform/web"),
            "-adapters",
            config["adapters"],
            "-reference",
            str(Path(config["adapters"]) / "reference.zip"),
            "-science-manifest",
            config["adapter_manifest"],
            "-science-agent-budget-seconds",
            str(agent_budget_seconds(config)),
        ],
        "worker": [
            str(bins / "worker"),
            "-data",
            data,
            "-slots",
            str(config.get("functional_slots", 1)),
            "-science-project",
            str(project),
            "-science-python",
            config["python"],
            "-science-bridge",
            str(bridge),
            "-model-gateway",
            config["gateway_url"],
        ],
        "gateway": [
            str(bins / "gateway"),
            "-data",
            data,
            "-listen",
            urlsplit(config["gateway_url"]).netloc,
            "-provider-env",
            config["provider_env"],
            "-request-timeout",
            # A model SSE can legitimately take several minutes while the
            # agent is receiving a large tool-call response.  The old 360s
            # cutoff converted a live HTTP 200 stream with no terminal event
            # into a local 504 before the task's native budget expired.
            "600s",
            "-max-concurrent",
            str(config.get('model_max_concurrent', 49)),
            "-upstream-ip-family",
            upstream_ip_family(config),
        ],
        "review": [
            str(bins / "review"),
            "-db",
            str(Path(data) / "platform.db"),
            "-provider",
            "https",
            "-key-file",
            config["review_key"],
        ],
        "bootstrap": [
            str(bins / "admin"),
            "-data",
            data,
            "-candidate-id",
            config["candidate_id"],
        ],
    }
    if config.get("science_task_set_path"):
        result["api"] += ["-science-task-set", config["science_task_set_path"]]
    for key, flag in (("event_calendar_path", "-event-calendar"),
                      ("formal_execution_path", "-formal-execution")):
        if config.get(key):
            result["api"] += [flag, config[key]]
    if config.get("diagnostic_model"):
        for role in ("worker", "gateway", "review"):
            result[role] += ["-diagnostic-model", config["diagnostic_model"]]
        for role in ("gateway", "review"):
            result[role] += [
                "-diagnostic-rpm",
                str(config.get("diagnostic_rpm", 10)),
                "-diagnostic-daily-cap",
                str(config.get("diagnostic_daily_cap", 50)),
            ]
    if config.get("provider_profile_path"):
        for role in ("gateway", "review", "worker"):
            result[role] += ["-provider-profile", config["provider_profile_path"]]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    for key in (
        "source-root",
        "source-audit",
        "candidate-root",
        "project-root",
        "python",
        "adapter-manifest",
        "adapters",
        "provider-env",
        "r2-env",
        "review-key",
        "scanner-socket",
        "template-id",
    ):
        p.add_argument("--" + key, required=True)
    p.add_argument(
        "--diagnostic-model", choices=("", FREE_DIAGNOSTIC_MODEL), default=""
    )
    p.add_argument("--remote-source-root", default="/opt/qiqcbench")
    p.add_argument("--remote-python", default="/opt/harbor/bin/python")
    p.add_argument("--upstream-ip-family", default="auto", help="auto, ipv4 or ipv6; trusted gateway routing only")
    p.add_argument("--identity-schema", choices=(IDENTITY_SCHEMA,), default=IDENTITY_SCHEMA)
    p.add_argument("--port", type=int, default=19890)
    p.add_argument("--gateway-port", type=int, default=19889)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("run-role")
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument(
        "--role",
        choices=("api", "worker", "gateway", "review", "bootstrap"),
        required=True,
    )
    p = sub.add_parser("build")
    p.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        upstream_ip_family({"upstream_ip_family": args.upstream_ip_family})
        audit = json.loads(Path(args.source_audit).read_text())
        verify(Path(args.source_root), audit)
        root = Path(args.candidate_root).resolve()
        config = {
            key: os.path.abspath(getattr(args, key))
            if key == "python"
            else str(Path(getattr(args, key)).resolve())
            for key in (
                "source_root",
                "source_audit",
                "project_root",
                "python",
                "adapter_manifest",
                "adapters",
                "provider_env",
                "r2_env",
                "review_key",
                "scanner_socket",
            )
        }
        config.update(
            schema=SCHEMA,
            diagnostic_model=args.diagnostic_model,
            upstream_ip_family=args.upstream_ip_family,
            frozen=False,
            candidate_root=str(root),
            data=str(root / "data"),
            binaries=str(root / "bin"),
            credentials=str(root / "preview-credentials.json"),
            candidate_id=candidate_id_for_audit(audit, sha(args.source_audit), args.identity_schema),
            candidate_identity_schema=args.identity_schema,
            source_files_sha256=audit["source_files_sha256"],
            source_commit=audit["source_commit"],
            source_audit_sha256=sha(args.source_audit),
            url=f"http://127.0.0.1:{args.port}",
            gateway_url=f"http://127.0.0.1:{args.gateway_port}",
            template_id=args.template_id,
            remote_source_root=args.remote_source_root,
            remote_python=args.remote_python,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as out:
            json.dump(config, out, indent=2)
            out.write("\n")
        args.output.chmod(0o600)
        try:
            load_candidate(args.output, verify_controller=False)
        except Exception:
            args.output.unlink()
            raise
        print(
            json.dumps(
                {
                    "candidate_config": str(args.output),
                    "candidate_id": config["candidate_id"],
                    "frozen": False,
                    "services_started": False,
                }
            )
        )
        return
    config, _ = load_candidate(args.candidate)
    if args.action == "build":
        output = Path(config["binaries"])
        output.mkdir(parents=True, exist_ok=True)
        for name, package in [
            ("api", "."),
            ("worker", "./cmd/qiqc-worker"),
            ("gateway", "./cmd/qiqc-model-gateway"),
            ("admin", "./cmd/qiqc-admin"),
            ("review", "./cmd/review-worker"),
        ]:
            target = output / (name + ".next")
            subprocess.run(
                ["go", "build", "-o", str(target), package],
                cwd=Path(config["project_root"]) / "competition-platform/platform-go",
                check=True,
            )
            target.replace(output / name)
        return
    env = dict(
        os.environ,
        QIQC_NATIVE_CANDIDATE=str(args.candidate.resolve()),
        QIQC_R2_ENABLED="1",
        QIQC_R2_ENV=config["r2_env"],
        QIQC_SCANNER_SOCKET=config["scanner_socket"],
        QIQC_MODEL=model_id(config),
    )
    # No password or master provider key appears in argv. Bootstrap reads JSON stdin.
    command = commands(config)[args.role]
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
