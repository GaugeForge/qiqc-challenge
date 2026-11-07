#!/usr/bin/env python3
"""Read-only browser -> stage DB -> private R2 -> actual Harbor relay evidence.

Never writes raw rollouts to VPS, extracts files, or prints trajectory/secrets.
Only result.json and optional agent/context.json are parsed after bounded archive
preflight. Requires a separate output path and never overwrites evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import tarfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from archive_validation import validate_archive

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_JSON = 1024 * 1024
TOOLS = {"list_devices", "get_device_spec", "run_flux_pulse", "get_job_result"}


class EvidenceError(RuntimeError):
    """Fixed public-safe reason only, never underlying exception messages."""


def read_local_json(path):
    with path.open("rb") as source:
        raw = source.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise EvidenceError("local_report_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise EvidenceError("invalid_local_report")
    return value


def lookup_run(db_path, browser, *, catalog=False):
    batch = browser.get("batch_id")
    if (
        browser.get("status") != "passed"
        or browser.get("standard_harbor_rollout") is not True
        or not isinstance(batch, str)
        or not re.fullmatch("[a-zA-Z0-9_-]{1,80}", batch)
    ):
        raise EvidenceError("browser_report_not_completed")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(
            "SELECT r.id,r.state,r.object_key,r.object_sha,b.status,b.mode,v.sha256,r.reward,b.team_id,v.team_id,b.version_id,r.attempt,r.public FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id JOIN teams t ON t.id=b.team_id WHERE b.id=?",
            (batch,),
        ).fetchall()
    if len(rows) != 1:
        raise EvidenceError("expected_one_public_science_run")
    (
        run,
        state,
        key,
        sha,
        bstatus,
        mode,
        submission,
        reward,
        team,
        version_team,
        version,
        attempt,
        public,
    ) = rows[0]
    if team != version_team or public != (0 if catalog else 1) or type(attempt) is not int or attempt < 1:
        raise EvidenceError("invalid_run_ownership_or_attempt")
    if any(
        not isinstance(value, str) or not re.fullmatch("[a-zA-Z0-9_-]{1,80}", value)
        for value in (team, version, batch, run)
    ):
        raise EvidenceError("invalid_rollout_identity")
    if state != "completed" or bstatus != "completed" or mode != "scientific-smoke":
        raise EvidenceError("database_run_not_completed_science")
    if (
        not isinstance(sha, str)
        or not re.fullmatch("[a-f0-9]{64}", sha)
        or key != f"{team}/{version}/{batch}/{run}/{attempt}.tar.gz"
    ):
        raise EvidenceError("invalid_rollout_index")
    if browser.get("r2_archive_sha256") != sha:
        raise EvidenceError("browser_database_sha_mismatch")
    if not isinstance(submission, str) or not re.fullmatch("[a-f0-9]{64}", submission):
        raise EvidenceError("invalid_submission_sha")
    return {
        "batch_id": batch,
        "run_id": run,
        "attempt": attempt,
        "key": key,
        "sha256": sha,
        "submission_sha256": submission,
        "reward": reward,
    }


def read_r2_config(path):
    # Credentials remain in memory; refuse links and files accessible to others.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise EvidenceError("unsafe_r2_config_permissions")
        with os.fdopen(fd, "r", closefd=False) as source:
            raw = source.read(16385)
    finally:
        os.close(fd)
    result = {}
    for line in raw.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in {
            "R2_ENDPOINT",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
        }:
            result[key] = value.strip().strip("\"'")
    if len(result) != 4 or not all(result.values()):
        raise EvidenceError("missing_r2_configuration")
    target = urlsplit(result["R2_ENDPOINT"])
    if (
        target.scheme != "https"
        or target.username
        or target.password
        or target.port not in (None, 443)
        or target.path not in ("", "/")
        or target.query
        or target.fragment
        or not (target.hostname or "").endswith(".r2.cloudflarestorage.com")
    ):
        raise EvidenceError("invalid_r2_endpoint")
    return result


def bounded_download(response, expected_sha, limit=MAX_ARCHIVE):
    body = response["Body"]
    data = bytearray()
    digest = hashlib.sha256()
    started = time.monotonic()
    try:
        if response.get("ContentLength", limit + 1) > limit:
            raise EvidenceError("r2_archive_too_large")
        while True:
            if time.monotonic() - started > 60:
                raise EvidenceError("r2_download_deadline")
            chunk = body.read(min(65536, limit - len(data) + 1))
            if not chunk:
                break
            if len(data) + len(chunk) > limit:
                raise EvidenceError("r2_archive_too_large")
            digest.update(chunk)
            data.extend(chunk)
    finally:
        body.close()
    if digest.hexdigest() != expected_sha:
        raise EvidenceError("r2_archive_sha_mismatch")
    return bytes(data)


def fetch_archive(config, index):
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=config["R2_ENDPOINT"],
        region_name="auto",
        aws_access_key_id=config["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=config["R2_SECRET_ACCESS_KEY"],
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}),
    )
    try:
        response = client.get_object(Bucket=config["R2_BUCKET"], Key=index["key"])
        return bounded_download(response, index["sha256"])
    finally:
        client.close()


def inspect_archive(archive, index, expected_release=None):
    if hashlib.sha256(archive).hexdigest() != index["sha256"]:
        raise EvidenceError("archive_sha_mismatch")
    # Includes PAX/GNU metadata, padding and trailing gzip data before parsing.
    try:
        validate_archive(archive)
    except ValueError as exc:
        raise EvidenceError("unsafe_archive") from exc
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            if member.name not in {
                "result.json",
                "agent/context.json",
                "benchmark-release.json",
            }:
                continue
            if not member.isfile() or member.size > MAX_JSON:
                raise EvidenceError("harbor_json_too_large")
            file = source.extractfile(member)
            if file is None:
                raise EvidenceError("missing_harbor_json")
            with file:
                raw = file.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise EvidenceError("harbor_json_too_large")
            selected[member.name] = raw
    if "result.json" not in selected:
        raise EvidenceError("missing_standard_result")
    release_verified = False
    if expected_release is not None:
        if (
            "benchmark-release.json" not in selected
            or json.loads(selected["benchmark-release.json"]) != expected_release
        ):
            raise EvidenceError("benchmark_release_mismatch")
        release_verified = True
    result = json.loads(selected["result.json"])
    if (
        expected_release is not None
        and (result.get("agent_result") or {})
        .get("metadata", {})
        .get("benchmark_release")
        != expected_release
    ):
        raise EvidenceError("standard_result_release_mismatch")
    if not isinstance(result, dict) or result.get("exception_info"):
        raise EvidenceError("harbor_trial_failed")
    metadata = (result.get("agent_result") or {}).get("metadata")
    if not isinstance(metadata, dict):
        raise EvidenceError("missing_agent_metadata")
    if "agent/context.json" in selected:
        context = json.loads(selected["agent/context.json"])
        if not isinstance(context, dict) or context.get("metadata") != metadata:
            raise EvidenceError("context_result_metadata_mismatch")
    if metadata.get("submission_sha256") != index["submission_sha256"]:
        raise EvidenceError("submission_sha_mismatch")
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if (
        type(reward) not in (int, float)
        or reward not in (0, 1)
        or reward != index["reward"]
    ):
        raise EvidenceError("verifier_database_mismatch")
    if (
        metadata.get("agent_exit") != 0
        or metadata.get("main_network") != "all-egress-disabled"
        or metadata.get("qsim_transport") != "controller-file-relay"
    ):
        raise EvidenceError("wrong_execution_boundary")
    events = metadata.get("qsim_relay_calls")
    if not isinstance(events, list) or not 4 <= len(events) <= 64:
        raise EvidenceError("missing_real_qsim_calls")
    counts = Counter()
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {"tool", "success"}
            or not isinstance(event["tool"], str)
            or event["tool"] not in TOOLS
            or event["success"] is not True
        ):
            raise EvidenceError("invalid_qsim_call_evidence")
        counts[event["tool"]] += 1
    if set(counts) != TOOLS:
        raise EvidenceError("missing_required_qsim_tool")
    boundary = metadata.get("boundary_probe")
    if not isinstance(boundary, dict):
        raise EvidenceError("missing_boundary_evidence")
    safe_boundary = {
        key: boundary.get(key) is True
        for key in [
            "private_paths_absent",
            "master_credentials_absent",
            "qsim_remote_capability_absent",
        ]
    }
    for target in ("public_internet", "e2b_management"):
        row = boundary.get(target)
        safe_boundary[target + "_blocked"] = (
            isinstance(row, dict)
            and row.get("status") is None
            and bool(row.get("transport"))
            and row.get("transport") != "response"
        )
    if not all(safe_boundary.values()):
        raise EvidenceError("boundary_evidence_failed")
    return {
        "benchmark_release_verified": release_verified,
        "benchmark_release": expected_release if release_verified else None,
        "qsim_tools": [
            {"tool": name, "count": counts[name], "success": True}
            for name in sorted(TOOLS)
        ],
        "main_network": "all-egress-disabled",
        "qsim_transport": "controller-file-relay",
        "boundary": safe_boundary,
        "result_sha256": hashlib.sha256(selected["result.json"]).hexdigest(),
        "context_metadata_matches": "agent/context.json" in selected,
        "formal_hidden_isolation_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--db", type=Path, default=Path("/var/lib/qiqc-staging/data/platform.db")
    )
    parser.add_argument(
        "--r2-env", type=Path, default=Path("/etc/qiqc-staging/api-r2.env")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = {"status": "failed", "schema": "qiqc-cloud-relay-evidence/1"}
    try:
        # Reserve with O_EXCL before any external request; never overwrite input.
        with args.output.open("x") as destination:
            try:
                browser = read_local_json(args.report)
                index = lookup_run(args.db, browser)
                config = read_r2_config(args.r2_env)
                archive = fetch_archive(config, index)
                output.update(
                    inspect_archive(archive, index, browser.get("benchmark_release"))
                )
                output.update(
                    status="passed",
                    batch_id=index["batch_id"],
                    archive_sha256=index["sha256"],
                )
            except Exception as exc:  # noqa: BLE001 — sanitize all external failures
                output["failure"] = (
                    str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
                )
            destination.write(json.dumps(output, indent=2) + "\n")
    except OSError:
        print(
            json.dumps(
                {"status": "failed", "failure": "output_unavailable_or_already_exists"}
            )
        )
        return 2
    print(json.dumps(output))
    return 0 if output["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
