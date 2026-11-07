#!/usr/bin/env python3
"""Bounded real E2B public-CZ negative tests; never runs payloads on the VPS.

No model calls or hidden task content. Probe output excludes URLs, credentials,
response bodies and private files. Three disposable sandboxes are always killed.
"""

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from e2b_runtime import PUBLIC_TASK, ROOT, create, provision, runtime_env, start_qsim
from security_matrix import classify
from verify_public import validate_archive

HERE = Path(__file__).resolve().parent
SCORER = f"/opt/runtime/harbor_tasks/{PUBLIC_TASK}/tests/score_cz.py"


def save(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(path)


def http_ready(url, headers):
    for _ in range(40):
        try:
            if httpx.get(url, headers=headers, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("acceptance_endpoint_not_ready")


def r2_endpoint():
    for line in (ROOT / "runtime/legacy-platform-data/r2.env").read_text().splitlines():
        if line.startswith("R2_ENDPOINT="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname.endswith(
                ".r2.cloudflarestorage.com"
            ):
                raise ValueError("invalid_r2_endpoint")
            return value
    raise ValueError("missing_r2_endpoint")


def targets(qsim, verifier):
    qurl = "https://" + qsim.get_host(8123)
    vurl = "https://" + verifier.get_host(9999)
    qheaders = {"e2b-traffic-access-token": qsim.traffic_access_token}
    vheaders = {"e2b-traffic-access-token": verifier.traffic_access_token}
    # Read the local address, never call a third-party IP discovery endpoint.
    vps = subprocess.run(
        ["hostname", "-I"], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    rows = [
        {"name": "public_internet", "url": "https://example.com", "expect": "deny"},
        {"name": "e2b_management", "url": "https://api.e2b.dev", "expect": "deny"},
        {
            "name": "cloudflare_management",
            "url": "https://api.cloudflare.com/client/v4/user/tokens/verify",
            "expect": "deny",
        },
        {"name": "r2_account_endpoint", "url": r2_endpoint(), "expect": "deny"},
        {
            "name": "controlled_webhook",
            "url": vurl + "/webhook",
            "method": "POST",
            "headers": vheaders,
            "expect": "deny",
        },
        {
            "name": "controlled_websocket",
            "kind": "websocket",
            "connect_host": urlsplit(vurl).hostname,
            "path": "/websocket",
            "headers": dict(
                vheaders,
                **{
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "Sec-WebSocket-Version": "13",
                },
            ),
            "expect": "deny",
        },
        {
            "name": "remote_verifier_write_route",
            "url": vurl + "/tests/score_cz.py",
            "method": "PUT",
            "headers": vheaders,
            "expect": "deny",
        },
        {
            "name": "vps_ssh_tcp",
            "kind": "ssh",
            "host": vps,
            "port": 22,
            "expect": "deny",
        },
        {
            "name": "external_ipv4_tcp",
            "kind": "tls",
            "host": "1.1.1.1",
            "port": 443,
            "expect": "deny",
        },
        {
            "name": "external_ipv6_tcp",
            "kind": "tls",
            "host": "2606:4700:4700::1111",
            "port": 443,
            "expect": "deny",
        },
        {
            "name": "link_local_metadata",
            "url": "http://169.254.169.254/latest/meta-data/",
            "expect": "deny",
        },
        {
            "name": "dns_exfil_channel",
            "kind": "dns",
            "host": secrets.token_hex(8) + ".example.com",
            "expect": "dns_unknown",
        },
        {
            "name": "allowed_qsim",
            "url": qurl + "/healthz",
            "headers": qheaders,
            "expect": "http_200",
        },
        {
            "name": "qsim_unauthenticated",
            "url": qurl + "/healthz",
            "expect": "http_403",
        },
        {
            "name": "qsim_wrong_token",
            "url": qurl + "/healthz",
            "headers": {"e2b-traffic-access-token": "invalid-fixture-token"},
            "expect": "http_403",
        },
        {
            "name": "qsim_private_file_url",
            "url": qurl
            + "/opt/runtime/configs/devices/tunable_coupler_cz_v0.hidden.example.yaml",
            "headers": qheaders,
            "expect": "http_404",
        },
        {
            "name": "qsim_path_traversal_url",
            "url": qurl
            + "/%2e%2e/%2e%2e/opt/runtime/configs/devices/tunable_coupler_cz_v0.hidden.example.yaml",
            "headers": qheaders,
            "expect": "http_404",
        },
    ]
    rows.append(
        {
            "name": "allowed_sni_forbidden_http_host",
            "kind": "sni_host_mismatch",
            "connect_host": urlsplit(qurl).hostname,
            "path": "/webhook",
            "method": "POST",
            "headers": dict(vheaders, Host=urlsplit(vurl).hostname),
            "expect": "deny",
        }
    )
    return rows, qurl, qheaders, vurl, vheaders


def probe(main, rows, *, user, forge=False):
    main.files.write(
        "/home/user/security-targets.json",
        json.dumps({"targets": rows, "forge_local_verifier": forge}),
        user="root",
    )
    main.commands.run("chmod 644 /home/user/security-targets.json", user="root")
    output = main.commands.run(
        "python /home/user/security_probe_payload.py", user=user, timeout=75
    )
    if output.exit_code != 0:
        raise RuntimeError("probe_exit_nonzero")
    return json.loads(output.stdout)


def digest(sb, path):
    return hashlib.sha256(
        bytes(sb.files.read(path, format="bytes", user="root"))
    ).hexdigest()


def main(output, relay_isolation=False):
    started = time.monotonic()
    probe_root = ROOT / "benchmark-assets/adapters/science-probe"
    sys.path.insert(0, str(probe_root))
    from qsim_relay import QsimRelay, install_relay

    report = {
        "schema": "qiqc-science-security-matrix/2",
        "task": PUBLIC_TASK,
        "checked_at_unix": time.time(),
        "scope": "real-three-sandbox-public-CZ",
        "relay_isolation": relay_isolation,
        "model_calls": 0,
        "sdk_versions": {"e2b": "2.56.0", "httpx": "0.28.1"},
        "official_network_reference": "https://docs.e2b.dev/network/internet-access",
        "hidden_tasks_executed": 0,
        "raw_truth_or_response_bodies_in_report": False,
        "network_policy": {
            "deny_out": ["0.0.0.0/0"],
            "allow_out": ["current-qsim-host"],
        },
        "roles": [],
        "cleanup": [],
        "completed": False,
        "formal_hidden_isolation_accepted": False,
        "limitations": [
            "One public CZ fixture; does not certify all 49 deployments.",
            "No controlled authoritative DNS sink; DNS exfiltration remains unverified.",
            "Unavailable pre-restriction targets prove only observed blocking, not policy efficacy.",
            "Finite fixed attack matrix is not a sandbox escape or kernel security proof.",
            "Network access to trusted qsim remains an intended channel; comprehensive qsim exploit resistance is unverified.",
            "E2B usage is nonzero; this probe does not retrieve billing usage or price.",
        ],
    }
    created = []
    save(output, report)
    try:
        for role in ("main", "qsim", "verifier"):
            sb = create(timeout=900)
            created.append((role, sb))
            report["roles"].append({"role": role, "sandbox_id": sb.sandbox_id})
            save(output, report)
        main_sb, qsim, verifier = [item[1] for item in created]
        for role, sb in created[1:]:
            provision(sb, task_id=PUBLIC_TASK, role=role)
            sb.commands.run("mkdir -p /opt/acceptance", user="root")
            sb.files.write(
                f"/opt/acceptance/{role}-marker.txt", secrets.token_hex(16), user="root"
            )
            sb.update_network({"allow_internet_access": False})
        for sb in (main_sb, verifier):
            sb.files.write(
                "/tmp/qiqc-security-e2b-only", "fixed-public-fixture", user="root"
            )
        main_sb.files.write(
            "/home/user/security_probe_payload.py",
            (HERE / "security_probe_payload.py").read_text(),
            user="root",
        )
        verifier.files.write(
            "/tmp/security_sink_payload.py",
            (HERE / "security_sink_payload.py").read_text(),
            user="root",
        )
        verifier.commands.run(
            "python /tmp/security_sink_payload.py >/tmp/security-sink.log 2>&1",
            user="root",
            background=True,
            timeout=0,
        )
        start_qsim(qsim, PUBLIC_TASK)
        rows, qurl, qheaders, vurl, vheaders = targets(qsim, verifier)
        if relay_isolation:
            # No qsim capability is placed into main, even during this baseline.
            rows = [
                row
                for row in rows
                if not row["name"].startswith(("qsim_", "allowed_qsim"))
            ]
            rows.append(
                {
                    "name": "direct_qsim_egress",
                    "url": qurl + "/healthz",
                    "expect": "deny",
                }
            )
            rows.append(
                {
                    "name": "direct_dns_resolver",
                    "kind": "dns_udp",
                    "resolver": "8.8.8.8",
                    "hostname": "example.com",
                    "expect": "deny",
                }
            )
            report["network_policy"] = {
                "allow_internet_access": False,
                "allow_out": [],
                "deny_out": ["0.0.0.0/0"],
            }
            report["main_qsim_capability_absent_from_probe_config"] = (
                qsim.traffic_access_token not in json.dumps(rows)
            )
        http_ready(qurl + "/healthz", qheaders)
        http_ready(vurl + "/health", vheaders)
        source_before = digest(verifier, SCORER)
        marker_before = {
            role: digest(sb, f"/opt/acceptance/{role}-marker.txt")
            for role, sb in created[1:]
        }
        baseline = probe(main_sb, rows, user="user")
        report["baseline"] = baseline
        counts_before = json.loads(
            verifier.files.read("/tmp/security-sink-counts.json", user="root")
        )
        report["controlled_sink_before"] = counts_before
        save(output, report)
        if relay_isolation:
            install_relay(main_sb, (probe_root / "qsim_call.py").read_text())
            command = "python3 /home/user/qsim_call.py list_devices '{}' >/home/user/relay-check.json; printf '%s' $? >/home/user/relay-check.exit"
            main_sb.commands.run(command, background=True, timeout=0)
            with QsimRelay(qurl, qsim.traffic_access_token) as relay:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    relay.poll(main_sb)
                    if relay.events:
                        break
                    time.sleep(0.2)
                if not relay.events:
                    raise RuntimeError("real_qsim_relay_failed")
                report["real_qsim_relay"] = {
                    "events": relay.events,
                    "network_mode": "all-egress-disabled",
                }
            for _ in range(20):
                try:
                    if main_sb.files.read("/home/user/relay-check.exit").strip() == "0":
                        report["real_qsim_relay"]["helper_exit"] = 0
                        break
                except Exception:  # noqa: BLE001,S110 — bounded probe file race
                    pass
                time.sleep(0.2)
            if report["real_qsim_relay"].get("helper_exit") != 0:
                raise RuntimeError("relay_helper_failed")
        else:
            main_sb.update_network(
                {"deny_out": ["0.0.0.0/0"], "allow_out": [urlsplit(qurl).hostname]}
            )
        locked = {}
        for user in ("user", "root"):
            locked[user] = probe(main_sb, rows, user=user, forge=user == "root")
            report["restricted"] = locked
            save(output, report)
        baseline_by_name = {item["name"]: item for item in baseline["network"]}
        report["matrix"] = []
        for user, data in locked.items():
            after = {item["name"]: item for item in data["network"]}
            for row in rows:
                report["matrix"].append(
                    {
                        "user": user,
                        "name": row["name"],
                        "expectation": row["expect"],
                        "outcome": classify(
                            baseline_by_name[row["name"]],
                            after[row["name"]],
                            row["expect"],
                        ),
                    }
                )
        counts_after = json.loads(
            verifier.files.read("/tmp/security-sink-counts.json", user="root")
        )
        report["controlled_sink_after"] = counts_after
        report["controlled_sink_positive_control"] = (
            counts_before["webhook"] > 0 and counts_before["websocket"] > 0
        )
        report["controlled_sink_unchanged_after_lock"] = counts_before == counts_after
        # Transfer only qsim evidence. Main's forged reward and source never enter verifier.
        qsim.commands.run(
            "tar -czf /root/qiqc-security-evidence.tar.gz -C /qsim_logs .",
            user="root",
            timeout=30,
        )
        evidence = bytes(
            qsim.files.read(
                "/root/qiqc-security-evidence.tar.gz", format="bytes", user="root"
            )
        )
        validate_archive(evidence)
        verifier.files.write(
            "/root/qiqc-security-evidence.tar.gz", evidence, user="root"
        )
        verifier.commands.run(
            "tar -xzf /root/qiqc-security-evidence.tar.gz -C /qsim_logs",
            user="root",
            timeout=30,
        )
        result = verifier.commands.run(
            "bash /opt/runtime/harbor_tasks/" + PUBLIC_TASK + "/tests/test.sh",
            envs=runtime_env(PUBLIC_TASK),
            user="root",
            timeout=180,
        )
        reward = verifier.files.read("/logs/verifier/reward.txt", user="root").strip()
        report["verifier"] = {
            "exit_code": result.exit_code,
            "reward": int(reward) if reward in ("0", "1") else None,
            "main_forged_reward": locked["root"].get("main_local_forged_reward"),
            "source_unchanged": source_before == digest(verifier, SCORER),
            "evidence_source": "qsim-only",
            "evidence_bytes": len(evidence),
            "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        }
        report["role_markers_unchanged"] = all(
            marker_before[role] == digest(sb, f"/opt/acceptance/{role}-marker.txt")
            for role, sb in created[1:]
        )
        report["filesystem_absence_passed"] = all(
            not item["readable"]
            for data in locked.values()
            for item in data["filesystem"]["paths"].values()
        )
        report["no_master_credential_names_found"] = all(
            not data["filesystem"]["forbidden_credential_key_names_found"]
            for data in locked.values()
        )
        report["tested_negative_cases_passed"] = (
            all(not row["outcome"].startswith("failed") for row in report["matrix"])
            and report["controlled_sink_positive_control"]
            and report["controlled_sink_unchanged_after_lock"]
            and report["filesystem_absence_passed"]
            and report["no_master_credential_names_found"]
            and report["role_markers_unchanged"]
            and report["verifier"]["source_unchanged"]
            and report["verifier"]["reward"] == 0
            and report["verifier"]["exit_code"] == 0
        )
        report["completed"] = True
    except Exception as exc:  # noqa: BLE001 — cleanup and sanitized evidence on all failures
        # Provider/commands exceptions can contain request headers or payloads.
        report["failure_class"] = type(exc).__name__
    finally:
        for role, sb in reversed(created):
            entry = {"role": role, "sandbox_id": sb.sandbox_id}
            try:
                sb.kill()
                entry["running_after_kill"] = sb.is_running()
                entry["destroyed"] = not entry["running_after_kill"]
            except Exception as exc:  # noqa: BLE001 — cleanup and sanitized evidence on all failures
                entry["destroyed"] = False
                entry["error_class"] = type(exc).__name__
            report["cleanup"].append(entry)
            save(output, report)
        report["cleanup_complete"] = len(report["cleanup"]) == len(created) and all(
            row["destroyed"] for row in report["cleanup"]
        )
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(output, report)
    print(
        json.dumps(
            {
                "report": str(output),
                "completed": report["completed"],
                "tested_negative_cases_passed": report.get(
                    "tested_negative_cases_passed", False
                ),
                "cleanup_complete": report["cleanup_complete"],
                "failure_class": report.get("failure_class"),
            }
        )
    )
    return (
        0
        if report.get("tested_negative_cases_passed") and report["cleanup_complete"]
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "competition-platform/platform/test-results/science-security-matrix-e2b.json",
    )
    parser.add_argument("--relay-isolation", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(args.output, args.relay_isolation))
