"""Trusted fixed probe, executed only INSIDE the disposable E2B main sandbox."""

import concurrent.futures
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def network_probe(target):
    started = time.monotonic()
    result = {"name": target["name"], "kind": target.get("kind", "http")}
    try:
        if target.get("kind") in {"ssh", "tls"}:
            with socket.create_connection(
                (target["host"], target["port"]), timeout=4
            ) as sock:
                result["tcp_accepted"] = True
                if target["kind"] == "ssh":
                    banner = sock.recv(256)
                    result["application_response"] = banner.startswith(b"SSH-")
                    result["protocol_bytes"] = len(banner)
                else:
                    with ssl.create_default_context().wrap_socket(
                        sock,
                        server_hostname=target.get("server_hostname", target["host"]),
                    ) as tls:
                        result["application_response"] = True
                        result["tls_version"] = tls.version()
        elif target.get("kind") in {"websocket", "sni_host_mismatch"}:
            connection = http.client.HTTPSConnection(
                target["connect_host"], 443, timeout=4
            )
            try:
                connection.request(
                    target.get("method", "GET"),
                    target["path"],
                    body=b"public-security-fixture"
                    if target.get("method") == "POST"
                    else None,
                    headers=target["headers"],
                )
                result["status"] = connection.getresponse().status
            finally:
                connection.close()
        elif target.get("kind") == "dns_udp":
            transaction = os.urandom(2)
            question = (
                b"".join(
                    bytes([len(part)]) + part.encode()
                    for part in target["hostname"].split(".")
                )
                + b"\0"
                + struct.pack("!HH", 1, 1)
            )
            packet = transaction + struct.pack("!HHHHH", 0x100, 1, 0, 0, 0) + question
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(4)
                udp.sendto(packet, (target["resolver"], 53))
                reply, _ = udp.recvfrom(4096)
                result["application_response"] = (
                    len(reply) >= 12 and reply[:2] == transaction
                )
                result["dns_response_code"] = (
                    reply[3] & 15 if len(reply) >= 12 else None
                )
        elif target.get("kind") == "dns":
            # A response from a recursive resolver does not prove whether an
            # authoritative query escaped; this remains explicitly inconclusive.
            socket.getaddrinfo(target["host"], 443)
            result["resolved"] = True
        else:
            request = Request(
                target["url"],
                data=(
                    b"public-security-fixture"
                    if target.get("method") == "POST"
                    else None
                ),
                headers=target.get("headers", {}),
                method=target.get("method", "GET"),
            )
            with build_opener(ProxyHandler({})).open(request, timeout=4) as response:
                result["status"] = response.status
    except HTTPError as exc:
        result["status"] = exc.code
        if target["name"] == "link_local_metadata" and exc.code in (401, 403):
            # Never request a metadata token or read a successful metadata body.
            body = exc.read(256)
            result["rejection_body_sha256"] = hashlib.sha256(body).hexdigest()
            result["rejection_body_bytes"] = len(body)
            result["server_family"] = (
                "AmazonEC2"
                if exc.headers.get("Server", "") == "EC2ws"
                else "other_or_unspecified"
            )
            result["content_type"] = exc.headers.get("Content-Type", "")[:64]
            server = exc.headers.get("Server", "")
            result["server_header"] = (
                server[:64] if server.isascii() else "non_ascii_omitted"
            )
    except (URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
        result["error_class"] = type(exc).__name__
        if isinstance(exc, socket.gaierror):
            result["resolver_code"] = exc.errno
    result["seconds"] = round(time.monotonic() - started, 4)
    return result


def file_probe():
    # Opening is enough to establish readability. Never read a truth byte.
    paths = {
        "current_device_truth": "/opt/runtime/configs/devices/tunable_coupler_cz_v0.hidden.example.yaml",
        "current_private_scorer": "/opt/runtime/configs/task_materials/tunable_coupler_cz_netzero/hidden/scorer.example.yaml",
        "verifier_source": "/opt/runtime/harbor_tasks/tunable_coupler_cz_netzero/tests/score_cz.py",
        "other_task_config": "/opt/runtime/configs/tasks/bell2_fidelity.yaml",
        "qsim_synthetic_marker": "/opt/acceptance/qsim-marker.txt",
        "verifier_synthetic_marker": "/opt/acceptance/verifier-marker.txt",
        "vps_project_credentials": "/root/QQ-Challenge/.env",
        "docker_socket": "/var/run/docker.sock",
    }
    outcomes = {}
    for name, path in paths.items():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            outcomes[name] = {"readable": True}
        except OSError as exc:
            outcomes[name] = {"readable": False, "error_class": type(exc).__name__}
    forbidden = {
        "E2B_API_KEY",
        "HY_API",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
    found = set(forbidden.intersection(os.environ))
    # Inspect key names only and return no process environment values.
    for proc in Path("/proc").glob("[0-9]*/environ"):
        try:
            data = proc.read_bytes()
            found.update(
                part.split(b"=", 1)[0].decode(errors="ignore")
                for part in data.split(b"\0")
                if part.split(b"=", 1)[0].decode(errors="ignore") in forbidden
            )
        except OSError:
            pass
    return {
        "paths": outcomes,
        "forbidden_credential_key_names_found": sorted(found),
        "uid": os.getuid(),
    }


def main():
    if (
        os.path.exists("/root/QQ-Challenge")
        or not Path("/tmp/qiqc-security-e2b-only").is_file()
    ):
        raise RuntimeError(
            "This fixed probe may run only in the disposable E2B fixture"
        )
    config = json.loads(Path("/home/user/security-targets.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(network_probe, config["targets"]))
    result = {"network": outcomes, "filesystem": file_probe()}
    if os.getuid() == 0 and config.get("forge_local_verifier"):
        Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
        Path("/logs/verifier/reward.txt").write_text("1\n")
        Path("/tests").mkdir(parents=True, exist_ok=True)
        Path("/tests/score_cz.py").write_text(
            "# LOCAL FORGERY ONLY: must never replace remote verifier\n"
        )
        result["main_local_forged_reward"] = 1
    print(json.dumps(result))


if __name__ == "__main__":
    main()
