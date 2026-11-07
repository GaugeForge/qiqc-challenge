#!/usr/bin/env python3
"""Explicit E2B transfer diagnostic: generated public bytes, no task or model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import time


def classes(error):
    allowed = {"TimeoutException", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
               "ReadError", "WriteError", "ConnectError", "ConnectionError", "TimeoutError",
               "SandboxUnreachableException", "SandboxNotRunningException", "RuntimeError",
               "ValueError", "OSError", "FileNotFoundException"}
    result = []
    seen = set()
    while error is not None and id(error) not in seen and len(result) < 6:
        seen.add(id(error))
        name = type(error).__name__
        result.append(name if name in allowed else "other")
        error = error.__cause__ or error.__context__
    return result


def error_flags(error):
    """Classify known transport wording without emitting raw provider errors."""
    flags = set()
    seen = set()
    patterns = ("connection reset", "stream error", "http2", "http/2", "broken pipe",
                "unexpected eof", "decode", "body", "timeout", "cancel", "protocol")
    while error is not None and id(error) not in seen and len(seen) < 6:
        seen.add(id(error))
        message = str(error).lower()
        flags.update(p for p in patterns if p in message)
        error = error.__cause__ or error.__context__
    return sorted(flags)


def transfer(sandbox, path, expected, timeout):
    started = time.monotonic()
    count = 0
    digest = hashlib.sha256()
    stage = "stat"
    result = {"request_timeout_seconds": timeout, "complete": False}
    try:
        info = sandbox.files.get_info(path, user="root", request_timeout=timeout)
        result["stat_bytes"] = info.size
        assert info.size == expected["size"]
        stage = "open"
        stream = sandbox.files.read(path, format="stream", user="root", request_timeout=timeout)
        stage = "read"
        with stream:
            for chunk in stream:
                count += len(chunk)
                if count > expected["size"]:
                    raise ValueError("fixture size exceeded")
                digest.update(chunk)
            stage = "close"
        result["complete"] = count == expected["size"] and digest.hexdigest() == expected["sha256"]
    except Exception as error:
        result.update(error_stage=stage, error_classes=classes(error), error_flags=error_flags(error))
    result.update(bytes_received=count, elapsed_seconds=round(time.monotonic() - started, 3))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-e2b", action="store_true")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-http-versions", action="store_true")
    args = parser.parse_args()
    if not args.real_e2b:
        parser.error("explicit --real-e2b required")
    from dotenv import dotenv_values
    from e2b import Sandbox
    import importlib.metadata

    config = json.loads(args.candidate.read_text())
    key = dotenv_values(Path(config["candidate_root"]) / "config/worker-e2b.env")["E2B_API_KEY"]
    report = {"schema": "qiqc-public-e2b-transfer/v1", "model_calls": 0,
              "scientific_tasks": 0, "sdk_version": importlib.metadata.version("e2b")}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    sandbox = None
    try:
        sandbox = Sandbox.create(template=config["template_id"], api_key=key, timeout=300,
                                 network={"allow_public_traffic": False, "deny_out": ["0.0.0.0/0"]},
                                 metadata={"purpose": "qiqc-public-transfer-diagnostic"})
        report["sandbox_id"] = sandbox.sandbox_id
        path = "/tmp/qiqc-public-transfer-fixture.bin"
        script = "import os,hashlib,json,pathlib;d=os.urandom(8*1024*1024);pathlib.Path(" + repr(path) + ").write_bytes(d);print(json.dumps({'size':len(d),'sha256':hashlib.sha256(d).hexdigest()}))"
        output = sandbox.commands.run(shlex.join(["python3", "-c", script]), user="root", timeout=30)
        expected = json.loads(output.stdout)
        report["fixture_bytes"] = expected["size"]
        if args.compare_http_versions:
            report["transfers"] = []
            for version in ("2", "1.1"):
                client = Sandbox.connect(sandbox.sandbox_id, api_key=key, http_version=version)
                result = transfer(client, path, expected, 60)
                result["configured_http_version"] = version
                report["transfers"].append(result)
        else:
            report["transfers"] = [transfer(sandbox, path, expected, timeout) for timeout in (10, 60)]
    except Exception as error:
        report["setup_error_classes"] = classes(error)
    finally:
        if sandbox:
            try:
                report["cleanup_confirmed"] = type(sandbox.kill(request_timeout=10)) is bool
            except Exception as error:
                report["cleanup_confirmed"] = False
                report["cleanup_error_classes"] = classes(error)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
